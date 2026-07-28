"""Deterministic project synchronization and bounded query service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections import deque
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping, Sequence

from . import extractors
from .extractors import ParsedExtraction, SourceFile
from .models import (
    CoverageGap,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    QueryReceipt,
    QueryResult,
    SnapshotDiff,
    SnapshotFacts,
    SnapshotFile,
    SourceWindow,
)
from .store import ProjectIndexStore, StoreError


_SNAPSHOT_FORMAT_VERSION = "project-index-snapshot-v2"
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


class ProjectIndexService:
    """Own a rebuildable graph database and expose bounded deterministic reads."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).resolve(strict=False)
        try:
            self._store = ProjectIndexStore(self._database_path)
        except sqlite3.DatabaseError as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index database is corrupt"
            ) from exc
        except OSError as exc:
            raise IndexError(
                "INDEX_UNAVAILABLE", "project index database is unavailable"
            ) from exc

    def close(self) -> None:
        self._store.close()

    def sync(
        self, workspace: str | Path, include_paths: Sequence[str | Path] | None = None
    ) -> IndexSnapshot:
        root = self._canonical_workspace(workspace)
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

        manifest_hash = _manifest_hash(normalized_includes, files)
        parser_set_hash = _parser_set_hash(parsed_files)
        head = _git_head(root)
        snapshot_id = _snapshot_identifier(
            root,
            manifest_hash=manifest_hash,
            parser_set_hash=parser_set_hash,
            head=head,
        )
        snapshot = IndexSnapshot(
            snapshot_id=snapshot_id,
            workspace=_workspace_key(root),
            state=IndexState.INDEX_PARTIAL
            if extraction.gaps
            else IndexState.INDEX_READY,
            file_count=len(files),
            blob_count=len({source.content_hash for source in files}),
            reused_blob_count=len(reused_hashes),
            node_count=len(extraction.nodes),
            edge_count=len(extraction.edges),
            gap_count=len(extraction.gaps),
            manifest_hash=manifest_hash,
            parser_set_hash=parser_set_hash,
            head=head,
        )
        try:
            return self._store.put_snapshot(
                snapshot,
                include_paths=normalized_includes,
                files=files,
                nodes=extraction.nodes,
                edges=extraction.edges,
                gaps=extraction.gaps,
                parsed_cache_entries=parsed_cache_entries,
            )
        except (sqlite3.DatabaseError, StoreError) as exc:
            raise IndexError(
                "INDEX_CORRUPT", "project index database rejected the snapshot"
            ) from exc

    def status(
        self,
        workspace: str | Path,
        snapshot_id: str | None = None,
        required_paths: Sequence[str | Path] | None = None,
    ) -> IndexStatus:
        root = self._canonical_workspace(workspace)
        workspace_key = _workspace_key(root)
        snapshot = (
            self._store.get_snapshot(snapshot_id)
            if snapshot_id
            else self._store.latest_snapshot(workspace_key)
        )
        required = self._normalize_paths(required_paths)
        if snapshot is None or snapshot.workspace != workspace_key:
            return IndexStatus(
                workspace_key,
                snapshot_id,
                IndexState.INDEX_UNAVAILABLE,
                required_paths=required,
            )

        expected = self._store.file_hashes(snapshot.snapshot_id)
        includes = self._store.include_paths(snapshot.snapshot_id)
        current_files = self._collect_files(root, includes)
        current = {source.path: source.content_hash for source in current_files}
        if required:
            missing_values: list[str] = []
            changed_values: set[str] = set()
            for scope in required:
                expected_scope = {
                    path for path in expected if _path_in_scope(path, scope)
                }
                current_scope = {
                    path for path in current if _path_in_scope(path, scope)
                }
                if not expected_scope:
                    missing_values.append(scope)
                    continue
                changed_values.update(
                    path
                    for path in expected_scope.union(current_scope)
                    if expected.get(path) != current.get(path)
                )
            missing = tuple(sorted(missing_values))
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
        else:
            state = snapshot.state
        return IndexStatus(
            workspace=workspace_key,
            snapshot_id=snapshot.snapshot_id,
            state=state,
            required_paths=required,
            missing_paths=missing,
            changed_paths=changed,
            gaps=self._store.gaps(snapshot.snapshot_id),
        )

    def query(
        self,
        workspace: str | Path,
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
    ) -> QueryResult:
        root = self._canonical_workspace(workspace)
        snapshot = self._require_snapshot(root, snapshot_id)
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

        kinds = tuple(sorted({str(value) for value in node_kinds if str(value)}))
        relation_filter = tuple(
            sorted({str(value) for value in relations if str(value)})
        )
        nodes = self._store.nodes(snapshot_id)
        edges = self._store.edges(snapshot_id)
        expected_hashes = self._store.file_hashes(snapshot_id)
        source_text = self._verified_source_text(root, expected_hashes)
        eligible = tuple(node for node in nodes if not kinds or node.kind in kinds)
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
        selected_ids = {node.node_id for node in selected}
        candidate_edges = tuple(
            edge
            for edge in edges
            if edge.source_id in selected_ids
            and edge.target_id in selected_ids
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
        gaps, _, gap_truncated = _bounded_items(
            self._store.gaps(snapshot_id), byte_budget, used_bytes
        )
        truncated = (
            truncated
            or node_truncated
            or edge_truncated
            or window_truncated
            or gap_truncated
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
        )
        result = QueryResult(
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            state=snapshot.state,
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

    def diff(self, from_snapshot_id: str, to_snapshot_id: str) -> SnapshotDiff:
        before = self._store.get_snapshot(from_snapshot_id)
        after = self._store.get_snapshot(to_snapshot_id)
        if before is None or after is None:
            raise IndexError("NOT_FOUND", "project index snapshot was not found")
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
        workspace: str | Path,
        snapshot_id: str,
        required_paths: Sequence[str | Path] | None = None,
    ) -> IndexSnapshot:
        current = self.status(workspace, snapshot_id, required_paths)
        if current.state is IndexState.INDEX_UNAVAILABLE:
            raise IndexError("NOT_FOUND", "project index snapshot was not found")
        if current.state is IndexState.INDEX_STALE or current.missing_paths:
            raise IndexError(
                "INDEX_STALE", "project index snapshot does not match the workspace"
            )
        root = self._canonical_workspace(workspace)
        return self._require_snapshot(root, snapshot_id)

    def _assert_current_with_files(
        self,
        root: Path,
        snapshot_id: str,
        captured_files: Sequence[SourceFile],
    ) -> IndexSnapshot:
        snapshot = self._require_snapshot(root, snapshot_id)
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

    def snapshot_facts(self, workspace: str | Path, snapshot_id: str) -> SnapshotFacts:
        root = self._canonical_workspace(workspace)
        snapshot = self._require_snapshot(root, snapshot_id)
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
        )

    def read_snapshot_files(
        self,
        workspace: str | Path,
        snapshot_id: str,
        paths: Sequence[str | Path],
        *,
        byte_budget: int,
    ) -> tuple[SnapshotFile, ...]:
        if type(byte_budget) is not int or byte_budget <= 0:
            raise IndexError("INVALID_QUERY", "byte_budget must be a positive integer")
        normalized_paths = self._normalize_read_paths(paths)
        root = self._canonical_workspace(workspace)
        snapshot = self._require_snapshot(root, snapshot_id)
        captured_files = self._collect_files(
            root,
            self._store.include_paths(snapshot.snapshot_id),
            error_code="INDEX_STALE",
        )
        self._assert_current_with_files(root, snapshot_id, captured_files)
        expected_hashes = self._store.file_hashes(snapshot_id)
        captured_by_path = {source.path: source for source in captured_files}
        files: list[SnapshotFile] = []
        used_bytes = 0
        for relative_path in normalized_paths:
            expected_hash = expected_hashes.get(relative_path)
            if expected_hash is None:
                raise IndexError(
                    "NOT_FOUND", "project index source file was not snapshotted"
                )
            source = captured_by_path.get(relative_path)
            if source is None or source.content_hash != expected_hash:
                raise IndexError(
                    "INDEX_STALE", "project index source hash no longer matches"
                )
            body = source.data
            used_bytes += len(body)
            if used_bytes > byte_budget:
                raise IndexError("INVALID_QUERY", "byte_budget exceeded")
            files.append(SnapshotFile(relative_path, expected_hash, body))
        return tuple(files)

    def _require_snapshot(self, root: Path, snapshot_id: str) -> IndexSnapshot:
        snapshot = self._store.get_snapshot(snapshot_id)
        if snapshot is None or snapshot.workspace != _workspace_key(root):
            raise IndexError("NOT_FOUND", "project index snapshot was not found")
        return snapshot

    def _canonical_workspace(self, workspace: str | Path) -> Path:
        try:
            root = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise IndexError("INDEX_UNAVAILABLE", "workspace is unavailable") from exc
        if not root.is_dir():
            raise IndexError("INDEX_UNAVAILABLE", "workspace is unavailable")
        return root

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
                    data = _capture_regular_file(full_path)
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


