"""Deterministic local SQLite/WAL graph and verified blob store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING

from project_index.workspace import is_workspace_id

from .canonical import canonical_hash, canonical_id, canonical_json
from .models import (
    ATLAS_MATCHER_VERSION,
    AtlasEdge,
    AtlasError,
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
    path_collision_key,
    validate_candidate_path,
    validate_fragment,
)

if TYPE_CHECKING:
    from devkit_runtime.sqlite_snapshot import VerifiedSqliteSnapshot

_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")
_BUNDLE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def open_verified_sqlite_snapshot(
    database_path: str | Path,
    *,
    scratch_root: str | Path | None = None,
    protected_roots: Iterable[str | Path] = (),
) -> VerifiedSqliteSnapshot:
    """Load the shared runtime primitive only for an actual read snapshot."""

    from devkit_runtime.sqlite_snapshot import open_verified_sqlite_snapshot

    return open_verified_sqlite_snapshot(
        database_path,
        scratch_root=scratch_root,
        protected_roots=protected_roots,
    )


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
    ) -> AtlasStore:
        """Open a stable durable DB/WAL copy without touching the durable root.

        SQLite's WAL-aware ``mode=ro`` can create ``-shm`` state beside the
        database. The reader therefore operates exclusively on a verified
        scratch snapshot, preserving a live durable WAL while never creating,
        deleting, or writing a durable sidecar. When ``scratch_root`` is
        omitted, the snapshot gets a private root beneath a pre-existing
        ``CODEX_TASK_TEMP`` (or a configured ``TMPDIR``/``TEMP``/``TMP``)
        directory and removes that empty root on close.
        """

        from devkit_runtime.sqlite_snapshot import SqliteSnapshotError

        database = _lexical_absolute(database_path)
        cas = _lexical_absolute(cas_root)
        connection: sqlite3.Connection | None = None
        snapshot: VerifiedSqliteSnapshot | None = None
        try:
            _assert_safe_existing_path(cas)
            if not stat.S_ISDIR(cas.lstat().st_mode):
                raise StoreConflictError()
            cas_directory_identities = _capture_cas_directory_identities(cas)
            _assert_cas_directory_identities(cas_directory_identities)
            snapshot = open_verified_sqlite_snapshot(
                database,
                scratch_root=scratch_root,
                protected_roots=(cas,),
            )
            connection = snapshot.connect()
            row = connection.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None or row[0] != "1":
                raise StoreConflictError()
            _assert_cas_directory_identities(cas_directory_identities)
            instance = cls.__new__(cls)
            instance._database_path = snapshot.database_path
            instance._cas_root = cas
            instance._cas_directory_identities = cas_directory_identities
            instance._conn = connection
            instance._verified_sqlite_snapshot = snapshot
            snapshot = None
            return instance
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            if snapshot is not None:
                snapshot.close()
            if isinstance(exc, StoreConflictError):
                raise
            if isinstance(exc, SqliteSnapshotError):
                raise StoreConflictError() from None
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
            snapshot = getattr(self, "_verified_sqlite_snapshot", None)
            if snapshot is not None:
                snapshot.close()
                self._verified_sqlite_snapshot = None

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
        packet_value = packet.to_dict()
        identity_value = dict(packet_value)
        identity_value.pop("packet_id", None)
        if (
            _HASH.fullmatch(packet.packet_id) is None
            or canonical_hash(identity_value) != packet.packet_id
            or not is_workspace_id(packet.workspace_id)
            or _HASH.fullmatch(packet.snapshot_id) is None
            or _HASH.fullmatch(packet.request_hash) is None
            or packet.matcher_version != ATLAS_MATCHER_VERSION
        ):
            raise StoreConflictError("packet receipt conflict")
        try:
            normalized_targets = tuple(
                sorted(validate_candidate_path(path) for path in packet.target_paths)
            )
        except (AtlasError, TypeError, ValueError) as exc:
            raise StoreConflictError("packet receipt conflict") from exc
        collision_keys = tuple(path_collision_key(path) for path in normalized_targets)
        if normalized_targets != packet.target_paths or len(set(collision_keys)) != len(
            collision_keys
        ):
            raise StoreConflictError("packet receipt conflict")
        payload = canonical_json(packet_value)
        digest = canonical_hash(packet_value)
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
            "workspace_id",
            "snapshot_id",
            "recipe_id",
            "next_action",
            "request_hash",
            "matcher_version",
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
            "target_paths",
            "target_symbols",
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
            "target_paths",
            "target_symbols",
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
                workspace_id=value["workspace_id"],
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
                request_hash=value["request_hash"],
                matcher_version=value["matcher_version"],
                target_paths=tuple(value["target_paths"]),
                target_symbols=tuple(value["target_symbols"]),
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

    def put_ingestion_bundle(
        self,
        *,
        nodes: Iterable[AtlasNode],
        edges: Iterable[AtlasEdge],
        manifest: RecipeManifest | None,
        recipe_node_ids: Iterable[str],
        recipe_edge_ids: Iterable[str],
        blobs: Iterable[tuple[str, bytes, str]],
        receipt: IngestionReceipt,
    ) -> IngestionReceipt:
        """Persist one accepted Atlas projection with its receipt as visibility marker.

        Unlike the compatibility ``put_*`` methods, this boundary keeps graph,
        recipe, CAS metadata, and receipt rows inside one SQLite transaction.
        CAS publication is content-addressed and no-replace; only files created
        by this invocation are eligible for rollback cleanup.
        """

        node_items = self._bundle_nodes(nodes)
        edge_items = self._bundle_edges(edges)
        linked_nodes = self._bundle_ids(recipe_node_ids, "recipe node", MAX_GRAPH_NODES)
        linked_edges = self._bundle_ids(recipe_edge_ids, "recipe edge", MAX_GRAPH_EDGES)
        blob_items = self._bundle_blobs(blobs)
        receipt_values = self._bundle_receipt_values(receipt)
        self._bundle_validate_shape(
            node_items,
            edge_items,
            manifest,
            linked_nodes,
            linked_edges,
            blob_items,
            receipt,
        )

        created_paths: list[tuple[Path, tuple[int, int, int, int, int]]] = []
        staged_paths: list[tuple[Path, tuple[int, int, int, int, int]]] = []
        committed = False
        try:
            if self._conn.in_transaction:
                raise StoreConflictError("bundle transaction conflict")
            self._conn.execute("BEGIN IMMEDIATE")
            existing_receipt = self._bundle_existing_receipt(receipt, receipt_values)
            self._bundle_validate_database(
                node_items,
                edge_items,
                manifest,
                linked_nodes,
                linked_edges,
                blob_items,
                receipt,
                require_existing=existing_receipt is not None,
            )
            if existing_receipt is not None:
                self._conn.rollback()
                return existing_receipt

            _assert_safe_existing_path(self._cas_root)
            _assert_cas_directory_identities(self._cas_directory_identities)
            for blob_hash, content, media_type in blob_items:
                self._bundle_publish_blob(
                    blob_hash,
                    content,
                    media_type,
                    created_paths,
                    staged_paths,
                )
            self._bundle_insert_blobs(blob_items)
            self._bundle_insert_nodes(node_items)
            self._bundle_insert_edges(edge_items)
            self._bundle_insert_recipe(manifest, linked_nodes, linked_edges)
            self._bundle_insert_receipt(receipt, receipt_values)
            for blob_hash, content, _media_type in blob_items:
                self._bundle_verify_blob_file(
                    self._blob_path(blob_hash), blob_hash, content
                )
            refreshed_identities = _capture_cas_directory_identities(self._cas_root)
            self._bundle_commit()
            committed = True
            self._cas_directory_identities = refreshed_identities
            return receipt
        except Exception:
            if self._conn.in_transaction:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
            self._bundle_cleanup_paths(staged_paths, created_paths)
            raise
        finally:
            if not committed:
                # Keep a pre-existing root pin usable after a failed attempt;
                # new empty CAS fan-out directories are intentionally inert.
                try:
                    self._cas_directory_identities = _capture_cas_directory_identities(
                        self._cas_root
                    )
                except StoreConflictError:
                    pass

    @staticmethod
    def _bundle_nodes(nodes: Iterable[AtlasNode]) -> tuple[AtlasNode, ...]:
        try:
            supplied = tuple(nodes)
        except TypeError as exc:
            raise StoreConflictError("bundle node conflict") from exc
        if len(supplied) > MAX_GRAPH_NODES:
            raise StoreConflictError("bundle node limit")
        values: dict[str, AtlasNode] = {}
        for node in supplied:
            if (
                type(node) is not AtlasNode
                or AtlasStore._node_identity(node) != node.node_id
            ):
                raise StoreConflictError("bundle node conflict")
            try:
                encoded = canonical_json(node.to_dict())
                validate_fragment(encoded, max_bytes=MAX_PACKET_BYTES)
            except Exception as exc:
                raise StoreConflictError("bundle node conflict") from exc
            prior = values.get(node.node_id)
            if prior is not None and prior != node:
                raise StoreConflictError("bundle node conflict")
            values[node.node_id] = node
        return tuple(sorted(values.values(), key=lambda item: item.node_id))

    @staticmethod
    def _bundle_edges(edges: Iterable[AtlasEdge]) -> tuple[AtlasEdge, ...]:
        try:
            supplied = tuple(edges)
        except TypeError as exc:
            raise StoreConflictError("bundle edge conflict") from exc
        if len(supplied) > MAX_GRAPH_EDGES:
            raise StoreConflictError("bundle edge limit")
        values: dict[str, AtlasEdge] = {}
        for edge in supplied:
            if (
                type(edge) is not AtlasEdge
                or AtlasStore._edge_identity(edge) != edge.edge_id
            ):
                raise StoreConflictError("bundle edge conflict")
            try:
                encoded = canonical_json(edge.to_dict())
                validate_fragment(encoded, max_bytes=MAX_PACKET_BYTES)
            except Exception as exc:
                raise StoreConflictError("bundle edge conflict") from exc
            prior = values.get(edge.edge_id)
            if prior is not None and prior != edge:
                raise StoreConflictError("bundle edge conflict")
            values[edge.edge_id] = edge
        return tuple(sorted(values.values(), key=lambda item: item.edge_id))

    @staticmethod
    def _bundle_ids(values: Iterable[str], label: str, maximum: int) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise StoreConflictError(f"bundle {label} conflict")
        try:
            supplied = tuple(values)
        except TypeError as exc:
            raise StoreConflictError(f"bundle {label} conflict") from exc
        if len(supplied) > maximum or any(
            not isinstance(value, str) or _HASH.fullmatch(value) is None
            for value in supplied
        ):
            raise StoreConflictError(f"bundle {label} conflict")
        if len(set(supplied)) != len(supplied):
            raise StoreConflictError(f"bundle {label} conflict")
        return tuple(sorted(supplied))

    @staticmethod
    def _bundle_blobs(
        blobs: Iterable[tuple[str, bytes, str]],
    ) -> tuple[tuple[str, bytes, str], ...]:
        if isinstance(blobs, (str, bytes)):
            raise StoreConflictError("bundle blob conflict")
        try:
            supplied = tuple(blobs)
        except TypeError as exc:
            raise StoreConflictError("bundle blob conflict") from exc
        if len(supplied) > MAX_GRAPH_NODES:
            raise StoreConflictError("bundle blob limit")
        total = 0
        values: dict[str, tuple[str, bytes, str]] = {}
        media_type = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
        for item in supplied:
            if type(item) is not tuple or len(item) != 3:
                raise StoreConflictError("bundle blob conflict")
            blob_hash, content, kind = item
            if (
                not isinstance(blob_hash, str)
                or _HASH.fullmatch(blob_hash) is None
                or not isinstance(content, bytes)
                or not isinstance(kind, str)
                or media_type.fullmatch(kind) is None
                or len(kind.encode("utf-8")) > 128
                or len(content) > MAX_TEMPLATE_BYTES
                or "sha256:" + hashlib.sha256(content).hexdigest() != blob_hash
            ):
                raise StoreConflictError("bundle blob conflict")
            try:
                validate_fragment(content, max_bytes=MAX_TEMPLATE_BYTES)
            except Exception as exc:
                raise StoreConflictError("bundle blob conflict") from exc
            if blob_hash in values:
                raise StoreConflictError("bundle blob duplicate")
            total += len(content)
            if total > MAX_RECIPE_BYTES:
                raise StoreConflictError("bundle blob limit")
            values[blob_hash] = (blob_hash, content, kind)
        return tuple(values[key] for key in sorted(values))

    @staticmethod
    def _bundle_receipt_values(
        receipt: IngestionReceipt,
    ) -> tuple[str, str, str, str, str, str]:
        if type(receipt) is not IngestionReceipt:
            raise StoreConflictError("bundle ingestion receipt conflict")
        if (
            _HASH.fullmatch(receipt.ingestion_key) is None
            or _HASH.fullmatch(receipt.payload_hash) is None
            or _HASH.fullmatch(receipt.episode_id) is None
            or (
                receipt.recipe_id is not None
                and _HASH.fullmatch(receipt.recipe_id) is None
            )
            or not isinstance(receipt.status, AtlasStatus)
            or receipt.status
            in {
                AtlasStatus.INGEST_PENDING,
                AtlasStatus.ATLAS_UNAVAILABLE,
                AtlasStatus.MODEL_UNAVAILABLE,
            }
            or receipt.created_at != ""
            or len(receipt.reasons) > MAX_GRAPH_NODES
            or any(
                not isinstance(reason, str) or _BUNDLE_REASON.fullmatch(reason) is None
                for reason in receipt.reasons
            )
            or receipt.reasons != tuple(sorted(set(receipt.reasons)))
        ):
            raise StoreConflictError("bundle ingestion receipt conflict")
        try:
            reasons = canonical_json(receipt.reasons)
            validate_fragment(reasons, max_bytes=MAX_PACKET_BYTES)
        except Exception as exc:
            raise StoreConflictError("bundle ingestion receipt conflict") from exc
        return (
            receipt.payload_hash,
            receipt.status.value,
            receipt.episode_id,
            receipt.recipe_id or "",
            reasons,
            receipt.created_at,
        )

    def _bundle_validate_shape(
        self,
        nodes: tuple[AtlasNode, ...],
        edges: tuple[AtlasEdge, ...],
        manifest: RecipeManifest | None,
        linked_nodes: tuple[str, ...],
        linked_edges: tuple[str, ...],
        blobs: tuple[tuple[str, bytes, str], ...],
        receipt: IngestionReceipt,
    ) -> None:
        if not nodes or not any(node.node_id == receipt.episode_id for node in nodes):
            raise StoreConflictError("bundle episode conflict")
        by_id = {node.node_id: node.kind for node in nodes}
        if by_id.get(receipt.episode_id) is not NodeKind.TASK_EPISODE:
            raise StoreConflictError("bundle episode conflict")
        for edge in edges:
            source_kind = by_id.get(edge.source_id)
            target_kind = by_id.get(edge.target_id)
            if source_kind is not None and source_kind is not edge.source_kind:
                raise StoreConflictError("bundle edge endpoint conflict")
            if target_kind is not None and target_kind is not edge.target_kind:
                raise StoreConflictError("bundle edge endpoint conflict")
        if manifest is None:
            if (
                linked_nodes
                or linked_edges
                or blobs
                or receipt.recipe_id is not None
                or receipt.status
                not in {
                    AtlasStatus.NO_VERIFIED_RECIPE,
                    AtlasStatus.UNSUPPORTED_LANGUAGE,
                    AtlasStatus.EVIDENCE_INCOMPLETE,
                    AtlasStatus.RECIPE_QUARANTINED,
                }
                or not receipt.reasons
            ):
                raise StoreConflictError("bundle episode-only conflict")
            return
        if type(manifest) is not RecipeManifest:
            raise StoreConflictError("bundle recipe conflict")
        if (
            manifest.recipe_id not in linked_nodes
            or receipt.recipe_id != manifest.recipe_id
            or by_id.get(manifest.recipe_id) is not NodeKind.RECIPE
            or receipt.status is not AtlasStatus.READY
            or receipt.reasons
        ):
            raise StoreConflictError("bundle recipe conflict")
        try:
            manifest_value = manifest.to_dict()
            recipe_payload = dict(manifest_value)
            del recipe_payload["recipe_id"]
            recipe_node = next(
                node for node in nodes if node.node_id == manifest.recipe_id
            )
            if canonical_json(recipe_node.payload) != canonical_json(recipe_payload):
                raise StoreConflictError("bundle recipe conflict")
            validate_fragment(
                canonical_json(manifest_value), max_bytes=MAX_PACKET_BYTES
            )
        except StoreConflictError:
            raise
        except Exception as exc:
            raise StoreConflictError("bundle recipe conflict") from exc

    def _bundle_existing_receipt(
        self,
        receipt: IngestionReceipt,
        values: tuple[str, str, str, str, str, str],
    ) -> IngestionReceipt | None:
        row = self._conn.execute(
            "SELECT payload_hash,status,episode_id,recipe_id,reasons_json,created_at "
            "FROM atlas_ingestion_receipts WHERE ingestion_key=?",
            (receipt.ingestion_key,),
        ).fetchone()
        if row is None:
            return None
        if tuple(row) != values:
            raise StoreConflictError("ingestion receipt conflict")
        return receipt

    def _bundle_validate_database(
        self,
        nodes: tuple[AtlasNode, ...],
        edges: tuple[AtlasEdge, ...],
        manifest: RecipeManifest | None,
        linked_nodes: tuple[str, ...],
        linked_edges: tuple[str, ...],
        blobs: tuple[tuple[str, bytes, str], ...],
        receipt: IngestionReceipt,
        *,
        require_existing: bool,
    ) -> None:
        kinds: dict[str, str] = {}
        for node in nodes:
            immutable = self._bundle_node_values(node)
            row = self._conn.execute(
                "SELECT kind,payload_json,schema_version,extractor_id,extractor_version,provenance,source_hashes_json FROM atlas_nodes WHERE node_id=?",
                (node.node_id,),
            ).fetchone()
            if row is not None and tuple(row) != immutable:
                raise StoreConflictError("node immutable payload conflict")
            if require_existing and row is None:
                raise StoreConflictError("ingestion receipt incomplete")
            kinds[node.node_id] = node.kind.value
        for edge in edges:
            immutable = self._bundle_edge_values(edge)
            row = self._conn.execute(
                "SELECT source_id,target_id,relation,payload_json,schema_version,provenance FROM atlas_edges WHERE edge_id=?",
                (edge.edge_id,),
            ).fetchone()
            if row is not None and tuple(row) != immutable:
                raise StoreConflictError("edge immutable payload conflict")
            if require_existing and row is None:
                raise StoreConflictError("ingestion receipt incomplete")
            for node_id, kind in (
                (edge.source_id, edge.source_kind.value),
                (edge.target_id, edge.target_kind.value),
            ):
                available = kinds.get(node_id)
                if available is None:
                    endpoint = self._conn.execute(
                        "SELECT kind FROM atlas_nodes WHERE node_id=?", (node_id,)
                    ).fetchone()
                    available = None if endpoint is None else endpoint[0]
                if available != kind:
                    raise StoreConflictError("edge endpoint conflict")
        episode = kinds.get(receipt.episode_id)
        if episode is None:
            row = self._conn.execute(
                "SELECT kind FROM atlas_nodes WHERE node_id=?", (receipt.episode_id,)
            ).fetchone()
            episode = None if row is None else row[0]
        if episode != NodeKind.TASK_EPISODE.value:
            raise StoreConflictError("bundle episode conflict")
        self._bundle_validate_recipe_database(
            manifest,
            linked_nodes,
            linked_edges,
            edges,
            kinds,
            require_existing,
        )
        for blob_hash, content, media_type in blobs:
            row = self._conn.execute(
                "SELECT size,media_type FROM atlas_blobs WHERE blob_hash=?",
                (blob_hash,),
            ).fetchone()
            path = self._blob_path(blob_hash)
            if row is not None and tuple(row) != (len(content), media_type):
                raise StoreConflictError("blob metadata conflict")
            if require_existing and row is None:
                raise StoreConflictError("ingestion receipt incomplete")
            if row is not None or path.exists() or path.is_symlink():
                self._bundle_verify_blob_file(path, blob_hash, content)
            elif require_existing:
                raise StoreConflictError("ingestion receipt incomplete")

        if manifest is not None:
            referenced = {operation.template_hash for operation in manifest.operations}
            if any(_HASH.fullmatch(blob_hash) is None for blob_hash in referenced):
                raise StoreConflictError("bundle blob conflict")
            supplied = {blob_hash for blob_hash, _content, _media_type in blobs}
            if supplied - referenced:
                raise StoreConflictError("bundle blob conflict")
            for blob_hash in sorted(referenced - supplied):
                try:
                    self.read_blob_verified(blob_hash, max_bytes=MAX_TEMPLATE_BYTES)
                except StoreConflictError as exc:
                    raise StoreConflictError("bundle blob missing") from exc

    def _bundle_validate_recipe_database(
        self,
        manifest: RecipeManifest | None,
        linked_nodes: tuple[str, ...],
        linked_edges: tuple[str, ...],
        edges: tuple[AtlasEdge, ...],
        local_kinds: dict[str, str],
        require_existing: bool,
    ) -> None:
        if manifest is None:
            return
        immutable = self._bundle_recipe_values(manifest)
        row = self._conn.execute(
            "SELECT intent_id,language,framework,layer,version,manifest_hash,repository_signature,state,supersedes_recipe_id FROM atlas_recipes WHERE recipe_id=?",
            (manifest.recipe_id,),
        ).fetchone()
        if row is not None and tuple(row) != immutable:
            raise StoreConflictError("recipe conflict")
        if require_existing and row is None:
            raise StoreConflictError("ingestion receipt incomplete")
        recipe_kind = local_kinds.get(manifest.recipe_id)
        if recipe_kind is None:
            recipe_row = self._conn.execute(
                "SELECT kind FROM atlas_nodes WHERE node_id=?", (manifest.recipe_id,)
            ).fetchone()
            recipe_kind = None if recipe_row is None else recipe_row[0]
        if recipe_kind != NodeKind.RECIPE.value:
            raise StoreConflictError("recipe node conflict")
        for node_id in linked_nodes:
            kind = local_kinds.get(node_id)
            if kind is None:
                node_row = self._conn.execute(
                    "SELECT kind FROM atlas_nodes WHERE node_id=?", (node_id,)
                ).fetchone()
                kind = None if node_row is None else node_row[0]
            if kind is None:
                raise StoreConflictError("recipe node link conflict")
        supplied_edges = {edge.edge_id: edge for edge in edges}
        for edge_id in linked_edges:
            supplied_edge = supplied_edges.get(edge_id)
            if supplied_edge is not None:
                endpoints = (supplied_edge.source_id, supplied_edge.target_id)
            else:
                edge_row = self._conn.execute(
                    "SELECT source_id,target_id FROM atlas_edges WHERE edge_id=?",
                    (edge_id,),
                ).fetchone()
                if edge_row is None:
                    raise StoreConflictError("recipe edge link conflict")
                endpoints = (edge_row[0], edge_row[1])
            if endpoints[0] not in linked_nodes or endpoints[1] not in linked_nodes:
                raise StoreConflictError("recipe edge link conflict")
        if row is not None:
            current_nodes = {
                item[0]
                for item in self._conn.execute(
                    "SELECT node_id FROM atlas_recipe_nodes WHERE recipe_id=?",
                    (manifest.recipe_id,),
                )
            }
            current_edges = {
                item[0]
                for item in self._conn.execute(
                    "SELECT edge_id FROM atlas_recipe_edges WHERE recipe_id=?",
                    (manifest.recipe_id,),
                )
            }
            if current_nodes != set(linked_nodes) or current_edges != set(linked_edges):
                raise StoreConflictError("recipe link conflict")

    @staticmethod
    def _bundle_node_values(
        node: AtlasNode,
    ) -> tuple[str, str, str, str, str, str, str]:
        return (
            node.kind.value,
            canonical_json(node.payload),
            node.schema_version,
            node.extractor_id,
            node.extractor_version,
            node.provenance,
            canonical_json(node.source_hashes),
        )

    @staticmethod
    def _bundle_edge_values(edge: AtlasEdge) -> tuple[str, str, str, str, str, str]:
        return (
            edge.source_id,
            edge.target_id,
            edge.relation.value,
            canonical_json(edge.payload),
            edge.schema_version,
            edge.provenance,
        )

    @staticmethod
    def _bundle_recipe_values(manifest: RecipeManifest) -> tuple[object, ...]:
        return (
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

    def _bundle_publish_blob(
        self,
        blob_hash: str,
        content: bytes,
        _media_type: str,
        created_paths: list[tuple[Path, tuple[int, int, int, int, int]]],
        staged_paths: list[tuple[Path, tuple[int, int, int, int, int]]],
    ) -> None:
        path = self._blob_path(blob_hash)
        existing = _safe_regular_identity(path, optional=True)
        if existing is not None:
            self._bundle_verify_blob_file(path, blob_hash, content)
            return
        self._bundle_prepare_blob_parent(path.parent)
        _assert_safe_existing_path(path.parent)
        temp: Path | None = None
        temp_identity: tuple[int, int, int, int, int] | None = None
        for _attempt in range(64):
            candidate = path.with_name(
                path.name + ".bundle-" + uuid.uuid4().hex + ".tmp"
            )
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise StoreConflictError("blob write conflict") from exc
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise StoreConflictError("blob write conflict") from exc
            temp = candidate
            temp_identity = _safe_regular_identity(temp)
            if temp_identity is None:
                raise StoreConflictError("blob write conflict")
            staged_paths.append((temp, temp_identity))
            break
        if temp is None or temp_identity is None:
            raise StoreConflictError("blob write conflict")
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            self._bundle_verify_blob_file(path, blob_hash, content)
        except OSError as exc:
            raise StoreConflictError("blob write conflict") from exc
        else:
            created_identity = _safe_regular_identity(path)
            if created_identity is None:
                raise StoreConflictError("blob write conflict")
            created_paths.append((path, created_identity))
            self._bundle_verify_blob_file(path, blob_hash, content)
        self._bundle_remove_owned_path(temp, temp_identity, strict=True)
        staged_paths.remove((temp, temp_identity))

    def _bundle_prepare_blob_parent(self, parent: Path) -> None:
        _assert_safe_existing_path(self._cas_root)
        try:
            relative = parent.relative_to(self._cas_root)
        except ValueError as exc:
            raise StoreConflictError("blob path conflict") from exc
        current = self._cas_root
        for part in relative.parts:
            current /= part
            try:
                current.mkdir(exist_ok=True)
                value = current.lstat()
            except OSError as exc:
                raise StoreConflictError("blob path conflict") from exc
            if _unsafe_file_status(current, value) or not stat.S_ISDIR(value.st_mode):
                raise StoreConflictError("blob path conflict")
        _assert_safe_existing_path(parent)

    def _bundle_verify_blob_file(
        self, path: Path, blob_hash: str, expected: bytes
    ) -> None:
        descriptor: int | None = None
        try:
            _assert_safe_existing_path(self._cas_root)
            chain = _capture_safe_path_chain(path, require_regular=True)
            before = path.lstat()
            if not chain or _file_identity(before) != chain[-1][1]:
                raise StoreConflictError("blob path conflict")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _file_identity(
                opened
            ) != _file_identity(before):
                raise StoreConflictError("blob path conflict")
            chunks: list[bytes] = []
            total = 0
            while total <= len(expected):
                chunk = os.read(descriptor, min(65_536, len(expected) + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if os.read(descriptor, 1) or total != len(expected):
                raise StoreConflictError("blob filesystem conflict")
            after_open = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(after_open):
                raise StoreConflictError("blob path conflict")
            body = b"".join(chunks)
            if (
                body != expected
                or "sha256:" + hashlib.sha256(body).hexdigest() != blob_hash
            ):
                raise StoreConflictError("blob filesystem conflict")
            _assert_path_chain_unchanged(chain)
            after = path.lstat()
            if _file_identity(before) != _file_identity(after):
                raise StoreConflictError("blob path conflict")
        except StoreConflictError as exc:
            if str(exc).startswith("blob "):
                raise
            raise StoreConflictError("blob path conflict") from exc
        except OSError as exc:
            raise StoreConflictError("blob path conflict") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise StoreConflictError("blob path conflict") from exc

    def _bundle_insert_blobs(self, blobs: tuple[tuple[str, bytes, str], ...]) -> None:
        for blob_hash, content, media_type in blobs:
            row = self._conn.execute(
                "SELECT size,media_type FROM atlas_blobs WHERE blob_hash=?",
                (blob_hash,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_blobs VALUES (?,?,?)",
                    (blob_hash, len(content), media_type),
                )

    def _bundle_insert_nodes(self, nodes: tuple[AtlasNode, ...]) -> None:
        for node in nodes:
            if (
                self._conn.execute(
                    "SELECT 1 FROM atlas_nodes WHERE node_id=?", (node.node_id,)
                ).fetchone()
                is None
            ):
                self._conn.execute(
                    "INSERT INTO atlas_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node.node_id,
                        *self._bundle_node_values(node),
                        node.created_at or "",
                        node.superseded_at,
                        node.quarantine_state or "",
                    ),
                )

    def _bundle_insert_edges(self, edges: tuple[AtlasEdge, ...]) -> None:
        for edge in edges:
            if (
                self._conn.execute(
                    "SELECT 1 FROM atlas_edges WHERE edge_id=?", (edge.edge_id,)
                ).fetchone()
                is None
            ):
                self._conn.execute(
                    "INSERT INTO atlas_edges VALUES (?,?,?,?,?,?,?,?)",
                    (
                        edge.edge_id,
                        *self._bundle_edge_values(edge),
                        edge.created_at or "",
                    ),
                )

    def _bundle_insert_recipe(
        self,
        manifest: RecipeManifest | None,
        linked_nodes: tuple[str, ...],
        linked_edges: tuple[str, ...],
    ) -> None:
        if manifest is None:
            return
        if (
            self._conn.execute(
                "SELECT 1 FROM atlas_recipes WHERE recipe_id=?", (manifest.recipe_id,)
            ).fetchone()
            is None
        ):
            self._conn.execute(
                "INSERT INTO atlas_recipes VALUES (?,?,?,?,?,?,?,?,?,?)",
                (manifest.recipe_id, *self._bundle_recipe_values(manifest)),
            )
        for node_id in linked_nodes:
            self._conn.execute(
                "INSERT OR IGNORE INTO atlas_recipe_nodes VALUES (?,?)",
                (manifest.recipe_id, node_id),
            )
        for edge_id in linked_edges:
            self._conn.execute(
                "INSERT OR IGNORE INTO atlas_recipe_edges VALUES (?,?)",
                (manifest.recipe_id, edge_id),
            )

    def _bundle_insert_receipt(
        self,
        receipt: IngestionReceipt,
        values: tuple[str, str, str, str, str, str],
    ) -> None:
        self._conn.execute(
            "INSERT INTO atlas_ingestion_receipts VALUES (?,?,?,?,?,?,?)",
            (receipt.ingestion_key, *values),
        )

    def _bundle_commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _bundle_remove_owned_path(
        path: Path,
        identity: tuple[int, int, int, int, int],
        *,
        strict: bool,
    ) -> None:
        try:
            current = _safe_regular_identity(path, optional=True)
            if current is None:
                return
            if current != identity:
                if strict:
                    raise StoreConflictError("blob cleanup conflict")
                return
            path.unlink()
        except StoreConflictError:
            if strict:
                raise
        except OSError:
            if strict:
                raise StoreConflictError("blob cleanup conflict") from None

    def _bundle_cleanup_paths(
        self,
        staged_paths: list[tuple[Path, tuple[int, int, int, int, int]]],
        created_paths: list[tuple[Path, tuple[int, int, int, int, int]]],
    ) -> None:
        for path, identity in reversed(staged_paths):
            self._bundle_remove_owned_path(path, identity, strict=False)
        for path, identity in reversed(created_paths):
            self._bundle_remove_owned_path(path, identity, strict=False)

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
