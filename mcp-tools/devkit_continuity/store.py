"""Independent SQLite v1 storage for immutable Continuity records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .canonical import canonical_json
from .cas import ContinuityCas, _bootstrap_cas
from .models import (
    ContinuityAttempt,
    ContinuityKey,
    ContinuityPointer,
    ContinuityReceipt,
    FrozenView,
)


class ContinuityStoreError(RuntimeError):
    """Stable private persistence failure."""


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
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode={mode}", uri=True)
            _verify_schema(connection)
            return cls(connection, read_only=read_only)
        except (sqlite3.Error, OSError, ContinuityStoreError) as error:
            if 'connection' in locals():
                connection.close()
            if isinstance(error, ContinuityStoreError):
                raise
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error

    def close(self) -> None:
        self._connection.close()

    def append_attempt_event(self, key: ContinuityKey, fence_epoch: int, state: str, view_id: str | None, receipt_hash: str | None) -> ContinuityAttempt:
        self._writable()
        attempt = ContinuityAttempt(key, fence_epoch, state, view_id, receipt_hash)
        row = self._connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM attempts WHERE key_hash=?", (key.key_hash,)).fetchone()
        self._connection.execute("INSERT INTO attempts(key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash) VALUES(?,?,?,?,?,?,?)", (key.key_hash, canonical_json(key.to_dict()), fence_epoch, row[0], state, view_id, receipt_hash))
        self._connection.commit()
        return attempt

    def current_attempt(self, key: ContinuityKey) -> ContinuityAttempt | None:
        row = self._connection.execute("SELECT * FROM attempts WHERE key_hash=? ORDER BY sequence DESC LIMIT 1", (key.key_hash,)).fetchone()
        return None if row is None else ContinuityAttempt(key, row["fence_epoch"], row["state"], row["view_id"], row["receipt_hash"])

    def insert_or_get_view(self, view: FrozenView, manifest_json: str) -> FrozenView:
        self._writable()
        if manifest_json != view.manifest_json:
            raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
        row = self._connection.execute("SELECT view_id,manifest_json FROM views WHERE key_hash=?", (view.key.key_hash,)).fetchone()
        if row is not None:
            if row["view_id"] != view.view_id or row["manifest_json"] != manifest_json:
                raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
            return view
        self._connection.execute("INSERT INTO views(view_id,key_hash,manifest_hash,cas_root_hash,manifest_json) VALUES(?,?,?,?,?)", (view.view_id, view.key.key_hash, view.manifest_hash, view.cas_root_hash, manifest_json))
        self._connection.executemany("INSERT INTO entries(view_id,role,path,content_hash,byte_length) VALUES(?,?,?,?,?)", [(view.view_id, item.role, item.path, item.content_hash, item.byte_length) for item in view.entries])
        self._connection.commit()
        return view

    def insert_or_get_receipt(self, receipt: ContinuityReceipt, receipt_json: str) -> ContinuityReceipt:
        self._writable()
        row = self._connection.execute("SELECT receipt_json FROM receipts WHERE receipt_hash=?", (receipt.receipt_hash,)).fetchone()
        if row is not None:
            if row["receipt_json"] != receipt_json:
                raise ContinuityStoreError("CONTINUITY_RECEIPT_CONFLICT")
            return receipt
        self._connection.execute("INSERT INTO receipts(receipt_hash,key_hash,view_id,kind,receipt_json) VALUES(?,?,?,?,?)", (receipt.receipt_hash, receipt.key.key_hash, receipt.view_id, receipt.kind, receipt_json))
        self._connection.commit()
        return receipt

    def pointer_for(self, key: ContinuityKey) -> ContinuityPointer | None:
        row = self._connection.execute("SELECT * FROM pointers WHERE workflow_id=? AND code_task_id=? AND code_task_version=?", (key.workflow_id, key.code_task_id, key.code_task_version)).fetchone()
        return None if row is None else ContinuityPointer(row["workflow_id"], row["code_task_id"], row["code_task_version"], row["view_id"], row["pointer_version"], row["fence_epoch"])

    def compare_and_swap_pointer(self, key: ContinuityKey, view: FrozenView, expected_pointer_version: int, expected_fence_epoch: int, new_fence_epoch: int) -> ContinuityPointer:
        self._writable()
        if view.key.key_hash != key.key_hash or type(expected_pointer_version) is not int or expected_pointer_version < 0 or type(expected_fence_epoch) is not int or expected_fence_epoch < 0 or type(new_fence_epoch) is not int or new_fence_epoch < 1:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        if self._connection.execute("SELECT 1 FROM views WHERE view_id=? AND key_hash=?", (view.view_id, key.key_hash)).fetchone() is None:
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        current = self.pointer_for(key)
        if (current is None and (expected_pointer_version != 0 or expected_fence_epoch != 0)) or (current is not None and (current.pointer_version != expected_pointer_version or current.fence_epoch != expected_fence_epoch)):
            raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        if current is None:
            self._connection.execute("INSERT INTO pointers(workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch) VALUES(?,?,?,?,?,?)", (key.workflow_id,key.code_task_id,key.code_task_version,view.view_id,1,new_fence_epoch))
            version = 1
        else:
            version = current.pointer_version + 1
            changed = self._connection.execute("UPDATE pointers SET view_id=?,pointer_version=?,fence_epoch=? WHERE workflow_id=? AND code_task_id=? AND code_task_version=? AND pointer_version=? AND fence_epoch=?", (view.view_id,version,new_fence_epoch,key.workflow_id,key.code_task_id,key.code_task_version,expected_pointer_version,expected_fence_epoch)).rowcount
            if changed != 1:
                raise ContinuityStoreError("CONTINUITY_POINTER_CONFLICT")
        self._connection.commit()
        return ContinuityPointer(key.workflow_id,key.code_task_id,key.code_task_version,view.view_id,version,new_fence_epoch)

    def _writable(self) -> None:
        if self.read_only:
            raise ContinuityStoreError("CONTINUITY_STORE_READ_ONLY")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS views (view_id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, cas_root_hash TEXT NOT NULL, manifest_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS entries (view_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL, PRIMARY KEY(view_id,role,path), FOREIGN KEY(view_id) REFERENCES views(view_id));
    CREATE TABLE IF NOT EXISTS receipts (receipt_hash TEXT PRIMARY KEY NOT NULL, key_hash TEXT NOT NULL, view_id TEXT NOT NULL, kind TEXT NOT NULL, receipt_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS attempts (key_hash TEXT NOT NULL, key_json TEXT NOT NULL, fence_epoch INTEGER NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('claimed','frozen','published','expired','abandoned')), view_id TEXT, receipt_hash TEXT, PRIMARY KEY(key_hash,sequence), CHECK((state='claimed' AND view_id IS NULL AND receipt_hash IS NULL) OR (state!='claimed' AND view_id IS NOT NULL AND receipt_hash IS NOT NULL)));
    CREATE TABLE IF NOT EXISTS pointers (workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL, view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL, fence_epoch INTEGER NOT NULL, PRIMARY KEY(workflow_id,code_task_id,code_task_version));
    INSERT OR IGNORE INTO schema_metadata(key,value) VALUES('schema_version','1');
    """)
    for table in ("views", "entries", "receipts", "attempts"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END")


def _bootstrap_store(database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
    """Runtime-private creation/migration seam; ordinary openers never create."""
    _bootstrap_cas(cas_root, scratch_root)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        _create_schema(connection)
        _verify_schema(connection)
        connection.commit()
        return ContinuityStore(connection, read_only=False)
    except Exception:
        connection.close()
        raise


def _verify_schema(connection: sqlite3.Connection) -> None:
    try:
        metadata = connection.execute("SELECT key,value FROM schema_metadata").fetchall()
        if metadata != [("schema_version", "1")]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        required = {"schema_metadata", "views", "entries", "receipts", "attempts", "pointers"}
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= names:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        columns = {
            "schema_metadata": (("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
            "views": (("view_id", "TEXT", 1, 1), ("key_hash", "TEXT", 1, 0), ("manifest_hash", "TEXT", 1, 0), ("cas_root_hash", "TEXT", 1, 0), ("manifest_json", "TEXT", 1, 0)),
            "entries": (("view_id", "TEXT", 1, 1), ("role", "TEXT", 1, 2), ("path", "TEXT", 1, 3), ("content_hash", "TEXT", 1, 0), ("byte_length", "INTEGER", 1, 0)),
            "receipts": (("receipt_hash", "TEXT", 1, 1), ("key_hash", "TEXT", 1, 0), ("view_id", "TEXT", 1, 0), ("kind", "TEXT", 1, 0), ("receipt_json", "TEXT", 1, 0)),
            "attempts": (("key_hash", "TEXT", 1, 1), ("key_json", "TEXT", 1, 0), ("fence_epoch", "INTEGER", 1, 0), ("sequence", "INTEGER", 1, 2), ("state", "TEXT", 1, 0), ("view_id", "TEXT", 0, 0), ("receipt_hash", "TEXT", 0, 0)),
            "pointers": (("workflow_id", "TEXT", 1, 1), ("code_task_id", "TEXT", 1, 2), ("code_task_version", "INTEGER", 1, 3), ("view_id", "TEXT", 1, 0), ("pointer_version", "INTEGER", 1, 0), ("fence_epoch", "INTEGER", 1, 0)),
        }
        for table, expected in columns.items():
            actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in connection.execute(f"PRAGMA table_xinfo({table})"))
            if actual != expected:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        foreign_keys = tuple(connection.execute("PRAGMA foreign_key_list(entries)"))
        if len(foreign_keys) != 1 or tuple(foreign_keys[0])[2:5] != ("views", "view_id", "view_id"):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        view_indexes = tuple(connection.execute("PRAGMA index_list(views)"))
        if sum(1 for row in view_indexes if row[2] and tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})")) == ("key_hash",)) != 1:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        attempt_sql = _normalized_schema_sql(connection, "table", "attempts")
        if "CHECK(STATEIN('CLAIMED','FROZEN','PUBLISHED','EXPIRED','ABANDONED'))" not in attempt_sql or "CHECK((STATE='CLAIMED'ANDVIEW_IDISNULLANDRECEIPT_HASHISNULL)OR(STATE!='CLAIMED'ANDVIEW_IDISNOTNULLANDRECEIPT_HASHISNOTNULL))" not in attempt_sql:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        for table in ("views", "entries", "receipts", "attempts"):
            for action in ("UPDATE", "DELETE"):
                expected = f"CREATETRIGGER{table}_IMMUTABLE_{action.lower()}BEFORE{action}ON{table}BEGINSELECTRAISE(ABORT,'CONTINUITY_IMMUTABLE');END".upper()
                if _normalized_schema_sql(connection, "trigger", f"{table}_immutable_{action.lower()}") != expected:
                    raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    except sqlite3.Error as error:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error


def _normalized_schema_sql(connection: sqlite3.Connection, kind: str, name: str) -> str:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
    if row is None or not isinstance(row[0], str):
        return ""
    return "".join(row[0].upper().split())
