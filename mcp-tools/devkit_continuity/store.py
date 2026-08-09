"""Independent SQLite storage with atomic Continuity state transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, canonical_json, is_hash_id
from .cas import ContinuityCas, _bootstrap_cas
from .models import (
    BoundExecutionReceipt,
    ChangedNode,
    ContinuityAttempt,
    ContinuityError,
    ContinuityKey,
    ContinuityPointer,
    ContinuityReceipt,
    CoverageGap,
    FrozenEntry,
    FrozenView,
    ReplayMetadata,
)

_SCHEMA_VERSION = "3"
_IMMUTABLE_TABLES = ("continuity_keys", "views", "entries", "receipts", "attempts")
_V1_TABLE_SQL = {
    "schema_metadata": "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "views": "CREATE TABLE views (view_id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, cas_root_hash TEXT NOT NULL, manifest_json TEXT NOT NULL)",
    "entries": "CREATE TABLE entries (view_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL, PRIMARY KEY(view_id,role,path), FOREIGN KEY(view_id) REFERENCES views(view_id))",
    "receipts": "CREATE TABLE receipts (receipt_hash TEXT PRIMARY KEY NOT NULL, key_hash TEXT NOT NULL, view_id TEXT NOT NULL, kind TEXT NOT NULL, receipt_json TEXT NOT NULL)",
    "attempts": "CREATE TABLE attempts (key_hash TEXT NOT NULL, key_json TEXT NOT NULL, fence_epoch INTEGER NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('claimed','frozen','published','expired','abandoned')), view_id TEXT, receipt_hash TEXT, PRIMARY KEY(key_hash,sequence), CHECK((state='claimed' AND view_id IS NULL AND receipt_hash IS NULL) OR (state!='claimed' AND view_id IS NOT NULL AND receipt_hash IS NOT NULL)))",
    "pointers": "CREATE TABLE pointers (workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL, view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL, fence_epoch INTEGER NOT NULL, PRIMARY KEY(workflow_id,code_task_id,code_task_version))",
}
_V2_TABLE_SQL = {
    "schema_metadata": "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    "continuity_keys": "CREATE TABLE continuity_keys (key_hash TEXT PRIMARY KEY NOT NULL, key_json TEXT UNIQUE NOT NULL, workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL CHECK(code_task_version>=0), acceptance_id TEXT NOT NULL, ingestion_key TEXT NOT NULL, payload_hash TEXT NOT NULL, evidence_binding_hash TEXT NOT NULL)",
    "views": "CREATE TABLE views (view_id TEXT PRIMARY KEY NOT NULL, key_hash TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, cas_root_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, UNIQUE(view_id,key_hash), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash))",
    "entries": "CREATE TABLE entries (view_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL CHECK(byte_length>=0), PRIMARY KEY(view_id,role,path), FOREIGN KEY(view_id) REFERENCES views(view_id))",
    "receipts": "CREATE TABLE receipts (receipt_hash TEXT PRIMARY KEY NOT NULL, key_hash TEXT NOT NULL, view_id TEXT NOT NULL, kind TEXT NOT NULL, receipt_json TEXT NOT NULL, UNIQUE(receipt_hash,key_hash), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash))",
    "attempts": "CREATE TABLE attempts (key_hash TEXT NOT NULL, key_json TEXT NOT NULL, fence_epoch INTEGER NOT NULL CHECK(fence_epoch>0), sequence INTEGER NOT NULL CHECK(sequence>0), state TEXT NOT NULL CHECK(state IN ('claimed','frozen','published','expired','abandoned')), view_id TEXT, receipt_hash TEXT, PRIMARY KEY(key_hash,sequence), CHECK((state='claimed' AND view_id IS NULL AND receipt_hash IS NULL) OR (state!='claimed' AND view_id IS NOT NULL AND receipt_hash IS NOT NULL)), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash), FOREIGN KEY(receipt_hash,key_hash) REFERENCES receipts(receipt_hash,key_hash))",
    "pointers": "CREATE TABLE pointers (key_hash TEXT PRIMARY KEY NOT NULL, workflow_id TEXT NOT NULL, code_task_id TEXT NOT NULL, code_task_version INTEGER NOT NULL CHECK(code_task_version>=0), view_id TEXT NOT NULL, pointer_version INTEGER NOT NULL CHECK(pointer_version>0), fence_epoch INTEGER NOT NULL CHECK(fence_epoch>0), UNIQUE(workflow_id,code_task_id,code_task_version), FOREIGN KEY(key_hash) REFERENCES continuity_keys(key_hash), FOREIGN KEY(view_id,key_hash) REFERENCES views(view_id,key_hash))",
}
_V1_AUDIT_TABLES = tuple(f"{table}_v1" for table in _V1_TABLE_SQL)
_V3_TABLE_SQL = {
    **_V2_TABLE_SQL,
    "v1_audit_seals": "CREATE TABLE v1_audit_seals (audit_id TEXT PRIMARY KEY NOT NULL CHECK(audit_id='v1'), source_version INTEGER NOT NULL CHECK(source_version=1), content_hash TEXT NOT NULL, schema_hash TEXT NOT NULL)",
}


def _v1_audit_table_sql(table: str) -> str:
    statement = _V1_TABLE_SQL[table]
    statement = statement.replace(
        f"CREATE TABLE {table} ", f'CREATE TABLE "{table}_v1" ', 1
    )
    return statement.replace("REFERENCES views(", 'REFERENCES "views_v1"(')


_V1_AUDIT_TABLE_SQL = {
    f"{table}_v1": _v1_audit_table_sql(table) for table in _V1_TABLE_SQL
}


def _column_contract(
    *columns: tuple[str, str, int, int],
) -> tuple[tuple[int, str, str, int, None, int, int], ...]:
    return tuple(
        (position, name, declared_type, not_null, None, primary_key, 0)
        for position, (name, declared_type, not_null, primary_key) in enumerate(columns)
    )


_V1_COLUMN_CONTRACTS = {
    "schema_metadata": _column_contract(("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)),
    "views": _column_contract(
        ("view_id", "TEXT", 1, 1),
        ("key_hash", "TEXT", 1, 0),
        ("manifest_hash", "TEXT", 1, 0),
        ("cas_root_hash", "TEXT", 1, 0),
        ("manifest_json", "TEXT", 1, 0),
    ),
    "entries": _column_contract(
        ("view_id", "TEXT", 1, 1),
        ("role", "TEXT", 1, 2),
        ("path", "TEXT", 1, 3),
        ("content_hash", "TEXT", 1, 0),
        ("byte_length", "INTEGER", 1, 0),
    ),
    "receipts": _column_contract(
        ("receipt_hash", "TEXT", 1, 1),
        ("key_hash", "TEXT", 1, 0),
        ("view_id", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("receipt_json", "TEXT", 1, 0),
    ),
    "attempts": _column_contract(
        ("key_hash", "TEXT", 1, 1),
        ("key_json", "TEXT", 1, 0),
        ("fence_epoch", "INTEGER", 1, 0),
        ("sequence", "INTEGER", 1, 2),
        ("state", "TEXT", 1, 0),
        ("view_id", "TEXT", 0, 0),
        ("receipt_hash", "TEXT", 0, 0),
    ),
    "pointers": _column_contract(
        ("workflow_id", "TEXT", 1, 1),
        ("code_task_id", "TEXT", 1, 2),
        ("code_task_version", "INTEGER", 1, 3),
        ("view_id", "TEXT", 1, 0),
        ("pointer_version", "INTEGER", 1, 0),
        ("fence_epoch", "INTEGER", 1, 0),
    ),
}
_V2_COLUMN_CONTRACTS = {
    **_V1_COLUMN_CONTRACTS,
    "continuity_keys": _column_contract(
        ("key_hash", "TEXT", 1, 1),
        ("key_json", "TEXT", 1, 0),
        ("workflow_id", "TEXT", 1, 0),
        ("code_task_id", "TEXT", 1, 0),
        ("code_task_version", "INTEGER", 1, 0),
        ("acceptance_id", "TEXT", 1, 0),
        ("ingestion_key", "TEXT", 1, 0),
        ("payload_hash", "TEXT", 1, 0),
        ("evidence_binding_hash", "TEXT", 1, 0),
    ),
    "views": _V1_COLUMN_CONTRACTS["views"],
    "entries": _V1_COLUMN_CONTRACTS["entries"],
    "receipts": _V1_COLUMN_CONTRACTS["receipts"],
    "attempts": _V1_COLUMN_CONTRACTS["attempts"],
    "pointers": _column_contract(
        ("key_hash", "TEXT", 1, 1),
        ("workflow_id", "TEXT", 1, 0),
        ("code_task_id", "TEXT", 1, 0),
        ("code_task_version", "INTEGER", 1, 0),
        ("view_id", "TEXT", 1, 0),
        ("pointer_version", "INTEGER", 1, 0),
        ("fence_epoch", "INTEGER", 1, 0),
    ),
}
_V1_FOREIGN_KEY_CONTRACTS = {
    "schema_metadata": (),
    "views": (),
    "entries": ((0, 0, "views", "view_id", "view_id", "NO ACTION", "NO ACTION", "NONE"),),
    "receipts": (),
    "attempts": (),
    "pointers": (),
}
_V2_FOREIGN_KEY_CONTRACTS = {
    "schema_metadata": (),
    "continuity_keys": (),
    "views": ((0, 0, "continuity_keys", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),),
    "entries": _V1_FOREIGN_KEY_CONTRACTS["entries"],
    "receipts": (
        (0, 0, "views", "view_id", "view_id", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "views", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "continuity_keys", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
    ),
    "attempts": (
        (0, 0, "receipts", "receipt_hash", "receipt_hash", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "receipts", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "views", "view_id", "view_id", "NO ACTION", "NO ACTION", "NONE"),
        (1, 1, "views", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
        (2, 0, "continuity_keys", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
    ),
    "pointers": (
        (0, 0, "views", "view_id", "view_id", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "views", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "continuity_keys", "key_hash", "key_hash", "NO ACTION", "NO ACTION", "NONE"),
    ),
}
_V1_INDEX_CONTRACTS = {
    "schema_metadata": (("pk", ("key",)),),
    "views": (("pk", ("view_id",)), ("u", ("key_hash",))),
    "entries": (("pk", ("view_id", "role", "path")),),
    "receipts": (("pk", ("receipt_hash",)),),
    "attempts": (("pk", ("key_hash", "sequence")),),
    "pointers": (("pk", ("workflow_id", "code_task_id", "code_task_version")),),
}
_V2_INDEX_CONTRACTS = {
    "schema_metadata": _V1_INDEX_CONTRACTS["schema_metadata"],
    "continuity_keys": (("pk", ("key_hash",)), ("u", ("key_json",))),
    "views": (("pk", ("view_id",)), ("u", ("key_hash",)), ("u", ("view_id", "key_hash"))),
    "entries": _V1_INDEX_CONTRACTS["entries"],
    "receipts": (("pk", ("receipt_hash",)), ("u", ("receipt_hash", "key_hash"))),
    "attempts": _V1_INDEX_CONTRACTS["attempts"],
    "pointers": (("pk", ("key_hash",)), ("u", ("workflow_id", "code_task_id", "code_task_version"))),
}
_V3_COLUMN_CONTRACTS = {
    **_V2_COLUMN_CONTRACTS,
    "v1_audit_seals": _column_contract(
        ("audit_id", "TEXT", 1, 1),
        ("source_version", "INTEGER", 1, 0),
        ("content_hash", "TEXT", 1, 0),
        ("schema_hash", "TEXT", 1, 0),
    ),
}
_V3_FOREIGN_KEY_CONTRACTS = {
    **_V2_FOREIGN_KEY_CONTRACTS,
    "v1_audit_seals": (),
}
_V3_INDEX_CONTRACTS = {
    **_V2_INDEX_CONTRACTS,
    "v1_audit_seals": (("pk", ("audit_id",)),),
}
_V1_AUDIT_COLUMN_CONTRACTS = {
    f"{table}_v1": contract for table, contract in _V1_COLUMN_CONTRACTS.items()
}
_V1_AUDIT_FOREIGN_KEY_CONTRACTS = {
    f"{table}_v1": tuple(
        (
            foreign_key_id,
            sequence,
            f"{target}_v1" if target in _V1_TABLE_SQL else target,
            source,
            destination,
            on_update,
            on_delete,
            match,
        )
        for (
            foreign_key_id,
            sequence,
            target,
            source,
            destination,
            on_update,
            on_delete,
            match,
        ) in contract
    )
    for table, contract in _V1_FOREIGN_KEY_CONTRACTS.items()
}
_V1_AUDIT_INDEX_CONTRACTS = {
    f"{table}_v1": contract for table, contract in _V1_INDEX_CONTRACTS.items()
}


class ContinuityStoreError(ContinuityError):
    """Stable persistence failure that deliberately exposes only a code."""


class ContinuityStore:
    def __init__(self, connection: sqlite3.Connection, *, read_only: bool) -> None:
        self._connection, self.read_only = connection, read_only
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open_readonly(cls, database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
        try:
            _verify_prepared_cas(cas_root, scratch_root, read_only=True)
        except Exception as error:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
        return cls._open(database, read_only=True)

    @classmethod
    def open_readwrite(cls, database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
        try:
            _verify_prepared_cas(cas_root, scratch_root, read_only=False)
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
            connection.row_factory = sqlite3.Row
            _configure_connection(connection)
            _verify_v3_schema(connection)
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
            if current is not None and current.state not in {"expired", "abandoned"}:
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

    def current_attempt(self, key: ContinuityKey) -> ContinuityAttempt | None:
        return self._current_attempt_row(key)

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

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        self._writable()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._connection.commit()
        except Exception as error:
            self._connection.rollback()
            if isinstance(error, ContinuityError):
                raise
            if isinstance(error, (sqlite3.Error, OSError, ValueError)):
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT") from error
            raise

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


def _verify_prepared_cas(cas_root: Path, scratch_root: Path, *, read_only: bool) -> None:
    """Close verification-only native CAS roots before opening SQLite."""
    cas = ContinuityCas.open_prepared(cas_root, scratch_root, read_only=read_only)
    try:
        return None
    finally:
        cas.close()


def _bootstrap_prepared_cas(cas_root: Path, scratch_root: Path) -> None:
    """Runtime-only CAS creation verification never retains an unused root handle."""
    cas = _bootstrap_cas(cas_root, scratch_root)
    try:
        return None
    finally:
        cas.close()


def _bootstrap_store(database: Path, cas_root: Path, scratch_root: Path) -> ContinuityStore:
    """Runtime-private creation/migration seam; ordinary openers never create."""
    connection: sqlite3.Connection | None = None
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        _configure_connection(connection)
        _verify_bootstrap_preflight(connection)
        _bootstrap_prepared_cas(cas_root, scratch_root)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        version = _schema_version(connection)
        if version is None:
            _create_v3_schema(connection)
        elif version == "1":
            _verify_v1_schema(connection)
            _migrate_v1_to_v3(connection)
        elif version == "2":
            _verify_v2_schema(connection)
            _migrate_v2_to_v3(connection)
        elif version == _SCHEMA_VERSION:
            _verify_v3_schema(connection)
        else:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        _verify_v3_schema(connection)
        connection.commit()
        return ContinuityStore(connection, read_only=False)
    except (sqlite3.Error, OSError, ContinuityError) as error:
        _rollback_and_close(connection)
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
    except Exception:
        _rollback_and_close(connection)
        raise


def _verify_bootstrap_preflight(connection: sqlite3.Connection) -> None:
    """Reject malformed legacy state before a journal-mode change can touch it."""
    version = _schema_version(connection)
    if version is None:
        return
    if version == "1":
        _verify_v1_schema(connection)
        _validate_v1_state(connection)
        return
    if version == "2":
        _verify_v2_schema(connection)
        return
    if version == "3":
        _verify_v3_schema(connection)
        return
    raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _rollback_and_close(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except sqlite3.Error:
        pass
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _schema_version(connection: sqlite3.Connection) -> str | None:
    rows = connection.execute("SELECT type FROM sqlite_master WHERE name='schema_metadata'").fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != "table":
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    rows = connection.execute("SELECT key,value FROM schema_metadata").fetchall()
    if len(rows) != 1 or rows[0][0] != "schema_version":
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return rows[0][1]


def _create_v3_schema(connection: sqlite3.Connection, *, seal_triggers: bool = True) -> None:
    for statement in _V3_TABLE_SQL.values():
        connection.execute(statement)
    connection.execute("INSERT INTO schema_metadata(key,value) VALUES('schema_version','3')")
    for table in _IMMUTABLE_TABLES:
        _create_immutable_triggers(connection, table, ("UPDATE", "DELETE"))
    if seal_triggers:
        _create_immutable_triggers(connection, "v1_audit_seals", ("INSERT", "UPDATE", "DELETE"))


def _create_immutable_triggers(
    connection: sqlite3.Connection, table: str, actions: tuple[str, ...]
) -> None:
    for action in actions:
        connection.execute(
            f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END"
        )


def _create_v1_audit_immutable_triggers(connection: sqlite3.Connection) -> None:
    for table in _V1_AUDIT_TABLES:
        _create_immutable_triggers(connection, table, ("INSERT", "UPDATE", "DELETE"))


def _migrate_v1_to_v3(connection: sqlite3.Connection) -> None:
    """Transactionally preserve and seal a verified v1 snapshot."""
    keys = _validate_v1_state(connection)
    legacy = ("schema_metadata", "views", "entries", "receipts", "attempts", "pointers")
    for table in _IMMUTABLE_TABLES[1:]:
        for action in ("update", "delete"):
            connection.execute(f"DROP TRIGGER {table}_immutable_{action}")
    for table in legacy:
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
    _create_v3_schema(connection, seal_triggers=False)
    _create_v1_audit_immutable_triggers(connection)
    connection.execute(
        "INSERT INTO v1_audit_seals(audit_id,source_version,content_hash,schema_hash) VALUES(?,?,?,?)",
        ("v1", 1, _audit_content_hash(connection), _audit_schema_hash(connection)),
    )
    _create_immutable_triggers(connection, "v1_audit_seals", ("INSERT", "UPDATE", "DELETE"))
    _copy_verified_v1_state_to_v3(connection, keys)


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Upgrade only a strictly verified v2 database with no unauditable snapshot."""
    connection.execute(_V3_TABLE_SQL["v1_audit_seals"])
    connection.execute("UPDATE schema_metadata SET value='3' WHERE key='schema_version'")
    _create_immutable_triggers(connection, "v1_audit_seals", ("INSERT", "UPDATE", "DELETE"))


