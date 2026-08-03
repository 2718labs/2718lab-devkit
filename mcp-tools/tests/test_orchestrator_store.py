"""Persistence contract tests for the SQLite orchestration store."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import Task, TaskState, Workflow, WorkflowKind
from orchestrator.store import (
    SQLiteStore,
    StaleLeaseError,
    StrictIndexError,
    VersionConflictError,
)


class SQLiteStoreTests(unittest.TestCase):
    _WORKSPACE_ID = "sha256:" + "1" * 64

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "orchestrator.sqlite3"
        self.store = SQLiteStore(self.database)
        self.workflow = Workflow("workflow-1", WorkflowKind.DAG, "Title", "Summary")
        self.store.create_workflow(self.workflow)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _task(self, task_id: str) -> Task:
        return Task(task_id, self.workflow.id, task_id, "terra")

    def test_schema_uses_wal_foreign_keys_and_current_schema_version(self) -> None:
        self.assertEqual(self.store.schema_version(), 9)
        self.assertEqual(self.store.journal_mode(), "wal")
        self.assertTrue(self.store.foreign_keys_enabled())

    def test_strict_binding_persists_only_an_opaque_workspace_authority(self) -> None:
        task = self.store.register_task(
            self._task("strict"),
            strict_index=True,
            workspace_id=self._WORKSPACE_ID,
            input_snapshot_id="sha256:input",
            task_node_ids=("sha256:task-node",),
        )

        binding = self.store.get_index_binding(task.id)
        row = self.store._connection.execute(
            "SELECT workspace_root, workspace_id FROM task_index_bindings WHERE task_id = ?",
            (task.id,),
        ).fetchone()

        self.assertEqual(self._WORKSPACE_ID, binding.workspace_id)
        self.assertFalse(hasattr(binding, "workspace_root"))
        self.assertEqual("", row["workspace_root"])
        self.assertEqual(self._WORKSPACE_ID, row["workspace_id"])

    def test_store_rejects_path_alias_and_invalid_workspace_id_before_writes(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            self.store.register_task(
                self._task("path-alias"),
                strict_index=True,
                workspace_root="D:/workspace",
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        with self.assertRaises(TypeError):
            self.store.register_task(
                self._task("workspace-alias"),
                strict_index=True,
                workspace="D:/workspace",
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        for task_id, workspace_id in (
            ("invalid-id", "D:/workspace"),
            ("path-value", self.database.parent),
        ):
            with self.subTest(workspace_id=workspace_id):
                with self.assertRaises(StrictIndexError) as invalid:
                    self.store.register_task(
                        self._task(task_id),
                        strict_index=True,
                        workspace_id=workspace_id,  # type: ignore[arg-type]
                        input_snapshot_id="sha256:input",
                        task_node_ids=("sha256:task-node",),
                    )
                self.assertEqual("INDEX_UNAVAILABLE", invalid.exception.code)

        for task_id in ("path-alias", "workspace-alias", "invalid-id", "path-value"):
            with self.assertRaises(KeyError):
                self.store.get_task(task_id)
            self.assertIsNone(self.store.get_index_binding(task_id))

    def test_reopen_preserves_workflows_tasks_and_append_only_event_order(self) -> None:
        self.store.register_task(self._task("one"))
        first = self.store.append_event(
            self.workflow.id, "one", "registered", "{}", "hash-1"
        )
        second = self.store.append_event(
            self.workflow.id, "one", "ready", "{}", "hash-2"
        )
        self.store.close()
        self.store = SQLiteStore(self.database)

        self.assertEqual(self.store.get_workflow(self.workflow.id), self.workflow)
        self.assertEqual(self.store.get_task("one"), self._task("one"))
        self.assertEqual(
            [event.sequence for event in self.store.list_events(self.workflow.id)],
            [first.sequence, second.sequence],
        )

    def test_dependencies_are_unique_and_readable(self) -> None:
        self.store.register_task(self._task("one"))
        self.store.register_task(self._task("two"), dependencies=("one", "one"))

        self.assertEqual(self.store.dependencies_for("two"), ("one",))

    def test_artifact_content_hash_is_reused(self) -> None:
        first = self.store.put_artifact("log", "content-hash", "safe/log.txt", 3, "v1")
        second = self.store.put_artifact(
            "log", "content-hash", "safe/other.txt", 99, "v2"
        )

        self.assertEqual(second, first)

    def test_task_version_compare_and_swap_rejects_stale_writer(self) -> None:
        self.store.register_task(self._task("one"))
        updated = self.store.update_task_state(
            "one", TaskState.READY, expected_version=0
        )
        self.assertEqual(updated.version, 1)
        with self.assertRaises(VersionConflictError):
            self.store.update_task_state("one", TaskState.RUNNING, expected_version=0)

    def test_leases_renew_and_take_over_with_monotonic_epochs(self) -> None:
        self.store.register_task(self._task("one"))
        initial = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:10:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        renewed = self.store.renew_lease(
            "one",
            "terra-a",
            initial.epoch,
            "2026-07-24T00:20:00+00:00",
            now="2026-07-24T00:01:00+00:00",
        )
        takeover = self.store.acquire_lease(
            "one",
            "terra-b",
            "2026-07-24T00:30:00+00:00",
            now="2026-07-24T00:21:00+00:00",
        )

        self.assertEqual((initial.epoch, renewed.epoch, takeover.epoch), (1, 1, 2))
        self.assertEqual(takeover.owner, "terra-b")

    def test_same_owner_reclaim_after_expiry_receives_new_epoch(self) -> None:
        self.store.register_task(self._task("one"))
        initial = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        reclaimed = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:03:00+00:00",
            now="2026-07-24T00:02:00+00:00",
        )

        self.assertEqual((initial.epoch, reclaimed.epoch), (1, 2))

    def test_expired_lease_cannot_complete_before_takeover(self) -> None:
        self.store.register_task(self._task("one"))
        lease = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )

        with self.assertRaises(StaleLeaseError) as raised:
            self.store.complete_task(
                "one",
                TaskState.DONE,
                0,
                "terra-a",
                lease.epoch,
                now="2026-07-24T00:02:00+00:00",
            )

        self.assertEqual(raised.exception.code, "STALE_LEASE")
        self.assertEqual(self.store.get_task("one").state, TaskState.NEW)

    def test_stale_owner_receives_stale_lease_code(self) -> None:
        self.store.register_task(self._task("one"))
        first = self.store.acquire_lease(
            "one",
            "terra-a",
            "2026-07-24T00:01:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        takeover = self.store.acquire_lease(
            "one",
            "terra-b",
            "2026-07-24T00:03:00+00:00",
            now="2026-07-24T00:02:00+00:00",
        )

        with self.assertRaises(StaleLeaseError) as raised:
            self.store.complete_task("one", TaskState.DONE, 0, "terra-a", first.epoch)
        self.assertEqual(raised.exception.code, "STALE_LEASE")
        completed = self.store.complete_task(
            "one",
            TaskState.DONE,
            0,
            "terra-b",
            takeover.epoch,
            now="2026-07-24T00:02:30+00:00",
        )
        self.assertEqual(completed.state, TaskState.DONE)


if __name__ == "__main__":
    unittest.main()
