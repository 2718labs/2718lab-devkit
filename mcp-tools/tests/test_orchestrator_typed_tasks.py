"""Typed task and durable acceptance/outbox storage coverage."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import (
    AtlasOutboxState,
    Task,
    TaskKind,
    Workflow,
    WorkflowKind,
    WorkflowState,
)
from orchestrator.store import (
    AcceptanceConflictError,
    AtlasOutboxAttemptLimitError,
    AtlasOutboxTransitionError,
    SQLiteStore,
)
from temp_support import task_scratch


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
                self.assertEqual(4, store.schema_version())
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
                self.assertEqual(4, restarted.schema_version())
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


class CodeTaskAcceptanceStoreTests(unittest.TestCase):
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
        self.task = self.store.register_task(
            Task(
                "code-task",
                self.workflow.id,
                "code task",
                "owner",
                task_kind=TaskKind.CODE,
                intent_id="intent-1",
                language="python",
                framework="pytest",
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def _register_code_task(self, task_id: str, intent_id: str) -> Task:
        return self.store.register_task(
            Task(
                task_id,
                self.workflow.id,
                task_id,
                "owner",
                task_kind=TaskKind.CODE,
                intent_id=intent_id,
                language="python",
                framework="pytest",
            )
        )

    def _insert_acceptance(
        self,
        *,
        created_at: str,
        task: Task | None = None,
        output_snapshot_id: str = "sha256:" + "b" * 64,
        intent_id: str | None = None,
    ):
        accepted_task = self.task if task is None else task
        return self.store.insert_code_task_acceptance(
            workflow_id=self.workflow.id,
            task_id=accepted_task.id,
            task_version=accepted_task.version,
            input_snapshot_id="sha256:" + "a" * 64,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash="sha256:" + "c" * 64,
            intent_id=accepted_task.intent_id if intent_id is None else intent_id,
            language=accepted_task.language,
            framework=accepted_task.framework,
            created_at=created_at,
        )

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
        earlier_task = self._register_code_task("code-task-earlier", "intent-2")
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