def _copy_verified_v1_state_to_v3(
    connection: sqlite3.Connection, keys: dict[str, ContinuityKey]
) -> None:
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


def _validate_v1_state(
    connection: sqlite3.Connection, *, suffix: str = ""
) -> dict[str, ContinuityKey]:
    """Rebuild every v1 record before migration or audit-seal acceptance."""
    if suffix not in {"", "_v1"}:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    try:
        keys = _v1_keys(connection, suffix)
        views = _v1_views(connection, keys, suffix)
        receipts = _v1_receipts(connection, keys, views, suffix)
        attempts = _v1_attempts(connection, keys, views, receipts, suffix)
        _validate_v1_pointers(connection, keys, views, attempts, suffix)
        return keys
    except ContinuityStoreError:
        raise
    except (ContinuityError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error


def _v1_keys(connection: sqlite3.Connection, suffix: str) -> dict[str, ContinuityKey]:
    keys: dict[str, ContinuityKey] = {}
    for row in connection.execute(
        f"SELECT key_hash,key_json FROM attempts{suffix} ORDER BY key_hash,sequence"
    ):
        value = json.loads(row["key_json"])
        key = ContinuityKey(**value)
        if key.key_hash != row["key_hash"] or canonical_json(key.to_dict()) != row["key_json"]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        existing = keys.get(key.key_hash)
        if existing is not None and existing != key:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        keys[key.key_hash] = key
    return keys


def _v1_views(
    connection: sqlite3.Connection, keys: dict[str, ContinuityKey], suffix: str
) -> dict[str, FrozenView]:
    entries: dict[str, list[FrozenEntry]] = {}
    for row in connection.execute(
        f"SELECT view_id,role,path,content_hash,byte_length FROM entries{suffix} "
        "ORDER BY view_id,role,path,content_hash"
    ):
        entries.setdefault(row["view_id"], []).append(
            FrozenEntry(row["role"], row["path"], row["content_hash"], row["byte_length"])
        )
    views: dict[str, FrozenView] = {}
    for row in connection.execute(
        f"SELECT view_id,key_hash,manifest_hash,cas_root_hash,manifest_json FROM views{suffix} "
        "ORDER BY view_id"
    ):
        key = keys.get(row["key_hash"])
        if key is None:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        view = _v1_frozen_view(key, row, tuple(entries.pop(row["view_id"], ())))
        if view.view_id != row["view_id"]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        views[view.view_id] = view
    if entries:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return views


def _v1_frozen_view(
    key: ContinuityKey, row: sqlite3.Row, entries: tuple[FrozenEntry, ...]
) -> FrozenView:
    view = _frozen_view_from_manifest(key, row, entries)
    if view.replay_metadata is not None:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return view


def _frozen_view_from_manifest(
    key: ContinuityKey, row: sqlite3.Row | dict[str, Any], entries: tuple[FrozenEntry, ...]
) -> FrozenView:
    """Rebuild one persisted v1 or v2 view with no permissive fields."""
    try:
        manifest_json = row["manifest_json"]
        manifest = json.loads(manifest_json)
        if not isinstance(manifest, dict) or canonical_json(manifest) != manifest_json:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        schema = manifest.get("schema")
        common_fields = {
            "schema",
            "key",
            "entries",
            "input_snapshot_ids",
            "output_snapshot_ids",
            "checkpoint_ids",
            "query_ids",
            "verification_artifact_hashes",
            "execution_receipt_ids",
            "request_hash",
            "evidence_hash",
            "changed_nodes",
            "coverage_gaps",
            "execution_receipts",
        }
        if schema == "continuity-frozen-view/v1":
            if set(manifest) != common_fields:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            replay_metadata = None
        elif schema == "continuity-frozen-view/v2":
            if set(manifest) != common_fields | {"replay_metadata"}:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            metadata = manifest["replay_metadata"]
            if not isinstance(metadata, dict) or set(metadata) != {
                "task_kind",
                "intent_id",
                "workspace_hash",
                "write_scope",
                "indexed_diff_hash",
                "language",
                "framework",
                "checkpoint_hash",
            } or not isinstance(metadata["write_scope"], list):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            replay_metadata = ReplayMetadata(
                task_kind=metadata["task_kind"],
                intent_id=metadata["intent_id"],
                workspace_hash=metadata["workspace_hash"],
                write_scope=tuple(metadata["write_scope"]),
                indexed_diff_hash=metadata["indexed_diff_hash"],
                language=metadata["language"],
                framework=metadata["framework"],
                checkpoint_hash=metadata["checkpoint_hash"],
            )
        else:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        if (
            manifest["key"] != key.to_dict()
            or manifest["entries"] != [item.to_dict() for item in entries]
        ):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        view = FrozenView.create(
            key=key,
            entries=entries,
            input_snapshot_ids=_v1_manifest_list(manifest, "input_snapshot_ids"),
            output_snapshot_ids=_v1_manifest_list(manifest, "output_snapshot_ids"),
            checkpoint_ids=_v1_manifest_list(manifest, "checkpoint_ids"),
            query_ids=_v1_manifest_list(manifest, "query_ids"),
            verification_artifact_hashes=_v1_manifest_list(
                manifest, "verification_artifact_hashes"
            ),
            execution_receipt_ids=_v1_manifest_list(manifest, "execution_receipt_ids"),
            request_hash=manifest["request_hash"],
            evidence_hash=manifest["evidence_hash"],
            replay_metadata=replay_metadata,
            changed_nodes=tuple(
                _v1_changed_node(value)
                for value in _v1_manifest_list(manifest, "changed_nodes")
            ),
            coverage_gaps=tuple(
                _v1_coverage_gap(value)
                for value in _v1_manifest_list(manifest, "coverage_gaps")
            ),
            execution_receipts=tuple(
                _v1_execution_receipt(value)
                for value in _v1_manifest_list(manifest, "execution_receipts")
            ),
        )
    except ContinuityStoreError:
        raise
    except (ContinuityError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error
    if (
        view.view_id != row["view_id"]
        or view.manifest_hash != row["manifest_hash"]
        or view.cas_root_hash != row["cas_root_hash"]
        or view.manifest_json != row["manifest_json"]
    ):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return view


def _v1_manifest_list(manifest: dict[str, Any], name: str) -> list[Any]:
    value = manifest[name]
    if not isinstance(value, list):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return value


def _v1_changed_node(value: Any) -> ChangedNode:
    if not isinstance(value, dict) or not isinstance(value.get("attributes"), list):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    attributes: list[tuple[Any, Any]] = []
    for item in value["attributes"]:
        if not isinstance(item, list) or len(item) != 2:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        attributes.append((item[0], item[1]))
    return ChangedNode(
        node_id=value["node_id"],
        kind=value["kind"],
        path=value["path"],
        content_hash=value["content_hash"],
        name=value["name"],
        qualified_name=value["qualified_name"],
        start_line=value["start_line"],
        end_line=value["end_line"],
        attributes=tuple(attributes),
        extractor_id=value["extractor_id"],
        extractor_version=value["extractor_version"],
        provenance=value["provenance"],
        start_byte=value["start_byte"],
        end_byte=value["end_byte"],
    )


def _v1_coverage_gap(value: Any) -> CoverageGap:
    if not isinstance(value, dict):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return CoverageGap(value["path"], value["code"], value["message"])


def _v1_execution_receipt(value: Any) -> BoundExecutionReceipt:
    if not isinstance(value, dict) or not isinstance(value.get("command_spec"), list):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return BoundExecutionReceipt(
        receipt_id=value["receipt_id"],
        kind=value["kind"],
        workflow_id=value["workflow_id"],
        task_id=value["task_id"],
        acceptance_id=value["acceptance_id"],
        workspace_hash=value["workspace_hash"],
        output_snapshot_id=value["output_snapshot_id"],
        command_spec=tuple(value["command_spec"]),
        command_spec_hash=value["command_spec_hash"],
        input_hash=value["input_hash"],
        output_hash=value["output_hash"],
        exit_code=value["exit_code"],
        success=value["success"],
    )


def _v1_receipts(
    connection: sqlite3.Connection,
    keys: dict[str, ContinuityKey],
    views: dict[str, FrozenView],
    suffix: str,
) -> dict[str, ContinuityReceipt]:
    receipts: dict[str, ContinuityReceipt] = {}
    for row in connection.execute(
        f"SELECT receipt_hash,key_hash,view_id,kind,receipt_json FROM receipts{suffix} "
        "ORDER BY receipt_hash"
    ):
        key = keys.get(row["key_hash"])
        view = views.get(row["view_id"])
        if key is None or view is None or view.key != key:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        receipt = ContinuityReceipt(key, row["view_id"], row["receipt_hash"], row["kind"])
        if row["receipt_json"] != _receipt_json(receipt):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        receipts[receipt.receipt_hash] = receipt
    return receipts


def _v1_attempts(
    connection: sqlite3.Connection,
    keys: dict[str, ContinuityKey],
    views: dict[str, FrozenView],
    receipts: dict[str, ContinuityReceipt],
    suffix: str,
) -> dict[str, tuple[ContinuityAttempt, ...]]:
    attempts: dict[str, list[ContinuityAttempt]] = {}
    for row in connection.execute(
        f"SELECT key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash FROM attempts{suffix} "
        "ORDER BY key_hash,sequence"
    ):
        key = keys.get(row["key_hash"])
        if key is None or row["key_json"] != canonical_json(key.to_dict()):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        attempt = ContinuityAttempt(
            key, row["fence_epoch"], row["state"], row["view_id"], row["receipt_hash"]
        )
        if type(row["sequence"]) is not int or row["sequence"] != len(attempts.get(key.key_hash, ())) + 1:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        if attempt.state != "claimed":
            view = views.get(attempt.view_id or "")
            receipt = receipts.get(attempt.receipt_hash or "")
            if view is None or receipt is None or view.key != key or receipt.key != key or receipt.view_id != attempt.view_id:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            if attempt.state in {"frozen", "published"} and receipt.kind != attempt.state:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            if attempt.state in {"expired", "abandoned"} and receipt.kind not in {"frozen", "published"}:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        attempts.setdefault(key.key_hash, []).append(attempt)
    verified = {key_hash: tuple(history) for key_hash, history in attempts.items()}
    for history in verified.values():
        _validate_v1_attempt_history(history)
    return verified


def _validate_v1_attempt_history(history: tuple[ContinuityAttempt, ...]) -> None:
    """Apply v1 append rules without retroactively imposing v2 transitions."""
    previous: ContinuityAttempt | None = None
    for attempt in history:
        if previous is not None and (
            attempt.fence_epoch < previous.fence_epoch
            or (
                attempt.fence_epoch == previous.fence_epoch
                and previous.state != "claimed"
            )
        ):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        previous = attempt


def _validate_v1_pointers(
    connection: sqlite3.Connection,
    keys: dict[str, ContinuityKey],
    views: dict[str, FrozenView],
    attempts: dict[str, tuple[ContinuityAttempt, ...]],
    suffix: str,
) -> None:
    """Require each legacy pointer to bind a verified same-fence view record."""
    for row in connection.execute(
        f"SELECT workflow_id,code_task_id,code_task_version,view_id,pointer_version,fence_epoch "
        f"FROM pointers{suffix}"
    ):
        candidates = [
            key
            for key in keys.values()
            if (key.workflow_id, key.code_task_id, key.code_task_version)
            == (row["workflow_id"], row["code_task_id"], row["code_task_version"])
        ]
        if len(candidates) != 1:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        key = candidates[0]
        pointer = ContinuityPointer(
            key.workflow_id,
            key.code_task_id,
            key.code_task_version,
            row["view_id"],
            row["pointer_version"],
            row["fence_epoch"],
        )
        if views.get(pointer.view_id) is None or views[pointer.view_id].key != key:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        if not any(
            attempt.fence_epoch == pointer.fence_epoch and attempt.view_id == pointer.view_id
            for attempt in attempts.get(key.key_hash, ())
        ):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _validate_live_state(connection: sqlite3.Connection) -> None:
    """Verify every current v2/v3 relation and parse every frozen manifest."""
    try:
        keys: dict[str, ContinuityKey] = {}
        for row in connection.execute(
            "SELECT key_hash,key_json,workflow_id,code_task_id,code_task_version,"
            "acceptance_id,ingestion_key,payload_hash,evidence_binding_hash "
            "FROM continuity_keys ORDER BY key_hash"
        ):
            value = json.loads(row["key_json"])
            key = ContinuityKey(**value)
            if (
                key.key_hash != row["key_hash"]
                or canonical_json(key.to_dict()) != row["key_json"]
                or (
                    key.workflow_id,
                    key.code_task_id,
                    key.code_task_version,
                    key.acceptance_id,
                    key.ingestion_key,
                    key.payload_hash,
                    key.evidence_binding_hash,
                )
                != (
                    row["workflow_id"],
                    row["code_task_id"],
                    row["code_task_version"],
                    row["acceptance_id"],
                    row["ingestion_key"],
                    row["payload_hash"],
                    row["evidence_binding_hash"],
                )
            ):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            keys[key.key_hash] = key
        entries: dict[str, list[FrozenEntry]] = {}
        for row in connection.execute(
            "SELECT view_id,role,path,content_hash,byte_length FROM entries "
            "ORDER BY view_id,role,path,content_hash"
        ):
            entries.setdefault(row["view_id"], []).append(
                FrozenEntry(
                    row["role"], row["path"], row["content_hash"], row["byte_length"]
                )
            )
        views: dict[str, FrozenView] = {}
        for row in connection.execute(
            "SELECT view_id,key_hash,manifest_hash,cas_root_hash,manifest_json FROM views "
            "ORDER BY view_id"
        ):
            key = keys.get(row["key_hash"])
            if key is None:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            view = _frozen_view_from_manifest(
                key, row, tuple(entries.pop(row["view_id"], ()))
            )
            if view.view_id != row["view_id"] or view.key != key:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            views[view.view_id] = view
        if entries:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        receipts: dict[str, ContinuityReceipt] = {}
        for row in connection.execute(
            "SELECT receipt_hash,key_hash,view_id,kind,receipt_json FROM receipts "
            "ORDER BY receipt_hash"
        ):
            key = keys.get(row["key_hash"])
            view = views.get(row["view_id"])
            if key is None or view is None or view.key != key:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            receipt = ContinuityReceipt(key, row["view_id"], row["receipt_hash"], row["kind"])
            if row["receipt_json"] != _receipt_json(receipt):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            receipts[receipt.receipt_hash] = receipt
        attempts: dict[str, list[ContinuityAttempt]] = {}
        for row in connection.execute(
            "SELECT key_hash,key_json,fence_epoch,sequence,state,view_id,receipt_hash "
            "FROM attempts ORDER BY key_hash,sequence"
        ):
            key = keys.get(row["key_hash"])
            if key is None or row["key_json"] != canonical_json(key.to_dict()):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            attempt = ContinuityAttempt(
                key,
                row["fence_epoch"],
                row["state"],
                row["view_id"],
                row["receipt_hash"],
            )
            history = attempts.setdefault(key.key_hash, [])
            if row["sequence"] != len(history) + 1:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            if attempt.state != "claimed":
                view = views.get(attempt.view_id or "")
                receipt = receipts.get(attempt.receipt_hash or "")
                if (
                    view is None
                    or receipt is None
                    or view.key != key
                    or receipt.key != key
                    or receipt.view_id != attempt.view_id
                    or (
                        attempt.state in {"frozen", "published"}
                        and receipt.kind != attempt.state
                    )
                    or (
                        attempt.state in {"expired", "abandoned"}
                        and receipt.kind not in {"frozen", "published"}
                    )
                ):
                    raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            history.append(attempt)
        if set(attempts) != set(keys):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        for history in attempts.values():
            _validate_live_attempt_history(tuple(history))
        for row in connection.execute(
            "SELECT key_hash,workflow_id,code_task_id,code_task_version,view_id,"
            "pointer_version,fence_epoch FROM pointers ORDER BY key_hash"
        ):
            key = keys.get(row["key_hash"])
            if key is None or (
                key.workflow_id,
                key.code_task_id,
                key.code_task_version,
            ) != (
                row["workflow_id"],
                row["code_task_id"],
                row["code_task_version"],
            ):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            pointer = ContinuityPointer(
                key.workflow_id,
                key.code_task_id,
                key.code_task_version,
                row["view_id"],
                row["pointer_version"],
                row["fence_epoch"],
            )
            if (
                views.get(pointer.view_id) is None
                or views[pointer.view_id].key != key
                or not any(
                    attempt.fence_epoch == pointer.fence_epoch
                    and attempt.view_id == pointer.view_id
                    for attempt in attempts[key.key_hash]
                )
            ):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    except ContinuityStoreError:
        raise
    except (ContinuityError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED") from error


def _validate_live_attempt_history(history: tuple[ContinuityAttempt, ...]) -> None:
    """Accept only durable v2/v3 lifecycle transitions, including sealed legacy rows."""
    previous: ContinuityAttempt | None = None
    allowed_same_epoch = {
        ("claimed", "frozen"),
        ("frozen", "published"),
        ("frozen", "expired"),
        ("frozen", "abandoned"),
        ("published", "expired"),
        ("published", "abandoned"),
    }
    for attempt in history:
        if previous is not None:
            if attempt.fence_epoch < previous.fence_epoch:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            if attempt.fence_epoch == previous.fence_epoch:
                if (previous.state, attempt.state) not in allowed_same_epoch:
                    raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
            elif previous.state not in {"expired", "abandoned"}:
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        previous = attempt


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})"))


def _verify_v1_schema(connection: sqlite3.Connection) -> None:
    if _schema_version(connection) != "1" or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    _verify_schema_contract(
        connection,
        table_sql=_V1_TABLE_SQL,
        column_contracts=_V1_COLUMN_CONTRACTS,
        foreign_key_contracts=_V1_FOREIGN_KEY_CONTRACTS,
        index_contracts=_V1_INDEX_CONTRACTS,
        immutable_tables=("views", "entries", "receipts", "attempts"),
        allow_v1_audit=False,
    )


def _verify_v2_schema(connection: sqlite3.Connection) -> None:
    if _schema_version(connection) != "2" or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    _verify_schema_contract(
        connection,
        table_sql=_V2_TABLE_SQL,
        column_contracts=_V2_COLUMN_CONTRACTS,
        foreign_key_contracts=_V2_FOREIGN_KEY_CONTRACTS,
        index_contracts=_V2_INDEX_CONTRACTS,
        immutable_tables=_IMMUTABLE_TABLES,
        allow_v1_audit=False,
    )
    _validate_live_state(connection)


def _verify_v3_schema(connection: sqlite3.Connection) -> None:
    if _schema_version(connection) != "3" or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    audit_present = _verify_schema_contract(
        connection,
        table_sql=_V3_TABLE_SQL,
        column_contracts=_V3_COLUMN_CONTRACTS,
        foreign_key_contracts=_V3_FOREIGN_KEY_CONTRACTS,
        index_contracts=_V3_INDEX_CONTRACTS,
        immutable_tables=_IMMUTABLE_TABLES,
        allow_v1_audit=True,
        fully_immutable_tables=("v1_audit_seals",),
    )
    if audit_present:
        _verify_table_contracts(
            connection,
            table_sql=_V1_AUDIT_TABLE_SQL,
            column_contracts=_V1_AUDIT_COLUMN_CONTRACTS,
            foreign_key_contracts=_V1_AUDIT_FOREIGN_KEY_CONTRACTS,
            index_contracts=_V1_AUDIT_INDEX_CONTRACTS,
            check_foreign_keys=False,
        )
        _validate_v1_state(connection, suffix="_v1")
        _verify_v1_audit_seal(connection, expected_rows=1)
    else:
        _verify_v1_audit_seal(connection, expected_rows=0)
    _validate_live_state(connection)


def _verify_schema_contract(
    connection: sqlite3.Connection,
    *,
    table_sql: dict[str, str],
    column_contracts: dict[str, tuple[tuple[int, str, str, int, None, int, int], ...]],
    foreign_key_contracts: dict[str, tuple[tuple[int, int, str, str, str, str, str, str], ...]],
    index_contracts: dict[str, tuple[tuple[str, tuple[str, ...]], ...]],
    immutable_tables: tuple[str, ...],
    allow_v1_audit: bool,
    fully_immutable_tables: tuple[str, ...] = (),
) -> bool:
    audit_present = _verify_schema_objects(
        connection,
        table_names=set(table_sql),
        immutable_tables=immutable_tables,
        allow_v1_audit=allow_v1_audit,
        fully_immutable_tables=fully_immutable_tables,
    )
    _verify_table_contracts(
        connection,
        table_sql=table_sql,
        column_contracts=column_contracts,
        foreign_key_contracts=foreign_key_contracts,
        index_contracts=index_contracts,
        check_foreign_keys=True,
    )
    return audit_present


def _verify_table_contracts(
    connection: sqlite3.Connection,
    *,
    table_sql: dict[str, str],
    column_contracts: dict[str, tuple[tuple[int, str, str, int, None, int, int], ...]],
    foreign_key_contracts: dict[str, tuple[tuple[int, int, str, str, str, str, str, str], ...]],
    index_contracts: dict[str, tuple[tuple[str, tuple[str, ...]], ...]],
    check_foreign_keys: bool,
) -> None:
    if check_foreign_keys and connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for table, expected_sql in table_sql.items():
        if _normalized_schema_sql(connection, "table", table) != _normalize_sql(expected_sql):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        actual_columns = tuple(tuple(row) for row in connection.execute(f"PRAGMA table_xinfo({table})"))
        if actual_columns != column_contracts[table]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        actual_foreign_keys = tuple(
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        )
        if actual_foreign_keys != foreign_key_contracts[table]:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        _verify_index_contract(
            connection,
            table,
            column_contracts[table],
            index_contracts[table],
        )


def _verify_schema_objects(
    connection: sqlite3.Connection,
    *,
    table_names: set[str],
    immutable_tables: tuple[str, ...],
    allow_v1_audit: bool,
    fully_immutable_tables: tuple[str, ...],
) -> bool:
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE lower(substr(name,1,7)) != 'sqlite_' ORDER BY type,name"
        )
    )
    actual_tables = {name for kind, name, _table, _sql in objects if kind == "table"}
    allowed_table_sets = {frozenset(table_names)}
    if allow_v1_audit:
        allowed_table_sets.add(frozenset(table_names | set(_V1_AUDIT_TABLES)))
    if frozenset(actual_tables) not in allowed_table_sets:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    audit_present = bool(set(_V1_AUDIT_TABLES) & actual_tables)
    expected_actions = {
        table: ("UPDATE", "DELETE") for table in immutable_tables
    }
    expected_actions.update(
        {table: ("INSERT", "UPDATE", "DELETE") for table in fully_immutable_tables}
    )
    if audit_present:
        expected_actions.update(
            {table: ("INSERT", "UPDATE", "DELETE") for table in _V1_AUDIT_TABLES}
        )
    expected_trigger_names = {
        f"{table}_immutable_{action.lower()}"
        for table, actions in expected_actions.items()
        for action in actions
    }
    actual_triggers = {
        name: sql for kind, name, _table, sql in objects if kind == "trigger"
    }
    if set(actual_triggers) != expected_trigger_names:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    for table, actions in expected_actions.items():
        for action in actions:
            name = f"{table}_immutable_{action.lower()}"
            trigger_sql = actual_triggers[name]
            if (
                not isinstance(trigger_sql, str)
                or _normalize_sql(trigger_sql) != _immutable_trigger_sql(table, action)
            ):
                raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    if any(kind not in {"table", "trigger"} for kind, _name, _table, _sql in objects):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    return audit_present