def _manifest_hash(include_paths: Sequence[str], files: Sequence[SourceFile]) -> str:
    return _hash_json(
        {
            "files": tuple(
                (source.path, source.content_hash, len(source.data)) for source in files
            ),
            "include_paths": tuple(include_paths),
        }
    )


def _parser_set_hash(parsed: Sequence[ParsedExtraction]) -> str:
    return _hash_json(
        tuple(sorted({(item.extractor_id, item.extractor_version) for item in parsed}))
    )


def _snapshot_identifier(
    root: Path, *, manifest_hash: str, parser_set_hash: str, head: str | None
) -> str:
    return _hash_json(
        {
            "format": _SNAPSHOT_FORMAT_VERSION,
            "head": head,
            "manifest_hash": manifest_hash,
            "parser_set_hash": parser_set_hash,
            "workspace": _workspace_key(root),
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
        }
    )


def _hash_json(value: object) -> str:
    data = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _content_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _workspace_key(root: Path) -> str:
    return root.as_posix()


def _unsafe_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return True


def _verified_workspace_file(root: Path, relative_path: str) -> Path:
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


def _capture_regular_file(candidate: Path) -> bytes:
    try:
        before = candidate.lstat()
        if _unsafe_stat_result(before) or not stat.S_ISREG(before.st_mode):
            raise IndexError("INDEX_STALE", "project index source path is not regular")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0))
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
            asdict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )


def _node_order(node: IndexNode) -> tuple[object, ...]:
    return (
        node.path,
        node.start_line,
        node.end_line,
        node.kind,
        node.qualified_name,
        node.node_id,
    )
