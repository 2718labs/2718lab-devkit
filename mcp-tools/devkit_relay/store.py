"""SQLite transactions for Relay v3's durable scheduling state.

The store never invokes a Codex host API.  It persists only host actions and
their lifecycle evidence, while the host itself performs any agent spawn.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .canonical import canonical_bytes, canonical_hash
from .evidence import validate_evidence
from .models import (
    ACTIVE_TASK_STATES,
    RefillDirective,
    RelayLease,
    RelayRun,
    RelayTask,
    RelayTaskState,
)
from .proofs import (
    FinalizationState,
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReceipt,
    IntegrationScopeEntry,
    ProofFinalizationEvidence,
    ProofFinalizationFence,
    validate_finalization_evidence,
    validate_finalization_fence,
    validate_integration_proof,
)

_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class RelayStoreError(RuntimeError):
    """Base durable-state error carrying one stable code."""

    code = "RELAY_STORAGE_ERROR"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class RelayIdempotencyConflict(RelayStoreError):
    code = "RELAY_IDEMPOTENCY_CONFLICT"


class RelayStateStale(RelayStoreError):
    code = "RELAY_STATE_STALE"


class RelayLeaseConflict(RelayStoreError):
    code = "RELAY_LEASE_CONFLICT"


class RelayCandidateError(RelayStoreError):
    code = "RELAY_CANDIDATE_INVALID"


class RelayStaleBase(RelayStoreError):
    code = "RELAY_STALE_BASE"


class RelayStorageFailure(RelayStoreError):
    code = "RELAY_STORAGE_ERROR"


class RelaySchemaIncompatible(RelayStoreError):
    code = "RELAY_SCHEMA_INCOMPATIBLE"


class RelayTopologyInvalid(RelayStoreError):
    code = "RELAY_TOPOLOGY_INVALID"


class RelayIntegrationProofCorrupt(RelayStoreError):
    code = "RELAY_INTEGRATION_PROOF_CORRUPT"


class RelayIntegrationProofReplay(RelayStoreError):
    code = "RELAY_INTEGRATION_PROOF_REPLAY"


class RelayIntegrationHeadStale(RelayStoreError):
    code = "RELAY_INTEGRATION_HEAD_STALE"


class RelayExpectationStale(RelayStoreError):
    code = "RELAY_EXPECTATION_STALE"


class RelayFinalizationConflict(RelayStoreError):
    code = "RELAY_FINALIZATION_CONFLICT"


class RelayStore:
    """Own the atomic durable state behind Relay's five public tools."""

    _SCHEMA_VERSION = 8
    _MAX_IDEMPOTENCY_KEY_LENGTH = 256
    _SCHEMA_TABLE_INFO = {
        "relay_v3_actions": (
            ("action_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("lease_id", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_candidates": (
            ("candidate_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("originating_epoch", "INTEGER", 1, 0),
            ("branch", "TEXT", 1, 0),
            ("base_commit", "TEXT", 1, 0),
            ("head_commit", "TEXT", 1, 0),
            ("diff_hash", "TEXT", 1, 0),
            ("evidence_hashes_json", "TEXT", 1, 0),
            ("pr_reference", "TEXT", 0, 0),
            ("status", "TEXT", 1, 0),
            ("review_digest", "TEXT", 0, 0),
            ("integration_commit", "TEXT", 0, 0),
            ("integration_tree", "TEXT", 0, 0),
            ("integration_proof_id", "TEXT", 0, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ),
        "relay_v3_cleanup_ledger": (
            ("candidate_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("retention_rounds", "INTEGER", 1, 0),
            ("branch_identity", "TEXT", 1, 0),
            ("worktree_identity", "TEXT", 1, 0),
            ("delete_merged_branch", "INTEGER", 1, 0),
            ("remove_disposable_worktree", "INTEGER", 1, 0),
            ("integration_proof_id", "TEXT", 0, 0),
            ("integration_commit", "TEXT", 0, 0),
            ("merged_integration_version", "INTEGER", 0, 0),
            ("eligible_after_integration_version", "INTEGER", 0, 0),
            ("state", "TEXT", 1, 0),
            ("rollback_receipt_hash", "TEXT", 0, 0),
            ("cleanup_receipt_hash", "TEXT", 0, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ),
        "relay_v3_directives": (
            ("directive_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("workflow_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("expected_schedule_version", "INTEGER", 1, 0),
            ("route_json", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("consumed_idempotency_key", "TEXT", 0, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_evidence": (
            ("evidence_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("lease_id", "TEXT", 1, 0),
            ("epoch", "INTEGER", 1, 0),
            ("kind", "TEXT", 1, 0),
            ("selector", "TEXT", 1, 0),
            ("digest", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_finalization_journal": (
            ("finalization_id", "TEXT", 0, 1),
            ("reservation_epoch", "INTEGER", 1, 0),
            ("integration_proof_id", "TEXT", 1, 0),
            ("workspace_id", "TEXT", 1, 0),
            ("expectation_key", "TEXT", 1, 0),
            ("expectation_version", "INTEGER", 1, 0),
            ("expectation_hash", "TEXT", 1, 0),
            ("target_ref", "TEXT", 1, 0),
            ("base_oid", "TEXT", 1, 0),
            ("final_oid", "TEXT", 1, 0),
            ("fence_hash", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("result_hash", "TEXT", 0, 0),
            ("journal_version", "INTEGER", 1, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ),
        "relay_v3_finalization_outcomes": (
            ("finalization_id", "TEXT", 0, 1),
            ("fence_hash", "TEXT", 1, 0),
            ("integration_proof_id", "TEXT", 1, 0),
            ("expectation_key", "TEXT", 1, 0),
            ("expectation_version", "INTEGER", 1, 0),
            ("expectation_hash", "TEXT", 1, 0),
            ("result_hash", "TEXT", 1, 0),
            ("result_json", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_idempotency": (
            ("idempotency_key", "TEXT", 0, 1),
            ("payload_hash", "TEXT", 1, 0),
            ("result_json", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_integration_proofs": (
            ("proof_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("workflow_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("candidate_id", "TEXT", 1, 0),
            ("integration_version", "INTEGER", 1, 0),
            ("expectation_hash", "TEXT", 1, 0),
            ("expectation_json", "TEXT", 1, 0),
            ("receipt_json", "TEXT", 1, 0),
            ("repository_id", "TEXT", 1, 0),
            ("integration_ref", "TEXT", 1, 0),
            ("predecessor_commit", "TEXT", 1, 0),
            ("final_commit", "TEXT", 1, 0),
            ("final_tree", "TEXT", 1, 0),
            ("attestor_id", "TEXT", 1, 0),
            ("attestor_version", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_leases": (
            ("lease_id", "TEXT", 0, 1),
            ("run_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 0),
            ("action_id", "TEXT", 1, 0),
            ("epoch", "INTEGER", 1, 0),
            ("task_version", "INTEGER", 1, 0),
            ("lease_kind", "TEXT", 1, 0),
            ("endpoint", "TEXT", 0, 0),
            ("state", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
            ("released_at", "TEXT", 0, 0),
            ("last_heartbeat_at", "TEXT", 0, 0),
        ),
        "relay_v3_runs": (
            ("run_id", "TEXT", 0, 1),
            ("workflow_id", "TEXT", 1, 0),
            ("plan_hash", "TEXT", 1, 0),
            ("plan_json", "TEXT", 1, 0),
            ("workspace_id", "TEXT", 1, 0),
            ("input_snapshot_id", "TEXT", 1, 0),
            ("base_commit", "TEXT", 1, 0),
            ("integration_head", "TEXT", 1, 0),
            ("integration_version", "INTEGER", 1, 0),
            ("capacity", "INTEGER", 1, 0),
            ("schedule_version", "INTEGER", 1, 0),
            ("created_at", "TEXT", 1, 0),
        ),
        "relay_v3_scheduler_groups": (
            ("run_id", "TEXT", 1, 1),
            ("scheduler_id", "TEXT", 1, 2),
            ("coordinator_lease_id", "TEXT", 1, 0),
            ("worktree_identity", "TEXT", 1, 0),
            ("writer_task_ids_json", "TEXT", 1, 0),
            ("prewarm_task_ids_json", "TEXT", 1, 0),
        ),
        "relay_v3_scheduler_writer_slots": (
            ("run_id", "TEXT", 1, 1),
            ("scheduler_id", "TEXT", 1, 0),
            ("task_id", "TEXT", 1, 2),
            ("slot", "INTEGER", 1, 0),
        ),
        "relay_v3_schema_metadata": (
            ("key", "TEXT", 0, 1),
            ("value", "TEXT", 1, 0),
        ),
        "relay_v3_tasks": (
            ("run_id", "TEXT", 1, 1),
            ("task_id", "TEXT", 1, 2),
            ("ordinal", "INTEGER", 1, 0),
            ("kind", "TEXT", 1, 0),
            ("priority", "INTEGER", 1, 0),
            ("task_json", "TEXT", 1, 0),
            ("dependencies_json", "TEXT", 1, 0),
            ("write_scope_json", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("task_version", "INTEGER", 1, 0),
            ("scope_owner", "TEXT", 0, 0),
            ("candidate_id", "TEXT", 0, 0),
            ("last_lease_epoch", "INTEGER", 1, 0),
        ),
    }
    _SCHEMA_FOREIGN_KEYS = {
        "relay_v3_actions": frozenset(
            {
                (
                    ("lease_id",),
                    "relay_v3_leases",
                    ("lease_id",),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_candidates": frozenset(
            {
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_cleanup_ledger": frozenset(
            {
                (
                    ("candidate_id",),
                    "relay_v3_candidates",
                    ("candidate_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                (
                    ("run_id",),
                    "relay_v3_runs",
                    ("run_id",),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
                (
                    ("integration_proof_id",),
                    "relay_v3_integration_proofs",
                    ("proof_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            }
        ),
        "relay_v3_directives": frozenset(
            {
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_evidence": frozenset(
            {
                (
                    ("lease_id",),
                    "relay_v3_leases",
                    ("lease_id",),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_finalization_journal": frozenset(
            {
                (
                    ("integration_proof_id",),
                    "relay_v3_integration_proofs",
                    ("proof_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            }
        ),
        "relay_v3_finalization_outcomes": frozenset(
            {
                (
                    ("finalization_id",),
                    "relay_v3_finalization_journal",
                    ("finalization_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                (
                    ("integration_proof_id",),
                    "relay_v3_integration_proofs",
                    ("proof_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            }
        ),
        "relay_v3_integration_proofs": frozenset(
            {
                (
                    ("candidate_id",),
                    "relay_v3_candidates",
                    ("candidate_id",),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            }
        ),
        "relay_v3_leases": frozenset(
            {
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_scheduler_groups": frozenset(
            {
                (
                    ("run_id",),
                    "relay_v3_runs",
                    ("run_id",),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_scheduler_writer_slots": frozenset(
            {
                (
                    ("run_id", "scheduler_id"),
                    "relay_v3_scheduler_groups",
                    ("run_id", "scheduler_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
                (
                    ("run_id", "task_id"),
                    "relay_v3_tasks",
                    ("run_id", "task_id"),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
        "relay_v3_tasks": frozenset(
            {
                (
                    ("run_id",),
                    "relay_v3_runs",
                    ("run_id",),
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            }
        ),
    }

    def __init__(
        self,
        database: str | Path,
        *,
        host_writer_capacity: int = 9,
        host_reader_capacity: int = 9,
    ) -> None:
        if (
            type(host_writer_capacity) is not int
            or not 1 <= host_writer_capacity <= 9
            or type(host_reader_capacity) is not int
            or not 1 <= host_reader_capacity <= 9
        ):
            raise RelayTopologyInvalid()
        self._database = str(database)
        self._host_writer_capacity = host_writer_capacity
        self._host_reader_capacity = host_reader_capacity
        self._legacy_schema_version: str | None = None
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            self._database, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._assert_schema_compatible()
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._create_schema()
            self._assert_schema_shape()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def database_fingerprint(self) -> str:
        """Return a read-only canonical digest of every v3 authority table."""

        connection = self._require_connection()
        tables = (
            "relay_v3_schema_metadata",
            "relay_v3_runs",
            "relay_v3_tasks",
            "relay_v3_leases",
            "relay_v3_actions",
            "relay_v3_directives",
            "relay_v3_idempotency",
            "relay_v3_evidence",
            "relay_v3_candidates",
            "relay_v3_integration_proofs",
            "relay_v3_finalization_journal",
            "relay_v3_finalization_outcomes",
            "relay_v3_cleanup_ledger",
            "relay_v3_scheduler_groups",
            "relay_v3_scheduler_writer_slots",
        )
        return canonical_hash(
            {
                table: [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                ]
                for table in tables
            }
        )

    def start_create(
        self, plan: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, object]:
        """Durably create a run and its initial host actions in one transaction."""

        self._validate_idempotency_key(idempotency_key)
        plan_data = _clone_json(plan)
        topology = self._validate_scheduler_topology(plan_data)
        payload_hash = canonical_hash({"mode": "create", "plan": plan_data})
        workflow_id = str(plan_data["workflow_id"])
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                replay = self._idempotency_replay(cursor, idempotency_key, payload_hash)
                if replay is not None:
                    return replay
                if self._run_row_for_workflow(cursor, workflow_id) is not None:
                    raise RelayStateStale()

                binding = plan_data["workspace_binding"]
                run_id = _stable_id(
                    "run",
                    {"workflow_id": workflow_id, "plan_hash": plan_data["plan_hash"]},
                )
                cursor.execute(
                    """
                    INSERT INTO relay_v3_runs
                        (run_id, workflow_id, plan_hash, plan_json, workspace_id,
                         input_snapshot_id, base_commit, integration_head,
                         integration_version, capacity, schedule_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?)
                    """,
                    (
                        run_id,
                        workflow_id,
                        str(plan_data["plan_hash"]),
                        _encode_json(plan_data),
                        str(binding["workspace_id"]),
                        str(binding["input_snapshot_id"]),
                        str(plan_data["base_commit"]),
                        str(plan_data["base_commit"]),
                        min(int(plan_data["capacity"]), self._host_writer_capacity),
                        now,
                    ),
                )
                queues = plan_data["queues"]
                ready_ids = {
                    str(item)
                    for name in ("ready", "writer_ready", "design_ready", "prewarm_ready")
                    for item in queues.get(name, [])
                }
                for ordinal, task in enumerate(plan_data["tasks"]):
                    task_id = str(task["task_id"])
                    cursor.execute(
                        """
                        INSERT INTO relay_v3_tasks
                            (run_id, task_id, ordinal, kind, priority, task_json,
                             dependencies_json, write_scope_json, state, task_version,
                             scope_owner, candidate_id, last_lease_epoch)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, 0)
                        """,
                        (
                            run_id,
                            task_id,
                            ordinal,
                            str(task["kind"]),
                            int(task["priority"]),
                            _encode_json(task),
                            _encode_json(task["dependencies"]),
                            _encode_json(task["write_scope"]),
                            (
                                RelayTaskState.READY.value
                                if task_id in ready_ids
                                else RelayTaskState.PREPARED.value
                            ),
                        ),
                    )

                if topology is not None:
                    self._record_scheduler_topology(cursor, run_id, topology)

                run = self._run_from_row(self._run_row(cursor, run_id))
                actions = [
                    self._allocate_action(cursor, run, task, now=now)
                    for task in self._select_eligible_tasks(cursor, run)
                ]
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                result = self._start_result(run, actions)
                self._record_idempotency(
                    cursor,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    result=result,
                    created_at=now,
                )
                return result
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def start_refill(
        self,
        workflow_id: str,
        directive_id: str,
        *,
        expected_schedule_version: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Consume one current directive and reserve exactly one new action."""

        self._validate_idempotency_key(idempotency_key)
        payload_hash = canonical_hash(
            {
                "mode": "refill",
                "workflow_id": workflow_id,
                "refill_directive_id": directive_id,
                "expected_schedule_version": expected_schedule_version,
            }
        )
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                replay = self._idempotency_replay(cursor, idempotency_key, payload_hash)
                if replay is not None:
                    return replay
                run = self._run_for_workflow(cursor, workflow_id)
                if run.schedule_version != expected_schedule_version:
                    raise RelayStateStale()
                directive = cursor.execute(
                    """
                    SELECT * FROM relay_v3_directives
                    WHERE directive_id = ? AND run_id = ? AND state = 'outstanding'
                    """,
                    (directive_id, run.run_id),
                ).fetchone()
                if (
                    directive is None
                    or int(directive["expected_schedule_version"])
                    != expected_schedule_version
                ):
                    raise RelayStateStale()
                task = self._task_for_run(cursor, run.run_id, str(directive["task_id"]))
                if task.task_id not in {
                    candidate.task_id
                    for candidate in self._select_eligible_tasks(cursor, run)
                }:
                    raise RelayStateStale()

                action = self._allocate_action(cursor, run, task, now=now)
                cursor.execute(
                    """
                    UPDATE relay_v3_directives
                    SET state = 'consumed', consumed_idempotency_key = ?
                    WHERE directive_id = ?
                    """,
                    (idempotency_key, directive_id),
                )
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                result = self._start_result(run, [action])
                self._record_idempotency(
                    cursor,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    result=result,
                    created_at=now,
                )
                return result
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def recover_lease(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        predecessor_action_id: str,
        predecessor_lease_id: str,
        recovery_kind: str,
    ) -> dict[str, object]:
        """Replace one exact abandoned lease with a predecessor-bound action."""

        if recovery_kind not in {"stale_recovery", "interruption_recovery"}:
            raise RelayStateStale()
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if (
                    lease.action_id != predecessor_action_id
                    or lease.lease_id != predecessor_lease_id
                ):
                    raise RelayLeaseConflict()
                if (recovery_kind == "stale_recovery") != (lease.endpoint is None):
                    raise RelayStateStale()
                recovery = self._recovery_context(
                    cursor, run, task, lease, recovery_kind
                )
                self._release_lease(cursor, lease, now=now)
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, scope_owner = NULL
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.READY.value, run.run_id, task.task_id),
                )
                replacement = self._allocate_action(
                    cursor,
                    run,
                    self._task_for_run(cursor, run.run_id, task.task_id),
                    now=now,
                    recovery=recovery,
                )
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._start_result(run, [replacement])
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def bind_endpoint(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        endpoint: str,
        expected_task_version: int,
    ) -> dict[str, object]:
        """Record host-observed endpoint binding without claiming a spawn occurred."""

        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if lease.endpoint not in {None, endpoint}:
                    raise RelayLeaseConflict()
                if lease.endpoint == endpoint and task.state is RelayTaskState.RUNNING:
                    return {
                        "workflow_id": workflow_id,
                        "task": task.to_dict(),
                        "lease": lease.to_dict(),
                    }
                cursor.execute(
                    """
                    UPDATE relay_v3_leases
                    SET endpoint = ?, last_heartbeat_at = ?
                    WHERE lease_id = ?
                    """,
                    (endpoint, now, lease.lease_id),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.RUNNING.value, run.run_id, task.task_id),
                )
                return {
                    "workflow_id": workflow_id,
                    "task": self._task_for_run(cursor, run.run_id, task_id).to_dict(),
                    "lease": self._lease_for_id(cursor, lease.lease_id).to_dict(),
                }
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def heartbeat(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        endpoint: str,
        expected_task_version: int,
    ) -> dict[str, object]:
        """Update only an active endpoint's liveness marker."""

        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if lease.endpoint != endpoint:
                    raise RelayLeaseConflict()
                cursor.execute(
                    "UPDATE relay_v3_leases SET last_heartbeat_at = ? WHERE lease_id = ?",
                    (now, lease.lease_id),
                )
                return {
                    "workflow_id": workflow_id,
                    "task": task.to_dict(),
                    "lease": self._lease_for_id(cursor, lease.lease_id).to_dict(),
                }
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def record_evidence(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        endpoint: str,
        expected_task_version: int,
        evidence: Mapping[str, object],
    ) -> dict[str, object]:
        """Append immutable task-local evidence under its live worker lease."""

        try:
            normalized = validate_evidence(dict(evidence))
        except (TypeError, ValueError) as error:
            raise RelayCandidateError() from error
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if lease.endpoint != endpoint:
                    raise RelayLeaseConflict()
                evidence_id = _stable_id(
                    "evidence",
                    {
                        "run_id": run.run_id,
                        "task_id": task.task_id,
                        "epoch": epoch,
                        **normalized,
                    },
                )
                cursor.execute(
                    """
                    INSERT INTO relay_v3_evidence
                        (evidence_id, run_id, task_id, lease_id, epoch, kind,
                         selector, digest, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, task_id, epoch, kind, selector, digest)
                    DO NOTHING
                    """,
                    (
                        evidence_id,
                        run.run_id,
                        task.task_id,
                        lease.lease_id,
                        epoch,
                        normalized["kind"],
                        normalized["selector"],
                        normalized["digest"],
                        now,
                    ),
                )
                return {
                    "workflow_id": workflow_id,
                    "task": task.to_dict(),
                    "evidence": {"evidence_id": evidence_id, **normalized},
                }
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def worker_terminal(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        endpoint: str,
        expected_task_version: int,
        outcome: str,
    ) -> dict[str, object]:
        """Release a worker slot and write the next refill atomically."""

        if outcome not in {"blocked", "cancelled", "completed"}:
            raise RelayStateStale()
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if lease.endpoint != endpoint:
                    raise RelayLeaseConflict()
                if outcome == "completed" and task.kind == "implementation":
                    raise RelayCandidateError()
                if outcome == "completed":
                    self._require_required_evidence(cursor, run, task, epoch)
                target = (
                    RelayTaskState.REVIEW_INTEGRATION
                    if outcome == "completed"
                    else RelayTaskState(outcome)
                )
                self._release_lease(cursor, lease, now=now)
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1, scope_owner = NULL
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (target.value, run.run_id, task.task_id),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._mutation_result(cursor, run, task.task_id)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def candidate_handoff(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        endpoint: str,
        expected_task_version: int,
        candidate: Mapping[str, object],
    ) -> dict[str, object]:
        """Transfer one implementation candidate and its scope to Sol."""

        candidate_data = self._normalize_candidate(candidate)
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task, lease = self._active_task_and_lease(
                    cursor,
                    run,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                )
                if task.kind != "implementation" or lease.endpoint != endpoint:
                    raise RelayLeaseConflict()
                if candidate_data["base_commit"] != self._lease_bootstrap_base(
                    cursor, lease
                ):
                    raise RelayStaleBase()
                self._require_evidence(cursor, run, task, epoch, candidate_data)
                existing = cursor.execute(
                    "SELECT 1 FROM relay_v3_candidates WHERE candidate_id = ?",
                    (candidate_data["candidate_id"],),
                ).fetchone()
                if existing is not None:
                    raise RelayCandidateError()
                cursor.execute(
                    """
                    INSERT INTO relay_v3_candidates
                        (candidate_id, run_id, task_id, originating_epoch, branch,
                         base_commit, head_commit, diff_hash, evidence_hashes_json,
                         pr_reference, status, review_digest, integration_commit,
                         integration_tree, integration_proof_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', NULL,
                            NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        candidate_data["candidate_id"],
                        run.run_id,
                        task.task_id,
                        epoch,
                        candidate_data["branch"],
                        candidate_data["base_commit"],
                        candidate_data["head_commit"],
                        candidate_data["diff_hash"],
                        _encode_json(candidate_data["evidence_hashes"]),
                        candidate_data["pr_reference"],
                        now,
                        now,
                    ),
                )
                cleanup_policy = cast(
                    dict[str, object] | None, candidate_data["cleanup_policy"]
                )
                if cleanup_policy is not None:
                    cursor.execute(
                        """
                        INSERT INTO relay_v3_cleanup_ledger
                            (candidate_id, run_id, retention_rounds, branch_identity,
                             worktree_identity, delete_merged_branch,
                             remove_disposable_worktree, integration_proof_id,
                             integration_commit, merged_integration_version,
                             eligible_after_integration_version, state,
                             rollback_receipt_hash, cleanup_receipt_hash,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                                'CLEANUP_PENDING', NULL, NULL, ?, ?)
                        """,
                        (
                            candidate_data["candidate_id"],
                            run.run_id,
                            cleanup_policy["retention_rounds"],
                            cleanup_policy["branch_identity"],
                            cleanup_policy["worktree_identity"],
                            1,
                            1,
                            now,
                            now,
                        ),
                    )
                self._release_lease(cursor, lease, now=now)
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1, scope_owner = 'sol',
                        candidate_id = ?
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (
                        RelayTaskState.REVIEW_INTEGRATION.value,
                        candidate_data["candidate_id"],
                        run.run_id,
                        task.task_id,
                    ),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._mutation_result(
                    cursor,
                    run,
                    task.task_id,
                    candidate_id=str(candidate_data["candidate_id"]),
                )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def review_candidate(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
        review_digest: str,
    ) -> dict[str, object]:
        """Persist Sol's review gate without granting integration authority away."""

        if _DIGEST.fullmatch(review_digest) is None:
            raise RelayCandidateError()
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run, task, candidate = self._candidate_for_sol(
                    cursor,
                    workflow_id,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                    candidate_id=candidate_id,
                )
                if str(candidate["status"]) not in {"pending_review", "rebased"}:
                    raise RelayStateStale()
                cursor.execute(
                    """
                    UPDATE relay_v3_candidates
                    SET status = 'reviewed', review_digest = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (review_digest, now, candidate_id),
                )
                return self._mutation_result(
                    cursor, run, task.task_id, candidate_id=candidate_id
                )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def rebase_candidate(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
        base_commit: str,
        head_commit: str,
        diff_hash: str,
        evidence_hashes: Sequence[str],
    ) -> dict[str, object]:
        """Record a Sol-approved candidate rebase pending a new review."""

        if (
            _COMMIT.fullmatch(base_commit) is None
            or _COMMIT.fullmatch(head_commit) is None
            or _DIGEST.fullmatch(diff_hash) is None
            or not _valid_digest_list(evidence_hashes)
        ):
            raise RelayCandidateError()
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run, task, candidate = self._candidate_for_sol(
                    cursor,
                    workflow_id,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                    candidate_id=candidate_id,
                )
                if str(candidate["status"]) not in {
                    "pending_review",
                    "reviewed",
                    "rebased",
                }:
                    raise RelayStateStale()
                if base_commit != run.integration_head:
                    raise RelayIntegrationHeadStale()
                self._require_evidence_hashes(
                    cursor, run, task, epoch, list(evidence_hashes)
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_candidates
                    SET base_commit = ?, head_commit = ?, diff_hash = ?,
                        evidence_hashes_json = ?, status = 'rebased',
                        review_digest = NULL, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        base_commit,
                        head_commit,
                        diff_hash,
                        _encode_json(sorted(evidence_hashes)),
                        now,
                        candidate_id,
                    ),
                )
                return self._mutation_result(
                    cursor, run, task.task_id, candidate_id=candidate_id
                )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def reject_candidate(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
    ) -> dict[str, object]:
        """Let Sol reject a candidate and release its held write scope."""

        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run, task, _candidate = self._candidate_for_sol(
                    cursor,
                    workflow_id,
                    task_id,
                    epoch=epoch,
                    expected_task_version=expected_task_version,
                    candidate_id=candidate_id,
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_candidates
                    SET status = 'rejected', updated_at = ? WHERE candidate_id = ?
                    """,
                    (now, candidate_id),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1, scope_owner = NULL
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.REJECTED.value, run.run_id, task.task_id),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._mutation_result(
                    cursor, run, task.task_id, candidate_id=candidate_id
                )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def integration_expectation(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
        proof_id: str,
    ) -> IntegrationExpectation:
        """Snapshot the exact read-only binding a host proof must attest."""

        connection = self._require_connection()
        run = self._run_for_workflow(connection, workflow_id)
        task = self._task_for_run(connection, run.run_id, task_id)
        candidate = connection.execute(
            """
            SELECT * FROM relay_v3_candidates
            WHERE candidate_id = ? AND run_id = ? AND task_id = ?
              AND originating_epoch = ?
            """,
            (candidate_id, run.run_id, task_id, epoch),
        ).fetchone()
        if candidate is None:
            raise RelayStateStale()

        fresh = (
            task.kind == "implementation"
            and task.state is RelayTaskState.REVIEW_INTEGRATION
            and task.task_version == expected_task_version
            and task.last_lease_epoch == epoch
            and task.candidate_id == candidate_id
            and str(candidate["status"]) == "reviewed"
        )
        recovery = (
            task.kind == "implementation"
            and task.state is RelayTaskState.INTEGRATED
            and task.task_version == expected_task_version + 1
            and task.last_lease_epoch == epoch
            and task.candidate_id == candidate_id
            and str(candidate["status"]) == "integrated"
        )
        if not fresh and not recovery:
            raise RelayStateStale()

        proof_row = connection.execute(
            "SELECT * FROM relay_v3_integration_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        if proof_row is not None and str(proof_row["candidate_id"]) != candidate_id:
            raise RelayIntegrationProofReplay()

        if fresh:
            if proof_row is not None:
                raise RelayIntegrationProofReplay()
            return self._integration_expectation(run, task, candidate)

        if proof_row is not None and candidate["integration_proof_id"] == proof_id:
            receipt = self._validated_proof_row(connection, proof_row)
            return receipt.expectation
        raise RelayStateStale()

    def prepare_finalization(
        self, *, fence: ProofFinalizationFence
    ) -> ProofFinalizationEvidence:
        """Durably create or re-observe a fenced prepared finalization row."""

        self._validate_finalization_fence(fence)
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                journal = self._finalization_journal(cursor, fence.finalization_id)
                outcome = self._finalization_outcome(cursor, fence.finalization_id)
                proof = self._integration_proof(cursor, fence.integration_proof_id)
                if journal is None:
                    if outcome is not None or proof is not None:
                        raise RelayFinalizationConflict()
                    self._require_current_finalization_expectation(cursor, fence)
                    cursor.execute(
                        """
                        INSERT INTO relay_v3_finalization_journal
                            (finalization_id, reservation_epoch, integration_proof_id,
                             workspace_id, expectation_key, expectation_version,
                             expectation_hash, target_ref, base_oid, final_oid,
                             fence_hash, state, result_hash, journal_version,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, 1, ?, ?)
                        """,
                        (*self._fence_values(fence), now, now),
                    )
                    return self._finalization_evidence(
                        fence, "prepared", None, journal_version=1
                    )

                self._require_matching_finalization_fence(journal, fence)
                evidence = self._evidence_from_journal(journal)
                if evidence.state == "prepared":
                    if outcome is not None or proof is not None:
                        raise RelayFinalizationConflict()
                    self._require_current_finalization_expectation(cursor, fence)
                    return evidence
                if evidence.state == "committed":
                    self._require_committed_finalization(cursor, fence, journal, outcome)
                    return evidence
                if outcome is not None or proof is not None:
                    raise RelayFinalizationConflict()
                return evidence
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def resolve_or_abort_finalization(
        self, *, fence: ProofFinalizationFence
    ) -> ProofFinalizationEvidence:
        """Serialize the only safe terminal decision after uncertain execution."""

        self._validate_finalization_fence(fence)
        now = _utc_now()
        try:
            with self._recovery_transaction() as cursor:
                journal = self._finalization_journal(cursor, fence.finalization_id)
                outcome = self._finalization_outcome(cursor, fence.finalization_id)
                proof = self._integration_proof(cursor, fence.integration_proof_id)
                if journal is None:
                    if outcome is not None or proof is not None:
                        raise RelayFinalizationConflict()
                    cursor.execute(
                        """
                        INSERT INTO relay_v3_finalization_journal
                            (finalization_id, reservation_epoch, integration_proof_id,
                             workspace_id, expectation_key, expectation_version,
                             expectation_hash, target_ref, base_oid, final_oid,
                             fence_hash, state, result_hash, journal_version,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'aborted', NULL, 1, ?, ?)
                        """,
                        (*self._fence_values(fence), now, now),
                    )
                    return self._finalization_evidence(
                        fence, "aborted", None, journal_version=1
                    )

                self._require_matching_finalization_fence(journal, fence)
                evidence = self._evidence_from_journal(journal)
                if evidence.state == "committed":
                    self._require_committed_finalization(cursor, fence, journal, outcome)
                    return evidence
                if evidence.state == "aborted":
                    if outcome is not None or proof is not None:
                        raise RelayFinalizationConflict()
                    return evidence
                if outcome is not None or proof is not None:
                    raise RelayFinalizationConflict()
                journal_version = evidence.journal_version + 1
                cursor.execute(
                    """
                    UPDATE relay_v3_finalization_journal
                    SET state = 'aborted', result_hash = NULL, journal_version = ?,
                        updated_at = ?
                    WHERE finalization_id = ? AND fence_hash = ? AND state = 'prepared'
                    """,
                    (journal_version, now, fence.finalization_id, fence.fence_hash),
                )
                if cursor.rowcount != 1:
                    raise RelayFinalizationConflict()
                return self._finalization_evidence(
                    fence, "aborted", None, journal_version=journal_version
                )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def find_committed_finalization(
        self,
        *,
        integration_proof_id: str,
        expectation_hash: str,
    ) -> ProofFinalizationEvidence | None:
        """Find one exact committed journal decision without exposing its outcome."""

        if _DIGEST.fullmatch(integration_proof_id) is None or _DIGEST.fullmatch(
            expectation_hash
        ) is None:
            raise RelayFinalizationConflict()
        connection = self._require_connection()
        try:
            rows = connection.execute(
                """
                SELECT * FROM relay_v3_finalization_journal
                WHERE integration_proof_id = ? AND expectation_hash = ?
                  AND state = 'committed'
                ORDER BY journal_version DESC
                """,
                (integration_proof_id, expectation_hash),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise RelayFinalizationConflict()
            journal = rows[0]
            fence = self._fence_from_journal(journal)
            outcome = self._finalization_outcome(connection, fence.finalization_id)
            self._require_committed_finalization(connection, fence, journal, outcome)
            return self._evidence_from_journal(journal)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def committed_finalization_result(
        self,
        *,
        integration_proof_id: str,
        expectation_hash: str,
    ) -> dict[str, object] | None:
        """Return the bounded pre-existing public result for an exact commit."""

        evidence = self.find_committed_finalization(
            integration_proof_id=integration_proof_id,
            expectation_hash=expectation_hash,
        )
        if evidence is None:
            return None
        connection = self._require_connection()
        try:
            outcome = self._finalization_outcome(connection, evidence.finalization_id)
            if outcome is None:
                raise RelayFinalizationConflict()
            result = _decode_json_object(str(outcome["result_json"]))
            if canonical_hash(result) != outcome["result_hash"]:
                raise RelayFinalizationConflict()
            return result
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def integrate_candidate(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
        proof_id: str,
        expectation: IntegrationExpectation,
        receipt: IntegrationProofReceipt,
        finalization_fence: ProofFinalizationFence,
    ) -> tuple[dict[str, object], ProofFinalizationEvidence]:
        """Atomically persist the integration outcome and committed journal evidence."""

        try:
            validate_integration_proof(proof_id, expectation, receipt)
            validate_finalization_fence(finalization_fence)
        except IntegrationProofError as error:
            raise RelayStoreError(error.code) from error
        except ValueError as error:
            raise RelayFinalizationConflict() from error
        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task = self._task_for_run(cursor, run.run_id, task_id)
                candidate = cursor.execute(
                    """
                    SELECT * FROM relay_v3_candidates
                    WHERE candidate_id = ? AND run_id = ? AND task_id = ?
                      AND originating_epoch = ?
                    """,
                    (candidate_id, run.run_id, task_id, epoch),
                ).fetchone()
                if candidate is None:
                    raise RelayStateStale()

                fresh = (
                    task.kind == "implementation"
                    and task.state is RelayTaskState.REVIEW_INTEGRATION
                    and task.task_version == expected_task_version
                    and task.last_lease_epoch == epoch
                    and task.candidate_id == candidate_id
                    and str(candidate["status"]) == "reviewed"
                )
                recovery = (
                    task.kind == "implementation"
                    and task.state is RelayTaskState.INTEGRATED
                    and task.task_version == expected_task_version + 1
                    and task.last_lease_epoch == epoch
                    and task.candidate_id == candidate_id
                    and str(candidate["status"]) == "integrated"
                )
                if not fresh and not recovery:
                    raise RelayStateStale()

                existing = cursor.execute(
                    "SELECT * FROM relay_v3_integration_proofs WHERE proof_id = ?",
                    (proof_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["candidate_id"]) != candidate_id
                        or candidate["integration_proof_id"] != proof_id
                        or not recovery
                    ):
                        raise RelayIntegrationProofReplay()
                    persisted = self._validated_proof_row(cursor, existing)
                    if persisted != receipt or persisted.expectation != expectation:
                        raise RelayIntegrationProofCorrupt()
                    if (
                        task.state is not RelayTaskState.INTEGRATED
                        or task.task_version != expected_task_version + 1
                        or task.last_lease_epoch != epoch
                        or task.candidate_id != candidate_id
                        or str(candidate["status"]) != "integrated"
                    ):
                        raise RelayIntegrationProofCorrupt()
                    self._require_finalization_integration_fence(
                        finalization_fence,
                        proof_id=proof_id,
                        expectation=persisted.expectation,
                        receipt=persisted,
                    )
                    journal = self._finalization_journal(
                        cursor, finalization_fence.finalization_id
                    )
                    outcome = self._finalization_outcome(
                        cursor, finalization_fence.finalization_id
                    )
                    if journal is None:
                        raise RelayFinalizationConflict()
                    self._require_matching_finalization_fence(
                        journal, finalization_fence
                    )
                    evidence = self._evidence_from_journal(journal)
                    self._require_committed_finalization(
                        cursor, finalization_fence, journal, outcome
                    )
                    result = self._outcome_result(outcome)
                    return result, evidence

                if not fresh:
                    raise RelayStateStale()

                current = self._integration_expectation(run, task, candidate)
                if (
                    current.predecessor_integration_head
                    != expectation.predecessor_integration_head
                    or current.predecessor_integration_version
                    != expectation.predecessor_integration_version
                ):
                    raise RelayIntegrationHeadStale()
                if current != expectation:
                    raise RelayStoreError("RELAY_INTEGRATION_BINDING_MISMATCH")
                try:
                    validate_integration_proof(proof_id, current, receipt)
                except IntegrationProofError as error:
                    raise RelayStoreError(error.code) from error
                self._require_finalization_integration_fence(
                    finalization_fence,
                    proof_id=proof_id,
                    expectation=current,
                    receipt=receipt,
                )
                journal = self._finalization_journal(
                    cursor, finalization_fence.finalization_id
                )
                outcome = self._finalization_outcome(
                    cursor, finalization_fence.finalization_id
                )
                if journal is None:
                    raise RelayFinalizationConflict()
                self._require_matching_finalization_fence(journal, finalization_fence)
                prepared = self._evidence_from_journal(journal)
                if prepared.state != "prepared" or outcome is not None:
                    raise RelayFinalizationConflict()

                cursor.execute(
                    """
                    INSERT INTO relay_v3_integration_proofs
                        (proof_id, run_id, workflow_id, task_id, candidate_id,
                         integration_version, expectation_hash, expectation_json,
                         receipt_json, repository_id, integration_ref,
                         predecessor_commit, final_commit, final_tree, attestor_id,
                         attestor_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proof_id,
                        run.run_id,
                        run.workflow_id,
                        task.task_id,
                        candidate_id,
                        run.integration_version + 1,
                        current.expectation_hash,
                        _encode_json(current.to_dict()),
                        _encode_json(receipt.to_dict()),
                        receipt.repository_id,
                        receipt.integration_ref,
                        receipt.predecessor_commit,
                        receipt.final_commit,
                        receipt.final_tree,
                        receipt.attestor_id,
                        receipt.attestor_version,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_candidates
                    SET status = 'integrated', integration_commit = ?,
                        integration_tree = ?, integration_proof_id = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        receipt.final_commit,
                        receipt.final_tree,
                        proof_id,
                        now,
                        candidate_id,
                    ),
                )
                merged_version = run.integration_version + 1
                cleanup = cursor.execute(
                    """
                    SELECT retention_rounds FROM relay_v3_cleanup_ledger
                    WHERE candidate_id = ? AND run_id = ?
                      AND state = 'CLEANUP_PENDING'
                      AND integration_proof_id IS NULL
                    """,
                    (candidate_id, run.run_id),
                ).fetchone()
                if cleanup is not None:
                    cursor.execute(
                        """
                        UPDATE relay_v3_cleanup_ledger
                        SET integration_proof_id = ?, integration_commit = ?,
                            merged_integration_version = ?,
                            eligible_after_integration_version = ?,
                            updated_at = ?
                        WHERE candidate_id = ? AND run_id = ?
                        """,
                        (
                            proof_id,
                            receipt.final_commit,
                            merged_version,
                            merged_version + int(cleanup["retention_rounds"]),
                            now,
                            candidate_id,
                            run.run_id,
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1, scope_owner = NULL
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.INTEGRATED.value, run.run_id, task.task_id),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_runs
                    SET integration_head = ?,
                        integration_version = integration_version + 1
                    WHERE run_id = ? AND integration_head = ?
                      AND integration_version = ?
                    """,
                    (
                        receipt.final_commit,
                        run.run_id,
                        run.integration_head,
                        run.integration_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RelayIntegrationHeadStale()
                cursor.execute(
                    """
                    UPDATE relay_v3_cleanup_ledger
                    SET state = 'CLEANUP_ELIGIBLE', updated_at = ?
                    WHERE run_id = ? AND state = 'CLEANUP_PENDING'
                      AND integration_proof_id IS NOT NULL
                      AND eligible_after_integration_version <= ?
                    """,
                    (now, run.run_id, merged_version),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                result = self._mutation_result(
                    cursor, run, task.task_id, candidate_id=candidate_id
                )
                result_hash = canonical_hash(result)
                journal_version = prepared.journal_version + 1
                cursor.execute(
                    """
                    INSERT INTO relay_v3_finalization_outcomes
                        (finalization_id, fence_hash, integration_proof_id,
                         expectation_key, expectation_version, expectation_hash,
                         result_hash, result_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finalization_fence.finalization_id,
                        finalization_fence.fence_hash,
                        finalization_fence.integration_proof_id,
                        finalization_fence.expectation_key,
                        finalization_fence.expectation_version,
                        finalization_fence.expectation_hash,
                        result_hash,
                        _encode_json(result),
                        now,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_finalization_journal
                    SET state = 'committed', result_hash = ?, journal_version = ?,
                        updated_at = ?
                    WHERE finalization_id = ? AND fence_hash = ? AND state = 'prepared'
                    """,
                    (
                        result_hash,
                        journal_version,
                        now,
                        finalization_fence.finalization_id,
                        finalization_fence.fence_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RelayFinalizationConflict()
                evidence = self._finalization_evidence(
                    finalization_fence,
                    "committed",
                    result_hash,
                    journal_version=journal_version,
                )
                return result, evidence
        except IntegrationProofError as error:
            raise RelayStoreError(error.code) from error
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def approve_readonly(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
    ) -> dict[str, object]:
        """Mark a handed-off reader complete only after Sol's authorization."""

        now = _utc_now()
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                task = self._task_for_run(cursor, run.run_id, task_id)
                if (
                    task.kind == "implementation"
                    or task.state is not RelayTaskState.REVIEW_INTEGRATION
                    or task.task_version != expected_task_version
                    or task.last_lease_epoch != epoch
                ):
                    raise RelayStateStale()
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.COMPLETED.value, run.run_id, task.task_id),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._mutation_result(cursor, run, task.task_id)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def cleanup_ledger(
        self, workflow_id: str, candidate_id: str
    ) -> dict[str, object]:
        """Read one candidate-bound cleanup ledger without changing its state."""

        connection = self._require_connection()
        try:
            run = self._run_row_for_workflow(connection, workflow_id)
            if run is None or _IDENTIFIER.fullmatch(candidate_id) is None:
                raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
            row = connection.execute(
                """
                SELECT * FROM relay_v3_cleanup_ledger
                WHERE run_id = ? AND candidate_id = ?
                """,
                (str(run["run_id"]), candidate_id),
            ).fetchone()
            if row is None:
                raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
            return self._cleanup_ledger_public(row)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def prepare_cleanup_operation(
        self,
        workflow_id: str,
        candidate_id: str,
        *,
        host_recheck: Mapping[str, object],
    ) -> dict[str, object]:
        """Return a host-only cleanup descriptor after a fresh, non-destructive check."""

        connection = self._require_connection()
        try:
            run_row = self._run_row_for_workflow(connection, workflow_id)
            if run_row is None or _IDENTIFIER.fullmatch(candidate_id) is None:
                raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
            row = connection.execute(
                """
                SELECT ledger.*, candidates.branch, candidates.head_commit
                FROM relay_v3_cleanup_ledger AS ledger
                JOIN relay_v3_candidates AS candidates
                  ON candidates.candidate_id = ledger.candidate_id
                WHERE ledger.run_id = ? AND ledger.candidate_id = ?
                """,
                (str(run_row["run_id"]), candidate_id),
            ).fetchone()
            if row is None or str(row["state"]) == "CLEANUP_PENDING":
                raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
            if str(row["state"]) == "CLEANUP_ROLLBACK_OBSERVED":
                raise RelayStoreError("CLEANUP_ROLLBACK_OBSERVED")
            if str(row["state"]) != "CLEANUP_ELIGIBLE":
                raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
            self._validate_cleanup_recheck(row, run_row, host_recheck)
            body = {
                "schema": "2718lab-devkit/cleanup-operation-v1",
                "candidate_id": candidate_id,
                "integration_proof_id": str(row["integration_proof_id"]),
                "integration_version": int(row["merged_integration_version"]),
                "branch_identity": str(row["branch_identity"]),
                "worktree_identity": str(row["worktree_identity"]),
                "delete_merged_branch": bool(row["delete_merged_branch"]),
                "remove_disposable_worktree": bool(row["remove_disposable_worktree"]),
                "host_recheck_hash": canonical_hash(host_recheck),
            }
            return {**body, "operation_hash": canonical_hash(body)}
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def record_rollback_receipt(
        self,
        workflow_id: str,
        candidate_id: str,
        *,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Permanently block cleanup only for a canonical candidate rollback receipt."""

        expected = {
            "schema",
            "candidate_id",
            "integration_proof_id",
            "pre_rollback_integration_version",
            "receipt_hash",
        }
        if type(receipt) is not dict or set(receipt) != expected:
            raise RelayStoreError("CLEANUP_HOST_RECHECK_FAILED")
        try:
            receipt_hash = receipt["receipt_hash"]
            if (
                receipt["schema"] != "2718lab-devkit/rollback-receipt-v1"
                or receipt["candidate_id"] != candidate_id
                or type(receipt_hash) is not str
                or _DIGEST.fullmatch(receipt_hash) is None
                or type(receipt["pre_rollback_integration_version"]) is not int
            ):
                raise RelayStoreError("CLEANUP_HOST_RECHECK_FAILED")
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                row = cursor.execute(
                    """
                    SELECT * FROM relay_v3_cleanup_ledger
                    WHERE run_id = ? AND candidate_id = ?
                    """,
                    (run.run_id, candidate_id),
                ).fetchone()
                if row is None:
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                merged_version = row["merged_integration_version"]
                if (
                    receipt["integration_proof_id"] != row["integration_proof_id"]
                    or merged_version is None
                    or not int(merged_version)
                    <= receipt["pre_rollback_integration_version"]
                    <= run.integration_version
                ):
                    raise RelayStoreError("CLEANUP_HOST_RECHECK_FAILED")
                cursor.execute(
                    """
                    UPDATE relay_v3_cleanup_ledger
                    SET state = 'CLEANUP_ROLLBACK_OBSERVED',
                        rollback_receipt_hash = ?, updated_at = ?
                    WHERE candidate_id = ? AND run_id = ?
                      AND state != 'CLEANED'
                    """,
                    (receipt_hash, _utc_now(), candidate_id, run.run_id),
                )
                if cursor.rowcount != 1:
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                updated = cursor.execute(
                    """
                    SELECT * FROM relay_v3_cleanup_ledger WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                if updated is None:
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                return self._cleanup_ledger_public(updated)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def record_cleanup_receipt(
        self,
        workflow_id: str,
        candidate_id: str,
        *,
        operation: Mapping[str, object],
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Record only a successful terminal host cleanup receipt; never perform cleanup."""

        operation_hash = self._validate_cleanup_operation(operation, candidate_id)
        expected = {"schema", "candidate_id", "operation_hash", "status", "receipt_hash"}
        if type(receipt) is not dict or set(receipt) != expected:
            raise RelayStoreError("CLEANUP_HOST_FAILED")
        if (
            receipt["schema"] != "2718lab-devkit/cleanup-receipt-v1"
            or receipt["candidate_id"] != candidate_id
            or receipt["operation_hash"] != operation_hash
            or type(receipt["receipt_hash"]) is not str
            or _DIGEST.fullmatch(receipt["receipt_hash"]) is None
        ):
            raise RelayStoreError("CLEANUP_HOST_FAILED")
        if receipt["status"] != "completed":
            raise RelayStoreError("CLEANUP_HOST_FAILED")
        try:
            with self._transaction() as cursor:
                run = self._run_for_workflow(cursor, workflow_id)
                row = cursor.execute(
                    """
                    SELECT * FROM relay_v3_cleanup_ledger
                    WHERE candidate_id = ? AND run_id = ?
                    """,
                    (candidate_id, run.run_id),
                ).fetchone()
                if row is None or str(row["state"]) != "CLEANUP_ELIGIBLE":
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                if (
                    operation["integration_proof_id"] != row["integration_proof_id"]
                    or operation["integration_version"]
                    != row["merged_integration_version"]
                    or operation["branch_identity"] != row["branch_identity"]
                    or operation["worktree_identity"] != row["worktree_identity"]
                    or operation["delete_merged_branch"] is not True
                    or operation["remove_disposable_worktree"] is not True
                ):
                    raise RelayStoreError("CLEANUP_HOST_FAILED")
                cursor.execute(
                    """
                    UPDATE relay_v3_cleanup_ledger
                    SET state = 'CLEANED', cleanup_receipt_hash = ?, updated_at = ?
                    WHERE candidate_id = ? AND run_id = ?
                      AND state = 'CLEANUP_ELIGIBLE'
                    """,
                    (receipt["receipt_hash"], _utc_now(), candidate_id, run.run_id),
                )
                if cursor.rowcount != 1:
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                updated = cursor.execute(
                    """
                    SELECT * FROM relay_v3_cleanup_ledger WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                if updated is None:
                    raise RelayStoreError("CLEANUP_NOT_ELIGIBLE")
                return self._cleanup_ledger_public(updated)
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    @staticmethod
    def _cleanup_ledger_public(row: sqlite3.Row) -> dict[str, object]:
        return {
            "candidate_id": str(row["candidate_id"]),
            "retention_rounds": int(row["retention_rounds"]),
            "branch_identity": str(row["branch_identity"]),
            "worktree_identity": str(row["worktree_identity"]),
            "integration_proof_id": (
                None if row["integration_proof_id"] is None else str(row["integration_proof_id"])
            ),
            "integration_commit": (
                None if row["integration_commit"] is None else str(row["integration_commit"])
            ),
            "merged_integration_version": row["merged_integration_version"],
            "eligible_after_integration_version": row[
                "eligible_after_integration_version"
            ],
            "state": str(row["state"]),
            "rollback_receipt_hash": (
                None if row["rollback_receipt_hash"] is None else str(row["rollback_receipt_hash"])
            ),
            "cleanup_receipt_hash": (
                None if row["cleanup_receipt_hash"] is None else str(row["cleanup_receipt_hash"])
            ),
        }

    @staticmethod
    def _validate_cleanup_operation(
        operation: Mapping[str, object], candidate_id: str
    ) -> str:
        fields = {
            "schema",
            "candidate_id",
            "integration_proof_id",
            "integration_version",
            "branch_identity",
            "worktree_identity",
            "delete_merged_branch",
            "remove_disposable_worktree",
            "host_recheck_hash",
            "operation_hash",
        }
        if type(operation) is not dict or set(operation) != fields:
            raise RelayStoreError("CLEANUP_HOST_FAILED")
        body = {key: value for key, value in operation.items() if key != "operation_hash"}
        if (
            operation["schema"] != "2718lab-devkit/cleanup-operation-v1"
            or operation["candidate_id"] != candidate_id
            or operation["operation_hash"] != canonical_hash(body)
        ):
            raise RelayStoreError("CLEANUP_HOST_FAILED")
        return str(operation["operation_hash"])

    @staticmethod
    def _validate_cleanup_recheck(
        row: sqlite3.Row, run_row: sqlite3.Row, value: Mapping[str, object]
    ) -> None:
        fields = {
            "schema",
            "candidate_id",
            "integration_proof_id",
            "integration_version",
            "integration_head",
            "contains_candidate_integration_commit",
            "branch_identity",
            "worktree_identity",
            "branch_is_protected",
            "branch_is_current",
            "active_lease",
            "pending_review",
            "approved_g_task_root",
            "worktree_disposable",
            "rollback_receipt_hash",
            "attestation_hash",
        }
        if type(value) is not dict or set(value) != fields:
            raise RelayStoreError("CLEANUP_HOST_RECHECK_FAILED")
        if (
            value["schema"] != "2718lab-devkit/host-cleanup-recheck-v1"
            or value["candidate_id"] != row["candidate_id"]
            or value["integration_proof_id"] != row["integration_proof_id"]
            or value["integration_version"] != run_row["integration_version"]
            or value["integration_head"] != run_row["integration_head"]
            or value["contains_candidate_integration_commit"] is not True
            or value["branch_identity"] != row["branch_identity"]
            or value["worktree_identity"] != row["worktree_identity"]
            or value["branch_is_protected"] is not False
            or value["branch_is_current"] is not False
            or value["active_lease"] is not False
            or value["pending_review"] is not False
            or value["approved_g_task_root"] is not True
            or value["worktree_disposable"] is not True
            or value["rollback_receipt_hash"] is not None
            or type(value["attestation_hash"]) is not str
            or _DIGEST.fullmatch(value["attestation_hash"]) is None
        ):
            raise RelayStoreError("CLEANUP_HOST_RECHECK_FAILED")

    def status(self, workflow_id: str) -> dict[str, object]:
        """Read durable state only; status never allocates or releases anything."""

        connection = self._require_connection()
        row = self._run_row_for_workflow(connection, workflow_id)
        if row is None:
            raise KeyError(workflow_id)
        run = self._run_from_row(row)
        tasks = self._tasks_for_run(connection, run.run_id)
        queues: dict[str, list[dict[str, object]]] = {
            "prepared_prewarms": [],
            "ready": [],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        }
        for task in tasks:
            queues[_queue_for(task.state)].append(task.to_dict())
        leases = [
            self._lease_from_row(row).to_dict()
            for row in connection.execute(
                """
                SELECT * FROM relay_v3_leases WHERE run_id = ?
                ORDER BY task_id, epoch
                """,
                (run.run_id,),
            ).fetchall()
        ]
        directives = [
            self._directive_from_row(row).to_dict()
            for row in connection.execute(
                """
                SELECT directives.* FROM relay_v3_directives AS directives
                JOIN relay_v3_tasks AS tasks
                  ON tasks.run_id = directives.run_id AND tasks.task_id = directives.task_id
                WHERE directives.run_id = ? AND directives.state = 'outstanding'
                ORDER BY tasks.priority DESC, tasks.ordinal, tasks.task_id, directives.directive_id
                """,
                (run.run_id,),
            ).fetchall()
        ]
        candidates = [
            self._candidate_public(row)
            for row in connection.execute(
                """
                SELECT * FROM relay_v3_candidates WHERE run_id = ?
                ORDER BY task_id, candidate_id
                """,
                (run.run_id,),
            ).fetchall()
        ]
        proof_summaries: list[dict[str, object]] = []
        predecessor_head = run.base_commit
        predecessor_version = 0
        proof_rows = connection.execute(
            """
            SELECT * FROM relay_v3_integration_proofs
            WHERE run_id = ? ORDER BY integration_version, proof_id
            """,
            (run.run_id,),
        ).fetchall()
        for proof_row in proof_rows:
            receipt = self._validated_proof_row(connection, proof_row)
            integration_version = int(proof_row["integration_version"])
            if (
                integration_version != predecessor_version + 1
                or receipt.expectation.predecessor_integration_version
                != predecessor_version
                or receipt.predecessor_commit != predecessor_head
            ):
                raise RelayIntegrationProofCorrupt()
            proof_summaries.append(
                {
                    "proof_id": receipt.proof_id,
                    "expectation_hash": receipt.expectation.expectation_hash,
                    "task_id": receipt.expectation.task_id,
                    "candidate_id": receipt.expectation.candidate_id,
                    "integration_version": integration_version,
                    "predecessor_commit": receipt.predecessor_commit,
                    "final_commit": receipt.final_commit,
                    "final_tree": receipt.final_tree,
                    "attestor_id": receipt.attestor_id,
                    "attestor_version": receipt.attestor_version,
                }
            )
            predecessor_head = receipt.final_commit
            predecessor_version = integration_version
        if (
            predecessor_head != run.integration_head
            or predecessor_version != run.integration_version
        ):
            raise RelayIntegrationProofCorrupt()
        return {
            "schema": "2718lab-devkit/relay-status-v2",
            "workflow_id": workflow_id,
            "run": run.to_dict(),
            "schedule_version": run.schedule_version,
            "tasks": [task.to_dict() for task in tasks],
            "leases": leases,
            "candidates": candidates,
            "integration_proofs": proof_summaries,
            "outstanding_action_ids": [
                str(row["action_id"])
                for row in connection.execute(
                    """
                    SELECT action_id FROM relay_v3_actions
                    WHERE run_id = ? AND state = 'outstanding'
                    ORDER BY action_id
                    """,
                    (run.run_id,),
                ).fetchall()
            ],
            "refill_directives": directives,
            "queues": queues,
        }

    def _allocate_action(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        *,
        now: str,
        recovery: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if task.state not in {RelayTaskState.READY, RelayTaskState.PREPARED}:
            raise RelayStateStale()
        if task.state is RelayTaskState.PREPARED and task.kind != "prewarm":
            raise RelayStateStale()
        epoch = task.last_lease_epoch + 1
        next_task_version = task.task_version + 1
        lease_id = _stable_id(
            "lease",
            {"run_id": run.run_id, "task_id": task.task_id, "epoch": epoch},
        )
        action_id = _stable_id(
            "action",
            {"run_id": run.run_id, "task_id": task.task_id, "epoch": epoch},
        )
        lease_kind = "writer" if task.kind == "implementation" else "reader"
        cursor.execute(
            """
            INSERT INTO relay_v3_leases
                (lease_id, run_id, task_id, action_id, epoch, task_version, lease_kind,
                 endpoint, state, created_at, released_at, last_heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, NULL, NULL)
            """,
            (
                lease_id,
                run.run_id,
                task.task_id,
                action_id,
                epoch,
                next_task_version,
                lease_kind,
                now,
            ),
        )
        cursor.execute(
            """
            UPDATE relay_v3_tasks
            SET state = ?, task_version = ?, scope_owner = ?, last_lease_epoch = ?
            WHERE run_id = ? AND task_id = ?
            """,
            (
                RelayTaskState.LEASED.value,
                next_task_version,
                "worker" if task.kind == "implementation" else None,
                epoch,
                run.run_id,
                task.task_id,
            ),
        )
        action: dict[str, object] = {
            "action_id": action_id,
            "kind": "codex.spawn_agent",
            "workflow_id": run.workflow_id,
            "task_id": task.task_id,
            "lease": {
                "lease_id": lease_id,
                "epoch": epoch,
                "task_version": next_task_version,
            },
            "route": _task_route(task),
            "model": _task_route(task)["model"],
            "reasoning_effort": _task_route(task)["reasoning_effort"],
            "task_contract": task.task_contract(),
        }
        if task.kind == "implementation":
            action["worktree_bootstrap"] = self._writer_bootstrap(run, task, epoch)
        else:
            action["read_contract"] = {
                "workspace_id": run.workspace_id,
                "input_snapshot_id": run.input_snapshot_id,
            }
        if recovery is not None:
            action["recovery"] = dict(recovery)
        slot = self._host_scheduler_projection(cursor, run, task)
        if slot is not None:
            action["relay_host_scheduler_slot"] = slot
        cursor.execute(
            """
            INSERT INTO relay_v3_actions
                (action_id, run_id, task_id, lease_id, state, payload_json, created_at)
            VALUES (?, ?, ?, ?, 'outstanding', ?, ?)
            """,
            (action_id, run.run_id, task.task_id, lease_id, _encode_json(action), now),
        )
        return action

    def _writer_bootstrap(
        self, run: RelayRun, task: RelayTask, epoch: int
    ) -> dict[str, object]:
        suffix = canonical_hash(
            {"run_id": run.run_id, "task_id": task.task_id, "epoch": epoch}
        )[7:31]
        return {
            "context_reservation_id": f"context-{suffix}",
            "branch": f"relay/{suffix}",
            "base_commit": run.integration_head,
            "worktree_suffix": f"worktrees/{suffix}",
            "temporary_root_suffix": f"contexts/{suffix}",
        }

    def _host_scheduler_projection(
        self, cursor: sqlite3.Cursor, run: RelayRun, task: RelayTask
    ) -> dict[str, object] | None:
        """Project one SQLite-verified Relay group into the host slot schema."""

        rows = cursor.execute(
            """
            SELECT scheduler_id, coordinator_lease_id, worktree_identity,
                   writer_task_ids_json, prewarm_task_ids_json
            FROM relay_v3_scheduler_groups WHERE run_id = ?
            """,
            (run.run_id,),
        ).fetchall()
        plan_row = cursor.execute(
            "SELECT plan_hash, plan_json FROM relay_v3_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        if plan_row is None or str(plan_row["plan_hash"]) != run.plan_hash:
            raise RelayStorageFailure()
        plan = _decode_json_object(str(plan_row["plan_json"]))
        if plan.get("plan_hash") != run.plan_hash:
            raise RelayStorageFailure()
        try:
            topology = self._validate_scheduler_topology(plan)
        except RelayStoreError as error:
            raise RelayStorageFailure() from error
        if topology is None:
            if rows:
                raise RelayStorageFailure()
            return None
        if not rows:
            raise RelayStorageFailure()
        groups = {str(group["scheduler_id"]): group for group in topology}
        if len(groups) != len(rows):
            raise RelayStorageFailure()
        topology_hash = canonical_hash(plan["scheduler_topology"])
        target = task.task_id
        if task.kind == "design":
            raw_target = task.contract.get("design_for_task_id")
            if type(raw_target) is not str:
                raise RelayStorageFailure()
            target = raw_target
        for row in rows:
            writers = _decode_string_list(str(row["writer_task_ids_json"]))
            prewarms = _decode_string_list(str(row["prewarm_task_ids_json"]))
            binding = {
                "scheduler_id": str(row["scheduler_id"]),
                "coordinator_lease_id": str(row["coordinator_lease_id"]),
                "worktree_identity": str(row["worktree_identity"]),
                "writer_task_ids": writers,
                "prewarm_task_ids": prewarms,
            }
            if groups.get(binding["scheduler_id"]) != binding:
                raise RelayStorageFailure()
            if target not in writers and task.task_id not in prewarms:
                continue
            is_writer = task.kind == "implementation"
            writer_slot: int | None = None
            if is_writer:
                slot_row = cursor.execute(
                    """
                    SELECT slot FROM relay_v3_scheduler_writer_slots
                    WHERE run_id = ? AND scheduler_id = ? AND task_id = ?
                    """,
                    (run.run_id, binding["scheduler_id"], task.task_id),
                ).fetchone()
                if slot_row is None:
                    raise RelayStorageFailure()
                candidate_slot = slot_row["slot"]
                if (
                    type(candidate_slot) is not int
                    or not 1 <= candidate_slot <= 3
                    or candidate_slot != writers.index(task.task_id) + 1
                ):
                    raise RelayStorageFailure()
                writer_slot = candidate_slot
            return {
                "schema": "2718lab-devkit/relay-host-scheduler-slot-v1",
                "plan_hash": run.plan_hash,
                "topology_hash": topology_hash,
                "group_binding_hash": canonical_hash(binding),
                "scheduler_id": binding["scheduler_id"],
                "coordinator_lease_id": binding["coordinator_lease_id"],
                "worktree_identity": binding["worktree_identity"],
                "writer_slot": writer_slot,
                "read_only": not is_writer,
            }
        raise RelayStorageFailure()

    @staticmethod
    def _lease_bootstrap_base(cursor: sqlite3.Cursor, lease: RelayLease) -> str:
        row = cursor.execute(
            "SELECT payload_json FROM relay_v3_actions WHERE lease_id = ?",
            (lease.lease_id,),
        ).fetchone()
        if row is None:
            raise RelayLeaseConflict()
        try:
            payload = _decode_json_object(str(row["payload_json"]))
            bootstrap = payload["worktree_bootstrap"]
            if type(bootstrap) is not dict:
                raise TypeError
            base_commit = bootstrap["base_commit"]
            if type(base_commit) is not str or _COMMIT.fullmatch(base_commit) is None:
                raise TypeError
            return base_commit
        except (KeyError, TypeError, ValueError):
            raise RelayStorageFailure() from None

    def _release_lease(
        self, cursor: sqlite3.Cursor, lease: RelayLease, *, now: str
    ) -> None:
        cursor.execute(
            """
            UPDATE relay_v3_leases
            SET state = 'released', released_at = ? WHERE lease_id = ? AND state = 'active'
            """,
            (now, lease.lease_id),
        )
        if cursor.rowcount != 1:
            raise RelayLeaseConflict()
        cursor.execute(
            """
            UPDATE relay_v3_actions SET state = 'settled'
            WHERE lease_id = ? AND state = 'outstanding'
            """,
            (lease.lease_id,),
        )

    def _promote_dependency_safe_tasks(
        self, cursor: sqlite3.Cursor, run_id: str
    ) -> None:
        for task in self._tasks_for_run(cursor, run_id, state=RelayTaskState.PREPARED):
            if task.kind == "prewarm":
                continue
            if self._dependencies_satisfied(cursor, run_id, _task_dependencies(task)):
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1
                    WHERE run_id = ? AND task_id = ? AND state = ?
                    """,
                    (
                        RelayTaskState.READY.value,
                        run_id,
                        task.task_id,
                        RelayTaskState.PREPARED.value,
                    ),
                )

    def _dependencies_satisfied(
        self, cursor: sqlite3.Cursor, run_id: str, dependencies: Sequence[str]
    ) -> bool:
        for dependency in dependencies:
            row = cursor.execute(
                """
                SELECT kind, state FROM relay_v3_tasks
                WHERE run_id = ? AND task_id = ?
                """,
                (run_id, dependency),
            ).fetchone()
            if row is None:
                return False
            required = (
                RelayTaskState.INTEGRATED.value
                if str(row["kind"]) == "implementation"
                else RelayTaskState.COMPLETED.value
            )
            if str(row["state"]) != required:
                return False
        return True

    def _refresh_directives(
        self, cursor: sqlite3.Cursor, run: RelayRun, *, now: str
    ) -> None:
        """Atomically replace stale directives with the current safe refill set."""

        cursor.execute(
            """
            UPDATE relay_v3_directives SET state = 'superseded'
            WHERE run_id = ? AND state = 'outstanding'
            """,
            (run.run_id,),
        )
        for task in self._select_eligible_tasks(cursor, run):
            route = _task_route(task)
            directive_id = _stable_id(
                "directive",
                {
                    "run_id": run.run_id,
                    "task_id": task.task_id,
                    "schedule_version": run.schedule_version,
                },
            )
            cursor.execute(
                """
                INSERT INTO relay_v3_directives
                    (directive_id, run_id, workflow_id, task_id,
                     expected_schedule_version, route_json, state,
                     consumed_idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'outstanding', NULL, ?)
                """,
                (
                    directive_id,
                    run.run_id,
                    run.workflow_id,
                    task.task_id,
                    run.schedule_version,
                    _encode_json(route),
                    now,
                ),
            )

    def _select_eligible_tasks(
        self, cursor: sqlite3.Cursor, run: RelayRun
    ) -> tuple[RelayTask, ...]:
        active_writers = int(
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM relay_v3_tasks
                WHERE run_id = ? AND kind = 'implementation' AND state IN (?, ?)
                """,
                (
                    run.run_id,
                    RelayTaskState.LEASED.value,
                    RelayTaskState.RUNNING.value,
                ),
            ).fetchone()["count"]
        )
        active_readers = int(
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM relay_v3_tasks
                WHERE run_id = ? AND kind != 'implementation' AND state IN (?, ?)
                """,
                (
                    run.run_id,
                    RelayTaskState.LEASED.value,
                    RelayTaskState.RUNNING.value,
                ),
            ).fetchone()["count"]
        )
        writer_remaining = max(0, run.capacity - active_writers)
        reader_remaining = max(0, self._host_reader_capacity - active_readers)
        if writer_remaining == 0 and reader_remaining == 0:
            return ()
        held_scopes = self._held_writer_scopes(cursor, run.run_id)
        selected: list[RelayTask] = []
        selected_scopes: list[dict[str, str]] = []
        for task in self._dispatchable_tasks(cursor, run.run_id):
            if task.kind == "implementation":
                if writer_remaining == 0:
                    continue
                scopes = _task_scopes(task)
                if _scopes_conflict(scopes, [*held_scopes, *selected_scopes]):
                    continue
                selected_scopes.extend(scopes)
                writer_remaining -= 1
            else:
                if reader_remaining == 0:
                    continue
                reader_remaining -= 1
            selected.append(task)
            if writer_remaining == 0 and reader_remaining == 0:
                break
        return tuple(selected)

    def _dispatchable_tasks(
        self, cursor: sqlite3.Cursor, run_id: str
    ) -> tuple[RelayTask, ...]:
        rows = cursor.execute(
            """
            SELECT * FROM relay_v3_tasks
            WHERE run_id = ? AND (state = ? OR (state = ? AND kind = 'prewarm'))
            ORDER BY priority DESC, ordinal, task_id
            """,
            (run_id, RelayTaskState.READY.value, RelayTaskState.PREPARED.value),
        ).fetchall()
        tasks = tuple(self._task_from_row(row) for row in rows)
        all_tasks = {
            task.task_id: task
            for task in self._tasks_for_run(cursor, run_id)
        }
        return tuple(
            task
            for task in tasks
            if not (
                task.kind == "prewarm"
                and isinstance(task.contract.get("prewarm_for_task_id"), str)
                and all_tasks.get(str(task.contract["prewarm_for_task_id"]))
                is not None
                and all_tasks[str(task.contract["prewarm_for_task_id"])].contract.get(
                    "split_verdict"
                )
                == "UNSPLITTABLE_SCOPE_CONFLICT"
            )
        )

    def _held_writer_scopes(
        self, cursor: sqlite3.Cursor, run_id: str
    ) -> list[dict[str, str]]:
        rows = cursor.execute(
            """
            SELECT write_scope_json FROM relay_v3_tasks
            WHERE run_id = ? AND kind = 'implementation'
              AND state IN (?, ?, ?)
            ORDER BY ordinal, task_id
            """,
            (
                run_id,
                RelayTaskState.LEASED.value,
                RelayTaskState.RUNNING.value,
                RelayTaskState.REVIEW_INTEGRATION.value,
            ),
        ).fetchall()
        return [
            scope
            for row in rows
            for scope in _decode_scope_list(row["write_scope_json"])
        ]

    def _active_task_and_lease(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
    ) -> tuple[RelayTask, RelayLease]:
        task = self._task_for_run(cursor, run.run_id, task_id)
        if (
            task.state not in ACTIVE_TASK_STATES
            or task.task_version != expected_task_version
        ):
            raise RelayStateStale()
        row = cursor.execute(
            """
            SELECT * FROM relay_v3_leases
            WHERE run_id = ? AND task_id = ? AND epoch = ? AND state = 'active'
            """,
            (run.run_id, task_id, epoch),
        ).fetchone()
        if row is None:
            raise RelayLeaseConflict()
        lease = self._lease_from_row(row)
        if lease.task_version > task.task_version:
            raise RelayStateStale()
        return task, lease

    def _recovery_context(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        lease: RelayLease,
        recovery_kind: str,
    ) -> dict[str, object]:
        row = cursor.execute(
            """
            SELECT payload_json FROM relay_v3_actions
            WHERE action_id = ? AND run_id = ? AND task_id = ? AND lease_id = ?
              AND state = 'outstanding'
            """,
            (lease.action_id, run.run_id, task.task_id, lease.lease_id),
        ).fetchone()
        if row is None:
            raise RelayLeaseConflict()
        payload = _decode_json_object(str(row["payload_json"]))
        if (
            payload.get("action_id") != lease.action_id
            or payload.get("kind") != "codex.spawn_agent"
            or payload.get("workflow_id") != run.workflow_id
            or payload.get("task_id") != task.task_id
            or payload.get("lease") != lease.to_public_tuple()
            or payload.get("route") != _task_route(task)
            or payload.get("task_contract") != task.task_contract()
        ):
            raise RelayStorageFailure()
        slot = self._host_scheduler_projection(cursor, run, task)
        if slot is None:
            if "relay_host_scheduler_slot" in payload:
                raise RelayStorageFailure()
        elif payload.get("relay_host_scheduler_slot") != slot:
            raise RelayStorageFailure()
        context: dict[str, object] = {
            "route": _task_route(task),
            "task_contract": task.task_contract(),
        }
        if slot is not None:
            context["relay_host_scheduler_slot"] = slot
        if task.kind == "implementation":
            bootstrap = payload.get("worktree_bootstrap")
            if type(bootstrap) is not dict:
                raise RelayStorageFailure()
            context["worktree_bootstrap"] = bootstrap
        else:
            read_contract = {
                "workspace_id": run.workspace_id,
                "input_snapshot_id": run.input_snapshot_id,
            }
            if payload.get("read_contract") != read_contract:
                raise RelayStorageFailure()
            context["read_contract"] = read_contract
        return {
            "kind": recovery_kind,
            "predecessor_action_id": lease.action_id,
            "predecessor_lease_id": lease.lease_id,
            "predecessor_epoch": lease.epoch,
            "predecessor_context_hash": canonical_hash(context),
        }

    def _candidate_for_sol(
        self,
        cursor: sqlite3.Cursor,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
    ) -> tuple[RelayRun, RelayTask, sqlite3.Row]:
        run = self._run_for_workflow(cursor, workflow_id)
        task = self._task_for_run(cursor, run.run_id, task_id)
        if (
            task.kind != "implementation"
            or task.state is not RelayTaskState.REVIEW_INTEGRATION
            or task.task_version != expected_task_version
            or task.last_lease_epoch != epoch
            or task.candidate_id != candidate_id
        ):
            raise RelayStateStale()
        candidate = cursor.execute(
            """
            SELECT * FROM relay_v3_candidates
            WHERE candidate_id = ? AND run_id = ? AND task_id = ? AND originating_epoch = ?
            """,
            (candidate_id, run.run_id, task_id, epoch),
        ).fetchone()
        if candidate is None:
            raise RelayCandidateError()
        return run, task, candidate

    @staticmethod
    def _integration_expectation(
        run: RelayRun, task: RelayTask, candidate: sqlite3.Row
    ) -> IntegrationExpectation:
        review_digest = candidate["review_digest"]
        if type(review_digest) is not str or _DIGEST.fullmatch(review_digest) is None:
            raise RelayStateStale()
        try:
            evidence_hashes = tuple(
                _decode_string_list(str(candidate["evidence_hashes_json"]))
            )
            write_scope = tuple(
                IntegrationScopeEntry(path=item["path"], kind=item["kind"])
                for item in _task_scopes(task)
            )
        except (KeyError, TypeError, ValueError):
            raise RelayStorageFailure() from None
        return IntegrationExpectation(
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            plan_hash=run.plan_hash,
            workspace_id=run.workspace_id,
            task_id=task.task_id,
            task_version=task.task_version,
            originating_epoch=int(candidate["originating_epoch"]),
            sol_scope="sol:integrate",
            candidate_id=str(candidate["candidate_id"]),
            candidate_base_commit=str(candidate["base_commit"]),
            candidate_head_commit=str(candidate["head_commit"]),
            candidate_diff_hash=str(candidate["diff_hash"]),
            candidate_evidence_hashes=evidence_hashes,
            review_digest=review_digest,
            predecessor_integration_head=run.integration_head,
            predecessor_integration_version=run.integration_version,
            write_scope=write_scope,
        )

    @staticmethod
    def _validate_finalization_fence(fence: ProofFinalizationFence) -> None:
        try:
            validate_finalization_fence(fence)
        except ValueError as error:
            raise RelayFinalizationConflict() from error

    @staticmethod
    def _fence_values(fence: ProofFinalizationFence) -> tuple[object, ...]:
        return (
            fence.finalization_id,
            fence.reservation_epoch,
            fence.integration_proof_id,
            fence.workspace_id,
            fence.expectation_key,
            fence.expectation_version,
            fence.expectation_hash,
            fence.target_ref,
            fence.base_oid,
            fence.final_oid,
            fence.fence_hash,
        )

    @staticmethod
    def _finalization_journal(
        connection: sqlite3.Connection | sqlite3.Cursor,
        finalization_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM relay_v3_finalization_journal WHERE finalization_id = ?",
            (finalization_id,),
        ).fetchone()

    @staticmethod
    def _finalization_outcome(
        connection: sqlite3.Connection | sqlite3.Cursor,
        finalization_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM relay_v3_finalization_outcomes WHERE finalization_id = ?",
            (finalization_id,),
        ).fetchone()

    @staticmethod
    def _integration_proof(
        connection: sqlite3.Connection | sqlite3.Cursor,
        proof_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM relay_v3_integration_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()

    @classmethod
    def _fence_from_journal(cls, row: sqlite3.Row) -> ProofFinalizationFence:
        try:
            fence = ProofFinalizationFence(
                finalization_id=str(row["finalization_id"]),
                reservation_epoch=int(row["reservation_epoch"]),
                integration_proof_id=str(row["integration_proof_id"]),
                workspace_id=str(row["workspace_id"]),
                expectation_key=str(row["expectation_key"]),
                expectation_version=int(row["expectation_version"]),
                expectation_hash=str(row["expectation_hash"]),
                target_ref=str(row["target_ref"]),
                base_oid=str(row["base_oid"]),
                final_oid=str(row["final_oid"]),
            )
            cls._validate_finalization_fence(fence)
            if row["fence_hash"] != fence.fence_hash:
                raise RelayFinalizationConflict()
            return fence
        except (KeyError, TypeError, ValueError) as error:
            raise RelayFinalizationConflict() from error

    @classmethod
    def _require_matching_finalization_fence(
        cls, row: sqlite3.Row, fence: ProofFinalizationFence
    ) -> None:
        if cls._fence_from_journal(row) != fence:
            raise RelayFinalizationConflict()

    @staticmethod
    def _finalization_evidence(
        fence: ProofFinalizationFence,
        state: FinalizationState,
        result_hash: str | None,
        *,
        journal_version: int,
    ) -> ProofFinalizationEvidence:
        evidence = ProofFinalizationEvidence(
            finalization_id=fence.finalization_id,
            state=state,
            fence_hash=fence.fence_hash,
            result_hash=result_hash,
            journal_version=journal_version,
        )
        try:
            validate_finalization_evidence(evidence)
        except ValueError as error:
            raise RelayFinalizationConflict() from error
        return evidence

    @classmethod
    def _evidence_from_journal(
        cls, row: sqlite3.Row
    ) -> ProofFinalizationEvidence:
        fence = cls._fence_from_journal(row)
        state_value = row["state"]
        result_hash = row["result_hash"]
        journal_version = row["journal_version"]
        if (
            type(state_value) is not str
            or state_value not in {"prepared", "committed", "aborted"}
            or (result_hash is not None and type(result_hash) is not str)
            or type(journal_version) is not int
        ):
            raise RelayFinalizationConflict()
        return cls._finalization_evidence(
            fence,
            cast(FinalizationState, state_value),
            cast(str | None, result_hash),
            journal_version=journal_version,
        )

    @staticmethod
    def _integration_proof_matches_fence(
        proof: sqlite3.Row, fence: ProofFinalizationFence
    ) -> bool:
        return (
            proof["proof_id"] == fence.integration_proof_id
            and proof["candidate_id"] == fence.expectation_key
            and proof["expectation_hash"] == fence.expectation_hash
            and proof["integration_ref"] == fence.target_ref
            and proof["predecessor_commit"] == fence.base_oid
            and proof["final_commit"] == fence.final_oid
        )

    @staticmethod
    def _outcome_result(outcome: sqlite3.Row | None) -> dict[str, object]:
        if outcome is None or type(outcome["result_hash"]) is not str:
            raise RelayFinalizationConflict()
        try:
            result = _decode_json_object(str(outcome["result_json"]))
        except (TypeError, ValueError):
            raise RelayFinalizationConflict() from None
        if canonical_hash(result) != outcome["result_hash"]:
            raise RelayFinalizationConflict()
        return result

    @classmethod
    def _require_committed_finalization(
        cls,
        connection: sqlite3.Connection | sqlite3.Cursor,
        fence: ProofFinalizationFence,
        journal: sqlite3.Row,
        outcome: sqlite3.Row | None,
    ) -> None:
        evidence = cls._evidence_from_journal(journal)
        proof = cls._integration_proof(connection, fence.integration_proof_id)
        if (
            evidence.state != "committed"
            or evidence.result_hash is None
            or outcome is None
            or proof is None
            or not cls._integration_proof_matches_fence(proof, fence)
            or outcome["finalization_id"] != fence.finalization_id
            or outcome["fence_hash"] != fence.fence_hash
            or outcome["integration_proof_id"] != fence.integration_proof_id
            or outcome["expectation_key"] != fence.expectation_key
            or outcome["expectation_version"] != fence.expectation_version
            or outcome["expectation_hash"] != fence.expectation_hash
            or outcome["result_hash"] != evidence.result_hash
        ):
            raise RelayFinalizationConflict()
        cls._outcome_result(outcome)

    def _require_current_finalization_expectation(
        self,
        connection: sqlite3.Connection | sqlite3.Cursor,
        fence: ProofFinalizationFence,
    ) -> None:
        candidate = connection.execute(
            "SELECT * FROM relay_v3_candidates WHERE candidate_id = ?",
            (fence.expectation_key,),
        ).fetchone()
        if candidate is None:
            raise RelayExpectationStale()
        run_row = connection.execute(
            "SELECT * FROM relay_v3_runs WHERE run_id = ?", (candidate["run_id"],)
        ).fetchone()
        task_row = connection.execute(
            """
            SELECT * FROM relay_v3_tasks WHERE run_id = ? AND task_id = ?
            """,
            (candidate["run_id"], candidate["task_id"]),
        ).fetchone()
        if run_row is None or task_row is None:
            raise RelayFinalizationConflict()
        run = self._run_from_row(run_row)
        task = self._task_from_row(task_row)
        if (
            task.kind != "implementation"
            or task.state is not RelayTaskState.REVIEW_INTEGRATION
            or task.candidate_id != fence.expectation_key
            or str(candidate["status"]) != "reviewed"
        ):
            raise RelayExpectationStale()
        current = self._integration_expectation(run, task, candidate)
        if (
            fence.workspace_id != run.workspace_id
            or fence.expectation_version != current.task_version
            or fence.expectation_hash != current.expectation_hash
        ):
            raise RelayExpectationStale()

    @staticmethod
    def _require_finalization_integration_fence(
        fence: ProofFinalizationFence,
        *,
        proof_id: str,
        expectation: IntegrationExpectation,
        receipt: IntegrationProofReceipt,
    ) -> None:
        if (
            fence.integration_proof_id != proof_id
            or fence.workspace_id != expectation.workspace_id
            or fence.expectation_key != expectation.candidate_id
            or fence.expectation_version != expectation.task_version
            or fence.expectation_hash != expectation.expectation_hash
            or fence.target_ref != receipt.integration_ref
            or fence.base_oid != receipt.ref_before_commit
            or fence.final_oid != receipt.ref_after_commit
        ):
            raise RelayFinalizationConflict()

    def _validated_proof_row(
        self,
        connection: sqlite3.Connection | sqlite3.Cursor,
        row: sqlite3.Row,
    ) -> IntegrationProofReceipt:
        try:
            expectation_data = _decode_json_object(str(row["expectation_json"]))
            receipt_data = _decode_json_object(str(row["receipt_json"]))
            expectation = IntegrationExpectation.from_dict(expectation_data)
            receipt = IntegrationProofReceipt.from_dict(receipt_data)
            proof_id = str(row["proof_id"])
            validate_integration_proof(proof_id, expectation, receipt)
            if (
                row["expectation_hash"] != expectation.expectation_hash
                or receipt.expectation != expectation
                or row["run_id"] != expectation.run_id
                or row["workflow_id"] != expectation.workflow_id
                or row["task_id"] != expectation.task_id
                or row["candidate_id"] != expectation.candidate_id
                or row["integration_version"]
                != expectation.predecessor_integration_version + 1
                or row["repository_id"] != receipt.repository_id
                or row["integration_ref"] != receipt.integration_ref
                or row["predecessor_commit"] != receipt.predecessor_commit
                or row["final_commit"] != receipt.final_commit
                or row["final_tree"] != receipt.final_tree
                or row["attestor_id"] != receipt.attestor_id
                or row["attestor_version"] != receipt.attestor_version
                or _encode_json(expectation.to_dict()) != row["expectation_json"]
                or _encode_json(receipt.to_dict()) != row["receipt_json"]
            ):
                raise ValueError("proof row binding mismatch")
            run_row = connection.execute(
                "SELECT * FROM relay_v3_runs WHERE run_id = ?",
                (expectation.run_id,),
            ).fetchone()
            task_row = connection.execute(
                """
                SELECT * FROM relay_v3_tasks WHERE run_id = ? AND task_id = ?
                """,
                (expectation.run_id, expectation.task_id),
            ).fetchone()
            if run_row is None or task_row is None:
                raise ValueError("missing proof authority row")
            task = self._task_from_row(task_row)
            durable_scope = tuple(
                IntegrationScopeEntry(path=item["path"], kind=item["kind"])
                for item in _task_scopes(task)
            )
            if (
                run_row["workflow_id"] != expectation.workflow_id
                or run_row["plan_hash"] != expectation.plan_hash
                or run_row["workspace_id"] != expectation.workspace_id
                or task.kind != "implementation"
                or task.state is not RelayTaskState.INTEGRATED
                or task.task_version != expectation.task_version + 1
                or task.last_lease_epoch != expectation.originating_epoch
                or task.candidate_id != expectation.candidate_id
                or durable_scope != expectation.write_scope
            ):
                raise ValueError("task proof binding mismatch")
            candidate = connection.execute(
                "SELECT * FROM relay_v3_candidates WHERE candidate_id = ?",
                (expectation.candidate_id,),
            ).fetchone()
            if (
                candidate is None
                or candidate["run_id"] != expectation.run_id
                or candidate["task_id"] != expectation.task_id
                or candidate["originating_epoch"] != expectation.originating_epoch
                or candidate["base_commit"] != expectation.candidate_base_commit
                or candidate["head_commit"] != expectation.candidate_head_commit
                or candidate["diff_hash"] != expectation.candidate_diff_hash
                or tuple(_decode_string_list(str(candidate["evidence_hashes_json"])))
                != expectation.candidate_evidence_hashes
                or candidate["review_digest"] != expectation.review_digest
                or candidate["integration_commit"] != receipt.final_commit
                or candidate["integration_tree"] != receipt.final_tree
                or candidate["integration_proof_id"] != proof_id
                or candidate["status"] != "integrated"
            ):
                raise ValueError("candidate proof binding mismatch")
            return receipt
        except (
            IntegrationProofError,
            KeyError,
            RelayStoreError,
            TypeError,
            ValueError,
            UnicodeError,
        ):
            raise RelayIntegrationProofCorrupt() from None

    def _require_evidence(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        epoch: int,
        candidate: Mapping[str, object],
    ) -> None:
        evidence_hashes = candidate["evidence_hashes"]
        if type(evidence_hashes) is not list:
            raise RelayCandidateError()
        self._require_evidence_hashes(cursor, run, task, epoch, list(evidence_hashes))

    def _require_evidence_hashes(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        epoch: int,
        hashes: Sequence[str],
    ) -> None:
        available = self._available_evidence(cursor, run, task, epoch)
        required = [
            (str(item["kind"]), str(item["selector"]))
            for item in _task_required_evidence(task)
        ]
        provided = set(hashes)
        recorded = {
            digest
            for evidence_hashes in available.values()
            for digest in evidence_hashes
        }
        if provided != recorded or any(
            key not in available or not (available[key] & provided) for key in required
        ):
            raise RelayCandidateError()

    def _require_required_evidence(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        epoch: int,
    ) -> None:
        available = self._available_evidence(cursor, run, task, epoch)
        required = [
            (str(item["kind"]), str(item["selector"]))
            for item in _task_required_evidence(task)
        ]
        if any(key not in available for key in required):
            raise RelayCandidateError()

    @staticmethod
    def _available_evidence(
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        epoch: int,
    ) -> dict[tuple[str, str], frozenset[str]]:
        available: dict[tuple[str, str], set[str]] = {}
        for row in cursor.execute(
            """
            SELECT kind, selector, digest FROM relay_v3_evidence
            WHERE run_id = ? AND task_id = ? AND epoch = ?
            ORDER BY kind, selector, digest
            """,
            (run.run_id, task.task_id, epoch),
        ).fetchall():
            key = (str(row["kind"]), str(row["selector"]))
            available.setdefault(key, set()).add(str(row["digest"]))
        return {key: frozenset(digests) for key, digests in available.items()}

    def _normalize_candidate(self, value: Mapping[str, object]) -> dict[str, object]:
        expected = {
            "candidate_id",
            "branch",
            "base_commit",
            "head_commit",
            "diff_hash",
            "evidence_hashes",
            "pr_reference",
        }
        optional = {"cleanup_policy"}
        if type(value) is not dict or (
            set(value) != expected and set(value) != expected | optional
        ):
            raise RelayCandidateError()
        candidate_id = value["candidate_id"]
        branch = value["branch"]
        base_commit = value["base_commit"]
        head_commit = value["head_commit"]
        diff_hash = value["diff_hash"]
        evidence_hashes = value["evidence_hashes"]
        pr_reference = value["pr_reference"]
        cleanup_policy = (
            self._normalize_cleanup_policy(
                value["cleanup_policy"], branch=branch, head_commit=head_commit
            )
            if "cleanup_policy" in value
            else None
        )
        if (
            type(candidate_id) is not str
            or _IDENTIFIER.fullmatch(candidate_id) is None
            or not _safe_branch(branch)
            or type(base_commit) is not str
            or _COMMIT.fullmatch(base_commit) is None
            or type(head_commit) is not str
            or _COMMIT.fullmatch(head_commit) is None
            or type(diff_hash) is not str
            or _DIGEST.fullmatch(diff_hash) is None
            or not _valid_digest_list(evidence_hashes)
            or not _valid_pr_reference(pr_reference)
        ):
            raise RelayCandidateError()
        return {
            "candidate_id": candidate_id,
            "branch": branch,
            "base_commit": base_commit,
            "head_commit": head_commit,
            "diff_hash": diff_hash,
            "evidence_hashes": sorted(evidence_hashes),
            "pr_reference": pr_reference,
            "cleanup_policy": cleanup_policy,
        }

    @staticmethod
    def _normalize_cleanup_policy(
        value: object, *, branch: object, head_commit: object
    ) -> dict[str, object]:
        expected = {
            "retention_rounds",
            "delete_merged_branch",
            "remove_disposable_worktree",
            "branch_identity",
            "worktree_identity",
        }
        if type(value) is not dict or set(value) != expected:
            raise RelayCandidateError()
        retention_rounds = value["retention_rounds"]
        branch_identity = value["branch_identity"]
        worktree_identity = value["worktree_identity"]
        if (
            type(retention_rounds) is not int
            or not 1 <= retention_rounds <= 32
            or value["delete_merged_branch"] is not True
            or value["remove_disposable_worktree"] is not True
            or type(branch) is not str
            or type(head_commit) is not str
            or branch_identity
            != canonical_hash({"branch": branch, "head_commit": head_commit})
            or type(worktree_identity) is not str
            or _DIGEST.fullmatch(worktree_identity) is None
        ):
            raise RelayCandidateError()
        return {
            "retention_rounds": retention_rounds,
            "delete_merged_branch": True,
            "remove_disposable_worktree": True,
            "branch_identity": branch_identity,
            "worktree_identity": worktree_identity,
        }

    def _mutation_result(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task_id: str,
        *,
        candidate_id: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "workflow_id": run.workflow_id,
            "schedule_version": run.schedule_version,
            "task": self._task_for_run(cursor, run.run_id, task_id).to_dict(),
        }
        if candidate_id is not None:
            row = cursor.execute(
                "SELECT * FROM relay_v3_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise RelayCandidateError()
            result["candidate"] = self._candidate_public(row)
        return result

    def _start_result(
        self, run: RelayRun, actions: Sequence[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "schema": "2718lab-devkit/relay-start-result-v1",
            "workflow_id": run.workflow_id,
            "run_id": run.run_id,
            "schedule_version": run.schedule_version,
            "host_actions": [dict(action) for action in actions],
        }

    def _increment_schedule_version(
        self, cursor: sqlite3.Cursor, run_id: str
    ) -> RelayRun:
        cursor.execute(
            """
            UPDATE relay_v3_runs
            SET schedule_version = schedule_version + 1 WHERE run_id = ?
            """,
            (run_id,),
        )
        return self._run_from_row(self._run_row(cursor, run_id))

    def _idempotency_replay(
        self, cursor: sqlite3.Cursor, key: str, payload_hash: str
    ) -> dict[str, object] | None:
        row = cursor.execute(
            """
            SELECT payload_hash, result_json FROM relay_v3_idempotency
            WHERE idempotency_key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_hash"]) != payload_hash:
            raise RelayIdempotencyConflict()
        return _decode_json_object(str(row["result_json"]))

    def _record_idempotency(
        self,
        cursor: sqlite3.Cursor,
        *,
        idempotency_key: str,
        payload_hash: str,
        result: Mapping[str, object],
        created_at: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO relay_v3_idempotency
                (idempotency_key, payload_hash, result_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (idempotency_key, payload_hash, _encode_json(result), created_at),
        )

    def _run_for_workflow(
        self,
        connection: sqlite3.Connection | sqlite3.Cursor,
        workflow_id: str,
    ) -> RelayRun:
        row = self._run_row_for_workflow(connection, workflow_id)
        if row is None:
            raise KeyError(workflow_id)
        return self._run_from_row(row)

    @staticmethod
    def _run_row(cursor: sqlite3.Cursor, run_id: str) -> sqlite3.Row:
        row = cursor.execute(
            "SELECT * FROM relay_v3_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    @staticmethod
    def _run_row_for_workflow(
        connection: sqlite3.Connection | sqlite3.Cursor, workflow_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM relay_v3_runs WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()

    def _task_for_run(
        self,
        connection: sqlite3.Connection | sqlite3.Cursor,
        run_id: str,
        task_id: str,
    ) -> RelayTask:
        row = connection.execute(
            """
            SELECT * FROM relay_v3_tasks WHERE run_id = ? AND task_id = ?
            """,
            (run_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def _tasks_for_run(
        self,
        connection: sqlite3.Connection | sqlite3.Cursor,
        run_id: str,
        *,
        state: RelayTaskState | None = None,
    ) -> tuple[RelayTask, ...]:
        if state is None:
            rows = connection.execute(
                """
                SELECT * FROM relay_v3_tasks WHERE run_id = ?
                ORDER BY priority DESC, ordinal, task_id
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM relay_v3_tasks WHERE run_id = ? AND state = ?
                ORDER BY priority DESC, ordinal, task_id
                """,
                (run_id, state.value),
            ).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RelayRun:
        capacity = row["capacity"]
        if type(capacity) is not int or not 1 <= capacity <= 9:
            raise RelayStorageFailure()
        integration_head = row["integration_head"]
        integration_version = row["integration_version"]
        if (
            type(integration_head) is not str
            or _COMMIT.fullmatch(integration_head) is None
            or type(integration_version) is not int
            or integration_version < 0
        ):
            raise RelayIntegrationProofCorrupt()
        return RelayRun(
            run_id=str(row["run_id"]),
            workflow_id=str(row["workflow_id"]),
            plan_hash=str(row["plan_hash"]),
            workspace_id=str(row["workspace_id"]),
            input_snapshot_id=str(row["input_snapshot_id"]),
            base_commit=str(row["base_commit"]),
            integration_head=integration_head,
            integration_version=integration_version,
            capacity=capacity,
            schedule_version=int(row["schedule_version"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> RelayTask:
        contract = _decode_json_object(str(row["task_json"]))
        return RelayTask(
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            ordinal=int(row["ordinal"]),
            kind=str(row["kind"]),
            priority=int(row["priority"]),
            contract=contract,
            state=RelayTaskState(str(row["state"])),
            task_version=int(row["task_version"]),
            scope_owner=(
                None if row["scope_owner"] is None else str(row["scope_owner"])
            ),
            candidate_id=(
                None if row["candidate_id"] is None else str(row["candidate_id"])
            ),
            last_lease_epoch=int(row["last_lease_epoch"]),
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> RelayLease:
        return RelayLease(
            lease_id=str(row["lease_id"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            action_id=str(row["action_id"]),
            epoch=int(row["epoch"]),
            task_version=int(row["task_version"]),
            lease_kind=str(row["lease_kind"]),
            endpoint=None if row["endpoint"] is None else str(row["endpoint"]),
            state=str(row["state"]),
            created_at=str(row["created_at"]),
            released_at=(
                None if row["released_at"] is None else str(row["released_at"])
            ),
        )

    def _lease_for_id(self, cursor: sqlite3.Cursor, lease_id: str) -> RelayLease:
        row = cursor.execute(
            "SELECT * FROM relay_v3_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        if row is None:
            raise RelayLeaseConflict()
        return self._lease_from_row(row)

    @staticmethod
    def _directive_from_row(row: sqlite3.Row) -> RefillDirective:
        route = _decode_json_object(str(row["route_json"]))
        return RefillDirective(
            directive_id=str(row["directive_id"]),
            run_id=str(row["run_id"]),
            workflow_id=str(row["workflow_id"]),
            task_id=str(row["task_id"]),
            expected_schedule_version=int(row["expected_schedule_version"]),
            route={str(key): str(value) for key, value in route.items()},
            state=str(row["state"]),
        )

    @staticmethod
    def _candidate_public(row: sqlite3.Row) -> dict[str, object]:
        return {
            "candidate_id": str(row["candidate_id"]),
            "task_id": str(row["task_id"]),
            "originating_epoch": int(row["originating_epoch"]),
            "branch": str(row["branch"]),
            "base_commit": str(row["base_commit"]),
            "head_commit": str(row["head_commit"]),
            "diff_hash": str(row["diff_hash"]),
            "evidence_hashes": _decode_string_list(str(row["evidence_hashes_json"])),
            "pr_reference": (
                None if row["pr_reference"] is None else str(row["pr_reference"])
            ),
            "status": str(row["status"]),
            "review_digest": (
                None if row["review_digest"] is None else str(row["review_digest"])
            ),
            "integration_commit": (
                None
                if row["integration_commit"] is None
                else str(row["integration_commit"])
            ),
            "integration_tree": (
                None
                if row["integration_tree"] is None
                else str(row["integration_tree"])
            ),
            "integration_proof_id": (
                None
                if row["integration_proof_id"] is None
                else str(row["integration_proof_id"])
            ),
        }

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if (
            type(value) is not str
            or not value
            or len(value) > RelayStore._MAX_IDEMPOTENCY_KEY_LENGTH
            or any(character.isspace() for character in value)
        ):
            raise RelayIdempotencyConflict()

    @staticmethod
    def _validate_scheduler_topology(
        plan: Mapping[str, Any],
    ) -> tuple[dict[str, object], ...] | None:
        """Fail closed for supplied Scheduler Topology V1 authority."""

        value = plan.get("scheduler_topology")
        if value is None:
            return None
        if plan.get("plan_hash") != canonical_hash(
            {key: item for key, item in plan.items() if key != "plan_hash"}
        ):
            raise RelayTopologyInvalid()
        expected = {
            "schema",
            "max_writers_per_scheduler",
            "max_parallel_writers",
            "groups",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or value["schema"] != "2718lab-devkit/scheduler-topology-v1"
            or type(value["max_writers_per_scheduler"]) is not int
            or value["max_writers_per_scheduler"] != 3
            or type(value["max_parallel_writers"]) is not int
            or value["max_parallel_writers"] != 9
            or type(value["groups"]) is not list
            or not value["groups"]
        ):
            raise RelayTopologyInvalid()
        tasks = plan.get("tasks")
        if type(tasks) is not list or any(type(task) is not dict for task in tasks):
            raise RelayTopologyInvalid()
        task_index = {str(task.get("task_id")): task for task in tasks}
        if len(task_index) != len(tasks) or any(
            not _opaque_topology_value(task.get("task_id")) for task in tasks
        ):
            raise RelayTopologyInvalid()
        implementation_ids = {
            str(task["task_id"])
            for task in tasks
            if task.get("kind") == "implementation"
        }
        unsplittable_implementation_ids = {
            str(task["task_id"])
            for task in tasks
            if task.get("kind") == "implementation"
            and task.get("split_verdict") == "UNSPLITTABLE_SCOPE_CONFLICT"
        }
        split_children: dict[str, set[str]] = {}
        for task_id in implementation_ids:
            parent_id = task_index[task_id].get("split_parent_task_id")
            if type(parent_id) is str:
                split_children.setdefault(parent_id, set()).add(task_id)
        prewarm_targets: dict[str, set[str]] = {}
        for task in tasks:
            if task.get("kind") != "prewarm":
                continue
            target = task.get("prewarm_for_task_id")
            members = (
                {target}
                if type(target) is str and target in implementation_ids
                else set(split_children.get(target, set()))
                if type(target) is str
                else set()
            )
            if members and not members & unsplittable_implementation_ids:
                prewarm_targets[str(task["task_id"])] = members
        prewarm_ids = set(prewarm_targets)

        groups: list[dict[str, object]] = []
        scheduler_ids: set[str] = set()
        coordinator_leases: set[str] = set()
        worktrees: set[str] = set()
        writer_owner: dict[str, str] = {}
        prewarm_owner: dict[str, str] = {}
        for group in value["groups"]:
            expected_group = {
                "scheduler_id",
                "coordinator_lease_id",
                "worktree_identity",
                "writer_task_ids",
                "prewarm_task_ids",
            }
            if type(group) is not dict or set(group) != expected_group:
                raise RelayTopologyInvalid()
            scheduler_id = group["scheduler_id"]
            coordinator_lease_id = group["coordinator_lease_id"]
            worktree_identity = group["worktree_identity"]
            writer_task_ids = group["writer_task_ids"]
            prewarm_task_ids = group["prewarm_task_ids"]
            if (
                not _opaque_topology_value(scheduler_id)
                or not _opaque_topology_value(coordinator_lease_id)
                or not _opaque_topology_value(worktree_identity)
                or type(writer_task_ids) is not list
                or type(prewarm_task_ids) is not list
                or len(writer_task_ids) > 3
                or any(
                    not _opaque_topology_value(task_id) for task_id in writer_task_ids
                )
                or any(
                    not _opaque_topology_value(task_id) for task_id in prewarm_task_ids
                )
            ):
                raise RelayTopologyInvalid()
            scheduler = str(scheduler_id)
            coordinator = str(coordinator_lease_id)
            worktree = str(worktree_identity)
            writers = [str(task_id) for task_id in writer_task_ids]
            prewarms = [str(task_id) for task_id in prewarm_task_ids]
            if (
                scheduler in scheduler_ids
                or coordinator in coordinator_leases
                or worktree in worktrees
                or len(set(writers)) != len(writers)
                or len(set(prewarms)) != len(prewarms)
            ):
                raise RelayTopologyInvalid()
            scheduler_ids.add(scheduler)
            coordinator_leases.add(coordinator)
            worktrees.add(worktree)
            for task_id in writers:
                if (
                    task_id not in task_index
                    or task_index[task_id].get("kind") != "implementation"
                    or task_id not in implementation_ids
                ):
                    raise RelayTopologyInvalid()
                if task_id in writer_owner:
                    raise RelayTopologyInvalid()
                writer_owner[task_id] = scheduler
            for task_id in prewarms:
                if (
                    task_id not in task_index
                    or task_index[task_id].get("kind") != "prewarm"
                    or task_id not in prewarm_ids
                    or not prewarm_targets[task_id] <= set(writers)
                ):
                    raise RelayTopologyInvalid()
                if task_id in prewarm_owner:
                    raise RelayTopologyInvalid()
                prewarm_owner[task_id] = scheduler
            groups.append(
                {
                    "scheduler_id": scheduler,
                    "coordinator_lease_id": coordinator,
                    "worktree_identity": worktree,
                    "writer_task_ids": writers,
                    "prewarm_task_ids": prewarms,
                }
            )

        if (
            set(writer_owner) != implementation_ids
            or set(prewarm_owner) != prewarm_ids
            or len(writer_owner) > 9
        ):
            raise RelayTopologyInvalid()
        for left_id, left_scheduler in writer_owner.items():
            for right_id, right_scheduler in writer_owner.items():
                if left_id >= right_id or left_scheduler == right_scheduler:
                    continue
                left = task_index[left_id]
                right = task_index[right_id]
                if not _scopes_conflict(
                    _topology_task_scopes(left), _topology_task_scopes(right)
                ) and not (
                    _declared_child_split(left, right)
                    or _declared_child_split(right, left)
                ):
                    continue
                if not (
                    _declared_child_split(left, right)
                    or _declared_child_split(right, left)
                ):
                    raise RelayTopologyInvalid()
        return tuple(groups)

    @staticmethod
    def _record_scheduler_topology(
        cursor: sqlite3.Cursor,
        run_id: str,
        groups: Sequence[Mapping[str, object]],
    ) -> None:
        for group in groups:
            scheduler_id = str(group["scheduler_id"])
            cursor.execute(
                """
                INSERT INTO relay_v3_scheduler_groups
                    (run_id, scheduler_id, coordinator_lease_id, worktree_identity,
                     writer_task_ids_json, prewarm_task_ids_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scheduler_id,
                    str(group["coordinator_lease_id"]),
                    str(group["worktree_identity"]),
                    _encode_json(group["writer_task_ids"]),
                    _encode_json(group["prewarm_task_ids"]),
                ),
            )
            writer_task_ids = group["writer_task_ids"]
            if type(writer_task_ids) is not list:
                raise RelayTopologyInvalid()
            for slot, task_id in enumerate(writer_task_ids, start=1):
                cursor.execute(
                    """
                    INSERT INTO relay_v3_scheduler_writer_slots
                        (run_id, scheduler_id, task_id, slot)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, scheduler_id, str(task_id), slot),
                )

    def _assert_schema_compatible(self) -> None:
        """Inspect existing Relay metadata before any WAL or DDL mutation."""

        connection = self._require_connection()
        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            metadata = "relay_v3_schema_metadata"
            relay_tables = {name for name in tables if name.startswith("relay_v3_")}
            if metadata not in tables:
                if relay_tables:
                    raise RelaySchemaIncompatible()
                return
            rows = connection.execute(
                "SELECT value FROM relay_v3_schema_metadata WHERE key = ?",
                ("schema_version",),
            ).fetchall()
            if len(rows) != 1 or str(rows[0]["value"]) not in {
                "5",
                "6",
                "7",
                str(self._SCHEMA_VERSION),
            }:
                raise RelaySchemaIncompatible()
            version = str(rows[0]["value"])
            self._legacy_schema_version = (
                version if version != str(self._SCHEMA_VERSION) else None
            )
        except RelaySchemaIncompatible:
            raise
        except sqlite3.Error as error:
            raise RelaySchemaIncompatible() from error

    def _create_schema(self) -> None:
        connection = self._require_connection()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS relay_v3_schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relay_v3_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL UNIQUE,
                    plan_hash TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    input_snapshot_id TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    integration_head TEXT NOT NULL,
                    integration_version INTEGER NOT NULL
                        CHECK (integration_version >= 0),
                    capacity INTEGER NOT NULL
                        CHECK (typeof(capacity) = 'integer')
                        CHECK (capacity BETWEEN 1 AND 9),
                    schedule_version INTEGER NOT NULL CHECK (schedule_version >= 0),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relay_v3_tasks (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    task_json TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    write_scope_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    task_version INTEGER NOT NULL CHECK (task_version >= 0),
                    scope_owner TEXT,
                    candidate_id TEXT,
                    last_lease_epoch INTEGER NOT NULL CHECK (last_lease_epoch >= 0),
                    PRIMARY KEY (run_id, task_id),
                    FOREIGN KEY (run_id) REFERENCES relay_v3_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_leases (
                    lease_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL UNIQUE,
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    task_version INTEGER NOT NULL CHECK (task_version >= 1),
                    lease_kind TEXT NOT NULL,
                    endpoint TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    last_heartbeat_at TEXT,
                    UNIQUE (run_id, task_id, epoch),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_actions (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE,
                    FOREIGN KEY (lease_id) REFERENCES relay_v3_leases(lease_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_directives (
                    directive_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    expected_schedule_version INTEGER NOT NULL,
                    route_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    consumed_idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relay_v3_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    selector TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, task_id, epoch, kind, selector, digest),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE,
                    FOREIGN KEY (lease_id) REFERENCES relay_v3_leases(lease_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    originating_epoch INTEGER NOT NULL,
                    branch TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    head_commit TEXT NOT NULL,
                    diff_hash TEXT NOT NULL,
                    evidence_hashes_json TEXT NOT NULL,
                    pr_reference TEXT,
                    status TEXT NOT NULL,
                    review_digest TEXT,
                    integration_commit TEXT,
                    integration_tree TEXT,
                    integration_proof_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, task_id),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_integration_proofs (
                    proof_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL UNIQUE,
                    integration_version INTEGER NOT NULL CHECK (integration_version >= 1),
                    expectation_hash TEXT NOT NULL,
                    expectation_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    integration_ref TEXT NOT NULL,
                    predecessor_commit TEXT NOT NULL,
                    final_commit TEXT NOT NULL,
                    final_tree TEXT NOT NULL,
                    attestor_id TEXT NOT NULL,
                    attestor_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, integration_version),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (candidate_id)
                        REFERENCES relay_v3_candidates(candidate_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS relay_v3_finalization_journal (
                    finalization_id TEXT PRIMARY KEY,
                    reservation_epoch INTEGER NOT NULL
                        CHECK (reservation_epoch >= 1),
                    integration_proof_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    expectation_key TEXT NOT NULL,
                    expectation_version INTEGER NOT NULL
                        CHECK (expectation_version >= 1),
                    expectation_hash TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    base_oid TEXT NOT NULL,
                    final_oid TEXT NOT NULL,
                    fence_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('prepared', 'committed', 'aborted')),
                    result_hash TEXT,
                    journal_version INTEGER NOT NULL CHECK (journal_version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (state = 'committed' AND result_hash IS NOT NULL)
                        OR (state IN ('prepared', 'aborted') AND result_hash IS NULL)
                    ),
                    UNIQUE (integration_proof_id, reservation_epoch),
                    FOREIGN KEY (integration_proof_id)
                        REFERENCES relay_v3_integration_proofs(proof_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS relay_v3_finalization_outcomes (
                    finalization_id TEXT PRIMARY KEY,
                    fence_hash TEXT NOT NULL UNIQUE,
                    integration_proof_id TEXT NOT NULL,
                    expectation_key TEXT NOT NULL,
                    expectation_version INTEGER NOT NULL
                        CHECK (expectation_version >= 1),
                    expectation_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (finalization_id)
                        REFERENCES relay_v3_finalization_journal(finalization_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY (integration_proof_id)
                        REFERENCES relay_v3_integration_proofs(proof_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS relay_v3_cleanup_ledger (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    retention_rounds INTEGER NOT NULL
                        CHECK (retention_rounds BETWEEN 1 AND 32),
                    branch_identity TEXT NOT NULL,
                    worktree_identity TEXT NOT NULL,
                    delete_merged_branch INTEGER NOT NULL CHECK (
                        delete_merged_branch IN (0, 1)
                    ),
                    remove_disposable_worktree INTEGER NOT NULL CHECK (
                        remove_disposable_worktree IN (0, 1)
                    ),
                    integration_proof_id TEXT,
                    integration_commit TEXT,
                    merged_integration_version INTEGER,
                    eligible_after_integration_version INTEGER,
                    state TEXT NOT NULL CHECK (state IN (
                        'CLEANUP_PENDING', 'CLEANUP_ELIGIBLE',
                        'CLEANUP_ROLLBACK_OBSERVED', 'CLEANED'
                    )),
                    rollback_receipt_hash TEXT,
                    cleanup_receipt_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (candidate_id)
                        REFERENCES relay_v3_candidates(candidate_id) ON DELETE RESTRICT,
                    FOREIGN KEY (run_id) REFERENCES relay_v3_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (integration_proof_id)
                        REFERENCES relay_v3_integration_proofs(proof_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS relay_v3_scheduler_groups (
                    run_id TEXT NOT NULL,
                    scheduler_id TEXT NOT NULL,
                    coordinator_lease_id TEXT NOT NULL,
                    worktree_identity TEXT NOT NULL,
                    writer_task_ids_json TEXT NOT NULL,
                    prewarm_task_ids_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, scheduler_id),
                    UNIQUE (run_id, coordinator_lease_id),
                    UNIQUE (run_id, worktree_identity),
                    FOREIGN KEY (run_id) REFERENCES relay_v3_runs(run_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relay_v3_scheduler_writer_slots (
                    run_id TEXT NOT NULL,
                    scheduler_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
                    PRIMARY KEY (run_id, task_id),
                    UNIQUE (run_id, scheduler_id, slot),
                    FOREIGN KEY (run_id, scheduler_id)
                        REFERENCES relay_v3_scheduler_groups(run_id, scheduler_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS relay_v3_tasks_by_state
                    ON relay_v3_tasks(run_id, state, priority DESC, ordinal, task_id);
                CREATE INDEX IF NOT EXISTS relay_v3_directives_by_state
                    ON relay_v3_directives(run_id, state, expected_schedule_version);
                CREATE INDEX IF NOT EXISTS relay_v3_proofs_by_run
                    ON relay_v3_integration_proofs(run_id, integration_version);
                CREATE INDEX IF NOT EXISTS relay_v3_finalizations_by_proof
                    ON relay_v3_finalization_journal(
                        integration_proof_id, expectation_hash, state
                    );
                CREATE INDEX IF NOT EXISTS relay_v3_cleanup_by_run_state
                    ON relay_v3_cleanup_ledger(
                        run_id, state, eligible_after_integration_version
                    );
                CREATE INDEX IF NOT EXISTS relay_v3_scheduler_slots_by_group
                    ON relay_v3_scheduler_writer_slots(run_id, scheduler_id, slot);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO relay_v3_schema_metadata (key, value)
                VALUES (?, ?)
                """,
                ("schema_version", str(self._SCHEMA_VERSION)),
            )
            if self._legacy_schema_version is not None:
                self._migrate_to_schema_v8()
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def _migrate_to_schema_v8(self) -> None:
        """Advance every known legacy Relay schema to V8 in this open operation."""

        connection = self._require_connection()
        legacy_version = self._legacy_schema_version
        if legacy_version not in {"5", "6", "7"}:
            raise RelaySchemaIncompatible()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not self._runs_capacity_is_v8(connection):
                connection.execute(
                    """
                    CREATE TABLE relay_v3_runs_v8 (
                        run_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL UNIQUE,
                        plan_hash TEXT NOT NULL,
                        plan_json TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        input_snapshot_id TEXT NOT NULL,
                        base_commit TEXT NOT NULL,
                        integration_head TEXT NOT NULL,
                        integration_version INTEGER NOT NULL
                            CHECK (integration_version >= 0),
                        capacity INTEGER NOT NULL
                            CHECK (typeof(capacity) = 'integer')
                            CHECK (capacity BETWEEN 1 AND 9),
                        schedule_version INTEGER NOT NULL CHECK (schedule_version >= 0),
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO relay_v3_runs_v8 SELECT * FROM relay_v3_runs"
                )
                connection.execute("DROP TABLE relay_v3_runs")
                connection.execute("ALTER TABLE relay_v3_runs_v8 RENAME TO relay_v3_runs")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RelaySchemaIncompatible()
            result = connection.execute(
                """
                UPDATE relay_v3_schema_metadata SET value = '8'
                WHERE key = ? AND value = ?
                """,
                ("schema_version", legacy_version),
            )
            if result.rowcount != 1:
                raise RelaySchemaIncompatible()
            connection.commit()
            self._legacy_schema_version = None
        except RelaySchemaIncompatible:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise RelaySchemaIncompatible() from error
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _assert_schema_shape(self) -> None:
        connection = self._require_connection()
        try:
            for table, expected_columns in self._SCHEMA_TABLE_INFO.items():
                table_info = tuple(
                    (
                        str(row["name"]),
                        str(row["type"]),
                        int(row["notnull"]),
                        int(row["pk"]),
                    )
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if (
                    table_info != expected_columns
                    or any(
                        row["dflt_value"] is not None
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    )
                    or self._foreign_key_constraints(connection, table)
                    != self._SCHEMA_FOREIGN_KEYS.get(table, frozenset())
                ):
                    raise RelaySchemaIncompatible()
            if (
                not self._runs_capacity_is_v8(connection)
                or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise RelaySchemaIncompatible()
        except RelaySchemaIncompatible:
            raise
        except sqlite3.Error as error:
            raise RelaySchemaIncompatible() from error

    @staticmethod
    def _foreign_key_constraints(
        connection: sqlite3.Connection, table: str
    ) -> frozenset[tuple[tuple[str, ...], str, tuple[str, ...], str, str, str]]:
        constraints: dict[int, list[sqlite3.Row]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
            constraints.setdefault(int(row["id"]), []).append(row)
        return frozenset(
            (
                tuple(str(row["from"]) for row in rows),
                str(rows[0]["table"]),
                tuple(str(row["to"]) for row in rows),
                str(rows[0]["on_update"]),
                str(rows[0]["on_delete"]),
                str(rows[0]["match"]),
            )
            for rows in (
                sorted(group, key=lambda row: int(row["seq"]))
                for group in constraints.values()
            )
        )

    @staticmethod
    def _runs_capacity_is_v8(connection: sqlite3.Connection) -> bool:
        connection.execute("SAVEPOINT relay_v3_schema_capacity_probe")
        try:
            for index, (capacity, accepted) in enumerate(
                ((0, False), (10, False), (0.5, False), (1, True), (9, True))
            ):
                try:
                    connection.execute(
                        """
                        INSERT INTO relay_v3_runs
                            (run_id, workflow_id, plan_hash, plan_json, workspace_id,
                             input_snapshot_id, base_commit, integration_head,
                             integration_version, capacity, schedule_version, created_at)
                        VALUES (?, ?, 'schema-probe', '{}', 'schema-probe',
                                'schema-probe', 'schema-probe', 'schema-probe',
                                0, ?, 0, 'schema-probe')
                        """,
                        (
                            f"relay-v3-schema-capacity-run-{index}",
                            f"relay-v3-schema-capacity-workflow-{index}",
                            capacity,
                        ),
                    )
                except sqlite3.IntegrityError:
                    if accepted:
                        return False
                else:
                    if not accepted:
                        return False
            return True
        finally:
            connection.execute("ROLLBACK TO relay_v3_schema_capacity_probe")
            connection.execute("RELEASE relay_v3_schema_capacity_probe")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        connection = self._require_connection()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            yield cursor
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            cursor.close()

    @contextmanager
    def _recovery_transaction(self) -> Iterator[sqlite3.Cursor]:
        """Probe recovery evidence from a fresh SQLite connection when possible."""

        connection = self._fresh_recovery_connection()
        close_connection = connection is not self._connection
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            yield cursor
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            cursor.close()
            if close_connection:
                connection.close()

    def _fresh_recovery_connection(self) -> sqlite3.Connection:
        if self._database == ":memory:":
            return self._require_connection()
        try:
            connection = sqlite3.connect(self._database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RelayStorageFailure()
        return self._connection


def _clone_json(value: Mapping[str, Any]) -> dict[str, Any]:
    cloned = json.loads(canonical_bytes(value).decode("utf-8"))
    if type(cloned) is not dict:
        raise RelayStorageFailure()
    return cloned


def _encode_json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode_json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if type(decoded) is not dict:
        raise RelayStorageFailure()
    return decoded


def _decode_string_list(value: str) -> list[str]:
    decoded = json.loads(value)
    if type(decoded) is not list or any(type(item) is not str for item in decoded):
        raise RelayStorageFailure()
    return list(decoded)


def _decode_scope_list(value: str) -> list[dict[str, str]]:
    decoded = json.loads(value)
    if type(decoded) is not list or any(
        type(item) is not dict
        or set(item) != {"path", "kind"}
        or type(item["path"]) is not str
        or item["kind"] not in {"file", "tree"}
        for item in decoded
    ):
        raise RelayStorageFailure()
    return [{"path": item["path"], "kind": item["kind"]} for item in decoded]


def _opaque_topology_value(value: object) -> bool:
    """Permit opaque IDs while rejecting raw path-shaped values."""

    if type(value) is not str or not value.strip() or len(value) > 256:
        return False
    if "\x00" in value or "/" in value or "\\" in value:
        return False
    return value not in {".", ".."} and re.match(r"^[A-Za-z]:", value) is None


def _topology_task_scopes(task: Mapping[str, object]) -> list[dict[str, str]]:
    try:
        return _decode_scope_list(_encode_json(task["write_scope"]))
    except (KeyError, RelayStorageFailure, TypeError, ValueError):
        raise RelayTopologyInvalid() from None


def _scope_contains(parent: Mapping[str, str], child: Mapping[str, str]) -> bool:
    if parent["path"] == child["path"]:
        return True
    return parent["kind"] == "tree" and child["path"].startswith(parent["path"] + "/")


def _declared_child_split(
    parent: Mapping[str, object], child: Mapping[str, object]
) -> bool:
    """Allow only a direct child whose scope is a strict subset of its parent."""

    dependencies = child.get("dependencies")
    if type(dependencies) is not list or parent.get("task_id") not in dependencies:
        return False
    parent_scopes = _topology_task_scopes(parent)
    child_scopes = _topology_task_scopes(child)
    return (
        bool(child_scopes)
        and all(
            any(
                _scope_contains(parent_scope, child_scope)
                for parent_scope in parent_scopes
            )
            for child_scope in child_scopes
        )
        and not all(
            any(
                _scope_contains(child_scope, parent_scope)
                for child_scope in child_scopes
            )
            for parent_scope in parent_scopes
        )
    )


def _task_scopes(task: RelayTask) -> list[dict[str, str]]:
    return _decode_scope_list(_encode_json(task.contract["write_scope"]))


def _task_dependencies(task: RelayTask) -> list[str]:
    dependencies = task.contract["dependencies"]
    if type(dependencies) is not list or any(
        type(item) is not str for item in dependencies
    ):
        raise RelayStorageFailure()
    return list(dependencies)


def _task_required_evidence(task: RelayTask) -> list[dict[str, str]]:
    entries = task.contract["required_evidence"]
    if type(entries) is not list or any(
        type(item) is not dict
        or set(item) != {"kind", "selector"}
        or type(item["kind"]) is not str
        or type(item["selector"]) is not str
        for item in entries
    ):
        raise RelayStorageFailure()
    return [{"kind": item["kind"], "selector": item["selector"]} for item in entries]


def _task_route(task: RelayTask) -> dict[str, str]:
    route = task.contract["route"]
    if (
        type(route) is not dict
        or set(route) != {"route_class", "model", "reasoning_effort"}
        or any(type(value) is not str for value in route.values())
    ):
        raise RelayStorageFailure()
    return {str(key): str(value) for key, value in route.items()}


def _scopes_conflict(
    candidate: Sequence[Mapping[str, str]], active: Sequence[Mapping[str, str]]
) -> bool:
    return any(_scopes_overlap(left, right) for left in candidate for right in active)


def _scopes_overlap(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    left_path = left["path"]
    right_path = right["path"]
    if left_path == right_path:
        return True
    if left["kind"] == "tree" and right_path.startswith(left_path + "/"):
        return True
    return right["kind"] == "tree" and left_path.startswith(right_path + "/")


def _queue_for(state: RelayTaskState) -> str:
    if state is RelayTaskState.PREPARED:
        return "prepared_prewarms"
    if state is RelayTaskState.READY:
        return "ready"
    if state in ACTIVE_TASK_STATES:
        return "running_slots"
    if state is RelayTaskState.REVIEW_INTEGRATION:
        return "review_integration"
    return "terminal"


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}-{canonical_hash(value)[7:31]}"


def _safe_branch(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 256:
        return False
    normalized = value.replace("\\", "/")
    return (
        not normalized.startswith("/")
        and not re.match(r"^[A-Za-z]:", normalized)
        and all(part not in {"", ".", ".."} for part in normalized.split("/"))
    )


def _valid_digest_list(value: object) -> bool:
    return (
        type(value) is list
        and len(value) <= 32
        and all(
            type(item) is str and _DIGEST.fullmatch(item) is not None for item in value
        )
        and len(value) == len(set(value))
    )


def _valid_pr_reference(value: object) -> bool:
    return value is None or (
        type(value) is str
        and bool(value.strip())
        and len(value) <= 512
        and "\r" not in value
        and "\n" not in value
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