def _immutable_trigger_sql(table: str, action: str) -> str:
    return _normalize_sql(
        f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'CONTINUITY_IMMUTABLE'); END"
    )


def _verify_v1_audit_seal(connection: sqlite3.Connection, *, expected_rows: int) -> None:
    rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT audit_id,source_version,content_hash,schema_hash FROM v1_audit_seals "
            "ORDER BY audit_id"
        )
    )
    if len(rows) != expected_rows:
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
    if expected_rows == 0:
        return
    audit_id, source_version, content_hash, schema_hash = rows[0]
    if (
        audit_id != "v1"
        or source_version != 1
        or not is_hash_id(content_hash)
        or not is_hash_id(schema_hash)
        or content_hash != _audit_content_hash(connection)
        or schema_hash != _audit_schema_hash(connection)
    ):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _audit_content_hash(connection: sqlite3.Connection) -> str:
    tables: list[dict[str, Any]] = []
    for table, columns in _V1_COLUMN_CONTRACTS.items():
        names = tuple(column[1] for column in columns)
        primary_key = tuple(column[1] for column in sorted(columns, key=lambda column: column[5]) if column[5])
        query = (
            f"SELECT {','.join(names)} FROM {table}_v1 "
            f"ORDER BY {','.join(primary_key)}"
        )
        tables.append(
            {
                "table": table,
                "columns": list(names),
                "rows": [list(row) for row in connection.execute(query)],
            }
        )
    return canonical_hash({"schema": "continuity-v1-audit-content/v1", "tables": tables})


