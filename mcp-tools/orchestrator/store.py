"""SQLite persistence for orchestration records.

The store deliberately keeps scheduling decisions in :mod:`orchestrator.scheduler`.
It owns durable records and the compare-and-swap boundaries around mutations.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .models import (
    AtlasFinalization,
    AtlasOutboxItem,
    AtlasOutboxState,
    CodeTaskAcceptance,
    ExternalBootstrapBatch,
    ExternalBootstrapBatchItem,
    ExternalBootstrapOutboxItem,
    ExternalBootstrapState,
    ExternalDispatchGrant,
    ExternalSourceDescriptor,
    HostOperationReceipt,
    RoleEnvelope,
    RoleEnvelopeDirection,
    RoleRiskItem,
    Task,
    TaskKind,
    TaskState,
    Workflow,
    WorkflowKind,
    WorkflowState,
)

_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
_SQLITE_DELETE_JOURNAL_FORMAT = (1, 1)
_SQLITE_WAL_JOURNAL_FORMAT = (2, 2)
_ATLAS_FINALIZATION_SCHEMA_VERSION = "atlas-finalization/v1"
_ATLAS_FINALIZATION_DOMAIN = "2718lab/orchestrator/atlas-finalization/v1"
_ATLAS_FINALIZATION_COLUMNS = (
    "schema_version",
    "acceptance_id",
    "ingestion_key",
    "payload_hash",
    "continuity_key_hash",
    "view_id",
    "fence_epoch",
    "pointer_version",
    "published_receipt_hash",
    "atlas_receipt_digest",
    "finalization_hash",
    "created_at",
)
_ATLAS_FINALIZATION_REQUIRED_CHECKS = frozenset(
    {
        ("schema_version", "=", f"'{_ATLAS_FINALIZATION_SCHEMA_VERSION}'"),
        ("fence_epoch", ">", "0"),
        ("pointer_version", ">", "0"),
    }
)
_ATLAS_OUTBOX_COLUMNS = (
    "ingestion_key",
    "acceptance_id",
    "payload_json",
    "payload_hash",
    "state",
    "attempt_count",
    "last_error_code",
    "reason_codes_json",
    "created_at",
    "updated_at",
)
_ATLAS_OUTBOX_COLUMN_CONTRACT = (
    ("ingestion_key", "TEXT", 1, 1),
    ("acceptance_id", "TEXT", 1, 0),
    ("payload_json", "TEXT", 1, 0),
    ("payload_hash", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("attempt_count", "INTEGER", 1, 0),
    ("last_error_code", "TEXT", 1, 0),
    ("reason_codes_json", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
)
_ATLAS_FINALIZATION_COLUMN_CONTRACT = (
    ("schema_version", "TEXT", 1, 0),
    ("acceptance_id", "TEXT", 1, 0),
    ("ingestion_key", "TEXT", 1, 0),
    ("payload_hash", "TEXT", 1, 0),
    ("continuity_key_hash", "TEXT", 1, 0),
    ("view_id", "TEXT", 1, 0),
    ("fence_epoch", "INTEGER", 1, 0),
    ("pointer_version", "INTEGER", 1, 0),
    ("published_receipt_hash", "TEXT", 1, 0),
    ("atlas_receipt_digest", "TEXT", 1, 0),
    ("finalization_hash", "TEXT", 1, 1),
    ("created_at", "TEXT", 1, 0),
)
_ATLAS_OUTBOX_IDENTITY = ("acceptance_id", "ingestion_key", "payload_hash")
_SQLITE_WAL_FORMAT_VERSION = 3_007_000
_SQLITE_WAL_INDEX_HEADER_BYTES = 96
_SQLITE_WAL_INDEX_BLOCK_BYTES = 32_768


def _sqlite_wal_sidecars(database: Path) -> tuple[Path, Path]:
    return (
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    )


def _require_absent_sqlite_sidecar(path: Path) -> None:
    try:
        path.stat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise StoreError("orchestrator store is not prepared") from error
    raise StoreError("orchestrator store is not prepared")


def _require_delete_journal_invariant(database: Path) -> None:
    """Prove the primary file has no WAL recovery dependency or sidecars."""
    try:
        with database.open("rb") as source:
            header = source.read(20)
    except OSError as error:
        raise StoreError("orchestrator store is not prepared") from error
    if (
        len(header) != 20
        or header[:16] != _SQLITE_HEADER_MAGIC
        or (header[18], header[19]) != _SQLITE_DELETE_JOURNAL_FORMAT
    ):
        raise StoreError("orchestrator store is not prepared")
    for sidecar in _sqlite_wal_sidecars(database):
        _require_absent_sqlite_sidecar(sidecar)


def _preflight_legacy_physical_state(database: Path) -> None:
    """Read legacy physical state before SQLite can alter a malformed sidecar."""
    if not database.exists():
        return
    try:
        with database.open("rb") as source:
            header = source.read(100)
    except OSError as error:
        raise StoreError("orchestrator store is not prepared") from error
    if len(header) != 100 or header[:16] != _SQLITE_HEADER_MAGIC:
        raise StoreError("orchestrator store is not prepared")
    wal, shm = _sqlite_wal_sidecars(database)
    wal_exists, shm_exists = wal.exists(), shm.exists()
    format_bytes = (header[18], header[19])
    if format_bytes == _SQLITE_DELETE_JOURNAL_FORMAT:
        if wal_exists or shm_exists:
            raise StoreError("orchestrator store is not prepared")
        return
    if format_bytes != _SQLITE_WAL_JOURNAL_FORMAT or wal_exists != shm_exists:
        raise StoreError("orchestrator store is not prepared")
    if not wal_exists:
        return
    try:
        wal_bytes = wal.read_bytes()
        shm_bytes = shm.read_bytes()
    except OSError as error:
        raise StoreError("orchestrator store is not prepared") from error
    _validate_quiescent_wal_pair(header, wal_bytes, shm_bytes)


def _u32(data: bytes, offset: int, *, byteorder: Literal["big", "little"]) -> int:
    return int.from_bytes(data[offset : offset + 4], byteorder=byteorder)


def _sqlite_page_size(header: bytes) -> int:
    page_size = int.from_bytes(header[16:18], byteorder="big")
    if page_size == 1:
        return 65_536
    if page_size < 512 or page_size > 32_768 or page_size & (page_size - 1):
        raise StoreError("orchestrator store is not prepared")
    return page_size


def _wal_checksum(
    data: bytes,
    *,
    byteorder: Literal["big", "little"],
    initial: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """Apply SQLite's documented WAL checksum over aligned 32-bit word pairs."""

    if len(data) % 8:
        raise StoreError("orchestrator store is not prepared")
    first, second = initial
    for offset in range(0, len(data), 8):
        left = _u32(data, offset, byteorder=byteorder)
        right = _u32(data, offset + 4, byteorder=byteorder)
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def _validate_quiescent_wal_pair(
    database_header: bytes, wal_bytes: bytes, shm_bytes: bytes
) -> None:
    """Prove a paired legacy WAL has no unapplied recovery frames before opening it.

    The WAL header, every indexed frame, and both WAL-index header copies must
    agree.  Requiring ``mxFrame == nBackfill`` makes the primary independently
    complete, so a checkpoint cannot be needed merely to recover the v12 rows.
    """

    if len(wal_bytes) < 32 or len(shm_bytes) < _SQLITE_WAL_INDEX_BLOCK_BYTES:
        raise StoreError("orchestrator store is not prepared")
    if len(shm_bytes) % _SQLITE_WAL_INDEX_BLOCK_BYTES:
        raise StoreError("orchestrator store is not prepared")
    magic = _u32(wal_bytes, 0, byteorder="big")
    if magic not in {0x377F0682, 0x377F0683}:
        raise StoreError("orchestrator store is not prepared")
    checksum_byteorder = "little" if magic == 0x377F0682 else "big"
    if _u32(wal_bytes, 4, byteorder="big") != _SQLITE_WAL_FORMAT_VERSION:
        raise StoreError("orchestrator store is not prepared")
    page_size = _sqlite_page_size(database_header)
    if _u32(wal_bytes, 8, byteorder="big") != page_size:
        raise StoreError("orchestrator store is not prepared")
    header_checksum = _wal_checksum(wal_bytes[:24], byteorder=checksum_byteorder)
    if header_checksum != (
        _u32(wal_bytes, 24, byteorder="big"),
        _u32(wal_bytes, 28, byteorder="big"),
    ):
        raise StoreError("orchestrator store is not prepared")

    wal_index = shm_bytes[:_SQLITE_WAL_INDEX_HEADER_BYTES]
    if wal_index[:48] != wal_index[48:96]:
        raise StoreError("orchestrator store is not prepared")
    native_byteorder: Literal["big", "little"] = (
        "little" if sys.byteorder == "little" else "big"
    )
    if (
        _u32(wal_index, 0, byteorder=native_byteorder) != _SQLITE_WAL_FORMAT_VERSION
        or wal_index[4:8] != b"\x00\x00\x00\x00"
        or wal_index[12] != 1
        or wal_index[13] != (1 if checksum_byteorder == "big" else 0)
        or int.from_bytes(wal_index[14:16], byteorder=native_byteorder)
        != (1 if page_size == 65_536 else page_size)
        or wal_index[32:40] != wal_bytes[16:24]
        or _wal_checksum(wal_index[:40], byteorder=native_byteorder)
        != (
            _u32(wal_index, 40, byteorder=native_byteorder),
            _u32(wal_index, 44, byteorder=native_byteorder),
        )
    ):
        raise StoreError("orchestrator store is not prepared")
    frame_count = (len(wal_bytes) - 32) // (24 + page_size)
    if len(wal_bytes) != 32 + frame_count * (24 + page_size):
        raise StoreError("orchestrator store is not prepared")
    max_frame = _u32(wal_index, 16, byteorder=native_byteorder)
    backfill = _u32(shm_bytes, 128, byteorder=native_byteorder)
    if max_frame != backfill or max_frame != frame_count:
        raise StoreError("orchestrator store is not prepared")

    frame_checksum = header_checksum
    for frame_number in range(frame_count):
        frame_offset = 32 + frame_number * (24 + page_size)
        frame_header = wal_bytes[frame_offset : frame_offset + 24]
        frame_data = wal_bytes[frame_offset + 24 : frame_offset + 24 + page_size]
        if (
            frame_header[8:16] != wal_bytes[16:24]
            or _u32(frame_header, 0, byteorder="big") == 0
        ):
            raise StoreError("orchestrator store is not prepared")
        frame_checksum = _wal_checksum(
            frame_header[:8] + frame_data,
            byteorder=checksum_byteorder,
            initial=frame_checksum,
        )
        if frame_checksum != (
            _u32(frame_header, 16, byteorder="big"),
            _u32(frame_header, 20, byteorder="big"),
        ):
            raise StoreError("orchestrator store is not prepared")
    if frame_count and _u32(wal_bytes, 32 + (frame_count - 1) * (24 + page_size) + 4, byteorder="big") == 0:
        raise StoreError("orchestrator store is not prepared")
    if frame_count and frame_checksum != (
        _u32(wal_index, 24, byteorder=native_byteorder),
        _u32(wal_index, 28, byteorder=native_byteorder),
    ):
        raise StoreError("orchestrator store is not prepared")


