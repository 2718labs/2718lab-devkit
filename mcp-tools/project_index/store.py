"""Append-only SQLite persistence for immutable project-index snapshots."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .extractors import (
    ParsedExtraction,
    SourceFile,
    deserialize_parsed_extraction,
    serialize_parsed_extraction,
)
from .models import (
    CoverageGap,
    IndexEdge,
    IndexNode,
    IndexSnapshot,
    IndexState,
    QueryReceipt,
)


class StoreError(RuntimeError):
    """Raised when durable index data cannot be read or written."""


class ProjectIndexStore:
    """Store immutable snapshots and path-neutral parser artifacts."""

    _SCHEMA_VERSION = 2

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.database_path), isolation_level=None
        )
        self._closed = False
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

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
        parsed_cache_entries: Sequence[tuple[SourceFile, ParsedExtraction]] = (),
    ) -> IndexSnapshot:
        """Insert a complete graph once and append a workspace sync pointer."""
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
                        (snapshot_id, workspace, state, include_paths, file_count, blob_count,
                         reused_blob_count, node_count, edge_count, gap_count, manifest_hash,
                         parser_set_hash, head)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.workspace,
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
                stored = snapshot
            else:
                stored = self._snapshot_from_row(existing)
                if stored.workspace != snapshot.workspace:
                    raise StoreError("snapshot identifier collision across workspaces")
            cursor.execute(
                "INSERT INTO project_index_syncs (workspace, snapshot_id) VALUES (?, ?)",
                (snapshot.workspace, snapshot.snapshot_id),
            )
        return stored

    def put_query_receipt(self, receipt: QueryReceipt) -> QueryReceipt:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO project_index_query_receipts
                    (trace_id, snapshot_id, query_text, mode, node_kinds, relations,
                     max_nodes, max_depth, source_lines, byte_budget, allow_miss_escape,
                     miss_escape_used, returned_node_ids, returned_edge_ids,
                     returned_source_windows, gaps, truncated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return None if row is None else self._snapshot_from_row(row)

    def latest_snapshot(self, workspace: str) -> IndexSnapshot | None:
        row = self._connection.execute(
            """
            SELECT snapshots.*
            FROM project_index_syncs AS syncs
            JOIN project_index_snapshots AS snapshots USING (snapshot_id)
            WHERE syncs.workspace = ?
            ORDER BY syncs.sequence DESC
            LIMIT 1
            """,
            (workspace,),
        ).fetchone()
        return None if row is None else self._snapshot_from_row(row)

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
                    truncated INTEGER NOT NULL
                );
                """
            )
            self._migrate_v1_columns()
            row = self._connection.execute(
                "SELECT value FROM project_index_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > self._SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    "project index schema is newer than this runtime"
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

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> IndexSnapshot:
        return IndexSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            workspace=str(row["workspace"]),
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
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
