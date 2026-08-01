"""SQLite transactions for Relay v3's durable scheduling state.

The store never invokes a Codex host API.  It persists only host actions and
their lifecycle evidence, while the host itself performs any agent spawn.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

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


class RelayStore:
    """Own the atomic durable state behind Relay's five public tools."""

    _SCHEMA_VERSION = 3
    _MAX_IDEMPOTENCY_KEY_LENGTH = 256

    def __init__(self, database: str | Path) -> None:
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            str(database), isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

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
                         input_snapshot_id, base_commit, capacity, schedule_version,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        run_id,
                        workflow_id,
                        str(plan_data["plan_hash"]),
                        _encode_json(plan_data),
                        str(binding["workspace_id"]),
                        str(binding["input_snapshot_id"]),
                        str(plan_data["base_commit"]),
                        int(plan_data["capacity"]),
                        now,
                    ),
                )
                ready_ids = {str(item) for item in plan_data["queues"]["ready"]}
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
                if candidate_data["base_commit"] != run.base_commit:
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
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', NULL,
                            NULL, ?, ?)
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
                    candidate_id=candidate_data["candidate_id"],
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

    def integrate_candidate(
        self,
        workflow_id: str,
        task_id: str,
        *,
        epoch: int,
        expected_task_version: int,
        candidate_id: str,
        integration_head: str,
        integration_commit: str,
    ) -> dict[str, object]:
        """Record only Sol's reviewed matching-base integration decision."""

        if (
            _COMMIT.fullmatch(integration_head) is None
            or _COMMIT.fullmatch(integration_commit) is None
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
                if str(candidate["status"]) != "reviewed":
                    raise RelayStateStale()
                if str(candidate["base_commit"]) != integration_head:
                    raise RelayStaleBase()
                cursor.execute(
                    """
                    UPDATE relay_v3_candidates
                    SET status = 'integrated', integration_commit = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (integration_commit, now, candidate_id),
                )
                cursor.execute(
                    """
                    UPDATE relay_v3_tasks
                    SET state = ?, task_version = task_version + 1, scope_owner = NULL
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (RelayTaskState.INTEGRATED.value, run.run_id, task.task_id),
                )
                self._promote_dependency_safe_tasks(cursor, run.run_id)
                run = self._increment_schedule_version(cursor, run.run_id)
                self._refresh_directives(cursor, run, now=now)
                return self._mutation_result(
                    cursor, run, task.task_id, candidate_id=candidate_id
                )
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
        return {
            "schema": "2718lab-devkit/relay-status-v1",
            "workflow_id": workflow_id,
            "run": run.to_dict(),
            "schedule_version": run.schedule_version,
            "tasks": [task.to_dict() for task in tasks],
            "leases": leases,
            "candidates": candidates,
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
            "base_commit": run.base_commit,
            "worktree_suffix": f"worktrees/{suffix}",
            "temporary_root_suffix": f"contexts/{suffix}",
        }

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
        active_count = int(
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM relay_v3_tasks
                WHERE run_id = ? AND state IN (?, ?)
                """,
                (
                    run.run_id,
                    RelayTaskState.LEASED.value,
                    RelayTaskState.RUNNING.value,
                ),
            ).fetchone()["count"]
        )
        remaining = max(0, run.capacity - active_count)
        if remaining == 0:
            return ()
        held_scopes = self._held_writer_scopes(cursor, run.run_id)
        selected: list[RelayTask] = []
        selected_scopes: list[dict[str, str]] = []
        for task in self._dispatchable_tasks(cursor, run.run_id):
            if task.kind == "implementation":
                scopes = _task_scopes(task)
                if _scopes_conflict(scopes, [*held_scopes, *selected_scopes]):
                    continue
                selected_scopes.extend(scopes)
            selected.append(task)
            if len(selected) == remaining:
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
        return tuple(self._task_from_row(row) for row in rows)

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
        context: dict[str, object] = {
            "route": _task_route(task),
            "task_contract": task.task_contract(),
        }
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

    def _require_evidence(
        self,
        cursor: sqlite3.Cursor,
        run: RelayRun,
        task: RelayTask,
        epoch: int,
        candidate: Mapping[str, object],
    ) -> None:
        self._require_evidence_hashes(
            cursor, run, task, epoch, list(candidate["evidence_hashes"])
        )

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
        if type(value) is not dict or set(value) != expected:
            raise RelayCandidateError()
        candidate_id = value["candidate_id"]
        branch = value["branch"]
        base_commit = value["base_commit"]
        head_commit = value["head_commit"]
        diff_hash = value["diff_hash"]
        evidence_hashes = value["evidence_hashes"]
        pr_reference = value["pr_reference"]
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

    def _run_for_workflow(self, cursor: sqlite3.Cursor, workflow_id: str) -> RelayRun:
        row = self._run_row_for_workflow(cursor, workflow_id)
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
        self, cursor: sqlite3.Cursor, run_id: str, task_id: str
    ) -> RelayTask:
        row = cursor.execute(
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
        return RelayRun(
            run_id=str(row["run_id"]),
            workflow_id=str(row["workflow_id"]),
            plan_hash=str(row["plan_hash"]),
            workspace_id=str(row["workspace_id"]),
            input_snapshot_id=str(row["input_snapshot_id"]),
            base_commit=str(row["base_commit"]),
            capacity=int(row["capacity"]),
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
                    capacity INTEGER NOT NULL CHECK (capacity >= 1),
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, task_id),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES relay_v3_tasks(run_id, task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS relay_v3_tasks_by_state
                    ON relay_v3_tasks(run_id, state, priority DESC, ordinal, task_id);
                CREATE INDEX IF NOT EXISTS relay_v3_directives_by_state
                    ON relay_v3_directives(run_id, state, expected_schedule_version);
                """
            )
            connection.execute(
                """
                INSERT INTO relay_v3_schema_metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("schema_version", str(self._SCHEMA_VERSION)),
            )
        except sqlite3.Error as error:
            raise RelayStorageFailure() from error

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
