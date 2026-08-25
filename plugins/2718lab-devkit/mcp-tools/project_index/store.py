"""Append-only SQLite persistence for immutable project-index snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .extractors import (
    ParsedExtraction,
    SourceFile,
    deserialize_parsed_extraction,
    serialize_parsed_extraction,
)
from .models import (
    CoverageGap,
    IndexCompactionResult,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexRetentionApply,
    IndexRetentionCandidate,
    IndexRetentionPreview,
    IndexSnapshot,
    IndexState,
    PackageDescriptor,
    QueryReceipt,
)
from .workspace import (
    canonical_workspace_root,
    is_workspace_id,
    workspace_id_for_serialized_path,
    workspace_identity,
)


class StoreError(RuntimeError):
    """Raised when durable index data cannot be read or written."""


@dataclass(frozen=True)
class WorkspaceRegistration:
    """Private durable data used to resolve an opaque workspace identifier."""

    root_path: str
    identity: str


class ProjectIndexStore:
    """Store immutable snapshots and path-neutral parser artifacts."""

    _SCHEMA_VERSION = 5
    _RETENTION_MAX_SNAPSHOTS = 32
    _RETENTION_MAX_HASHES = 65_536
    _RETENTION_MAX_ROWS = 1_000_000
    _RETENTION_MAX_INCREMENTAL_VACUUM_PAGES = 1_024

    def __init__(self, database_path: str | Path) -> None:
        """Open an existing prepared store without changing durable state."""

        self.database_path = Path(database_path).resolve(strict=False)
        self._owns_connection = True
        try:
            connection = sqlite3.connect(
                self.database_path.as_uri() + "?mode=rw",
                uri=True,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as exc:
            raise StoreError("project index store is not prepared") from exc
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self.validate_prepared_connection(self._connection)
        except (sqlite3.DatabaseError, StoreError):
            self.close()
            raise

    @classmethod
    def bootstrap(cls, database_path: str | Path) -> ProjectIndexStore:
        """Create and migrate storage exclusively for RuntimeBootstrap."""

        store = cls.__new__(cls)
        store.database_path = Path(database_path).resolve(strict=False)
        store._owns_connection = True
        store.database_path.parent.mkdir(parents=True, exist_ok=True)
        store._connection = sqlite3.connect(
            str(store.database_path), isolation_level=None
        )
        store._connection.row_factory = sqlite3.Row
        store._connection.execute("PRAGMA foreign_keys = ON")
        store._connection.execute("PRAGMA busy_timeout = 5000")
        if int(store._connection.execute("PRAGMA page_count").fetchone()[0]) == 0:
            store._connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        store._connection.execute("PRAGMA journal_mode = WAL")
        store._create_schema()
        cls.validate_prepared_connection(store._connection)
        return store

    @classmethod
    def open_prepared(cls, database_path: str | Path) -> ProjectIndexStore:
        """Retain a named zero-write opener for legacy service construction."""

        return cls(database_path)

    @classmethod
    def from_prepared_connection(
        cls, database_path: str | Path, connection: sqlite3.Connection
    ) -> ProjectIndexStore:
        """Bind a runtime-owned connection after an external read-only check."""

        store = cls.__new__(cls)
        store.database_path = Path(database_path)
        store._connection = connection
        store._owns_connection = False
        connection.row_factory = sqlite3.Row
        cls.validate_prepared_connection(connection)
        return store

    @classmethod
    def validate_prepared_connection(cls, connection: sqlite3.Connection) -> None:
        """Reject a database that is missing the current complete index schema."""

        connection.row_factory = sqlite3.Row
        required_tables = {
            "project_index_metadata",
            "project_index_blobs",
            "project_index_snapshots",
            "project_index_snapshot_files",
            "project_index_snapshot_packages",
            "project_index_parse_cache",
            "project_index_nodes",
            "project_index_edges",
            "project_index_gaps",
            "project_index_syncs",
            "project_index_workspaces",
            "project_index_snapshot_bindings",
            "project_index_query_receipts",
        }

        def table_columns(table_name: str) -> dict[str, sqlite3.Row]:
            return {
                str(row["name"]): row
                for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }

        def primary_key(columns: dict[str, sqlite3.Row]) -> tuple[str, ...]:
            return tuple(
                str(row["name"])
                for row in sorted(columns.values(), key=lambda row: int(row["pk"]))
                if int(row["pk"])
            )

        def index_columns(index_name: object) -> tuple[str, ...]:
            if type(index_name) is not str:
                raise ValueError("index name is invalid")
            identifier = index_name.replace('"', '""')
            return tuple(
                str(row["name"])
                for row in connection.execute(f'PRAGMA index_info("{identifier}")')
            )

        def unique_columns(table_name: str) -> set[tuple[str, ...]]:
            return {
                index_columns(row["name"])
                for row in connection.execute(f'PRAGMA index_list("{table_name}")')
                if int(row["unique"]) and not int(row["partial"])
            }

        def foreign_keys(table_name: str) -> set[tuple[str, str, str]]:
            return {
                (str(row["from"]), str(row["table"]), str(row["to"]))
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table_name}")'
                )
            }

        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            version_row = connection.execute(
                "SELECT value FROM project_index_metadata WHERE key = 'schema_version'"
            ).fetchone()
            version = None if version_row is None else int(version_row["value"])
            package_columns = table_columns("project_index_snapshot_packages")
            receipt_columns = table_columns("project_index_query_receipts")
            package_primary_key = primary_key(package_columns)
            receipt_primary_key = primary_key(receipt_columns)
            package_unique_columns = unique_columns("project_index_snapshot_packages")
            package_foreign_keys = foreign_keys("project_index_snapshot_packages")
            receipt_foreign_keys = foreign_keys("project_index_query_receipts")
        except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise StoreError("project index schema is corrupt") from exc

        required_package_columns = {
            "snapshot_id",
            "package_id",
            "ecosystem",
            "name",
            "root_path",
            "manifest_path",
            "manifest_hash",
        }
        required_receipt_columns = {
            "trace_id",
            "snapshot_id",
            "query_text",
            "mode",
            "node_kinds",
            "relations",
            "max_nodes",
            "max_depth",
            "source_lines",
            "byte_budget",
            "allow_miss_escape",
            "miss_escape_used",
            "returned_node_ids",
            "returned_edge_ids",
            "returned_source_windows",
            "gaps",
            "truncated",
            "package_ids",
        }
        package_columns_are_not_null = all(
            int(package_columns[column]["notnull"]) == 1
            for column in required_package_columns
            if column in package_columns
        )
        receipt_columns_are_not_null = all(
            int(receipt_columns[column]["notnull"]) == 1
            for column in required_receipt_columns - {"trace_id"}
            if column in receipt_columns
        )
        package_ids_column = receipt_columns.get("package_ids")
        has_package_shape = (
            required_package_columns.issubset(package_columns)
            and package_columns_are_not_null
            and package_primary_key == ("snapshot_id", "package_id")
            and ("snapshot_id", "manifest_path") in package_unique_columns
            and (
                "snapshot_id",
                "project_index_snapshots",
                "snapshot_id",
            )
            in package_foreign_keys
            and (
                "manifest_hash",
                "project_index_blobs",
                "content_hash",
            )
            in package_foreign_keys
        )
        has_receipt_shape = (
            required_receipt_columns.issubset(receipt_columns)
            and receipt_columns_are_not_null
            and receipt_primary_key == ("trace_id",)
            and (
                "snapshot_id",
                "project_index_snapshots",
                "snapshot_id",
            )
            in receipt_foreign_keys
            and package_ids_column is not None
            and int(package_ids_column["notnull"]) == 1
            and str(package_ids_column["dflt_value"]) == "'[]'"
        )
        if (
            not required_tables.issubset(tables)
            or version != cls._SCHEMA_VERSION
            or not has_package_shape
            or not has_receipt_shape
        ):
            raise StoreError("project index store is not prepared")

    def close(self) -> None:
        if self._owns_connection and self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM project_index_metadata WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"])

    def journal_mode(self) -> str:
        return str(
            self._connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).casefold()

    def foreign_keys_enabled(self) -> bool:
        return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def blob_count(self) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM project_index_blobs"
            ).fetchone()[0]
        )

    def existing_blob_count(self, content_hashes: Sequence[str]) -> int:
        unique_hashes = tuple(sorted(set(content_hashes)))
        if not unique_hashes:
            return 0
        placeholders = ",".join("?" for _ in unique_hashes)
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM project_index_blobs WHERE content_hash IN ({placeholders})",
            unique_hashes,
        ).fetchone()
        return int(row[0])

    def register_workspace(
        self, workspace_id: str, root_path: str, identity: str
    ) -> WorkspaceRegistration:
        """Persist one immutable root binding, returning the first registration."""
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT root_path, identity
                FROM project_index_workspaces
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO project_index_workspaces
                        (workspace_id, root_path, identity)
                    VALUES (?, ?, ?)
                    """,
                    (workspace_id, root_path, identity),
                )
                return WorkspaceRegistration(root_path, identity)
            return WorkspaceRegistration(str(row["root_path"]), str(row["identity"]))

    def get_workspace_registration(
        self, workspace_id: str
    ) -> WorkspaceRegistration | None:
        row = self._connection.execute(
            """
            SELECT root_path, identity
            FROM project_index_workspaces
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        return WorkspaceRegistration(str(row["root_path"]), str(row["identity"]))

    def get_snapshot_for_workspace(
        self, workspace_id: str, snapshot_id: str
    ) -> IndexSnapshot | None:
        """Load a snapshot only through one registered opaque workspace binding."""
        row = self._connection.execute(
            """
            SELECT snapshots.*, bindings.binding_state AS workspace_binding_state
            FROM project_index_snapshot_bindings AS bindings
            JOIN project_index_snapshots AS snapshots USING (snapshot_id)
            WHERE bindings.workspace_id = ? AND bindings.snapshot_id = ?
            """,
            (workspace_id, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        return self._with_packages(self._bound_snapshot_from_row(row, workspace_id))

    def snapshot_has_historical_binding(self, snapshot_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM project_index_snapshot_bindings
            WHERE snapshot_id = ? AND binding_state = 'historical_unverified'
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
        return row is not None

    def historical_binding_identity(
        self, workspace_id: str, snapshot_id: str
    ) -> str | None:
        row = self._connection.execute(
            """
            SELECT root_identity
            FROM project_index_snapshot_bindings
            WHERE workspace_id = ?
                AND snapshot_id = ?
                AND binding_state = 'historical_unverified'
            """,
            (workspace_id, snapshot_id),
        ).fetchone()
        return None if row is None else str(row["root_identity"])

    def activate_historical_snapshot(
        self, workspace_id: str, snapshot_id: str
    ) -> IndexSnapshot:
        """Mark a checked historical path snapshot as safe for normal use."""
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT snapshots.*, bindings.binding_state AS workspace_binding_state
                FROM project_index_snapshot_bindings AS bindings
                JOIN project_index_snapshots AS snapshots USING (snapshot_id)
                WHERE bindings.workspace_id = ? AND bindings.snapshot_id = ?
                """,
                (workspace_id, snapshot_id),
            ).fetchone()
            if row is None:
                raise StoreError("snapshot not found")
            snapshot = self._bound_snapshot_from_row(row, workspace_id)
            if snapshot.binding_state != "historical_unverified":
                raise StoreError("historical snapshot does not match workspace")
            cursor.execute(
                """
                UPDATE project_index_snapshot_bindings
                SET binding_state = 'active'
                WHERE workspace_id = ? AND snapshot_id = ?
                """,
                (workspace_id, snapshot_id),
            )
        activated = self.get_snapshot_for_workspace(workspace_id, snapshot_id)
        if activated is None:
            raise StoreError("snapshot not found")
        return activated

    def get_parse_cache(
        self, content_hash: str, extractor_id: str, extractor_version: str
    ) -> ParsedExtraction | None:
        row = self._connection.execute(
            """
            SELECT payload FROM project_index_parse_cache
            WHERE content_hash = ? AND extractor_id = ? AND extractor_version = ?
            """,
            (content_hash, extractor_id, extractor_version),
        ).fetchone()
        if row is None:
            return None
        try:
            return deserialize_parsed_extraction(str(row["payload"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoreError("parser cache is corrupt") from exc

    def put_snapshot(
        self,
        snapshot: IndexSnapshot,
        *,
        include_paths: Sequence[str],
        files: Sequence[SourceFile],
        nodes: Sequence[IndexNode],
        edges: Sequence[IndexEdge],
        gaps: Sequence[CoverageGap],
        packages: Sequence[PackageDescriptor] | None = None,
        parsed_cache_entries: Sequence[tuple[SourceFile, ParsedExtraction]] = (),
    ) -> IndexSnapshot:
        """Insert a complete graph once and append a workspace sync pointer."""
        workspace_id = snapshot.workspace_id or snapshot.workspace
        package_descriptors = snapshot.packages if packages is None else tuple(packages)
        if not is_workspace_id(workspace_id):
            raise StoreError("snapshot has no registered workspace binding")
        with self._transaction() as cursor:
            cursor.executemany(
                "INSERT OR IGNORE INTO project_index_blobs (content_hash, size) VALUES (?, ?)",
                ((source.content_hash, len(source.data)) for source in files),
            )
            cursor.executemany(
                """
                INSERT OR IGNORE INTO project_index_parse_cache
                    (content_hash, extractor_id, extractor_version, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        source.content_hash,
                        parsed.extractor_id,
                        parsed.extractor_version,
                        serialize_parsed_extraction(parsed),
                    )
                    for source, parsed in parsed_cache_entries
                ),
            )
            existing = cursor.execute(
                "SELECT * FROM project_index_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO project_index_snapshots
                        (snapshot_id, workspace, workspace_id, binding_state, state, include_paths, file_count, blob_count,
                         reused_blob_count, node_count, edge_count, gap_count, manifest_hash,
                         parser_set_hash, head)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        "",
                        "",
                        "active",
                        snapshot.state.value,
                        _json(tuple(include_paths)),
                        snapshot.file_count,
                        snapshot.blob_count,
                        snapshot.reused_blob_count,
                        snapshot.node_count,
                        snapshot.edge_count,
                        snapshot.gap_count,
                        snapshot.manifest_hash,
                        snapshot.parser_set_hash,
                        snapshot.head,
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO project_index_snapshot_files
                        (snapshot_id, path, content_hash, size)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            snapshot.snapshot_id,
                            source.path,
                            source.content_hash,
                            len(source.data),
                        )
                        for source in files
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO project_index_snapshot_packages
                        (snapshot_id, package_id, ecosystem, name, root_path,
                         manifest_path, manifest_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            snapshot.snapshot_id,
                            package.package_id,
                            package.ecosystem,
                            package.name,
                            package.root_path,
                            package.manifest_path,
                            package.manifest_hash,
                        )
                        for package in package_descriptors
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO project_index_nodes
                        (snapshot_id, node_id, kind, path, name, qualified_name,
                         start_line, end_line, content_hash, attributes, extractor_id,
                         extractor_version, provenance, start_byte, end_byte)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            snapshot.snapshot_id,
                            node.node_id,
                            node.kind,
                            node.path,
                            node.name,
                            node.qualified_name,
                            node.start_line,
                            node.end_line,
                            node.content_hash,
                            _json(node.attributes),
                            node.extractor_id,
                            node.extractor_version,
                            node.provenance,
                            node.start_byte,
                            node.end_byte,
                        )
                        for node in nodes
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO project_index_edges
                        (snapshot_id, edge_id, source_id, target_id, relation, path,
                         start_line, end_line, start_byte, end_byte, content_hash,
                         extractor_id, extractor_version, provenance)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            snapshot.snapshot_id,
                            edge.edge_id,
                            edge.source_id,
                            edge.target_id,
                            edge.relation,
                            edge.path,
                            edge.start_line,
                            edge.end_line,
                            edge.start_byte,
                            edge.end_byte,
                            edge.content_hash,
                            edge.extractor_id,
                            edge.extractor_version,
                            edge.provenance,
                        )
                        for edge in edges
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO project_index_gaps (snapshot_id, path, code, message)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (snapshot.snapshot_id, gap.path, gap.code, gap.message)
                        for gap in gaps
                    ),
                )
            else:
                stored = self._snapshot_from_row(existing)
                if (
                    stored.state != snapshot.state
                    or stored.file_count != snapshot.file_count
                    or stored.blob_count != snapshot.blob_count
                    or stored.node_count != snapshot.node_count
                    or stored.edge_count != snapshot.edge_count
                    or stored.gap_count != snapshot.gap_count
                    or stored.manifest_hash != snapshot.manifest_hash
                    or stored.parser_set_hash != snapshot.parser_set_hash
                    or stored.head != snapshot.head
                    or package_descriptors != self.packages(snapshot.snapshot_id)
                ):
                    raise StoreError("snapshot identifier collision")
            cursor.execute(
                """
                INSERT OR IGNORE INTO project_index_snapshot_bindings
                    (workspace_id, snapshot_id, binding_state)
                VALUES (?, ?, 'active')
                """,
                (workspace_id, snapshot.snapshot_id),
            )
            cursor.execute(
                "INSERT INTO project_index_syncs (workspace, snapshot_id) VALUES (?, ?)",
                (workspace_id, snapshot.snapshot_id),
            )
        stored = self.get_snapshot_for_workspace(workspace_id, snapshot.snapshot_id)
        if stored is None:
            raise StoreError("snapshot binding was not stored")
        return stored

    def put_query_receipt(self, receipt: QueryReceipt) -> QueryReceipt:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO project_index_query_receipts
                    (trace_id, snapshot_id, query_text, mode, node_kinds, relations,
                      max_nodes, max_depth, source_lines, byte_budget, allow_miss_escape,
                      miss_escape_used, returned_node_ids, returned_edge_ids,
                      returned_source_windows, gaps, truncated, package_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.trace_id,
                    receipt.snapshot_id,
                    receipt.query,
                    receipt.mode,
                    _json(receipt.node_kinds),
                    _json(receipt.relations),
                    receipt.max_nodes,
                    receipt.max_depth,
                    receipt.source_lines,
                    receipt.byte_budget,
                    int(receipt.allow_miss_escape),
                    int(receipt.miss_escape_used),
                    _json(receipt.returned_node_ids),
                    _json(receipt.returned_edge_ids),
                    _json(receipt.returned_source_windows),
                    _json(
                        tuple((gap.path, gap.code, gap.message) for gap in receipt.gaps)
                    ),
                    int(receipt.truncated),
                    _json(receipt.package_ids),
                ),
            )
        stored = self.get_query_receipt(receipt.trace_id)
        if stored is None:
            raise StoreError("query receipt was not stored")
        return stored

    def get_query_receipt(self, trace_id: str) -> QueryReceipt | None:
        row = self._connection.execute(
            "SELECT * FROM project_index_query_receipts WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return self._query_receipt_from_row(row)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoreError("query receipt is corrupt") from exc

    def get_snapshot(self, snapshot_id: str) -> IndexSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM project_index_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return (
            None if row is None else self._with_packages(self._snapshot_from_row(row))
        )

    def latest_snapshot(self, workspace_id: str) -> IndexSnapshot | None:
        row = self._connection.execute(
            """
            SELECT snapshots.*, bindings.binding_state AS workspace_binding_state
            FROM project_index_syncs AS syncs
            JOIN project_index_snapshots AS snapshots USING (snapshot_id)
            JOIN project_index_snapshot_bindings AS bindings
                ON bindings.workspace_id = syncs.workspace
                AND bindings.snapshot_id = syncs.snapshot_id
            WHERE syncs.workspace = ?
            ORDER BY syncs.sequence DESC
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        return (
            None
            if row is None
            else self._with_packages(self._bound_snapshot_from_row(row, workspace_id))
        )

    def include_paths(self, snapshot_id: str) -> tuple[str, ...]:
        row = self._connection.execute(
            "SELECT include_paths FROM project_index_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise StoreError("snapshot not found")
        return tuple(str(value) for value in json.loads(row["include_paths"]))

    def file_hashes(self, snapshot_id: str) -> Mapping[str, str]:
        rows = self._connection.execute(
            """
            SELECT path, content_hash
            FROM project_index_snapshot_files
            WHERE snapshot_id = ?
            ORDER BY path
            """,
            (snapshot_id,),
        ).fetchall()
        return {str(row["path"]): str(row["content_hash"]) for row in rows}

    def packages(self, snapshot_id: str) -> tuple[PackageDescriptor, ...]:
        rows = self._connection.execute(
            """
            SELECT package_id, ecosystem, name, root_path, manifest_path, manifest_hash
            FROM project_index_snapshot_packages
            WHERE snapshot_id = ?
            ORDER BY root_path, manifest_path, ecosystem, package_id
            """,
            (snapshot_id,),
        ).fetchall()
        return tuple(
            PackageDescriptor(
                package_id=str(row["package_id"]),
                ecosystem=str(row["ecosystem"]),
                name=str(row["name"]),
                root_path=str(row["root_path"]),
                manifest_path=str(row["manifest_path"]),
                manifest_hash=str(row["manifest_hash"]),
            )
            for row in rows
        )

    def nodes(self, snapshot_id: str) -> tuple[IndexNode, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM project_index_nodes
            WHERE snapshot_id = ?
            ORDER BY path, start_byte, end_byte, kind, qualified_name, node_id
            """,
            (snapshot_id,),
        ).fetchall()
        return tuple(self._node_from_row(row) for row in rows)

    def edges(self, snapshot_id: str) -> tuple[IndexEdge, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM project_index_edges
            WHERE snapshot_id = ?
            ORDER BY path, start_byte, end_byte, relation, source_id, target_id
            """,
            (snapshot_id,),
        ).fetchall()
        return tuple(self._edge_from_row(row) for row in rows)

    def gaps(self, snapshot_id: str) -> tuple[CoverageGap, ...]:
        rows = self._connection.execute(
            """
            SELECT path, code, message FROM project_index_gaps
            WHERE snapshot_id = ?
            ORDER BY path, code, message
            """,
            (snapshot_id,),
        ).fetchall()
        return tuple(
            CoverageGap(str(row["path"]), str(row["code"]), str(row["message"]))
            for row in rows
        )

    def preview_retention(
        self, protected_snapshot_ids: Sequence[str] = ()
    ) -> IndexRetentionPreview:
        """Compute a conservative snapshot-release plan without mutating storage."""

        try:
            return self._retention_preview(
                self._connection.cursor(), protected_snapshot_ids
            )
        except (sqlite3.DatabaseError, StoreError):
            return IndexRetentionPreview(
                preview_id=None,
                candidates=(),
                protected_snapshot_ids=(),
                blocked_reason="RETENTION_REFERENCE_SCHEMA_UNAVAILABLE",
            )

    def apply_retention(
        self,
        preview_id: str | None,
        protected_snapshot_ids: Sequence[str] = (),
    ) -> IndexRetentionApply:
        """Recheck and release only the exact previously previewed candidates."""

        if not isinstance(preview_id, str) or not preview_id:
            return IndexRetentionApply(
                preview_id=None,
                deleted_snapshot_ids=(),
                deleted_row_count=0,
                blocked_reason="RETENTION_PREVIEW_ID_INVALID",
            )
        try:
            with self._transaction() as cursor:
                preview = self._retention_preview(cursor, protected_snapshot_ids)
                if preview.preview_id != preview_id:
                    return IndexRetentionApply(
                        preview_id=preview.preview_id,
                        deleted_snapshot_ids=(),
                        deleted_row_count=0,
                        blocked_reason="RETENTION_PREVIEW_STALE",
                    )
                deleted_rows = 0
                deleted_snapshot_ids = tuple(
                    candidate.snapshot_id for candidate in preview.candidates
                )
                if not deleted_snapshot_ids:
                    return IndexRetentionApply(
                        preview_id=preview_id,
                        deleted_snapshot_ids=(),
                        deleted_row_count=0,
                    )
                if not self._install_retention_hash_plan(cursor, deleted_snapshot_ids):
                    return IndexRetentionApply(
                        preview_id=preview.preview_id,
                        deleted_snapshot_ids=(),
                        deleted_row_count=0,
                        blocked_reason="RETENTION_CANDIDATE_BUDGET_EXCEEDED",
                    )
                for snapshot_id in deleted_snapshot_ids:
                    deleted_rows += self._delete_retained_snapshot(cursor, snapshot_id)
                deleted_rows += self._delete_unreferenced_payloads(cursor)
                self._release_incremental_pages(cursor)
                cursor.execute("DROP TABLE temp.project_index_retention_hash_plan")
                return IndexRetentionApply(
                    preview_id=preview_id,
                    deleted_snapshot_ids=deleted_snapshot_ids,
                    deleted_row_count=deleted_rows,
                )
        except (sqlite3.DatabaseError, StoreError):
            return IndexRetentionApply(
                preview_id=None,
                deleted_snapshot_ids=(),
                deleted_row_count=0,
                blocked_reason="RETENTION_REFERENCE_SCHEMA_UNAVAILABLE",
            )

    def compact_storage(
        self, *, allow_full_rewrite: bool = False
    ) -> IndexCompactionResult:
        """Compact local index pages under an explicit, space-checked request."""

        database_before, wal_before = self._storage_file_sizes()
        mode = "unknown"
        try:
            self._retention_tables_are_safe(self._connection.cursor())
            checkpoint = self._connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if int(checkpoint[0]) != 0:
                database_after, wal_after = self._storage_file_sizes()
                return IndexCompactionResult(
                    database_before,
                    database_after,
                    wal_before,
                    wal_after,
                    mode,
                    "RETENTION_COMPACTION_BUSY",
                )
            auto_vacuum = int(
                self._connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            )
            mode = {0: "none", 1: "full", 2: "incremental"}.get(auto_vacuum, "unknown")
            if auto_vacuum == 0:
                if not allow_full_rewrite:
                    database_after, wal_after = self._storage_file_sizes()
                    return IndexCompactionResult(
                        database_before,
                        database_after,
                        wal_before,
                        wal_after,
                        mode,
                        "RETENTION_FULL_REWRITE_REQUIRED",
                    )
                current_database, current_wal = self._storage_file_sizes()
                required_free = current_database * 2 + current_wal + 64 * 1024 * 1024
                available_free = shutil.disk_usage(self.database_path.parent).free
                if available_free < required_free:
                    return IndexCompactionResult(
                        database_before,
                        current_database,
                        wal_before,
                        current_wal,
                        mode,
                        "RETENTION_COMPACTION_SPACE_INSUFFICIENT",
                    )
                self._connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
                self._connection.execute("VACUUM")
                mode = "incremental"
            free_pages = int(
                self._connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            for _ in range(
                min(free_pages, self._RETENTION_MAX_INCREMENTAL_VACUUM_PAGES)
            ):
                self._connection.execute("PRAGMA incremental_vacuum(1)")
            checkpoint = self._connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            database_after, wal_after = self._storage_file_sizes()
            if int(checkpoint[0]) != 0:
                return IndexCompactionResult(
                    database_before,
                    database_after,
                    wal_before,
                    wal_after,
                    mode,
                    "RETENTION_COMPACTION_CHECKPOINT_BUSY",
                )
            return IndexCompactionResult(
                database_before,
                database_after,
                wal_before,
                wal_after,
                mode,
            )
        except (OSError, sqlite3.DatabaseError, StoreError):
            database_after, wal_after = self._storage_file_sizes()
            return IndexCompactionResult(
                database_before,
                database_after,
                wal_before,
                wal_after,
                mode,
                "RETENTION_COMPACTION_UNAVAILABLE",
            )

    def _storage_file_sizes(self) -> tuple[int, int]:
        try:
            database_bytes = (
                self.database_path.stat().st_size if self.database_path.is_file() else 0
            )
            wal_path = Path(f"{self.database_path}-wal")
            wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
            return database_bytes, wal_bytes
        except OSError:
            return 0, 0

    @staticmethod
    def _retention_tables_are_safe(cursor: sqlite3.Cursor) -> None:
        required_tables = {
            "project_index_blobs",
            "project_index_snapshots",
            "project_index_snapshot_files",
            "project_index_snapshot_packages",
            "project_index_parse_cache",
            "project_index_nodes",
            "project_index_edges",
            "project_index_gaps",
            "project_index_syncs",
            "project_index_workspaces",
            "project_index_snapshot_bindings",
            "project_index_query_receipts",
        }
        tables = {
            str(row["name"])
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required_tables.issubset(tables):
            raise StoreError("retention reference schema is unavailable")

        snapshot_children: set[str] = set()
        blob_children: set[tuple[str, str]] = set()
        for table in tables:
            for row in cursor.execute(
                "SELECT * FROM pragma_foreign_key_list(?)", (table,)
            ):
                if str(row["table"]) == "project_index_snapshots":
                    snapshot_children.add(table)
                if str(row["table"]) == "project_index_blobs":
                    blob_children.add((table, str(row["from"])))
        expected_children = {
            "project_index_snapshot_files",
            "project_index_snapshot_packages",
            "project_index_nodes",
            "project_index_edges",
            "project_index_gaps",
            "project_index_syncs",
            "project_index_snapshot_bindings",
            "project_index_query_receipts",
        }
        if snapshot_children != expected_children:
            raise StoreError("retention snapshot ownership is unknown")
        expected_blob_children = {
            ("project_index_snapshot_files", "content_hash"),
            ("project_index_snapshot_packages", "manifest_hash"),
            ("project_index_parse_cache", "content_hash"),
        }
        if blob_children != expected_blob_children:
            raise StoreError("retention blob ownership is unknown")

    def _retention_preview(
        self, cursor: sqlite3.Cursor, external_protected_snapshot_ids: Sequence[str]
    ) -> IndexRetentionPreview:
        self._retention_tables_are_safe(cursor)
        retained_by_workspace: dict[str, int] = {}
        protected_snapshot_ids: set[str] = set()
        sync_rows = cursor.execute(
            """
            SELECT workspace, snapshot_id, MAX(sequence) AS latest_sequence
            FROM project_index_syncs
            GROUP BY workspace, snapshot_id
            ORDER BY workspace, latest_sequence DESC, snapshot_id
            """
        ).fetchall()
        for row in sync_rows:
            workspace = str(row["workspace"])
            count = retained_by_workspace.get(workspace, 0)
            if count < 2:
                protected_snapshot_ids.add(str(row["snapshot_id"]))
                retained_by_workspace[workspace] = count + 1
        receipt_rows = cursor.execute(
            "SELECT DISTINCT snapshot_id FROM project_index_query_receipts"
        ).fetchall()
        protected_snapshot_ids.update(str(row["snapshot_id"]) for row in receipt_rows)
        protected_snapshot_ids.update(self._external_snapshot_references(cursor))
        if any(
            not isinstance(snapshot_id, str) or not snapshot_id
            for snapshot_id in external_protected_snapshot_ids
        ):
            raise StoreError("retention external reference is invalid")
        protected_snapshot_ids.update(external_protected_snapshot_ids)

        snapshot_rows = cursor.execute(
            """
            SELECT snapshot.snapshot_id, COALESCE(MAX(sync.sequence), -1) AS last_sequence
            FROM project_index_snapshots AS snapshot
            LEFT JOIN project_index_syncs AS sync
              ON sync.snapshot_id = snapshot.snapshot_id
            GROUP BY snapshot.snapshot_id
            ORDER BY last_sequence, snapshot.snapshot_id
            """
        ).fetchall()
        eligible_ids = tuple(
            str(row["snapshot_id"])
            for row in snapshot_rows
            if str(row["snapshot_id"]) not in protected_snapshot_ids
        )
        row_count_queries = (
            "SELECT COUNT(*) FROM project_index_snapshots WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_snapshot_files WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_snapshot_packages WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_nodes WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_edges WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_gaps WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_syncs WHERE snapshot_id = ?",
            "SELECT COUNT(*) FROM project_index_snapshot_bindings WHERE snapshot_id = ?",
        )
        candidate_ids: list[str] = []
        base_row_counts: dict[str, int] = {}
        planned_hashes: set[str] = set()
        planned_base_rows = 0
        for snapshot_id in eligible_ids[: self._RETENTION_MAX_SNAPSHOTS]:
            base_rows = sum(
                int(cursor.execute(query, (snapshot_id,)).fetchone()[0])
                for query in row_count_queries
            )
            hash_rows = cursor.execute(
                """
                SELECT content_hash
                FROM project_index_snapshot_files
                WHERE snapshot_id = ?
                UNION
                SELECT manifest_hash
                FROM project_index_snapshot_packages
                WHERE snapshot_id = ?
                LIMIT ?
                """,
                (snapshot_id, snapshot_id, self._RETENTION_MAX_HASHES + 1),
            ).fetchall()
            snapshot_hashes = {str(row["content_hash"]) for row in hash_rows}
            if (
                planned_base_rows + base_rows > self._RETENTION_MAX_ROWS
                or len(planned_hashes | snapshot_hashes) > self._RETENTION_MAX_HASHES
            ):
                break
            candidate_ids.append(snapshot_id)
            base_row_counts[snapshot_id] = base_rows
            planned_base_rows += base_rows
            planned_hashes.update(snapshot_hashes)
        if eligible_ids and not candidate_ids:
            return IndexRetentionPreview(
                preview_id=None,
                candidates=(),
                protected_snapshot_ids=tuple(sorted(protected_snapshot_ids)),
                blocked_reason="RETENTION_CANDIDATE_BUDGET_EXCEEDED",
            )
        payload_estimates, _ = self._batch_payload_estimates(cursor, candidate_ids)
        while (
            candidate_ids
            and sum(
                base_row_counts[snapshot_id] + payload_estimates[snapshot_id][0]
                for snapshot_id in candidate_ids
            )
            > self._RETENTION_MAX_ROWS
        ):
            candidate_ids.pop()
            payload_estimates, _ = self._batch_payload_estimates(cursor, candidate_ids)
        if eligible_ids and not candidate_ids:
            return IndexRetentionPreview(
                preview_id=None,
                candidates=(),
                protected_snapshot_ids=tuple(sorted(protected_snapshot_ids)),
                blocked_reason="RETENTION_CANDIDATE_BUDGET_EXCEEDED",
            )
        candidates = tuple(
            IndexRetentionCandidate(
                snapshot_id=snapshot_id,
                reasons=(
                    "outside_newest_two_sync_references",
                    "no_query_receipt",
                ),
                estimated_row_count=(
                    base_row_counts[snapshot_id] + payload_estimates[snapshot_id][0]
                ),
                estimated_reclaimable_bytes=payload_estimates[snapshot_id][1],
            )
            for snapshot_id in candidate_ids
        )
        protected = tuple(sorted(protected_snapshot_ids))
        return IndexRetentionPreview(
            _retention_preview_identifier(candidates, protected),
            candidates,
            protected,
        )

    def _batch_payload_estimates(
        self, cursor: sqlite3.Cursor, snapshot_ids: Sequence[str]
    ) -> tuple[dict[str, tuple[int, int]], int]:
        """Assign each batch-orphan payload to its oldest candidate once."""

        estimates = {snapshot_id: (0, 0) for snapshot_id in snapshot_ids}
        if not snapshot_ids:
            return estimates, 0
        encoded_snapshot_ids = _json(tuple(snapshot_ids))
        rows = cursor.execute(
            """
            WITH candidates(snapshot_id, ordinal) AS (
                SELECT CAST(value AS TEXT), CAST(key AS INTEGER)
                FROM json_each(?)
            ),
            candidate_refs(content_hash, ordinal) AS (
                SELECT file.content_hash, candidate.ordinal
                FROM project_index_snapshot_files AS file
                JOIN candidates AS candidate USING (snapshot_id)
                UNION ALL
                SELECT package.manifest_hash, candidate.ordinal
                FROM project_index_snapshot_packages AS package
                JOIN candidates AS candidate USING (snapshot_id)
            ),
            exclusive_hashes(content_hash, owner_ordinal) AS (
                SELECT reference.content_hash, MIN(reference.ordinal)
                FROM candidate_refs AS reference
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM project_index_snapshot_files AS retained_file
                    WHERE retained_file.content_hash = reference.content_hash
                      AND NOT EXISTS (
                          SELECT 1 FROM candidates
                          WHERE snapshot_id = retained_file.snapshot_id
                      )
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM project_index_snapshot_packages AS retained_package
                    WHERE retained_package.manifest_hash = reference.content_hash
                      AND NOT EXISTS (
                          SELECT 1 FROM candidates
                          WHERE snapshot_id = retained_package.snapshot_id
                      )
                )
                GROUP BY reference.content_hash
            )
            SELECT
                candidate.snapshot_id,
                (SELECT COUNT(*) FROM exclusive_hashes AS exclusive
                 WHERE exclusive.owner_ordinal = candidate.ordinal)
                  + (SELECT COUNT(*)
                     FROM project_index_parse_cache AS cache
                     JOIN exclusive_hashes AS exclusive
                       ON exclusive.content_hash = cache.content_hash
                     WHERE exclusive.owner_ordinal = candidate.ordinal) AS payload_rows,
                COALESCE((SELECT SUM(length(CAST(cache.payload AS BLOB)))
                          FROM project_index_parse_cache AS cache
                          JOIN exclusive_hashes AS exclusive
                            ON exclusive.content_hash = cache.content_hash
                          WHERE exclusive.owner_ordinal = candidate.ordinal), 0)
                    AS payload_bytes,
                (SELECT COUNT(DISTINCT content_hash) FROM candidate_refs)
                    AS planned_hash_count
            FROM candidates AS candidate
            ORDER BY candidate.ordinal
            """,
            (encoded_snapshot_ids,),
        ).fetchall()
        planned_hash_count = 0
        for row in rows:
            estimates[str(row["snapshot_id"])] = (
                int(row["payload_rows"]),
                int(row["payload_bytes"]),
            )
            planned_hash_count = int(row["planned_hash_count"])
        return estimates, planned_hash_count

    @staticmethod
    def _external_snapshot_references(cursor: sqlite3.Cursor) -> set[str]:
        """Discover durable cross-module roots stored beside the index schema."""

        index_owned_tables = {
            "project_index_snapshots",
            "project_index_snapshot_files",
            "project_index_snapshot_packages",
            "project_index_nodes",
            "project_index_edges",
            "project_index_gaps",
            "project_index_syncs",
            "project_index_snapshot_bindings",
            "project_index_query_receipts",
        }
        tables = tuple(
            str(row["name"])
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if str(row["name"]) not in index_owned_tables
        )
        for table in tables:
            columns = tuple(
                str(row["name"])
                for row in cursor.execute(
                    "SELECT * FROM pragma_table_info(?)", (table,)
                )
                if str(row["name"]) == "snapshot_id"
                or str(row["name"]).endswith("_snapshot_id")
            )
            if columns:
                raise StoreError("retention external snapshot ownership is unknown")
        return set()

    @staticmethod
    def _delete_retained_snapshot(cursor: sqlite3.Cursor, snapshot_id: str) -> int:
        deleted_rows = 0
        delete_queries = (
            "DELETE FROM project_index_edges WHERE snapshot_id = ?",
            "DELETE FROM project_index_nodes WHERE snapshot_id = ?",
            "DELETE FROM project_index_snapshot_files WHERE snapshot_id = ?",
            "DELETE FROM project_index_snapshot_packages WHERE snapshot_id = ?",
            "DELETE FROM project_index_gaps WHERE snapshot_id = ?",
            "DELETE FROM project_index_syncs WHERE snapshot_id = ?",
            "DELETE FROM project_index_snapshot_bindings WHERE snapshot_id = ?",
            "DELETE FROM project_index_snapshots WHERE snapshot_id = ?",
        )
        for query in delete_queries:
            result = cursor.execute(query, (snapshot_id,))
            deleted_rows += max(0, result.rowcount)
        return deleted_rows

    def _install_retention_hash_plan(
        self, cursor: sqlite3.Cursor, snapshot_ids: Sequence[str]
    ) -> bool:
        """Capture the bounded hash set authorized by the current preview."""

        cursor.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS project_index_retention_hash_plan (
                content_hash TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        cursor.execute("DELETE FROM temp.project_index_retention_hash_plan")
        if not snapshot_ids:
            return True
        encoded_snapshot_ids = _json(tuple(snapshot_ids))
        cursor.execute(
            """
            WITH candidates(snapshot_id) AS (
                SELECT CAST(value AS TEXT)
                FROM json_each(?)
            )
            INSERT OR IGNORE INTO temp.project_index_retention_hash_plan(content_hash)
            SELECT file.content_hash
            FROM project_index_snapshot_files AS file
            JOIN candidates AS candidate USING (snapshot_id)
            """,
            (encoded_snapshot_ids,),
        )
        cursor.execute(
            """
            WITH candidates(snapshot_id) AS (
                SELECT CAST(value AS TEXT)
                FROM json_each(?)
            )
            INSERT OR IGNORE INTO temp.project_index_retention_hash_plan(content_hash)
            SELECT package.manifest_hash
            FROM project_index_snapshot_packages AS package
            JOIN candidates AS candidate USING (snapshot_id)
            """,
            (encoded_snapshot_ids,),
        )
        row = cursor.execute(
            "SELECT COUNT(*) FROM temp.project_index_retention_hash_plan"
        ).fetchone()
        if int(row[0]) <= self._RETENTION_MAX_HASHES:
            return True
        cursor.execute("DROP TABLE temp.project_index_retention_hash_plan")
        return False

    @staticmethod
    def _delete_unreferenced_payloads(cursor: sqlite3.Cursor) -> int:
        """Release only preview-planned payload no retained snapshot can reach."""

        deleted_rows = 0
        for query in (
            """
            DELETE FROM project_index_parse_cache
            WHERE content_hash IN (
                SELECT content_hash FROM temp.project_index_retention_hash_plan
            )
              AND NOT EXISTS (
                SELECT 1
                FROM project_index_snapshot_files AS retained_file
                WHERE retained_file.content_hash = project_index_parse_cache.content_hash
            )
              AND NOT EXISTS (
                SELECT 1
                FROM project_index_snapshot_packages AS retained_package
                WHERE retained_package.manifest_hash = project_index_parse_cache.content_hash
            )
            """,
            """
            DELETE FROM project_index_blobs
            WHERE content_hash IN (
                SELECT content_hash FROM temp.project_index_retention_hash_plan
            )
              AND NOT EXISTS (
                SELECT 1
                FROM project_index_snapshot_files AS retained_file
                WHERE retained_file.content_hash = project_index_blobs.content_hash
            )
              AND NOT EXISTS (
                SELECT 1
                FROM project_index_snapshot_packages AS retained_package
                WHERE retained_package.manifest_hash = project_index_blobs.content_hash
            )
            """,
        ):
            result = cursor.execute(query)
            deleted_rows += max(0, result.rowcount)
        return deleted_rows

    def _release_incremental_pages(self, cursor: sqlite3.Cursor) -> None:
        """Return a bounded number of pages when the store already supports it."""

        auto_vacuum = int(cursor.execute("PRAGMA auto_vacuum").fetchone()[0])
        if auto_vacuum != 2:
            return
        free_pages = int(cursor.execute("PRAGMA freelist_count").fetchone()[0])
        for _ in range(min(free_pages, self._RETENTION_MAX_INCREMENTAL_VACUUM_PAGES)):
            cursor.execute("PRAGMA incremental_vacuum(1)")

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_index_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_index_blobs (
                    content_hash TEXT PRIMARY KEY,
                    size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_index_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    state TEXT NOT NULL,
                    include_paths TEXT NOT NULL,
                    file_count INTEGER NOT NULL,
                    blob_count INTEGER NOT NULL,
                    reused_blob_count INTEGER NOT NULL,
                    node_count INTEGER NOT NULL,
                    edge_count INTEGER NOT NULL,
                    gap_count INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    parser_set_hash TEXT NOT NULL,
                    head TEXT
                );
                CREATE TABLE IF NOT EXISTS project_index_snapshot_files (
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL REFERENCES project_index_blobs(content_hash),
                    size INTEGER NOT NULL,
                    PRIMARY KEY (snapshot_id, path)
                );
                CREATE INDEX IF NOT EXISTS project_index_files_hash
                    ON project_index_snapshot_files(content_hash);
                CREATE TABLE IF NOT EXISTS project_index_snapshot_packages (
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    package_id TEXT NOT NULL,
                    ecosystem TEXT NOT NULL,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL REFERENCES project_index_blobs(content_hash),
                    PRIMARY KEY (snapshot_id, package_id),
                    UNIQUE (snapshot_id, manifest_path)
                );
                CREATE INDEX IF NOT EXISTS project_index_packages_manifest
                    ON project_index_snapshot_packages(snapshot_id, manifest_path);
                CREATE INDEX IF NOT EXISTS project_index_packages_manifest_hash
                    ON project_index_snapshot_packages(manifest_hash);
                CREATE TABLE IF NOT EXISTS project_index_parse_cache (
                    content_hash TEXT NOT NULL REFERENCES project_index_blobs(content_hash),
                    extractor_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (content_hash, extractor_id, extractor_version)
                );
                CREATE TABLE IF NOT EXISTS project_index_nodes (
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    attributes TEXT NOT NULL,
                    extractor_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    PRIMARY KEY (snapshot_id, node_id)
                );
                CREATE INDEX IF NOT EXISTS project_index_nodes_lookup
                    ON project_index_nodes(snapshot_id, kind, path, name);
                CREATE TABLE IF NOT EXISTS project_index_edges (
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    edge_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    extractor_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, edge_id),
                    FOREIGN KEY (snapshot_id, source_id)
                        REFERENCES project_index_nodes(snapshot_id, node_id),
                    FOREIGN KEY (snapshot_id, target_id)
                        REFERENCES project_index_nodes(snapshot_id, node_id)
                );
                CREATE INDEX IF NOT EXISTS project_index_edges_source
                    ON project_index_edges(snapshot_id, source_id, relation);
                CREATE INDEX IF NOT EXISTS project_index_edges_target
                    ON project_index_edges(snapshot_id, target_id, relation);
                CREATE TABLE IF NOT EXISTS project_index_gaps (
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    path TEXT NOT NULL,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, path, code, message)
                );
                CREATE TABLE IF NOT EXISTS project_index_syncs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS project_index_syncs_workspace
                    ON project_index_syncs(workspace, sequence);
                CREATE INDEX IF NOT EXISTS project_index_syncs_snapshot
                    ON project_index_syncs(snapshot_id, sequence);
                CREATE TABLE IF NOT EXISTS project_index_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    identity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_index_snapshot_bindings (
                    workspace_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    binding_state TEXT NOT NULL,
                    root_identity TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (workspace_id, snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS project_index_snapshot_bindings_snapshot
                    ON project_index_snapshot_bindings(snapshot_id, binding_state);
                CREATE TABLE IF NOT EXISTS project_index_query_receipts (
                    trace_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES project_index_snapshots(snapshot_id),
                    query_text TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    node_kinds TEXT NOT NULL,
                    relations TEXT NOT NULL,
                    max_nodes INTEGER NOT NULL,
                    max_depth INTEGER NOT NULL,
                    source_lines INTEGER NOT NULL,
                    byte_budget INTEGER NOT NULL,
                    allow_miss_escape INTEGER NOT NULL,
                    miss_escape_used INTEGER NOT NULL,
                    returned_node_ids TEXT NOT NULL,
                    returned_edge_ids TEXT NOT NULL,
                    returned_source_windows TEXT NOT NULL,
                    gaps TEXT NOT NULL,
                    truncated INTEGER NOT NULL,
                    package_ids TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            row = self._connection.execute(
                "SELECT value FROM project_index_metadata WHERE key = 'schema_version'"
            ).fetchone()
            try:
                previous_schema_version = 0 if row is None else int(row["value"])
            except (TypeError, ValueError) as exc:
                raise sqlite3.DatabaseError(
                    "project index schema version is corrupt"
                ) from exc
            if previous_schema_version > self._SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    "project index schema is newer than this runtime"
                )
            self._migrate_v1_columns()
            self._migrate_workspace_binding_columns()
            self._migrate_workspace_registry(
                # Schema 5 only adds package descriptors; v4 bindings remain valid.
                quarantine_legacy=previous_schema_version < 4
            )
            self._connection.execute(
                """
                INSERT INTO project_index_metadata (key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(self._SCHEMA_VERSION),),
            )

    def _migrate_v1_columns(self) -> None:
        additions = {
            "project_index_snapshots": (
                ("manifest_hash", "TEXT NOT NULL DEFAULT ''"),
                ("parser_set_hash", "TEXT NOT NULL DEFAULT ''"),
                ("head", "TEXT"),
                ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
                ("binding_state", "TEXT NOT NULL DEFAULT 'active'"),
            ),
            "project_index_nodes": (
                ("extractor_id", "TEXT NOT NULL DEFAULT ''"),
                ("extractor_version", "TEXT NOT NULL DEFAULT ''"),
                ("provenance", "TEXT NOT NULL DEFAULT 'observed'"),
                ("start_byte", "INTEGER NOT NULL DEFAULT 0"),
                ("end_byte", "INTEGER NOT NULL DEFAULT 0"),
            ),
            "project_index_edges": (
                ("path", "TEXT NOT NULL DEFAULT ''"),
                ("start_line", "INTEGER NOT NULL DEFAULT 1"),
                ("end_line", "INTEGER NOT NULL DEFAULT 1"),
                ("start_byte", "INTEGER NOT NULL DEFAULT 0"),
                ("end_byte", "INTEGER NOT NULL DEFAULT 0"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("extractor_id", "TEXT NOT NULL DEFAULT ''"),
                ("extractor_version", "TEXT NOT NULL DEFAULT ''"),
                ("provenance", "TEXT NOT NULL DEFAULT 'observed'"),
            ),
            "project_index_query_receipts": (
                ("package_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ),
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"])
                for row in self._connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, definition in columns:
                if name not in existing:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

    def _migrate_workspace_binding_columns(self) -> None:
        existing = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(project_index_snapshot_bindings)"
            ).fetchall()
        }
        if "root_identity" not in existing:
            self._connection.execute(
                """
                ALTER TABLE project_index_snapshot_bindings
                ADD COLUMN root_identity TEXT NOT NULL DEFAULT ''
                """
            )

    def _migrate_workspace_registry(self, *, quarantine_legacy: bool) -> None:
        """Quarantine snapshots that predate workspace-bound snapshot identifiers."""
        rows = self._connection.execute(
            """
            SELECT snapshot_id, workspace, workspace_id, binding_state
            FROM project_index_snapshots
            """
        ).fetchall()
        for row in rows:
            snapshot_id = str(row["snapshot_id"])
            bindings = self._connection.execute(
                """
                SELECT workspace_id, binding_state, root_identity
                FROM project_index_snapshot_bindings
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()
            if bindings:
                if quarantine_legacy:
                    binding_ids = {
                        str(binding["workspace_id"])
                        for binding in bindings
                        if is_workspace_id(str(binding["workspace_id"]))
                    }
                    for binding in bindings:
                        workspace_id = str(binding["workspace_id"])
                        root_identity = str(binding["root_identity"])
                        if not root_identity:
                            registration = self.get_workspace_registration(workspace_id)
                            root_identity = (
                                "" if registration is None else registration.identity
                            )
                        self._connection.execute(
                            """
                            UPDATE project_index_snapshot_bindings
                            SET binding_state = 'historical_unverified', root_identity = ?
                            WHERE workspace_id = ? AND snapshot_id = ?
                            """,
                            (root_identity, workspace_id, snapshot_id),
                        )
                    self._rekey_legacy_syncs(snapshot_id, binding_ids)
                    self._clear_legacy_snapshot_reference(snapshot_id)
                continue
            workspace_id = str(row["workspace_id"])
            historical_root = str(row["workspace"])
            if not is_workspace_id(workspace_id):
                if not historical_root:
                    self._clear_legacy_snapshot_reference(snapshot_id)
                    continue
                workspace_id = workspace_id_for_serialized_path(historical_root)
            root_identity = _historical_root_identity(historical_root)
            if not root_identity:
                registration = self.get_workspace_registration(workspace_id)
                root_identity = "" if registration is None else registration.identity
            self._connection.execute(
                """
                INSERT INTO project_index_snapshot_bindings
                    (workspace_id, snapshot_id, binding_state, root_identity)
                VALUES (?, ?, ?, ?)
                """,
                (workspace_id, snapshot_id, "historical_unverified", root_identity),
            )
            self._clear_legacy_snapshot_reference(snapshot_id)
            self._connection.execute(
                """
                UPDATE project_index_syncs
                SET workspace = ?
                WHERE snapshot_id = ?
                """,
                (workspace_id, snapshot_id),
            )

    def _rekey_legacy_syncs(self, snapshot_id: str, workspace_ids: set[str]) -> None:
        """Replace only legacy path pointers that prove one existing binding."""
        if not workspace_ids:
            return
        rows = self._connection.execute(
            """
            SELECT sequence, workspace
            FROM project_index_syncs
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        for row in rows:
            workspace = str(row["workspace"])
            if workspace in workspace_ids:
                continue
            workspace_id = workspace_id_for_serialized_path(workspace)
            if workspace_id not in workspace_ids:
                continue
            self._connection.execute(
                "UPDATE project_index_syncs SET workspace = ? WHERE sequence = ?",
                (workspace_id, int(row["sequence"])),
            )

    def _clear_legacy_snapshot_reference(self, snapshot_id: str) -> None:
        self._connection.execute(
            """
            UPDATE project_index_snapshots
            SET workspace = '', workspace_id = '', binding_state = 'active'
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield cursor
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _with_packages(self, snapshot: IndexSnapshot) -> IndexSnapshot:
        return replace(snapshot, packages=self.packages(snapshot.snapshot_id))

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> IndexSnapshot:
        return IndexSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            workspace=str(row["workspace_id"] or row["workspace"]),
            state=IndexState(str(row["state"])),
            file_count=int(row["file_count"]),
            blob_count=int(row["blob_count"]),
            reused_blob_count=int(row["reused_blob_count"]),
            node_count=int(row["node_count"]),
            edge_count=int(row["edge_count"]),
            gap_count=int(row["gap_count"]),
            manifest_hash=str(row["manifest_hash"]),
            parser_set_hash=str(row["parser_set_hash"]),
            head=None if row["head"] is None else str(row["head"]),
            workspace_id=str(row["workspace_id"] or row["workspace"]),
            binding_state=str(row["binding_state"]),
        )

    @staticmethod
    def _bound_snapshot_from_row(row: sqlite3.Row, workspace_id: str) -> IndexSnapshot:
        return replace(
            ProjectIndexStore._snapshot_from_row(row),
            workspace=workspace_id,
            workspace_id=workspace_id,
            binding_state=str(row["workspace_binding_state"]),
        )

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> IndexNode:
        return IndexNode(
            node_id=str(row["node_id"]),
            kind=str(row["kind"]),
            path=str(row["path"]),
            name=str(row["name"]),
            qualified_name=str(row["qualified_name"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            content_hash=str(row["content_hash"]),
            attributes=tuple(
                (str(key), str(value)) for key, value in json.loads(row["attributes"])
            ),
            extractor_id=str(row["extractor_id"]),
            extractor_version=str(row["extractor_version"]),
            provenance=str(row["provenance"]),
            start_byte=int(row["start_byte"]),
            end_byte=int(row["end_byte"]),
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> IndexEdge:
        return IndexEdge(
            edge_id=str(row["edge_id"]),
            source_id=str(row["source_id"]),
            target_id=str(row["target_id"]),
            relation=str(row["relation"]),
            path=str(row["path"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            start_byte=int(row["start_byte"]),
            end_byte=int(row["end_byte"]),
            content_hash=str(row["content_hash"]),
            extractor_id=str(row["extractor_id"]),
            extractor_version=str(row["extractor_version"]),
            provenance=str(row["provenance"]),
        )

    @staticmethod
    def _query_receipt_from_row(row: sqlite3.Row) -> QueryReceipt:
        return QueryReceipt(
            trace_id=str(row["trace_id"]),
            snapshot_id=str(row["snapshot_id"]),
            query=str(row["query_text"]),
            mode=str(row["mode"]),
            node_kinds=tuple(str(value) for value in json.loads(row["node_kinds"])),
            relations=tuple(str(value) for value in json.loads(row["relations"])),
            max_nodes=int(row["max_nodes"]),
            max_depth=int(row["max_depth"]),
            source_lines=int(row["source_lines"]),
            byte_budget=int(row["byte_budget"]),
            allow_miss_escape=bool(row["allow_miss_escape"]),
            miss_escape_used=bool(row["miss_escape_used"]),
            returned_node_ids=tuple(
                str(value) for value in json.loads(row["returned_node_ids"])
            ),
            returned_edge_ids=tuple(
                str(value) for value in json.loads(row["returned_edge_ids"])
            ),
            returned_source_windows=tuple(
                (str(path), int(start), int(end), str(content_hash))
                for path, start, end, content_hash in json.loads(
                    row["returned_source_windows"]
                )
            ),
            gaps=tuple(
                CoverageGap(str(path), str(code), str(message))
                for path, code, message in json.loads(row["gaps"])
            ),
            truncated=bool(row["truncated"]),
            package_ids=tuple(str(value) for value in json.loads(row["package_ids"])),
        )


def _historical_root_identity(workspace_root: str) -> str:
    try:
        return workspace_identity(canonical_workspace_root(workspace_root))
    except IndexError:
        return ""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _retention_preview_identifier(
    candidates: Sequence[IndexRetentionCandidate], protected_snapshot_ids: Sequence[str]
) -> str:
    payload = {
        "candidates": [
            {
                "snapshot_id": candidate.snapshot_id,
                "reasons": candidate.reasons,
                "estimated_row_count": candidate.estimated_row_count,
                "estimated_reclaimable_bytes": candidate.estimated_reclaimable_bytes,
            }
            for candidate in candidates
        ],
        "protected_snapshot_ids": tuple(protected_snapshot_ids),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