def _audit_schema_hash(connection: sqlite3.Connection) -> str:
    names = set(_V1_AUDIT_TABLES)
    names.update(
        f"{table}_immutable_{action.lower()}"
        for table in _V1_AUDIT_TABLES
        for action in ("INSERT", "UPDATE", "DELETE")
    )
    records = [
        {"type": kind, "name": name, "table": table, "sql": sql}
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE lower(substr(name,1,7)) != 'sqlite_' AND type IN ('table','trigger') "
            "ORDER BY type,name"
        )
        if name in names
    ]
    return canonical_hash({"schema": "continuity-v1-audit-schema/v1", "objects": records})


def _normalized_schema_sql(connection: sqlite3.Connection, kind: str, name: str) -> str:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
    return "" if row is None or not isinstance(row[0], str) else _normalize_sql(row[0])


def _normalize_sql(sql: str) -> str:
    return "".join(sql.upper().split())


def _verify_index_contract(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[tuple[int, str, str, int, None, int, int], ...],
    expected: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    actual: list[tuple[str, tuple[str, ...]]] = []
    column_ids = {name: position for position, name, *_rest in columns}
    for row in connection.execute(f"PRAGMA index_list({table})"):
        if row[2] != 1 or row[3] not in {"pk", "u"} or row[4] != 0:
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        xinfo = tuple(tuple(item) for item in connection.execute(f"PRAGMA index_xinfo({row[1]})"))
        key_columns = tuple(item[2] for item in xinfo if item[5] == 1)
        if (
            any(not isinstance(name, str) or name not in column_ids for name in key_columns)
            or xinfo != _expected_index_xinfo(column_ids, key_columns)
        ):
            raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")
        actual.append((row[3], key_columns))
    if tuple(sorted(actual, key=repr)) != tuple(sorted(expected, key=repr)):
        raise ContinuityStoreError("CONTINUITY_STORE_UNPREPARED")


def _expected_index_xinfo(
    column_ids: dict[str, int], key_columns: tuple[str | None, ...]
) -> tuple[tuple[int, int, str | None, int, str, int], ...]:
    return tuple(
        (position, column_ids[column], column, 0, "BINARY", 1)
        for position, column in enumerate(key_columns)
        if column is not None
    ) + ((len(key_columns), -1, None, 0, "BINARY", 0),)