def _schema_version_from_connection(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT type FROM sqlite_master WHERE name = 'schema_metadata'"
    ).fetchone()
    if row is None:
        return None
    if len(row) != 1 or row[0] != "table":
        raise StoreError("orchestrator store is not prepared")
    rows = connection.execute(
        "SELECT key, value FROM schema_metadata"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != "schema_version":
        raise StoreError("orchestrator store is not prepared")
    try:
        value = int(rows[0][1])
    except (TypeError, ValueError) as error:
        raise StoreError("orchestrator store is not prepared") from error
    if str(value) != rows[0][1]:
        raise StoreError("orchestrator store is not prepared")
    return value


def _main_database_path(connection: sqlite3.Connection) -> Path:
    rows = connection.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if len(row) == 3 and row[1] == "main" and isinstance(row[2], str) and row[2]:
            return Path(row[2])
    raise StoreError("orchestrator store is not prepared")


def _transition_to_delete_journal(connection: sqlite3.Connection, database: Path) -> None:
    """Checkpoint a valid legacy WAL exactly, then make DELETE durable."""
    row = connection.execute("PRAGMA journal_mode").fetchone()
    if row is None or len(row) != 1 or not isinstance(row[0], str):
        raise StoreError("orchestrator store is not prepared")
    mode = row[0].casefold()
    if mode == "wal":
        try:
            with database.open("rb") as source:
                header = source.read(20)
        except OSError as error:
            raise StoreError("orchestrator store is not prepared") from error
        if (
            len(header) != 20
            or header[:16] != _SQLITE_HEADER_MAGIC
            or (header[18], header[19]) != _SQLITE_WAL_JOURNAL_FORMAT
        ):
            raise StoreError("orchestrator store is not prepared")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or tuple(checkpoint) != (0, 0, 0):
            raise StoreError("orchestrator store is not prepared")
    elif mode not in {"delete", "truncate", "persist", "memory", "off"}:
        raise StoreError("orchestrator store is not prepared")
    elif mode == "delete":
        for sidecar in _sqlite_wal_sidecars(database):
            _require_absent_sqlite_sidecar(sidecar)
    result = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    if (
        result is None
        or len(result) != 1
        or not isinstance(result[0], str)
        or result[0].casefold() != "delete"
    ):
        raise StoreError("orchestrator store is not prepared")
    for sidecar in _sqlite_wal_sidecars(database):
        _require_absent_sqlite_sidecar(sidecar)

_ATLAS_OUTBOX_REQUIRED_CHECKS = frozenset(
    {
        ("ingestion_key", "=", "payload_hash"),
        (
            "state",
            "in",
            "(",
            "'pending'",
            ",",
            "'projected'",
            ",",
            "'quarantined'",
            ")",
        ),
        ("attempt_count", "between", "0", "and", "16"),
    }
)


def _sqlite_check_expressions(table_sql: object) -> frozenset[tuple[str, ...]]:
    """Return complete CHECK expressions from one SQLite table declaration."""

    if type(table_sql) is not str:
        raise ValueError("table declaration is not text")
    tokens = _sqlite_schema_tokens(table_sql)
    checks: set[tuple[str, ...]] = set()
    index = 0
    while index < len(tokens):
        if tokens[index] != "check" or index + 1 == len(tokens) or tokens[index + 1] != "(":
            index += 1
            continue
        expression, index = _sqlite_parenthesized_tokens(tokens, index + 1)
        checks.add(_strip_sql_outer_parentheses(expression))
    return frozenset(checks)


def _sqlite_schema_tokens(table_sql: str) -> tuple[str, ...]:
    """Tokenize enough SQLite DDL to distinguish real CHECK clauses from comments."""

    tokens: list[str] = []
    index = 0
    while index < len(table_sql):
        character = table_sql[index]
        if character.isspace():
            index += 1
        elif table_sql.startswith("--", index):
            newline = table_sql.find("\n", index + 2)
            index = len(table_sql) if newline < 0 else newline + 1
        elif table_sql.startswith("/*", index):
            ending = table_sql.find("*/", index + 2)
            if ending < 0:
                raise ValueError("unterminated SQLite comment")
            index = ending + 2
        elif character in "'\"`[":
            token, index = _sqlite_quoted_token(table_sql, index)
            tokens.append(token)
        elif character.isalnum() or character in "_$":
            ending = index + 1
            while ending < len(table_sql) and (
                table_sql[ending].isalnum() or table_sql[ending] in "_$"
            ):
                ending += 1
            tokens.append(table_sql[index:ending].casefold())
            index = ending
        else:
            tokens.append(character)
            index += 1
    return tuple(tokens)


def _sqlite_quoted_token(table_sql: str, index: int) -> tuple[str, int]:
    opening = table_sql[index]
    closing = "]" if opening == "[" else opening
    parts: list[str] = []
    index += 1
    while index < len(table_sql):
        character = table_sql[index]
        if character != closing:
            parts.append(character)
            index += 1
            continue
        if index + 1 < len(table_sql) and table_sql[index + 1] == closing:
            parts.append(closing)
            index += 2
            continue
        value = "".join(parts)
        return (
            f"'{value}'" if opening == "'" else value.casefold(),
            index + 1,
        )
    raise ValueError("unterminated SQLite quoted token")


def _sqlite_parenthesized_tokens(
    tokens: tuple[str, ...], opening_index: int
) -> tuple[tuple[str, ...], int]:
    if opening_index >= len(tokens) or tokens[opening_index] != "(":
        raise ValueError("SQLite CHECK expression is malformed")
    depth = 0
    for index in range(opening_index, len(tokens)):
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
            if depth == 0:
                return tokens[opening_index + 1 : index], index + 1
            if depth < 0:
                break
    raise ValueError("SQLite CHECK expression is unbalanced")


def _strip_sql_outer_parentheses(tokens: tuple[str, ...]) -> tuple[str, ...]:
    while len(tokens) >= 2 and tokens[0] == "(":
        inner, ending = _sqlite_parenthesized_tokens(tokens, 0)
        if ending != len(tokens):
            break
        tokens = inner
    return tokens


def _table_column_contract(
    executor: sqlite3.Connection | sqlite3.Cursor, table_name: str
) -> tuple[tuple[str, str, int, int, int], ...]:
    identifier = table_name.replace('"', '""')
    return tuple(
        (
            str(row["name"]),
            str(row["type"]).casefold(),
            int(row["notnull"]),
            int(row["pk"]),
            int(row["hidden"]),
        )
        for row in executor.execute(f'PRAGMA table_xinfo("{identifier}")')
    )


def _unique_index_contract(
    executor: sqlite3.Connection | sqlite3.Cursor, table_name: str
) -> tuple[tuple[str, ...], ...]:
    identifier = table_name.replace('"', '""')
    unique_indexes: list[tuple[str, ...]] = []
    indexes = executor.execute(f'PRAGMA index_list("{identifier}")').fetchall()
    for index in indexes:
        if not int(index["unique"]) or int(index["partial"]):
            continue
        index_name = str(index["name"]).replace('"', '""')
        unique_indexes.append(
            tuple(
                str(column["name"])
                for column in executor.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
            )
        )
    return tuple(unique_indexes)


def _foreign_key_contract(
    executor: sqlite3.Connection | sqlite3.Cursor, table_name: str
) -> frozenset[tuple[tuple[str, str, str, str, str, str], ...]]:
    identifier = table_name.replace('"', '""')
    groups: dict[int, list[tuple[int, tuple[str, str, str, str, str, str]]]] = {}
    for row in executor.execute(f'PRAGMA foreign_key_list("{identifier}")'):
        groups.setdefault(int(row["id"]), []).append(
            (
                int(row["seq"]),
                (
                    str(row["from"]),
                    str(row["table"]),
                    str(row["to"]),
                    str(row["on_update"]).casefold(),
                    str(row["on_delete"]).casefold(),
                    str(row["match"]).casefold(),
                ),
            )
        )
    return frozenset(
        tuple(value for _, value in sorted(group, key=lambda item: item[0]))
        for group in groups.values()
    )


def _table_sql(
    executor: sqlite3.Connection | sqlite3.Cursor, table_name: str
) -> str:
    row = executor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None or type(row["sql"]) is not str:
        raise StoreError("orchestrator store is not prepared")
    return str(row["sql"])


def _atlas_finalization_trigger_tokens() -> dict[str, tuple[str, ...]]:
    return {
        "atlas_finalizations_no_update": _sqlite_schema_tokens(
            """
            CREATE TRIGGER atlas_finalizations_no_update
            BEFORE UPDATE ON atlas_finalizations
            BEGIN
                SELECT RAISE(ABORT, 'atlas finalization is immutable');
            END
            """
        ),
        "atlas_finalizations_no_delete": _sqlite_schema_tokens(
            """
            CREATE TRIGGER atlas_finalizations_no_delete
            BEFORE DELETE ON atlas_finalizations
            BEGIN
                SELECT RAISE(ABORT, 'atlas finalization is immutable');
            END
            """
        ),
        "atlas_finalizations_require_projected_outbox": _sqlite_schema_tokens(
            """
            CREATE TRIGGER atlas_finalizations_require_projected_outbox
            BEFORE INSERT ON atlas_finalizations
            WHEN NOT EXISTS (
                SELECT 1 FROM atlas_ingestion_outbox AS outbox
                WHERE outbox.acceptance_id = NEW.acceptance_id
                  AND outbox.ingestion_key = NEW.ingestion_key
                  AND outbox.payload_hash = NEW.payload_hash
                  AND outbox.state = 'projected'
            )
            BEGIN
                SELECT RAISE(ABORT, 'atlas finalization requires projected exact outbox');
            END
            """
        ),
    }


class StoreError(RuntimeError):
    """Base error raised by the persistence layer."""


class VersionConflictError(StoreError):
    """Raised when a mutation's expected record version is no longer current."""

    code = "VERSION_CONFLICT"


class StaleLeaseError(StoreError):
    """Raised when a mutation is attempted with a superseded lease epoch."""

    code = "STALE_LEASE"


class LeaseConflictError(StoreError):
    """Raised when another unexpired owner holds a lease."""

    code = "LEASE_HELD"


class WorkflowCancelledError(StoreError):
    """Raised when a mutation targets a cancelled workflow."""

    code = "WORKFLOW_CANCELLED"


class ArtifactConflictError(StoreError):
    """Raised when an artifact hash is reused with different metadata."""

    code = "ARTIFACT_CONFLICT"


class AcceptanceConflictError(StoreError):
    """Raised when one code task is reused with different accepted content."""

    code = "ACCEPTANCE_CONFLICT"


class AcceptanceAuthorizationError(StoreError):
    """Raised when durable task/coordinator authority is not satisfied."""

    code = "ACCEPTANCE_FORBIDDEN"


class AcceptanceEvidenceError(StoreError):
    """Raised when an acceptance has no immutable evidence binding."""

    code = "EVIDENCE_INCOMPLETE"


class AtlasOutboxTransitionError(StoreError):
    """Raised when an outbox item would leave an immutable terminal state."""

    code = "OUTBOX_TERMINAL"


class AtlasOutboxAttemptLimitError(StoreError):
    """Raised when a pending outbox item reaches its bounded retry limit."""

    code = "OUTBOX_ATTEMPTS_EXHAUSTED"


class InvalidTaskStateError(StoreError):
    """Raised when an operation requires a different task state."""

    code = "INVALID_STATE"


class CardHashMismatchError(StoreError):
    """Raised when a task card body does not match its registered content hash."""

    code = "CARD_HASH_MISMATCH"


class PeerForbiddenError(StoreError):
    code = "PEER_FORBIDDEN"


class CapabilityInvalidError(StoreError):
    code = "CAPABILITY_INVALID"


class ArtifactNotOwnedError(StoreError):
    code = "ARTIFACT_NOT_OWNED"


class TTLInvalidError(StoreError):
    code = "TTL_INVALID"


class QuotaExceededError(StoreError):
    code = "QUOTA_EXCEEDED"


class MailboxForbiddenError(StoreError):
    code = "MAILBOX_FORBIDDEN"


class MessageExpiredError(StoreError):
    code = "MESSAGE_EXPIRED"


class CorrelationConflictError(StoreError):
    code = "CORRELATION_CONFLICT"


class RoleEnvelopeInvalidError(StoreError):
    code = "ROLE_ENVELOPE_INVALID"


class RoleEnvelopeForbiddenError(StoreError):
    code = "ROLE_ENVELOPE_FORBIDDEN"


class HostOperationConflictError(StoreError):
    code = "HOST_OPERATION_CONFLICT"


class HostTargetInvalidError(StoreError):
    code = "HOST_TARGET_INVALID"


class ExternalBootstrapConflictError(StoreError):
    """Raised when a hash-bound external bootstrap identity is reused differently."""

    code = "EXTERNAL_BOOTSTRAP_CONFLICT"


class ExternalDispatchGrantError(StoreError):
    """Raised when a one-shot external dispatch grant is expired or not bound."""

    code = "EXTERNAL_DISPATCH_GRANT_FORBIDDEN"


class StrictIndexError(StoreError):
    """Raised when a strict task violates an index workflow gate."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Lease:
    task_id: str
    owner: str
    epoch: int
    expires_at: str
    heartbeat_at: str
    host_target: str | None = None


@dataclass(frozen=True)
class Event:
    sequence: int
    workflow_id: str
    task_id: str | None
    event_type: str
    redacted_payload: str
    payload_hash: str
    created_at: str


@dataclass(frozen=True)
class Artifact:
    kind: str
    content_hash: str
    safe_path: str
    size: int
    redaction_version: str
    created_at: str


@dataclass(frozen=True)
class Peer:
    task_id: str
    relationship: str
    capability: str


@dataclass(frozen=True)
class Message:
    delivery_id: str
    sequence: int
    workflow_id: str
    sender_task_id: str
    recipient_task_id: str
    correlation_id: str
    artifact_hash: str
    redacted_metadata: tuple[tuple[str, str], ...]
    created_at: str
    expires_at: str
    delivery_state: str
    acknowledged_at: str | None


@dataclass(frozen=True)
class TaskContextRequirements:
    direct_contract_hashes: tuple[str, ...]
    required_evidence: tuple[str, ...]


@dataclass(frozen=True)
class IndexBinding:
    task_id: str
    workspace_id: str
    input_snapshot_id: str
    output_snapshot_id: str
    task_node_ids: tuple[str, ...]
    contract_node_ids: tuple[str, ...]
    checkpoint_id: str
    indexed_diff_hash: str
    fallback_count: int


@dataclass(frozen=True)
class TaskAcceptanceEvidence:
    """Bound output evidence identifiers selected from one strict task."""

    output_query_trace_id: str
    verification_artifact_hashes: tuple[str, ...]


@dataclass(frozen=True)
class CodeTaskEvidenceBinding:
    """Canonical, privacy-bounded evidence retained with one acceptance."""

    schema_version: str
    workflow_id: str
    code_task_id: str
    code_task_version: int
    input_snapshot_id: str
    output_snapshot_id: str
    indexed_diff_hash: str
    checkpoint_id: str
    checkpoint_hash: str
    output_query_trace_id: str
    verification_artifact_hashes: tuple[str, ...]
    execution_receipt_ids: tuple[str, ...]
    evidence_binding_hash: str


@dataclass(frozen=True)
class CodeTaskReceiptAttestation:
    """Producer-owned content address binding receipts to one code-task output."""

    schema_version: str
    workflow_id: str
    code_task_id: str
    code_task_version: int
    input_snapshot_id: str
    output_snapshot_id: str
    workspace_hash: str
    execution_receipt_ids: tuple[str, ...]
    attestation_hash: str


@dataclass(frozen=True)
class AcceptedCodeTaskEvidence:
    """Immutable facts needed to rebuild one accepted Atlas projection."""

    acceptance: CodeTaskAcceptance
    task: Task
    index_binding: IndexBinding
    task_evidence: TaskAcceptanceEvidence
    evidence_binding: CodeTaskEvidenceBinding
    receipt_attestation: CodeTaskReceiptAttestation


class SQLiteStore:
    """A small transactional store backed by a single SQLite database file."""

    _SCHEMA_VERSION = 13
    _MAX_MESSAGE_TTL_SECONDS = 86_400
    _MAX_INBOX_LIMIT = 100
    _MAX_HOST_TARGET_LENGTH = 256
    _MAX_ATLAS_OUTBOX_ATTEMPTS = 16
    _MAX_ATLAS_OUTBOX_LIMIT = 100
    _MAX_SAFE_ACCEPTANCE_IDENTIFIER_LENGTH = 256
    _MAX_SAFE_OUTBOX_CODE_LENGTH = 64
    _MAX_SAFE_OUTBOX_REASON_COUNT = 8
    _MAX_CODE_TASK_EVIDENCE_ITEMS = 32
    _MAX_CODE_TASK_ACCEPTANCE_LIST = 100
    _MAX_ROLE_REFERENCE_COUNT = 32
    _MAX_ROLE_RISK_ITEMS = 8
    _MAX_ROLE_TOKEN_LENGTH = 256
    _MAX_ROLE_CORRELATION_LENGTH = 128
    _ROLE_ENVELOPE_SCHEMA_VERSION = "durable-role-envelope/v1"
    _HOST_ARCHIVE_RECEIPT_SCHEMA_VERSION = "host-archive-receipt/v1"
    _EVIDENCE_BINDING_SCHEMA_VERSION = "acceptance-evidence-binding/v1"
    _EVIDENCE_BINDING_EVENT_TYPE = "code_task_evidence_binding"
    _RECEIPT_ATTESTATION_SCHEMA_VERSION = "code-task-receipt-attestation/v1"
    _HOST_TARGET_PATTERN = re.compile(r"/root(?:/[a-z0-9_]+)*\Z")
    _SAFE_ACCEPTANCE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
    _SHA256_IDENTIFIER_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
    _SAFE_OUTBOX_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")
    _SAFE_ROLE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
    _SAFE_RISK_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")
    _ROLE_RISK_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
    _EXTERNAL_BOOTSTRAP_AVAILABILITY = "HOST_API_UNAVAILABLE"
    _MAX_EXTERNAL_BOOTSTRAP_BATCH_ITEMS = 9

    def __init__(self, database: str | Path) -> None:
        self._database = Path(database)
        _preflight_legacy_physical_state(self._database)
        self._preflight_legacy_v12_schema_readonly()
        connection = sqlite3.connect(str(self._database), isolation_level=None)
        self._connection = connection
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._prepare_legacy_journal()
            self._create_schema()
            self.validate_prepared_connection(connection)
            _require_delete_journal_invariant(self._database)
        except BaseException:
            connection.close()
            self._connection = None  # type: ignore[assignment]
            raise

    def _preflight_legacy_v12_schema_readonly(self) -> None:
        """Reject malformed v12 before a writable SQLite open can touch WAL state."""

        if not self._database.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self._database.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            if _schema_version_from_connection(connection) == 12:
                self._validate_schema_metadata_layout(connection)
                self._validate_v12_required_shape(connection)
        except (OSError, sqlite3.DatabaseError, ValueError) as error:
            raise StoreError("orchestrator store is not prepared") from error
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def from_prepared_connection(cls, connection: sqlite3.Connection) -> SQLiteStore:
        """Bind a validated, invocation-owned connection without creating schema."""

        try:
            connection.execute("PRAGMA foreign_keys = ON")
            foreign_keys_enabled = bool(
                connection.execute("PRAGMA foreign_keys").fetchone()[0]
            )
        except (IndexError, TypeError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator store is not prepared") from error
        if not foreign_keys_enabled:
            raise StoreError("orchestrator store is not prepared")
        cls.validate_prepared_connection(connection)
        _require_delete_journal_invariant(_main_database_path(connection))
        store = cls.__new__(cls)
        store._connection = connection
        store._database = _main_database_path(connection)
        connection.row_factory = sqlite3.Row
        return store

    def _prepare_legacy_journal(self) -> None:
        """Fail closed before an on-disk v12 database is changed to DELETE."""
        connection = self._connection
        if connection is None:
            raise StoreError("orchestrator store is not prepared")
        try:
            version = _schema_version_from_connection(connection)
            if version is None:
                _transition_to_delete_journal(connection, self._database)
                return
            if version == self._SCHEMA_VERSION:
                _require_delete_journal_invariant(self._database)
                return
            if not 1 <= version < self._SCHEMA_VERSION:
                raise StoreError("orchestrator store is not prepared")
            if version == 12:
                self._validate_v12_schema_before_journal_transition()
            _transition_to_delete_journal(connection, self._database)
        except (sqlite3.DatabaseError, OSError, ValueError) as error:
            raise StoreError("orchestrator store is not prepared") from error

    def _validate_v12_schema_before_journal_transition(self) -> None:
        """Prove a legacy v12 primary is complete before touching journal state."""
        connection = self._connection
        if connection is None:
            raise StoreError("orchestrator store is not prepared")
        try:
            self._validate_schema_metadata_layout(connection)
            row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or str(row["value"]) != "12":
                raise StoreError("orchestrator store is not prepared")
            self._validate_v12_required_shape(connection)
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator store is not prepared") from error

    @classmethod
    def _schema_metadata_layout(
        cls, executor: sqlite3.Connection | sqlite3.Cursor
    ) -> tuple[dict[str, sqlite3.Row], tuple[str, ...]]:
        try:
            columns = {
                str(row["name"]): row
                for row in executor.execute("PRAGMA table_xinfo(schema_metadata)")
            }
            primary_key = tuple(
                str(row["name"])
                for row in sorted(columns.values(), key=lambda row: int(row["pk"]))
                if int(row["pk"])
            )
        except (IndexError, OSError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator schema is corrupt") from error
        return columns, primary_key

    @classmethod
    def _validate_schema_metadata_layout(
        cls, executor: sqlite3.Connection | sqlite3.Cursor
    ) -> None:
        columns, primary_key = cls._schema_metadata_layout(executor)
        if (
            set(columns) != {"key", "value"}
            or str(columns["key"]["type"]).casefold() != "text"
            or str(columns["value"]["type"]).casefold() != "text"
            or primary_key != ("key",)
            or int(columns["key"]["hidden"]) != 0
            or int(columns["value"]["hidden"]) != 0
            or not int(columns["key"]["notnull"])
            or not int(columns["value"]["notnull"])
        ):
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _migrate_schema_metadata_key_not_null(
        cls,
        cursor: sqlite3.Cursor,
        *,
        fresh_database: bool,
    ) -> None:
        """Rebuild only a complete legacy metadata table with non-nullable contents."""

        columns, primary_key = cls._schema_metadata_layout(cursor)
        if (
            set(columns) != {"key", "value"}
            or str(columns["key"]["type"]).casefold() != "text"
            or str(columns["value"]["type"]).casefold() != "text"
            or primary_key != ("key",)
            or int(columns["key"]["hidden"]) != 0
            or int(columns["value"]["hidden"]) != 0
            or not int(columns["value"]["notnull"])
        ):
            raise StoreError("orchestrator store is not prepared")
        try:
            metadata_rows = cursor.execute(
                "SELECT key, value FROM schema_metadata"
            ).fetchall()
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator store is not prepared") from error
        if int(columns["key"]["notnull"]):
            if fresh_database and not metadata_rows:
                return
            if (
                len(metadata_rows) != 1
                or type(metadata_rows[0]["key"]) is not str
                or metadata_rows[0]["key"] != "schema_version"
                or type(metadata_rows[0]["value"]) is not str
                or metadata_rows[0]["value"]
                not in {"12", str(cls._SCHEMA_VERSION)}
            ):
                raise StoreError("orchestrator store is not prepared")
            return
        try:
            source_version = (
                None
                if (
                    len(metadata_rows) != 1
                    or type(metadata_rows[0]["key"]) is not str
                    or metadata_rows[0]["key"] != "schema_version"
                    or type(metadata_rows[0]["value"]) is not str
                )
                else int(metadata_rows[0]["value"])
            )
        except (TypeError, ValueError) as error:
            raise StoreError("orchestrator store is not prepared") from error
        if (
            source_version is None
            or not 1 <= source_version < cls._SCHEMA_VERSION
            or str(source_version) != metadata_rows[0]["value"]
        ):
            raise StoreError("orchestrator store is not prepared")
        try:
            cursor.execute(
                """
                CREATE TABLE schema_metadata_v12 (
                    key TEXT NOT NULL PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO schema_metadata_v12 (key, value)
                SELECT key, value FROM schema_metadata
                """
            )
            cursor.execute("DROP TABLE schema_metadata")
            cursor.execute(
                "ALTER TABLE schema_metadata_v12 RENAME TO schema_metadata"
            )
        except sqlite3.IntegrityError as error:
            raise StoreError("orchestrator store is not prepared") from error
        except sqlite3.DatabaseError as error:
            raise StoreError("orchestrator schema is corrupt") from error

    @classmethod
    def validate_prepared_connection(cls, connection: sqlite3.Connection) -> None:
        """Reject an absent, stale, or incomplete store before runtime use."""

        required_tables = {
            "schema_metadata",
            "workflows",
            "tasks",
            "code_task_acceptances",
            "code_task_receipt_attestations",
            "code_task_receipt_owners",
            "atlas_ingestion_outbox",
            "atlas_finalizations",
            "task_dependencies",
            "lease_epochs",
            "leases",
            "events",
            "artifacts",
            "task_inputs",
            "artifact_owners",
            "task_cards",
            "task_contract_subscriptions",
            "task_required_evidence",
            "task_index_bindings",
            "task_index_query_receipts",
            "task_index_verification_artifacts",
            "task_index_binding_events",
            "peer_capabilities",
            "messages",
            "role_envelopes",
            "host_operation_receipts",
            "external_bootstrap_descriptors",
            "external_bootstrap_batches",
            "external_bootstrap_batch_items",
            "external_bootstrap_outbox",
            "external_dispatch_grants",
            "external_dispatch_grant_bindings",
            "external_bootstrap_batch_commitments",
            "external_dispatch_grant_commitments",
        }
        connection.row_factory = sqlite3.Row
        try:
            cls._validate_schema_metadata_layout(connection)
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            metadata_rows = connection.execute(
                "SELECT key, value FROM schema_metadata"
            ).fetchall()
            has_current_schema_metadata = (
                len(metadata_rows) == 1
                and type(metadata_rows[0]["key"]) is str
                and metadata_rows[0]["key"] == "schema_version"
                and type(metadata_rows[0]["value"]) is str
                and metadata_rows[0]["value"] == str(cls._SCHEMA_VERSION)
            )
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).casefold()
            cls._validate_atlas_outbox_shape(
                connection,
                require_finalization_identity=True,
            )
            cls._validate_atlas_finalization_shape(connection)
            cls._validate_atlas_finalization_rows(connection)
            foreign_key_violation = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone()
        except (IndexError, OSError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator schema is corrupt") from error
        if (
            not required_tables.issubset(tables)
            or not has_current_schema_metadata
            or journal_mode != "delete"
            or foreign_key_violation is not None
        ):
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _validate_atlas_outbox_shape(
        cls,
        connection: sqlite3.Connection | sqlite3.Cursor,
        *,
        require_finalization_identity: bool,
    ) -> None:
        """Prove every outbox identity invariant before it can participate in I/O."""

        expected_columns = tuple(
            (name, column_type.casefold(), not_null, primary_key, 0)
            for name, column_type, not_null, primary_key in _ATLAS_OUTBOX_COLUMN_CONTRACT
        )
        legacy_unique = {
            ("ingestion_key",),
            ("acceptance_id",),
            ("payload_hash",),
        }
        expected_unique = legacy_unique | {_ATLAS_OUTBOX_IDENTITY}
        expected_foreign_keys = frozenset(
            {
                (
                    (
                        "acceptance_id",
                        "code_task_acceptances",
                        "acceptance_id",
                        "no action",
                        "no action",
                        "none",
                    ),
                )
            }
        )
        unique_indexes = set(_unique_index_contract(connection, "atlas_ingestion_outbox"))
        if (
            _table_column_contract(connection, "atlas_ingestion_outbox")
            != expected_columns
            or (
                unique_indexes != expected_unique
                if require_finalization_identity
                else unique_indexes not in (legacy_unique, expected_unique)
            )
            or _foreign_key_contract(connection, "atlas_ingestion_outbox")
            != expected_foreign_keys
            or _sqlite_check_expressions(_table_sql(connection, "atlas_ingestion_outbox"))
            != _ATLAS_OUTBOX_REQUIRED_CHECKS
        ):
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _validate_v12_required_shape(
        cls, connection: sqlite3.Connection | sqlite3.Cursor
    ) -> None:
        """Reject malformed v12 contents before its WAL checkpoint can mutate it."""
        required_tables = {
            "schema_metadata",
            "workflows",
            "tasks",
            "code_task_acceptances",
            "code_task_receipt_attestations",
            "code_task_receipt_owners",
            "atlas_ingestion_outbox",
            "task_dependencies",
            "lease_epochs",
            "leases",
            "events",
            "artifacts",
            "task_inputs",
            "artifact_owners",
            "task_cards",
            "task_contract_subscriptions",
            "task_required_evidence",
            "task_index_bindings",
            "task_index_query_receipts",
            "task_index_verification_artifacts",
            "task_index_binding_events",
            "peer_capabilities",
            "messages",
            "role_envelopes",
            "host_operation_receipts",
            "external_bootstrap_descriptors",
            "external_bootstrap_batches",
            "external_bootstrap_batch_items",
            "external_bootstrap_outbox",
            "external_dispatch_grants",
            "external_dispatch_grant_bindings",
            "external_bootstrap_batch_commitments",
            "external_dispatch_grant_commitments",
        }
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required_tables.issubset(tables):
            raise StoreError("orchestrator store is not prepared")
        cls._validate_atlas_outbox_shape(
            connection,
            require_finalization_identity=False,
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _validate_atlas_finalization_shape(
        cls, connection: sqlite3.Connection | sqlite3.Cursor
    ) -> None:
        """Verify the immutable certificate table and its exact anchors."""
        expected_columns = tuple(
            (name, column_type.casefold(), not_null, primary_key, 0)
            for name, column_type, not_null, primary_key
            in _ATLAS_FINALIZATION_COLUMN_CONTRACT
        )
        expected_foreign_keys = frozenset(
            {
                (
                    (
                        "acceptance_id",
                        "code_task_acceptances",
                        "acceptance_id",
                        "no action",
                        "no action",
                        "none",
                    ),
                ),
                (
                    (
                        "ingestion_key",
                        "atlas_ingestion_outbox",
                        "ingestion_key",
                        "no action",
                        "no action",
                        "none",
                    ),
                ),
                tuple(
                    (
                        field,
                        "atlas_ingestion_outbox",
                        field,
                        "no action",
                        "no action",
                        "none",
                    )
                    for field in _ATLAS_OUTBOX_IDENTITY
                ),
            }
        )
        trigger_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'atlas_finalizations'"
        ).fetchall()
        trigger_contract = {
            str(row["name"]): _sqlite_schema_tokens(str(row["sql"]))
            for row in trigger_rows
            if type(row["sql"]) is str
        }
        if (
            _table_column_contract(connection, "atlas_finalizations") != expected_columns
            or set(_unique_index_contract(connection, "atlas_finalizations"))
            != {("finalization_hash",), ("acceptance_id",), ("ingestion_key",)}
            or _foreign_key_contract(connection, "atlas_finalizations")
            != expected_foreign_keys
            or _sqlite_check_expressions(_table_sql(connection, "atlas_finalizations"))
            != _ATLAS_FINALIZATION_REQUIRED_CHECKS
            or trigger_contract != _atlas_finalization_trigger_tokens()
        ):
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _migrate_atlas_finalization_binding(
        cls, cursor: sqlite3.Cursor
    ) -> None:
        """Upgrade only the previous v13 certificate layout to exact outbox binding."""

        try:
            cls._validate_atlas_finalization_shape(cursor)
            return
        except StoreError:
            pass
        expected_columns = tuple(
            (name, column_type.casefold(), not_null, primary_key, 0)
            for name, column_type, not_null, primary_key
            in _ATLAS_FINALIZATION_COLUMN_CONTRACT
        )
        expected_previous_foreign_keys = frozenset(
            {
                (
                    (
                        "acceptance_id",
                        "code_task_acceptances",
                        "acceptance_id",
                        "no action",
                        "no action",
                        "none",
                    ),
                ),
                (
                    (
                        "ingestion_key",
                        "atlas_ingestion_outbox",
                        "ingestion_key",
                        "no action",
                        "no action",
                        "none",
                    ),
                ),
            }
        )
        trigger_contract = {
            str(row["name"]): _sqlite_schema_tokens(str(row["sql"]))
            for row in cursor.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'atlas_finalizations'"
            )
            if type(row["sql"]) is str
        }
        expected_triggers = {
            name: tokens
            for name, tokens in _atlas_finalization_trigger_tokens().items()
            if name
            in {
                "atlas_finalizations_no_update",
                "atlas_finalizations_no_delete",
            }
        }
        if (
            _table_column_contract(cursor, "atlas_finalizations") != expected_columns
            or set(_unique_index_contract(cursor, "atlas_finalizations"))
            != {("finalization_hash",), ("acceptance_id",), ("ingestion_key",)}
            or _foreign_key_contract(cursor, "atlas_finalizations")
            != expected_previous_foreign_keys
            or _sqlite_check_expressions(_table_sql(cursor, "atlas_finalizations"))
            != _ATLAS_FINALIZATION_REQUIRED_CHECKS
            or (
                trigger_contract != expected_triggers
                and trigger_contract != _atlas_finalization_trigger_tokens()
            )
        ):
            raise StoreError("orchestrator store is not prepared")
        cls._validate_atlas_finalization_rows(cursor)
        try:
            cursor.execute(
                """
                CREATE TABLE atlas_finalizations_v13 (
                    schema_version TEXT NOT NULL
                        CHECK (schema_version = 'atlas-finalization/v1'),
                    acceptance_id TEXT NOT NULL UNIQUE
                        REFERENCES code_task_acceptances(acceptance_id),
                    ingestion_key TEXT NOT NULL UNIQUE
                        REFERENCES atlas_ingestion_outbox(ingestion_key),
                    payload_hash TEXT NOT NULL,
                    continuity_key_hash TEXT NOT NULL,
                    view_id TEXT NOT NULL,
                    fence_epoch INTEGER NOT NULL CHECK (fence_epoch > 0),
                    pointer_version INTEGER NOT NULL CHECK (pointer_version > 0),
                    published_receipt_hash TEXT NOT NULL,
                    atlas_receipt_digest TEXT NOT NULL,
                    finalization_hash TEXT NOT NULL PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (acceptance_id, ingestion_key, payload_hash)
                        REFERENCES atlas_ingestion_outbox(
                            acceptance_id, ingestion_key, payload_hash
                        )
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO atlas_finalizations_v13 (
                    schema_version, acceptance_id, ingestion_key, payload_hash,
                    continuity_key_hash, view_id, fence_epoch, pointer_version,
                    published_receipt_hash, atlas_receipt_digest, finalization_hash,
                    created_at
                )
                SELECT schema_version, acceptance_id, ingestion_key, payload_hash,
                       continuity_key_hash, view_id, fence_epoch, pointer_version,
                       published_receipt_hash, atlas_receipt_digest, finalization_hash,
                       created_at
                FROM atlas_finalizations
                """
            )
            cursor.execute("DROP TABLE atlas_finalizations")
            cursor.execute(
                "ALTER TABLE atlas_finalizations_v13 RENAME TO atlas_finalizations"
            )
            _execute_schema_statements(
                cursor,
                """
                CREATE TRIGGER atlas_finalizations_no_update
                BEFORE UPDATE ON atlas_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'atlas finalization is immutable');
                END;
                CREATE TRIGGER atlas_finalizations_no_delete
                BEFORE DELETE ON atlas_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'atlas finalization is immutable');
                END;
                CREATE TRIGGER atlas_finalizations_require_projected_outbox
                BEFORE INSERT ON atlas_finalizations
                WHEN NOT EXISTS (
                    SELECT 1 FROM atlas_ingestion_outbox AS outbox
                    WHERE outbox.acceptance_id = NEW.acceptance_id
                      AND outbox.ingestion_key = NEW.ingestion_key
                      AND outbox.payload_hash = NEW.payload_hash
                      AND outbox.state = 'projected'
                )
                BEGIN
                    SELECT RAISE(
                        ABORT, 'atlas finalization requires projected exact outbox'
                    );
                END;
                """,
            )
        except sqlite3.DatabaseError as error:
            raise StoreError("orchestrator schema is corrupt") from error

    @classmethod
    def _validate_atlas_finalization_rows(
        cls, connection: sqlite3.Connection | sqlite3.Cursor
    ) -> None:
        """Revalidate persisted certificates against their exact projected outbox."""

        rows = connection.execute(
            """
            SELECT finalization.*
            FROM atlas_finalizations AS finalization
            LEFT JOIN atlas_ingestion_outbox AS outbox
                ON outbox.acceptance_id = finalization.acceptance_id
               AND outbox.ingestion_key = finalization.ingestion_key
               AND outbox.payload_hash = finalization.payload_hash
               AND outbox.state = ?
            WHERE outbox.ingestion_key IS NULL
            """,
            (AtlasOutboxState.PROJECTED.value,),
        ).fetchall()
        if rows:
            raise StoreError("orchestrator finalization identity mismatch")
        for row in connection.execute("SELECT * FROM atlas_finalizations"):
            try:
                cls._validate_atlas_finalization(cls._atlas_finalization_from_row(row))
            except (KeyError, TypeError, ValueError) as error:
                raise StoreError("orchestrator finalization is not prepared") from error

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def index_retention_references(self) -> tuple[str, ...]:
        """Return every durable orchestrator reference to an index snapshot."""

        return self._index_retention_references(self._connection.cursor())

    @contextmanager
    def index_retention_fence(self) -> Iterator[tuple[str, ...]]:
        """Fence orchestrator writers while a cross-database release is applied."""

        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield self._index_retention_references(cursor)
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.rollback()

    @staticmethod
    def _index_retention_references(cursor: sqlite3.Cursor) -> tuple[str, ...]:
        references: set[str] = set()
        tables = tuple(
            str(row["name"])
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        for table in tables:
            escaped_table = table.replace('"', '""')
            columns = tuple(
                str(row["name"])
                for row in cursor.execute(f'PRAGMA table_info("{escaped_table}")')
                if str(row["name"]) == "snapshot_id"
                or str(row["name"]).endswith("_snapshot_id")
            )
            for column in columns:
                escaped_column = column.replace('"', '""')
                rows = cursor.execute(
                    f'SELECT DISTINCT "{escaped_column}" AS snapshot_id '
                    f'FROM "{escaped_table}" '
                    f'WHERE "{escaped_column}" IS NOT NULL'
                )
                references.update(
                    str(row["snapshot_id"])
                    for row in rows
                    if isinstance(row["snapshot_id"], str) and row["snapshot_id"]
                )
        return tuple(sorted(references))

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM schema_metadata WHERE key = ?", ("schema_version",)
        ).fetchone()
        return int(row["value"])

    def journal_mode(self) -> str:
        return str(
            self._connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()

    def foreign_keys_enabled(self) -> bool:
        return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def admit_external_bootstrap(
        self,
        descriptor: ExternalSourceDescriptor,
        batch: ExternalBootstrapBatch,
        grant: ExternalDispatchGrant,
        *,
        now: str | None = None,
    ) -> tuple[
        ExternalSourceDescriptor,
        ExternalBootstrapBatch,
        ExternalBootstrapOutboxItem,
        ExternalDispatchGrant,
    ]:
        """Atomically retain one hash-only bootstrap descriptor, outbox and grant.

        This repository-only boundary deliberately creates no Host work and leaves
        every durable record pending with ``HOST_API_UNAVAILABLE``.
        """
        created_at = _utc_timestamp(now) if now is not None else _utc_now()
        batch = replace(batch, expires_at=_utc_timestamp(batch.expires_at))
        grant = replace(grant, expires_at=_utc_timestamp(grant.expires_at))
        self._validate_external_bootstrap_records(descriptor, batch, grant)
        descriptor_payload = _canonical_payload_json(
            self._external_descriptor_payload(descriptor)
        )
        batch_payload = _canonical_payload_json(self._external_batch_payload(batch))
        grant_payload = _canonical_payload_json(self._external_grant_payload(grant))
        descriptor_payload_hash = _payload_hash(descriptor_payload)
        batch_payload_hash = _payload_hash(batch_payload)
        grant_payload_hash = _payload_hash(grant_payload)
        outbox_payload = _canonical_payload_json(
            {
                "availability": self._EXTERNAL_BOOTSTRAP_AVAILABILITY,
                "batch_hash": batch.batch_hash,
                "descriptor_hash": descriptor.descriptor_hash,
                "state": ExternalBootstrapState.PENDING.value,
            }
        )
        with self._transaction() as cursor:
            batch_exists = cursor.execute(
                "SELECT 1 FROM external_bootstrap_batches WHERE batch_hash = ?",
                (batch.batch_hash,),
            ).fetchone() is not None
            grant_exists = cursor.execute(
                "SELECT 1 FROM external_dispatch_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone() is not None
            self._require_external_payload(
                cursor,
                "external_bootstrap_descriptors",
                "descriptor_hash",
                descriptor.descriptor_hash,
                descriptor_payload,
            )
            self._require_external_payload(
                cursor,
                "external_bootstrap_batches",
                "batch_hash",
                batch.batch_hash,
                batch_payload,
            )
            self._require_external_payload(
                cursor,
                "external_dispatch_grants",
                "grant_id",
                grant.grant_id,
                grant_payload,
            )
            existing_idempotency = cursor.execute(
                "SELECT batch_hash FROM external_bootstrap_batches WHERE idempotency_key = ?",
                (batch.idempotency_key,),
            ).fetchone()
            if (
                existing_idempotency is not None
                and str(existing_idempotency["batch_hash"]) != batch.batch_hash
            ):
                raise ExternalBootstrapConflictError("bootstrap idempotency binding conflicts")
            existing_grant_binding = cursor.execute(
                """
                SELECT grant_id FROM external_dispatch_grants
                WHERE descriptor_hash = ? AND batch_hash = ? AND assignment_hash = ?
                """,
                (
                    grant.descriptor_hash,
                    grant.batch_hash,
                    grant.assignment_hash,
                ),
            ).fetchone()
            if (
                existing_grant_binding is not None
                and str(existing_grant_binding["grant_id"]) != grant.grant_id
            ):
                raise ExternalBootstrapConflictError("external dispatch grant binding conflicts")
            existing_composite_binding = cursor.execute(
                "SELECT * FROM external_dispatch_grant_bindings WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()
            if existing_composite_binding is not None and any(
                str(existing_composite_binding[field]) != value
                for field, value in (
                    ("descriptor_hash", grant.descriptor_hash),
                    ("batch_hash", grant.batch_hash),
                    ("assignment_hash", grant.assignment_hash),
                )
            ):
                raise ExternalBootstrapConflictError(
                    "external dispatch grant composite binding conflicts"
                )
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_bootstrap_descriptors
                    (descriptor_hash, payload_json, payload_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    descriptor.descriptor_hash,
                    descriptor_payload,
                    descriptor_payload_hash,
                    created_at,
                ),
            )
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_bootstrap_batches
                    (batch_hash, descriptor_hash, idempotency_key, payload_json,
                     payload_hash, state, availability, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_hash,
                    batch.descriptor_hash,
                    batch.idempotency_key,
                    batch_payload,
                    batch_payload_hash,
                    batch.state.value,
                    batch.availability,
                    batch.expires_at,
                    created_at,
                ),
            )
            for item in batch.items:
                item_payload = _canonical_payload_json(self._external_batch_item_payload(item))
                row = cursor.execute(
                    """
                    SELECT payload_json FROM external_bootstrap_batch_items
                    WHERE batch_hash = ? AND item_index = ?
                    """,
                    (batch.batch_hash, item.item_index),
                ).fetchone()
                if row is not None and str(row["payload_json"]) != item_payload:
                    raise ExternalBootstrapConflictError("bootstrap batch item conflicts")
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO external_bootstrap_batch_items
                        (batch_hash, item_index, assignment_hash, payload_json, payload_hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        batch.batch_hash,
                        item.item_index,
                        item.assignment_hash,
                        item_payload,
                        _payload_hash(item_payload),
                    ),
                )
            self._require_external_payload(
                cursor,
                "external_bootstrap_outbox",
                "batch_hash",
                batch.batch_hash,
                outbox_payload,
            )
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_bootstrap_outbox
                    (batch_hash, descriptor_hash, payload_json, payload_hash, state,
                     availability, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_hash,
                    descriptor.descriptor_hash,
                    outbox_payload,
                    _payload_hash(outbox_payload),
                    ExternalBootstrapState.PENDING.value,
                    self._EXTERNAL_BOOTSTRAP_AVAILABILITY,
                    created_at,
                ),
            )
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_dispatch_grants
                    (grant_id, descriptor_hash, batch_hash, assignment_hash, payload_json,
                     payload_hash, state, availability, expires_at, consumed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    grant.grant_id,
                    grant.descriptor_hash,
                    grant.batch_hash,
                    grant.assignment_hash,
                    grant_payload,
                    grant_payload_hash,
                    grant.state.value,
                    grant.availability,
                    grant.expires_at,
                    created_at,
                ),
            )
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_dispatch_grant_bindings
                    (grant_id, descriptor_hash, batch_hash, assignment_hash)
                VALUES (?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    grant.descriptor_hash,
                    grant.batch_hash,
                    grant.assignment_hash,
                ),
            )
            self._insert_or_require_external_commitment(
                cursor,
                "external_bootstrap_batch_commitments",
                {
                    "batch_hash": batch.batch_hash,
                    "descriptor_hash": descriptor.descriptor_hash,
                    "descriptor_payload_hash": descriptor_payload_hash,
                    "batch_payload_hash": batch_payload_hash,
                },
                parent_exists=batch_exists,
            )
            self._insert_or_require_external_commitment(
                cursor,
                "external_dispatch_grant_commitments",
                {
                    "grant_id": grant.grant_id,
                    "descriptor_hash": descriptor.descriptor_hash,
                    "batch_hash": batch.batch_hash,
                    "assignment_hash": grant.assignment_hash,
                    "descriptor_payload_hash": descriptor_payload_hash,
                    "batch_payload_hash": batch_payload_hash,
                    "grant_payload_hash": grant_payload_hash,
                },
                parent_exists=grant_exists,
            )
            outbox_row = cursor.execute(
                "SELECT * FROM external_bootstrap_outbox WHERE batch_hash = ?",
                (batch.batch_hash,),
            ).fetchone()
            grant_row = cursor.execute(
                "SELECT * FROM external_dispatch_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()
            self._validate_external_grant_binding_at_read(grant_row, cursor=cursor)
        return descriptor, batch, self._external_outbox_from_row(outbox_row), self._external_grant_from_row(grant_row)

    def get_external_dispatch_grant(self, grant_id: str) -> ExternalDispatchGrant | None:
        row = self._connection.execute(
            "SELECT * FROM external_dispatch_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            return None
        self._validate_external_grant_binding_at_read(row)
        return self._external_grant_from_row(row)

    def consume_external_dispatch_grant(
        self,
        grant_id: str,
        *,
        descriptor_hash: str,
        batch_hash: str,
        assignment_hash: str,
        now: str | None = None,
    ) -> ExternalDispatchGrant:
        """Atomically consume a grant once after exact hash binding verification."""
        consumed_at = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE external_dispatch_grants
                SET consumed_at = ?
                WHERE grant_id = ? AND descriptor_hash = ? AND batch_hash = ?
                  AND assignment_hash = ? AND consumed_at IS NULL AND expires_at > ?
                  AND state = ? AND availability = ?
                  AND EXISTS (
                      SELECT 1 FROM external_dispatch_grant_bindings AS binding
                      WHERE binding.grant_id = external_dispatch_grants.grant_id
                        AND binding.descriptor_hash = external_dispatch_grants.descriptor_hash
                        AND binding.batch_hash = external_dispatch_grants.batch_hash
                        AND binding.assignment_hash = external_dispatch_grants.assignment_hash
                  )
                """,
                (
                    consumed_at,
                    grant_id,
                    descriptor_hash,
                    batch_hash,
                    assignment_hash,
                    consumed_at,
                    ExternalBootstrapState.PENDING.value,
                    self._EXTERNAL_BOOTSTRAP_AVAILABILITY,
                ),
            )
            if cursor.rowcount != 1:
                raise ExternalDispatchGrantError("external dispatch grant is expired, replayed, or unbound")
            row = cursor.execute(
                "SELECT * FROM external_dispatch_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            self._validate_external_grant_binding_at_read(row, cursor=cursor)
        return self._external_grant_from_row(row)

    def external_bootstrap_counts(self) -> tuple[int, int, int, int]:
        """Return descriptor, batch, outbox and grant counts for repository checks."""
        tables = (
            "external_bootstrap_descriptors",
            "external_bootstrap_batches",
            "external_bootstrap_outbox",
            "external_dispatch_grants",
        )
        return tuple(
            int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        )  # type: ignore[return-value]

    def create_workflow(self, workflow: Workflow) -> Workflow:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO workflows
                    (id, kind, title, product_summary, state, version, policy_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.kind.value,
                    workflow.title,
                    workflow.product_summary,
                    workflow.state.value,
                    workflow.version,
                    workflow.policy_version,
                    workflow.created_at,
                    workflow.updated_at,
                ),
            )
        return workflow

    def get_workflow(self, workflow_id: str) -> Workflow:
        row = self._connection.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"workflow not found: {workflow_id!r}")
        return self._workflow_from_row(row)

    def register_task(
        self,
        task: Task,
        *,
        dependencies: tuple[str, ...] = (),
        card_body: str | None = None,
        contract_subscriptions: tuple[str, ...] = (),
        required_evidence: tuple[str, ...] = (),
        strict_index: bool = False,
        workspace_id: str = "",
        input_snapshot_id: str = "",
        task_node_ids: tuple[str, ...] = (),
        contract_node_ids: tuple[str, ...] = (),
    ) -> Task:
        """Register a task and, when provided, its hash-bound task card atomically."""
        if card_body is not None and task.card_hash != _card_hash(card_body):
            raise CardHashMismatchError(
                f"task card hash does not match task {task.id!r}"
            )
        if strict_index and (
            type(workspace_id) is not str
            or self._SHA256_IDENTIFIER_PATTERN.fullmatch(workspace_id) is None
            or not input_snapshot_id
            or not task_node_ids
        ):
            raise StrictIndexError("INDEX_UNAVAILABLE")
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO tasks
                    (id, workflow_id, title, owner_role, state, write_scope, card_hash, result_hash,
                     version, task_kind, intent_id, language, framework)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.workflow_id,
                    task.title,
                    task.owner_role,
                    task.state.value,
                    _encode_strings(task.write_scope),
                    task.card_hash,
                    task.result_hash,
                    task.version,
                    task.task_kind.value,
                    task.intent_id,
                    task.language,
                    task.framework,
                ),
            )
            if card_body is not None:
                cursor.execute(
                    "INSERT INTO task_cards (task_id, card_hash, card_body) VALUES (?, ?, ?)",
                    (task.id, task.card_hash, card_body),
                )
            for contract_hash in dict.fromkeys(contract_subscriptions):
                cursor.execute(
                    "INSERT INTO task_contract_subscriptions (task_id, contract_hash) VALUES (?, ?)",
                    (task.id, contract_hash),
                )
            for position, evidence in enumerate(required_evidence):
                cursor.execute(
                    """
                    INSERT INTO task_required_evidence (task_id, position, evidence)
                    VALUES (?, ?, ?)
                    """,
                    (task.id, position, evidence),
                )
            for dependency_id in dict.fromkeys(dependencies):
                dependency = cursor.execute(
                    "SELECT workflow_id FROM tasks WHERE id = ?", (dependency_id,)
                ).fetchone()
                if dependency is None or dependency["workflow_id"] != task.workflow_id:
                    raise ValueError(
                        f"invalid dependency for task {task.id!r}: {dependency_id!r}"
                    )
                cursor.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, dependency_id) VALUES (?, ?)",
                    (task.id, dependency_id),
                )
            if strict_index:
                cursor.execute(
                    """
                    INSERT INTO task_index_bindings (
                        task_id, workspace_root, workspace_id, input_snapshot_id,
                        output_snapshot_id, task_node_ids, contract_node_ids,
                        checkpoint_id, indexed_diff_hash, fallback_count
                    ) VALUES (?, '', ?, ?, ?, ?, ?, '', '', 0)
                    """,
                    (
                        task.id,
                        workspace_id,
                        input_snapshot_id,
                        input_snapshot_id if not task.write_scope else "",
                        _encode_strings(task_node_ids),
                        _encode_strings(contract_node_ids),
                    ),
                )
                self._append_binding_event(
                    cursor,
                    task.id,
                    "registered",
                    snapshot_id=input_snapshot_id,
                )
        return task

    def get_index_binding(self, task_id: str) -> IndexBinding | None:
        row = self._connection.execute(
            "SELECT * FROM task_index_bindings WHERE task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else self._index_binding_from_row(row)

    def strict_task_context(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        now: str | None = None,
    ) -> tuple[Task, IndexBinding]:
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            task_row = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            binding_row = self._require_index_binding(cursor, task_id)
            task = self._task_from_row(task_row, self.dependencies_for(task_id))
            binding = self._index_binding_from_row(binding_row)
        return task, binding

    def record_index_query(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        trace_id: str,
        snapshot_id: str,
        miss_escape_used: bool,
        now: str | None = None,
    ) -> IndexBinding:
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            binding = self._require_index_binding(cursor, task_id)
            if snapshot_id not in {
                str(binding["input_snapshot_id"]),
                str(binding["output_snapshot_id"]),
            }:
                raise StrictIndexError("SNAPSHOT_MISMATCH")
            cursor.execute(
                """
                INSERT INTO task_index_query_receipts
                    (task_id, trace_id, snapshot_id, miss_escape_used, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, trace_id) DO NOTHING
                """,
                (task_id, trace_id, snapshot_id, int(miss_escape_used), now_utc),
            )
            if miss_escape_used and cursor.rowcount == 1:
                cursor.execute(
                    "UPDATE task_index_bindings SET fallback_count = fallback_count + 1 WHERE task_id = ?",
                    (task_id,),
                )
            self._append_binding_event(
                cursor, task_id, "query", snapshot_id=snapshot_id, trace_id=trace_id
            )
            row = self._require_index_binding(cursor, task_id)
        return self._index_binding_from_row(row)

    def record_checkpoint(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        checkpoint_id: str,
        *,
        now: str | None = None,
    ) -> IndexBinding:
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            binding = self._require_index_binding(cursor, task_id)
            task = cursor.execute(
                "SELECT write_scope FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not _decode_strings(task["write_scope"]):
                raise StrictIndexError("CHECKPOINT_REQUIRED")
            existing = str(binding["checkpoint_id"])
            if existing and existing != checkpoint_id:
                raise StrictIndexError("SNAPSHOT_MISMATCH")
            cursor.execute(
                "UPDATE task_index_bindings SET checkpoint_id = ? WHERE task_id = ?",
                (checkpoint_id, task_id),
            )
            self._append_binding_event(
                cursor, task_id, "checkpoint", trace_id=checkpoint_id
            )
            row = self._require_index_binding(cursor, task_id)
        return self._index_binding_from_row(row)

    def record_output_snapshot(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        snapshot_id: str,
        diff_hash: str,
        now: str | None = None,
    ) -> IndexBinding:
        if not snapshot_id or not diff_hash:
            raise StrictIndexError("INDEXED_DIFF_REQUIRED")
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            self._require_index_binding(cursor, task_id)
            task = cursor.execute(
                "SELECT write_scope FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not _decode_strings(task["write_scope"]):
                raise StrictIndexError("INDEXED_DIFF_REQUIRED")
            cursor.execute(
                """
                UPDATE task_index_bindings
                SET output_snapshot_id = ?, indexed_diff_hash = ?
                WHERE task_id = ?
                """,
                (snapshot_id, diff_hash, task_id),
            )
            self._append_binding_event(
                cursor, task_id, "output", snapshot_id=snapshot_id
            )
            row = self._require_index_binding(cursor, task_id)
        return self._index_binding_from_row(row)

    def get_task_card(self, task_id: str) -> str:
        """Return a single task's registered card body without any list projection."""
        row = self._connection.execute(
            "SELECT card_body FROM task_cards WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task card not found: {task_id!r}")
        return str(row["card_body"])

    def get_task_context_requirements(self, task_id: str) -> TaskContextRequirements:
        """Return only one task's durable contracts and evidence requirements."""
        task = self._connection.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise KeyError(f"task not found: {task_id!r}")
        contract_rows = self._connection.execute(
            """
            SELECT contract_hash FROM task_contract_subscriptions
            WHERE task_id = ? ORDER BY rowid
            """,
            (task_id,),
        ).fetchall()
        evidence_rows = self._connection.execute(
            """
            SELECT evidence FROM task_required_evidence
            WHERE task_id = ? ORDER BY position
            """,
            (task_id,),
        ).fetchall()
        return TaskContextRequirements(
            tuple(str(row["contract_hash"]) for row in contract_rows),
            tuple(str(row["evidence"]) for row in evidence_rows),
        )

    def list_authorized_peers(self, workflow_id: str, task_id: str) -> tuple[Peer, ...]:
        """Return the fixed dependency or common-contract recipients for one task."""
        with self._transaction() as cursor:
            self._require_task_in_workflow(cursor, workflow_id, task_id, KeyError)
            relationships = self._peer_relationships(cursor, workflow_id, task_id)
            peers: list[Peer] = []
            for peer_id, relationship in relationships:
                row = cursor.execute(
                    """
                    SELECT capability FROM peer_capabilities
                    WHERE workflow_id = ? AND sender_task_id = ? AND recipient_task_id = ?
                        AND relationship = ?
                    """,
                    (workflow_id, task_id, peer_id, relationship),
                ).fetchone()
                if row is None:
                    capability = secrets.token_urlsafe(32)
                    cursor.execute(
                        """
                        INSERT INTO peer_capabilities
                            (workflow_id, sender_task_id, recipient_task_id, relationship, capability)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (workflow_id, task_id, peer_id, relationship, capability),
                    )
                else:
                    capability = str(row["capability"])
                peers.append(Peer(peer_id, relationship, capability))
        return tuple(peers)

    def enqueue_message(
        self,
        workflow_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        *,
        capability: str,
        correlation_id: str,
        artifact_hash: str,
        metadata: Mapping[str, str],
        ttl_seconds: int,
        now: str | None = None,
        max_count: int,
        max_bytes: int,
    ) -> Message:
        """Atomically authorize and persist one recipient mailbox entry."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 0 < ttl_seconds <= self._MAX_MESSAGE_TTL_SECONDS
        ):
            raise TTLInvalidError("message TTL is outside the permitted range")
        if max_count < 1 or max_bytes < 1:
            raise QuotaExceededError("message quotas must be positive")
        metadata_json = _encode_metadata(metadata)
        with self._transaction() as cursor:
            self._require_current_lease(
                cursor, sender_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                cursor, workflow_id, sender_task_id, PeerForbiddenError
            )
            self._require_task_in_workflow(
                cursor, workflow_id, recipient_task_id, PeerForbiddenError
            )
            relationship = dict(
                self._peer_relationships(cursor, workflow_id, sender_task_id)
            ).get(recipient_task_id)
            if relationship is None:
                raise PeerForbiddenError(
                    f"task is not an authorized peer: {recipient_task_id!r}"
                )
            capability_row = cursor.execute(
                """
                SELECT capability FROM peer_capabilities
                WHERE workflow_id = ? AND sender_task_id = ? AND recipient_task_id = ?
                    AND relationship = ?
                """,
                (workflow_id, sender_task_id, recipient_task_id, relationship),
            ).fetchone()
            if capability_row is None or capability_row["capability"] != capability:
                raise CapabilityInvalidError(
                    "delivery capability is not valid for this peer"
                )
            existing = cursor.execute(
                """
                SELECT * FROM messages
                WHERE workflow_id = ? AND sender_task_id = ? AND recipient_task_id = ?
                    AND correlation_id = ?
                """,
                (workflow_id, sender_task_id, recipient_task_id, correlation_id),
            ).fetchone()
            if existing is not None:
                if existing["artifact_hash"] == artifact_hash:
                    return self._message_from_row(existing)
                raise CorrelationConflictError(
                    "correlation id is already bound to another artifact"
                )
            artifact = cursor.execute(
                """
                SELECT artifacts.size, artifacts.redaction_version
                FROM artifacts JOIN artifact_owners ON artifact_owners.content_hash = artifacts.content_hash
                WHERE artifacts.content_hash = ? AND artifact_owners.task_id = ?
                """,
                (artifact_hash, sender_task_id),
            ).fetchone()
            if artifact is None or not artifact["redaction_version"]:
                raise ArtifactNotOwnedError(
                    f"artifact is not owned by sender: {artifact_hash!r}"
                )
            artifact_size = int(artifact["size"])
            recipient_lease = cursor.execute(
                """
                SELECT epoch FROM leases
                WHERE task_id = ? AND expires_at > ?
                """,
                (recipient_task_id, now_utc),
            ).fetchone()
            self._require_durable_mailbox_quota(
                cursor,
                workflow_id=workflow_id,
                recipient_task_id=recipient_task_id,
                recipient_epoch=(
                    int(recipient_lease["epoch"]) if recipient_lease is not None else None
                ),
                now=now_utc,
                incoming_bytes=artifact_size,
                max_count=max_count,
                max_bytes=max_bytes,
            )
            created_at = now_utc
            expires_at = (
                datetime.fromisoformat(now_utc) + timedelta(seconds=ttl_seconds)
            ).isoformat()
            delivery_id = uuid.uuid4().hex
            cursor.execute(
                """
                INSERT INTO messages
                    (delivery_id, workflow_id, sender_task_id, recipient_task_id, correlation_id,
                     artifact_hash, redacted_metadata, created_at, expires_at, delivery_state, acknowledged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    delivery_id,
                    workflow_id,
                    sender_task_id,
                    recipient_task_id,
                    correlation_id,
                    artifact_hash,
                    metadata_json,
                    created_at,
                    expires_at,
                    "pending",
                ),
            )
            sequence = int(cursor.lastrowid)
            self._append_event_in_transaction(
                cursor,
                workflow_id,
                sender_task_id,
                "message_enqueued",
                f"delivery={delivery_id};artifact={artifact_hash}",
            )
            message = cursor.execute(
                "SELECT * FROM messages WHERE sequence = ?", (sequence,)
            ).fetchone()
        return self._message_from_row(message)

    def enqueue_role_envelope(
        self,
        workflow_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        *,
        recipient_epoch: int,
        direction: RoleEnvelopeDirection | str,
        sender_role: str,
        recipient_role: str,
        assignment_token: str,
        dispatch_context_hash: str,
        route_provenance_hash: str,
        correlation_id: str,
        ttl_seconds: int,
        task_card_hash: str = "",
        contract_hashes: tuple[str, ...] = (),
        index_evidence_hashes: tuple[str, ...] = (),
        terminal_result_hash: str = "",
        evidence_hashes: tuple[str, ...] = (),
        dependency_hashes: tuple[str, ...] = (),
        risk_items: tuple[RoleRiskItem | Mapping[str, str], ...] = (),
        coordinator_task_id: str | None = None,
        coordinator_epoch: int | None = None,
        capability: str | None = None,
        now: str | None = None,
        max_count: int,
        max_bytes: int,
    ) -> RoleEnvelope:
        """Persist one exact, transcript-free role envelope under both lease fences."""

        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 0 < ttl_seconds <= self._MAX_MESSAGE_TTL_SECONDS
        ):
            raise TTLInvalidError("message TTL is outside the permitted range")
        if max_count < 1 or max_bytes < 1:
            raise QuotaExceededError("message quotas must be positive")
        direction_value = self._role_direction(direction)
        expires_at = (
            datetime.fromisoformat(now_utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, sender_task_id, owner, epoch, now=now_utc)
            self._require_task_in_workflow(
                cursor, workflow_id, sender_task_id, RoleEnvelopeForbiddenError
            )
            self._require_task_in_workflow(
                cursor, workflow_id, recipient_task_id, RoleEnvelopeForbiddenError
            )
            self._require_current_recipient_lease(
                cursor, recipient_task_id, recipient_epoch, now=now_utc
            )
            coordinator_id, coordinator_lease_epoch = self._role_coordinator_binding(
                cursor,
                workflow_id=workflow_id,
                direction=direction_value,
                sender_task_id=sender_task_id,
                sender_epoch=epoch,
                recipient_task_id=recipient_task_id,
                recipient_epoch=recipient_epoch,
                coordinator_task_id=coordinator_task_id,
                coordinator_epoch=coordinator_epoch,
                now=now_utc,
            )
            self._require_role_task_roles(
                cursor,
                sender_task_id=sender_task_id,
                sender_role=sender_role,
                recipient_task_id=recipient_task_id,
                recipient_role=recipient_role,
                direction=direction_value,
            )
            recipient_capability_hash = ""
            if direction_value is RoleEnvelopeDirection.PEER_TO_PEER:
                recipient_capability_hash = self._require_role_peer_capability(
                    cursor,
                    workflow_id=workflow_id,
                    sender_task_id=sender_task_id,
                    recipient_task_id=recipient_task_id,
                    capability=capability,
                )
            elif capability is not None:
                raise CapabilityInvalidError(
                    "a capability is valid only for a peer role envelope"
                )
            payload = self._role_envelope_payload(
                direction=direction_value,
                workflow_id=workflow_id,
                sender_task_id=sender_task_id,
                sender_role=sender_role,
                sender_epoch=epoch,
                recipient_task_id=recipient_task_id,
                recipient_role=recipient_role,
                recipient_epoch=recipient_epoch,
                coordinator_task_id=coordinator_id,
                coordinator_epoch=coordinator_lease_epoch,
                assignment_token=assignment_token,
                dispatch_context_hash=dispatch_context_hash,
                route_provenance_hash=route_provenance_hash,
                correlation_id=correlation_id,
                task_card_hash=task_card_hash,
                contract_hashes=contract_hashes,
                index_evidence_hashes=index_evidence_hashes,
                terminal_result_hash=terminal_result_hash,
                evidence_hashes=evidence_hashes,
                dependency_hashes=dependency_hashes,
                risk_items=risk_items,
                recipient_capability_hash=recipient_capability_hash,
                issued_at=now_utc,
                expires_at=expires_at,
            )
            payload_json = _canonical_payload_json(payload)
            envelope_hash = _payload_hash(payload_json)
            existing = cursor.execute(
                """
                SELECT * FROM role_envelopes
                WHERE workflow_id = ? AND sender_task_id = ? AND recipient_task_id = ?
                    AND correlation_id = ?
                """,
                (workflow_id, sender_task_id, recipient_task_id, correlation_id),
            ).fetchone()
            if existing is not None:
                if str(existing["envelope_hash"]) == envelope_hash:
                    return self._role_envelope_from_row(existing)
                raise CorrelationConflictError(
                    "correlation id is already bound to another role envelope"
                )
            self._require_role_direction_references(
                cursor,
                direction=direction_value,
                workflow_id=workflow_id,
                sender_task_id=sender_task_id,
                recipient_task_id=recipient_task_id,
                payload=payload,
            )
            if direction_value is not RoleEnvelopeDirection.COORDINATOR_TO_WORKER:
                self._require_live_role_assignment(
                    cursor,
                    workflow_id=workflow_id,
                    worker_task_id=sender_task_id,
                    worker_epoch=epoch,
                    coordinator_task_id=coordinator_id,
                    coordinator_epoch=coordinator_lease_epoch,
                    assignment_token_hash=str(payload["assignment_token_hash"]),
                    dispatch_context_hash=dispatch_context_hash,
                    route_provenance_hash=route_provenance_hash,
                    now=now_utc,
                )
            reference_bytes = self._role_reference_bytes(
                cursor,
                sender_task_id=sender_task_id,
                payload=payload,
            )
            self._require_durable_mailbox_quota(
                cursor,
                workflow_id=workflow_id,
                recipient_task_id=recipient_task_id,
                recipient_epoch=recipient_epoch,
                now=now_utc,
                incoming_bytes=reference_bytes,
                max_count=max_count,
                max_bytes=max_bytes,
            )
            delivery_id = uuid.uuid4().hex
            cursor.execute(
                """
                INSERT INTO role_envelopes (
                    delivery_id, workflow_id, sender_task_id, recipient_task_id, direction,
                    sender_role, recipient_role, sender_epoch, recipient_epoch, correlation_id,
                    assignment_token_hash, dispatch_context_hash, route_provenance_hash,
                    coordinator_task_id, coordinator_epoch, correlation_fence_hash, payload_json,
                    envelope_hash, reference_bytes, issued_at, expires_at, delivery_state,
                    acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    delivery_id,
                    workflow_id,
                    sender_task_id,
                    recipient_task_id,
                    direction_value.value,
                    sender_role,
                    recipient_role,
                    epoch,
                    recipient_epoch,
                    correlation_id,
                    payload["assignment_token_hash"],
                    dispatch_context_hash,
                    route_provenance_hash,
                    coordinator_id,
                    coordinator_lease_epoch,
                    payload["correlation_fence_hash"],
                    payload_json,
                    envelope_hash,
                    reference_bytes,
                    now_utc,
                    expires_at,
                    "pending",
                ),
            )
            sequence = int(cursor.lastrowid)
            self._append_event_in_transaction(
                cursor,
                workflow_id,
                sender_task_id,
                "role_envelope_enqueued",
                f"delivery={delivery_id};envelope={envelope_hash}",
            )
            row = cursor.execute(
                "SELECT * FROM role_envelopes WHERE sequence = ?", (sequence,)
            ).fetchone()
        return self._role_envelope_from_row(row)

    def read_role_inbox(
        self,
        workflow_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        *,
        recipient_role: str,
        cursor: str | None = None,
        limit: int = 50,
        now: str | None = None,
    ) -> tuple[RoleEnvelope, ...]:
        """Read only unexpired role envelopes for the live recipient lease epoch."""

        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        bounded_limit = min(max(1, limit), self._MAX_INBOX_LIMIT)
        with self._transaction() as transaction:
            self._require_current_lease(
                transaction, recipient_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                transaction, workflow_id, recipient_task_id, MailboxForbiddenError
            )
            role_row = transaction.execute(
                "SELECT owner_role FROM tasks WHERE id = ?", (recipient_task_id,)
            ).fetchone()
            if role_row is None or str(role_row["owner_role"]) != recipient_role:
                raise RoleEnvelopeForbiddenError("recipient role is not authoritative")
            after_sequence = self._role_envelope_cursor(
                transaction, workflow_id, recipient_task_id, cursor
            )
            rows = transaction.execute(
                """
                SELECT * FROM role_envelopes
                WHERE workflow_id = ? AND recipient_task_id = ? AND recipient_epoch = ?
                    AND sequence > ? AND acknowledged_at IS NULL AND expires_at > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (
                    workflow_id,
                    recipient_task_id,
                    epoch,
                    after_sequence,
                    now_utc,
                    bounded_limit,
                ),
            ).fetchall()
        return tuple(self._role_envelope_from_row(row) for row in rows)

    def ack_role_envelope(
        self,
        workflow_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        delivery_id: str,
        *,
        recipient_role: str,
        now: str | None = None,
    ) -> RoleEnvelope:
        """Acknowledge a role envelope without removing its durable audit row."""

        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(
                cursor, recipient_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                cursor, workflow_id, recipient_task_id, MailboxForbiddenError
            )
            row = cursor.execute(
                "SELECT owner_role FROM tasks WHERE id = ?", (recipient_task_id,)
            ).fetchone()
            if row is None or str(row["owner_role"]) != recipient_role:
                raise RoleEnvelopeForbiddenError("recipient role is not authoritative")
            envelope = cursor.execute(
                """
                SELECT * FROM role_envelopes
                WHERE delivery_id = ? AND workflow_id = ? AND recipient_task_id = ?
                    AND recipient_epoch = ?
                """,
                (delivery_id, workflow_id, recipient_task_id, epoch),
            ).fetchone()
            if envelope is None:
                raise MailboxForbiddenError(
                    f"role envelope does not belong to recipient: {delivery_id!r}"
                )
            if envelope["acknowledged_at"] is not None:
                return self._role_envelope_from_row(envelope)
            if str(envelope["expires_at"]) <= now_utc:
                raise MessageExpiredError(f"role envelope is expired: {delivery_id!r}")
            cursor.execute(
                """
                UPDATE role_envelopes
                SET delivery_state = ?, acknowledged_at = ?
                WHERE delivery_id = ? AND acknowledged_at IS NULL
                """,
                ("acknowledged", now_utc, delivery_id),
            )
            envelope = cursor.execute(
                "SELECT * FROM role_envelopes WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return self._role_envelope_from_row(envelope)

    def record_host_archive_result(
        self,
        workflow_id: str,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        operation_id: str,
        assignment_token: str,
        dispatch_context_hash: str,
        route_provenance_hash: str,
        coordinator_task_id: str,
        coordinator_epoch: int,
        errno: int,
        now: str | None = None,
    ) -> HostOperationReceipt:
        """Record an archive report only; this never invokes a host or task transition."""

        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        if not isinstance(errno, int) or isinstance(errno, bool):
            raise RoleEnvelopeInvalidError("archive errno must be an integer")
        if not self._safe_role_identifier(operation_id):
            raise RoleEnvelopeInvalidError("archive operation id is not safe")
        assignment_token_hash = self._assignment_token_hash(assignment_token)
        self._require_role_hash(dispatch_context_hash, "dispatch context hash")
        self._require_role_hash(route_provenance_hash, "route provenance hash")
        status_code = (
            "HOST_ARCHIVE_OS_ERROR_17" if errno == 17 else "HOST_ARCHIVE_REPORTED"
        )
        outcome = "blocked" if errno == 17 else "reported"
        receipt_payload = {
            "schema_version": self._HOST_ARCHIVE_RECEIPT_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "operation": "archive",
            "operation_id": operation_id,
            "lease_epoch": epoch,
            "assignment_token_hash": assignment_token_hash,
            "dispatch_context_hash": dispatch_context_hash,
            "route_provenance_hash": route_provenance_hash,
            "coordinator_task_id": coordinator_task_id,
            "coordinator_epoch": coordinator_epoch,
            "errno": errno,
            "status_code": status_code,
            "outcome": outcome,
        }
        receipt_hash = _payload_hash(_canonical_payload_json(receipt_payload))
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            self._require_task_in_workflow(
                cursor, workflow_id, task_id, RoleEnvelopeForbiddenError
            )
            task = cursor.execute(
                "SELECT owner_role FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None or str(task["owner_role"]) != "worker":
                raise RoleEnvelopeForbiddenError("archive report is not owned by a worker")
            existing = cursor.execute(
                "SELECT * FROM host_operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["receipt_hash"]) == receipt_hash:
                    return self._host_operation_receipt_from_row(existing)
                raise HostOperationConflictError("archive operation id was reused")
            self._require_live_role_assignment(
                cursor,
                workflow_id=workflow_id,
                worker_task_id=task_id,
                worker_epoch=epoch,
                coordinator_task_id=coordinator_task_id,
                coordinator_epoch=coordinator_epoch,
                assignment_token_hash=assignment_token_hash,
                dispatch_context_hash=dispatch_context_hash,
                route_provenance_hash=route_provenance_hash,
                now=now_utc,
            )
            cursor.execute(
                """
                INSERT INTO host_operation_receipts (
                    operation_id, workflow_id, task_id, operation, lease_epoch,
                    assignment_token_hash, dispatch_context_hash, route_provenance_hash,
                    coordinator_task_id, coordinator_epoch, errno, status_code, outcome,
                    receipt_hash, reported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    workflow_id,
                    task_id,
                    "archive",
                    epoch,
                    assignment_token_hash,
                    dispatch_context_hash,
                    route_provenance_hash,
                    coordinator_task_id,
                    coordinator_epoch,
                    errno,
                    status_code,
                    outcome,
                    receipt_hash,
                    now_utc,
                ),
            )
            self._append_event_in_transaction(
                cursor,
                workflow_id,
                task_id,
                "host_archive_reported",
                f"operation={operation_id};status={status_code}",
            )
            row = cursor.execute(
                "SELECT * FROM host_operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._host_operation_receipt_from_row(row)

    def read_inbox(
        self,
        workflow_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        *,
        cursor: str | None = None,
        limit: int = 50,
        now: str | None = None,
    ) -> tuple[Message, ...]:
        """Read only the caller-owned live lease's unexpired, unacknowledged mailbox rows."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        bounded_limit = min(max(1, limit), self._MAX_INBOX_LIMIT)
        with self._transaction() as transaction:
            self._require_current_lease(
                transaction, recipient_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                transaction, workflow_id, recipient_task_id, MailboxForbiddenError
            )
            after_sequence = self._message_cursor(
                transaction, workflow_id, recipient_task_id, cursor
            )
            rows = transaction.execute(
                """
                SELECT * FROM messages
                WHERE workflow_id = ? AND recipient_task_id = ? AND sequence > ?
                    AND acknowledged_at IS NULL AND expires_at > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (
                    workflow_id,
                    recipient_task_id,
                    after_sequence,
                    now_utc,
                    bounded_limit,
                ),
            ).fetchall()
        return tuple(self._message_from_row(row) for row in rows)

    def ack_message(
        self,
        workflow_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        delivery_id: str,
        *,
        now: str | None = None,
    ) -> Message:
        """Acknowledge one recipient-owned mailbox entry without deleting its audit row."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            lease = cursor.execute(
                "SELECT owner FROM leases WHERE task_id = ?", (recipient_task_id,)
            ).fetchone()
            if lease is None or lease["owner"] != owner:
                raise MailboxForbiddenError(
                    f"mailbox does not belong to owner: {recipient_task_id!r}"
                )
            self._require_current_lease(
                cursor, recipient_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                cursor, workflow_id, recipient_task_id, MailboxForbiddenError
            )
            message = cursor.execute(
                "SELECT * FROM messages WHERE delivery_id = ? AND workflow_id = ?",
                (delivery_id, workflow_id),
            ).fetchone()
            if message is None or message["recipient_task_id"] != recipient_task_id:
                raise MailboxForbiddenError(
                    f"message does not belong to recipient: {delivery_id!r}"
                )
            if message["acknowledged_at"] is not None:
                return self._message_from_row(message)
            if str(message["expires_at"]) <= now_utc:
                raise MessageExpiredError(f"message is expired: {delivery_id!r}")
            cursor.execute(
                """
                UPDATE messages
                SET delivery_state = ?, acknowledged_at = ?
                WHERE delivery_id = ? AND acknowledged_at IS NULL
                """,
                ("acknowledged", now_utc, delivery_id),
            )
            message = cursor.execute(
                "SELECT * FROM messages WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return self._message_from_row(message)

    def resolve_message_artifact(
        self,
        workflow_id: str,
        recipient_task_id: str,
        owner: str,
        epoch: int,
        delivery_id: str,
        *,
        now: str | None = None,
    ) -> Artifact:
        """Resolve an unexpired mailbox delivery to its registered artifact metadata."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            lease = cursor.execute(
                "SELECT owner FROM leases WHERE task_id = ?", (recipient_task_id,)
            ).fetchone()
            if lease is None or lease["owner"] != owner:
                raise MailboxForbiddenError(
                    f"mailbox does not belong to owner: {recipient_task_id!r}"
                )
            self._require_current_lease(
                cursor, recipient_task_id, owner, epoch, now=now_utc
            )
            self._require_task_in_workflow(
                cursor, workflow_id, recipient_task_id, MailboxForbiddenError
            )
            row = cursor.execute(
                """
                SELECT
                    artifacts.kind,
                    artifacts.content_hash,
                    artifacts.safe_path,
                    artifacts.size,
                    artifacts.redaction_version,
                    artifacts.created_at,
                    messages.expires_at
                FROM messages
                JOIN artifacts ON artifacts.content_hash = messages.artifact_hash
                WHERE messages.delivery_id = ?
                    AND messages.workflow_id = ?
                    AND messages.recipient_task_id = ?
                """,
                (delivery_id, workflow_id, recipient_task_id),
            ).fetchone()
            if row is None:
                raise MailboxForbiddenError(
                    f"message does not belong to recipient: {delivery_id!r}"
                )
            if str(row["expires_at"]) <= now_utc:
                raise MessageExpiredError(f"message is expired: {delivery_id!r}")
        return self._artifact_from_row(row)

    def get_task_host_target(
        self, task_id: str, *, now: str | None = None
    ) -> str | None:
        """Return the current lease-bound Codex target, if one is still usable."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        row = self._connection.execute(
            "SELECT host_target, expires_at FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None or not row["host_target"] or str(row["expires_at"]) <= now_utc:
            return None
        return str(row["host_target"])

    def is_task_online(self, task_id: str, *, now: str | None = None) -> bool:
        return self.get_task_host_target(task_id, now=now) is not None

    def get_task(self, task_id: str) -> Task:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id!r}")
        return self._task_from_row(row, self.dependencies_for(task_id))

    def list_tasks(self, workflow_id: str) -> tuple[Task, ...]:
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY rowid", (workflow_id,)
        ).fetchall()
        return tuple(
            self._task_from_row(row, self.dependencies_for(row["id"])) for row in rows
        )

    def insert_code_task_acceptance(
        self,
        *,
        workflow_id: str,
        task_id: str,
        task_version: int,
        coordinator_task_id: str,
        coordinator_owner: str,
        coordinator_epoch: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        intent_id: str,
        language: str,
        framework: str,
        evidence_binding: CodeTaskEvidenceBinding | None = None,
        created_at: str,
        now: str | None = None,
    ) -> tuple[CodeTaskAcceptance, AtlasOutboxItem]:
        """Authorize and insert one acceptance with its outbox item atomically.

        This persistence boundary enforces durable task, strict-index, and live
        coordinator gates. Receipt-specific policy and Atlas projection stay in
        the service layer.
        """
        payload_json = self._canonical_code_task_acceptance_payload(
            workflow_id=workflow_id,
            task_id=task_id,
            task_version=task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
            intent_id=intent_id,
            language=language,
            framework=framework,
        )
        payload_hash = _payload_hash(payload_json)
        accepted_at = _utc_timestamp(created_at)
        authorization_now = _utc_timestamp(now) if now is not None else _utc_now()
        if evidence_binding is None:
            raise AcceptanceEvidenceError("acceptance evidence binding is required")
        self._validate_code_task_evidence_binding(
            evidence_binding,
            workflow_id=workflow_id,
            task_id=task_id,
            task_version=task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
        )

        with self._transaction() as cursor:
            self._require_authorized_code_task_acceptance(
                cursor,
                workflow_id=workflow_id,
                task_id=task_id,
                task_version=task_version,
                coordinator_task_id=coordinator_task_id,
                coordinator_owner=coordinator_owner,
                coordinator_epoch=coordinator_epoch,
                input_snapshot_id=input_snapshot_id,
                output_snapshot_id=output_snapshot_id,
                indexed_diff_hash=indexed_diff_hash,
                intent_id=intent_id,
                language=language,
                framework=framework,
                now=authorization_now,
            )
            receipt_attestation_row = cursor.execute(
                """
                SELECT * FROM code_task_receipt_attestations
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if receipt_attestation_row is None:
                raise AcceptanceEvidenceError(
                    "code task receipt attestation is required"
                )
            receipt_attestation = self._code_task_receipt_attestation_from_row(
                receipt_attestation_row
            )
            if (
                receipt_attestation.workflow_id != workflow_id
                or receipt_attestation.code_task_version != task_version
                or receipt_attestation.input_snapshot_id != input_snapshot_id
                or receipt_attestation.output_snapshot_id != output_snapshot_id
                or receipt_attestation.execution_receipt_ids
                != evidence_binding.execution_receipt_ids
                or receipt_attestation.attestation_hash
                not in evidence_binding.verification_artifact_hashes
            ):
                raise AcceptanceConflictError(
                    "receipt attestation does not match acceptance evidence"
                )
            existing_row = cursor.execute(
                "SELECT * FROM code_task_acceptances WHERE code_task_id = ?",
                (task_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._acceptance_from_row(existing_row)
                if (
                    existing.payload_hash != payload_hash
                    or str(existing_row["payload_json"]) != payload_json
                ):
                    raise AcceptanceConflictError(
                        f"code task already has different acceptance content: {task_id!r}"
                    )
                outbox_row = cursor.execute(
                    "SELECT * FROM atlas_ingestion_outbox WHERE acceptance_id = ?",
                    (existing.acceptance_id,),
                ).fetchone()
                if outbox_row is None:
                    raise StoreError(
                        f"acceptance is missing its durable outbox item: {task_id!r}"
                    )
                self._require_existing_code_task_evidence_binding(
                    cursor, existing, evidence_binding
                )
                return existing, self._atlas_outbox_from_row(outbox_row)

            cursor.execute(
                """
                INSERT INTO code_task_acceptances (
                    acceptance_id, workflow_id, code_task_id, code_task_version,
                    input_snapshot_id, output_snapshot_id, indexed_diff_hash,
                    intent_id, language, framework, payload_json, payload_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload_hash,
                    workflow_id,
                    task_id,
                    task_version,
                    input_snapshot_id,
                    output_snapshot_id,
                    indexed_diff_hash,
                    intent_id,
                    language,
                    framework,
                    payload_json,
                    payload_hash,
                    accepted_at,
                ),
            )
            acceptance_row = cursor.execute(
                "SELECT * FROM code_task_acceptances WHERE acceptance_id = ?",
                (payload_hash,),
            ).fetchone()
            acceptance = self._acceptance_from_row(acceptance_row)
            self._insert_code_task_evidence_binding(
                cursor,
                acceptance,
                evidence_binding,
                created_at=accepted_at,
            )
            outbox = self._insert_atlas_outbox(
                cursor,
                acceptance,
                payload_json=payload_json,
                created_at=accepted_at,
            )
        return acceptance, outbox

    def acceptance_for_task(self, task_id: str) -> CodeTaskAcceptance | None:
        """Return an accepted code task's immutable metadata, if it exists."""
        row = self._connection.execute(
            "SELECT * FROM code_task_acceptances WHERE code_task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else self._acceptance_from_row(row)

    def acceptance_for_workflow_task(
        self, workflow_id: str, code_task_id: str
    ) -> CodeTaskAcceptance | None:
        """Look up one acceptance through its exact immutable task identity."""

        self._safe_acceptance_identifier("workflow_id", workflow_id)
        self._safe_acceptance_identifier("code_task_id", code_task_id)
        row = self._connection.execute(
            """
            SELECT * FROM code_task_acceptances
            WHERE workflow_id = ? AND code_task_id = ?
            """,
            (workflow_id, code_task_id),
        ).fetchone()
        return None if row is None else self._acceptance_from_row(row)

    def list_code_task_acceptances(
        self, workflow_id: str, *, limit: int
    ) -> tuple[CodeTaskAcceptance, ...]:
        """Return a bounded deterministic acceptance projection for one workflow."""

        self._validate_code_task_acceptance_list_limit(limit)
        rows = self._connection.execute(
            """
            SELECT * FROM code_task_acceptances
            WHERE workflow_id = ?
            ORDER BY created_at, acceptance_id
            LIMIT ?
            """,
            (workflow_id, limit),
        ).fetchall()
        return tuple(self._acceptance_from_row(row) for row in rows)

    def atlas_outbox_for_acceptance(self, acceptance_id: str) -> AtlasOutboxItem | None:
        """Return the one outbox row owned by an immutable acceptance, if present."""

        self._safe_acceptance_identifier("acceptance_id", acceptance_id)
        row = self._connection.execute(
            "SELECT * FROM atlas_ingestion_outbox WHERE acceptance_id = ?",
            (acceptance_id,),
        ).fetchone()
        return None if row is None else self._atlas_outbox_from_row(row)

    def task_acceptance_evidence(
        self, task_id: str, output_snapshot_id: str
    ) -> TaskAcceptanceEvidence:
        """Return only deterministic output evidence identifiers for one task."""

        self._safe_acceptance_identifier("task_id", task_id)
        self._safe_acceptance_identifier("output_snapshot_id", output_snapshot_id)
        query_rows = self._connection.execute(
            """
            SELECT trace_id FROM task_index_query_receipts
            WHERE task_id = ? AND snapshot_id = ?
            ORDER BY trace_id
            LIMIT 2
            """,
            (task_id, output_snapshot_id),
        ).fetchall()
        if len(query_rows) != 1:
            raise StrictIndexError("QUERY_RECEIPT_REQUIRED")
        artifact_rows = self._connection.execute(
            """
            SELECT content_hash FROM task_index_verification_artifacts
            WHERE task_id = ? AND snapshot_id = ?
            ORDER BY content_hash
            LIMIT ?
            """,
            (task_id, output_snapshot_id, self._MAX_CODE_TASK_EVIDENCE_ITEMS + 1),
        ).fetchall()
        attestation_row = self._connection.execute(
            """
            SELECT attestation_hash FROM code_task_receipt_attestations
            WHERE task_id = ? AND output_snapshot_id = ?
            """,
            (task_id, output_snapshot_id),
        ).fetchone()
        verification_hashes = tuple(
            sorted(
                {
                    *(str(row["content_hash"]) for row in artifact_rows),
                    *(
                        ()
                        if attestation_row is None
                        else (str(attestation_row["attestation_hash"]),)
                    ),
                }
            )
        )
        if (
            not verification_hashes
            or len(verification_hashes) > self._MAX_CODE_TASK_EVIDENCE_ITEMS
        ):
            raise StrictIndexError("VERIFICATION_EVIDENCE_REQUIRED")
        return TaskAcceptanceEvidence(
            str(query_rows[0]["trace_id"]),
            verification_hashes,
        )

    def code_task_receipt_attestation_for_task(
        self, task_id: str
    ) -> CodeTaskReceiptAttestation | None:
        """Recover and integrity-check one completion-gated receipt attestation."""

        self._safe_acceptance_identifier("task_id", task_id)
        row = self._connection.execute(
            "SELECT * FROM code_task_receipt_attestations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._code_task_receipt_attestation_from_row(row)

    def code_task_receipt_attestation_for_acceptance(
        self, acceptance_id: str
    ) -> CodeTaskReceiptAttestation | None:
        """Recover a typed receipt attestation through immutable acceptance identity."""

        self._safe_acceptance_identifier("acceptance_id", acceptance_id)
        acceptance = self._connection.execute(
            "SELECT * FROM code_task_acceptances WHERE acceptance_id = ?",
            (acceptance_id,),
        ).fetchone()
        if acceptance is None:
            return None
        row = self._connection.execute(
            "SELECT * FROM code_task_receipt_attestations WHERE task_id = ?",
            (str(acceptance["code_task_id"]),),
        ).fetchone()
        if row is None:
            raise AcceptanceConflictError(
                "accepted code task has no receipt attestation"
            )
        attestation = self._code_task_receipt_attestation_from_row(row)
        if (
            attestation.workflow_id != str(acceptance["workflow_id"])
            or attestation.code_task_id != str(acceptance["code_task_id"])
            or attestation.code_task_version != int(acceptance["code_task_version"])
            or attestation.input_snapshot_id != str(acceptance["input_snapshot_id"])
            or attestation.output_snapshot_id != str(acceptance["output_snapshot_id"])
        ):
            raise AcceptanceConflictError(
                "receipt attestation does not match acceptance"
            )
        return attestation

    def evidence_binding_for_acceptance(
        self, acceptance_id: str
    ) -> CodeTaskEvidenceBinding | None:
        """Read one typed recovery binding by immutable acceptance identity."""

        self._safe_acceptance_identifier("acceptance_id", acceptance_id)
        row = self._connection.execute(
            "SELECT * FROM code_task_acceptances WHERE acceptance_id = ?",
            (acceptance_id,),
        ).fetchone()
        if row is None:
            return None
        return self._code_task_evidence_binding_for_acceptance(
            self._acceptance_from_row(row)
        )

    def evidence_binding_for_ingestion(
        self, ingestion_key: str
    ) -> CodeTaskEvidenceBinding | None:
        """Read one typed recovery binding by immutable ingestion identity."""

        self._safe_acceptance_identifier("ingestion_key", ingestion_key)
        row = self._connection.execute(
            """
            SELECT acceptances.*
            FROM atlas_ingestion_outbox AS outbox
            JOIN code_task_acceptances AS acceptances
                ON acceptances.acceptance_id = outbox.acceptance_id
            WHERE outbox.ingestion_key = ?
            """,
            (ingestion_key,),
        ).fetchone()
        if row is None:
            return None
        return self._code_task_evidence_binding_for_acceptance(
            self._acceptance_from_row(row)
        )

    def accepted_code_task_evidence(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> AcceptedCodeTaskEvidence | None:
        """Read one exact acceptance without trusting its mutable outbox payload.

        The returned values are reconstructed from durable acceptance, binding,
        task, receipt-attestation, and artifact records. Outbox state and
        ``payload_json`` deliberately remain outside this boundary.
        """

        for field_name, value in (
            ("workflow_id", workflow_id),
            ("code_task_id", code_task_id),
            ("acceptance_id", acceptance_id),
            ("ingestion_key", ingestion_key),
        ):
            self._safe_acceptance_identifier(field_name, value)

        connection = self._connection
        if connection is None:
            raise AcceptanceEvidenceError("accepted code task store is unavailable")
        acceptance = self.acceptance_for_workflow_task(workflow_id, code_task_id)
        if acceptance is None:
            known_acceptance = connection.execute(
                "SELECT 1 FROM code_task_acceptances WHERE acceptance_id = ?",
                (acceptance_id,),
            ).fetchone()
            known_ingestion = connection.execute(
                "SELECT 1 FROM atlas_ingestion_outbox WHERE ingestion_key = ?",
                (ingestion_key,),
            ).fetchone()
            if known_acceptance is not None or known_ingestion is not None:
                raise AcceptanceConflictError(
                    "accepted code task identity does not match its public pair"
                )
            return None
        expected_payload = self._canonical_code_task_acceptance_payload(
            workflow_id=acceptance.workflow_id,
            task_id=acceptance.code_task_id,
            task_version=acceptance.code_task_version,
            input_snapshot_id=acceptance.input_snapshot_id,
            output_snapshot_id=acceptance.output_snapshot_id,
            indexed_diff_hash=acceptance.indexed_diff_hash,
            intent_id=acceptance.intent_id,
            language=acceptance.language,
            framework=acceptance.framework,
        )
        expected_payload_hash = _payload_hash(expected_payload)
        if (
            acceptance.acceptance_id != expected_payload_hash
            or acceptance.payload_hash != expected_payload_hash
            or acceptance_id != expected_payload_hash
        ):
            raise AcceptanceConflictError("accepted code task identity is inconsistent")

        outbox = connection.execute(
            """
            SELECT ingestion_key, acceptance_id, payload_hash
            FROM atlas_ingestion_outbox
            WHERE acceptance_id = ?
            """,
            (acceptance.acceptance_id,),
        ).fetchone()
        if outbox is None:
            raise AcceptanceEvidenceError("accepted code task outbox is unavailable")
        if (
            str(outbox["acceptance_id"]) != acceptance.acceptance_id
            or str(outbox["ingestion_key"]) != expected_payload_hash
            or str(outbox["payload_hash"]) != expected_payload_hash
            or ingestion_key != expected_payload_hash
        ):
            raise AcceptanceConflictError(
                "accepted code task ingestion identity changed"
            )

        try:
            task = self.get_task(code_task_id)
        except KeyError as error:
            raise AcceptanceEvidenceError(
                "accepted code task is unavailable"
            ) from error
        if (
            task.workflow_id != workflow_id
            or task.task_kind is not TaskKind.CODE
            or task.state is not TaskState.DONE
            or task.version != acceptance.code_task_version
            or task.intent_id != acceptance.intent_id
            or task.language != acceptance.language
            or task.framework != acceptance.framework
            or not task.write_scope
        ):
            raise AcceptanceConflictError("accepted code task metadata changed")

        index_binding = self.get_index_binding(code_task_id)
        if index_binding is None:
            raise AcceptanceEvidenceError(
                "accepted code task index binding is unavailable"
            )
        if (
            index_binding.input_snapshot_id != acceptance.input_snapshot_id
            or index_binding.output_snapshot_id != acceptance.output_snapshot_id
            or index_binding.indexed_diff_hash != acceptance.indexed_diff_hash
            or not index_binding.checkpoint_id
        ):
            raise AcceptanceConflictError("accepted code task index binding changed")

        binding_count = connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE workflow_id = ? AND task_id = ? AND event_type = ?
            """,
            (
                workflow_id,
                code_task_id,
                self._EVIDENCE_BINDING_EVENT_TYPE,
            ),
        ).fetchone()[0]
        if int(binding_count) == 0:
            raise AcceptanceEvidenceError(
                "accepted code task evidence binding is unavailable"
            )
        if int(binding_count) != 1:
            raise AcceptanceConflictError("accepted code task evidence binding changed")
        evidence_binding = self.evidence_binding_for_acceptance(
            acceptance.acceptance_id
        )
        if evidence_binding is None:
            raise AcceptanceEvidenceError(
                "accepted code task evidence binding is unavailable"
            )
        if (
            evidence_binding.workflow_id != workflow_id
            or evidence_binding.code_task_id != code_task_id
            or evidence_binding.code_task_version != acceptance.code_task_version
            or evidence_binding.input_snapshot_id != acceptance.input_snapshot_id
            or evidence_binding.output_snapshot_id != acceptance.output_snapshot_id
            or evidence_binding.indexed_diff_hash != acceptance.indexed_diff_hash
            or evidence_binding.checkpoint_id != index_binding.checkpoint_id
        ):
            raise AcceptanceConflictError("accepted code task evidence binding changed")

        try:
            task_evidence = self.task_acceptance_evidence(
                code_task_id, acceptance.output_snapshot_id
            )
        except StrictIndexError as error:
            raise AcceptanceEvidenceError(
                "accepted code task output evidence is unavailable"
            ) from error
        if (
            task_evidence.output_query_trace_id
            != evidence_binding.output_query_trace_id
        ):
            raise AcceptanceConflictError("accepted code task output evidence changed")
        if (
            task_evidence.verification_artifact_hashes
            != evidence_binding.verification_artifact_hashes
        ):
            if set(task_evidence.verification_artifact_hashes).issubset(
                evidence_binding.verification_artifact_hashes
            ):
                raise AcceptanceEvidenceError(
                    "accepted code task verification evidence is unavailable"
                )
            raise AcceptanceConflictError("accepted code task output evidence changed")

        attestation_count = connection.execute(
            "SELECT COUNT(*) FROM code_task_receipt_attestations WHERE task_id = ?",
            (code_task_id,),
        ).fetchone()[0]
        if int(attestation_count) == 0:
            raise AcceptanceEvidenceError(
                "accepted code task receipt attestation is unavailable"
            )
        if int(attestation_count) != 1:
            raise AcceptanceConflictError(
                "accepted code task receipt attestation changed"
            )
        receipt_attestation = self.code_task_receipt_attestation_for_acceptance(
            acceptance.acceptance_id
        )
        if receipt_attestation is None:
            raise AcceptanceEvidenceError(
                "accepted code task receipt attestation is unavailable"
            )
        if (
            receipt_attestation.workflow_id != workflow_id
            or receipt_attestation.code_task_id != code_task_id
            or receipt_attestation.code_task_version != acceptance.code_task_version
            or receipt_attestation.input_snapshot_id != acceptance.input_snapshot_id
            or receipt_attestation.output_snapshot_id != acceptance.output_snapshot_id
            or receipt_attestation.execution_receipt_ids
            != evidence_binding.execution_receipt_ids
            or receipt_attestation.attestation_hash
            not in evidence_binding.verification_artifact_hashes
        ):
            raise AcceptanceConflictError(
                "accepted code task receipt attestation changed"
            )
        if not task.result_hash:
            raise AcceptanceEvidenceError(
                "accepted code task result evidence is unavailable"
            )
        if task.result_hash not in evidence_binding.verification_artifact_hashes:
            raise AcceptanceConflictError("accepted code task result evidence changed")
        for artifact_hash in evidence_binding.verification_artifact_hashes:
            if (
                artifact_hash == receipt_attestation.attestation_hash
                and artifact_hash != task.result_hash
            ):
                continue
            artifact = self.get_artifact(artifact_hash)
            if artifact is None:
                raise AcceptanceEvidenceError(
                    "accepted code task verification evidence is unavailable"
                )
            if artifact.kind != "verification":
                raise AcceptanceConflictError(
                    "accepted code task verification evidence changed"
                )

        return AcceptedCodeTaskEvidence(
            acceptance=acceptance,
            task=task,
            index_binding=index_binding,
            task_evidence=task_evidence,
            evidence_binding=evidence_binding,
            receipt_attestation=receipt_attestation,
        )

    def pending_atlas_outbox(self, *, limit: int) -> tuple[AtlasOutboxItem, ...]:
        """Return pending ingestion work in deterministic creation/key order."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._MAX_ATLAS_OUTBOX_LIMIT
        ):
            raise ValueError(
                "outbox limit must be an integer between 1 and "
                f"{self._MAX_ATLAS_OUTBOX_LIMIT}"
            )
        rows = self._connection.execute(
            """
            SELECT * FROM atlas_ingestion_outbox
            WHERE state = ?
            ORDER BY created_at, ingestion_key
            LIMIT ?
            """,
            (AtlasOutboxState.PENDING.value, limit),
        ).fetchall()
        return tuple(self._atlas_outbox_from_row(row) for row in rows)

    def pending_ingestions(self, *, limit: int) -> tuple[AtlasOutboxItem, ...]:
        """Compatibility name for the durable, pending Atlas ingestion queue."""
        return self.pending_atlas_outbox(limit=limit)

    def mark_atlas_outbox_state(
        self,
        ingestion_key: str,
        state: AtlasOutboxState,
        *,
        error_code: str = "",
        reason_codes: tuple[str, ...] = (),
        now: str | None = None,
    ) -> AtlasOutboxItem:
        """Advance one pending outbox item or record a bounded pending retry."""
        if not isinstance(state, AtlasOutboxState):
            raise ValueError("outbox state must be an AtlasOutboxState")
        self._safe_acceptance_identifier("ingestion_key", ingestion_key)
        safe_error_code = self._safe_outbox_code(
            "error_code", error_code, allow_empty=True
        )
        safe_reason_codes = self._safe_outbox_reason_codes(reason_codes)
        if state is AtlasOutboxState.PENDING and not safe_error_code:
            raise ValueError("pending retry requires a stable error code")
        if state is AtlasOutboxState.PROJECTED and safe_error_code:
            raise ValueError("projected outbox state cannot retain an error code")
        if state is AtlasOutboxState.QUARANTINED and not safe_error_code:
            raise ValueError("quarantined outbox state requires a stable error code")
        updated_at = _utc_timestamp(now) if now is not None else _utc_now()

        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM atlas_ingestion_outbox WHERE ingestion_key = ?",
                (ingestion_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"atlas outbox item not found: {ingestion_key!r}")
            current = AtlasOutboxState(str(row["state"]))
            if current is not AtlasOutboxState.PENDING:
                if current is state:
                    return self._atlas_outbox_from_row(row)
                raise AtlasOutboxTransitionError(
                    f"terminal outbox item cannot change state: {ingestion_key!r}"
                )

            if state is AtlasOutboxState.PENDING:
                if int(row["attempt_count"]) >= self._MAX_ATLAS_OUTBOX_ATTEMPTS:
                    raise AtlasOutboxAttemptLimitError(
                        f"outbox retry limit reached: {ingestion_key!r}"
                    )
                cursor.execute(
                    """
                    UPDATE atlas_ingestion_outbox
                    SET attempt_count = attempt_count + 1,
                        last_error_code = ?,
                        reason_codes_json = ?,
                        updated_at = ?
                    WHERE ingestion_key = ?
                    """,
                    (
                        safe_error_code,
                        _encode_outbox_reason_codes(safe_reason_codes),
                        updated_at,
                        ingestion_key,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE atlas_ingestion_outbox
                    SET state = ?,
                        last_error_code = ?,
                        reason_codes_json = ?,
                        updated_at = ?
                    WHERE ingestion_key = ?
                    """,
                    (
                        state.value,
                        safe_error_code,
                        _encode_outbox_reason_codes(safe_reason_codes),
                        updated_at,
                        ingestion_key,
                    ),
                )
            updated_row = cursor.execute(
                "SELECT * FROM atlas_ingestion_outbox WHERE ingestion_key = ?",
                (ingestion_key,),
            ).fetchone()
        return self._atlas_outbox_from_row(updated_row)

    def mark_atlas_outbox_projected(
        self,
        ingestion_key: str,
        *,
        reason_codes: tuple[str, ...] = (),
        now: str | None = None,
    ) -> AtlasOutboxItem:
        """Mark a pending outbox item permanently projected."""
        return self.mark_atlas_outbox_state(
            ingestion_key,
            AtlasOutboxState.PROJECTED,
            reason_codes=reason_codes,
            now=now,
        )

    def mark_atlas_outbox_quarantined(
        self,
        ingestion_key: str,
        *,
        error_code: str,
        reason_codes: tuple[str, ...] = (),
        now: str | None = None,
    ) -> AtlasOutboxItem:
        """Mark a pending outbox item permanently quarantined."""
        return self.mark_atlas_outbox_state(
            ingestion_key,
            AtlasOutboxState.QUARANTINED,
            error_code=error_code,
            reason_codes=reason_codes,
            now=now,
        )

    def mark_atlas_outbox_retry(
        self,
        ingestion_key: str,
        *,
        error_code: str,
        reason_codes: tuple[str, ...] = (),
        now: str | None = None,
    ) -> AtlasOutboxItem:
        """Record one bounded retry while leaving the item pending."""
        return self.mark_atlas_outbox_state(
            ingestion_key,
            AtlasOutboxState.PENDING,
            error_code=error_code,
            reason_codes=reason_codes,
            now=now,
        )

    @classmethod
    def _build_atlas_finalization(
        cls,
        *,
        acceptance_id: str,
        ingestion_key: str,
        payload_hash: str,
        continuity_key_hash: str,
        view_id: str,
        fence_epoch: int,
        pointer_version: int,
        published_receipt_hash: str,
        atlas_receipt_digest: str,
        created_at: str,
    ) -> AtlasFinalization:
        """Build a private canonical certificate for a future attached finalizer."""
        payload = cls._atlas_finalization_payload(
            acceptance_id=acceptance_id,
            ingestion_key=ingestion_key,
            payload_hash=payload_hash,
            continuity_key_hash=continuity_key_hash,
            view_id=view_id,
            fence_epoch=fence_epoch,
            pointer_version=pointer_version,
            published_receipt_hash=published_receipt_hash,
            atlas_receipt_digest=atlas_receipt_digest,
            created_at=created_at,
        )
        finalization_hash = _domain_separated_hash(
            _ATLAS_FINALIZATION_DOMAIN, _canonical_payload_json(payload)
        )
        return AtlasFinalization(
            str(payload["schema_version"]),
            str(payload["acceptance_id"]),
            str(payload["ingestion_key"]),
            str(payload["payload_hash"]),
            str(payload["continuity_key_hash"]),
            str(payload["view_id"]),
            int(payload["fence_epoch"]),
            int(payload["pointer_version"]),
            str(payload["published_receipt_hash"]),
            str(payload["atlas_receipt_digest"]),
            finalization_hash,
            str(payload["created_at"]),
        )

    @classmethod
    def _atlas_finalization_payload(
        cls,
        *,
        acceptance_id: str,
        ingestion_key: str,
        payload_hash: str,
        continuity_key_hash: str,
        view_id: str,
        fence_epoch: int,
        pointer_version: int,
        published_receipt_hash: str,
        atlas_receipt_digest: str,
        created_at: str,
    ) -> dict[str, object]:
        def identifier(name: str, value: str) -> str:
            return cls._safe_acceptance_identifier(name, value)

        def hash_identifier(name: str, value: str) -> str:
            return cls._safe_sha256_identifier(name, value)

        if (
            isinstance(fence_epoch, bool)
            or not isinstance(fence_epoch, int)
            or not 0 < fence_epoch <= 2**63 - 1
        ):
            raise ValueError("fence_epoch must be a positive SQLite integer")
        if (
            isinstance(pointer_version, bool)
            or not isinstance(pointer_version, int)
            or not 0 < pointer_version <= 2**63 - 1
        ):
            raise ValueError("pointer_version must be a positive SQLite integer")
        return {
            "acceptance_id": hash_identifier("acceptance_id", acceptance_id),
            "atlas_receipt_digest": hash_identifier(
                "atlas_receipt_digest", atlas_receipt_digest
            ),
            "continuity_key_hash": hash_identifier(
                "continuity_key_hash", continuity_key_hash
            ),
            "created_at": _utc_timestamp(created_at),
            "fence_epoch": fence_epoch,
            "ingestion_key": hash_identifier("ingestion_key", ingestion_key),
            "payload_hash": hash_identifier("payload_hash", payload_hash),
            "pointer_version": pointer_version,
            "published_receipt_hash": hash_identifier(
                "published_receipt_hash", published_receipt_hash
            ),
            "schema_version": _ATLAS_FINALIZATION_SCHEMA_VERSION,
            "view_id": identifier("view_id", view_id),
        }

    @classmethod
    def _validate_atlas_finalization(
        cls, finalization: AtlasFinalization
    ) -> AtlasFinalization:
        if not isinstance(finalization, AtlasFinalization):
            raise ValueError("finalization must be an AtlasFinalization")
        expected = cls._build_atlas_finalization(
            acceptance_id=finalization.acceptance_id,
            ingestion_key=finalization.ingestion_key,
            payload_hash=finalization.payload_hash,
            continuity_key_hash=finalization.continuity_key_hash,
            view_id=finalization.view_id,
            fence_epoch=finalization.fence_epoch,
            pointer_version=finalization.pointer_version,
            published_receipt_hash=finalization.published_receipt_hash,
            atlas_receipt_digest=finalization.atlas_receipt_digest,
            created_at=finalization.created_at,
        )
        if finalization != expected:
            raise ValueError("atlas finalization is not canonical")
        return expected

    def _insert_atlas_finalization(
        self, cursor: sqlite3.Cursor, finalization: AtlasFinalization
    ) -> AtlasFinalization:
        """Insert through an invocation-owned cursor whose ``main`` is this store.

        The cursor may belong to an attached-continuity connection.  That is the
        required future UoW seam: ``main`` remains this prepared Orchestrator
        database while Continuity is attached alongside it in the same outer
        transaction.
        """

        if not isinstance(cursor, sqlite3.Cursor):
            raise StoreError("orchestrator finalization is not prepared")
        try:
            cursor_connection = cursor.connection
            cursor_main = _main_database_path(cursor_connection)
            if self._database is None or not cursor_main.samefile(self._database):
                raise StoreError("orchestrator finalization is not prepared")
            self.validate_prepared_connection(cursor_connection)
        except (OSError, sqlite3.DatabaseError, ValueError) as error:
            raise StoreError("orchestrator finalization is not prepared") from error
        candidate = self._validate_atlas_finalization(finalization)
        try:
            outbox = cursor.execute(
                """
                SELECT 1
                FROM main.atlas_ingestion_outbox
                WHERE acceptance_id = ?
                  AND ingestion_key = ?
                  AND payload_hash = ?
                  AND state = ?
                """,
                (
                    candidate.acceptance_id,
                    candidate.ingestion_key,
                    candidate.payload_hash,
                    AtlasOutboxState.PROJECTED.value,
                ),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise StoreError("orchestrator finalization is not prepared") from error
        if outbox is None:
            raise StoreError("orchestrator finalization identity mismatch")
        try:
            cursor.execute(
                """
                INSERT INTO main.atlas_finalizations (
                    schema_version, acceptance_id, ingestion_key, payload_hash,
                    continuity_key_hash, view_id, fence_epoch, pointer_version,
                    published_receipt_hash, atlas_receipt_digest, finalization_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.schema_version,
                    candidate.acceptance_id,
                    candidate.ingestion_key,
                    candidate.payload_hash,
                    candidate.continuity_key_hash,
                    candidate.view_id,
                    candidate.fence_epoch,
                    candidate.pointer_version,
                    candidate.published_receipt_hash,
                    candidate.atlas_receipt_digest,
                    candidate.finalization_hash,
                    candidate.created_at,
                ),
            )
            row = cursor.execute(
                "SELECT * FROM main.atlas_finalizations WHERE finalization_hash = ?",
                (candidate.finalization_hash,),
            ).fetchone()
        except sqlite3.IntegrityError as error:
            raise StoreError("orchestrator finalization conflict") from error
        if row is None:
            raise StoreError("orchestrator finalization is not prepared")
        return self._atlas_finalization_from_row(row)

    def _atlas_finalization_for_acceptance(
        self, acceptance_id: str
    ) -> AtlasFinalization | None:
        """Private read seam for an attached finalizer's idempotency check."""
        acceptance_id = self._safe_sha256_identifier("acceptance_id", acceptance_id)
        row = self._connection.execute(
            """
            SELECT finalization.*, outbox.state AS outbox_state
            FROM main.atlas_finalizations AS finalization
            LEFT JOIN main.atlas_ingestion_outbox AS outbox
                ON outbox.acceptance_id = finalization.acceptance_id
               AND outbox.ingestion_key = finalization.ingestion_key
               AND outbox.payload_hash = finalization.payload_hash
            WHERE finalization.acceptance_id = ?
            """,
            (acceptance_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            if str(row["outbox_state"]) != AtlasOutboxState.PROJECTED.value:
                raise StoreError("orchestrator finalization identity mismatch")
            finalization = self._atlas_finalization_from_row(row)
            return self._validate_atlas_finalization(finalization)
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError("orchestrator finalization is not prepared") from error

    def dependencies_for(self, task_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT dependency_id FROM task_dependencies WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        return tuple(str(row["dependency_id"]) for row in rows)

    def update_task_state(
        self, task_id: str, state: TaskState, *, expected_version: int
    ) -> Task:
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE tasks
                SET state = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (state.value, task_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError(f"task version is not current: {task_id!r}")
            row = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row)

    def complete_task(
        self,
        task_id: str,
        state: TaskState,
        expected_version: int,
        owner: str,
        epoch: int,
        *,
        result_hash: str | None = None,
        receipt_attestation: CodeTaskReceiptAttestation | None = None,
        now: str | None = None,
    ) -> Task:
        """Complete a task only when the caller still owns the supplied epoch."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            task_row = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"task not found: {task_id!r}")
            workflow = cursor.execute(
                """
                SELECT workflows.state
                FROM workflows JOIN tasks ON tasks.workflow_id = workflows.id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
            if (
                workflow is not None
                and workflow["state"] == WorkflowState.CANCELLED.value
            ):
                raise WorkflowCancelledError(
                    f"workflow is cancelled for task {task_id!r}"
                )
            is_code_completion = (
                state is TaskState.DONE
                and str(task_row["task_kind"]) == TaskKind.CODE.value
            )
            final_version = expected_version
            if is_code_completion:
                if (
                    isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)
                    or not 0 <= expected_version < 2**63 - 1
                ):
                    raise VersionConflictError(
                        f"task version is not current: {task_id!r}"
                    )
                final_version += 1

            if (
                is_code_completion
                and str(task_row["state"]) == TaskState.DONE.value
                and int(task_row["version"]) == final_version
            ):
                if receipt_attestation is None:
                    raise AcceptanceEvidenceError(
                        "code task receipt attestation is required"
                    )
                binding = self._require_index_binding(cursor, task_id)
                self._validate_completion_receipt_attestation(
                    receipt_attestation,
                    task_row=task_row,
                    binding=binding,
                    final_version=final_version,
                )
                if (
                    result_hash is not None
                    and str(task_row["result_hash"]) != result_hash
                ):
                    raise AcceptanceConflictError(
                        "completed task result hash does not match retry"
                    )
                persisted_row = cursor.execute(
                    """
                    SELECT * FROM code_task_receipt_attestations
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if persisted_row is None:
                    raise AcceptanceConflictError(
                        "completed code task has no receipt attestation"
                    )
                persisted = self._code_task_receipt_attestation_from_row(persisted_row)
                if persisted != receipt_attestation:
                    raise AcceptanceConflictError(
                        "completed task receipt attestation does not match retry"
                    )
                return self._task_from_row(task_row)

            if is_code_completion and str(task_row["state"]) != TaskState.RUNNING.value:
                raise InvalidTaskStateError(
                    f"task must be running before completion: {task_id!r}"
                )
            if int(task_row["version"]) != expected_version:
                raise VersionConflictError(f"task version is not current: {task_id!r}")
            self._require_strict_completion(cursor, task_id)

            if is_code_completion:
                if receipt_attestation is None:
                    raise AcceptanceEvidenceError(
                        "code task receipt attestation is required"
                    )
                binding = self._require_index_binding(cursor, task_id)
                self._validate_completion_receipt_attestation(
                    receipt_attestation,
                    task_row=task_row,
                    binding=binding,
                    final_version=final_version,
                )

            if is_code_completion:
                cursor.execute(
                    """
                    UPDATE tasks
                    SET state = ?, result_hash = COALESCE(?, result_hash),
                        version = version + 1
                    WHERE id = ? AND state = ? AND version = ?
                    """,
                    (
                        state.value,
                        result_hash,
                        task_id,
                        TaskState.RUNNING.value,
                        expected_version,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE tasks
                    SET state = ?, result_hash = COALESCE(?, result_hash),
                        version = version + 1
                    WHERE id = ? AND version = ?
                    """,
                    (state.value, result_hash, task_id, expected_version),
                )
            if cursor.rowcount != 1:
                raise VersionConflictError(f"task version is not current: {task_id!r}")
            if is_code_completion:
                self._insert_code_task_receipt_attestation(
                    cursor, receipt_attestation, created_at=now_utc
                )
            row = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row)

    def _validate_completion_receipt_attestation(
        self,
        receipt_attestation: CodeTaskReceiptAttestation,
        *,
        task_row: sqlite3.Row,
        binding: sqlite3.Row,
        final_version: int,
    ) -> None:
        try:
            self._validate_code_task_receipt_attestation(
                receipt_attestation,
                workflow_id=str(task_row["workflow_id"]),
                task_id=str(task_row["id"]),
                task_version=final_version,
                input_snapshot_id=str(binding["input_snapshot_id"]),
                output_snapshot_id=str(binding["output_snapshot_id"]),
            )
        except ValueError as error:
            raise AcceptanceEvidenceError(
                "code task receipt attestation is invalid"
            ) from error

    def _insert_code_task_receipt_attestation(
        self,
        cursor: sqlite3.Cursor,
        receipt_attestation: CodeTaskReceiptAttestation,
        *,
        created_at: str,
    ) -> None:
        existing = cursor.execute(
            "SELECT * FROM code_task_receipt_attestations WHERE task_id = ?",
            (receipt_attestation.code_task_id,),
        ).fetchone()
        if existing is not None:
            persisted = self._code_task_receipt_attestation_from_row(existing)
            if persisted != receipt_attestation:
                raise AcceptanceConflictError(
                    "code task already has a different receipt attestation"
                )
            return
        try:
            cursor.execute(
                """
                INSERT INTO code_task_receipt_attestations (
                    task_id, workflow_id, code_task_version, input_snapshot_id,
                    output_snapshot_id, workspace_hash, execution_receipt_ids,
                    attestation_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_attestation.code_task_id,
                    receipt_attestation.workflow_id,
                    receipt_attestation.code_task_version,
                    receipt_attestation.input_snapshot_id,
                    receipt_attestation.output_snapshot_id,
                    receipt_attestation.workspace_hash,
                    _encode_strings(receipt_attestation.execution_receipt_ids),
                    receipt_attestation.attestation_hash,
                    created_at,
                ),
            )
            for receipt_id in receipt_attestation.execution_receipt_ids:
                cursor.execute(
                    """
                    INSERT INTO code_task_receipt_owners (
                        receipt_id, task_id, code_task_version, attestation_hash
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        receipt_attestation.code_task_id,
                        receipt_attestation.code_task_version,
                        receipt_attestation.attestation_hash,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise AcceptanceConflictError(
                "execution receipt is already owned by another code task"
            ) from error

    def cancel_workflow(
        self, workflow_id: str, *, expected_version: int | None = None
    ) -> tuple[Workflow, tuple[Task, ...]]:
        """Cancel a workflow and its nonterminal tasks in one transaction."""
        terminal_states = (
            TaskState.DONE.value,
            TaskState.BLOCKED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        )
        with self._transaction() as cursor:
            workflow = cursor.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise KeyError(f"workflow not found: {workflow_id!r}")
            if (
                expected_version is not None
                and int(workflow["version"]) != expected_version
            ):
                raise VersionConflictError(
                    f"workflow version is not current: {workflow_id!r}"
                )
            if workflow["state"] != WorkflowState.CANCELLED.value:
                cursor.execute(
                    """
                    UPDATE workflows
                    SET state = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        WorkflowState.CANCELLED.value,
                        _utc_now(),
                        workflow_id,
                        workflow["version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise VersionConflictError(
                        f"workflow version is not current: {workflow_id!r}"
                    )
                cursor.execute(
                    """
                    UPDATE tasks SET state = ?, version = version + 1
                    WHERE workflow_id = ? AND state NOT IN (?, ?, ?, ?)
                    """,
                    (TaskState.CANCELLED.value, workflow_id, *terminal_states),
                )
            workflow = cursor.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            task_rows = cursor.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
        tasks = tuple(
            self._task_from_row(row, self.dependencies_for(row["id"]))
            for row in task_rows
        )
        return self._workflow_from_row(workflow), tasks

    def put_artifact(
        self,
        kind: str,
        content_hash: str,
        safe_path: str,
        size: int,
        redaction_version: str,
    ) -> Artifact:
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM artifacts WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO artifacts
                        (kind, content_hash, safe_path, size, redaction_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        content_hash,
                        safe_path,
                        size,
                        redaction_version,
                        _utc_now(),
                    ),
                )
                row = cursor.execute(
                    "SELECT * FROM artifacts WHERE content_hash = ?", (content_hash,)
                ).fetchone()
        return self._artifact_from_row(row)

    def get_artifact(self, content_hash: str) -> Artifact | None:
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else self._artifact_from_row(row)

    def register_task_artifact(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        kind: str,
        content_hash: str,
        safe_path: str,
        size: int,
        redaction_version: str,
        snapshot_id: str | None = None,
        now: str | None = None,
    ) -> Artifact:
        """Register task-owned artifact metadata after validating the task lease."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            binding = self._optional_index_binding(cursor, task_id)
            task_context = cursor.execute(
                """
                SELECT
                    workflows.state AS workflow_state,
                    tasks.state AS task_state
                FROM workflows JOIN tasks ON tasks.workflow_id = workflows.id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
            if (
                task_context is not None
                and task_context["workflow_state"] == WorkflowState.CANCELLED.value
            ):
                raise WorkflowCancelledError(
                    f"workflow is cancelled for task {task_id!r}"
                )
            if (
                kind == "verification"
                and task_context is not None
                and task_context["task_state"] != TaskState.RUNNING.value
            ):
                raise InvalidTaskStateError(
                    f"verification evidence requires a running task: {task_id!r}"
                )
            artifact = cursor.execute(
                "SELECT * FROM artifacts WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if artifact is None:
                cursor.execute(
                    """
                    INSERT INTO artifacts
                        (kind, content_hash, safe_path, size, redaction_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        content_hash,
                        safe_path,
                        size,
                        redaction_version,
                        _utc_now(),
                    ),
                )
                artifact = cursor.execute(
                    "SELECT * FROM artifacts WHERE content_hash = ?", (content_hash,)
                ).fetchone()
            elif (
                artifact["kind"] != kind
                or artifact["safe_path"] != safe_path
                or int(artifact["size"]) != size
                or artifact["redaction_version"] != redaction_version
            ):
                raise ArtifactConflictError(
                    f"artifact metadata conflicts for {content_hash!r}"
                )
            artifact_owner = cursor.execute(
                "SELECT task_id FROM artifact_owners WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if artifact_owner is None:
                cursor.execute(
                    "INSERT INTO artifact_owners (content_hash, task_id) VALUES (?, ?)",
                    (content_hash, task_id),
                )
            elif artifact_owner["task_id"] != task_id:
                raise ArtifactConflictError(
                    f"artifact owner conflicts for {content_hash!r}"
                )
            if binding is not None and kind == "verification":
                expected_snapshot = str(binding["output_snapshot_id"])
                if not snapshot_id or snapshot_id != expected_snapshot:
                    raise StrictIndexError("SNAPSHOT_MISMATCH")
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO task_index_verification_artifacts
                        (task_id, content_hash, snapshot_id)
                    VALUES (?, ?, ?)
                    """,
                    (task_id, content_hash, snapshot_id),
                )
                self._append_binding_event(
                    cursor,
                    task_id,
                    "verification",
                    snapshot_id=snapshot_id,
                    trace_id=content_hash,
                )
        return self._artifact_from_row(artifact)

    def record_task_input(self, task_id: str, input_hash: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO task_inputs (task_id, input_hash) VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET input_hash = excluded.input_hash
                """,
                (task_id, input_hash),
            )

    def find_completed_task(
        self, workflow_id: str, *, input_hash: str, policy_version: str
    ) -> Task | None:
        row = self._connection.execute(
            """
            SELECT tasks.*
            FROM tasks
            JOIN workflows ON workflows.id = tasks.workflow_id
            JOIN task_inputs ON task_inputs.task_id = tasks.id
            WHERE tasks.workflow_id = ?
                AND task_inputs.input_hash = ?
                AND workflows.policy_version = ?
                AND tasks.state = ?
            ORDER BY tasks.id
            LIMIT 1
            """,
            (workflow_id, input_hash, policy_version, TaskState.DONE.value),
        ).fetchone()
        return (
            None
            if row is None
            else self._task_from_row(row, self.dependencies_for(row["id"]))
        )

    def promote_ready_tasks(self, workflow_id: str) -> tuple[Task, ...]:
        """Move the next dependency-satisfied DAG wave from NEW to READY."""
        with self._transaction() as cursor:
            workflow = cursor.execute(
                "SELECT state FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise KeyError(f"workflow not found: {workflow_id!r}")
            if workflow["state"] == WorkflowState.CANCELLED.value:
                return ()
            rows = cursor.execute(
                """
                SELECT tasks.*
                FROM tasks
                WHERE tasks.workflow_id = ? AND tasks.state = ?
                    AND NOT EXISTS (
                        SELECT 1
                        FROM task_dependencies
                        JOIN tasks AS dependencies ON dependencies.id = task_dependencies.dependency_id
                        WHERE task_dependencies.task_id = tasks.id
                            AND dependencies.state != ?
                    )
                ORDER BY tasks.rowid
                """,
                (workflow_id, TaskState.NEW.value, TaskState.DONE.value),
            ).fetchall()
            task_ids = tuple(row["id"] for row in rows)
            for task_id in task_ids:
                cursor.execute(
                    """
                    UPDATE tasks SET state = ?, version = version + 1
                    WHERE id = ? AND state = ?
                    """,
                    (TaskState.READY.value, task_id, TaskState.NEW.value),
                )
            if not task_ids:
                return ()
            rows = cursor.execute(
                "SELECT * FROM tasks WHERE id IN ({}) ORDER BY rowid".format(
                    ",".join("?" for _ in task_ids)
                ),
                task_ids,
            ).fetchall()
        return tuple(
            self._task_from_row(row, self.dependencies_for(row["id"])) for row in rows
        )

    def claim_task(
        self,
        task_id: str,
        owner: str,
        expires_at: str,
        *,
        host_target: str | None = None,
        now: str | None = None,
    ) -> tuple[Task, Lease]:
        """Claim a ready task, or take over a running task after lease expiry."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        expiry_utc = _utc_timestamp(expires_at)
        normalized_target = self._validate_host_target(host_target)
        with self._transaction() as cursor:
            task = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"task not found: {task_id!r}")
            self._optional_index_binding(cursor, task_id)
            workflow = cursor.execute(
                "SELECT state FROM workflows WHERE id = ?", (task["workflow_id"],)
            ).fetchone()
            if workflow["state"] == WorkflowState.CANCELLED.value:
                raise WorkflowCancelledError(
                    f"workflow is cancelled for task {task_id!r}"
                )
            if task["state"] == TaskState.READY.value:
                lease = self._acquire_lease_in_transaction(
                    cursor, task_id, owner, expiry_utc, now_utc, normalized_target
                )
                cursor.execute(
                    """
                    UPDATE tasks SET state = ?, version = version + 1
                    WHERE id = ? AND state = ?
                    """,
                    (TaskState.RUNNING.value, task_id, TaskState.READY.value),
                )
                if cursor.rowcount != 1:
                    raise InvalidTaskStateError(f"task is not ready: {task_id!r}")
            elif task["state"] == TaskState.RUNNING.value:
                existing_lease = cursor.execute(
                    "SELECT expires_at FROM leases WHERE task_id = ?", (task_id,)
                ).fetchone()
                if existing_lease is None:
                    raise InvalidTaskStateError(
                        f"task is not ready for lease recovery: {task_id!r}"
                    )
                if str(existing_lease["expires_at"]) > now_utc:
                    raise LeaseConflictError(
                        f"task is leased until {existing_lease['expires_at']!r}: {task_id!r}"
                    )
                lease = self._acquire_lease_in_transaction(
                    cursor, task_id, owner, expiry_utc, now_utc, normalized_target
                )
            else:
                raise InvalidTaskStateError(f"task is not ready: {task_id!r}")
            task = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(task, self.dependencies_for(task_id)), lease

    def write_scope_conflicts(
        self, workflow_id: str
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        terminal_states = (
            TaskState.DONE.value,
            TaskState.BLOCKED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        )
        rows = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE workflow_id = ? AND state NOT IN (?, ?, ?, ?)
            ORDER BY id
            """,
            (workflow_id, *terminal_states),
        ).fetchall()
        tasks = tuple(self._task_from_row(row) for row in rows)
        conflicts: list[tuple[str, str, tuple[str, ...]]] = []
        for index, task in enumerate(tasks):
            for other in tasks[index + 1 :]:
                overlap = tuple(
                    sorted(set(task.write_scope).intersection(other.write_scope))
                )
                if overlap:
                    conflicts.append((task.id, other.id, overlap))
        return tuple(conflicts)

    def append_event(
        self,
        workflow_id: str,
        task_id: str | None,
        event_type: str,
        redacted_payload: str,
        payload_hash: str,
    ) -> Event:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO events
                    (workflow_id, task_id, event_type, redacted_payload, payload_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    task_id,
                    event_type,
                    redacted_payload,
                    payload_hash,
                    _utc_now(),
                ),
            )
            sequence = int(cursor.lastrowid)
            row = cursor.execute(
                "SELECT * FROM events WHERE sequence = ?", (sequence,)
            ).fetchone()
        return self._event_from_row(row)

    def list_events(self, workflow_id: str) -> tuple[Event, ...]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE workflow_id = ? ORDER BY sequence",
            (workflow_id,),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def acquire_lease(
        self,
        task_id: str,
        owner: str,
        expires_at: str,
        *,
        host_target: str | None = None,
        now: str | None = None,
    ) -> Lease:
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        expiry_utc = _utc_timestamp(expires_at)
        normalized_target = self._validate_host_target(host_target)
        with self._transaction() as cursor:
            self._optional_index_binding(cursor, task_id)
            return self._acquire_lease_in_transaction(
                cursor, task_id, owner, expiry_utc, now_utc, normalized_target
            )

    def bind_host_target(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        host_target: str,
        *,
        now: str | None = None,
    ) -> Lease:
        """Bind a live Codex collaboration target to the current lease epoch."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        normalized_target = self._validate_host_target(host_target)
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            cursor.execute(
                "UPDATE leases SET host_target = ?, heartbeat_at = ? WHERE task_id = ?",
                (normalized_target, now_utc, task_id),
            )
            row = cursor.execute(
                "SELECT * FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._lease_from_row(row)

    def renew_lease(
        self,
        task_id: str,
        owner: str,
        epoch: int,
        expires_at: str,
        *,
        now: str | None = None,
    ) -> Lease:
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        expiry_utc = _utc_timestamp(expires_at)
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (
                row is None
                or row["owner"] != owner
                or int(row["epoch"]) != epoch
                or str(row["expires_at"]) <= now_utc
            ):
                raise StaleLeaseError(f"lease is stale for task {task_id!r}")
            cursor.execute(
                "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE task_id = ?",
                (expiry_utc, now_utc, task_id),
            )
            row = cursor.execute(
                "SELECT * FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._lease_from_row(row)

    def release_lease(self, task_id: str, owner: str, epoch: int) -> None:
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch)
            cursor.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))

    def get_lease(self, task_id: str) -> Lease | None:
        row = self._connection.execute(
            "SELECT * FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else self._lease_from_row(row)

    @staticmethod
    def _require_task_in_workflow(
        cursor: sqlite3.Cursor,
        workflow_id: str,
        task_id: str,
        error_type: type[Exception],
    ) -> None:
        row = cursor.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND workflow_id = ?",
            (task_id, workflow_id),
        ).fetchone()
        if row is None:
            raise error_type(f"task is not in workflow: {task_id!r}")

    @staticmethod
    def _peer_relationships(
        cursor: sqlite3.Cursor, workflow_id: str, task_id: str
    ) -> tuple[tuple[str, str], ...]:
        relationships: dict[str, str] = {}
        dependency_rows = cursor.execute(
            """
            SELECT dependency_id AS peer_id
            FROM task_dependencies
            JOIN tasks ON tasks.id = task_dependencies.task_id
            WHERE task_dependencies.task_id = ? AND tasks.workflow_id = ?
            UNION
            SELECT task_id AS peer_id
            FROM task_dependencies
            JOIN tasks ON tasks.id = task_dependencies.task_id
            WHERE task_dependencies.dependency_id = ? AND tasks.workflow_id = ?
            """,
            (task_id, workflow_id, task_id, workflow_id),
        ).fetchall()
        for row in dependency_rows:
            relationships[str(row["peer_id"])] = "dependency_edge"
        subscription_rows = cursor.execute(
            """
            SELECT DISTINCT subscribed.task_id AS peer_id
            FROM task_contract_subscriptions AS current
            JOIN task_contract_subscriptions AS subscribed
                ON subscribed.contract_hash = current.contract_hash
            JOIN tasks ON tasks.id = subscribed.task_id
            WHERE current.task_id = ? AND tasks.workflow_id = ? AND subscribed.task_id != ?
            """,
            (task_id, workflow_id, task_id),
        ).fetchall()
        for row in subscription_rows:
            relationships.setdefault(str(row["peer_id"]), "contract_subscriber")
        return tuple(
            (peer_id, relationships[peer_id]) for peer_id in sorted(relationships)
        )

    @classmethod
    def _role_direction(
        cls, value: RoleEnvelopeDirection | str
    ) -> RoleEnvelopeDirection:
        try:
            return RoleEnvelopeDirection(value)
        except (TypeError, ValueError) as error:
            raise RoleEnvelopeInvalidError("role envelope direction is not supported") from error

    @classmethod
    def _safe_role_identifier(cls, value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= cls._MAX_ROLE_TOKEN_LENGTH
            and cls._SAFE_ROLE_IDENTIFIER_PATTERN.fullmatch(value) is not None
        )

    @classmethod
    def _require_role_hash(cls, value: object, label: str) -> str:
        if not isinstance(value, str) or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise RoleEnvelopeInvalidError(f"{label} must be a sha256 reference")
        return value

    @classmethod
    def _assignment_token_hash(cls, value: object) -> str:
        """Accept only a content-addressed assignment token, never a bearer value."""

        return cls._require_role_hash(value, "assignment token")

    @classmethod
    def _role_hashes(cls, value: object, label: str) -> tuple[str, ...]:
        if not isinstance(value, tuple) or len(value) > cls._MAX_ROLE_REFERENCE_COUNT:
            raise RoleEnvelopeInvalidError(f"{label} is outside the bounded schema")
        hashes = tuple(cls._require_role_hash(item, label) for item in value)
        if len(set(hashes)) != len(hashes):
            raise RoleEnvelopeInvalidError(f"{label} contains duplicate references")
        return hashes

    @classmethod
    def _role_risk_items(
        cls, value: object
    ) -> tuple[RoleRiskItem, ...]:
        if not isinstance(value, tuple) or len(value) > cls._MAX_ROLE_RISK_ITEMS:
            raise RoleEnvelopeInvalidError("risk items are outside the bounded schema")
        items: list[RoleRiskItem] = []
        for item in value:
            if isinstance(item, RoleRiskItem):
                code, severity, evidence_hash = (
                    item.code,
                    item.severity,
                    item.evidence_hash,
                )
            elif isinstance(item, Mapping) and set(item) == {
                "code",
                "severity",
                "evidence_hash",
            }:
                code = item["code"]
                severity = item["severity"]
                evidence_hash = item["evidence_hash"]
            else:
                raise RoleEnvelopeInvalidError("risk item schema is not exact")
            if (
                not isinstance(code, str)
                or cls._SAFE_RISK_CODE_PATTERN.fullmatch(code) is None
                or not isinstance(severity, str)
                or severity not in cls._ROLE_RISK_SEVERITIES
            ):
                raise RoleEnvelopeInvalidError("risk item is not a bounded reference")
            items.append(
                RoleRiskItem(code, severity, cls._require_role_hash(evidence_hash, "risk evidence"))
            )
        if len({item.code for item in items}) != len(items):
            raise RoleEnvelopeInvalidError("risk item codes must be unique")
        return tuple(items)

    def _role_coordinator_binding(
        self,
        cursor: sqlite3.Cursor,
        *,
        workflow_id: str,
        direction: RoleEnvelopeDirection,
        sender_task_id: str,
        sender_epoch: int,
        recipient_task_id: str,
        recipient_epoch: int,
        coordinator_task_id: str | None,
        coordinator_epoch: int | None,
        now: str,
    ) -> tuple[str, int]:
        if direction is RoleEnvelopeDirection.COORDINATOR_TO_WORKER:
            if coordinator_task_id not in (None, sender_task_id) or coordinator_epoch not in (
                None,
                sender_epoch,
            ):
                raise RoleEnvelopeInvalidError("coordinator binding does not match sender")
            return sender_task_id, sender_epoch
        if direction is RoleEnvelopeDirection.WORKER_TO_COORDINATOR:
            if coordinator_task_id not in (None, recipient_task_id) or coordinator_epoch not in (
                None,
                recipient_epoch,
            ):
                raise RoleEnvelopeInvalidError("coordinator binding does not match recipient")
            return recipient_task_id, recipient_epoch
        if (
            not self._safe_role_identifier(coordinator_task_id)
            or not isinstance(coordinator_epoch, int)
            or isinstance(coordinator_epoch, bool)
            or coordinator_epoch < 1
        ):
            raise RoleEnvelopeInvalidError("peer envelope needs an exact coordinator binding")
        self._require_task_in_workflow(
            cursor, workflow_id, coordinator_task_id, RoleEnvelopeForbiddenError
        )
        self._require_current_recipient_lease(
            cursor, coordinator_task_id, coordinator_epoch, now=now
        )
        row = cursor.execute(
            "SELECT owner_role FROM tasks WHERE id = ?", (coordinator_task_id,)
        ).fetchone()
        if row is None or str(row["owner_role"]) != "coordinator":
            raise RoleEnvelopeForbiddenError("peer coordinator is not authoritative")
        return coordinator_task_id, coordinator_epoch

    @classmethod
    def _require_role_task_roles(
        cls,
        cursor: sqlite3.Cursor,
        *,
        sender_task_id: str,
        sender_role: str,
        recipient_task_id: str,
        recipient_role: str,
        direction: RoleEnvelopeDirection,
    ) -> None:
        expected = {
            RoleEnvelopeDirection.COORDINATOR_TO_WORKER: ("coordinator", "worker"),
            RoleEnvelopeDirection.WORKER_TO_COORDINATOR: ("worker", "coordinator"),
            RoleEnvelopeDirection.PEER_TO_PEER: ("worker", "worker"),
        }[direction]
        if (sender_role, recipient_role) != expected:
            raise RoleEnvelopeInvalidError("role direction does not match sender and recipient")
        rows = cursor.execute(
            "SELECT id, owner_role FROM tasks WHERE id IN (?, ?)",
            (sender_task_id, recipient_task_id),
        ).fetchall()
        roles = {str(row["id"]): str(row["owner_role"]) for row in rows}
        if roles.get(sender_task_id) != sender_role or roles.get(recipient_task_id) != recipient_role:
            raise RoleEnvelopeForbiddenError("task role is not authoritative")

    @staticmethod
    def _require_current_recipient_lease(
        cursor: sqlite3.Cursor, task_id: str, epoch: int, *, now: str
    ) -> None:
        if not isinstance(epoch, int) or isinstance(epoch, bool):
            raise StaleLeaseError(f"lease is stale for task {task_id!r}")
        row = cursor.execute(
            "SELECT epoch, expires_at FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if (
            row is None
            or int(row["epoch"]) != epoch
            or str(row["expires_at"]) <= now
        ):
            raise StaleLeaseError(f"lease is stale for task {task_id!r}")

    def _require_role_peer_capability(
        self,
        cursor: sqlite3.Cursor,
        *,
        workflow_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        capability: str | None,
    ) -> str:
        relationship = dict(
            self._peer_relationships(cursor, workflow_id, sender_task_id)
        ).get(recipient_task_id)
        if relationship is None:
            raise PeerForbiddenError("task is not an authorized peer")
        row = cursor.execute(
            """
            SELECT capability FROM peer_capabilities
            WHERE workflow_id = ? AND sender_task_id = ? AND recipient_task_id = ?
                AND relationship = ?
            """,
            (workflow_id, sender_task_id, recipient_task_id, relationship),
        ).fetchone()
        if (
            row is None
            or not isinstance(capability, str)
            or not secrets.compare_digest(str(row["capability"]), capability)
        ):
            raise CapabilityInvalidError("delivery capability is not valid for this peer")
        return _payload_hash(capability)

    def _role_envelope_payload(
        self,
        *,
        direction: RoleEnvelopeDirection,
        workflow_id: str,
        sender_task_id: str,
        sender_role: str,
        sender_epoch: int,
        recipient_task_id: str,
        recipient_role: str,
        recipient_epoch: int,
        coordinator_task_id: str,
        coordinator_epoch: int,
        assignment_token: str,
        dispatch_context_hash: str,
        route_provenance_hash: str,
        correlation_id: str,
        task_card_hash: str,
        contract_hashes: tuple[str, ...],
        index_evidence_hashes: tuple[str, ...],
        terminal_result_hash: str,
        evidence_hashes: tuple[str, ...],
        dependency_hashes: tuple[str, ...],
        risk_items: tuple[RoleRiskItem | Mapping[str, str], ...],
        recipient_capability_hash: str,
        issued_at: str,
        expires_at: str,
    ) -> dict[str, object]:
        if (
            not self._safe_role_identifier(workflow_id)
            or not self._safe_role_identifier(sender_task_id)
            or not self._safe_role_identifier(recipient_task_id)
            or not self._safe_role_identifier(coordinator_task_id)
            or not self._safe_role_identifier(correlation_id)
            or len(correlation_id) > self._MAX_ROLE_CORRELATION_LENGTH
            or not isinstance(sender_epoch, int)
            or isinstance(sender_epoch, bool)
            or sender_epoch < 1
            or not isinstance(recipient_epoch, int)
            or isinstance(recipient_epoch, bool)
            or recipient_epoch < 1
            or not isinstance(coordinator_epoch, int)
            or isinstance(coordinator_epoch, bool)
            or coordinator_epoch < 1
        ):
            raise RoleEnvelopeInvalidError("role envelope binding is not safe")
        assignment_token_hash = self._assignment_token_hash(assignment_token)
        self._require_role_hash(dispatch_context_hash, "dispatch context hash")
        self._require_role_hash(route_provenance_hash, "route provenance hash")
        card_hash = ""
        if task_card_hash:
            card_hash = self._require_role_hash(task_card_hash, "task card hash")
        contracts = self._role_hashes(contract_hashes, "contract references")
        index_evidence = self._role_hashes(index_evidence_hashes, "index evidence")
        evidence = self._role_hashes(evidence_hashes, "evidence references")
        dependencies = self._role_hashes(dependency_hashes, "dependency references")
        terminal = ""
        if terminal_result_hash:
            terminal = self._require_role_hash(terminal_result_hash, "terminal result")
        risks = self._role_risk_items(risk_items)
        if not {risk.evidence_hash for risk in risks}.issubset(set(evidence)):
            raise RoleEnvelopeInvalidError("risk evidence must be an envelope evidence reference")
        if direction is RoleEnvelopeDirection.COORDINATOR_TO_WORKER:
            if (
                not card_hash
                or not contracts
                or not index_evidence
                or terminal
                or evidence
                or dependencies
                or risks
                or recipient_capability_hash
            ):
                raise RoleEnvelopeInvalidError("coordinator envelope schema is not exact")
        elif direction is RoleEnvelopeDirection.WORKER_TO_COORDINATOR:
            if (
                card_hash
                or contracts
                or index_evidence
                or not terminal
                or not evidence
                or dependencies
                or recipient_capability_hash
            ):
                raise RoleEnvelopeInvalidError("worker envelope schema is not exact")
        elif (
            card_hash
            or contracts
            or index_evidence
            or terminal
            or not evidence
            or not dependencies
            or risks
            or not recipient_capability_hash
        ):
            raise RoleEnvelopeInvalidError("peer envelope schema is not exact")
        fence_payload = {
            "schema_version": self._ROLE_ENVELOPE_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "sender_task_id": sender_task_id,
            "recipient_task_id": recipient_task_id,
            "correlation_id": correlation_id,
            "assignment_token_hash": assignment_token_hash,
            "dispatch_context_hash": dispatch_context_hash,
            "route_provenance_hash": route_provenance_hash,
            "coordinator_task_id": coordinator_task_id,
            "coordinator_epoch": coordinator_epoch,
            "recipient_capability_hash": recipient_capability_hash,
        }
        correlation_fence_hash = _payload_hash(_canonical_payload_json(fence_payload))
        return {
            "schema_version": self._ROLE_ENVELOPE_SCHEMA_VERSION,
            "direction": direction.value,
            "workflow_id": workflow_id,
            "sender_task_id": sender_task_id,
            "sender_role": sender_role,
            "sender_epoch": sender_epoch,
            "recipient_task_id": recipient_task_id,
            "recipient_role": recipient_role,
            "recipient_epoch": recipient_epoch,
            "coordinator_task_id": coordinator_task_id,
            "coordinator_epoch": coordinator_epoch,
            "correlation_id": correlation_id,
            "assignment_token_hash": assignment_token_hash,
            "dispatch_context_hash": dispatch_context_hash,
            "route_provenance_hash": route_provenance_hash,
            "correlation_fence_hash": correlation_fence_hash,
            "task_card_hash": card_hash,
            "contract_hashes": list(contracts),
            "index_evidence_hashes": list(index_evidence),
            "terminal_result_hash": terminal,
            "evidence_hashes": list(evidence),
            "dependency_hashes": list(dependencies),
            "recipient_capability_hash": recipient_capability_hash,
            "risk_items": [
                {
                    "code": risk.code,
                    "severity": risk.severity,
                    "evidence_hash": risk.evidence_hash,
                }
                for risk in risks
            ],
            "issued_at": issued_at,
            "expires_at": expires_at,
        }

    def _require_role_direction_references(
        self,
        cursor: sqlite3.Cursor,
        *,
        direction: RoleEnvelopeDirection,
        workflow_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        payload: Mapping[str, object],
    ) -> None:
        if direction is not RoleEnvelopeDirection.COORDINATOR_TO_WORKER:
            return
        card = cursor.execute(
            "SELECT card_hash FROM task_cards WHERE task_id = ?", (recipient_task_id,)
        ).fetchone()
        if card is None or str(card["card_hash"]) != payload["task_card_hash"]:
            raise RoleEnvelopeForbiddenError("task card reference is not recipient-bound")
        allowed_contracts = {
            str(row["contract_hash"])
            for row in cursor.execute(
                "SELECT contract_hash FROM task_contract_subscriptions WHERE task_id = ?",
                (recipient_task_id,),
            ).fetchall()
        }
        requested_contracts = tuple(str(item) for item in payload["contract_hashes"])
        if not requested_contracts or not set(requested_contracts).issubset(allowed_contracts):
            raise RoleEnvelopeForbiddenError("contract references are not recipient-bound")

    def _require_live_role_assignment(
        self,
        cursor: sqlite3.Cursor,
        *,
        workflow_id: str,
        worker_task_id: str,
        worker_epoch: int,
        coordinator_task_id: str,
        coordinator_epoch: int,
        assignment_token_hash: str,
        dispatch_context_hash: str,
        route_provenance_hash: str,
        now: str,
    ) -> None:
        self._require_task_in_workflow(
            cursor, workflow_id, coordinator_task_id, RoleEnvelopeForbiddenError
        )
        coordinator = cursor.execute(
            "SELECT owner_role FROM tasks WHERE id = ?", (coordinator_task_id,)
        ).fetchone()
        if coordinator is None or str(coordinator["owner_role"]) != "coordinator":
            raise RoleEnvelopeForbiddenError("coordinator role is not authoritative")
        self._require_current_recipient_lease(
            cursor, coordinator_task_id, coordinator_epoch, now=now
        )
        row = cursor.execute(
            """
            SELECT 1 FROM role_envelopes
            WHERE direction = ? AND workflow_id = ? AND sender_task_id = ?
                AND recipient_task_id = ? AND sender_role = ? AND recipient_role = ?
                AND sender_epoch = ? AND recipient_epoch = ?
                AND assignment_token_hash = ? AND dispatch_context_hash = ?
                AND route_provenance_hash = ? AND expires_at > ?
            """,
            (
                RoleEnvelopeDirection.COORDINATOR_TO_WORKER.value,
                workflow_id,
                coordinator_task_id,
                worker_task_id,
                "coordinator",
                "worker",
                coordinator_epoch,
                worker_epoch,
                assignment_token_hash,
                dispatch_context_hash,
                route_provenance_hash,
                now,
            ),
        ).fetchone()
        if row is None:
            raise RoleEnvelopeForbiddenError("no live coordinator assignment matches envelope")

    def _role_reference_bytes(
        self,
        cursor: sqlite3.Cursor,
        *,
        sender_task_id: str,
        payload: Mapping[str, object],
    ) -> int:
        references = {
            *tuple(str(item) for item in payload["index_evidence_hashes"]),
            *tuple(str(item) for item in payload["evidence_hashes"]),
            *tuple(str(item) for item in payload["dependency_hashes"]),
        }
        terminal_result_hash = str(payload["terminal_result_hash"])
        if terminal_result_hash:
            references.add(terminal_result_hash)
        if not references:
            raise RoleEnvelopeInvalidError("role envelope requires artifact references")
        total = 0
        for content_hash in sorted(references):
            row = cursor.execute(
                """
                SELECT artifacts.size, artifacts.redaction_version
                FROM artifacts
                JOIN artifact_owners ON artifact_owners.content_hash = artifacts.content_hash
                WHERE artifacts.content_hash = ? AND artifact_owners.task_id = ?
                """,
                (content_hash, sender_task_id),
            ).fetchone()
            if row is None or not row["redaction_version"]:
                raise ArtifactNotOwnedError(
                    f"artifact is not owned by sender: {content_hash!r}"
                )
            total += int(row["size"])
        return total

    @staticmethod
    def _require_durable_mailbox_quota(
        cursor: sqlite3.Cursor,
        *,
        workflow_id: str,
        recipient_task_id: str,
        recipient_epoch: int | None,
        now: str,
        incoming_bytes: int,
        max_count: int,
        max_bytes: int,
    ) -> None:
        """Apply one transactionally serialized quota across both mailbox tables."""

        scopes = (
            (
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(item_bytes), 0) AS bytes
                FROM (
                    SELECT artifacts.size AS item_bytes
                    FROM messages
                    JOIN artifacts ON artifacts.content_hash = messages.artifact_hash
                    WHERE messages.workflow_id = ? AND messages.recipient_task_id = ?
                        AND messages.acknowledged_at IS NULL AND messages.expires_at > ?
                    UNION ALL
                    SELECT reference_bytes AS item_bytes
                    FROM role_envelopes
                    WHERE workflow_id = ? AND recipient_task_id = ? AND recipient_epoch = ?
                        AND acknowledged_at IS NULL AND expires_at > ?
                )
                """,
                (
                    workflow_id,
                    recipient_task_id,
                    now,
                    workflow_id,
                    recipient_task_id,
                    recipient_epoch,
                    now,
                ),
            ),
            (
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(item_bytes), 0) AS bytes
                FROM (
                    SELECT artifacts.size AS item_bytes
                    FROM messages
                    JOIN artifacts ON artifacts.content_hash = messages.artifact_hash
                    WHERE messages.workflow_id = ?
                        AND messages.acknowledged_at IS NULL AND messages.expires_at > ?
                    UNION ALL
                    SELECT reference_bytes AS item_bytes
                    FROM role_envelopes
                    WHERE workflow_id = ?
                        AND acknowledged_at IS NULL AND expires_at > ?
                )
                """,
                (workflow_id, now, workflow_id, now),
            ),
        )
        for query, parameters in scopes:
            usage = cursor.execute(query, parameters).fetchone()
            if (
                int(usage["count"]) + 1 > max_count
                or int(usage["bytes"]) + incoming_bytes > max_bytes
            ):
                raise QuotaExceededError("durable mailbox quota exceeded")

    @staticmethod
    def _role_envelope_cursor(
        cursor: sqlite3.Cursor,
        workflow_id: str,
        recipient_task_id: str,
        value: str | None,
    ) -> int:
        if value is None:
            return 0
        if value.isdecimal():
            return int(value)
        row = cursor.execute(
            """
            SELECT sequence FROM role_envelopes
            WHERE delivery_id = ? AND workflow_id = ? AND recipient_task_id = ?
            """,
            (value, workflow_id, recipient_task_id),
        ).fetchone()
        if row is None:
            raise MailboxForbiddenError("role inbox cursor is not recipient-owned")
        return int(row["sequence"])

    @staticmethod
    def _message_cursor(
        cursor: sqlite3.Cursor,
        workflow_id: str,
        recipient_task_id: str,
        value: str | None,
    ) -> int:
        if value is None:
            return 0
        if value.isdecimal():
            return int(value)
        row = cursor.execute(
            """
            SELECT sequence FROM messages
            WHERE delivery_id = ? AND workflow_id = ? AND recipient_task_id = ?
            """,
            (value, workflow_id, recipient_task_id),
        ).fetchone()
        if row is None:
            raise MailboxForbiddenError("mailbox cursor is not owned by this recipient")
        return int(row["sequence"])

    @staticmethod
    def _append_event_in_transaction(
        cursor: sqlite3.Cursor,
        workflow_id: str,
        task_id: str | None,
        event_type: str,
        redacted_payload: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO events
                (workflow_id, task_id, event_type, redacted_payload, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                task_id,
                event_type,
                redacted_payload,
                f"sha256:{hashlib.sha256(redacted_payload.encode('utf-8')).hexdigest()}",
                _utc_now(),
            ),
        )

    def _insert_atlas_outbox(
        self,
        cursor: sqlite3.Cursor,
        acceptance: CodeTaskAcceptance,
        *,
        payload_json: str,
        created_at: str,
    ) -> AtlasOutboxItem:
        """Insert the single pending ingestion row owned by an acceptance."""
        cursor.execute(
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_json, payload_hash, state,
                attempt_count, last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acceptance.payload_hash,
                acceptance.acceptance_id,
                payload_json,
                acceptance.payload_hash,
                AtlasOutboxState.PENDING.value,
                0,
                "",
                "[]",
                created_at,
                created_at,
            ),
        )
        row = cursor.execute(
            "SELECT * FROM atlas_ingestion_outbox WHERE ingestion_key = ?",
            (acceptance.payload_hash,),
        ).fetchone()
        return self._atlas_outbox_from_row(row)

    @classmethod
    def build_code_task_evidence_binding(
        cls,
        *,
        workflow_id: str,
        task_id: str,
        task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        checkpoint_id: str,
        checkpoint_hash: str,
        output_query_trace_id: str,
        verification_artifact_hashes: tuple[str, ...],
        execution_receipt_ids: tuple[str, ...],
    ) -> CodeTaskEvidenceBinding:
        """Construct one canonical typed evidence binding without persistence."""

        payload = cls._code_task_evidence_binding_payload(
            workflow_id=workflow_id,
            task_id=task_id,
            task_version=task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            output_query_trace_id=output_query_trace_id,
            verification_artifact_hashes=verification_artifact_hashes,
            execution_receipt_ids=execution_receipt_ids,
        )
        payload_json = _canonical_evidence_binding_json(payload)
        return CodeTaskEvidenceBinding(
            str(payload["schema_version"]),
            str(payload["workflow_id"]),
            str(payload["code_task_id"]),
            int(payload["code_task_version"]),
            str(payload["input_snapshot_id"]),
            str(payload["output_snapshot_id"]),
            str(payload["indexed_diff_hash"]),
            str(payload["checkpoint_id"]),
            str(payload["checkpoint_hash"]),
            str(payload["output_query_trace_id"]),
            tuple(payload["verification_artifact_hashes"]),
            tuple(payload["execution_receipt_ids"]),
            _payload_hash(payload_json),
        )

    @classmethod
    def build_code_task_receipt_attestation(
        cls,
        *,
        workflow_id: str,
        code_task_id: str,
        code_task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        workspace_hash: str,
        execution_receipt_ids: tuple[str, ...],
    ) -> CodeTaskReceiptAttestation:
        """Recompute the producer-owned receipt attestation without persistence."""

        payload = cls._code_task_receipt_attestation_payload(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            code_task_version=code_task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            workspace_hash=workspace_hash,
            execution_receipt_ids=execution_receipt_ids,
        )
        payload_json = _canonical_receipt_attestation_json(payload)
        return CodeTaskReceiptAttestation(
            str(payload["schema_version"]),
            str(payload["workflow_id"]),
            str(payload["code_task_id"]),
            int(payload["code_task_version"]),
            str(payload["input_snapshot_id"]),
            str(payload["output_snapshot_id"]),
            str(payload["workspace_hash"]),
            tuple(payload["execution_receipt_ids"]),
            _payload_hash(payload_json),
        )

    @classmethod
    def _validate_code_task_receipt_attestation(
        cls,
        receipt_attestation: CodeTaskReceiptAttestation,
        *,
        workflow_id: str | None = None,
        task_id: str | None = None,
        task_version: int | None = None,
        input_snapshot_id: str | None = None,
        output_snapshot_id: str | None = None,
    ) -> None:
        if not isinstance(receipt_attestation, CodeTaskReceiptAttestation):
            raise ValueError("receipt_attestation must be a CodeTaskReceiptAttestation")
        expected = cls.build_code_task_receipt_attestation(
            workflow_id=receipt_attestation.workflow_id,
            code_task_id=receipt_attestation.code_task_id,
            code_task_version=receipt_attestation.code_task_version,
            input_snapshot_id=receipt_attestation.input_snapshot_id,
            output_snapshot_id=receipt_attestation.output_snapshot_id,
            workspace_hash=receipt_attestation.workspace_hash,
            execution_receipt_ids=receipt_attestation.execution_receipt_ids,
        )
        if expected != receipt_attestation:
            raise ValueError("receipt attestation is not canonical")
        if (
            (workflow_id is not None and receipt_attestation.workflow_id != workflow_id)
            or (task_id is not None and receipt_attestation.code_task_id != task_id)
            or (
                task_version is not None
                and receipt_attestation.code_task_version != task_version
            )
            or (
                input_snapshot_id is not None
                and receipt_attestation.input_snapshot_id != input_snapshot_id
            )
            or (
                output_snapshot_id is not None
                and receipt_attestation.output_snapshot_id != output_snapshot_id
            )
        ):
            raise AcceptanceConflictError(
                "receipt attestation does not match code task completion"
            )

    def _code_task_receipt_attestation_from_row(
        self, row: sqlite3.Row
    ) -> CodeTaskReceiptAttestation:
        try:
            encoded_receipt_ids = str(row["execution_receipt_ids"])
            receipt_ids = _decode_strings(encoded_receipt_ids)
            attestation = self.build_code_task_receipt_attestation(
                workflow_id=str(row["workflow_id"]),
                code_task_id=str(row["task_id"]),
                code_task_version=int(row["code_task_version"]),
                input_snapshot_id=str(row["input_snapshot_id"]),
                output_snapshot_id=str(row["output_snapshot_id"]),
                workspace_hash=str(row["workspace_hash"]),
                execution_receipt_ids=receipt_ids,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AcceptanceConflictError(
                "code task receipt attestation is corrupt"
            ) from error
        if (
            encoded_receipt_ids != _encode_strings(attestation.execution_receipt_ids)
            or str(row["attestation_hash"]) != attestation.attestation_hash
        ):
            raise AcceptanceConflictError(
                "code task receipt attestation hash is corrupt"
            )

        task = self._connection.execute(
            "SELECT workflow_id, state, version, task_kind FROM tasks WHERE id = ?",
            (attestation.code_task_id,),
        ).fetchone()
        binding = self._connection.execute(
            """
            SELECT *
            FROM task_index_bindings WHERE task_id = ?
            """,
            (attestation.code_task_id,),
        ).fetchone()
        if binding is not None:
            self._index_binding_from_row(binding)
        owner_rows = self._connection.execute(
            """
            SELECT receipt_id, code_task_version, attestation_hash
            FROM code_task_receipt_owners
            WHERE task_id = ?
            ORDER BY receipt_id
            """,
            (attestation.code_task_id,),
        ).fetchall()
        if (
            task is None
            or str(task["workflow_id"]) != attestation.workflow_id
            or str(task["state"]) != TaskState.DONE.value
            or int(task["version"]) != attestation.code_task_version
            or str(task["task_kind"]) != TaskKind.CODE.value
            or binding is None
            or str(binding["input_snapshot_id"]) != attestation.input_snapshot_id
            or str(binding["output_snapshot_id"]) != attestation.output_snapshot_id
            or tuple(str(owner["receipt_id"]) for owner in owner_rows)
            != attestation.execution_receipt_ids
            or any(
                int(owner["code_task_version"]) != attestation.code_task_version
                or str(owner["attestation_hash"]) != attestation.attestation_hash
                for owner in owner_rows
            )
        ):
            raise AcceptanceConflictError(
                "code task receipt attestation ownership is corrupt"
            )
        return attestation

    def _insert_code_task_evidence_binding(
        self,
        cursor: sqlite3.Cursor,
        acceptance: CodeTaskAcceptance,
        evidence_binding: CodeTaskEvidenceBinding,
        *,
        created_at: str,
    ) -> None:
        payload_json = self._code_task_evidence_binding_json(evidence_binding)
        cursor.execute(
            """
            INSERT INTO events
                (workflow_id, task_id, event_type, redacted_payload, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                acceptance.workflow_id,
                acceptance.code_task_id,
                self._EVIDENCE_BINDING_EVENT_TYPE,
                payload_json,
                evidence_binding.evidence_binding_hash,
                created_at,
            ),
        )

    def _require_existing_code_task_evidence_binding(
        self,
        cursor: sqlite3.Cursor,
        acceptance: CodeTaskAcceptance,
        evidence_binding: CodeTaskEvidenceBinding,
    ) -> None:
        rows = cursor.execute(
            """
            SELECT redacted_payload, payload_hash FROM events
            WHERE workflow_id = ? AND task_id = ? AND event_type = ?
            ORDER BY sequence
            """,
            (
                acceptance.workflow_id,
                acceptance.code_task_id,
                self._EVIDENCE_BINDING_EVENT_TYPE,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise AcceptanceConflictError(
                f"code task acceptance evidence is not durable: {acceptance.code_task_id!r}"
            )
        payload_json = self._code_task_evidence_binding_json(evidence_binding)
        row = rows[0]
        if (
            str(row["redacted_payload"]) != payload_json
            or str(row["payload_hash"]) != evidence_binding.evidence_binding_hash
        ):
            raise AcceptanceConflictError(
                f"code task acceptance evidence changed: {acceptance.code_task_id!r}"
            )

    def _code_task_evidence_binding_for_acceptance(
        self, acceptance: CodeTaskAcceptance
    ) -> CodeTaskEvidenceBinding:
        rows = self._connection.execute(
            """
            SELECT redacted_payload, payload_hash FROM events
            WHERE workflow_id = ? AND task_id = ? AND event_type = ?
            ORDER BY sequence
            """,
            (
                acceptance.workflow_id,
                acceptance.code_task_id,
                self._EVIDENCE_BINDING_EVENT_TYPE,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise AcceptanceConflictError(
                f"code task acceptance evidence is not durable: {acceptance.code_task_id!r}"
            )
        binding = self._code_task_evidence_binding_from_event(rows[0])
        if (
            binding.workflow_id != acceptance.workflow_id
            or binding.code_task_id != acceptance.code_task_id
            or binding.code_task_version != acceptance.code_task_version
            or binding.input_snapshot_id != acceptance.input_snapshot_id
            or binding.output_snapshot_id != acceptance.output_snapshot_id
            or binding.indexed_diff_hash != acceptance.indexed_diff_hash
        ):
            raise AcceptanceConflictError(
                f"code task acceptance evidence is inconsistent: {acceptance.code_task_id!r}"
            )
        return binding

    @classmethod
    def _code_task_evidence_binding_from_event(
        cls, row: sqlite3.Row
    ) -> CodeTaskEvidenceBinding:
        try:
            payload_json = str(row["redacted_payload"])
            payload = json.loads(payload_json)
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "checkpoint_hash",
                    "checkpoint_id",
                    "code_task_id",
                    "code_task_version",
                    "execution_receipt_ids",
                    "indexed_diff_hash",
                    "input_snapshot_id",
                    "output_query_trace_id",
                    "output_snapshot_id",
                    "schema_version",
                    "verification_artifact_hashes",
                    "workflow_id",
                }
                or _canonical_evidence_binding_json(payload) != payload_json
                or not isinstance(payload["verification_artifact_hashes"], list)
                or not isinstance(payload["execution_receipt_ids"], list)
                or payload["schema_version"] != cls._EVIDENCE_BINDING_SCHEMA_VERSION
            ):
                raise ValueError("evidence binding payload is invalid")
            binding = cls.build_code_task_evidence_binding(
                workflow_id=payload["workflow_id"],
                task_id=payload["code_task_id"],
                task_version=payload["code_task_version"],
                input_snapshot_id=payload["input_snapshot_id"],
                output_snapshot_id=payload["output_snapshot_id"],
                indexed_diff_hash=payload["indexed_diff_hash"],
                checkpoint_id=payload["checkpoint_id"],
                checkpoint_hash=payload["checkpoint_hash"],
                output_query_trace_id=payload["output_query_trace_id"],
                verification_artifact_hashes=tuple(
                    payload["verification_artifact_hashes"]
                ),
                execution_receipt_ids=tuple(payload["execution_receipt_ids"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AcceptanceConflictError(
                "code task evidence binding is corrupt"
            ) from error
        if str(row["payload_hash"]) != binding.evidence_binding_hash:
            raise AcceptanceConflictError("code task evidence binding hash is corrupt")
        return binding

    @classmethod
    def _code_task_evidence_binding_json(
        cls, evidence_binding: CodeTaskEvidenceBinding
    ) -> str:
        cls._validate_code_task_evidence_binding(evidence_binding)
        payload = cls._code_task_evidence_binding_payload(
            workflow_id=evidence_binding.workflow_id,
            task_id=evidence_binding.code_task_id,
            task_version=evidence_binding.code_task_version,
            input_snapshot_id=evidence_binding.input_snapshot_id,
            output_snapshot_id=evidence_binding.output_snapshot_id,
            indexed_diff_hash=evidence_binding.indexed_diff_hash,
            checkpoint_id=evidence_binding.checkpoint_id,
            checkpoint_hash=evidence_binding.checkpoint_hash,
            output_query_trace_id=evidence_binding.output_query_trace_id,
            verification_artifact_hashes=evidence_binding.verification_artifact_hashes,
            execution_receipt_ids=evidence_binding.execution_receipt_ids,
        )
        return _canonical_evidence_binding_json(payload)

    @classmethod
    def _validate_code_task_evidence_binding(
        cls,
        evidence_binding: CodeTaskEvidenceBinding,
        *,
        workflow_id: str | None = None,
        task_id: str | None = None,
        task_version: int | None = None,
        input_snapshot_id: str | None = None,
        output_snapshot_id: str | None = None,
        indexed_diff_hash: str | None = None,
    ) -> None:
        if not isinstance(evidence_binding, CodeTaskEvidenceBinding):
            raise ValueError("evidence_binding must be a CodeTaskEvidenceBinding")
        expected = cls.build_code_task_evidence_binding(
            workflow_id=evidence_binding.workflow_id,
            task_id=evidence_binding.code_task_id,
            task_version=evidence_binding.code_task_version,
            input_snapshot_id=evidence_binding.input_snapshot_id,
            output_snapshot_id=evidence_binding.output_snapshot_id,
            indexed_diff_hash=evidence_binding.indexed_diff_hash,
            checkpoint_id=evidence_binding.checkpoint_id,
            checkpoint_hash=evidence_binding.checkpoint_hash,
            output_query_trace_id=evidence_binding.output_query_trace_id,
            verification_artifact_hashes=evidence_binding.verification_artifact_hashes,
            execution_receipt_ids=evidence_binding.execution_receipt_ids,
        )
        if expected != evidence_binding:
            raise ValueError("evidence binding is not canonical")
        if (
            (workflow_id is not None and evidence_binding.workflow_id != workflow_id)
            or (task_id is not None and evidence_binding.code_task_id != task_id)
            or (
                task_version is not None
                and evidence_binding.code_task_version != task_version
            )
            or (
                input_snapshot_id is not None
                and evidence_binding.input_snapshot_id != input_snapshot_id
            )
            or (
                output_snapshot_id is not None
                and evidence_binding.output_snapshot_id != output_snapshot_id
            )
            or (
                indexed_diff_hash is not None
                and evidence_binding.indexed_diff_hash != indexed_diff_hash
            )
        ):
            raise AcceptanceConflictError("evidence binding does not match acceptance")

    @classmethod
    def _code_task_evidence_binding_payload(
        cls,
        *,
        workflow_id: str,
        task_id: str,
        task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        checkpoint_id: str,
        checkpoint_hash: str,
        output_query_trace_id: str,
        verification_artifact_hashes: tuple[str, ...],
        execution_receipt_ids: tuple[str, ...],
    ) -> dict[str, object]:
        if (
            isinstance(task_version, bool)
            or not isinstance(task_version, int)
            or not 0 <= task_version <= 2**63 - 1
        ):
            raise ValueError("task_version must be a non-negative SQLite integer")
        return {
            "schema_version": cls._EVIDENCE_BINDING_SCHEMA_VERSION,
            "workflow_id": cls._safe_acceptance_identifier("workflow_id", workflow_id),
            "code_task_id": cls._safe_acceptance_identifier("code_task_id", task_id),
            "code_task_version": task_version,
            "input_snapshot_id": cls._safe_acceptance_identifier(
                "input_snapshot_id", input_snapshot_id
            ),
            "output_snapshot_id": cls._safe_acceptance_identifier(
                "output_snapshot_id", output_snapshot_id
            ),
            "indexed_diff_hash": cls._safe_acceptance_identifier(
                "indexed_diff_hash", indexed_diff_hash
            ),
            "checkpoint_id": cls._safe_acceptance_identifier(
                "checkpoint_id", checkpoint_id
            ),
            "checkpoint_hash": cls._safe_acceptance_identifier(
                "checkpoint_hash", checkpoint_hash
            ),
            "output_query_trace_id": cls._safe_acceptance_identifier(
                "output_query_trace_id", output_query_trace_id
            ),
            "verification_artifact_hashes": list(
                cls._safe_evidence_identifier_list(
                    "verification_artifact_hashes", verification_artifact_hashes
                )
            ),
            "execution_receipt_ids": list(
                cls._safe_evidence_identifier_list(
                    "execution_receipt_ids", execution_receipt_ids
                )
            ),
        }

    @classmethod
    def _code_task_receipt_attestation_payload(
        cls,
        *,
        workflow_id: str,
        code_task_id: str,
        code_task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        workspace_hash: str,
        execution_receipt_ids: tuple[str, ...],
    ) -> dict[str, object]:
        if (
            isinstance(code_task_version, bool)
            or not isinstance(code_task_version, int)
            or not 0 <= code_task_version <= 2**63 - 1
        ):
            raise ValueError("code_task_version must be a non-negative SQLite integer")
        receipt_ids = cls._safe_evidence_identifier_list(
            "execution_receipt_ids", execution_receipt_ids
        )
        return {
            "schema_version": cls._RECEIPT_ATTESTATION_SCHEMA_VERSION,
            "workflow_id": cls._safe_acceptance_identifier("workflow_id", workflow_id),
            "code_task_id": cls._safe_acceptance_identifier(
                "code_task_id", code_task_id
            ),
            "code_task_version": code_task_version,
            "input_snapshot_id": cls._safe_acceptance_identifier(
                "input_snapshot_id", input_snapshot_id
            ),
            "output_snapshot_id": cls._safe_acceptance_identifier(
                "output_snapshot_id", output_snapshot_id
            ),
            "workspace_hash": cls._safe_sha256_identifier(
                "workspace_hash", workspace_hash
            ),
            "execution_receipt_ids": [
                cls._safe_sha256_identifier("execution_receipt_ids", receipt_id)
                for receipt_id in receipt_ids
            ],
        }

    @classmethod
    def _safe_evidence_identifier_list(
        cls, field_name: str, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if (
            not isinstance(values, tuple)
            or not values
            or len(values) > cls._MAX_CODE_TASK_EVIDENCE_ITEMS
            or tuple(sorted(set(values))) != values
        ):
            raise ValueError(f"{field_name} must be a sorted unique bounded tuple")
        return tuple(
            cls._safe_acceptance_identifier(field_name, value) for value in values
        )

    def _require_authorized_code_task_acceptance(
        self,
        cursor: sqlite3.Cursor,
        *,
        workflow_id: str,
        task_id: str,
        task_version: int,
        coordinator_task_id: str,
        coordinator_owner: str,
        coordinator_epoch: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        intent_id: str,
        language: str,
        framework: str,
        now: str,
    ) -> None:
        coordinator = cursor.execute(
            "SELECT workflow_id, owner_role, state FROM tasks WHERE id = ?",
            (coordinator_task_id,),
        ).fetchone()
        if (
            coordinator is None
            or str(coordinator["workflow_id"]) != workflow_id
            or str(coordinator["owner_role"]) not in {"sol", "opus"}
            or str(coordinator["state"]) != TaskState.RUNNING.value
        ):
            raise AcceptanceAuthorizationError(
                "acceptance requires a same-workflow sol or opus coordinator"
            )
        self._require_current_lease(
            cursor,
            coordinator_task_id,
            coordinator_owner,
            coordinator_epoch,
            now=now,
        )

        task = cursor.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            task is None
            or str(task["workflow_id"]) != workflow_id
            or str(task["task_kind"]) != TaskKind.CODE.value
        ):
            raise AcceptanceAuthorizationError(
                "acceptance requires a code task in the coordinator workflow"
            )
        if int(task["version"]) != task_version:
            raise VersionConflictError(f"task version is not current: {task_id!r}")
        if str(task["state"]) != TaskState.DONE.value:
            raise InvalidTaskStateError(
                f"code task must be done before acceptance: {task_id!r}"
            )

        binding = self._require_index_binding(cursor, task_id)
        self._require_strict_completion(cursor, task_id)
        if (
            str(binding["input_snapshot_id"]) != input_snapshot_id
            or str(binding["output_snapshot_id"]) != output_snapshot_id
            or str(binding["indexed_diff_hash"]) != indexed_diff_hash
            or str(task["intent_id"]) != intent_id
            or str(task["language"]) != language
            or str(task["framework"]) != framework
        ):
            raise AcceptanceConflictError(
                f"acceptance metadata is not current for code task: {task_id!r}"
            )

    @classmethod
    def _canonical_code_task_acceptance_payload(
        cls,
        *,
        workflow_id: str,
        task_id: str,
        task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        intent_id: str,
        language: str,
        framework: str,
    ) -> str:
        if (
            isinstance(task_version, bool)
            or not isinstance(task_version, int)
            or not 0 <= task_version <= 2**63 - 1
        ):
            raise ValueError("task_version must be a non-negative SQLite integer")
        payload = {
            "framework": cls._safe_acceptance_identifier(
                "framework", framework, allow_empty=True
            ),
            "indexed_diff_hash": cls._safe_acceptance_identifier(
                "indexed_diff_hash", indexed_diff_hash
            ),
            "input_snapshot_id": cls._safe_acceptance_identifier(
                "input_snapshot_id", input_snapshot_id
            ),
            "intent_id": cls._safe_acceptance_identifier("intent_id", intent_id),
            "language": cls._safe_acceptance_identifier("language", language),
            "output_snapshot_id": cls._safe_acceptance_identifier(
                "output_snapshot_id", output_snapshot_id
            ),
            "task_id": cls._safe_acceptance_identifier("task_id", task_id),
            "task_kind": TaskKind.CODE.value,
            "task_version": task_version,
            "workflow_id": cls._safe_acceptance_identifier("workflow_id", workflow_id),
        }
        return _canonical_payload_json(payload)

    @classmethod
    def _safe_acceptance_identifier(
        cls, field_name: str, value: str, *, allow_empty: bool = False
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        if not value and allow_empty:
            return ""
        if (
            not value
            or len(value) > cls._MAX_SAFE_ACCEPTANCE_IDENTIFIER_LENGTH
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            raise ValueError(f"{field_name} must be a bounded opaque identifier")
        return value

    @classmethod
    def _safe_sha256_identifier(cls, field_name: str, value: str) -> str:
        cls._safe_acceptance_identifier(field_name, value)
        if cls._SHA256_IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be a canonical sha256 identifier")
        return value

    @classmethod
    def _validate_code_task_acceptance_list_limit(cls, limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= cls._MAX_CODE_TASK_ACCEPTANCE_LIST
        ):
            raise ValueError(
                "acceptance list limit must be an integer between 1 and "
                f"{cls._MAX_CODE_TASK_ACCEPTANCE_LIST}"
            )

    @classmethod
    def _safe_outbox_code(
        cls, field_name: str, value: str, *, allow_empty: bool = False
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        if not value and allow_empty:
            return ""
        if (
            not value
            or len(value) > cls._MAX_SAFE_OUTBOX_CODE_LENGTH
            or cls._SAFE_OUTBOX_CODE_PATTERN.fullmatch(value) is None
        ):
            raise ValueError(f"{field_name} must be a stable safe code")
        return value

    @classmethod
    def _safe_outbox_reason_codes(
        cls, reason_codes: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not isinstance(reason_codes, tuple):
            raise ValueError("reason_codes must be a tuple of stable safe codes")
        if len(reason_codes) > cls._MAX_SAFE_OUTBOX_REASON_COUNT:
            raise ValueError("too many outbox reason codes")
        return tuple(
            sorted(
                {
                    cls._safe_outbox_code("reason_code", reason_code)
                    for reason_code in reason_codes
                }
            )
        )

    @classmethod
    def _optional_index_binding(
        cls, cursor: sqlite3.Cursor, task_id: str
    ) -> sqlite3.Row | None:
        row = cursor.execute(
            "SELECT * FROM task_index_bindings WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is not None:
            cls._workspace_id_from_row(row)
        return row

    @classmethod
    def _require_index_binding(
        cls, cursor: sqlite3.Cursor, task_id: str
    ) -> sqlite3.Row:
        row = cls._optional_index_binding(cursor, task_id)
        if row is None:
            raise StrictIndexError("INDEX_UNAVAILABLE")
        return row

    @staticmethod
    def _append_binding_event(
        cursor: sqlite3.Cursor,
        task_id: str,
        event_type: str,
        *,
        snapshot_id: str = "",
        trace_id: str = "",
    ) -> None:
        cursor.execute(
            """
            INSERT INTO task_index_binding_events
                (task_id, event_type, snapshot_id, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, event_type, snapshot_id, trace_id, _utc_now()),
        )

    @classmethod
    def _require_strict_completion(cls, cursor: sqlite3.Cursor, task_id: str) -> None:
        binding = cls._optional_index_binding(cursor, task_id)
        if binding is None:
            return
        task = cursor.execute(
            "SELECT write_scope FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        write_scope = _decode_strings(task["write_scope"])
        output_snapshot_id = str(binding["output_snapshot_id"])
        if write_scope:
            if not str(binding["checkpoint_id"]):
                raise StrictIndexError("CHECKPOINT_REQUIRED")
            if not output_snapshot_id:
                raise StrictIndexError("OUTPUT_SNAPSHOT_REQUIRED")
            if not str(binding["indexed_diff_hash"]):
                raise StrictIndexError("INDEXED_DIFF_REQUIRED")
        if not output_snapshot_id:
            raise StrictIndexError("OUTPUT_SNAPSHOT_REQUIRED")
        receipt = cursor.execute(
            """
            SELECT 1 FROM task_index_query_receipts
            WHERE task_id = ? AND snapshot_id = ? LIMIT 1
            """,
            (task_id, output_snapshot_id),
        ).fetchone()
        if receipt is None:
            raise StrictIndexError("QUERY_RECEIPT_REQUIRED")
        verification = cursor.execute(
            """
            SELECT 1 FROM task_index_verification_artifacts
            WHERE task_id = ? AND snapshot_id = ? LIMIT 1
            """,
            (task_id, output_snapshot_id),
        ).fetchone()
        if verification is None:
            raise StrictIndexError("VERIFICATION_EVIDENCE_REQUIRED")

    @classmethod
    def _workspace_id_from_row(cls, row: sqlite3.Row) -> str:
        workspace_id = row["workspace_id"]
        if (
            type(workspace_id) is not str
            or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(workspace_id) is None
        ):
            raise StrictIndexError("INDEX_UNAVAILABLE")
        return workspace_id

    @classmethod
    def _index_binding_from_row(cls, row: sqlite3.Row) -> IndexBinding:
        return IndexBinding(
            str(row["task_id"]),
            cls._workspace_id_from_row(row),
            str(row["input_snapshot_id"]),
            str(row["output_snapshot_id"]),
            _decode_strings(row["task_node_ids"]),
            _decode_strings(row["contract_node_ids"]),
            str(row["checkpoint_id"]),
            str(row["indexed_diff_hash"]),
            int(row["fallback_count"]),
        )

    @classmethod
    def _validate_external_hash(cls, value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            raise ExternalBootstrapConflictError(f"{label} must be a sha256 identifier")

    @classmethod
    def _validate_external_identifier(cls, value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(value) is None
        ):
            raise ExternalBootstrapConflictError(f"{label} is outside the bounded schema")

    @classmethod
    def _external_descriptor_payload(
        cls, descriptor: ExternalSourceDescriptor
    ) -> dict[str, object]:
        return {
            "commit_hash": descriptor.commit_hash,
            "common_dir_hash": descriptor.common_dir_hash,
            "descriptor_hash": descriptor.descriptor_hash,
            "project_hash": descriptor.project_hash,
            "ref_hash": descriptor.ref_hash,
            "repository_hash": descriptor.repository_hash,
            "source_hash": descriptor.source_hash,
            "task_root_hash": descriptor.task_root_hash,
            "tree_hash": descriptor.tree_hash,
        }

    @classmethod
    def _external_batch_item_payload(
        cls, item: ExternalBootstrapBatchItem
    ) -> dict[str, object]:
        return {
            "assignment_hash": item.assignment_hash,
            "index_hash": item.index_hash,
            "item_index": item.item_index,
            "lease_epoch": item.lease_epoch,
            "lease_hash": item.lease_hash,
            "plan_hash": item.plan_hash,
            "predecessor_hash": item.predecessor_hash,
            "projection_hash": item.projection_hash,
            "quota_hash": item.quota_hash,
            "route_hash": item.route_hash,
            "task_id": item.task_id,
            "task_hash": item.task_hash,
            "workflow_id": item.workflow_id,
            "workflow_hash": item.workflow_hash,
        }

    @classmethod
    def _external_batch_payload(cls, batch: ExternalBootstrapBatch) -> dict[str, object]:
        return {
            "availability": batch.availability,
            "batch_hash": batch.batch_hash,
            "descriptor_hash": batch.descriptor_hash,
            "expires_at": batch.expires_at,
            "idempotency_key": batch.idempotency_key,
            "items": [cls._external_batch_item_payload(item) for item in batch.items],
            "state": batch.state.value,
        }

    @classmethod
    def _external_grant_payload(cls, grant: ExternalDispatchGrant) -> dict[str, object]:
        return {
            "assignment_hash": grant.assignment_hash,
            "availability": grant.availability,
            "batch_hash": grant.batch_hash,
            "descriptor_hash": grant.descriptor_hash,
            "expires_at": grant.expires_at,
            "grant_id": grant.grant_id,
            "state": grant.state.value,
        }

    @classmethod
    def _external_batch_item_from_row(
        cls, row: sqlite3.Row
    ) -> ExternalBootstrapBatchItem:
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("item payload must be an object")
            item = ExternalBootstrapBatchItem(
                item_index=int(payload["item_index"]),
                workflow_id=str(payload["workflow_id"]),
                task_id=str(payload["task_id"]),
                lease_epoch=int(payload["lease_epoch"]),
                plan_hash=str(payload["plan_hash"]),
                projection_hash=str(payload["projection_hash"]),
                assignment_hash=str(payload["assignment_hash"]),
                predecessor_hash=str(payload["predecessor_hash"]),
                quota_hash=str(payload["quota_hash"]),
                route_hash=str(payload["route_hash"]),
                index_hash=str(payload["index_hash"]),
                workflow_hash=str(payload["workflow_hash"]),
                task_hash=str(payload["task_hash"]),
                lease_hash=str(payload["lease_hash"]),
            )
            canonical_payload = _canonical_payload_json(
                cls._external_batch_item_payload(item)
            )
            if (
                str(row["payload_json"]) != canonical_payload
                or str(row["payload_hash"]) != _payload_hash(canonical_payload)
                or int(row["item_index"]) != item.item_index
                or str(row["assignment_hash"]) != item.assignment_hash
            ):
                raise ValueError("item payload binding differs from its row")
            return item
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExternalDispatchGrantError(
                "external bootstrap batch item binding is corrupt"
            ) from error

    @classmethod
    def _external_descriptor_from_row(
        cls, row: sqlite3.Row
    ) -> ExternalSourceDescriptor:
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("descriptor payload must be an object")
            descriptor = ExternalSourceDescriptor(
                descriptor_hash=str(payload["descriptor_hash"]),
                source_hash=str(payload["source_hash"]),
                repository_hash=str(payload["repository_hash"]),
                common_dir_hash=str(payload["common_dir_hash"]),
                project_hash=str(payload["project_hash"]),
                task_root_hash=str(payload["task_root_hash"]),
                ref_hash=str(payload["ref_hash"]),
                commit_hash=str(payload["commit_hash"]),
                tree_hash=str(payload["tree_hash"]),
            )
            canonical_payload = _canonical_payload_json(
                cls._external_descriptor_payload(descriptor)
            )
            if (
                str(row["descriptor_hash"]) != descriptor.descriptor_hash
                or str(row["payload_json"]) != canonical_payload
                or str(row["payload_hash"]) != _payload_hash(canonical_payload)
            ):
                raise ValueError("descriptor payload binding differs from its row")
            for label, value in cls._external_descriptor_payload(descriptor).items():
                if cls._SHA256_IDENTIFIER_PATTERN.fullmatch(str(value)) is None:
                    raise ValueError(f"{label} is not a sha256 identifier")
            return descriptor
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExternalDispatchGrantError(
                "external source descriptor binding is corrupt"
            ) from error

    @classmethod
    def _external_batch_from_row(
        cls,
        row: sqlite3.Row,
        items: tuple[ExternalBootstrapBatchItem, ...],
    ) -> ExternalBootstrapBatch:
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("batch payload must be an object")
            batch = ExternalBootstrapBatch(
                batch_hash=str(payload["batch_hash"]),
                descriptor_hash=str(payload["descriptor_hash"]),
                idempotency_key=str(payload["idempotency_key"]),
                items=items,
                expires_at=str(payload["expires_at"]),
                state=ExternalBootstrapState(str(payload["state"])),
                availability=str(payload["availability"]),
            )
            canonical_payload = _canonical_payload_json(
                cls._external_batch_payload(batch)
            )
            if (
                str(row["batch_hash"]) != batch.batch_hash
                or str(row["descriptor_hash"]) != batch.descriptor_hash
                or str(row["idempotency_key"]) != batch.idempotency_key
                or str(row["expires_at"]) != batch.expires_at
                or str(row["state"]) != batch.state.value
                or str(row["availability"]) != batch.availability
                or str(row["payload_json"]) != canonical_payload
                or str(row["payload_hash"]) != _payload_hash(canonical_payload)
                or batch.expires_at != _utc_timestamp(batch.expires_at)
                or batch.state is not ExternalBootstrapState.PENDING
                or batch.availability != cls._EXTERNAL_BOOTSTRAP_AVAILABILITY
                or not items
                or len(items) > cls._MAX_EXTERNAL_BOOTSTRAP_BATCH_ITEMS
                or tuple(item.item_index for item in items) != tuple(range(len(items)))
                or len({item.assignment_hash for item in items}) != len(items)
            ):
                raise ValueError("batch payload binding differs from its row")
            if (
                cls._SHA256_IDENTIFIER_PATTERN.fullmatch(batch.batch_hash) is None
                or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(batch.descriptor_hash)
                is None
                or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(
                    batch.idempotency_key
                )
                is None
            ):
                raise ValueError("batch identifiers are outside the bounded schema")
            for item in items:
                cls._validate_external_batch_item_from_read(item)
            return batch
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExternalDispatchGrantError(
                "external bootstrap batch binding is corrupt"
            ) from error

    @classmethod
    def _validate_external_batch_item_from_read(
        cls, item: ExternalBootstrapBatchItem
    ) -> None:
        if (
            isinstance(item.lease_epoch, bool)
            or item.lease_epoch < 0
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(item.workflow_id)
            is None
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(item.task_id) is None
        ):
            raise ValueError("batch item identifiers are outside the bounded schema")
        for label, value in cls._external_batch_item_payload(item).items():
            if str(label).endswith("_hash") and (
                cls._SHA256_IDENTIFIER_PATTERN.fullmatch(str(value)) is None
            ):
                raise ValueError(f"{label} is not a sha256 identifier")

    def _validate_external_grant_binding_at_read(
        self, row: sqlite3.Row, *, cursor: sqlite3.Cursor | None = None
    ) -> None:
        executor = self._connection if cursor is None else cursor
        try:
            grant = self._external_grant_from_row(row)
            canonical_expiry = _utc_timestamp(grant.expires_at)
            grant_payload = _canonical_payload_json(self._external_grant_payload(grant))
            if (
                grant.expires_at != canonical_expiry
                or str(row["payload_json"]) != grant_payload
                or str(row["payload_hash"]) != _payload_hash(grant_payload)
            ):
                raise ValueError("grant payload is not canonical")
            binding = executor.execute(
                "SELECT * FROM external_dispatch_grant_bindings WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()
            if binding is None or any(
                str(binding[field]) != value
                for field, value in (
                    ("descriptor_hash", grant.descriptor_hash),
                    ("batch_hash", grant.batch_hash),
                    ("assignment_hash", grant.assignment_hash),
                )
            ):
                raise ValueError("grant composite binding is absent or mismatched")
            descriptor_row = executor.execute(
                "SELECT * FROM external_bootstrap_descriptors WHERE descriptor_hash = ?",
                (grant.descriptor_hash,),
            ).fetchone()
            if descriptor_row is None:
                raise ValueError("grant does not bind a descriptor")
            descriptor = self._external_descriptor_from_row(descriptor_row)
            batch_row = executor.execute(
                "SELECT * FROM external_bootstrap_batches WHERE batch_hash = ?",
                (grant.batch_hash,),
            ).fetchone()
            item_rows = executor.execute(
                """
                SELECT * FROM external_bootstrap_batch_items
                WHERE batch_hash = ? ORDER BY item_index
                """,
                (grant.batch_hash,),
            ).fetchall()
            if batch_row is None:
                raise ValueError("grant does not bind a batch")
            batch = self._external_batch_from_row(
                batch_row,
                tuple(self._external_batch_item_from_row(item_row) for item_row in item_rows),
            )
            if (
                descriptor.descriptor_hash != grant.descriptor_hash
                or batch.descriptor_hash != descriptor.descriptor_hash
                or batch.expires_at != canonical_expiry
            ):
                raise ValueError("grant does not bind the canonical batch")
            if not any(
                item.assignment_hash == grant.assignment_hash for item in batch.items
            ):
                raise ValueError("grant does not bind a batch assignment")
            descriptor_payload_hash = _payload_hash(
                _canonical_payload_json(self._external_descriptor_payload(descriptor))
            )
            batch_payload_hash = _payload_hash(
                _canonical_payload_json(self._external_batch_payload(batch))
            )
            grant_payload_hash = _payload_hash(grant_payload)
            batch_commitment = executor.execute(
                """
                SELECT * FROM external_bootstrap_batch_commitments
                WHERE batch_hash = ?
                """,
                (batch.batch_hash,),
            ).fetchone()
            grant_commitment = executor.execute(
                """
                SELECT * FROM external_dispatch_grant_commitments
                WHERE grant_id = ?
                """,
                (grant.grant_id,),
            ).fetchone()
            expected_batch_commitment = {
                "batch_hash": batch.batch_hash,
                "descriptor_hash": descriptor.descriptor_hash,
                "descriptor_payload_hash": descriptor_payload_hash,
                "batch_payload_hash": batch_payload_hash,
            }
            expected_grant_commitment = {
                "grant_id": grant.grant_id,
                "descriptor_hash": descriptor.descriptor_hash,
                "batch_hash": batch.batch_hash,
                "assignment_hash": grant.assignment_hash,
                "descriptor_payload_hash": descriptor_payload_hash,
                "batch_payload_hash": batch_payload_hash,
                "grant_payload_hash": grant_payload_hash,
            }
            if (
                batch_commitment is None
                or grant_commitment is None
                or any(
                    str(batch_commitment[field]) != value
                    for field, value in expected_batch_commitment.items()
                )
                or any(
                    str(grant_commitment[field]) != value
                    for field, value in expected_grant_commitment.items()
                )
                or any(
                    str(batch_commitment[field]) != str(grant_commitment[field])
                    for field in (
                        "batch_hash",
                        "descriptor_hash",
                        "descriptor_payload_hash",
                        "batch_payload_hash",
                    )
                )
            ):
                raise ValueError("external bootstrap commitment chain is absent or mismatched")
        except (
            KeyError,
            TypeError,
            ValueError,
            sqlite3.DatabaseError,
            ExternalDispatchGrantError,
        ) as error:
            if isinstance(error, ExternalDispatchGrantError):
                raise
            raise ExternalDispatchGrantError(
                "external dispatch grant binding is corrupt"
            ) from error

    @classmethod
    def _validate_external_bootstrap_records(
        cls,
        descriptor: ExternalSourceDescriptor,
        batch: ExternalBootstrapBatch,
        grant: ExternalDispatchGrant,
    ) -> None:
        if not isinstance(descriptor, ExternalSourceDescriptor):
            raise ExternalBootstrapConflictError("external descriptor is invalid")
        for label, value in cls._external_descriptor_payload(descriptor).items():
            cls._validate_external_hash(value, str(label))
        if (
            not isinstance(batch, ExternalBootstrapBatch)
            or batch.state is not ExternalBootstrapState.PENDING
            or batch.availability != cls._EXTERNAL_BOOTSTRAP_AVAILABILITY
            or not batch.items
            or len(batch.items) > cls._MAX_EXTERNAL_BOOTSTRAP_BATCH_ITEMS
        ):
            raise ExternalBootstrapConflictError("external bootstrap batch is not pending")
        cls._validate_external_hash(batch.batch_hash, "batch_hash")
        cls._validate_external_hash(batch.descriptor_hash, "descriptor_hash")
        cls._validate_external_identifier(batch.idempotency_key, "idempotency_key")
        if batch.descriptor_hash != descriptor.descriptor_hash:
            raise ExternalBootstrapConflictError("batch does not bind the descriptor")
        _utc_timestamp(batch.expires_at)
        assignment_hashes: set[str] = set()
        for position, item in enumerate(batch.items):
            if (
                not isinstance(item, ExternalBootstrapBatchItem)
                or isinstance(item.item_index, bool)
                or item.item_index != position
                or isinstance(item.lease_epoch, bool)
                or item.lease_epoch < 0
            ):
                raise ExternalBootstrapConflictError("batch item ordering or lease is invalid")
            cls._validate_external_identifier(item.workflow_id, "workflow_id")
            cls._validate_external_identifier(item.task_id, "task_id")
            for label, value in cls._external_batch_item_payload(item).items():
                if str(label).endswith("_hash"):
                    cls._validate_external_hash(value, str(label))
            if item.assignment_hash in assignment_hashes:
                raise ExternalBootstrapConflictError("batch assignment hashes must be unique")
            assignment_hashes.add(item.assignment_hash)
        if (
            not isinstance(grant, ExternalDispatchGrant)
            or grant.state is not ExternalBootstrapState.PENDING
            or grant.availability != cls._EXTERNAL_BOOTSTRAP_AVAILABILITY
            or grant.consumed_at is not None
        ):
            raise ExternalBootstrapConflictError("external dispatch grant is not pending")
        cls._validate_external_identifier(grant.grant_id, "grant_id")
        for label, value in cls._external_grant_payload(grant).items():
            if str(label).endswith("_hash"):
                cls._validate_external_hash(value, str(label))
        _utc_timestamp(grant.expires_at)
        if (
            grant.descriptor_hash != descriptor.descriptor_hash
            or grant.batch_hash != batch.batch_hash
            or grant.expires_at != batch.expires_at
            or grant.assignment_hash not in assignment_hashes
        ):
            raise ExternalBootstrapConflictError("grant is not bound to descriptor batch assignment")

    @staticmethod
    def _insert_or_require_external_commitment(
        cursor: sqlite3.Cursor,
        table: str,
        values: Mapping[str, str],
        *,
        parent_exists: bool,
    ) -> None:
        identity_column, identity = next(iter(values.items()))
        row = cursor.execute(
            f"SELECT * FROM {table} WHERE {identity_column} = ?", (identity,)
        ).fetchone()
        if row is None:
            if parent_exists:
                raise ExternalBootstrapConflictError(
                    "legacy external bootstrap record lacks a commitment"
                )
            columns = tuple(values)
            cursor.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            return
        if any(str(row[column]) != value for column, value in values.items()):
            raise ExternalBootstrapConflictError("external bootstrap commitment conflicts")

    @staticmethod
    def _require_external_payload(
        cursor: sqlite3.Cursor,
        table: str,
        identity_column: str,
        identity: str,
        payload_json: str,
    ) -> None:
        row = cursor.execute(
            f"SELECT payload_json FROM {table} WHERE {identity_column} = ?", (identity,)
        ).fetchone()
        if row is not None and str(row["payload_json"]) != payload_json:
            raise ExternalBootstrapConflictError("external bootstrap binding conflicts")

    @staticmethod
    def _external_outbox_from_row(row: sqlite3.Row) -> ExternalBootstrapOutboxItem:
        return ExternalBootstrapOutboxItem(
            str(row["batch_hash"]),
            str(row["descriptor_hash"]),
            ExternalBootstrapState(str(row["state"])),
            str(row["availability"]),
            str(row["created_at"]),
        )

    @staticmethod
    def _external_grant_from_row(row: sqlite3.Row) -> ExternalDispatchGrant:
        return ExternalDispatchGrant(
            str(row["grant_id"]),
            str(row["descriptor_hash"]),
            str(row["batch_hash"]),
            str(row["assignment_hash"]),
            str(row["expires_at"]),
            ExternalBootstrapState(str(row["state"])),
            str(row["availability"]),
            None if row["consumed_at"] is None else str(row["consumed_at"]),
        )

    @classmethod
    def _preflight_external_bootstrap_rows(cls, cursor: sqlite3.Cursor) -> None:
        """Reject raw bootstrap drift before a schema migration can rewrite it."""
        try:
            descriptor_hashes = {
                cls._preflight_external_descriptor_row(row)
                for row in cursor.execute(
                    "SELECT * FROM external_bootstrap_descriptors"
                ).fetchall()
            }
            item_payloads_by_batch: dict[str, list[dict[str, object]]] = {}
            for row in cursor.execute(
                """
                SELECT * FROM external_bootstrap_batch_items
                ORDER BY batch_hash, item_index
                """
            ).fetchall():
                batch_hash, payload = cls._preflight_external_batch_item_row(row)
                item_payloads_by_batch.setdefault(batch_hash, []).append(payload)

            batches: dict[str, tuple[str, str, frozenset[str]]] = {}
            for row in cursor.execute(
                "SELECT * FROM external_bootstrap_batches"
            ).fetchall():
                batch_hash, descriptor_hash, expires_at, assignment_hashes = (
                    cls._preflight_external_batch_row(
                        row,
                        item_payloads_by_batch.pop(str(row["batch_hash"]), []),
                    )
                )
                if descriptor_hash not in descriptor_hashes:
                    raise ValueError("batch references an absent descriptor")
                batches[batch_hash] = (
                    descriptor_hash,
                    expires_at,
                    frozenset(assignment_hashes),
                )
            if item_payloads_by_batch:
                raise ValueError("batch item references an absent batch")

            for row in cursor.execute(
                "SELECT * FROM external_dispatch_grants"
            ).fetchall():
                cls._preflight_external_grant_row(row, batches, descriptor_hashes)
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ExternalDispatchGrantError,
        ) as error:
            raise ExternalBootstrapConflictError(
                "stored external bootstrap rows are not migration-safe"
            ) from error

    @classmethod
    def _preflight_external_descriptor_row(cls, row: sqlite3.Row) -> str:
        payload = cls._preflight_external_payload_from_row(row)
        expected_fields = {
            "commit_hash",
            "common_dir_hash",
            "descriptor_hash",
            "project_hash",
            "ref_hash",
            "repository_hash",
            "source_hash",
            "task_root_hash",
            "tree_hash",
        }
        if set(payload) != expected_fields or any(
            type(payload[field]) is not str for field in expected_fields
        ):
            raise ValueError("descriptor payload shape is invalid")
        descriptor = ExternalSourceDescriptor(
            descriptor_hash=payload["descriptor_hash"],
            source_hash=payload["source_hash"],
            repository_hash=payload["repository_hash"],
            common_dir_hash=payload["common_dir_hash"],
            project_hash=payload["project_hash"],
            task_root_hash=payload["task_root_hash"],
            ref_hash=payload["ref_hash"],
            commit_hash=payload["commit_hash"],
            tree_hash=payload["tree_hash"],
        )
        if str(row["descriptor_hash"]) != descriptor.descriptor_hash:
            raise ValueError("descriptor identity differs from its payload")
        for value in cls._external_descriptor_payload(descriptor).values():
            if cls._SHA256_IDENTIFIER_PATTERN.fullmatch(str(value)) is None:
                raise ValueError("descriptor hash is outside the bounded schema")
        return descriptor.descriptor_hash

    @classmethod
    def _preflight_external_batch_item_row(
        cls, row: sqlite3.Row
    ) -> tuple[str, dict[str, object]]:
        payload = cls._preflight_external_payload_from_row(row)
        legacy_fields = {
            "assignment_hash",
            "item_index",
            "lease_epoch",
            "plan_hash",
            "predecessor_hash",
            "projection_hash",
            "quota_hash",
            "route_hash",
            "task_id",
            "workflow_id",
        }
        current_fields = legacy_fields | {
            "index_hash",
            "lease_hash",
            "task_hash",
            "workflow_hash",
        }
        if set(payload) not in (legacy_fields, current_fields):
            raise ValueError("batch item payload shape is invalid")
        if (
            type(payload["item_index"]) is not int
            or type(payload["lease_epoch"]) is not int
            or payload["item_index"] < 0
            or payload["lease_epoch"] < 0
            or type(payload["workflow_id"]) is not str
            or type(payload["task_id"]) is not str
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(
                payload["workflow_id"]
            )
            is None
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(payload["task_id"])
            is None
        ):
            raise ValueError("batch item identifiers are invalid")
        for field, value in payload.items():
            if field.endswith("_hash") and (
                type(value) is not str
                or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(value) is None
            ):
                raise ValueError("batch item hash is outside the bounded schema")
        if (
            type(row["batch_hash"]) is not str
            or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(row["batch_hash"]) is None
            or type(row["item_index"]) is not int
            or row["item_index"] != payload["item_index"]
            or type(row["assignment_hash"]) is not str
            or row["assignment_hash"] != payload["assignment_hash"]
        ):
            raise ValueError("batch item row differs from its payload")
        return row["batch_hash"], payload

    @classmethod
    def _preflight_external_batch_row(
        cls,
        row: sqlite3.Row,
        item_payloads: list[dict[str, object]],
    ) -> tuple[str, str, str, set[str]]:
        payload = cls._preflight_external_payload_from_row(row)
        expected_fields = {
            "availability",
            "batch_hash",
            "descriptor_hash",
            "expires_at",
            "idempotency_key",
            "items",
            "state",
        }
        if set(payload) != expected_fields or type(payload["items"]) is not list:
            raise ValueError("batch payload shape is invalid")
        if any(
            type(payload[field]) is not str
            for field in expected_fields - {"items"}
        ):
            raise ValueError("batch payload values are invalid")
        if (
            payload["state"] != ExternalBootstrapState.PENDING.value
            or payload["availability"] != cls._EXTERNAL_BOOTSTRAP_AVAILABILITY
            or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(payload["batch_hash"])
            is None
            or cls._SHA256_IDENTIFIER_PATTERN.fullmatch(payload["descriptor_hash"])
            is None
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(
                payload["idempotency_key"]
            )
            is None
        ):
            raise ValueError("batch payload binding is invalid")
        _utc_timestamp(payload["expires_at"])
        if any(
            str(row[field]) != payload[field]
            for field in (
                "batch_hash",
                "descriptor_hash",
                "idempotency_key",
                "expires_at",
                "state",
                "availability",
            )
        ):
            raise ValueError("batch row differs from its payload")
        if (
            not item_payloads
            or len(item_payloads) > cls._MAX_EXTERNAL_BOOTSTRAP_BATCH_ITEMS
            or payload["items"] != item_payloads
        ):
            raise ValueError("batch items differ from their rows")
        item_indexes = [item["item_index"] for item in item_payloads]
        assignment_hashes = {str(item["assignment_hash"]) for item in item_payloads}
        if item_indexes != list(range(len(item_payloads))) or len(
            assignment_hashes
        ) != len(item_payloads):
            raise ValueError("batch item ordering or bindings are invalid")
        return (
            payload["batch_hash"],
            payload["descriptor_hash"],
            payload["expires_at"],
            assignment_hashes,
        )

    @classmethod
    def _preflight_external_grant_row(
        cls,
        row: sqlite3.Row,
        batches: Mapping[str, tuple[str, str, frozenset[str]]],
        descriptor_hashes: set[str],
    ) -> None:
        payload = cls._preflight_external_payload_from_row(row)
        expected_fields = {
            "assignment_hash",
            "availability",
            "batch_hash",
            "descriptor_hash",
            "expires_at",
            "grant_id",
            "state",
        }
        if set(payload) != expected_fields or any(
            type(payload[field]) is not str for field in expected_fields
        ):
            raise ValueError("grant payload shape is invalid")
        if (
            payload["state"] != ExternalBootstrapState.PENDING.value
            or payload["availability"] != cls._EXTERNAL_BOOTSTRAP_AVAILABILITY
            or cls._SAFE_ACCEPTANCE_IDENTIFIER_PATTERN.fullmatch(payload["grant_id"])
            is None
            or any(
                cls._SHA256_IDENTIFIER_PATTERN.fullmatch(payload[field]) is None
                for field in ("assignment_hash", "batch_hash", "descriptor_hash")
            )
        ):
            raise ValueError("grant payload binding is invalid")
        _utc_timestamp(payload["expires_at"])
        if any(
            str(row[field]) != payload[field]
            for field in (
                "grant_id",
                "descriptor_hash",
                "batch_hash",
                "assignment_hash",
                "expires_at",
                "state",
                "availability",
            )
        ):
            raise ValueError("grant row differs from its payload")
        batch = batches.get(payload["batch_hash"])
        if (
            payload["descriptor_hash"] not in descriptor_hashes
            or batch is None
            or batch[0] != payload["descriptor_hash"]
            or batch[1] != payload["expires_at"]
            or payload["assignment_hash"] not in batch[2]
        ):
            raise ValueError("grant does not bind its raw batch")

    @staticmethod
    def _preflight_external_payload_from_row(row: sqlite3.Row) -> dict[str, object]:
        payload_json = row["payload_json"]
        payload_hash = row["payload_hash"]
        if type(payload_json) is not str or type(payload_hash) is not str:
            raise ValueError("external payload storage types are invalid")
        payload = json.loads(payload_json)
        if (
            not isinstance(payload, dict)
            or payload_json != _canonical_payload_json(payload)
            or payload_hash != _payload_hash(payload_json)
        ):
            raise ValueError("external payload JSON or hash is invalid")
        return payload

    @staticmethod
    def _canonicalize_external_bootstrap_expiries(cursor: sqlite3.Cursor) -> None:
        for table, identity_column in (
            ("external_bootstrap_batches", "batch_hash"),
            ("external_dispatch_grants", "grant_id"),
        ):
            rows = cursor.execute(
                f"SELECT {identity_column}, expires_at, payload_json, payload_hash FROM {table}"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"]))
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                    raw_expiry = payload["expires_at"]
                    if not isinstance(raw_expiry, str):
                        raise ValueError("expiry must be a string")
                    raw_payload_json = _canonical_payload_json(payload)
                    if (
                        str(row["expires_at"]) != raw_expiry
                        or str(row["payload_json"]) != raw_payload_json
                        or str(row["payload_hash"])
                        != _payload_hash(raw_payload_json)
                    ):
                        raise ValueError("raw expiry column, payload, and hash disagree")
                    payload["expires_at"] = _utc_timestamp(raw_expiry)
                    canonical_payload = _canonical_payload_json(payload)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ExternalBootstrapConflictError(
                        "stored external bootstrap expiry is not canonicalizable"
                    ) from error
                cursor.execute(
                    f"""
                    UPDATE {table}
                    SET expires_at = ?, payload_json = ?, payload_hash = ?
                    WHERE {identity_column} = ?
                    """,
                    (
                        str(payload["expires_at"]),
                        canonical_payload,
                        _payload_hash(canonical_payload),
                        str(row[identity_column]),
                    ),
                )

    @classmethod
    def _validate_legacy_atlas_outbox_shape(cls, cursor: sqlite3.Cursor) -> None:
        """Prove the v6-v10 outbox is the known legacy DDL before rebuilding it."""

        expected_columns = tuple(
            (
                name,
                column_type.casefold(),
                0 if name == "ingestion_key" else not_null,
                primary_key,
                0,
                None,
            )
            for name, column_type, not_null, primary_key in _ATLAS_OUTBOX_COLUMN_CONTRACT
        )
        actual_columns = tuple(
            (
                str(row["name"]),
                str(row["type"]).casefold(),
                int(row["notnull"]),
                int(row["pk"]),
                int(row["hidden"]),
                row["dflt_value"],
            )
            for row in cursor.execute(
                "PRAGMA table_xinfo(atlas_ingestion_outbox)"
            ).fetchall()
        )
        if actual_columns != expected_columns:
            raise StoreError("orchestrator store is not prepared")

        table_sql = _table_sql(cursor, "atlas_ingestion_outbox")
        table_tokens = _sqlite_schema_tokens(table_sql)
        for index, token in enumerate(table_tokens):
            if token == "collate" and (
                index + 1 == len(table_tokens) or table_tokens[index + 1] != "binary"
            ):
                raise StoreError("orchestrator store is not prepared")

        def index_columns(index_name: object) -> tuple[str, ...]:
            if type(index_name) is not str:
                raise ValueError("index name is invalid")
            identifier = index_name.replace('"', '""')
            return tuple(
                str(row["name"])
                for row in cursor.execute(f'PRAGMA index_info("{identifier}")').fetchall()
            )

        def validate_index_xinfo(
            index_name: object, expected_columns: tuple[str, ...]
        ) -> None:
            if type(index_name) is not str:
                raise ValueError("index name is invalid")
            identifier = index_name.replace('"', '""')
            info_rows = cursor.execute(
                f'PRAGMA index_xinfo("{identifier}")'
            ).fetchall()
            key_rows = [row for row in info_rows if int(row["key"])]
            if len(key_rows) != len(expected_columns):
                raise StoreError("orchestrator store is not prepared")
            for sequence, (row, expected_column) in enumerate(
                zip(key_rows, expected_columns, strict=True)
            ):
                if (
                    int(row["seqno"]) != sequence
                    or int(row["cid"]) != _ATLAS_OUTBOX_COLUMNS.index(expected_column)
                    or row["name"] != expected_column
                    or str(row["coll"]).casefold() != "binary"
                    or int(row["desc"]) != 0
                    or int(row["key"]) != 1
                ):
                    raise StoreError("orchestrator store is not prepared")
            non_key_rows = [row for row in info_rows if not int(row["key"])]
            if len(non_key_rows) != 1:
                raise StoreError("orchestrator store is not prepared")
            non_key = non_key_rows[0]
            if (
                int(non_key["seqno"]) != len(expected_columns)
                or int(non_key["cid"]) != -1
                or non_key["name"] is not None
                or str(non_key["coll"]).casefold() != "binary"
                or int(non_key["desc"]) != 0
                or int(non_key["key"]) != 0
            ):
                raise StoreError("orchestrator store is not prepared")

        unique_indexes: set[tuple[tuple[str, ...], str]] = set()
        non_unique_indexes: set[tuple[tuple[str, ...], str, str]] = set()
        index_rows = cursor.execute(
            "PRAGMA index_list(atlas_ingestion_outbox)"
        ).fetchall()
        for index in index_rows:
            if int(index["partial"]):
                raise StoreError("orchestrator store is not prepared")
            columns = index_columns(index["name"])
            validate_index_xinfo(index["name"], columns)
            origin = str(index["origin"]).casefold()
            if int(index["unique"]):
                unique_indexes.add((columns, origin))
            else:
                non_unique_indexes.add((columns, origin, str(index["name"])))
        expected_unique_indexes = {
            (("ingestion_key",), "pk"),
            (("acceptance_id",), "u"),
            (("payload_hash",), "u"),
        }
        expected_non_unique_indexes = {
            (("state", "created_at", "ingestion_key"), "c", "idx_atlas_outbox_pending")
        }
        if (
            unique_indexes != expected_unique_indexes
            or non_unique_indexes != expected_non_unique_indexes
            or len(index_rows)
            != len(expected_unique_indexes) + len(expected_non_unique_indexes)
        ):
            raise StoreError("orchestrator store is not prepared")

        expected_foreign_keys = frozenset(
            {
                (
                    (
                        "acceptance_id",
                        "code_task_acceptances",
                        "acceptance_id",
                        "no action",
                        "no action",
                        "none",
                    ),
                )
            }
        )
        if (
            _foreign_key_contract(cursor, "atlas_ingestion_outbox")
            != expected_foreign_keys
            or _sqlite_check_expressions(table_sql) != _ATLAS_OUTBOX_REQUIRED_CHECKS
        ):
            raise StoreError("orchestrator store is not prepared")

    @classmethod
    def _validate_legacy_atlas_outbox_rows(cls, cursor: sqlite3.Cursor) -> None:
        """Reject null or non-convertible legacy rows before the table swap."""

        text_columns = {
            "ingestion_key",
            "acceptance_id",
            "payload_json",
            "payload_hash",
            "state",
            "last_error_code",
            "reason_codes_json",
            "created_at",
            "updated_at",
        }
        rows = cursor.execute("SELECT * FROM atlas_ingestion_outbox").fetchall()
        for row in rows:
            if any(row[column] is None for column in _ATLAS_OUTBOX_COLUMNS):
                raise StoreError("legacy atlas outbox row is invalid")
            if any(type(row[column]) is not str for column in text_columns):
                raise StoreError("legacy atlas outbox row is invalid")
            if type(row["attempt_count"]) is not int:
                raise StoreError("legacy atlas outbox row is invalid")
            if (
                row["state"] not in {"pending", "projected", "quarantined"}
                or not 0 <= row["attempt_count"] <= cls._MAX_ATLAS_OUTBOX_ATTEMPTS
                or row["ingestion_key"] != row["payload_hash"]
                or row["acceptance_id"] != row["ingestion_key"]
                or (
                    row["state"] == "projected"
                    and row["last_error_code"] != ""
                )
                or (
                    row["state"] == "quarantined"
                    and not row["last_error_code"]
                )
                or (
                    row["state"] == "pending"
                    and row["attempt_count"] > 0
                    and not row["last_error_code"]
                )
                or (
                    row["state"] == "pending"
                    and row["attempt_count"] == 0
                    and row["last_error_code"] != ""
                )
            ):
                raise StoreError("legacy atlas outbox row is invalid")
            try:
                acceptance = cursor.execute(
                    """
                    SELECT acceptance_id, workflow_id, code_task_id,
                           code_task_version, input_snapshot_id,
                           output_snapshot_id, indexed_diff_hash, intent_id,
                           language, framework, payload_json, payload_hash
                    FROM code_task_acceptances
                    WHERE acceptance_id = ?
                    """,
                    (row["acceptance_id"],),
                ).fetchone()
                if acceptance is None:
                    raise ValueError("referenced acceptance is missing")
                canonical_acceptance_payload = (
                    cls._canonical_code_task_acceptance_payload(
                        workflow_id=acceptance["workflow_id"],
                        task_id=acceptance["code_task_id"],
                        task_version=acceptance["code_task_version"],
                        input_snapshot_id=acceptance["input_snapshot_id"],
                        output_snapshot_id=acceptance["output_snapshot_id"],
                        indexed_diff_hash=acceptance["indexed_diff_hash"],
                        intent_id=acceptance["intent_id"],
                        language=acceptance["language"],
                        framework=acceptance["framework"],
                    )
                )
                canonical_acceptance_hash = _payload_hash(
                    canonical_acceptance_payload
                )
                if (
                    row["acceptance_id"] != acceptance["acceptance_id"]
                    or row["ingestion_key"] != acceptance["payload_hash"]
                    or row["acceptance_id"] != acceptance["payload_hash"]
                    or row["payload_json"] != acceptance["payload_json"]
                    or acceptance["payload_json"] != canonical_acceptance_payload
                    or acceptance["payload_hash"] != canonical_acceptance_hash
                    or acceptance["acceptance_id"] != canonical_acceptance_hash
                    or row["payload_hash"] != _payload_hash(row["payload_json"])
                    or row["payload_hash"] != canonical_acceptance_hash
                ):
                    raise ValueError("legacy acceptance content address mismatch")
                if (
                    row["created_at"] != _utc_timestamp(row["created_at"])
                    or row["updated_at"] != _utc_timestamp(row["updated_at"])
                ):
                    raise ValueError("outbox timestamps are not canonical UTC")
                cls._safe_acceptance_identifier("ingestion_key", row["ingestion_key"])
                cls._safe_acceptance_identifier("acceptance_id", row["acceptance_id"])
                cls._safe_acceptance_identifier("payload_hash", row["payload_hash"])
                cls._safe_outbox_code(
                    "last_error_code", row["last_error_code"], allow_empty=True
                )
                reason_codes = _decode_outbox_reason_codes(row["reason_codes_json"])
                if len(reason_codes) > cls._MAX_SAFE_OUTBOX_REASON_COUNT:
                    raise ValueError("too many outbox reason codes")
                for reason_code in reason_codes:
                    cls._safe_outbox_code("reason_code", reason_code)
            except (StoreError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise StoreError("legacy atlas outbox row is invalid") from error

    @classmethod
    def _migrate_atlas_outbox_ingestion_key_not_null(
        cls, cursor: sqlite3.Cursor
    ) -> None:
        """Rebuild the legacy outbox because SQLite cannot alter a primary key nullability."""

        try:
            columns = {
                str(row["name"]): row
                for row in cursor.execute(
                    "PRAGMA table_info(atlas_ingestion_outbox)"
                ).fetchall()
            }
            required_columns = {
                "ingestion_key",
                "acceptance_id",
                "payload_json",
                "payload_hash",
                "state",
                "attempt_count",
                "last_error_code",
                "reason_codes_json",
                "created_at",
                "updated_at",
            }
            if set(columns) != required_columns:
                raise StoreError("orchestrator store is not prepared")
            source_version_row = cursor.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            ingestion_key = columns["ingestion_key"]
            if source_version_row is None:
                if int(ingestion_key["notnull"]):
                    return
                raise StoreError("orchestrator store is not prepared")
            source_version_value = source_version_row["value"]
            if type(source_version_value) is not str:
                raise StoreError("orchestrator store is not prepared")
            try:
                source_version = int(source_version_value)
            except (TypeError, ValueError) as error:
                raise StoreError("orchestrator store is not prepared") from error
            if str(source_version) != source_version_value or not (
                6 <= source_version <= cls._SCHEMA_VERSION
            ):
                raise StoreError("orchestrator store is not prepared")
            if int(ingestion_key["notnull"]):
                return
            if source_version not in range(6, 11):
                raise StoreError("orchestrator store is not prepared")
            cls._validate_legacy_atlas_outbox_rows(cursor)
            cursor.execute(
                """
                CREATE TABLE atlas_ingestion_outbox_v11 (
                    ingestion_key TEXT NOT NULL PRIMARY KEY,
                    acceptance_id TEXT NOT NULL UNIQUE
                        REFERENCES code_task_acceptances(acceptance_id),
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'projected', 'quarantined')),
                    attempt_count INTEGER NOT NULL
                        CHECK (attempt_count BETWEEN 0 AND 16),
                    last_error_code TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (ingestion_key = payload_hash)
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO atlas_ingestion_outbox_v11 (
                    ingestion_key, acceptance_id, payload_json, payload_hash, state,
                    attempt_count, last_error_code, reason_codes_json, created_at, updated_at
                )
                SELECT
                    ingestion_key, acceptance_id, payload_json, payload_hash, state,
                    attempt_count, last_error_code, reason_codes_json, created_at, updated_at
                FROM atlas_ingestion_outbox
                """
            )
            cls._drop_atlas_finalization_projection_trigger_for_outbox_rebuild(
                cursor
            )
            cursor.execute("DROP TABLE atlas_ingestion_outbox")
            cursor.execute(
                "ALTER TABLE atlas_ingestion_outbox_v11 RENAME TO atlas_ingestion_outbox"
            )
            cursor.execute(
                """
                CREATE INDEX idx_atlas_outbox_pending
                    ON atlas_ingestion_outbox(state, created_at, ingestion_key)
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX idx_atlas_outbox_finalization_identity
                    ON atlas_ingestion_outbox(
                        acceptance_id, ingestion_key, payload_hash
                    )
                """
            )
            cls._restore_atlas_finalization_projection_trigger_after_outbox_rebuild(
                cursor
            )
        except sqlite3.IntegrityError as error:
            raise StoreError("legacy atlas outbox row is invalid") from error
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator schema is corrupt") from error

    @classmethod
    def _preflight_legacy_atlas_outbox_before_schema_ddl(
        cls, cursor: sqlite3.Cursor, *, fresh_database: bool
    ) -> None:
        """Validate legacy outbox DDL before current CREATE/INDEX statements run."""

        if fresh_database:
            return
        try:
            source_version = _schema_version_from_connection(cursor)
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator store is not prepared") from error
        if source_version is None or source_version not in range(6, 11):
            return
        try:
            cls._validate_legacy_atlas_outbox_shape(cursor)
        except StoreError:
            raise
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreError("orchestrator store is not prepared") from error

    @staticmethod
    def _drop_atlas_finalization_projection_trigger_for_outbox_rebuild(
        cursor: sqlite3.Cursor,
    ) -> None:
        """Remove only the verified projection guard while its outbox is rebuilt."""

        trigger_name = "atlas_finalizations_require_projected_outbox"
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        expected_tokens = _atlas_finalization_trigger_tokens()[trigger_name]
        if row is None or type(row["sql"]) is not str:
            raise StoreError("orchestrator store is not prepared")
        if _sqlite_schema_tokens(str(row["sql"])) != expected_tokens:
            raise StoreError("orchestrator store is not prepared")
        cursor.execute("DROP TRIGGER atlas_finalizations_require_projected_outbox")

    @staticmethod
    def _restore_atlas_finalization_projection_trigger_after_outbox_rebuild(
        cursor: sqlite3.Cursor,
    ) -> None:
        """Restore the canonical O v13 projected-outbox guard after the swap."""

        _execute_schema_statements(
            cursor,
            """
            CREATE TRIGGER atlas_finalizations_require_projected_outbox
            BEFORE INSERT ON atlas_finalizations
            WHEN NOT EXISTS (
                SELECT 1 FROM atlas_ingestion_outbox AS outbox
                WHERE outbox.acceptance_id = NEW.acceptance_id
                  AND outbox.ingestion_key = NEW.ingestion_key
                  AND outbox.payload_hash = NEW.payload_hash
                  AND outbox.state = 'projected'
            )
            BEGIN
                SELECT RAISE(
                    ABORT, 'atlas finalization requires projected exact outbox'
                );
            END;
            """,
        )

    def _create_schema(self) -> None:
        with self._transaction() as cursor:
            try:
                fresh_database = cursor.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    LIMIT 1
                    """
                ).fetchone() is None
            except sqlite3.DatabaseError as error:
                raise StoreError("orchestrator schema is corrupt") from error
            self._preflight_legacy_atlas_outbox_before_schema_ddl(
                cursor, fresh_database=fresh_database
            )
            _execute_schema_statements(
                cursor,
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT NOT NULL PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    product_summary TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    title TEXT NOT NULL,
                    owner_role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    write_scope TEXT NOT NULL,
                    card_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    task_kind TEXT NOT NULL DEFAULT 'general',
                    intent_id TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    framework TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_workflow_state ON tasks(workflow_id, state);
                CREATE TABLE IF NOT EXISTS code_task_acceptances (
                    acceptance_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    code_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    code_task_version INTEGER NOT NULL,
                    input_snapshot_id TEXT NOT NULL,
                    output_snapshot_id TEXT NOT NULL,
                    indexed_diff_hash TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    CHECK (acceptance_id = payload_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_code_task_acceptances_workflow
                    ON code_task_acceptances(workflow_id, created_at, acceptance_id);
                CREATE TABLE IF NOT EXISTS code_task_receipt_attestations (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    code_task_version INTEGER NOT NULL,
                    input_snapshot_id TEXT NOT NULL,
                    output_snapshot_id TEXT NOT NULL,
                    workspace_hash TEXT NOT NULL,
                    execution_receipt_ids TEXT NOT NULL,
                    attestation_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE (task_id, code_task_version, attestation_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_code_task_receipt_attestations_workflow
                    ON code_task_receipt_attestations(
                        workflow_id, code_task_version, task_id
                    );
                CREATE TABLE IF NOT EXISTS code_task_receipt_owners (
                    receipt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    code_task_version INTEGER NOT NULL,
                    attestation_hash TEXT NOT NULL,
                    FOREIGN KEY (task_id, code_task_version, attestation_hash)
                        REFERENCES code_task_receipt_attestations(
                            task_id, code_task_version, attestation_hash
                        )
                );
                CREATE INDEX IF NOT EXISTS idx_code_task_receipt_owners_task
                    ON code_task_receipt_owners(
                        task_id, code_task_version, attestation_hash, receipt_id
                    );
                CREATE TABLE IF NOT EXISTS atlas_ingestion_outbox (
                    ingestion_key TEXT NOT NULL PRIMARY KEY,
                    acceptance_id TEXT NOT NULL UNIQUE
                        REFERENCES code_task_acceptances(acceptance_id),
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'projected', 'quarantined')),
                    attempt_count INTEGER NOT NULL
                        CHECK (attempt_count BETWEEN 0 AND 16),
                    last_error_code TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (ingestion_key = payload_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_atlas_outbox_pending
                    ON atlas_ingestion_outbox(state, created_at, ingestion_key);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_atlas_outbox_finalization_identity
                    ON atlas_ingestion_outbox(
                        acceptance_id, ingestion_key, payload_hash
                    );
                CREATE TABLE IF NOT EXISTS atlas_finalizations (
                    schema_version TEXT NOT NULL
                        CHECK (schema_version = 'atlas-finalization/v1'),
                    acceptance_id TEXT NOT NULL UNIQUE
                        REFERENCES code_task_acceptances(acceptance_id),
                    ingestion_key TEXT NOT NULL UNIQUE
                        REFERENCES atlas_ingestion_outbox(ingestion_key),
                    payload_hash TEXT NOT NULL,
                    continuity_key_hash TEXT NOT NULL,
                    view_id TEXT NOT NULL,
                    fence_epoch INTEGER NOT NULL CHECK (fence_epoch > 0),
                    pointer_version INTEGER NOT NULL CHECK (pointer_version > 0),
                    published_receipt_hash TEXT NOT NULL,
                    atlas_receipt_digest TEXT NOT NULL,
                    finalization_hash TEXT NOT NULL PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (acceptance_id, ingestion_key, payload_hash)
                        REFERENCES atlas_ingestion_outbox(
                            acceptance_id, ingestion_key, payload_hash
                        )
                );
                CREATE TRIGGER IF NOT EXISTS atlas_finalizations_no_update
                BEFORE UPDATE ON atlas_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'atlas finalization is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS atlas_finalizations_no_delete
                BEFORE DELETE ON atlas_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'atlas finalization is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS atlas_finalizations_require_projected_outbox
                BEFORE INSERT ON atlas_finalizations
                WHEN NOT EXISTS (
                    SELECT 1 FROM atlas_ingestion_outbox AS outbox
                    WHERE outbox.acceptance_id = NEW.acceptance_id
                      AND outbox.ingestion_key = NEW.ingestion_key
                      AND outbox.payload_hash = NEW.payload_hash
                      AND outbox.state = 'projected'
                )
                BEGIN
                    SELECT RAISE(
                        ABORT, 'atlas finalization requires projected exact outbox'
                    );
                END;
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    dependency_id TEXT NOT NULL REFERENCES tasks(id),
                    PRIMARY KEY (task_id, dependency_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_dependencies_dependency ON task_dependencies(dependency_id);
                CREATE TABLE IF NOT EXISTS lease_epochs (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    owner TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    host_target TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    task_id TEXT REFERENCES tasks(id),
                    event_type TEXT NOT NULL,
                    redacted_payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_workflow_sequence ON events(workflow_id, sequence);
                CREATE TABLE IF NOT EXISTS artifacts (
                    content_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    safe_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    redaction_version TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_inputs (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    input_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_owners (
                    content_hash TEXT PRIMARY KEY REFERENCES artifacts(content_hash),
                    task_id TEXT NOT NULL REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS task_cards (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    card_hash TEXT NOT NULL,
                    card_body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_contract_subscriptions (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    contract_hash TEXT NOT NULL,
                    PRIMARY KEY (task_id, contract_hash)
                );
                CREATE TABLE IF NOT EXISTS task_required_evidence (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    position INTEGER NOT NULL,
                    evidence TEXT NOT NULL,
                    PRIMARY KEY (task_id, position)
                );
                CREATE TABLE IF NOT EXISTS task_index_bindings (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    workspace_root TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    input_snapshot_id TEXT NOT NULL,
                    output_snapshot_id TEXT NOT NULL,
                    task_node_ids TEXT NOT NULL,
                    contract_node_ids TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    indexed_diff_hash TEXT NOT NULL,
                    fallback_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_index_query_receipts (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    trace_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    miss_escape_used INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, trace_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_index_query_snapshot
                    ON task_index_query_receipts(task_id, snapshot_id);
                CREATE TABLE IF NOT EXISTS task_index_verification_artifacts (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    content_hash TEXT NOT NULL REFERENCES artifacts(content_hash),
                    snapshot_id TEXT NOT NULL,
                    PRIMARY KEY (task_id, content_hash)
                );
                CREATE TABLE IF NOT EXISTS task_index_binding_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    event_type TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS peer_capabilities (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    sender_task_id TEXT NOT NULL REFERENCES tasks(id),
                    recipient_task_id TEXT NOT NULL REFERENCES tasks(id),
                    relationship TEXT NOT NULL,
                    capability TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (workflow_id, sender_task_id, recipient_task_id, relationship)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    sender_task_id TEXT NOT NULL REFERENCES tasks(id),
                    recipient_task_id TEXT NOT NULL REFERENCES tasks(id),
                    correlation_id TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL REFERENCES artifacts(content_hash),
                    redacted_metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE (workflow_id, sender_task_id, recipient_task_id, correlation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_inbox
                    ON messages(workflow_id, recipient_task_id, sequence);
                CREATE TABLE IF NOT EXISTS role_envelopes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    sender_task_id TEXT NOT NULL REFERENCES tasks(id),
                    recipient_task_id TEXT NOT NULL REFERENCES tasks(id),
                    direction TEXT NOT NULL CHECK (direction IN (
                        'coordinator_to_worker', 'worker_to_coordinator', 'peer_to_peer'
                    )),
                    sender_role TEXT NOT NULL,
                    recipient_role TEXT NOT NULL,
                    sender_epoch INTEGER NOT NULL,
                    recipient_epoch INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL,
                    assignment_token_hash TEXT NOT NULL,
                    dispatch_context_hash TEXT NOT NULL,
                    route_provenance_hash TEXT NOT NULL,
                    coordinator_task_id TEXT NOT NULL REFERENCES tasks(id),
                    coordinator_epoch INTEGER NOT NULL,
                    correlation_fence_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    envelope_hash TEXT NOT NULL,
                    reference_bytes INTEGER NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE (workflow_id, sender_task_id, recipient_task_id, correlation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_role_envelopes_recipient_inbox
                    ON role_envelopes(workflow_id, recipient_task_id, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_role_envelopes_envelope_hash
                    ON role_envelopes(envelope_hash) WHERE envelope_hash IS NOT NULL;
                CREATE TABLE IF NOT EXISTS host_operation_receipts (
                    operation_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    operation TEXT NOT NULL CHECK (operation = 'archive'),
                    lease_epoch INTEGER NOT NULL,
                    assignment_token_hash TEXT NOT NULL,
                    dispatch_context_hash TEXT NOT NULL,
                    route_provenance_hash TEXT NOT NULL,
                    coordinator_task_id TEXT NOT NULL REFERENCES tasks(id),
                    coordinator_epoch INTEGER NOT NULL,
                    errno INTEGER NOT NULL,
                    status_code TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('blocked', 'reported')),
                    receipt_hash TEXT NOT NULL,
                    reported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_host_operation_receipts_task
                    ON host_operation_receipts(workflow_id, task_id, operation_id);
                CREATE TABLE IF NOT EXISTS external_bootstrap_descriptors (
                    descriptor_hash TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_bootstrap_batches (
                    batch_hash TEXT PRIMARY KEY,
                    descriptor_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_descriptors(descriptor_hash),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state = 'pending'),
                    availability TEXT NOT NULL CHECK (availability = 'HOST_API_UNAVAILABLE'),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_external_bootstrap_batches_descriptor
                    ON external_bootstrap_batches(descriptor_hash, created_at, batch_hash);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_external_bootstrap_batches_binding
                    ON external_bootstrap_batches(batch_hash, descriptor_hash);
                CREATE TABLE IF NOT EXISTS external_bootstrap_batch_items (
                    batch_hash TEXT NOT NULL REFERENCES external_bootstrap_batches(batch_hash),
                    item_index INTEGER NOT NULL CHECK (item_index >= 0),
                    assignment_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY (batch_hash, item_index),
                    UNIQUE (batch_hash, assignment_hash)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_external_bootstrap_batch_items_binding
                    ON external_bootstrap_batch_items(batch_hash, assignment_hash);
                CREATE TABLE IF NOT EXISTS external_bootstrap_outbox (
                    batch_hash TEXT PRIMARY KEY
                        REFERENCES external_bootstrap_batches(batch_hash),
                    descriptor_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_descriptors(descriptor_hash),
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state = 'pending'),
                    availability TEXT NOT NULL CHECK (availability = 'HOST_API_UNAVAILABLE'),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_external_bootstrap_outbox_pending
                    ON external_bootstrap_outbox(state, created_at, batch_hash);
                CREATE TABLE IF NOT EXISTS external_dispatch_grants (
                    grant_id TEXT PRIMARY KEY,
                    descriptor_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_descriptors(descriptor_hash),
                    batch_hash TEXT NOT NULL REFERENCES external_bootstrap_batches(batch_hash),
                    assignment_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state = 'pending'),
                    availability TEXT NOT NULL CHECK (availability = 'HOST_API_UNAVAILABLE'),
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (descriptor_hash, batch_hash, assignment_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_external_dispatch_grants_pending
                    ON external_dispatch_grants(batch_hash, assignment_hash, expires_at)
                    WHERE consumed_at IS NULL;
                CREATE TABLE IF NOT EXISTS external_dispatch_grant_bindings (
                    grant_id TEXT PRIMARY KEY
                        REFERENCES external_dispatch_grants(grant_id),
                    descriptor_hash TEXT NOT NULL,
                    batch_hash TEXT NOT NULL,
                    assignment_hash TEXT NOT NULL,
                    FOREIGN KEY (batch_hash, descriptor_hash)
                        REFERENCES external_bootstrap_batches(batch_hash, descriptor_hash),
                    FOREIGN KEY (batch_hash, assignment_hash)
                        REFERENCES external_bootstrap_batch_items(batch_hash, assignment_hash),
                    UNIQUE (descriptor_hash, batch_hash, assignment_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_external_dispatch_grant_bindings_batch
                    ON external_dispatch_grant_bindings(
                        batch_hash, assignment_hash, descriptor_hash, grant_id
                    );
                CREATE TABLE IF NOT EXISTS external_bootstrap_batch_commitments (
                    batch_hash TEXT PRIMARY KEY
                        REFERENCES external_bootstrap_batches(batch_hash),
                    descriptor_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_descriptors(descriptor_hash),
                    descriptor_payload_hash TEXT NOT NULL,
                    batch_payload_hash TEXT NOT NULL,
                    FOREIGN KEY (batch_hash, descriptor_hash)
                        REFERENCES external_bootstrap_batches(batch_hash, descriptor_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_external_bootstrap_batch_commitments_descriptor
                    ON external_bootstrap_batch_commitments(
                        descriptor_hash, descriptor_payload_hash, batch_payload_hash
                    );
                CREATE TABLE IF NOT EXISTS external_dispatch_grant_commitments (
                    grant_id TEXT PRIMARY KEY
                        REFERENCES external_dispatch_grants(grant_id),
                    descriptor_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_descriptors(descriptor_hash),
                    batch_hash TEXT NOT NULL
                        REFERENCES external_bootstrap_batches(batch_hash),
                    assignment_hash TEXT NOT NULL,
                    descriptor_payload_hash TEXT NOT NULL,
                    batch_payload_hash TEXT NOT NULL,
                    grant_payload_hash TEXT NOT NULL,
                    FOREIGN KEY (batch_hash, descriptor_hash)
                        REFERENCES external_bootstrap_batches(batch_hash, descriptor_hash),
                    FOREIGN KEY (batch_hash, assignment_hash)
                        REFERENCES external_bootstrap_batch_items(batch_hash, assignment_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_external_dispatch_grant_commitments_batch
                    ON external_dispatch_grant_commitments(
                        batch_hash, assignment_hash, descriptor_hash, grant_id
                    );
                """,
            )
            self._migrate_schema_metadata_key_not_null(
                cursor,
                fresh_database=fresh_database,
            )
            self._validate_schema_metadata_layout(cursor)
            self._migrate_atlas_outbox_ingestion_key_not_null(cursor)
            self._migrate_atlas_finalization_binding(cursor)
            self._preflight_external_bootstrap_rows(cursor)
            self._canonicalize_external_bootstrap_expiries(cursor)
            cursor.execute(
                """
                INSERT OR IGNORE INTO external_dispatch_grant_bindings
                    (grant_id, descriptor_hash, batch_hash, assignment_hash)
                SELECT grant.grant_id, grant.descriptor_hash, grant.batch_hash,
                       grant.assignment_hash
                FROM external_dispatch_grants AS grant
                JOIN external_bootstrap_batches AS batch
                    ON batch.batch_hash = grant.batch_hash
                   AND batch.descriptor_hash = grant.descriptor_hash
                JOIN external_bootstrap_batch_items AS item
                    ON item.batch_hash = grant.batch_hash
                   AND item.assignment_hash = grant.assignment_hash
                """
            )
            columns = {
                str(row["name"])
                for row in cursor.execute("PRAGMA table_info(leases)").fetchall()
            }
            if "host_target" not in columns:
                cursor.execute("ALTER TABLE leases ADD COLUMN host_target TEXT")
            task_columns = {
                str(row["name"])
                for row in cursor.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "task_kind" not in task_columns:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'general'"
                )
            if "intent_id" not in task_columns:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN intent_id TEXT NOT NULL DEFAULT ''"
                )
            if "language" not in task_columns:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN language TEXT NOT NULL DEFAULT ''"
                )
            if "framework" not in task_columns:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN framework TEXT NOT NULL DEFAULT ''"
                )
            binding_columns = {
                str(row["name"])
                for row in cursor.execute(
                    "PRAGMA table_info(task_index_bindings)"
                ).fetchall()
            }
            if "workspace_id" not in binding_columns:
                cursor.execute(
                    "ALTER TABLE task_index_bindings "
                    "ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''"
                )
            cursor.execute(
                """
                INSERT INTO schema_metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("schema_version", str(self._SCHEMA_VERSION)),
            )
            connection = self._connection
            if connection is None:
                raise StoreError("orchestrator store is not prepared")
            self.validate_prepared_connection(connection)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        connection = self._connection
        if connection is None:
            raise StoreError("orchestrator store is not prepared")
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield cursor
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _workflow_from_row(row: sqlite3.Row) -> Workflow:
        return Workflow(
            row["id"],
            WorkflowKind(row["kind"]),
            row["title"],
            row["product_summary"],
            WorkflowState(row["state"]),
            int(row["version"]),
            row["policy_version"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row, dependencies: tuple[str, ...] = ()) -> Task:
        return Task(
            row["id"],
            row["workflow_id"],
            row["title"],
            row["owner_role"],
            TaskState(row["state"]),
            dependencies,
            _decode_strings(row["write_scope"]),
            row["card_hash"],
            row["result_hash"],
            int(row["version"]),
            TaskKind(row["task_kind"]),
            row["intent_id"],
            row["language"],
            row["framework"],
        )

    @staticmethod
    def _acceptance_from_row(row: sqlite3.Row) -> CodeTaskAcceptance:
        return CodeTaskAcceptance(
            str(row["acceptance_id"]),
            str(row["workflow_id"]),
            str(row["code_task_id"]),
            int(row["code_task_version"]),
            str(row["input_snapshot_id"]),
            str(row["output_snapshot_id"]),
            str(row["indexed_diff_hash"]),
            str(row["intent_id"]),
            str(row["language"]),
            str(row["framework"]),
            str(row["payload_hash"]),
            str(row["created_at"]),
        )

    @staticmethod
    def _atlas_outbox_from_row(row: sqlite3.Row) -> AtlasOutboxItem:
        return AtlasOutboxItem(
            str(row["ingestion_key"]),
            str(row["acceptance_id"]),
            str(row["payload_hash"]),
            AtlasOutboxState(str(row["state"])),
            int(row["attempt_count"]),
            str(row["last_error_code"]),
            _decode_outbox_reason_codes(str(row["reason_codes_json"])),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _atlas_finalization_from_row(row: sqlite3.Row) -> AtlasFinalization:
        return AtlasFinalization(
            str(row["schema_version"]),
            str(row["acceptance_id"]),
            str(row["ingestion_key"]),
            str(row["payload_hash"]),
            str(row["continuity_key_hash"]),
            str(row["view_id"]),
            int(row["fence_epoch"]),
            int(row["pointer_version"]),
            str(row["published_receipt_hash"]),
            str(row["atlas_receipt_digest"]),
            str(row["finalization_hash"]),
            str(row["created_at"]),
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> Lease:
        return Lease(
            row["task_id"],
            row["owner"],
            int(row["epoch"]),
            row["expires_at"],
            row["heartbeat_at"],
            None if row["host_target"] is None else str(row["host_target"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            int(row["sequence"]),
            row["workflow_id"],
            row["task_id"],
            row["event_type"],
            row["redacted_payload"],
            row["payload_hash"],
            row["created_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            row["kind"],
            row["content_hash"],
            row["safe_path"],
            int(row["size"]),
            row["redaction_version"],
            row["created_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        metadata = _decode_metadata(str(row["redacted_metadata"]))
        return Message(
            str(row["delivery_id"]),
            int(row["sequence"]),
            str(row["workflow_id"]),
            str(row["sender_task_id"]),
            str(row["recipient_task_id"]),
            str(row["correlation_id"]),
            str(row["artifact_hash"]),
            metadata,
            str(row["created_at"]),
            str(row["expires_at"]),
            str(row["delivery_state"]),
            None if row["acknowledged_at"] is None else str(row["acknowledged_at"]),
        )

    def _role_envelope_from_row(self, row: sqlite3.Row) -> RoleEnvelope:
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise TypeError("role payload is not an object")
            if payload.get("schema_version") != self._ROLE_ENVELOPE_SCHEMA_VERSION:
                raise ValueError("role payload schema is not current")
            if _payload_hash(_canonical_payload_json(payload)) != str(row["envelope_hash"]):
                raise ValueError("role envelope hash is corrupt")
            direction = RoleEnvelopeDirection(str(row["direction"]))
            bindings = {
                "direction": direction.value,
                "workflow_id": str(row["workflow_id"]),
                "sender_task_id": str(row["sender_task_id"]),
                "sender_role": str(row["sender_role"]),
                "sender_epoch": int(row["sender_epoch"]),
                "recipient_task_id": str(row["recipient_task_id"]),
                "recipient_role": str(row["recipient_role"]),
                "recipient_epoch": int(row["recipient_epoch"]),
                "coordinator_task_id": str(row["coordinator_task_id"]),
                "coordinator_epoch": int(row["coordinator_epoch"]),
                "correlation_id": str(row["correlation_id"]),
                "assignment_token_hash": str(row["assignment_token_hash"]),
                "dispatch_context_hash": str(row["dispatch_context_hash"]),
                "route_provenance_hash": str(row["route_provenance_hash"]),
                "correlation_fence_hash": str(row["correlation_fence_hash"]),
                "issued_at": str(row["issued_at"]),
                "expires_at": str(row["expires_at"]),
            }
            if any(payload.get(key) != value for key, value in bindings.items()):
                raise ValueError("role envelope bindings are corrupt")
            contracts = tuple(str(item) for item in payload["contract_hashes"])
            index_evidence = tuple(str(item) for item in payload["index_evidence_hashes"])
            evidence = tuple(str(item) for item in payload["evidence_hashes"])
            dependencies = tuple(str(item) for item in payload["dependency_hashes"])
            risks = self._role_risk_items(tuple(payload["risk_items"]))
            if (
                tuple(payload["contract_hashes"]) != contracts
                or tuple(payload["index_evidence_hashes"]) != index_evidence
                or tuple(payload["evidence_hashes"]) != evidence
                or tuple(payload["dependency_hashes"]) != dependencies
            ):
                raise ValueError("role references are corrupt")
            return RoleEnvelope(
                str(row["delivery_id"]),
                int(row["sequence"]),
                direction,
                str(row["workflow_id"]),
                str(row["sender_task_id"]),
                str(row["sender_role"]),
                int(row["sender_epoch"]),
                str(row["recipient_task_id"]),
                str(row["recipient_role"]),
                int(row["recipient_epoch"]),
                str(row["correlation_id"]),
                str(row["assignment_token_hash"]),
                str(row["dispatch_context_hash"]),
                str(row["route_provenance_hash"]),
                str(row["coordinator_task_id"]),
                int(row["coordinator_epoch"]),
                str(row["correlation_fence_hash"]),
                str(payload["task_card_hash"]),
                contracts,
                index_evidence,
                str(payload["terminal_result_hash"]),
                evidence,
                dependencies,
                str(payload["recipient_capability_hash"]),
                risks,
                str(row["issued_at"]),
                str(row["expires_at"]),
                str(row["delivery_state"]),
                None if row["acknowledged_at"] is None else str(row["acknowledged_at"]),
                str(row["envelope_hash"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RoleEnvelopeInvalidError("durable role envelope is corrupt") from error

    def _host_operation_receipt_from_row(
        self, row: sqlite3.Row
    ) -> HostOperationReceipt:
        try:
            payload = {
                "schema_version": self._HOST_ARCHIVE_RECEIPT_SCHEMA_VERSION,
                "workflow_id": str(row["workflow_id"]),
                "task_id": str(row["task_id"]),
                "operation": str(row["operation"]),
                "operation_id": str(row["operation_id"]),
                "lease_epoch": int(row["lease_epoch"]),
                "assignment_token_hash": str(row["assignment_token_hash"]),
                "dispatch_context_hash": str(row["dispatch_context_hash"]),
                "route_provenance_hash": str(row["route_provenance_hash"]),
                "coordinator_task_id": str(row["coordinator_task_id"]),
                "coordinator_epoch": int(row["coordinator_epoch"]),
                "errno": int(row["errno"]),
                "status_code": str(row["status_code"]),
                "outcome": str(row["outcome"]),
            }
            if _payload_hash(_canonical_payload_json(payload)) != str(row["receipt_hash"]):
                raise ValueError("host operation receipt hash is corrupt")
            return HostOperationReceipt(
                str(row["operation_id"]),
                str(row["workflow_id"]),
                str(row["task_id"]),
                str(row["operation"]),
                int(row["lease_epoch"]),
                str(row["assignment_token_hash"]),
                str(row["dispatch_context_hash"]),
                str(row["route_provenance_hash"]),
                str(row["coordinator_task_id"]),
                int(row["coordinator_epoch"]),
                int(row["errno"]),
                str(row["status_code"]),
                str(row["outcome"]),
                str(row["receipt_hash"]),
                str(row["reported_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HostOperationConflictError("durable host operation receipt is corrupt") from error

    @staticmethod
    def _last_lease_epoch(cursor: sqlite3.Cursor, task_id: str) -> int:
        row = cursor.execute(
            "SELECT epoch FROM lease_epochs WHERE task_id = ?", (task_id,)
        ).fetchone()
        return 0 if row is None else int(row["epoch"])

    def _acquire_lease_in_transaction(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        owner: str,
        expires_at: str,
        now: str,
        host_target: str | None = None,
    ) -> Lease:
        row = cursor.execute(
            "SELECT * FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            epoch_row = cursor.execute(
                "SELECT epoch FROM lease_epochs WHERE task_id = ?", (task_id,)
            ).fetchone()
            epoch = (int(epoch_row["epoch"]) if epoch_row is not None else 0) + 1
            cursor.execute(
                "INSERT INTO lease_epochs (task_id, epoch) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET epoch = excluded.epoch",
                (task_id, epoch),
            )
            cursor.execute(
                """
                INSERT INTO leases
                    (task_id, owner, epoch, expires_at, heartbeat_at, host_target)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, owner, epoch, expires_at, now, host_target),
            )
        elif str(row["expires_at"]) <= now:
            epoch = max(int(row["epoch"]), self._last_lease_epoch(cursor, task_id)) + 1
            cursor.execute(
                "UPDATE lease_epochs SET epoch = ? WHERE task_id = ?", (epoch, task_id)
            )
            cursor.execute(
                """
                UPDATE leases
                SET owner = ?, epoch = ?, expires_at = ?, heartbeat_at = ?, host_target = ?
                WHERE task_id = ?
                """,
                (owner, epoch, expires_at, now, host_target, task_id),
            )
        elif row["owner"] == owner:
            if host_target is None:
                cursor.execute(
                    "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE task_id = ?",
                    (expires_at, now, task_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE leases
                    SET expires_at = ?, heartbeat_at = ?, host_target = ?
                    WHERE task_id = ?
                    """,
                    (expires_at, now, host_target, task_id),
                )
        else:
            raise LeaseConflictError(f"task is leased by {row['owner']!r}: {task_id!r}")
        lease = cursor.execute(
            "SELECT * FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._lease_from_row(lease)

    @staticmethod
    def _require_current_lease(
        cursor: sqlite3.Cursor,
        task_id: str,
        owner: str,
        epoch: int,
        *,
        now: str | None = None,
    ) -> None:
        row = cursor.execute(
            "SELECT owner, epoch, expires_at FROM leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if (
            row is None
            or row["owner"] != owner
            or int(row["epoch"]) != epoch
            or (now is not None and str(row["expires_at"]) <= now)
        ):
            raise StaleLeaseError(f"lease is stale for task {task_id!r}")

    @classmethod
    def _validate_host_target(cls, host_target: str | None) -> str | None:
        if host_target is None:
            return None
        is_agent_id = False
        if isinstance(host_target, str):
            try:
                is_agent_id = str(uuid.UUID(host_target)) == host_target.lower()
            except ValueError:
                pass
        if (
            not isinstance(host_target, str)
            or not host_target
            or len(host_target) > cls._MAX_HOST_TARGET_LENGTH
            or (
                cls._HOST_TARGET_PATTERN.fullmatch(host_target) is None
                and not is_agent_id
            )
        ):
            raise HostTargetInvalidError(
                "host target is not a supported Codex agent target"
            )
        return host_target


def _execute_schema_statements(cursor: sqlite3.Cursor, script: str) -> None:
    """Execute one schema script statement-by-statement in the caller's transaction."""
    pending_lines: list[str] = []
    for line in script.splitlines():
        pending_lines.append(line)
        statement = "\n".join(pending_lines).strip()
        if statement and sqlite3.complete_statement(statement):
            cursor.execute(statement)
            pending_lines.clear()
    if any(line.strip() for line in pending_lines):
        raise StoreError("schema script ended with an incomplete statement")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _card_hash(card_body: str) -> str:
    digest = hashlib.sha256(card_body.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_payload_json(payload: Mapping[str, object]) -> str:
    """Encode a fixed, safe metadata map in content-addressed form."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_evidence_binding_json(payload: Mapping[str, object]) -> str:
    """Encode the ATLAS-10D binding with its independently frozen contract."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical_receipt_attestation_json(payload: Mapping[str, object]) -> str:
    """Encode the producer receipt attestation with its frozen contract."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _payload_hash(payload_json: str) -> str:
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _domain_separated_hash(domain: str, payload_json: str) -> str:
    """Hash canonical metadata in a fixed domain to prevent cross-record reuse."""
    digest = hashlib.sha256(
        domain.encode("ascii") + b"\x00" + payload_json.encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return parsed.astimezone(UTC).isoformat()


def _encode_strings(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(values, separators=(",", ":"))


def _decode_strings(value: str) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(value))


def _encode_metadata(metadata: Mapping[str, str]) -> str:
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise ValueError("message metadata keys and values must be strings")
    return json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"))


def _decode_metadata(value: str) -> tuple[tuple[str, str], ...]:
    decoded = json.loads(value)
    return tuple(sorted((str(key), str(item)) for key, item in decoded.items()))


def _encode_outbox_reason_codes(reason_codes: tuple[str, ...]) -> str:
    return json.dumps(reason_codes, separators=(",", ":"))


def _decode_outbox_reason_codes(value: str) -> tuple[str, ...]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(
        not isinstance(reason_code, str) for reason_code in decoded
    ):
        raise StoreError("stored outbox reasons are invalid")
    return tuple(decoded)
