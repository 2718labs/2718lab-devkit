"""Independent SQLite storage with atomic Continuity state transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .canonical import canonical_json
from .cas import ContinuityCas, _bootstrap_cas
from .models import (
    ContinuityAttempt,
    ContinuityError,
    ContinuityKey,
    ContinuityPointer,
    ContinuityReceipt,
    FrozenView,
)

_SCHEMA_VERSION = "2"
_IMMUTABLE_TABLES = ("continuity_keys", "views", "entries", "receipts", "attempts")


class ContinuityStoreError(ContinuityError):
    """Stable persistence failure that deliberately exposes only a code."""


class ContinuityStore:
    def __init__(self, connection: sqlite3.Connection, *, read_only: bool) -> None:
        self._connection, self.read_only = connection, read_only
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open_readonly(cls, database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
        try:
            ContinuityCas.open_prepared(cas_root, scratch_root, read_only=True)
        except Exception as error:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
        return cls._open(database, read_only=True)

    @classmethod
    def open_readwrite(cls, database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
        try:
            ContinuityCas.open_prepared(cas_root, scratch_root, read_only=False)
        except Exception as error:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
        return cls._open(database, read_only=False)

    @classmethod
    def _open(cls, database: Path, *, read_only: bool) -> ContinuityStore:
        if not database.is_file():
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        mode = "ro" if read_only else "rw"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode={mode}", uri=True)
            _configure_connection(connection)
            _verify_v2_schema(connection)
            return cls(connection, read_only=read_only)
        except (sqlite3.Error, OSError, ContinuityError) as error:
            if connection is not None:
                connection.close()
            if isinstance(error, ContinuityError):
                raise
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error

    def close(self) -> None:
        self._connection.close()

    def claim_or_reuse_atomic(self, key: ContinuityKey) -> ContinuityAttempt:
        """Append exactly one new claim under a writer lock, or reuse final state."""
        with self._atomic():
            self._ensure_key_row(key)
            current = self._current_attempt_row(key)
            if current is not None and current.state in {"frozen", "published"}:
                return current
            epoch = 1 if current is None else current.fence_epoch + 1
            return self._append_attempt_row(key, epoch, self._next_sequence(key), "claimed", None, None)

    def freeze_attempt_atomic(
        self, attempt: ContinuityAttempt, view: FrozenView, receipt: ContinuityReceipt
    ) -> ContinuityAttempt:
        """Atomically bind a claimed attempt to its immutable view and receipt."""
        with self._atomic():
            self._ensure_exact_current(attempt)
            if view.key.key_hash != attempt.key.key_hash or receipt.key.key_hash != attempt.key.key_hash:
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if receipt.view_id != view.view_id or receipt.kind != "frozen":
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if attempt.state == "frozen":
                if attempt.view_id == view.view_id and attempt.receipt_hash == receipt.receipt_hash:
                    return attempt
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if attempt.state != "claimed":
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            self._insert_view_rows(view)
            self._insert_receipt_row(receipt, _receipt_json(receipt))
            return self._append_attempt_row(
                attempt.key,
                attempt.fence_epoch,
                self._next_sequence(attempt.key),
                "frozen",
                view.view_id,
                receipt.receipt_hash,
            )

    def publish_attempt_atomic(
        self, attempt: ContinuityAttempt, view: FrozenView
    ) -> ContinuityPointer:
        """Atomically bind publication receipt, pointer, and terminal event."""
        with self._atomic():
            self._ensure_exact_current(attempt)
            if view.key.key_hash != attempt.key.key_hash:
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if attempt.state == "published":
                pointer = self.pointer_for(attempt.key)
                if pointer is not None and pointer.view_id == view.view_id and pointer.fence_epoch == attempt.fence_epoch:
                    return pointer
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if attempt.state != "frozen" or attempt.view_id != view.view_id:
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            self._require_view_row(view)
            receipt = ContinuityReceipt.create(key=attempt.key, view_id=view.view_id, kind="published")
            self._insert_receipt_row(receipt, _receipt_json(receipt))
            pointer = self._advance_pointer(attempt.key, view, attempt.fence_epoch)
            self._append_attempt_row(
                attempt.key,
                attempt.fence_epoch,
                self._next_sequence(attempt.key),
                "published",
                view.view_id,
                receipt.receipt_hash,
            )
            return pointer

    # Legacy narrow operations remain atomic individually; service code must use the
    # command methods above for multi-row state transitions.
    def append_attempt_event(self, key: ContinuityKey, fence_epoch: int, state: str, view_id: str | None, receipt_hash: str | None) -> ContinuityAttempt:
        with self._atomic():
            self._ensure_key_row(key)
            current = self._current_attempt_row(key)
            if current is not None and fence_epoch < current.fence_epoch:
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if current is not None and fence_epoch == current.fence_epoch and current.state != "claimed":
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            return self._append_attempt_row(key, fence_epoch, self._next_sequence(key), state, view_id, receipt_hash)

    def current_attempt(self, key: ContinuityKey) -> ContinuityAttempt | None:
        return self._current_attempt_row(key)

    def insert_or_get_view(self, view: FrozenView, manifest_json: str) -> FrozenView:
        if manifest_json != view.manifest_json:
            raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
        with self._atomic():
            self._ensure_key_row(view.key)
            self._insert_view_rows(view)
            return view

    def insert_or_get_receipt(self, receipt: ContinuityReceipt, receipt_json: str) -> ContinuityReceipt:
        with self._atomic():
            self._ensure_key_row(receipt.key)
            self._insert_receipt_row(receipt, receipt_json)
            return receipt

    def pointer_for(self, key: ContinuityKey) -> ContinuityPointer | None:
        row = self._connection.execute(
            "SELECT view_id,pointer_version,fence_epoch FROM pointers WHERE key_hash=?",
            (key.key_hash,),
        ).fetchone()
        if row is None:
            return None
        return ContinuityPointer(
            key.workflow_id,
            key.code_task_id,
            key.code_task_version,
            row["view_id"],
            row["pointer_version"],
            row["fence_epoch"],
        )

    def compare_and_swap_pointer(self, key: ContinuityKey, view: FrozenView, expected_pointer_version: int, expected_fence_epoch: int, new_fence_epoch: int) -> ContinuityPointer:
        if type(expected_pointer_version) is not int or type(expected_fence_epoch) is not int:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        with self._atomic():
            self._ensure_key_row(key)
            self._require_view_row(view)
            current = self.pointer_for(key)
            if current is None:
                if expected_pointer_version != 0 or expected_fence_epoch != 0:
                    raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
            elif current.pointer_version != expected_pointer_version or current.fence_epoch != expected_fence_epoch:
                raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
            return self._advance_pointer(key, view, new_fence_epoch)

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        self._writable()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._connection.commit()
        except ContinuityError:
            self._connection.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            self._connection.rollback()
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT") from error

    def _writable(self) -> None:
        if self.read_only:
            raise ContinuityStoreError("CONTINUITY_STORE_READ_ONLY")

    def _ensure_key_row(self, key: ContinuityKey) -> None:
        key_json = canonical_json(key.to_dict())
        row = self._connection.execute("SELECT key_json FROM continuity_keys WHERE key_hash=?", (key.key_hash,)).fetchone()
        if row is not None:
            if row["key_json"] != key_json:
                raise ContinuityStoreError("CONTINUITY_KEY_CONFLICT")
            return
        self._connection.execute(
            "INSERT INTO continuity_keys(key_hash,key_json,workflow_id,code_task_id,code_task_version,acceptance_id,ingestion_key,payload_hash,evidence_binding_hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (key.key_hash, key_json, key.workflow_id, key.code_task_id, key.code_task_version, key.acceptance_id, key.ingestion_key, key.payload_hash, key.evidence_binding_hash),
        )

    def _current_attempt_row(self, key: ContinuityKey) -> ContinuityAttempt | None:
        row = self._connection.execute(
            "SELECT fence_epoch,state,view_id,receipt_hash FROM attempts WHERE key_hash=? ORDER BY sequence DESC LIMIT 1",
            (key.key_hash,),
        ).fetchone()
        if row is None:
            return None
        return ContinuityAttempt(key, row["fence_epoch"], row["state"], row["view_id"], row["receipt_hash"])

    def _ensure_exact_current(self, attempt: ContinuityAttempt) -> None:
        current = self._current_attempt_row(attempt.key)
        if current != attempt:
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")

    def _next_sequence(self, key: ContinuityKey) -> int:
        return int(self._connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM attempts WHERE key_hash=?", (key.key_hash,)).fetchone()[0])

    def _append_attempt_row(self, key: ContinuityKey, fence_epoch: int, sequence: int, state: str, view_id: str | None, receipt_hash: str | None) -> ContinuityAttempt:
        attempt = ContinuityAttempt(key, fence_epoch, state, view_id, receipt_hash)
        self._connection.execute(
            "INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)",
            (key.key_hash, canonical_json(key.to_dict()), fence_epoch, sequence, state, view_id, receipt_hash),
        )
        return attempt

    def _insert_view_rows(self, view: FrozenView) -> None:
        row = self._connection.execute("SELECT view_id,manifest_json FROM views WHERE key_hash=?", (view.key.key_hash,)).fetchone()
        if row is not None:
            if row["view_id"] != view.view_id or row["manifest_json"] != view.manifest_json:
                raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
            return
        self._connection.execute(
            "INSERT INTO views(view_id,key_hash,manifest_hash,cas_root_hash,manifest_json) VALUES(?,?,?,?,?)",
            (view.view_id, view.key.key_hash, view.manifest_hash, view.cas_root_hash, view.manifest_json),
        )
        self._connection.executemany(
            "INSERT INTO entries(view_id,role,path,content_hash,byte_length) VALUES(?,?,?,?,?)",
            [(view.view_id, item.role, item.path, item.content_hash, item.byte_length) for item in view.entries],
        )

    def _require_view_row(self, view: FrozenView) -> None:
        row = self._connection.execute("SELECT manifest_json FROM views WHERE view_id=? AND key_hash=?", (view.view_id, view.key.key_hash)).fetchone()
        if row is None or row["manifest_json"] != view.manifest_json:
            raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")

    def _insert_receipt_row(self, receipt: ContinuityReceipt, receipt_json: str) -> None:
        row = self._connection.execute("SELECT receipt_json FROM receipts WHERE receipt_hash=?", (receipt.receipt_hash,)).fetchone()
        if row is not None:
            if row["receipt_json"] != receipt_json:
                raise ContinuityStoreError("CONTINUITY_RECEIPT_CONFLICT")
            return
        self._connection.execute(
            "INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) VALUES(?,?,?,?,?)",
            (receipt.receipt_hash, receipt.key.key_hash, receipt.view_id, receipt.kind, receipt_json),
        )

    def _advance_pointer(self, key: ContinuityKey, view: FrozenView, new_fence_epoch: int) -> ContinuityPointer:
        if view.key.key_hash != key.key_hash or type(new_fence_epoch) is not int or new_fence_epoch < 1:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        current = self.pointer_for(key)
        if current is None:
            self._insert_pointer_row(key, view, new_fence_epoch)
            return ContinuityPointer(key.workflow_id, key.code_task_id, key.code_task_version, view.view_id, 1, new_fence_epoch)
        if new_fence_epoch < current.fence_epoch:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        if new_fence_epoch == current.fence_epoch:
            if current.view_id != view.view_id:
                raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
            return current
        changed = self._connection.execute(
            "UPDATE pointers SET view_id=?,pointer_version=?,fence_epoch=? WHERE key_hash=? AND pointer_version=? AND fence_epoch=?",
            (view.view_id, current.pointer_version + 1, new_fence_epoch, key.key_hash, current.pointer_version, current.fence_epoch),
        ).rowcount
        if changed != 1:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        return ContinuityPointer(key.workflow_id, key.code_task_id, key.code_task_version, view.view_id, current.pointer_version + 1, new_fence_epoch)

    def _insert_pointer_row(self, key: ContinuityKey, view: FrozenView, fence_epoch: int) -> None:
        self._connection.execute(
            "INSERT INTO pointers(key_hash,workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?,?)",
            (key.key_hash, key.workflow_id, key.code_task_id, key.code_task_version, view.view_id, 1, fence_epoch),
        )


def _receipt_json(receipt: ContinuityReceipt) -> str:
    return canonical_json({"key": receipt.key.to_dict(), "view_id": receipt.view_id, "kind": receipt.kind})


def _bootstrap_store(database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
    """Runtime-private creation/migration seam; ordinary openers never create."""
    _bootstrap_cas(cas_root, scratch_root)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.row_factory = sqlite3.Row
        _configure_connection(connection)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        version = _schema_version(connection)
        if version is None:
            _create_v2_schema(connection)
        elif version == "1":
            _verify_v1_schema(connection)
            _migrate_v1_to_v2(connection)
        elif version == _SCHEMA_VERSION:
            _verify_v2_schema(connection)
        else:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        _verify_v2_schema(connection)
        connection.commit()
        return ContinuityStore(connection, read_only=False)
    except Exception:
        connection.rollback()
        connection.close()
        raise


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _schema_version(connection: sqlite3.Connection) -> str | None:
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'").fetchone()
    if table is None:
        return None
    rows = connection.execute("SELECT key,value FROM schema_metadata").fetchall()
    if len(rows) != 1 or rows[0][0] != "schema_version":
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return rows[0][1]


def _create_v2_schema(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
        "CREATE TABLE continuity_keys (key_hash TEXT PRIMARY KEY NOT NULL, key_json TEXT UNIQUE NOT NULL, workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL CHECK(code_task_version>=0), acceptance_id TEXT NOT NULL, ingestion_key TEXT NOT NULL, payload_hash TEXT NOT NULL, evidence_binding_hash TEXT NOT NULL)",
        "CREATE TABLE views (view_id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, cas_root_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, UNIQUE(view_id,key_hash), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash))",
        "CREATE TABLE entries (view_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL CHECK(byte_length>=0), PRIMARY KEY(view_id,role,path), FOREIGN KEY(view_id) REFERENCES views(view_id))",
        "CREATE TABLE receipts (receipt_hash TEXT PRIMARY KEY NOT NULL, key_hash TEXT NOT NULL, view_id TEXT NOT NULL, kind TEXT NOT NULL, receipt_json TEXT NOT NULL, UNIQUE(receipt_hash,key_hash), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash))",
        "CREATE TABLE attempts (key_hash TEXT NOT NULL, key_json TEXT NOT NULL, fence_epoch INTEGER NOT NULL CHECK(fence_epoch>0), sequence INTEGER NOT NULL CHECK(sequence>0), state TEXT NOT NULL CHECK(state IN ('claimed','frozen','published','expired','abandoned')), view_id TEXT, receipt_hash TEXT, PRIMARY KEY(key_hash,sequence), CHECK((state='claimed' AND view_id IS NULL AND receipt_hash IS NULL) OR (state!='claimed' AND view_id IS NOT NULL AND receipt_hash IS NOT NULL)), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash), FOREIGN KEY(receipt_hash,key_hash) REFERENCES receipts(receipt_hash,key_hash))",
        "CREATE TABLE pointers (key_hash TEXT PRIMARY KEY NOT NULL, workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL CHECK(code_task_version>=0), view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL CHECK(pointer_version>0), fence_epoch INTEGER NOT NULL CHECK(fence_epoch>0), UNIQUE(workflow_id,code_task_id,code_task_version), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash))",
        "INSERT INTO schema_metadata(key,value) VALUES('schema_version','2')",
    )
    for statement in statements:
        connection.execute(statement)
    for table in _IMMUTABLE_TABLES:
        connection.execute(f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")
        connection.execute(f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Transactionally preserve a verified v1 snapshot under *_v1 audit names."""
    legacy = ("schema_metadata", "views", "entries", "receipts", "attempts", "pointers")
    for table in _IMMUTABLE_TABLES[1:]:
        for action in ("update", "delete"):
            connection.execute(f"DROP TRIGGER {table}_immutable_{action}")
    for table in legacy:
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
    _create_v2_schema(connection)
    keys: dict[str, ContinuityKey] = {}
    for row in connection.execute("SELECT key_hash,key_json FROM attempts_v1"):
        try:
            value = json.loads(row["key_json"])
            key = ContinuityKey(**value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
        if key.key_hash != row["key_hash"] or canonical_json(key.to_dict()) != row["key_json"]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        keys[key.key_hash] = key
    view_keys = {row["key_hash"] for row in connection.execute("SELECT key_hash FROM views_v1")}
    if not view_keys <= set(keys):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for key in keys.values():
        connection.execute(
            "INSERT INTO continuity_keys(key_hash,key_json,workflow_id,code_task_id,code_task_version,acceptance_id,ingestion_key,payload_hash,evidence_binding_hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (key.key_hash, canonical_json(key.to_dict()), key.workflow_id, key.code_task_id, key.code_task_version, key.acceptance_id, key.ingestion_key, key.payload_hash, key.evidence_binding_hash),
        )
    for table in ("views", "entries", "receipts", "attempts"):
        columns = ",".join(_column_names(connection, f"{table}_v1"))
        connection.execute(f"INSERT INTO {table}({columns}) SELECT {columns} FROM {table}_v1")
    for row in connection.execute("SELECT * FROM pointers_v1"):
        candidates = [key for key in keys.values() if (key.workflow_id, key.code_task_id, key.code_task_version) == (row["workflow_id"], row["code_task_id"], row["code_task_version"])]
        if len(candidates) != 1:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        key = candidates[0]
        connection.execute(
            "INSERT INTO pointers(key_hash,workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?,?)",
            (key.key_hash, row["workflow_id"], row["code_task_id"], row["code_task_version"], row["view_id"], row["pointer_version"], row["fence_epoch"]),
        )


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})"))


