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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Mapping

from .models import (
    AtlasOutboxItem,
    AtlasOutboxState,
    CodeTaskAcceptance,
    Task,
    TaskKind,
    TaskState,
    Workflow,
    WorkflowKind,
    WorkflowState,
)


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


class HostTargetInvalidError(StoreError):
    code = "HOST_TARGET_INVALID"


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
    workspace_root: str
    input_snapshot_id: str
    output_snapshot_id: str
    task_node_ids: tuple[str, ...]
    contract_node_ids: tuple[str, ...]
    checkpoint_id: str
    indexed_diff_hash: str
    fallback_count: int


class SQLiteStore:
    """A small transactional store backed by a single SQLite database file."""

    _SCHEMA_VERSION = 4
    _MAX_MESSAGE_TTL_SECONDS = 86_400
    _MAX_INBOX_LIMIT = 100
    _MAX_HOST_TARGET_LENGTH = 256
    _MAX_ATLAS_OUTBOX_ATTEMPTS = 16
    _MAX_ATLAS_OUTBOX_LIMIT = 100
    _MAX_SAFE_ACCEPTANCE_IDENTIFIER_LENGTH = 256
    _MAX_SAFE_OUTBOX_CODE_LENGTH = 64
    _MAX_SAFE_OUTBOX_REASON_COUNT = 8
    _HOST_TARGET_PATTERN = re.compile(r"/root(?:/[a-z0-9_]+)*\Z")
    _SAFE_ACCEPTANCE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
    _SAFE_OUTBOX_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")

    def __init__(self, database: str | Path) -> None:
        self._connection = sqlite3.connect(str(database), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

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
        workspace_root: str = "",
        input_snapshot_id: str = "",
        task_node_ids: tuple[str, ...] = (),
        contract_node_ids: tuple[str, ...] = (),
    ) -> Task:
        """Register a task and, when provided, its hash-bound task card atomically."""
        if card_body is not None and task.card_hash != _card_hash(card_body):
            raise CardHashMismatchError(
                f"task card hash does not match task {task.id!r}"
            )
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
                        task_id, workspace_root, input_snapshot_id,
                        output_snapshot_id, task_node_ids, contract_node_ids,
                        checkpoint_id, indexed_diff_hash, fallback_count
                    ) VALUES (?, ?, ?, ?, ?, ?, '', '', 0)
                    """,
                    (
                        task.id,
                        workspace_root,
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
            self._require_message_quota(
                cursor,
                workflow_id,
                recipient_task_id,
                now_utc,
                artifact_size,
                max_count,
                max_bytes,
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
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        intent_id: str,
        language: str,
        framework: str,
        created_at: str,
    ) -> tuple[CodeTaskAcceptance, AtlasOutboxItem]:
        """Insert one immutable acceptance and its pending outbox item atomically.

        This is deliberately a storage primitive only.  It validates canonical,
        privacy-bounded metadata but does not authorize a task, inspect receipts,
        or invoke Code Atlas.
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

        with self._transaction() as cursor:
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
        now: str | None = None,
    ) -> Task:
        """Complete a task only when the caller still owns the supplied epoch."""
        now_utc = _utc_timestamp(now) if now is not None else _utc_now()
        with self._transaction() as cursor:
            self._require_current_lease(cursor, task_id, owner, epoch, now=now_utc)
            self._require_strict_completion(cursor, task_id)
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
            cursor.execute(
                """
                UPDATE tasks
                SET state = ?, result_hash = COALESCE(?, result_hash), version = version + 1
                WHERE id = ? AND version = ?
                """,
                (state.value, result_hash, task_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError(f"task version is not current: {task_id!r}")
            row = cursor.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row)

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
            binding = cursor.execute(
                "SELECT * FROM task_index_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
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
    def _require_message_quota(
        cursor: sqlite3.Cursor,
        workflow_id: str,
        recipient_task_id: str,
        now: str,
        artifact_size: int,
        max_count: int,
        max_bytes: int,
    ) -> None:
        for recipient_filter in (True, False):
            if recipient_filter:
                query = """
                    SELECT COUNT(*) AS count, COALESCE(SUM(artifacts.size), 0) AS bytes
                    FROM messages JOIN artifacts ON artifacts.content_hash = messages.artifact_hash
                    WHERE messages.workflow_id = ? AND messages.recipient_task_id = ?
                        AND messages.acknowledged_at IS NULL AND messages.expires_at > ?
                    """
                parameters = (workflow_id, recipient_task_id, now)
            else:
                query = """
                    SELECT COUNT(*) AS count, COALESCE(SUM(artifacts.size), 0) AS bytes
                    FROM messages JOIN artifacts ON artifacts.content_hash = messages.artifact_hash
                    WHERE messages.workflow_id = ? AND messages.acknowledged_at IS NULL
                        AND messages.expires_at > ?
                    """
                parameters = (workflow_id, now)
            usage = cursor.execute(query, parameters).fetchone()
            if (
                int(usage["count"]) + 1 > max_count
                or int(usage["bytes"]) + artifact_size > max_bytes
            ):
                raise QuotaExceededError("mailbox quota exceeded")

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

    @staticmethod
    def _require_index_binding(cursor: sqlite3.Cursor, task_id: str) -> sqlite3.Row:
        row = cursor.execute(
            "SELECT * FROM task_index_bindings WHERE task_id = ?", (task_id,)
        ).fetchone()
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

    @staticmethod
    def _require_strict_completion(cursor: sqlite3.Cursor, task_id: str) -> None:
        binding = cursor.execute(
            "SELECT * FROM task_index_bindings WHERE task_id = ?", (task_id,)
        ).fetchone()
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

    @staticmethod
    def _index_binding_from_row(row: sqlite3.Row) -> IndexBinding:
        return IndexBinding(
            str(row["task_id"]),
            str(row["workspace_root"]),
            str(row["input_snapshot_id"]),
            str(row["output_snapshot_id"]),
            _decode_strings(row["task_node_ids"]),
            _decode_strings(row["contract_node_ids"]),
            str(row["checkpoint_id"]),
            str(row["indexed_diff_hash"]),
            int(row["fallback_count"]),
        )

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
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
                CREATE TABLE IF NOT EXISTS atlas_ingestion_outbox (
                    ingestion_key TEXT PRIMARY KEY,
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
                    workspace_root TEXT NOT NULL,
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
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(leases)"
                ).fetchall()
            }
            if "host_target" not in columns:
                self._connection.execute(
                    "ALTER TABLE leases ADD COLUMN host_target TEXT"
                )
            task_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(tasks)"
                ).fetchall()
            }
            if "task_kind" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'general'"
                )
            if "intent_id" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN intent_id TEXT NOT NULL DEFAULT ''"
                )
            if "language" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN language TEXT NOT NULL DEFAULT ''"
                )
            if "framework" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN framework TEXT NOT NULL DEFAULT ''"
                )
            self._connection.execute(
                """
                INSERT INTO schema_metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("schema_version", str(self._SCHEMA_VERSION)),
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _card_hash(card_body: str) -> str:
    digest = hashlib.sha256(card_body.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_payload_json(payload: Mapping[str, object]) -> str:
    """Encode a fixed, safe metadata map in content-addressed form."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _payload_hash(payload_json: str) -> str:
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
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
