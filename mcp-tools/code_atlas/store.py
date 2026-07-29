"""Deterministic local SQLite/WAL graph and verified blob store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import fields
from pathlib import Path
from typing import Iterable

from .canonical import canonical_hash, canonical_id, canonical_json
from .models import (
    AtlasEdge,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    ImplementationPacket,
    IngestionReceipt,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec,
)
from .security import (
    MAX_GRAPH_DEPTH,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
    MAX_RECIPE_BYTES,
    MAX_TEMPLATE_BYTES,
    validate_fragment,
)


_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")


def _lexical_absolute(path: str | Path) -> Path:
    """Normalize ``.``/``..`` without resolving through a possible link."""

    return Path(os.path.abspath(os.fspath(path)))


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the attributes that identify a regular file across a read."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _unsafe_file_status(path: Path, value: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & 0x400
        or (callable(is_junction) and is_junction(path))
    )


def _capture_safe_path_chain(
    path: Path, *, require_regular: bool = False
) -> tuple[tuple[Path, tuple[int, int, int, int, int]], ...]:
    """Capture every safe component without resolving through a link."""

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise StoreConflictError()
    cursor = Path(parts[0])
    records: list[tuple[Path, tuple[int, int, int, int, int]]] = []
    try:
        for part in parts[1:]:
            cursor /= part
            item = cursor.lstat()
            if _unsafe_file_status(cursor, item):
                raise StoreConflictError()
            records.append((cursor, _file_identity(item)))
        final = absolute.lstat()
    except StoreConflictError:
        raise
    except OSError as exc:
        raise StoreConflictError() from exc
    if require_regular and not stat.S_ISREG(final.st_mode):
        raise StoreConflictError()
    if records and _file_identity(final) != records[-1][1]:
        raise StoreConflictError()
    return tuple(records)


def _assert_safe_existing_path(path: Path, *, require_regular: bool = False) -> None:
    """Reject any linked/reparse component without resolving through it."""

    _capture_safe_path_chain(path, require_regular=require_regular)


def _assert_path_chain_unchanged(
    records: tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
) -> None:
    """Fail closed if a checked component was replaced or modified."""

    for path, identity in records:
        try:
            value = path.lstat()
        except OSError as exc:
            raise StoreConflictError() from exc
        if _unsafe_file_status(path, value) or _file_identity(value) != identity:
            raise StoreConflictError()


def _capture_cas_directory_identities(
    cas_root: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    """Pin every existing CAS directory and its lexical parent chain."""

    root_chain = _capture_safe_path_chain(cas_root)
    try:
        root_status = cas_root.lstat()
    except OSError as exc:
        raise StoreConflictError() from exc
    if _unsafe_file_status(cas_root, root_status) or not stat.S_ISDIR(
        root_status.st_mode
    ):
        raise StoreConflictError()
    records = [
        (path, _object_identity_from_identity(identity))
        for path, identity in root_chain
    ]
    stack = [cas_root]
    seen = {cas_root}
    records.append((cas_root, _object_identity(root_status)))
    try:
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                value = child.lstat()
                if _unsafe_file_status(child, value):
                    raise StoreConflictError()
                if not stat.S_ISDIR(value.st_mode):
                    continue
                if child in seen:
                    raise StoreConflictError()
                seen.add(child)
                records.append((child, _object_identity(value)))
                stack.append(child)
    except StoreConflictError:
        raise
    except OSError as exc:
        raise StoreConflictError() from exc
    return tuple(records)


def _object_identity_from_identity(
    identity: tuple[int, int, int, int, int],
) -> tuple[int, int, int]:
    return identity[:3]


def _assert_cas_directory_identities(
    records: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> None:
    """Require all components pinned for this store instance to remain intact."""

    for path, identity in records:
        try:
            value = path.lstat()
        except OSError as exc:
            raise StoreConflictError() from exc
        if (
            _unsafe_file_status(path, value)
            or not stat.S_ISDIR(value.st_mode)
            or _object_identity(value) != identity
        ):
            raise StoreConflictError()


def _object_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return only the stable object identity of a file or directory."""

    return _file_identity(value)[:3]


