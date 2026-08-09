"""Service-facing transaction API coverage for the orchestration store."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temp_support import task_scratch

from orchestrator.models import Task, TaskState, Workflow, WorkflowKind, WorkflowState
from orchestrator.store import (
    ArtifactConflictError,
    CardHashMismatchError,
    LeaseConflictError,
    SQLiteStore,
    StrictIndexError,
    VersionConflictError,
    WorkflowCancelledError,
)


def _replace_schema_metadata_with_legacy_version(
    connection: sqlite3.Connection,
    version: str,
) -> None:
    connection.execute("DROP TABLE schema_metadata")
    connection.execute(
        """
        CREATE TABLE schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
        ("schema_version", version),
    )


class SQLiteStoreServiceApiTests(unittest.TestCase):
    _WORKSPACE_ID = "sha256:" + "1" * 64

    def setUp(self) -> None:
        scratch_root = task_scratch("orchestrator-store")
        self._temporary_directory = tempfile.TemporaryDirectory(dir=scratch_root)
        self._database = Path(self._temporary_directory.name) / "orchestrator.sqlite"
        self.store = SQLiteStore(self._database)
        self.workflow = self.store.create_workflow(
            Workflow(
                "workflow-1",
                WorkflowKind.DAG,
                "workflow",
                "summary",
                WorkflowState.RUNNING,
                0,
                "policy-1",
                "2026-07-24T00:00:00+00:00",
                "2026-07-24T00:00:00+00:00",
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _register_task(self, task_id: str, state: TaskState = TaskState.NEW) -> Task:
        return self.store.register_task(
            Task(task_id, self.workflow.id, task_id, "owner", state=state)
        )

    def _seed_v5_strict_database(self, name: str) -> Path:
        database = Path(self._temporary_directory.name) / name
        seeded = SQLiteStore(database)
        try:
            seeded.create_workflow(
                Workflow(
                    "legacy-v5-workflow",
                    WorkflowKind.DAG,
                    "legacy",
                    "summary",
                    WorkflowState.RUNNING,
                )
            )
            seeded.register_task(
                Task(
                    "legacy-v5-task",
                    "legacy-v5-workflow",
                    "legacy strict task",
                    "worker",
                    state=TaskState.READY,
                ),
                strict_index=True,
                workspace_id=self._WORKSPACE_ID,
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        finally:
            seeded.close()

        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE task_index_bindings SET workspace_root = ?",
                ("D:/legacy-secret-workspace",),
            )
            connection.execute(
                "ALTER TABLE task_index_bindings DROP COLUMN workspace_id"
            )
            _replace_schema_metadata_with_legacy_version(connection, "5")
            connection.commit()
        finally:
            connection.close()
        return database

    def test_cancel_workflow_cancels_only_nonterminal_tasks_atomically(self) -> None:
        pending = self._register_task("pending", TaskState.RUNNING)
        completed = self._register_task("completed", TaskState.DONE)

        workflow, tasks = self.store.cancel_workflow(
            self.workflow.id, expected_version=self.workflow.version
        )

        self.assertEqual(workflow.state, WorkflowState.CANCELLED)
        self.assertEqual(self.store.get_task(pending.id).state, TaskState.CANCELLED)
        self.assertEqual(self.store.get_task(completed.id).state, TaskState.DONE)
        self.assertEqual(tuple(task.id for task in tasks), (pending.id, completed.id))
        again, _ = self.store.cancel_workflow(self.workflow.id)
        self.assertEqual(again.version, workflow.version)
        with self.assertRaises(VersionConflictError):
            self.store.cancel_workflow(
                self.workflow.id, expected_version=self.workflow.version
            )

    def test_complete_task_rejects_a_cancelled_workflow(self) -> None:
        task = self._register_task("running", TaskState.RUNNING)
        lease = self.store.acquire_lease(
            task.id,
            "owner",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        self.store.cancel_workflow(self.workflow.id)

        with self.assertRaises(WorkflowCancelledError) as raised:
            self.store.complete_task(
                task.id,
                TaskState.DONE,
                task.version,
                "owner",
                lease.epoch,
                now="2026-07-24T00:01:00+00:00",
            )
        self.assertEqual(raised.exception.code, "WORKFLOW_CANCELLED")

    def test_artifacts_are_retrievable_and_content_hash_deduplicated(self) -> None:
        artifact = self.store.put_artifact(
            "result", "sha256:one", "artifacts/one", 12, "redacted-v1"
        )

        self.assertEqual(self.store.get_artifact(artifact.content_hash), artifact)
        self.assertIsNone(self.store.get_artifact("sha256:missing"))
        self.assertEqual(
            self.store.put_artifact(
                "result", "sha256:one", "artifacts/other", 12, "redacted-v2"
            ),
            artifact,
        )

    def test_completed_task_lookup_uses_durable_input_and_workflow_policy(self) -> None:
        first = self._register_task("first", TaskState.DONE)
        self.store.record_task_input(first.id, "sha256:input")

        self.assertEqual(
            self.store.find_completed_task(
                self.workflow.id, input_hash="sha256:input", policy_version="policy-1"
            ),
            first,
        )
        self.assertIsNone(
            self.store.find_completed_task(
                self.workflow.id, input_hash="sha256:input", policy_version="policy-2"
            )
        )

    def test_complete_task_cas_updates_the_result_hash(self) -> None:
        task = self._register_task("complete", TaskState.RUNNING)
        lease = self.store.acquire_lease(
            task.id,
            "owner",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        completed = self.store.complete_task(
            task.id,
            TaskState.DONE,
            task.version,
            "owner",
            lease.epoch,
            result_hash="sha256:result",
            now="2026-07-24T00:01:00+00:00",
        )

        self.assertEqual(completed.result_hash, "sha256:result")
        self.assertEqual(completed.state, TaskState.DONE)

    def test_promote_ready_tasks_only_returns_the_new_ready_wave(self) -> None:
        dependency = self._register_task("dependency", TaskState.DONE)
        downstream = Task("downstream", self.workflow.id, "downstream", "owner")
        self.store.register_task(downstream, dependencies=(dependency.id,))
        blocked = Task("blocked", self.workflow.id, "blocked", "owner")
        self.store.register_task(blocked, dependencies=(downstream.id,))

        self.assertEqual(
            self.store.promote_ready_tasks(self.workflow.id),
            (self.store.get_task(downstream.id),),
        )
        self.assertEqual(self.store.promote_ready_tasks(self.workflow.id), ())
        self.assertEqual(self.store.get_task(blocked.id).state, TaskState.NEW)

    def test_claim_task_atomically_starts_a_ready_task(self) -> None:
        task = self._register_task("ready", TaskState.READY)

        claimed, lease = self.store.claim_task(
            task.id,
            "worker",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        self.assertEqual(claimed.state, TaskState.RUNNING)
        self.assertEqual(lease.task_id, task.id)
        with self.assertRaises(LeaseConflictError) as raised:
            self.store.claim_task(
                task.id,
                "worker",
                "2026-07-24T01:00:00+00:00",
                now="2026-07-24T00:01:00+00:00",
            )
        self.assertEqual(raised.exception.code, "LEASE_HELD")

    def test_claim_binds_canonical_host_target_to_current_lease(self) -> None:
        task = self._register_task("host-bound", TaskState.READY)

        claimed, lease = self.store.claim_task(
            task.id,
            "worker",
            "2026-07-24T01:00:00+00:00",
            host_target="/root/receiver",
            now="2026-07-24T00:00:00+00:00",
        )

        self.assertEqual(TaskState.RUNNING, claimed.state)
        self.assertEqual("/root/receiver", lease.host_target)
        self.assertTrue(
            self.store.is_task_online(task.id, now="2026-07-24T00:30:00+00:00")
        )
        self.store.close()
        self.store = SQLiteStore(self._database)
        self.assertEqual("/root/receiver", self.store.get_lease(task.id).host_target)

    def test_claim_accepts_host_agent_id_target(self) -> None:
        task = self._register_task("host-agent-id", TaskState.READY)
        host_agent_id = "019f9536-f7e5-7c01-8fff-e1d15fbc0ddd"

        _, lease = self.store.claim_task(
            task.id,
            "worker",
            "2026-07-24T01:00:00+00:00",
            host_target=host_agent_id,
            now="2026-07-24T00:00:00+00:00",
        )

        self.assertEqual(host_agent_id, lease.host_target)
        self.assertTrue(
            self.store.is_task_online(task.id, now="2026-07-24T00:30:00+00:00")
        )

    def test_active_lease_without_host_target_is_offline(self) -> None:
        task = self._register_task("mailbox-only", TaskState.READY)

        _, lease = self.store.claim_task(
            task.id,
            "worker",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        self.assertIsNone(lease.host_target)
        self.assertFalse(
            self.store.is_task_online(task.id, now="2026-07-24T00:30:00+00:00")
        )

    def test_expired_lease_takeover_does_not_reuse_previous_host_target(self) -> None:
        task = self._register_task("host-takeover", TaskState.READY)
        _, original = self.store.claim_task(
            task.id,
            "first",
            "2026-07-24T00:01:00+00:00",
            host_target="/root/first",
            now="2026-07-24T00:00:00+00:00",
        )

        _, replacement = self.store.claim_task(
            task.id,
            "second",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:02:00+00:00",
        )

        self.assertGreater(replacement.epoch, original.epoch)
        self.assertIsNone(replacement.host_target)
        self.assertFalse(
            self.store.is_task_online(task.id, now="2026-07-24T00:03:00+00:00")
        )

    def test_version_one_database_migrates_without_treating_legacy_lease_as_online(
        self,
    ) -> None:
        legacy_database = Path(self._temporary_directory.name) / "legacy-v1.sqlite"
        connection = sqlite3.connect(legacy_database)
        connection.executescript(
            """
            CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_metadata (key, value) VALUES ('schema_version', '1');
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
            CREATE TABLE lease_epochs (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), epoch INTEGER NOT NULL
            );
            CREATE TABLE leases (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), owner TEXT NOT NULL,
                epoch INTEGER NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
            );
            INSERT INTO workflows VALUES (
                'legacy-workflow', 'dag', 'legacy', 'summary', 'running', 0,
                'policy-1', '2026-07-24T00:00:00+00:00', '2026-07-24T00:00:00+00:00'
            );
            INSERT INTO tasks VALUES (
                'legacy-task', 'legacy-workflow', 'legacy', 'worker', 'running',
                '[]', '', '', 1
            );
            INSERT INTO lease_epochs VALUES ('legacy-task', 1);
            INSERT INTO leases VALUES (
                'legacy-task', 'legacy-owner', 1,
                '2026-07-24T01:00:00+00:00', '2026-07-24T00:00:00+00:00'
            );
            """
        )
        connection.close()

        migrated = SQLiteStore(legacy_database)
        try:
            lease = migrated.get_lease("legacy-task")
            self.assertGreaterEqual(migrated.schema_version(), 2)
            self.assertIsNone(lease.host_target)
            self.assertFalse(
                migrated.is_task_online("legacy-task", now="2026-07-24T00:30:00+00:00")
            )
        finally:
            migrated.close()

    def test_v4_database_additively_migrates_receipt_trust_tables_and_keeps_data(
        self,
    ) -> None:
        legacy_database = Path(self._temporary_directory.name) / "legacy-v4.sqlite"
        seeded = SQLiteStore(legacy_database)
        try:
            seeded.create_workflow(
                Workflow(
                    "legacy-v4-workflow",
                    WorkflowKind.DAG,
                    "legacy",
                    "summary",
                    WorkflowState.RUNNING,
                )
            )
            seeded.register_task(
                Task(
                    "legacy-v4-task",
                    "legacy-v4-workflow",
                    "legacy task",
                    "worker",
                )
            )
        finally:
            seeded.close()

        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute("DROP TABLE IF EXISTS code_task_receipt_owners")
            connection.execute("DROP TABLE IF EXISTS code_task_receipt_attestations")
            _replace_schema_metadata_with_legacy_version(connection, "4")
            connection.commit()
        finally:
            connection.close()

        migrated = SQLiteStore(legacy_database)
        try:
            self.assertEqual(12, migrated.schema_version())
            table_names = {
                str(row["name"])
                for row in migrated._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("code_task_receipt_attestations", table_names)
            self.assertIn("code_task_receipt_owners", table_names)
            self.assertEqual("legacy task", migrated.get_task("legacy-v4-task").title)
        finally:
            migrated.close()

    def test_v5_strict_rows_migrate_unbound_without_path_promotion_and_fail_closed(
        self,
    ) -> None:
        legacy_database = self._seed_v5_strict_database("legacy-v5.sqlite")

        migrated = SQLiteStore(legacy_database)
        try:
            columns = {
                str(row["name"])
                for row in migrated._connection.execute(
                    "PRAGMA table_info(task_index_bindings)"
                )
            }
            row = migrated._connection.execute(
                "SELECT workspace_root, workspace_id FROM task_index_bindings WHERE task_id = ?",
                ("legacy-v5-task",),
            ).fetchone()
            before = migrated.get_task("legacy-v5-task")
            event_count = migrated._connection.execute(
                "SELECT COUNT(*) FROM events WHERE task_id = ?", ("legacy-v5-task",)
            ).fetchone()[0]

            self.assertEqual(12, migrated.schema_version())
            self.assertIn("workspace_id", columns)
            self.assertEqual("D:/legacy-secret-workspace", row["workspace_root"])
            self.assertEqual("", row["workspace_id"])
            with self.assertRaises(StrictIndexError) as unreadable:
                migrated.get_index_binding("legacy-v5-task")
            self.assertEqual("INDEX_UNAVAILABLE", unreadable.exception.code)
            with self.assertRaises(StrictIndexError) as unclaimable:
                migrated.claim_task(
                    "legacy-v5-task",
                    "owner",
                    "2099-01-01T00:00:00+00:00",
                )
            self.assertEqual("INDEX_UNAVAILABLE", unclaimable.exception.code)
            self.assertEqual(before, migrated.get_task("legacy-v5-task"))
            self.assertIsNone(migrated.get_lease("legacy-v5-task"))
            self.assertEqual(
                event_count,
                migrated._connection.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id = ?",
                    ("legacy-v5-task",),
                ).fetchone()[0],
            )
        finally:
            migrated.close()

    def test_interrupted_v5_workspace_id_migration_rolls_back_column_and_version(
        self,
    ) -> None:
        legacy_database = self._seed_v5_strict_database("interrupted-v5.sqlite")
        real_connect = sqlite3.connect
        interrupted_connections: list[sqlite3.Connection] = []

        def connect_with_interrupted_migration(*args, **kwargs):
            interrupted = real_connect(*args, **kwargs)
            interrupted_connections.append(interrupted)

            def deny_schema_version_update(action, table, _column, _database, _trigger):
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
                SQLiteStore(legacy_database)
        for interrupted in interrupted_connections:
            interrupted.close()

        connection = real_connect(legacy_database)
        try:
            schema_version = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(task_index_bindings)"
                ).fetchall()
            }
        finally:
            connection.close()

        self.assertEqual("5", schema_version)
        self.assertNotIn("workspace_id", columns)

        reopened = SQLiteStore(legacy_database)
        try:
            self.assertEqual(12, reopened.schema_version())
            with self.assertRaises(StrictIndexError):
                reopened.get_index_binding("legacy-v5-task")
        finally:
            reopened.close()

    def test_claim_running_task_requires_expired_lease_for_takeover(self) -> None:
        task = self._register_task("running-claim", TaskState.RUNNING)
        original = self.store.acquire_lease(
            task.id,
            "first",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        with self.assertRaises(LeaseConflictError) as raised:
            self.store.claim_task(
                task.id,
                "second",
                "2026-07-24T01:00:00+00:00",
                now="2026-07-24T00:01:00+00:00",
            )
        self.assertEqual(raised.exception.code, "LEASE_HELD")

        claimed, replacement = self.store.claim_task(
            task.id,
            "second",
            "2026-07-24T02:00:00+00:00",
            now="2026-07-24T01:01:00+00:00",
        )
        self.assertEqual(claimed.state, TaskState.RUNNING)
        self.assertGreater(replacement.epoch, original.epoch)

    def test_write_scope_conflicts_returns_nonterminal_pairs_in_task_id_order(
        self,
    ) -> None:
        self.store.register_task(
            Task(
                "task-z",
                self.workflow.id,
                "z",
                "owner",
                TaskState.RUNNING,
                write_scope=("store.py",),
            )
        )
        self.store.register_task(
            Task(
                "task-a",
                self.workflow.id,
                "a",
                "owner",
                TaskState.READY,
                write_scope=("store.py", "models.py"),
            )
        )
        self.store.register_task(
            Task(
                "task-done",
                self.workflow.id,
                "done",
                "owner",
                TaskState.DONE,
                write_scope=("store.py",),
            )
        )

        self.assertEqual(
            self.store.write_scope_conflicts(self.workflow.id),
            (("task-a", "task-z", ("store.py",)),),
        )

    def test_register_task_artifact_is_lease_scoped_and_owner_idempotent(self) -> None:
        task = self._register_task("artifact-owner", TaskState.RUNNING)
        lease = self.store.acquire_lease(
            task.id,
            "owner",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        artifact = self.store.register_task_artifact(
            task.id,
            "owner",
            lease.epoch,
            kind="result",
            content_hash="sha256:owned",
            safe_path="artifacts/owned",
            size=20,
            redaction_version="redacted-v1",
            now="2026-07-24T00:01:00+00:00",
        )

        self.assertEqual(
            self.store.register_task_artifact(
                task.id,
                "owner",
                lease.epoch,
                kind="result",
                content_hash="sha256:owned",
                safe_path="artifacts/owned",
                size=20,
                redaction_version="redacted-v1",
                now="2026-07-24T00:02:00+00:00",
            ),
            artifact,
        )
        other = self._register_task("other-owner", TaskState.RUNNING)
        other_lease = self.store.acquire_lease(
            other.id,
            "other",
            "2026-07-24T01:00:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        with self.assertRaises(ArtifactConflictError):
            self.store.register_task_artifact(
                other.id,
                "other",
                other_lease.epoch,
                kind="result",
                content_hash="sha256:owned",
                safe_path="artifacts/owned",
                size=20,
                redaction_version="redacted-v1",
                now="2026-07-24T00:01:00+00:00",
            )

    def test_task_card_is_hash_bound_and_survives_store_reopen(self) -> None:
        card_body = "# Task card\n\nRead only this task scope."
        card_hash = f"sha256:{hashlib.sha256(card_body.encode('utf-8')).hexdigest()}"
        task = Task(
            "card-owner",
            self.workflow.id,
            "card owner",
            "owner",
            card_hash=card_hash,
        )

        self.store.register_task(task, card_body=card_body)
        self.store.close()
        self.store = SQLiteStore(self._database)

        self.assertEqual(self.store.get_task_card(task.id), card_body)
        with self.assertRaises(KeyError):
            self.store.get_task_card("missing-task")
        self._register_task("without-card")
        with self.assertRaises(KeyError):
            self.store.get_task_card("without-card")

    def test_task_card_rejects_a_body_that_does_not_match_the_task_hash(self) -> None:
        task = Task(
            "mismatched-card",
            self.workflow.id,
            "mismatched card",
            "owner",
            card_hash="sha256:not-the-card-body",
        )

        with self.assertRaises(CardHashMismatchError) as raised:
            self.store.register_task(task, card_body="the actual body")
        self.assertEqual(raised.exception.code, "CARD_HASH_MISMATCH")
        with self.assertRaises(KeyError):
            self.store.get_task(task.id)

    def test_task_context_requirements_survive_reopen_and_are_task_scoped(self) -> None:
        own = Task("projection-owner", self.workflow.id, "owner", "owner")
        sibling = Task("projection-sibling", self.workflow.id, "sibling", "owner")
        self.store.register_task(
            own,
            contract_subscriptions=("sha256:own-contract",),
            required_evidence=("own proof",),
        )
        self.store.register_task(
            sibling,
            contract_subscriptions=("sha256:sibling-contract",),
            required_evidence=("sibling proof",),
        )

        self.store.close()
        self.store = SQLiteStore(self._database)
        projection = self.store.get_task_context_requirements(own.id)

        self.assertEqual(("sha256:own-contract",), projection.direct_contract_hashes)
        self.assertEqual(("own proof",), projection.required_evidence)
        self.assertNotIn("sibling", repr(projection))


if __name__ == "__main__":
    unittest.main()
