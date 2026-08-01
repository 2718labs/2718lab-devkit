"""Typed task and durable acceptance/outbox storage coverage."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temp_support import task_scratch

from orchestrator.models import (
    AtlasOutboxState,
    Task,
    TaskKind,
    TaskState,
    Workflow,
    WorkflowKind,
    WorkflowState,
)
from orchestrator.store import (
    AcceptanceConflictError,
    AtlasOutboxAttemptLimitError,
    AtlasOutboxTransitionError,
    InvalidTaskStateError,
    SQLiteStore,
    StaleLeaseError,
    StoreError,
    StrictIndexError,
    VersionConflictError,
)


class TypedTaskModelTests(unittest.TestCase):
    def test_legacy_task_defaults_to_general_with_empty_typed_fields(self) -> None:
        task = Task("task-1", "workflow-1", "title", "owner")

        self.assertEqual(TaskKind.GENERAL, task.task_kind)
        self.assertEqual("", task.intent_id)
        self.assertEqual("", task.language)
        self.assertEqual("", task.framework)


class TypedTaskStoreTests(unittest.TestCase):
    def test_populated_v3_rows_migrate_to_typed_v4_and_survive_restart(self) -> None:
        scratch_root = task_scratch("orchestrator-typed-tasks")
        with tempfile.TemporaryDirectory(dir=scratch_root) as temporary_directory:
            database = Path(temporary_directory) / "legacy-v3.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_metadata (key, value) VALUES ('schema_version', '3');
                CREATE TABLE workflows (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    product_summary TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    title TEXT NOT NULL, owner_role TEXT NOT NULL, state TEXT NOT NULL,
                    write_scope TEXT NOT NULL, card_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL, version INTEGER NOT NULL
                );
                INSERT INTO workflows VALUES (
                    'workflow-1', 'dag', 'workflow', 'summary', 'running', 0,
                    'policy-1', '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00'
                );
                INSERT INTO tasks VALUES (
                    'legacy-task', 'workflow-1', 'legacy', 'owner', 'new',
                    '[]', '', '', 2
                );
                """
            )
            connection.close()

            store = SQLiteStore(database)
            try:
                self.assertEqual(6, store.schema_version())
                self.assertEqual(
                    {"task_kind", "intent_id", "language", "framework"},
                    {
                        str(row["name"])
                        for row in store._connection.execute("PRAGMA table_info(tasks)")
                        if str(row["name"])
                        in {"task_kind", "intent_id", "language", "framework"}
                    },
                )
                self.assertEqual(
                    Task("legacy-task", "workflow-1", "legacy", "owner", version=2),
                    store.get_task("legacy-task"),
                )
                store.register_task(
                    Task(
                        "code-task",
                        "workflow-1",
                        "code",
                        "owner",
                        task_kind=TaskKind.CODE,
                        intent_id="intent-1",
                        language="python",
                        framework="pytest",
                    )
                )
            finally:
                store.close()

            restarted = SQLiteStore(database)
            try:
                self.assertEqual(6, restarted.schema_version())
                self.assertEqual(
                    Task(
                        "code-task",
                        "workflow-1",
                        "code",
                        "owner",
                        task_kind=TaskKind.CODE,
                        intent_id="intent-1",
                        language="python",
                        framework="pytest",
                    ),
                    restarted.get_task("code-task"),
                )
            finally:
                restarted.close()

    def test_interrupted_v3_migration_rolls_back_and_reopens_cleanly(self) -> None:
        scratch_root = task_scratch("orchestrator-typed-migration-rollback")
        with tempfile.TemporaryDirectory(dir=scratch_root) as temporary_directory:
            database = Path(temporary_directory) / "interrupted-v3.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_metadata (key, value) VALUES ('schema_version', '3');
                CREATE TABLE workflows (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    product_summary TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    title TEXT NOT NULL, owner_role TEXT NOT NULL, state TEXT NOT NULL,
                    write_scope TEXT NOT NULL, card_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL, version INTEGER NOT NULL
                );
                INSERT INTO workflows VALUES (
                    'workflow-1', 'dag', 'workflow', 'summary', 'running', 0,
                    'policy-1', '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00'
                );
                INSERT INTO tasks VALUES (
                    'legacy-task', 'workflow-1', 'legacy', 'owner', 'new',
                    '[]', '', '', 2
                );
                """
            )
            connection.close()
            real_connect = sqlite3.connect
            interrupted_connections: list[sqlite3.Connection] = []

            def connect_with_interrupted_migration(*args, **kwargs):
                interrupted = real_connect(*args, **kwargs)
                interrupted_connections.append(interrupted)

                def deny_schema_version_update(
                    action, table, _column, _database, _trigger
                ):
                    if action == sqlite3.SQLITE_UPDATE and table == "schema_metadata":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                interrupted.set_authorizer(deny_schema_version_update)
                return interrupted

            with mock.patch(
                "orchestrator.store.sqlite3.connect",
                side_effect=connect_with_interrupted_migration,
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    SQLiteStore(database)
            for interrupted in interrupted_connections:
                interrupted.close()

            connection = real_connect(database)
            try:
                schema_version = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                task_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                table_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                connection.close()

            self.assertEqual("3", schema_version)
            self.assertNotIn("task_kind", task_columns)
            self.assertNotIn("code_task_acceptances", table_names)
            self.assertNotIn("atlas_ingestion_outbox", table_names)

            reopened = SQLiteStore(database)
            try:
                self.assertEqual(6, reopened.schema_version())
                self.assertEqual(
                    Task("legacy-task", "workflow-1", "legacy", "owner", version=2),
                    reopened.get_task("legacy-task"),
                )
            finally:
                reopened.close()


class CodeTaskAcceptanceStoreTests(unittest.TestCase):
    _WORKSPACE_ID = "sha256:" + "1" * 64

    def setUp(self) -> None:
        scratch_root = task_scratch("orchestrator-code-acceptance")
        self._temporary_directory = tempfile.TemporaryDirectory(dir=scratch_root)
        database = Path(self._temporary_directory.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(database)
        self.workflow = self.store.create_workflow(
            Workflow(
                "workflow-acceptance",
                WorkflowKind.DAG,
                "workflow",
                "summary",
                WorkflowState.RUNNING,
                0,
                "policy-1",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
            )
        )
        self._acceptance_inputs: dict[str, tuple[str, str, str]] = {}
        self.coordinator = self.store.register_task(
            Task(
                "coordinator-task",
                self.workflow.id,
                "coordinator",
                "sol",
                state=TaskState.RUNNING,
            )
        )
        self.coordinator_owner = "coordinator-owner"
        self.coordinator_lease = self.store.acquire_lease(
            self.coordinator.id,
            self.coordinator_owner,
            "2026-07-29T05:00:00+00:00",
            now="2026-07-29T00:00:00+00:00",
        )
        self.task = self._register_evidenced_task("code-task", intent_id="intent-1")

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _register_evidenced_task(
        self,
        task_id: str,
        intent_id: str,
        *,
        workflow_id: str | None = None,
        task_kind: TaskKind = TaskKind.CODE,
        complete: bool = True,
    ) -> Task:
        accepted_workflow_id = self.workflow.id if workflow_id is None else workflow_id
        input_snapshot_id = f"sha256:input-{task_id}"
        output_snapshot_id = f"sha256:output-{task_id}"
        indexed_diff_hash = f"sha256:diff-{task_id}"
        task = self.store.register_task(
            Task(
                task_id,
                accepted_workflow_id,
                task_id,
                "owner",
                state=TaskState.RUNNING,
                write_scope=(f"src/{task_id}.py",),
                task_kind=task_kind,
                intent_id=intent_id,
                language="python",
                framework="pytest",
            ),
            strict_index=True,
            workspace_id=self._WORKSPACE_ID,
            input_snapshot_id=input_snapshot_id,
            task_node_ids=(f"sha256:task-node-{task_id}",),
            contract_node_ids=(f"sha256:contract-node-{task_id}",),
        )
        owner = f"worker-{task_id}"
        lease = self.store.acquire_lease(
            task.id,
            owner,
            "2026-07-29T04:00:00+00:00",
            now="2026-07-29T00:00:00+00:00",
        )
        self.store.record_checkpoint(
            task.id,
            owner,
            lease.epoch,
            f"sha256:checkpoint-{task_id}",
            now="2026-07-29T00:05:00+00:00",
        )
        self.store.record_output_snapshot(
            task.id,
            owner,
            lease.epoch,
            snapshot_id=output_snapshot_id,
            diff_hash=indexed_diff_hash,
            now="2026-07-29T00:06:00+00:00",
        )
        self.store.record_index_query(
            task.id,
            owner,
            lease.epoch,
            trace_id=f"sha256:query-{task_id}",
            snapshot_id=output_snapshot_id,
            miss_escape_used=False,
            now="2026-07-29T00:07:00+00:00",
        )
        self.store.register_task_artifact(
            task.id,
            owner,
            lease.epoch,
            kind="verification",
            content_hash=f"sha256:verification-{task_id}",
            safe_path=f"evidence/{task_id}.json",
            size=10,
            redaction_version="r1",
            snapshot_id=output_snapshot_id,
            now="2026-07-29T00:08:00+00:00",
        )
        self._acceptance_inputs[task.id] = (
            input_snapshot_id,
            output_snapshot_id,
            indexed_diff_hash,
        )
        if not complete:
            return task
        receipt_ids = tuple(
            sorted(
                (
                    "sha256:" + hashlib.sha256(f"{task_id}:patch".encode()).hexdigest(),
                    "sha256:" + hashlib.sha256(f"{task_id}:shell".encode()).hexdigest(),
                )
            )
        )
        receipt_attestation = self.store.build_code_task_receipt_attestation(
            workflow_id=accepted_workflow_id,
            code_task_id=task.id,
            code_task_version=task.version + 1,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            workspace_hash="sha256:" + "a" * 64,
            execution_receipt_ids=receipt_ids,
        )
        return self.store.complete_task(
            task.id,
            TaskState.DONE,
            task.version,
            owner,
            lease.epoch,
            receipt_attestation=receipt_attestation,
            now="2026-07-29T00:09:00+00:00",
        )

    def _insert_acceptance(
        self,
        *,
        created_at: str,
        task: Task | None = None,
        workflow_id: str | None = None,
        task_version: int | None = None,
        coordinator_task: Task | None = None,
        coordinator_owner: str | None = None,
        coordinator_epoch: int | None = None,
        input_snapshot_id: str | None = None,
        output_snapshot_id: str | None = None,
        indexed_diff_hash: str | None = None,
        intent_id: str | None = None,
        now: str = "2026-07-29T01:00:00+00:00",
    ):
        accepted_task = self.task if task is None else task
        authorized_coordinator = (
            self.coordinator if coordinator_task is None else coordinator_task
        )
        stored_input, stored_output, stored_diff = self._acceptance_inputs[
            accepted_task.id
        ]
        accepted_workflow_id = (
            accepted_task.workflow_id if workflow_id is None else workflow_id
        )
        accepted_task_version = (
            accepted_task.version if task_version is None else task_version
        )
        accepted_coordinator_owner = (
            self.coordinator_owner if coordinator_owner is None else coordinator_owner
        )
        accepted_coordinator_epoch = (
            self.coordinator_lease.epoch
            if coordinator_epoch is None
            else coordinator_epoch
        )
        accepted_input_snapshot_id = (
            stored_input if input_snapshot_id is None else input_snapshot_id
        )
        accepted_output_snapshot_id = (
            stored_output if output_snapshot_id is None else output_snapshot_id
        )
        accepted_indexed_diff_hash = (
            stored_diff if indexed_diff_hash is None else indexed_diff_hash
        )
        accepted_intent_id = accepted_task.intent_id if intent_id is None else intent_id
        receipt_attestation = self.store.code_task_receipt_attestation_for_task(
            accepted_task.id
        )
        receipt_ids = (
            ("sha256:" + "e" * 64, "sha256:" + "f" * 64)
            if receipt_attestation is None
            else receipt_attestation.execution_receipt_ids
        )
        verification_hashes = {"sha256:" + "d" * 64}
        if receipt_attestation is not None:
            verification_hashes.add(receipt_attestation.attestation_hash)
        evidence_binding = self.store.build_code_task_evidence_binding(
            workflow_id=accepted_workflow_id,
            task_id=accepted_task.id,
            task_version=accepted_task_version,
            input_snapshot_id=accepted_input_snapshot_id,
            output_snapshot_id=accepted_output_snapshot_id,
            indexed_diff_hash=accepted_indexed_diff_hash,
            checkpoint_id=f"checkpoint-{accepted_task.id}",
            checkpoint_hash="sha256:" + "c" * 64,
            output_query_trace_id=f"trace-{accepted_task.id}",
            verification_artifact_hashes=tuple(sorted(verification_hashes)),
            execution_receipt_ids=receipt_ids,
        )
        return self.store.insert_code_task_acceptance(
            workflow_id=accepted_workflow_id,
            task_id=accepted_task.id,
            task_version=accepted_task_version,
            coordinator_task_id=authorized_coordinator.id,
            coordinator_owner=accepted_coordinator_owner,
            coordinator_epoch=accepted_coordinator_epoch,
            input_snapshot_id=accepted_input_snapshot_id,
            output_snapshot_id=accepted_output_snapshot_id,
            indexed_diff_hash=accepted_indexed_diff_hash,
            intent_id=accepted_intent_id,
            language=accepted_task.language,
            framework=accepted_task.framework,
            evidence_binding=evidence_binding,
            created_at=created_at,
            now=now,
        )

    def test_acceptance_api_requires_coordinator_lease_identity(self) -> None:
        parameters = inspect.signature(
            SQLiteStore.insert_code_task_acceptance
        ).parameters

        self.assertTrue(
            {
                "coordinator_task_id",
                "coordinator_owner",
                "coordinator_epoch",
                "now",
            }.issubset(parameters)
        )

    def test_general_task_is_rejected_at_the_storage_boundary(self) -> None:
        general = self._register_evidenced_task(
            "general-task",
            intent_id="intent-general",
            task_kind=TaskKind.GENERAL,
        )

        with self.assertRaises(StoreError) as raised:
            self._insert_acceptance(
                task=general,
                created_at="2026-07-29T01:00:00+00:00",
            )

        self.assertEqual("ACCEPTANCE_FORBIDDEN", raised.exception.code)
        self.assertIsNone(self.store.acceptance_for_task(general.id))

    def test_task_workflow_and_version_must_match_durable_state(self) -> None:
        other_workflow = self.store.create_workflow(
            Workflow(
                "workflow-other",
                WorkflowKind.DAG,
                "other",
                "summary",
                WorkflowState.RUNNING,
                0,
                "policy-1",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
            )
        )

        with self.assertRaises(StoreError) as wrong_workflow:
            self._insert_acceptance(
                workflow_id=other_workflow.id,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertEqual("ACCEPTANCE_FORBIDDEN", wrong_workflow.exception.code)

        with self.assertRaises(VersionConflictError):
            self._insert_acceptance(
                task_version=self.task.version - 1,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertIsNone(self.store.acceptance_for_task(self.task.id))

    def test_task_must_be_done_before_storage_acceptance(self) -> None:
        running = self._register_evidenced_task(
            "running-code-task",
            intent_id="intent-running",
            complete=False,
        )

        with self.assertRaises(InvalidTaskStateError) as raised:
            self._insert_acceptance(
                task=running,
                created_at="2026-07-29T01:00:00+00:00",
            )

        self.assertEqual("INVALID_STATE", raised.exception.code)
        self.assertIsNone(self.store.acceptance_for_task(running.id))

    def test_strict_binding_completion_and_payload_identity_are_required(self) -> None:
        unbound = self.store.register_task(
            Task(
                "unbound-code-task",
                self.workflow.id,
                "unbound",
                "owner",
                state=TaskState.DONE,
                task_kind=TaskKind.CODE,
                intent_id="intent-unbound",
                language="python",
                framework="pytest",
            )
        )
        self._acceptance_inputs[unbound.id] = (
            "sha256:input-unbound",
            "sha256:output-unbound",
            "sha256:diff-unbound",
        )
        with self.assertRaises(StrictIndexError) as unavailable:
            self._insert_acceptance(
                task=unbound,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertEqual("INDEX_UNAVAILABLE", unavailable.exception.code)

        incomplete = self.store.register_task(
            Task(
                "incomplete-code-task",
                self.workflow.id,
                "incomplete",
                "owner",
                state=TaskState.DONE,
                write_scope=("src/incomplete.py",),
                task_kind=TaskKind.CODE,
                intent_id="intent-incomplete",
                language="python",
                framework="pytest",
            ),
            strict_index=True,
            workspace_id=self._WORKSPACE_ID,
            input_snapshot_id="sha256:input-incomplete",
            task_node_ids=("sha256:task-node-incomplete",),
        )
        self._acceptance_inputs[incomplete.id] = (
            "sha256:input-incomplete",
            "sha256:output-incomplete",
            "sha256:diff-incomplete",
        )
        with self.assertRaises(StrictIndexError) as incomplete_evidence:
            self._insert_acceptance(
                task=incomplete,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertEqual("CHECKPOINT_REQUIRED", incomplete_evidence.exception.code)

        with self.assertRaises(AcceptanceConflictError):
            self._insert_acceptance(
                input_snapshot_id="sha256:different-input",
                created_at="2026-07-29T01:00:00+00:00",
            )
        with self.assertRaises(AcceptanceConflictError):
            self._insert_acceptance(
                intent_id="intent-different",
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertIsNone(self.store.acceptance_for_task(self.task.id))

    def test_coordinator_must_be_same_workflow_authorized_and_live(self) -> None:
        wrong_role = self.store.register_task(
            Task(
                "worker-coordinator",
                self.workflow.id,
                "worker coordinator",
                "worker",
                state=TaskState.RUNNING,
            )
        )
        wrong_role_lease = self.store.acquire_lease(
            wrong_role.id,
            "worker-coordinator-owner",
            "2026-07-29T05:00:00+00:00",
            now="2026-07-29T00:00:00+00:00",
        )
        with self.assertRaises(StoreError) as forbidden_role:
            self._insert_acceptance(
                coordinator_task=wrong_role,
                coordinator_owner="worker-coordinator-owner",
                coordinator_epoch=wrong_role_lease.epoch,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertEqual("ACCEPTANCE_FORBIDDEN", forbidden_role.exception.code)

        other_workflow = self.store.create_workflow(
            Workflow(
                "coordinator-workflow-other",
                WorkflowKind.DAG,
                "other",
                "summary",
                WorkflowState.RUNNING,
                0,
                "policy-1",
                "2026-07-29T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
            )
        )
        other_coordinator = self.store.register_task(
            Task(
                "other-coordinator",
                other_workflow.id,
                "other coordinator",
                "opus",
                state=TaskState.RUNNING,
            )
        )
        other_lease = self.store.acquire_lease(
            other_coordinator.id,
            "other-owner",
            "2026-07-29T05:00:00+00:00",
            now="2026-07-29T00:00:00+00:00",
        )
        with self.assertRaises(StoreError) as forbidden_workflow:
            self._insert_acceptance(
                coordinator_task=other_coordinator,
                coordinator_owner="other-owner",
                coordinator_epoch=other_lease.epoch,
                created_at="2026-07-29T01:00:00+00:00",
            )
        self.assertEqual("ACCEPTANCE_FORBIDDEN", forbidden_workflow.exception.code)

        with self.assertRaises(StaleLeaseError):
            self._insert_acceptance(
                coordinator_owner="stale-owner",
                created_at="2026-07-29T01:00:00+00:00",
            )
        with self.assertRaises(StaleLeaseError):
            self._insert_acceptance(
                coordinator_epoch=self.coordinator_lease.epoch + 1,
                created_at="2026-07-29T01:00:00+00:00",
            )
        with self.assertRaises(StaleLeaseError):
            self._insert_acceptance(
                created_at="2026-07-29T06:00:00+00:00",
                now="2026-07-29T06:00:00+00:00",
            )
        self.assertIsNone(self.store.acceptance_for_task(self.task.id))

    def test_coordinator_task_must_be_running(self) -> None:
        terminal_coordinator = self.store.register_task(
            Task(
                "terminal-coordinator",
                self.workflow.id,
                "terminal coordinator",
                "sol",
                state=TaskState.DONE,
            )
        )
        terminal_lease = self.store.acquire_lease(
            terminal_coordinator.id,
            "terminal-owner",
            "2026-07-29T05:00:00+00:00",
            now="2026-07-29T00:00:00+00:00",
        )

        with self.assertRaises(StoreError) as raised:
            self._insert_acceptance(
                coordinator_task=terminal_coordinator,
                coordinator_owner="terminal-owner",
                coordinator_epoch=terminal_lease.epoch,
                created_at="2026-07-29T01:00:00+00:00",
            )

        self.assertEqual("ACCEPTANCE_FORBIDDEN", raised.exception.code)
        self.assertIsNone(self.store.acceptance_for_task(self.task.id))

    def test_insert_returns_the_original_pair_for_same_canonical_content(self) -> None:
        acceptance, outbox = self._insert_acceptance(
            created_at="2026-07-29T01:00:00+00:00"
        )

        repeated = self._insert_acceptance(created_at="2026-07-29T02:00:00+00:00")

        self.assertEqual((acceptance, outbox), repeated)
        self.assertEqual(acceptance.acceptance_id, acceptance.payload_hash)
        self.assertEqual(AtlasOutboxState.PENDING, outbox.state)
        self.assertEqual(0, outbox.attempts)
        self.assertEqual("2026-07-29T01:00:00+00:00", acceptance.created_at)
        self.assertEqual("2026-07-29T01:00:00+00:00", outbox.created_at)

    def test_same_task_with_a_different_canonical_payload_is_a_conflict(self) -> None:
        self._insert_acceptance(created_at="2026-07-29T01:00:00+00:00")

        with self.assertRaises(AcceptanceConflictError) as raised:
            self._insert_acceptance(
                created_at="2026-07-29T02:00:00+00:00",
                output_snapshot_id="sha256:" + "d" * 64,
            )

        self.assertEqual("ACCEPTANCE_CONFLICT", raised.exception.code)

    def test_outbox_insert_failure_rolls_back_the_acceptance(self) -> None:
        with mock.patch.object(
            self.store,
            "_insert_atlas_outbox",
            side_effect=sqlite3.OperationalError("injected outbox failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self._insert_acceptance(created_at="2026-07-29T01:00:00+00:00")

        self.assertIsNone(self.store.acceptance_for_task(self.task.id))
        self.assertEqual((), self.store.pending_atlas_outbox(limit=10))

    def test_pending_outbox_lookup_is_ordered_by_creation_then_identity(self) -> None:
        later = self._insert_acceptance(created_at="2026-07-29T03:00:00+00:00")
        earlier_task = self._register_evidenced_task(
            "code-task-earlier", intent_id="intent-2"
        )
        earlier = self._insert_acceptance(
            task=earlier_task,
            created_at="2026-07-29T01:00:00+00:00",
        )

        pending = self.store.pending_atlas_outbox(limit=10)

        self.assertEqual(
            (earlier[1].ingestion_key, later[1].ingestion_key),
            tuple(item.ingestion_key for item in pending),
        )
        projected = self.store.mark_atlas_outbox_state(
            earlier[1].ingestion_key,
            AtlasOutboxState.PROJECTED,
            now="2026-07-29T04:00:00+00:00",
        )
        self.assertEqual(AtlasOutboxState.PROJECTED, projected.state)
        self.assertEqual(earlier[1].ingestion_key, projected.ingestion_key)
        self.assertEqual((later[1],), self.store.pending_atlas_outbox(limit=10))

    def test_terminal_outbox_state_is_immutable_after_pending_transition(self) -> None:
        _, outbox = self._insert_acceptance(created_at="2026-07-29T01:00:00+00:00")

        projected = self.store.mark_atlas_outbox_state(
            outbox.ingestion_key,
            AtlasOutboxState.PROJECTED,
            now="2026-07-29T02:00:00+00:00",
        )

        self.assertEqual(
            projected,
            self.store.mark_atlas_outbox_state(
                outbox.ingestion_key,
                AtlasOutboxState.PROJECTED,
                now="2026-07-29T03:00:00+00:00",
            ),
        )
        with self.assertRaises(AtlasOutboxTransitionError) as raised:
            self.store.mark_atlas_outbox_state(
                outbox.ingestion_key,
                AtlasOutboxState.QUARANTINED,
                error_code="PAYLOAD_CONFLICT",
                now="2026-07-29T04:00:00+00:00",
            )
        self.assertEqual("OUTBOX_TERMINAL", raised.exception.code)

    def test_pending_retry_is_bounded_and_stores_only_safe_codes(self) -> None:
        _, outbox = self._insert_acceptance(created_at="2026-07-29T01:00:00+00:00")

        with self.assertRaises(ValueError):
            self.store.mark_atlas_outbox_state(
                outbox.ingestion_key,
                AtlasOutboxState.PENDING,
                error_code="stdout=fake-secret",
                now="2026-07-29T02:00:00+00:00",
            )

        with mock.patch.object(SQLiteStore, "_MAX_ATLAS_OUTBOX_ATTEMPTS", 1):
            retried = self.store.mark_atlas_outbox_state(
                outbox.ingestion_key,
                AtlasOutboxState.PENDING,
                error_code="SQLITE_BUSY",
                reason_codes=("RETRYABLE",),
                now="2026-07-29T02:00:00+00:00",
            )
            self.assertEqual(1, retried.attempts)
            self.assertEqual("SQLITE_BUSY", retried.last_error_code)
            self.assertEqual(("RETRYABLE",), retried.reason_codes)
            with self.assertRaises(AtlasOutboxAttemptLimitError) as raised:
                self.store.mark_atlas_outbox_state(
                    outbox.ingestion_key,
                    AtlasOutboxState.PENDING,
                    error_code="SQLITE_BUSY",
                    now="2026-07-29T03:00:00+00:00",
                )

        self.assertEqual("OUTBOX_ATTEMPTS_EXHAUSTED", raised.exception.code)

    def test_canonical_payload_excludes_timestamps_state_and_raw_secret_content(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            self._insert_acceptance(
                created_at="2026-07-29T01:00:00+00:00",
                intent_id="intent-1\nstdout=fake-secret",
            )

        acceptance, _ = self._insert_acceptance(created_at="2026-07-29T01:00:00+00:00")
        row = self.store._connection.execute(
            "SELECT payload_json FROM code_task_acceptances WHERE acceptance_id = ?",
            (acceptance.acceptance_id,),
        ).fetchone()
        payload_json = str(row["payload_json"])
        payload = json.loads(payload_json)

        self.assertNotIn("created_at", payload)
        self.assertNotIn("state", payload)
        self.assertNotIn("attempts", payload)
        self.assertNotIn("fake-secret", payload_json)
        self.assertNotIn("stdout", payload_json)
        self.assertNotIn("source", payload_json)


if __name__ == "__main__":
    unittest.main()