def _safe_regular_identity(
    path: Path, *, optional: bool = False
) -> tuple[int, int, int, int, int] | None:
    """Read a file identity without resolving links or accepting a race."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise StoreConflictError() from None
    except OSError as exc:
        raise StoreConflictError() from exc
    if _unsafe_file_status(path, before) or not stat.S_ISREG(before.st_mode):
        raise StoreConflictError()
    _assert_safe_existing_path(path, require_regular=True)
    try:
        after = path.lstat()
    except OSError as exc:
        raise StoreConflictError() from exc
    if _file_identity(before) != _file_identity(after):
        raise StoreConflictError()
    return _file_identity(after)


def _snapshot_source_state(
    database: Path,
) -> tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None]:
    """Capture the exact durable DB/WAL generation used by a snapshot."""

    database_identity = _safe_regular_identity(database)
    if database_identity is None:
        raise StoreConflictError()
    return database_identity, _safe_regular_identity(
        Path(str(database) + "-wal"), optional=True
    )


def _copy_snapshot_file(
    source: Path, destination: Path
) -> tuple[int, int, int, int, int]:
    """Copy one stable no-follow source file into an exclusive scratch path."""

    before = _safe_regular_identity(source)
    if before is None:
        raise StoreConflictError()
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != before:
            raise StoreConflictError()
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        while True:
            chunk = os.read(source_descriptor, 65_536)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise StoreConflictError()
                offset += written
        if _file_identity(os.fstat(source_descriptor)) != before:
            raise StoreConflictError()
        os.fsync(destination_descriptor)
    except StoreConflictError:
        raise
    except OSError as exc:
        raise StoreConflictError() from exc
    finally:
        if destination_descriptor is not None:
            try:
                os.close(destination_descriptor)
            except OSError:
                pass
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
    if _safe_regular_identity(source) != before:
        raise StoreConflictError()
    return before


def _capture_snapshot_files(
    stage: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    """Capture only the direct regular files that SQLite may later clean up."""

    _assert_safe_existing_path(stage)
    try:
        stage_status = stage.lstat()
        if _unsafe_file_status(stage, stage_status) or not stat.S_ISDIR(
            stage_status.st_mode
        ):
            raise StoreConflictError()
        records: list[tuple[Path, tuple[int, int, int]]] = []
        for child in stage.iterdir():
            value = child.lstat()
            if _unsafe_file_status(child, value) or not stat.S_ISREG(value.st_mode):
                raise StoreConflictError()
            records.append((child, _object_identity(value)))
        return tuple(records)
    except StoreConflictError:
        raise
    except OSError as exc:
        raise StoreConflictError() from exc


def _cleanup_readonly_snapshot(
    stage: Path | None,
    scratch: Path | None,
    scratch_identity: tuple[int, int, int] | None,
    stage_identity: tuple[int, int, int] | None,
    files: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> None:
    """Remove only a still-proven private snapshot; never recurse blindly."""

    if (
        stage is None
        or scratch is None
        or scratch_identity is None
        or stage_identity is None
        or stage.parent != scratch
    ):
        return
    try:
        _assert_safe_existing_path(scratch)
        scratch_status = scratch.lstat()
        stage_status = stage.lstat()
        if (
            _unsafe_file_status(scratch, scratch_status)
            or _unsafe_file_status(stage, stage_status)
            or not stat.S_ISDIR(scratch_status.st_mode)
            or not stat.S_ISDIR(stage_status.st_mode)
            or _object_identity(scratch_status) != scratch_identity
            or _object_identity(stage_status) != stage_identity
        ):
            return
        for path, identity in files:
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            if (
                _unsafe_file_status(path, value)
                or not stat.S_ISREG(value.st_mode)
                or _object_identity(value) != identity
            ):
                continue
            path.unlink()
        stage.rmdir()
    except (OSError, StoreConflictError):
        return


def _cleanup_owned_scratch(
    scratch: Path, scratch_identity: tuple[int, int, int] | None
) -> None:
    """Remove only the empty private scratch root created for this reader."""

    if scratch_identity is None:
        return
    try:
        _assert_safe_existing_path(scratch)
        value = scratch.lstat()
        if (
            _unsafe_file_status(scratch, value)
            or not stat.S_ISDIR(value.st_mode)
            or _object_identity(value) != scratch_identity
        ):
            return
        scratch.rmdir()
    except (OSError, StoreConflictError):
        return


def _paths_overlap(left: Path, right: Path) -> bool:
    """Check lexical containment without resolving through a possible link."""

    left = _lexical_absolute(left)
    right = _lexical_absolute(right)
    try:
        left_value = os.path.normcase(os.fspath(left))
        right_value = os.path.normcase(os.fspath(right))
        common = os.path.commonpath((left_value, right_value))
        return common in {left_value, right_value}
    except ValueError:
        return False


class StoreConflictError(ValueError):
    """A safe, stable error raised when durable content conflicts."""

    code = "store_conflict"

    def __init__(self, message: str = "store conflict") -> None:
        super().__init__(message)


_SCHEMA = """
CREATE TABLE atlas_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE atlas_nodes (node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL, extractor_id TEXT NOT NULL, extractor_version TEXT NOT NULL, provenance TEXT NOT NULL, source_hashes_json TEXT NOT NULL, created_at TEXT NOT NULL, superseded_at TEXT, quarantine_state TEXT NOT NULL);
CREATE TABLE atlas_edges (edge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), target_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), relation TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX atlas_edges_source ON atlas_edges(source_id, relation);
CREATE INDEX atlas_edges_target ON atlas_edges(target_id, relation);
CREATE TABLE atlas_recipes (recipe_id TEXT PRIMARY KEY REFERENCES atlas_nodes(node_id), intent_id TEXT NOT NULL, language TEXT NOT NULL, framework TEXT NOT NULL, layer TEXT NOT NULL CHECK (layer IN ('bundled', 'local')), version INTEGER NOT NULL, manifest_hash TEXT NOT NULL, repository_signature TEXT NOT NULL, state TEXT NOT NULL, supersedes_recipe_id TEXT);
CREATE INDEX atlas_recipe_match ON atlas_recipes(intent_id, language, framework, state);
CREATE TABLE atlas_recipe_nodes (recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id), node_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), PRIMARY KEY (recipe_id, node_id));
CREATE TABLE atlas_recipe_edges (recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id), edge_id TEXT NOT NULL REFERENCES atlas_edges(edge_id), PRIMARY KEY (recipe_id, edge_id));
CREATE TABLE atlas_blobs (blob_hash TEXT PRIMARY KEY, size INTEGER NOT NULL, media_type TEXT NOT NULL);
CREATE TABLE atlas_packet_receipts (packet_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, packet_json TEXT NOT NULL, packet_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE atlas_ingestion_receipts (ingestion_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, status TEXT NOT NULL, episode_id TEXT NOT NULL, recipe_id TEXT NOT NULL, reasons_json TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class AtlasStore:
    def __init__(
        self, database_path: str | Path, cas_root: str | Path | None = None
    ) -> None:
        self._database_path = _lexical_absolute(database_path)
        self._cas_root = (
            _lexical_absolute(cas_root)
            if cas_root is not None
            else self._database_path.parent / "cas"
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._cas_root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._database_path, timeout=30.0)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._cas_directory_identities = _capture_cas_directory_identities(
            self._cas_root
        )

    @classmethod
    def open_readonly(
        cls,
        database_path: str | Path,
        cas_root: str | Path,
        *,
        scratch_root: str | Path | None = None,
    ) -> "AtlasStore":
        """Open a stable durable DB/WAL copy without touching the durable root.

        SQLite's WAL-aware ``mode=ro`` can create ``-shm`` state beside the
        database. The reader therefore operates exclusively on a verified
        scratch snapshot, preserving a live durable WAL while never creating,
        deleting, or writing a durable sidecar. When ``scratch_root`` is
        omitted, the snapshot gets a private root beneath ``CODEX_TASK_TEMP``
        (or the process temp directory) and removes that empty root on close.
        """

        database = _lexical_absolute(database_path)
        cas = _lexical_absolute(cas_root)
        owned_scratch: Path | None = None
        scratch: Path | None = None
        connection: sqlite3.Connection | None = None
        stage: Path | None = None
        scratch_identity: tuple[int, int, int] | None = None
        stage_identity: tuple[int, int, int] | None = None
        snapshot_files: tuple[tuple[Path, tuple[int, int, int]], ...] = ()
        try:
            database_chain = _capture_safe_path_chain(database, require_regular=True)
            _assert_safe_existing_path(cas)
            if not stat.S_ISDIR(cas.lstat().st_mode):
                raise StoreConflictError()
            cas_directory_identities = _capture_cas_directory_identities(cas)
            if scratch_root is None:
                configured_temp = os.environ.get("CODEX_TASK_TEMP")
                scratch_parent = (
                    _lexical_absolute(configured_temp)
                    if configured_temp
                    else _lexical_absolute(tempfile.gettempdir())
                )
                scratch_parent_chain = _capture_safe_path_chain(scratch_parent)
                scratch_parent_status = scratch_parent.lstat()
                if (
                    _unsafe_file_status(scratch_parent, scratch_parent_status)
                    or not stat.S_ISDIR(scratch_parent_status.st_mode)
                    or _paths_overlap(scratch_parent, database.parent)
                    or _paths_overlap(scratch_parent, cas)
                ):
                    raise StoreConflictError()
                _assert_path_chain_unchanged(database_chain)
                _assert_cas_directory_identities(cas_directory_identities)
                _assert_path_chain_unchanged(scratch_parent_chain)
                owned_scratch = _lexical_absolute(
                    Path(
                        tempfile.mkdtemp(
                            prefix=".code-atlas-readonly-root-", dir=scratch_parent
                        )
                    )
                )
                scratch = owned_scratch
            else:
                scratch = _lexical_absolute(scratch_root)
            if scratch is None:
                raise StoreConflictError()
            _assert_safe_existing_path(scratch)
            if not stat.S_ISDIR(scratch.lstat().st_mode):
                raise StoreConflictError()
            if _paths_overlap(scratch, database.parent) or _paths_overlap(scratch, cas):
                raise StoreConflictError()
            scratch_status = scratch.lstat()
            if _unsafe_file_status(scratch, scratch_status):
                raise StoreConflictError()
            scratch_identity = _object_identity(scratch_status)
            source_state = _snapshot_source_state(database)
            stage = Path(tempfile.mkdtemp(prefix=".code-atlas-readonly-", dir=scratch))
            stage_status = stage.lstat()
            if _unsafe_file_status(stage, stage_status) or not stat.S_ISDIR(
                stage_status.st_mode
            ):
                raise StoreConflictError()
            stage_identity = _object_identity(stage_status)
            if _object_identity(scratch.lstat()) != scratch_identity:
                raise StoreConflictError()
            snapshot_database = stage / "code-atlas.sqlite3"
            _copy_snapshot_file(database, snapshot_database)
            if source_state[1] is not None:
                _copy_snapshot_file(
                    Path(str(database) + "-wal"),
                    Path(str(snapshot_database) + "-wal"),
                )
            if _snapshot_source_state(database) != source_state:
                raise StoreConflictError()
            _assert_safe_existing_path(stage)
            if _object_identity(stage.lstat()) != stage_identity:
                raise StoreConflictError()
            uri = _lexical_absolute(snapshot_database).as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30.0)
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None or row[0] != "1":
                raise StoreConflictError()
            if _snapshot_source_state(database) != source_state:
                raise StoreConflictError()
            _assert_path_chain_unchanged(database_chain)
            _assert_cas_directory_identities(cas_directory_identities)
            snapshot_files = _capture_snapshot_files(stage)
            instance = cls.__new__(cls)
            instance._database_path = snapshot_database
            instance._cas_root = cas
            instance._cas_directory_identities = cas_directory_identities
            instance._conn = connection
            instance._readonly_snapshot = (
                stage,
                scratch,
                scratch_identity,
                stage_identity,
                snapshot_files,
            )
            instance._readonly_owned_scratch = owned_scratch
            return instance
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            if stage is not None:
                try:
                    snapshot_files = _capture_snapshot_files(stage)
                except StoreConflictError:
                    pass
            _cleanup_readonly_snapshot(
                stage,
                scratch,
                scratch_identity,
                stage_identity,
                snapshot_files,
            )
            if owned_scratch is not None:
                _cleanup_owned_scratch(owned_scratch, scratch_identity)
            if isinstance(exc, StoreConflictError):
                raise
            raise StoreConflictError() from exc

    def _migrate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atlas_metadata'"
            ).fetchone()
            if exists is None:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
            current = self._conn.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()
            if current is None:
                self._conn.execute(
                    "INSERT INTO atlas_metadata(key,value) VALUES ('schema_version','1')"
                )
            elif current[0] != "1":
                raise StoreConflictError("unsupported schema version")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            snapshot = getattr(self, "_readonly_snapshot", None)
            if snapshot is not None:
                _cleanup_readonly_snapshot(*snapshot)
            owned_scratch = getattr(self, "_readonly_owned_scratch", None)
            if owned_scratch is not None:
                _cleanup_owned_scratch(owned_scratch, snapshot[2])

    def schema_version(self) -> int:
        return int(
            self._conn.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()[0]
        )

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def foreign_keys_enabled(self) -> bool:
        return bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])

    @staticmethod
    def _node_identity(node: AtlasNode) -> str:
        return canonical_id(
            node.kind.value,
            node.payload,
            schema_version=node.schema_version,
            extractor_id=node.extractor_id,
            extractor_version=node.extractor_version,
            provenance=node.provenance,
            source_hashes=node.source_hashes,
        )

    @staticmethod
    def _edge_identity(edge: AtlasEdge) -> str:
        return canonical_hash(
            {
                "relation": edge.relation.value,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "schema_version": edge.schema_version,
                "provenance": edge.provenance,
                "payload": edge.payload,
            }
        )

    def put_nodes(self, nodes: Iterable[AtlasNode]) -> tuple[AtlasNode, ...]:
        items = tuple(nodes)
        for node in items:
            if self._node_identity(node) != node.node_id:
                raise StoreConflictError("node identity conflict")
        with self._conn:
            for node in items:
                immutable = (
                    node.kind.value,
                    canonical_json(node.payload),
                    node.schema_version,
                    node.extractor_id,
                    node.extractor_version,
                    node.provenance,
                    canonical_json(node.source_hashes),
                )
                row = self._conn.execute(
                    "SELECT kind,payload_json,schema_version,extractor_id,extractor_version,provenance,source_hashes_json FROM atlas_nodes WHERE node_id=?",
                    (node.node_id,),
                ).fetchone()
                if row is not None and tuple(row) != immutable:
                    raise StoreConflictError("node immutable payload conflict")
                if row is None:
                    self._conn.execute(
                        "INSERT INTO atlas_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            node.node_id,
                            *immutable,
                            node.created_at or "",
                            node.superseded_at,
                            node.quarantine_state or "",
                        ),
                    )
        return items

    def put_edges(self, edges: Iterable[AtlasEdge]) -> tuple[AtlasEdge, ...]:
        items = tuple(edges)
        for edge in items:
            if self._edge_identity(edge) != edge.edge_id:
                raise StoreConflictError("edge identity conflict")
            endpoints = self._conn.execute(
                "SELECT node_id,kind FROM atlas_nodes WHERE node_id IN (?,?)",
                (edge.source_id, edge.target_id),
            ).fetchall()
            if (
                len(endpoints) != 2
                or dict(endpoints).get(edge.source_id) != edge.source_kind.value
                or dict(endpoints).get(edge.target_id) != edge.target_kind.value
            ):
                raise StoreConflictError("edge endpoint conflict")
        with self._conn:
            for edge in items:
                immutable = (
                    edge.source_id,
                    edge.target_id,
                    edge.relation.value,
                    canonical_json(edge.payload),
                    edge.schema_version,
                    edge.provenance,
                )
                row = self._conn.execute(
                    "SELECT source_id,target_id,relation,payload_json,schema_version,provenance FROM atlas_edges WHERE edge_id=?",
                    (edge.edge_id,),
                ).fetchone()
                if row is not None and tuple(row) != immutable:
                    raise StoreConflictError("edge immutable payload conflict")
                if row is None:
                    self._conn.execute(
                        "INSERT INTO atlas_edges VALUES (?,?,?,?,?,?,?,?)",
                        (edge.edge_id, *immutable, edge.created_at or ""),
                    )
        return items

    def put_recipe(
        self,
        manifest: RecipeManifest,
        *,
        node_ids: Iterable[str] = (),
        edge_ids: Iterable[str] = (),
    ) -> str:
        linked_nodes = tuple(node_ids)
        linked_edges = tuple(edge_ids)
        immutable = (
            manifest.intent_id,
            manifest.language_name,
            manifest.framework_name or "",
            manifest.layer,
            manifest.version,
            manifest.manifest_hash,
            manifest.repository_signature,
            manifest.quarantine_state or "ready",
            manifest.superseded_ids[0] if manifest.superseded_ids else None,
        )
        with self._conn:
            recipe_row = self._conn.execute(
                "SELECT kind FROM atlas_nodes WHERE node_id=?", (manifest.recipe_id,)
            ).fetchone()
            if recipe_row is None or recipe_row[0] != NodeKind.RECIPE.value:
                raise StoreConflictError("recipe node conflict")
            row = self._conn.execute(
                "SELECT intent_id,language,framework,layer,version,manifest_hash,repository_signature,state,supersedes_recipe_id FROM atlas_recipes WHERE recipe_id=?",
                (manifest.recipe_id,),
            ).fetchone()
            if row is not None and tuple(row) != immutable:
                raise StoreConflictError("recipe conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_recipes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (manifest.recipe_id, *immutable),
                )
            for node_id in linked_nodes:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM atlas_nodes WHERE node_id=?", (node_id,)
                    ).fetchone()
                    is None
                ):
                    raise StoreConflictError("recipe node link conflict")
                self._conn.execute(
                    "INSERT OR IGNORE INTO atlas_recipe_nodes VALUES (?,?)",
                    (manifest.recipe_id, node_id),
                )
            for edge_id in linked_edges:
                row_edge = self._conn.execute(
                    "SELECT source_id,target_id FROM atlas_edges WHERE edge_id=?",
                    (edge_id,),
                ).fetchone()
                if (
                    row_edge is None
                    or row_edge[0] not in linked_nodes
                    or row_edge[1] not in linked_nodes
                ):
                    raise StoreConflictError("recipe edge link conflict")
                self._conn.execute(
                    "INSERT OR IGNORE INTO atlas_recipe_edges VALUES (?,?)",
                    (manifest.recipe_id, edge_id),
                )
        return manifest.recipe_id

    def _blob_path(self, blob_hash: str) -> Path:
        match = _HASH.fullmatch(blob_hash)
        if not match:
            raise StoreConflictError("blob hash conflict")
        digest = match.group(1)
        return self._cas_root / "sha256" / digest[:2] / digest[2:]

    @staticmethod
    def _read_blob_file(path: Path) -> bytes:
        try:
            if not path.is_file():
                raise StoreConflictError("blob path conflict")
            return path.read_bytes()
        except OSError as exc:
            raise StoreConflictError("blob path conflict") from exc

    def put_blob(
        self,
        blob_hash: str,
        content: bytes,
        media_type: str = "application/octet-stream",
    ) -> str:
        if not isinstance(content, bytes):
            raise StoreConflictError("blob hash conflict")
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if blob_hash != actual:
            raise StoreConflictError("blob hash conflict")
        _assert_cas_directory_identities(self._cas_directory_identities)
        path = self._blob_path(blob_hash)
        created_path = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            path.parent.mkdir(parents=True, exist_ok=True)
            path_exists = path.exists()
            path_is_file = path.is_file() if path_exists else False
            row = self._conn.execute(
                "SELECT size,media_type FROM atlas_blobs WHERE blob_hash=?",
                (blob_hash,),
            ).fetchone()
            if (row is None) == path_exists:
                raise StoreConflictError("blob consistency conflict")
            if path_exists and not path_is_file:
                raise StoreConflictError("blob path conflict")
            values = (len(content), media_type)
            if row is not None and tuple(row) != values:
                raise StoreConflictError("blob metadata conflict")
            if path_exists:
                if self._read_blob_file(path) != content:
                    raise StoreConflictError("blob filesystem conflict")
            else:
                temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with open(temp, "xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                    created_path = True
                except OSError as exc:
                    raise StoreConflictError("blob write conflict") from exc
                finally:
                    try:
                        if temp.exists():
                            temp.unlink()
                    except OSError as exc:
                        raise StoreConflictError("blob write conflict") from exc
                if self._read_blob_file(path) != content:
                    raise StoreConflictError("blob verification conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_blobs VALUES (?,?,?)", (blob_hash, *values)
                )
            self._conn.commit()
        except OSError as exc:
            self._conn.rollback()
            if created_path:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise StoreConflictError("blob path conflict") from exc
        except Exception:
            self._conn.rollback()
            if created_path:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        self._cas_directory_identities = _capture_cas_directory_identities(
            self._cas_root
        )
        return blob_hash

    def read_blob_verified(self, blob_hash: str, *, max_bytes: int) -> bytes:
        """Read a bounded text blob through the CAS no-follow integrity boundary."""

        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_RECIPE_BYTES
            or _HASH.fullmatch(blob_hash) is None
        ):
            raise StoreConflictError()
        path = self._blob_path(blob_hash)
        row = self._conn.execute(
            "SELECT size FROM atlas_blobs WHERE blob_hash=?", (blob_hash,)
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0 or row[0] > max_bytes:
            raise StoreConflictError()
        descriptor: int | None = None
        try:
            _assert_safe_existing_path(self._cas_root)
            _assert_cas_directory_identities(self._cas_directory_identities)
            path_chain = _capture_safe_path_chain(path, require_regular=True)
            before = path.lstat()
            if not path_chain or _file_identity(before) != path_chain[-1][1]:
                raise StoreConflictError()
            flags = (
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _file_identity(
                before
            ) != _file_identity(opened):
                raise StoreConflictError()
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(65_536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            after_open = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(after_open):
                raise StoreConflictError()
            if total > max_bytes:
                raise StoreConflictError()
        except StoreConflictError:
            raise
        except OSError as exc:
            raise StoreConflictError() from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise StoreConflictError() from exc
        try:
            _assert_cas_directory_identities(self._cas_directory_identities)
            _assert_path_chain_unchanged(path_chain)
            post = path.lstat()
        except StoreConflictError:
            raise
        if _file_identity(before) != _file_identity(post):
            raise StoreConflictError()
        content = b"".join(chunks)
        if (
            len(content) != row[0]
            or "sha256:" + hashlib.sha256(content).hexdigest() != blob_hash
        ):
            raise StoreConflictError()
        try:
            validate_fragment(content, max_bytes=max_bytes)
        except Exception as exc:
            raise StoreConflictError() from exc
        return content

    def read_blob(self, blob_hash: str) -> bytes:
        """Compatibility alias for the verified bounded template/blob reader."""

        try:
            return self.read_blob_verified(blob_hash, max_bytes=MAX_TEMPLATE_BYTES)
        except StoreConflictError as exc:
            raise StoreConflictError("blob conflict") from exc

    def put_packet(self, packet: ImplementationPacket, *, created_at: str = "") -> str:
        payload = canonical_json(packet.to_dict())
        digest = canonical_hash(packet.to_dict())
        with self._conn:
            row = self._conn.execute(
                "SELECT snapshot_id,packet_json,packet_hash FROM atlas_packet_receipts WHERE packet_id=?",
                (packet.packet_id,),
            ).fetchone()
            values = (packet.snapshot_id, payload, digest)
            if row is not None and tuple(row) != values:
                raise StoreConflictError("packet receipt conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_packet_receipts VALUES (?,?,?,?,?)",
                    (packet.packet_id, *values, created_at),
                )
        return packet.packet_id

    @staticmethod
    def _hydrate_packet(value: object) -> ImplementationPacket:
        """Hydrate one canonical packet with no permissive fallback codec."""

        expected = {item.name for item in fields(ImplementationPacket)}
        if type(value) is not dict or set(value) != expected:
            raise StoreConflictError()
        text_fields = {
            "packet_id",
            "trace_id",
            "workspace",
            "snapshot_id",
            "recipe_id",
            "next_action",
        }
        if any(not isinstance(value[name], str) for name in text_fields):
            raise StoreConflictError()
        sequence_fields = {
            "node_ids",
            "edge_ids",
            "evidence_windows",
            "evidence_hashes",
            "operations",
            "slots",
            "constraints",
            "dependencies",
            "tests",
            "gaps",
            "source_hashes",
            "template_hashes",
            "receipt_hashes",
        }
        if any(type(value[name]) is not list for name in sequence_fields):
            raise StoreConflictError()
        string_sequences = {
            "node_ids",
            "edge_ids",
            "evidence_hashes",
            "gaps",
            "source_hashes",
            "template_hashes",
            "receipt_hashes",
        }
        if any(
            any(not isinstance(item, str) for item in value[name])
            for name in string_sequences
        ):
            raise StoreConflictError()

        def record_items(
            raw: object, record_type: type[object]
        ) -> tuple[dict[str, object], ...]:
            names = {item.name for item in fields(record_type)}
            if type(raw) is not list or any(
                type(item) is not dict or set(item) != names for item in raw
            ):
                raise StoreConflictError()
            return tuple(raw)

        operations = record_items(value["operations"], TemplateOperation)
        if any(
            any(not isinstance(item[name], str) for name in item) for item in operations
        ):
            raise StoreConflictError()
        slots = record_items(value["slots"], SlotSpec)
        if any(
            not isinstance(item["name"], str)
            or not isinstance(item["type"], str)
            or type(item["required"]) is not bool
            for item in slots
        ):
            raise StoreConflictError()
        constraints = record_items(value["constraints"], ConstraintSpec)
        if any(
            not isinstance(item["kind"], str) or not isinstance(item["subject"], str)
            for item in constraints
        ):
            raise StoreConflictError()
        dependencies = record_items(value["dependencies"], DependencySpec)
        if any(
            any(not isinstance(item[name], str) for name in item)
            for item in dependencies
        ):
            raise StoreConflictError()
        tests = record_items(value["tests"], TestSpec)
        if any(
            type(item["argv"]) is not list
            or any(not isinstance(argument, str) for argument in item["argv"])
            or type(item["expected_exit_code"]) is not int
            for item in tests
        ):
            raise StoreConflictError()
        if any(type(item) is not dict for item in value["evidence_windows"]):
            raise StoreConflictError()
        try:
            return ImplementationPacket(
                packet_id=value["packet_id"],
                trace_id=value["trace_id"],
                workspace=value["workspace"],
                snapshot_id=value["snapshot_id"],
                recipe_id=value["recipe_id"],
                node_ids=tuple(value["node_ids"]),
                edge_ids=tuple(value["edge_ids"]),
                evidence_windows=tuple(value["evidence_windows"]),
                evidence_hashes=tuple(value["evidence_hashes"]),
                operations=tuple(TemplateOperation(**item) for item in operations),
                slots=tuple(SlotSpec(**item) for item in slots),
                constraints=tuple(ConstraintSpec(**item) for item in constraints),
                dependencies=tuple(DependencySpec(**item) for item in dependencies),
                tests=tuple(
                    TestSpec(tuple(item["argv"]), item["expected_exit_code"])
                    for item in tests
                ),
                gaps=tuple(value["gaps"]),
                source_hashes=tuple(value["source_hashes"]),
                template_hashes=tuple(value["template_hashes"]),
                receipt_hashes=tuple(value["receipt_hashes"]),
                next_action=value["next_action"],
            )
        except (TypeError, ValueError) as exc:
            raise StoreConflictError() from exc

    def get_packet(self, packet_id: str) -> ImplementationPacket | None:
        """Return a structurally strict packet for backwards-compatible readers."""

        row = self._conn.execute(
            "SELECT packet_json FROM atlas_packet_receipts WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._hydrate_packet(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError, StoreConflictError) as exc:
            raise StoreConflictError() from exc

    def get_packet_verified(self, packet_id: str) -> ImplementationPacket | None:
        """Read one packet row and prove its canonical integrity before use."""

        if not isinstance(packet_id, str) or _HASH.fullmatch(packet_id) is None:
            raise StoreConflictError()
        row = self._conn.execute(
            "SELECT snapshot_id,packet_json,packet_hash FROM atlas_packet_receipts "
            "WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        if row is None:
            return None
        row_snapshot, raw, stored_hash = row
        try:
            if (
                not isinstance(row_snapshot, str)
                or not isinstance(raw, str)
                or not isinstance(stored_hash, str)
                or len(raw.encode("utf-8")) > MAX_PACKET_BYTES
                or _HASH.fullmatch(stored_hash) is None
            ):
                raise StoreConflictError()
            decoded = json.loads(raw)
            if canonical_json(decoded) != raw:
                raise StoreConflictError()
            packet = self._hydrate_packet(decoded)
            payload = packet.to_dict()
            packet_value = payload.pop("packet_id")
            if (
                packet_value != packet_id
                or packet.snapshot_id != row_snapshot
                or canonical_hash(packet.to_dict()) != stored_hash
                or canonical_hash(payload) != packet_id
            ):
                raise StoreConflictError()
            return packet
        except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
            raise StoreConflictError() from exc

    def put_ingestion_receipt(self, receipt: IngestionReceipt) -> str:
        values = (
            receipt.payload_hash,
            receipt.status.value,
            receipt.episode_id,
            receipt.recipe_id or "",
            canonical_json(receipt.reasons),
            receipt.created_at,
        )
        with self._conn:
            row = self._conn.execute(
                "SELECT payload_hash,status,episode_id,recipe_id,reasons_json,created_at FROM atlas_ingestion_receipts WHERE ingestion_key=?",
                (receipt.ingestion_key,),
            ).fetchone()
            if row is not None and tuple(row) != values:
                raise StoreConflictError("ingestion receipt conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_ingestion_receipts VALUES (?,?,?,?,?,?,?)",
                    (receipt.ingestion_key, *values),
                )
        return receipt.ingestion_key

    def get_ingestion_receipt(self, ingestion_key: str) -> IngestionReceipt | None:
        row = self._conn.execute(
            "SELECT payload_hash,status,episode_id,recipe_id,reasons_json,created_at FROM atlas_ingestion_receipts WHERE ingestion_key=?",
            (ingestion_key,),
        ).fetchone()
        return (
            None
            if row is None
            else IngestionReceipt(
                ingestion_key,
                row[0],
                AtlasStatus(row[1]),
                row[2],
                row[3] or None,
                tuple(json.loads(row[4])),
                row[5],
            )
        )

    def recipes_for_intent(
        self,
        intent_id: str,
        *,
        language: str | None = None,
        framework: str | None = None,
        state: str | None = None,
        limit: int = MAX_GRAPH_NODES + 1,
    ) -> tuple[str, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 < limit <= MAX_GRAPH_NODES + 1
        ):
            raise ValueError("invalid recipe discovery limit")
        clauses: list[str] = ["intent_id=?"]
        args: list[object] = [intent_id]
        for column, value in (
            ("language", language),
            ("framework", framework),
            ("state", state),
        ):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        args.append(limit)
        return tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT recipe_id FROM atlas_recipes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY recipe_id LIMIT ?",
                args,
            )
        )

    def recipe_metadata(self, recipe_id: str) -> dict[str, object] | None:
        """Return immutable recipe-table metadata without exposing SQLite internals."""

        if not isinstance(recipe_id, str) or _HASH.fullmatch(recipe_id) is None:
            raise StoreConflictError()
        row = self._conn.execute(
            "SELECT recipe_id,intent_id,language,framework,layer,version,manifest_hash,"
            "repository_signature,state,supersedes_recipe_id FROM atlas_recipes "
            "WHERE recipe_id=?",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "recipe_id",
            "intent_id",
            "language",
            "framework",
            "layer",
            "version",
            "manifest_hash",
            "repository_signature",
            "state",
            "supersedes_recipe_id",
        )
        return dict(zip(keys, row, strict=True))

    def _node(self, node_id: str) -> AtlasNode:
        row = self._conn.execute(
            "SELECT node_id,kind,payload_json,schema_version,extractor_id,extractor_version,provenance,source_hashes_json,created_at,superseded_at,quarantine_state FROM atlas_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        return AtlasNode(
            row[0],
            NodeKind(row[1]),
            json.loads(row[2]),
            row[3],
            row[4],
            row[5],
            row[6],
            tuple(json.loads(row[7])),
            row[8] or None,
            row[9],
            row[10] or None,
        )

    def _edge(self, edge_id: str) -> AtlasEdge:
        row = self._conn.execute(
            "SELECT e.edge_id,e.relation,e.source_id,e.target_id,s.kind,t.kind,e.payload_json,e.schema_version,e.provenance,e.created_at FROM atlas_edges e JOIN atlas_nodes s ON s.node_id=e.source_id JOIN atlas_nodes t ON t.node_id=e.target_id WHERE e.edge_id=?",
            (edge_id,),
        ).fetchone()
        return AtlasEdge(
            row[0],
            EdgeRelation(row[1]),
            row[2],
            row[3],
            NodeKind(row[4]),
            NodeKind(row[5]),
            json.loads(row[6]),
            row[7],
            row[8],
            row[9] or None,
        )

    def graph_query(
        self,
        root_node_ids: Iterable[str],
        *,
        max_nodes: int = MAX_GRAPH_NODES,
        max_edges: int = MAX_GRAPH_EDGES,
        max_depth: int = MAX_GRAPH_DEPTH,
        byte_budget: int = MAX_PACKET_BYTES,
        node_kinds: Iterable[NodeKind] | None = None,
        relations: Iterable[EdgeRelation] | None = None,
    ) -> GraphQueryResult:
        if not (
            0 < max_nodes <= MAX_GRAPH_NODES
            and 0 < max_edges <= MAX_GRAPH_EDGES
            and 0 < max_depth <= MAX_GRAPH_DEPTH
            and 0 < byte_budget <= MAX_PACKET_BYTES
        ):
            raise ValueError("invalid graph budget")
        allowed_kinds = (
            None
            if node_kinds is None
            else {
                item.value if isinstance(item, NodeKind) else str(item)
                for item in node_kinds
            }
        )
        allowed_relations = (
            None
            if relations is None
            else {
                item.value if isinstance(item, EdgeRelation) else str(item)
                for item in relations
            }
        )
        roots = tuple(root_node_ids)
        chosen = set()
        frontier = sorted(set(roots))
        truncated = False
        for root in frontier:
            if (
                self._conn.execute(
                    "SELECT 1 FROM atlas_nodes WHERE node_id=?", (root,)
                ).fetchone()
                is not None
            ):
                if len(chosen) >= max_nodes:
                    truncated = True
                    break
                chosen.add(root)
        selected_edges: set[str] = set()
        for _depth in range(max_depth):
            next_frontier: set[str] = set()
            for node_id in sorted(frontier):
                rows = self._conn.execute(
                    "SELECT edge_id,source_id,target_id,relation FROM atlas_edges WHERE source_id=? OR target_id=? ORDER BY edge_id",
                    (node_id, node_id),
                ).fetchall()
                for edge_id, source, target, relation in rows:
                    other = target if source == node_id else source
                    if (
                        allowed_relations is not None
                        and relation not in allowed_relations
                    ):
                        continue
                    if allowed_kinds is not None:
                        kind = self._conn.execute(
                            "SELECT kind FROM atlas_nodes WHERE node_id=?", (other,)
                        ).fetchone()[0]
                        if kind not in allowed_kinds:
                            continue
                    if other not in chosen and len(chosen) >= max_nodes:
                        truncated = True
                        continue
                    if (
                        edge_id not in selected_edges
                        and len(selected_edges) >= max_edges
                    ):
                        truncated = True
                        continue
                    chosen.add(other)
                    selected_edges.add(edge_id)
                    next_frontier.add(other)
            frontier = sorted(next_frontier)
        # The final frontier is present at the permitted depth; report omitted
        # descendants instead of making a depth-limited result look complete.
        for node_id in frontier:
            for _edge_id, source, target, relation in self._conn.execute(
                "SELECT edge_id,source_id,target_id,relation FROM atlas_edges WHERE source_id=? OR target_id=? ORDER BY edge_id",
                (node_id, node_id),
            ):
                other = target if source == node_id else source
                if allowed_relations is not None and relation not in allowed_relations:
                    continue
                if allowed_kinds is not None:
                    kind = self._conn.execute(
                        "SELECT kind FROM atlas_nodes WHERE node_id=?", (other,)
                    ).fetchone()[0]
                    if kind not in allowed_kinds:
                        continue
                if _edge_id not in selected_edges or other not in chosen:
                    truncated = True
        nodes = tuple(self._node(item) for item in sorted(chosen))
        edges = tuple(
            edge
            for edge in (self._edge(item) for item in sorted(selected_edges))
            if edge.source_id in chosen and edge.target_id in chosen
        )
        while nodes or edges:
            result = GraphQueryResult(nodes, edges, truncated)
            if len(canonical_json(result.to_dict()).encode("utf-8")) <= byte_budget:
                return result
            truncated = True
            if edges:
                edges = edges[:-1]
            else:
                root_ids = set(roots)
                removable = [node for node in nodes if node.node_id not in root_ids]
                if removable:
                    remove_id = removable[-1].node_id
                    nodes = tuple(node for node in nodes if node.node_id != remove_id)
                    present = {node.node_id for node in nodes}
                    edges = tuple(
                        edge
                        for edge in edges
                        if edge.source_id in present and edge.target_id in present
                    )
                else:
                    return GraphQueryResult((), (), True)
        return GraphQueryResult((), (), truncated)
