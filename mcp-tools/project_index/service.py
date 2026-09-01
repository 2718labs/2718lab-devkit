"""Deterministic project synchronization and bounded query service."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from devkit_runtime.workspace_authority import WorkspaceRootAuthority

from . import extractors
from .extractors import ParsedExtraction, SourceFile
from .models import (
    CoverageGap,
    IndexCompactionResult,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexRetentionApply,
    IndexRetentionPreview,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    PackageDescriptor,
    PackagePage,
    QueryReceipt,
    QueryResult,
    SnapshotDiff,
    SnapshotFacts,
    SnapshotFile,
    SourceWindow,
)
from .packages import discover_packages, package_descriptor_sort_key
from .registry import WorkspaceRegistry
from .store import ProjectIndexStore, StoreError
from .workspace import is_workspace_id, workspace_identity

_SNAPSHOT_FORMAT_VERSION = "project-index-snapshot-v5"
_MAX_PACKAGE_PAGE_LIMIT = 128
_READ_CHUNK_SIZE = 64 * 1024
_REPARSE_POINT = 0x400
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_INDEX_DATABASE_NAMES = frozenset(
    {
        ".project-index.db",
        ".project-index.sqlite",
        ".project-index.sqlite3",
        "project_index.db",
        "project_index.sqlite",
        "project_index.sqlite3",
    }
)


@dataclass
class _OpenedWorkspaceFile:
    """A verified descriptor held only for the duration of one file read."""

    relative_path: str
    before: os.stat_result
    opened: os.stat_result
    stream: BinaryIO


@dataclass(frozen=True)
class _StandaloneWorkspaceAccess:
    """Temporary standalone-package compatibility binding until R9 packaging."""

    workspace_id: str
    root: Path


class _StandaloneWorkspaceRootAuthority:
    """Keep legacy Project Index-only distributions usable during R1-R8."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    def resolve(self, workspace_id: str) -> _StandaloneWorkspaceAccess:
        return _StandaloneWorkspaceAccess(
            workspace_id, self._registry.resolve(workspace_id)
        )


def _workspace_authority_for(registry: WorkspaceRegistry) -> WorkspaceRootAuthority:
    """Use the runtime authority, retaining standalone compatibility through R8."""

    try:
        from devkit_runtime.workspace_authority import WorkspaceRootAuthority
    except ModuleNotFoundError as exc:
        if exc.name not in {"devkit_runtime", "devkit_runtime.workspace_authority"}:
            raise
        return cast(
            "WorkspaceRootAuthority", _StandaloneWorkspaceRootAuthority(registry)
        )
    return WorkspaceRootAuthority(registry)


def _called_by_runtime_bootstrap() -> bool:
    """Keep the explicit bootstrap call as the only mutating constructor path."""

    frame = inspect.currentframe()
    try:
        while frame is not None:
            if (
                frame.f_code.co_name == "_bootstrap_stores"
                and frame.f_globals.get("__name__") == "devkit_runtime.bootstrap"
            ):
                return True
            frame = frame.f_back
    finally:
        del frame
    return False