def _verify_v1_schema(connection: sqlite3.Connection) -> None:
    required = {"schema_metadata", "views", "entries", "receipts", "attempts", "pointers"}
    actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= actual:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    expected = {
        "schema_metadata": ("key", "value"), "views": ("view_id", "key_hash", "manifest_hash", "cas_root_hash", "manifest_json"),
        "entries": ("view_id", "role", "path", "content_hash", "byte_length"), "receipts": ("receipt_hash", "key_hash", "view_id", "kind", "receipt_json"),
        "attempts": ("key_hash", "key_json", "fence_epoch", "sequence", "state", "view_id", "receipt_hash"),
        "pointers": ("workflow_id", "code_task_id", "code_task_version", "view_id", "pointer_version", "fence_epoch"),
    }
    if any(_column_names(connection, table) != columns for table, columns in expected.items()):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    expected_primary_keys = {
        "schema_metadata": (("key", 1),),
        "views": (("view_id", 1),),
        "entries": (("view_id", 1), ("role", 2), ("path", 3)),
        "receipts": (("receipt_hash", 1),),
        "attempts": (("key_hash", 1), ("sequence", 2)),
        "pointers": (("workflow_id", 1), ("code_task_id", 2), ("code_task_version", 3)),
    }
    for table, primary_key in expected_primary_keys.items():
        actual_primary_key = tuple((row[1], row[5]) for row in connection.execute(f"PRAGMA table_xinfo({table})") if row[5])
        if actual_primary_key != primary_key:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    entries_foreign_keys = {(row[2], row[3], row[4]) for row in connection.execute("PRAGMA foreign_key_list(entries)")}
    if entries_foreign_keys != {("views", "view_id", "view_id")} or not _has_unique_index(connection, "views", ("key_hash",)):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    attempts_sql = _normalized_schema_sql(connection, "table", "attempts")
    if "CHECK(STATEIN('CLAIMED','FROZEN','PUBLISHED','EXPIRED','ABANDONED'))" not in attempts_sql or "CHECK((STATE='CLAIMED'ANDVIEW_IDISNULLANDRECEIPT_HASHISNULL)OR(STATE!='CLAIMED'ANDVIEW_IDISNOTNULLANDRECEIPT_HASHISNOTNULL))" not in attempts_sql:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for table in ("views", "entries", "receipts", "attempts"):
        for action in ("UPDATE", "DELETE"):
            expected_trigger = f"CREATETRIGGER{table}_IMMUTABLE_{action.lower()}BEFORE{action}ON{table}BEGINSELECTRAISE(ABORT,'CONTINUITY_IMMUTABLE');END".upper()
            if _normalized_schema_sql(connection, "trigger", f"{table}_immutable_{action.lower()}") != expected_trigger:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _verify_v2_schema(connection: sqlite3.Connection) -> None:
    if _schema_version(connection) != _SCHEMA_VERSION or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    required = {"schema_metadata", "continuity_keys", "views", "entries", "receipts", "attempts", "pointers"}
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= names:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    expected = {
        "schema_metadata": ("key", "value"),
        "continuity_keys": ("key_hash", "key_json", "workflow_id", "code_task_id", "code_task_version", "acceptance_id", "ingestion_key", "payload_hash", "evidence_binding_hash"),
        "views": ("view_id", "key_hash", "manifest_hash", "cas_root_hash", "manifest_json"),
        "entries": ("view_id", "role", "path", "content_hash", "byte_length"),
        "receipts": ("receipt_hash", "key_hash", "view_id", "kind", "receipt_json"),
        "attempts": ("key_hash", "key_json", "fence_epoch", "sequence", "state", "view_id", "receipt_hash"),
        "pointers": ("key_hash", "workflow_id", "code_task_id", "code_task_version", "view_id", "pointer_version", "fence_epoch"),
    }
    if any(_column_names(connection, table) != columns for table, columns in expected.items()):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    expected_primary_keys = {
        "schema_metadata": (("key", 1),),
        "continuity_keys": (("key_hash", 1),),
        "views": (("view_id", 1),),
        "entries": (("view_id", 1), ("role", 2), ("path", 3)),
        "receipts": (("receipt_hash", 1),),
        "attempts": (("key_hash", 1), ("sequence", 2)),
        "pointers": (("key_hash", 1),),
    }
    for table, primary_key in expected_primary_keys.items():
        actual = tuple((row[1], row[5]) for row in connection.execute(f"PRAGMA table_xinfo({table})") if row[5])
        if actual != primary_key:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    expected_foreign_keys = {
        "views": {("continuity_keys", "key_hash", "key_hash")},
        "entries": {("views", "view_id", "view_id")},
        "receipts": {("continuity_keys", "key_hash", "key_hash"), ("views", "view_id", "view_id"), ("views", "key_hash", "key_hash")},
        "attempts": {("continuity_keys", "key_hash", "key_hash"), ("views", "view_id", "view_id"), ("views", "key_hash", "key_hash"), ("receipts", "receipt_hash", "receipt_hash"), ("receipts", "key_hash", "key_hash")},
        "pointers": {("continuity_keys", "key_hash", "key_hash"), ("views", "view_id", "view_id"), ("views", "key_hash", "key_hash")},
    }
    for table, foreign_keys in expected_foreign_keys.items():
        actual = {(row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
        if foreign_keys != actual:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for table, columns in (
        ("continuity_keys", ("key_json",)),
        ("views", ("key_hash",)),
        ("views", ("view_id", "key_hash")),
        ("receipts", ("receipt_hash", "key_hash")),
        ("pointers", ("workflow_id", "code_task_id", "code_task_version")),
    ):
        if not _has_unique_index(connection, table, columns):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for table in _IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            expected_trigger = f"CREATETRIGGER{table}_IMMUTABLE_{action.lower()}BEFORE{action}ON{table}BEGINSELECTRAISE(ABORT,'CONTINUITY_IMMUTABLE');END".upper()
            if _normalized_schema_sql(connection, "trigger", f"{table}_immutable_{action.lower()}") != expected_trigger:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _normalized_schema_sql(connection: sqlite3.Connection, kind: str, name: str) -> str:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
    return "" if row is None or not isinstance(row[0], str) else "".join(row[0].upper().split())


def _has_unique_index(connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> bool:
    for row in connection.execute(f"PRAGMA index_list({table})"):
        if not row[2]:
            continue
        indexed = tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})"))
        if indexed == columns:
            return True
    return False