class ProjectIndexService:
    """Own a rebuildable graph database and expose bounded deterministic reads."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).resolve(strict=False)
        try:
            if _called_by_runtime_bootstrap():
                self._store = ProjectIndexStore.bootstrap(self._database_path)
            else:
                self._store = ProjectIndexStore.open_prepared(self._database_path)
            self._registry = WorkspaceRegistry(self._store)
            self._workspace_authority = _workspace_authority_for(self._registry)
        except sqlite3.DatabaseError as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index database is corrupt"
            ) from exc
        except StoreError as exc:
            raise IndexError(
                "INDEX_UNAVAILABLE", "project index database is unavailable"
            ) from exc
        except OSError as exc:
            raise IndexError(
                "INDEX_UNAVAILABLE", "project index database is unavailable"
            ) from exc

    @classmethod
    def from_prepared_store(cls, store: ProjectIndexStore) -> ProjectIndexService:
        """Create a non-mutating service over one externally prepared store."""

        service = cls.__new__(cls)
        service._database_path = store.database_path
        service._store = store
        service._registry = WorkspaceRegistry(store)
        service._workspace_authority = _workspace_authority_for(service._registry)
        return service

    @property
    def workspace_authority(self) -> WorkspaceRootAuthority:
        """Return the sole typed authority shared with checkpoint operations."""

        return self._workspace_authority

    def close(self) -> None:
        self._store.close()

    def preview_retention(
        self, protected_snapshot_ids: Sequence[str] = ()
    ) -> IndexRetentionPreview:
        """Return a local-only snapshot retention plan without making changes."""

        return self._store.preview_retention(protected_snapshot_ids)

    def apply_retention(
        self,
        preview_id: str | None,
        protected_snapshot_ids: Sequence[str] = (),
    ) -> IndexRetentionApply:
        """Apply a previously previewed local-only retention plan."""

        return self._store.apply_retention(preview_id, protected_snapshot_ids)

    def compact_storage(
        self, *, allow_full_rewrite: bool = False
    ) -> IndexCompactionResult:
        """Compact released local index pages after explicit authorization."""

        return self._store.compact_storage(allow_full_rewrite=allow_full_rewrite)

    def project_index_register(self, workspace_root: str | Path) -> str:
        """Register the sole accepted filesystem-root input for project indexing."""
        try:
            return self._registry.project_index_register(workspace_root)
        except StoreError as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index workspace registry is corrupt"
            ) from exc

    def revalidate_snapshot(self, workspace_id: str, snapshot_id: str) -> IndexSnapshot:
        """Explicitly promote one migrated path snapshot after a full current check."""
        registered_id, root = self._workspace_for_reference(workspace_id)
        snapshot = self._require_snapshot(
            registered_id, snapshot_id, allow_historical=True
        )
        if snapshot.binding_state != "historical_unverified":
            raise IndexError("INVALID_QUERY", "snapshot is not awaiting revalidation")
        historical_identity = self._store.historical_binding_identity(
            registered_id, snapshot_id
        )
        try:
            current_identity = workspace_identity(root)
        except IndexError as exc:
            raise IndexError(
                "WORKSPACE_REBIND", "workspace registration is no longer valid"
            ) from exc
        if not historical_identity or historical_identity != current_identity:
            raise IndexError(
                "WORKSPACE_REBIND", "workspace registration is no longer valid"
            )
        expected = self._store.file_hashes(snapshot_id)
        current = {
            source.path: source.content_hash
            for source in self._collect_files(
                root,
                self._store.include_paths(snapshot_id),
                error_code="INDEX_STALE",
            )
        }
        if current != expected:
            raise IndexError(
                "INDEX_STALE", "project index snapshot does not match the workspace"
            )
        try:
            return self._store.activate_historical_snapshot(registered_id, snapshot_id)
        except (sqlite3.DatabaseError, StoreError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index historical binding is corrupt"
            ) from exc

    def sync(
        self,
        workspace_id: str,
        include_paths: Sequence[str | Path] | None = None,
    ) -> IndexSnapshot:
        workspace_id, root = self._workspace_for_reference(workspace_id)
        normalized_includes = self._normalize_paths(include_paths)
        files = self._collect_files(root, normalized_includes)
        parsed_by_key: dict[tuple[str, str, str], ParsedExtraction] = {}
        parsed_files: list[ParsedExtraction] = []
        parsed_cache_entries: list[tuple[SourceFile, ParsedExtraction]] = []
        reused_hashes: set[str] = set()
        try:
            for source in files:
                spec = extractors.extractor_for(source)
                cache_key = (
                    source.content_hash,
                    spec.extractor_id,
                    spec.extractor_version,
                )
                parsed = parsed_by_key.get(cache_key)
                if parsed is None:
                    parsed = self._store.get_parse_cache(*cache_key)
                    if parsed is None:
                        parsed = extractors.parse_source(source)
                        parsed_cache_entries.append((source, parsed))
                    else:
                        reused_hashes.add(source.content_hash)
                    parsed_by_key[cache_key] = parsed
                parsed_files.append(parsed)
            extraction = extractors.assemble_extractions(files, parsed_files)
        except (StoreError, ValueError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index parser cache is corrupt"
            ) from exc

        packages, package_gaps = discover_packages(workspace_id, files)
        gaps = tuple(
            sorted(
                (*extraction.gaps, *package_gaps),
                key=lambda gap: (gap.path, gap.code, gap.message),
            )
        )
        manifest_hash = _manifest_hash(normalized_includes, files, packages)
        parser_set_hash = _parser_set_hash(parsed_files)
        head = _git_head(root)
        snapshot_id = _snapshot_identifier(
            workspace_id=workspace_id,
            manifest_hash=manifest_hash,
            parser_set_hash=parser_set_hash,
            head=head,
        )
        snapshot = IndexSnapshot(
            snapshot_id=snapshot_id,
            workspace=workspace_id,
            state=IndexState.INDEX_PARTIAL if gaps else IndexState.INDEX_READY,
            file_count=len(files),
            blob_count=len({source.content_hash for source in files}),
            reused_blob_count=len(reused_hashes),
            node_count=len(extraction.nodes),
            edge_count=len(extraction.edges),
            gap_count=len(gaps),
            manifest_hash=manifest_hash,
            parser_set_hash=parser_set_hash,
            head=head,
            workspace_id=workspace_id,
            packages=packages,
        )
        try:
            return self._store.put_snapshot(
                snapshot,
                include_paths=normalized_includes,
                files=files,
                nodes=extraction.nodes,
                edges=extraction.edges,
                gaps=gaps,
                packages=packages,
                parsed_cache_entries=parsed_cache_entries,
            )
        except (sqlite3.DatabaseError, StoreError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index database rejected the snapshot"
            ) from exc

    def status(
        self,
        workspace_id: str,
        snapshot_id: str | None = None,
        required_paths: Sequence[str | Path] | None = None,
        package_ids: Sequence[str] | None = None,
    ) -> IndexStatus:
        workspace_id, root = self._workspace_for_reference(workspace_id)
        snapshot = (
            self._store.get_snapshot_for_workspace(workspace_id, snapshot_id)
            if snapshot_id
            else self._store.latest_snapshot(workspace_id)
        )
        required = self._normalize_paths(required_paths)
        selected_ids = _normalize_package_ids(package_ids)
        if snapshot is None or snapshot.workspace_id != workspace_id:
            return IndexStatus(
                workspace_id,
                snapshot_id,
                IndexState.INDEX_UNAVAILABLE,
                required_paths=required,
            )
        if snapshot.binding_state != "active":
            return IndexStatus(
                workspace=workspace_id,
                snapshot_id=snapshot.snapshot_id,
                state=IndexState.HISTORICAL_UNVERIFIED,
                required_paths=required,
                binding_state=snapshot.binding_state,
            )

        expected = self._store.file_hashes(snapshot.snapshot_id)
        includes = self._store.include_paths(snapshot.snapshot_id)
        selected_packages = (
            ()
            if selected_ids is None
            else _select_packages(snapshot.packages, selected_ids)
        )
        scope_entries = [(scope, scope) for scope in required]
        scope_entries.extend(
            (package.root_path, package.root_path or package.manifest_path)
            for package in selected_packages
        )
        uncovered = tuple(
            (scope, missing_label)
            for scope, missing_label in scope_entries
            if not _scope_is_fully_included(scope, includes)
        )
        covered = tuple(
            (scope, missing_label)
            for scope, missing_label in scope_entries
            if _scope_is_fully_included(scope, includes)
        )
        if scope_entries and not covered:
            current: Mapping[str, str] = {}
        else:
            verification_includes = (
                _scopes_to_includes(tuple(scope for scope, _ in covered))
                if selected_packages
                else includes
            )
            current = {
                source.path: source.content_hash
                for source in self._collect_files(root, verification_includes)
            }
        snapshot_gaps = self._store.gaps(snapshot.snapshot_id)
        package_roots = tuple(package.root_path for package in selected_packages)
        status_gaps = (
            tuple(
                gap
                for gap in snapshot_gaps
                if _path_in_any_scope(gap.path, package_roots)
            )
            if selected_packages
            else snapshot_gaps
        )
        if scope_entries:
            missing_values = [missing_label for _, missing_label in uncovered]
            changed_values: set[str] = set()
            for scope, missing_label in covered:
                expected_scope = {
                    path for path in expected if _path_in_scope_or_root(path, scope)
                }
                current_scope = {
                    path for path in current if _path_in_scope_or_root(path, scope)
                }
                if not expected_scope:
                    missing_values.append(missing_label)
                    continue
                changed_values.update(
                    path
                    for path in expected_scope.union(current_scope)
                    if expected.get(path) != current.get(path)
                )
            missing = tuple(sorted(set(missing_values)))
            changed = tuple(sorted(changed_values))
        else:
            missing = ()
            changed = tuple(
                sorted(
                    path
                    for path in set(expected).union(current)
                    if expected.get(path) != current.get(path)
                )
            )

        if changed:
            state = IndexState.INDEX_STALE
        elif missing:
            state = IndexState.INDEX_PARTIAL
        elif selected_packages:
            state = IndexState.INDEX_PARTIAL if status_gaps else IndexState.INDEX_READY
        else:
            state = snapshot.state
        return IndexStatus(
            workspace=workspace_id,
            snapshot_id=snapshot.snapshot_id,
            state=state,
            required_paths=required,
            missing_paths=missing,
            changed_paths=changed,
            gaps=status_gaps,
            binding_state=snapshot.binding_state,
        )

    def query(
        self,
        workspace_id: str,
        snapshot_id: str,
        query: str,
        mode: str = "lexical",
        node_kinds: Sequence[str] = (),
        relations: Sequence[str] = (),
        max_nodes: int = 50,
        max_depth: int = 1,
        source_lines: int = 12,
        byte_budget: int = 32768,
        allow_miss_escape: bool = False,
        package_ids: Sequence[str] | None = None,
    ) -> QueryResult:
        workspace_id, root = self._workspace_for_reference(workspace_id)
        snapshot = self._require_snapshot(workspace_id, snapshot_id)
        normalized_mode = str(mode).casefold()
        if normalized_mode not in {"lexical", "graph", "impact"}:
            raise IndexError("INVALID_QUERY", "query mode is not supported")
        if not isinstance(query, str):
            raise IndexError("INVALID_QUERY", "query must be text")
        if not 1 <= max_nodes <= 500:
            raise IndexError("INVALID_QUERY", "max_nodes must be between 1 and 500")
        if not 0 <= max_depth <= 8:
            raise IndexError("INVALID_QUERY", "max_depth must be between 0 and 8")
        if not 0 <= source_lines <= 200:
            raise IndexError("INVALID_QUERY", "source_lines must be between 0 and 200")
        if not 1 <= byte_budget <= 4_194_304:
            raise IndexError(
                "INVALID_QUERY", "byte_budget must be between 1 and 4194304"
            )

        selected_package_ids = _normalize_package_ids(package_ids)
        selector_ids = () if selected_package_ids is None else selected_package_ids
        selected_packages = (
            ()
            if selected_package_ids is None
            else _select_packages(snapshot.packages, selected_package_ids)
        )
        kinds = tuple(sorted({str(value) for value in node_kinds if str(value)}))
        relation_filter = tuple(
            sorted({str(value) for value in relations if str(value)})
        )
        nodes = self._store.nodes(snapshot_id)
        edges = self._store.edges(snapshot_id)
        expected_hashes = self._store.file_hashes(snapshot_id)
        package_roots = tuple(package.root_path for package in selected_packages)
        if selected_packages:
            if any(
                not _scope_is_fully_included(
                    package.root_path, self._store.include_paths(snapshot_id)
                )
                for package in selected_packages
            ):
                raise IndexError(
                    "INDEX_PARTIAL",
                    "project index snapshot does not fully cover selected packages",
                )
            expected_hashes = {
                path: content_hash
                for path, content_hash in expected_hashes.items()
                if _path_in_any_scope(path, package_roots)
            }
            current_files = self._collect_files(
                root,
                _scopes_to_includes(package_roots),
                error_code="INDEX_STALE",
            )
            current_hashes = {
                source.path: source.content_hash for source in current_files
            }
            if current_hashes != expected_hashes:
                raise IndexError(
                    "INDEX_STALE", "project index snapshot does not match the workspace"
                )
            source_text = {source.path: source.text or "" for source in current_files}
        else:
            source_text = self._verified_source_text(root, expected_hashes)
        eligible = tuple(node for node in nodes if not kinds or node.kind in kinds)
        if selected_packages:
            eligible = tuple(
                node
                for node in eligible
                if _path_in_any_scope(node.path, package_roots)
            )
        ranked = _lexical_matches(eligible, query, source_text)
        miss_escape_used = False
        if not ranked and allow_miss_escape and query.strip():
            miss_escape_used = True
            ranked = _miss_escape_matches(eligible, query)

        selected = _expand_graph(
            ranked,
            eligible,
            edges,
            normalized_mode,
            relation_filter,
            max_depth,
        )
        truncated = len(selected) > max_nodes
        selected = selected[:max_nodes]
        selected, used_bytes, node_truncated = _bounded_items(selected, byte_budget, 0)
        truncated = truncated or node_truncated
        selected_node_ids = {node.node_id for node in selected}
        candidate_edges = tuple(
            edge
            for edge in edges
            if edge.source_id in selected_node_ids
            and edge.target_id in selected_node_ids
            and (not relation_filter or edge.relation in relation_filter)
        )
        selected_edges, used_bytes, edge_truncated = _bounded_items(
            candidate_edges, byte_budget, used_bytes
        )
        windows, used_bytes, window_truncated = _source_windows(
            selected,
            source_text,
            expected_hashes,
            source_lines,
            byte_budget,
            used_bytes,
        )
        query_gaps = self._store.gaps(snapshot_id)
        if selected_packages:
            query_gaps = tuple(
                gap for gap in query_gaps if _path_in_any_scope(gap.path, package_roots)
            )
        gaps, _, gap_truncated = _bounded_items(query_gaps, byte_budget, used_bytes)
        truncated = (
            truncated
            or node_truncated
            or edge_truncated
            or window_truncated
            or gap_truncated
        )
        result_state = (
            (IndexState.INDEX_PARTIAL if query_gaps else IndexState.INDEX_READY)
            if selected_packages
            else snapshot.state
        )
        trace_id = _trace_identifier(
            snapshot_id,
            query,
            normalized_mode,
            kinds,
            relation_filter,
            max_nodes,
            max_depth,
            source_lines,
            byte_budget,
            allow_miss_escape,
            selector_ids,
        )
        result = QueryResult(
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            state=result_state,
            nodes=tuple(selected),
            edges=selected_edges,
            source_windows=windows,
            gaps=gaps,
            truncated=truncated,
        )
        receipt = QueryReceipt(
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            query=query,
            mode=normalized_mode,
            node_kinds=kinds,
            relations=relation_filter,
            max_nodes=max_nodes,
            max_depth=max_depth,
            source_lines=source_lines,
            byte_budget=byte_budget,
            allow_miss_escape=bool(allow_miss_escape),
            miss_escape_used=miss_escape_used,
            returned_node_ids=tuple(node.node_id for node in result.nodes),
            returned_edge_ids=tuple(edge.edge_id for edge in result.edges),
            returned_source_windows=tuple(
                (window.path, window.start_line, window.end_line, window.content_hash)
                for window in result.source_windows
            ),
            gaps=result.gaps,
            truncated=result.truncated,
            package_ids=selector_ids,
        )
        try:
            self._store.put_query_receipt(receipt)
        except (sqlite3.DatabaseError, StoreError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index rejected the query receipt"
            ) from exc
        return result

    def get_query_receipt(self, trace_id: str) -> QueryReceipt:
        try:
            receipt = self._store.get_query_receipt(str(trace_id))
        except (sqlite3.DatabaseError, StoreError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index query receipt is corrupt"
            ) from exc
        if receipt is None:
            raise IndexError("NOT_FOUND", "project index query receipt was not found")
        return receipt

    def query_receipt(self, trace_id: str) -> QueryReceipt:
        """Compatibility alias for fetching a successful query receipt."""
        return self.get_query_receipt(trace_id)

    def host_attestation_material(
        self,
        workspace_id: str,
        *,
        snapshot_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, str]:
        """Project current persisted facts into path-free Host-side digests."""

        registered_id, root = self._workspace_for_reference(workspace_id)
        root_identity_hash = _opaque_hash({"root_identity": workspace_identity(root)})
        workspace_binding_hash = _opaque_hash(
            {
                "workspace_id": registered_id,
                "root_identity_hash": root_identity_hash,
            }
        )
        material = {
            "workspace_id": registered_id,
            "root_identity_hash": root_identity_hash,
            "workspace_binding_hash": workspace_binding_hash,
        }
        if snapshot_id is None:
            if trace_id is not None:
                raise IndexError("INVALID_QUERY", "query attestation needs a snapshot")
            return material
        snapshot = self.assert_current(registered_id, snapshot_id)
        if (
            not snapshot.head
            or not snapshot.manifest_hash
            or not snapshot.parser_set_hash
        ):
            raise IndexError("INDEX_STALE", "snapshot has no provable head")
        head_hash = _opaque_hash({"head": snapshot.head})
        snapshot_attestation_hash = _opaque_hash(
            {
                **material,
                "snapshot_id": snapshot.snapshot_id,
                "head_hash": head_hash,
                "manifest_hash": snapshot.manifest_hash,
                "parser_set_hash": snapshot.parser_set_hash,
            }
        )
        material.update(
            {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_attestation_hash": snapshot_attestation_hash,
                "head_hash": head_hash,
                "manifest_hash": snapshot.manifest_hash,
                "parser_set_hash": snapshot.parser_set_hash,
            }
        )
        if trace_id is None:
            return material
        receipt = self.get_query_receipt(trace_id)
        if receipt.snapshot_id != snapshot.snapshot_id:
            raise IndexError("INDEX_STALE", "query receipt snapshot changed")
        query_projection = self._public_query_projection(registered_id, receipt)
        index_context_hash = _opaque_hash(query_projection)
        query_receipt_hash = _opaque_hash(
            {
                "schema": "2718lab-devkit/project-index-query-receipt-binding-v1",
                "receipt": asdict(receipt),
                "index_context_hash": index_context_hash,
            }
        )
        material.update(
            {
                "query_receipt_hash": query_receipt_hash,
                "index_context_hash": index_context_hash,
            }
        )
        return material

    def _public_query_projection(
        self, workspace_id: str, receipt: QueryReceipt
    ) -> dict[str, object]:
        """Rebuild the exact bounded public query facts hashed for the Host."""

        nodes_by_id = {
            node.node_id: node for node in self._store.nodes(receipt.snapshot_id)
        }
        edges_by_id = {
            edge.edge_id: edge for edge in self._store.edges(receipt.snapshot_id)
        }
        try:
            nodes = [
                _public_query_node(nodes_by_id[node_id])
                for node_id in receipt.returned_node_ids
            ]
            edges = [
                _public_query_edge(edges_by_id[edge_id])
                for edge_id in receipt.returned_edge_ids
            ]
        except KeyError as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index query receipt is corrupt"
            ) from exc
        return {
            "workspace_id": workspace_id,
            "snapshot_id": receipt.snapshot_id,
            "trace_id": receipt.trace_id,
            "nodes": nodes,
            "edges": edges,
            "source_windows": [
                {
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "content_hash": content_hash,
                }
                for path, start_line, end_line, content_hash in receipt.returned_source_windows
            ],
            "gaps": [
                {"path": gap.path, "code": gap.code, "message": gap.message}
                for gap in receipt.gaps
            ],
            "truncated": receipt.truncated,
        }

    def diff(
        self, workspace_id: str, from_snapshot_id: str, to_snapshot_id: str
    ) -> SnapshotDiff:
        """Compare two active snapshots belonging to one registered workspace."""
        workspace_id, _ = self._workspace_for_reference(workspace_id)
        self._require_snapshot(workspace_id, from_snapshot_id)
        self._require_snapshot(workspace_id, to_snapshot_id)
        before_files = self._store.file_hashes(from_snapshot_id)
        after_files = self._store.file_hashes(to_snapshot_id)
        before_paths = set(before_files)
        after_paths = set(after_files)
        before_nodes = {node.node_id for node in self._store.nodes(from_snapshot_id)}
        after_nodes = {node.node_id for node in self._store.nodes(to_snapshot_id)}
        before_edges = {edge.edge_id for edge in self._store.edges(from_snapshot_id)}
        after_edges = {edge.edge_id for edge in self._store.edges(to_snapshot_id)}
        return SnapshotDiff(
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
            added_paths=tuple(sorted(after_paths - before_paths)),
            removed_paths=tuple(sorted(before_paths - after_paths)),
            changed_paths=tuple(
                sorted(
                    path
                    for path in before_paths & after_paths
                    if before_files[path] != after_files[path]
                )
            ),
            added_node_ids=tuple(sorted(after_nodes - before_nodes)),
            removed_node_ids=tuple(sorted(before_nodes - after_nodes)),
            added_edge_ids=tuple(sorted(after_edges - before_edges)),
            removed_edge_ids=tuple(sorted(before_edges - after_edges)),
        )

    def assert_current(
        self,
        workspace_id: str,
        snapshot_id: str,
        required_paths: Sequence[str | Path] | None = None,
    ) -> IndexSnapshot:
        workspace_id, root = self._workspace_for_reference(workspace_id)
        if required_paths is None:
            snapshot = self._require_snapshot(workspace_id, snapshot_id)
            captured_files = self._collect_files(
                root,
                self._store.include_paths(snapshot.snapshot_id),
                error_code="INDEX_STALE",
            )
            return self._assert_current_with_files(
                workspace_id, snapshot_id, captured_files
            )
        current = self.status(workspace_id, snapshot_id, required_paths)
        if current.state is IndexState.INDEX_UNAVAILABLE:
            raise IndexError("NOT_FOUND", "project index snapshot was not found")
        if current.state is IndexState.INDEX_STALE or current.missing_paths:
            raise IndexError(
                "INDEX_STALE", "project index snapshot does not match the workspace"
            )
        return self._require_snapshot(workspace_id, snapshot_id)

    def _assert_current_with_files(
        self,
        workspace_id: str,
        snapshot_id: str,
        captured_files: Sequence[SourceFile],
    ) -> IndexSnapshot:
        snapshot = self._require_snapshot(workspace_id, snapshot_id)
        expected = self._store.file_hashes(snapshot.snapshot_id)
        current = {source.path: source.content_hash for source in captured_files}
        if any(
            expected.get(path) != current.get(path)
            for path in set(expected).union(current)
        ):
            raise IndexError(
                "INDEX_STALE", "project index snapshot does not match the workspace"
            )
        return snapshot

    def snapshot_facts(self, workspace_id: str, snapshot_id: str) -> SnapshotFacts:
        workspace_id, _ = self._workspace_for_reference(workspace_id)
        snapshot = self._require_snapshot(
            workspace_id, snapshot_id, allow_historical=True
        )
        return SnapshotFacts(
            snapshot=snapshot,
            file_hashes=tuple(sorted(self._store.file_hashes(snapshot_id).items())),
            nodes=tuple(
                sorted(
                    self._store.nodes(snapshot_id),
                    key=lambda node: (node.path, node.node_id),
                )
            ),
            edges=tuple(
                sorted(self._store.edges(snapshot_id), key=lambda edge: edge.edge_id)
            ),
            gaps=tuple(
                sorted(
                    self._store.gaps(snapshot_id),
                    key=lambda gap: (gap.path, gap.code, gap.message),
                )
            ),
            packages=snapshot.packages,
        )

    def package_page(
        self,
        workspace_id: str,
        snapshot_id: str,
        *,
        offset: int,
        limit: int,
    ) -> PackagePage:
        """Read one canonical descriptor page from an explicit persisted snapshot.

        Pages are bounded to 128 descriptors so a caller can retrieve an
        arbitrarily large package catalog without an unbounded result.
        """

        normalized_offset, normalized_limit = _normalize_package_page_parameters(
            offset, limit
        )
        workspace_id, _ = self._workspace_for_reference(workspace_id)
        snapshot = self._require_snapshot(
            workspace_id, snapshot_id, allow_historical=True
        )
        packages = tuple(sorted(snapshot.packages, key=package_descriptor_sort_key))
        total_count = len(packages)
        if normalized_offset > total_count:
            raise IndexError(
                "INVALID_QUERY", "package page offset exceeds snapshot package count"
            )
        end = min(total_count, normalized_offset + normalized_limit)
        return PackagePage(
            snapshot_id=snapshot.snapshot_id,
            offset=normalized_offset,
            limit=normalized_limit,
            total_count=total_count,
            packages=packages[normalized_offset:end],
            next_offset=end if end < total_count else None,
        )

    def read_snapshot_files(
        self,
        workspace_id: str,
        snapshot_id: str,
        paths: Sequence[str | Path],
        *,
        byte_budget: int,
    ) -> tuple[SnapshotFile, ...]:
        if type(byte_budget) is not int or byte_budget <= 0:
            raise IndexError("INVALID_QUERY", "byte_budget must be a positive integer")
        normalized_paths = self._normalize_read_paths(paths)
        workspace_id, root = self._workspace_for_reference(workspace_id)
        snapshot = self._require_snapshot(workspace_id, snapshot_id)
        expected_hashes = self._store.file_hashes(snapshot_id)
        for relative_path in normalized_paths:
            if relative_path not in expected_hashes:
                raise IndexError(
                    "NOT_FOUND", "project index source file was not snapshotted"
                )
        _prevalidate_requested_file_sizes(root, normalized_paths, byte_budget)
        captured_hashes, captured_bodies = self._scan_snapshot_files(
            root,
            self._store.include_paths(snapshot.snapshot_id),
            frozenset(normalized_paths),
            byte_budget,
        )
        if captured_hashes != expected_hashes:
            raise IndexError(
                "INDEX_STALE", "project index snapshot does not match the workspace"
            )
        files: list[SnapshotFile] = []
        for relative_path in normalized_paths:
            expected_hash = expected_hashes.get(relative_path)
            if expected_hash is None:  # Defensive: membership was checked pre-scan.
                raise IndexError(
                    "NOT_FOUND", "project index source file was not snapshotted"
                )
            body = captured_bodies.get(relative_path)
            if body is None or captured_hashes.get(relative_path) != expected_hash:
                raise IndexError(
                    "INDEX_STALE", "project index source hash no longer matches"
                )
            files.append(SnapshotFile(relative_path, expected_hash, body))
        return tuple(files)

    def _scan_snapshot_files(
        self,
        root: Path,
        include_paths: Sequence[str],
        requested: frozenset[str],
        byte_budget: int,
    ) -> tuple[dict[str, str], dict[str, bytes]]:
        hashes: dict[str, str] = {}
        bodies: dict[str, bytes] = {}
        used = 0
        database_names = {
            os.path.normcase(str(self._database_path)),
            os.path.normcase(f"{self._database_path}-wal"),
            os.path.normcase(f"{self._database_path}-shm"),
        }
        for current_root, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name.casefold() not in _IGNORED_DIRECTORIES
                and not _unsafe_path(current / name)
                and _path_may_match(
                    (current / name).relative_to(root).as_posix(), include_paths
                )
            )
            for name in sorted(file_names):
                candidate = current / name
                relative = candidate.relative_to(root).as_posix()
                if (
                    not _path_matches(relative, include_paths)
                    or _unsafe_path(candidate)
                    or _ignored_database(candidate, database_names)
                ):
                    continue
                retain = relative in requested
                digest, body, size = _stream_workspace_file(
                    root, candidate, retain, byte_budget - used
                )
                if retain:
                    used += size
                    if used > byte_budget:
                        raise IndexError("INVALID_QUERY", "byte_budget exceeded")
                    if body is None:
                        raise IndexError(
                            "INDEX_STALE", "project index source disappeared"
                        )
                    bodies[relative] = body
                hashes[relative] = digest
        return hashes, bodies

    def _require_snapshot(
        self, workspace_id: str, snapshot_id: str, *, allow_historical: bool = False
    ) -> IndexSnapshot:
        snapshot = self._store.get_snapshot_for_workspace(workspace_id, snapshot_id)
        if snapshot is None:
            raise IndexError("NOT_FOUND", "project index snapshot was not found")
        if snapshot.binding_state != "active" and not allow_historical:
            raise IndexError(
                "HISTORICAL_UNVERIFIED", "project index snapshot requires revalidation"
            )
        return snapshot

    def _workspace_for_reference(self, workspace_id: str) -> tuple[str, Path]:
        """Resolve a previously registered opaque workspace identifier only."""
        if not isinstance(workspace_id, str) or not is_workspace_id(workspace_id):
            raise IndexError("WORKSPACE_UNREGISTERED", "workspace is not registered")
        return workspace_id, self.workspace_authority.resolve(workspace_id).root

    def _normalize_paths(self, paths: Sequence[str | Path] | None) -> tuple[str, ...]:
        if paths is None:
            return ()
        normalized: set[str] = set()
        for raw_path in paths:
            raw_value = str(raw_path)
            value = raw_value.replace("\\", "/")
            pure = PurePosixPath(value)
            windows_path = PureWindowsPath(raw_value)
            if (
                not value
                or Path(raw_value).is_absolute()
                or pure.is_absolute()
                or windows_path.is_absolute()
                or bool(windows_path.drive)
                or any(part in {"", ".."} for part in pure.parts)
            ):
                raise IndexError(
                    "SCOPE_ESCAPE", "index path must stay inside the workspace"
                )
            clean = pure.as_posix()
            if clean == ".":
                return ()
            normalized.add(clean)
        return tuple(sorted(normalized))

    def _normalize_read_paths(self, paths: Sequence[str | Path]) -> tuple[str, ...]:
        if isinstance(paths, (str, bytes)):
            raise IndexError("SCOPE_ESCAPE", "index paths must be a sequence")
        try:
            supplied = tuple(paths)
        except TypeError as exc:
            raise IndexError("SCOPE_ESCAPE", "index paths must be a sequence") from exc
        if not supplied:
            raise IndexError("SCOPE_ESCAPE", "index paths must not be empty")
        normalized = self._normalize_paths(supplied)
        if len(normalized) != len(supplied) or tuple(normalized) != tuple(
            str(value).replace("\\", "/") for value in supplied
        ):
            raise IndexError("SCOPE_ESCAPE", "index paths must be unique and ordered")
        for path in normalized:
            parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
            if (
                any(part in _IGNORED_DIRECTORIES or ":" in part for part in parts)
                or parts[-1] in _INDEX_DATABASE_NAMES
            ):
                raise IndexError("SCOPE_ESCAPE", "index path is generated or internal")
        return normalized

    def _collect_files(
        self,
        root: Path,
        include_paths: Sequence[str],
        *,
        error_code: str = "INDEX_UNAVAILABLE",
    ) -> tuple[SourceFile, ...]:
        files: list[SourceFile] = []
        database_names = {
            os.path.normcase(str(self._database_path)),
            os.path.normcase(f"{self._database_path}-wal"),
            os.path.normcase(f"{self._database_path}-shm"),
        }
        for current_root, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name.casefold() not in _IGNORED_DIRECTORIES
                and not _unsafe_path(current / name)
                and _path_may_match(
                    (current / name).relative_to(root).as_posix(), include_paths
                )
            )
            for name in sorted(file_names):
                full_path = current / name
                relative_path = full_path.relative_to(root).as_posix()
                if not _path_matches(relative_path, include_paths):
                    continue
                if _unsafe_path(full_path) or _ignored_database(
                    full_path, database_names
                ):
                    continue
                try:
                    data = _capture_regular_file(root, full_path)
                except IndexError as exc:
                    if exc.code == error_code:
                        raise
                    raise IndexError(
                        error_code, f"workspace file is unreadable: {relative_path}"
                    ) from exc
                content_hash = _content_hash(data)
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = None
                files.append(SourceFile(relative_path, content_hash, data, text))
        return tuple(sorted(files, key=lambda source: source.path))

    def _verified_source_text(
        self, root: Path, expected_hashes: Mapping[str, str]
    ) -> Mapping[str, str]:
        verified: dict[str, str] = {}
        for relative_path, expected_hash in expected_hashes.items():
            full_path = _verified_workspace_file(root, relative_path)
            try:
                data = full_path.read_bytes()
            except OSError as exc:
                raise IndexError(
                    "INDEX_STALE", "project index source file is unavailable"
                ) from exc
            if _content_hash(data) != expected_hash:
                raise IndexError(
                    "INDEX_STALE", "project index source hash no longer matches"
                )
            try:
                verified[relative_path] = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                verified[relative_path] = ""
        return verified


def _manifest_hash(
    include_paths: Sequence[str],
    files: Sequence[SourceFile],
    packages: Sequence[PackageDescriptor],
) -> str:
    return _hash_json(
        {
            "files": tuple(
                (source.path, source.content_hash, len(source.data)) for source in files
            ),
            "include_paths": tuple(include_paths),
            "packages": tuple(
                (
                    package.package_id,
                    package.ecosystem,
                    package.name,
                    package.root_path,
                    package.manifest_path,
                    package.manifest_hash,
                )
                for package in sorted(packages, key=package_descriptor_sort_key)
            ),
        }
    )


def _parser_set_hash(parsed: Sequence[ParsedExtraction]) -> str:
    return _hash_json(
        tuple(sorted({(item.extractor_id, item.extractor_version) for item in parsed}))
    )


def _snapshot_identifier(
    *,
    workspace_id: str,
    manifest_hash: str,
    parser_set_hash: str,
    head: str | None,
) -> str:
    return _hash_json(
        {
            "format": _SNAPSHOT_FORMAT_VERSION,
            "head": head,
            "manifest_hash": manifest_hash,
            "parser_set_hash": parser_set_hash,
            "workspace_id": workspace_id,
        }
    )


def _git_head(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        return None
    return head.casefold()


def _trace_identifier(
    snapshot_id: str,
    query: str,
    mode: str,
    node_kinds: Sequence[str],
    relations: Sequence[str],
    max_nodes: int,
    max_depth: int,
    source_lines: int,
    byte_budget: int,
    allow_miss_escape: bool,
    package_ids: Sequence[str],
) -> str:
    return _hash_json(
        {
            "snapshot_id": snapshot_id,
            "query": query,
            "mode": mode,
            "node_kinds": tuple(node_kinds),
            "relations": tuple(relations),
            "max_nodes": max_nodes,
            "max_depth": max_depth,
            "source_lines": source_lines,
            "byte_budget": byte_budget,
            "allow_miss_escape": allow_miss_escape,
            "package_ids": tuple(package_ids),
        }
    )


def _hash_json(value: object) -> str:
    data = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _content_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _unsafe_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return True


def _verified_workspace_file(root: Path, relative_path: str) -> Path:
    if _unsafe_path(root):
        raise IndexError("INDEX_STALE", "project index workspace is no longer safe")
    candidate = root
    for part in PurePosixPath(relative_path).parts:
        candidate /= part
        if _unsafe_path(candidate):
            raise IndexError(
                "INDEX_STALE", "project index source path is no longer safe"
            )
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise IndexError(
            "INDEX_STALE", "project index source path left the workspace"
        ) from exc
    return candidate


def _capture_regular_file(root: Path, candidate: Path) -> bytes:
    try:
        relative_path = candidate.relative_to(root).as_posix()
        candidate = _verified_workspace_file(root, relative_path)
        before = candidate.lstat()
        if _unsafe_stat_result(before) or not stat.S_ISREG(before.st_mode):
            raise IndexError("INDEX_STALE", "project index source path is not regular")
        descriptor = os.open(candidate, _read_only_open_flags())
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            opened = os.fstat(stream.fileno())
            if _unsafe_stat_result(opened) or not stat.S_ISREG(opened.st_mode):
                raise IndexError(
                    "INDEX_STALE", "project index source path is not regular"
                )
            body = stream.read()
        candidate = _verified_workspace_file(root, relative_path)
        after = candidate.lstat()
    except IndexError:
        raise
    except OSError as exc:
        raise IndexError(
            "INDEX_STALE", "project index source file is unavailable"
        ) from exc
    if _file_identity(before) != _file_identity(opened) or _file_identity(
        opened
    ) != _file_identity(after):
        raise IndexError(
            "INDEX_STALE", "project index source file changed while reading"
        )
    if _unsafe_stat_result(after) or not stat.S_ISREG(after.st_mode):
        raise IndexError("INDEX_STALE", "project index source path is not regular")
    return body


def _prevalidate_requested_file_sizes(
    root: Path, requested_paths: Sequence[str], byte_budget: int
) -> None:
    """Reject a requested aggregate that cannot fit before any body is read."""

    used = 0
    for relative_path in requested_paths:
        candidate = root.joinpath(*PurePosixPath(relative_path).parts)
        opened_file = _open_workspace_file(root, candidate)
        try:
            size = opened_file.opened.st_size
            if size > byte_budget - used:
                raise IndexError("INVALID_QUERY", "byte_budget exceeded")
            used += size
        finally:
            _close_workspace_file(opened_file)


def _stream_workspace_file(
    root: Path, candidate: Path, retain: bool, remaining_budget: int
) -> tuple[str, bytes | None, int]:
    opened_file = _open_workspace_file(root, candidate)
    try:
        return _read_open_workspace_file(root, opened_file, retain, remaining_budget)
    finally:
        _close_workspace_file(opened_file)


def _open_workspace_file(root: Path, candidate: Path) -> _OpenedWorkspaceFile:
    stream: BinaryIO | None = None
    try:
        relative_path = candidate.relative_to(root).as_posix()
        candidate = _verified_workspace_file(root, relative_path)
        before = candidate.lstat()
        if _unsafe_stat_result(before) or not stat.S_ISREG(before.st_mode):
            raise IndexError("INDEX_STALE", "project index source path is not regular")
        descriptor = os.open(candidate, _read_only_open_flags())
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        opened = os.fstat(stream.fileno())
        if _unsafe_stat_result(opened) or not stat.S_ISREG(opened.st_mode):
            raise IndexError("INDEX_STALE", "project index source path is not regular")
        if _file_identity(before) != _file_identity(opened):
            raise IndexError(
                "INDEX_STALE", "project index source file changed while opening"
            )
        return _OpenedWorkspaceFile(relative_path, before, opened, stream)
    except IndexError:
        if stream is not None:
            _close_workspace_stream(stream)
        raise
    except (OSError, ValueError) as exc:
        if stream is not None:
            _close_workspace_stream(stream)
        raise IndexError(
            "INDEX_STALE", "project index source file is unavailable"
        ) from exc


def _read_open_workspace_file(
    root: Path,
    opened_file: _OpenedWorkspaceFile,
    retain: bool,
    remaining_budget: int,
) -> tuple[str, bytes | None, int]:
    try:
        _assert_opened_workspace_file_current(root, opened_file)
        size = opened_file.opened.st_size
        if retain and size > remaining_budget:
            raise IndexError("INVALID_QUERY", "byte_budget exceeded")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if retain else None
        retained = 0
        remaining = size
        while remaining:
            read_size = min(_READ_CHUNK_SIZE, remaining)
            chunk = opened_file.stream.read(read_size)
            if not chunk or len(chunk) > read_size:
                raise IndexError(
                    "INDEX_STALE", "project index source file changed while reading"
                )
            digest.update(chunk)
            remaining -= len(chunk)
            if chunks is not None:
                if retained + len(chunk) > remaining_budget:
                    raise IndexError("INVALID_QUERY", "byte_budget exceeded")
                retained += len(chunk)
                chunks.append(chunk)
        if opened_file.stream.read(1):
            raise IndexError(
                "INDEX_STALE", "project index source file changed while reading"
            )
        _assert_opened_workspace_file_current(root, opened_file)
    except IndexError:
        raise
    except (OSError, ValueError) as exc:
        raise IndexError(
            "INDEX_STALE", "project index source file is unavailable"
        ) from exc
    body = None if chunks is None else b"".join(chunks)
    return f"sha256:{digest.hexdigest()}", body, size


def _assert_opened_workspace_file_current(
    root: Path, opened_file: _OpenedWorkspaceFile
) -> None:
    candidate = _verified_workspace_file(root, opened_file.relative_path)
    after = candidate.lstat()
    if _unsafe_stat_result(after) or not stat.S_ISREG(after.st_mode):
        raise IndexError("INDEX_STALE", "project index source path is not regular")
    if _file_identity(opened_file.before) != _file_identity(
        opened_file.opened
    ) or _file_identity(opened_file.opened) != _file_identity(after):
        raise IndexError(
            "INDEX_STALE", "project index source file changed while reading"
        )


def _close_workspace_file(opened_file: _OpenedWorkspaceFile) -> None:
    _close_workspace_stream(opened_file.stream)


def _close_workspace_stream(stream: BinaryIO) -> None:
    try:
        stream.close()
    except OSError:
        pass


def _read_only_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_identity(path_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
    )


def _unsafe_stat_result(path_stat: os.stat_result) -> bool:
    return bool(getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _ignored_database(path: Path, exact_names: set[str]) -> bool:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    name = path.name.casefold()
    return (
        normalized in exact_names
        or name in _INDEX_DATABASE_NAMES
        or any(
            name == f"{database_name}{suffix}"
            for database_name in _INDEX_DATABASE_NAMES
            for suffix in ("-wal", "-shm")
        )
    )


def _path_matches(path: str, include_paths: Sequence[str]) -> bool:
    return not include_paths or any(
        path == include or path.startswith(f"{include}/") for include in include_paths
    )


def _path_in_scope(path: str, scope: str) -> bool:
    return path == scope or path.startswith(f"{scope}/")


def _scope_is_fully_included(scope: str, include_paths: Sequence[str]) -> bool:
    """Whether a captured include root contains every possible path in scope."""

    if not include_paths:
        return True
    if not scope:
        return False
    return any(
        scope == include or scope.startswith(f"{include}/") for include in include_paths
    )


def _path_in_scope_or_root(path: str, scope: str) -> bool:
    return not scope or _path_in_scope(path, scope)


def _path_in_any_scope(path: str, scopes: Sequence[str]) -> bool:
    return any(_path_in_scope_or_root(path, scope) for scope in scopes)


def _scopes_to_includes(scopes: Sequence[str]) -> tuple[str, ...]:
    """Canonicalize a union of package roots for bounded workspace scanning."""

    return () if any(not scope for scope in scopes) else tuple(sorted(set(scopes)))


def _normalize_package_ids(
    package_ids: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if package_ids is None:
        return None
    if isinstance(package_ids, (str, bytes)):
        raise IndexError("INVALID_QUERY", "package ids must be an ordered sequence")
    try:
        supplied = tuple(package_ids)
    except TypeError as exc:
        raise IndexError(
            "INVALID_QUERY", "package ids must be an ordered sequence"
        ) from exc
    if not supplied:
        raise IndexError("INVALID_QUERY", "package ids must not be empty")
    if any(
        not isinstance(package_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", package_id) is None
        for package_id in supplied
    ):
        raise IndexError("INVALID_QUERY", "package ids must be descriptor identifiers")
    if supplied != tuple(sorted(set(supplied))):
        raise IndexError("INVALID_QUERY", "package ids must be unique and ordered")
    return supplied


def _normalize_package_page_parameters(offset: int, limit: int) -> tuple[int, int]:
    if type(offset) is not int or offset < 0:
        raise IndexError(
            "INVALID_QUERY", "package page offset must be a non-negative integer"
        )
    if type(limit) is not int or limit <= 0 or limit > _MAX_PACKAGE_PAGE_LIMIT:
        raise IndexError(
            "INVALID_QUERY", "package page limit must be between 1 and 128"
        )
    return offset, limit


def _select_packages(
    packages: Sequence[PackageDescriptor], package_ids: Sequence[str]
) -> tuple[PackageDescriptor, ...]:
    descriptors = {package.package_id: package for package in packages}
    missing = tuple(
        package_id for package_id in package_ids if package_id not in descriptors
    )
    if missing:
        raise IndexError("NOT_FOUND", "project index package was not found")
    return tuple(descriptors[package_id] for package_id in package_ids)


def _path_may_match(path: str, include_paths: Sequence[str]) -> bool:
    return not include_paths or any(
        path == include
        or path.startswith(f"{include}/")
        or include.startswith(f"{path}/")
        for include in include_paths
    )


def _lexical_matches(
    nodes: Sequence[IndexNode], query: str, source_text: Mapping[str, str]
) -> tuple[IndexNode, ...]:
    terms = tuple(
        token.casefold() for token in re.findall(r"[\w.-]+", query, flags=re.UNICODE)
    )
    if not terms:
        return tuple(nodes)
    ranked: list[tuple[int, tuple[object, ...], IndexNode]] = []
    for node in nodes:
        direct = (
            f"{node.name}\n{node.qualified_name}\n{node.path}\n{node.kind}".casefold()
        )
        lines = source_text.get(node.path, "").splitlines()
        start = max(0, node.start_line - 1)
        end = max(start, node.end_line)
        source = "\n".join(lines[start:end]).casefold()
        if all(term in direct for term in terms):
            score = 0 if all(term in node.name.casefold() for term in terms) else 1
        elif all(term in source for term in terms):
            score = 2
        else:
            continue
        ranked.append((score, _node_order(node), node))
    return tuple(
        item[2] for item in sorted(ranked, key=lambda item: (item[0], item[1]))
    )


def _miss_escape_matches(
    nodes: Sequence[IndexNode], query: str
) -> tuple[IndexNode, ...]:
    pieces = tuple(
        part for part in re.split(r"[^\w]+", query.casefold()) if len(part) >= 2
    )
    if not pieces:
        return ()
    return tuple(
        node
        for node in nodes
        if any(
            piece in f"{node.name} {node.qualified_name} {node.path}".casefold()
            for piece in pieces
        )
    )


def _expand_graph(
    seeds: Sequence[IndexNode],
    nodes: Sequence[IndexNode],
    edges: Sequence[IndexEdge],
    mode: str,
    relations: Sequence[str],
    max_depth: int,
) -> list[IndexNode]:
    if mode == "lexical" or not seeds:
        return list(seeds)
    node_by_id = {node.node_id: node for node in nodes}
    allowed_edges = tuple(
        edge for edge in edges if not relations or edge.relation in relations
    )
    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    for edge in allowed_edges:
        if edge.source_id in node_by_id and edge.target_id in node_by_id:
            outgoing.setdefault(edge.source_id, []).append(edge.target_id)
            incoming.setdefault(edge.target_id, []).append(edge.source_id)
    for adjacency in (outgoing, incoming):
        for node_id in adjacency:
            adjacency[node_id].sort(key=lambda value: _node_order(node_by_id[value]))

    ordered: list[IndexNode] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for seed in seeds:
        if seed.node_id not in seen:
            seen.add(seed.node_id)
            ordered.append(seed)
            queue.append((seed.node_id, 0))
    while queue:
        node_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        neighbors = (
            incoming.get(node_id, ())
            if mode == "impact"
            else (
                *outgoing.get(node_id, ()),
                *incoming.get(node_id, ()),
            )
        )
        for neighbor_id in neighbors:
            if neighbor_id in seen:
                continue
            seen.add(neighbor_id)
            ordered.append(node_by_id[neighbor_id])
            queue.append((neighbor_id, depth + 1))
    return ordered


def _source_windows(
    nodes: Sequence[IndexNode],
    source_text: Mapping[str, str],
    expected_hashes: Mapping[str, str],
    source_lines: int,
    byte_budget: int,
    used_bytes: int,
) -> tuple[tuple[SourceWindow, ...], int, bool]:
    if source_lines == 0:
        return (), used_bytes, False
    windows: list[SourceWindow] = []
    seen: set[tuple[str, int, int]] = set()
    truncated = False
    for node in nodes:
        lines = source_text.get(node.path, "").splitlines(keepends=True)
        if not lines:
            continue
        start = max(1, min(node.start_line, len(lines)))
        end = min(len(lines), start + source_lines - 1)
        key = (node.path, start, end)
        if key in seen:
            continue
        seen.add(key)
        text = "".join(lines[start - 1 : end])
        window = SourceWindow(node.path, start, end, text, expected_hashes[node.path])
        size = _encoded_size(window)
        if used_bytes + size > byte_budget:
            truncated = True
            continue
        used_bytes += size
        windows.append(window)
    return tuple(windows), used_bytes, truncated


def _bounded_items(
    items: Sequence[IndexNode] | Sequence[IndexEdge] | Sequence[CoverageGap],
    byte_budget: int,
    used_bytes: int,
) -> tuple[tuple, int, bool]:
    selected: list[object] = []
    truncated = False
    for item in items:
        size = _encoded_size(item)
        if used_bytes + size > byte_budget:
            truncated = True
            continue
        used_bytes += size
        selected.append(item)
    return tuple(selected), used_bytes, truncated


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(
            asdict(cast(Any, value)),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _opaque_hash(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


def _public_query_node(node: IndexNode) -> dict[str, object]:
    """Mirror the stable public Project Index node projection."""

    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "path": node.path,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "content_hash": node.content_hash,
        "attributes": [
            {"name": name, "value": value} for name, value in node.attributes
        ],
        "extractor_id": node.extractor_id,
        "extractor_version": node.extractor_version,
        "provenance": node.provenance,
    }


def _public_query_edge(edge: IndexEdge) -> dict[str, object]:
    """Mirror the stable public Project Index edge projection."""

    return {
        "edge_id": edge.edge_id,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "relation": edge.relation,
        "path": edge.path,
        "start_line": edge.start_line,
        "end_line": edge.end_line,
        "content_hash": edge.content_hash,
        "extractor_id": edge.extractor_id,
        "extractor_version": edge.extractor_version,
        "provenance": edge.provenance,
    }


def _node_order(node: IndexNode) -> tuple[object, ...]:
    return (
        node.path,
        node.start_line,
        node.end_line,
        node.kind,
        node.qualified_name,
        node.node_id,
    )
