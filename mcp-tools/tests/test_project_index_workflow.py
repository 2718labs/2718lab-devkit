"""Strict index workflow integration tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from temp_support import task_scratch

from orchestrator.models import Task, Workflow, WorkflowKind
from orchestrator.service import OrchestratorService, ServiceError
from orchestrator.store import SQLiteStore
from project_index import IndexError, IndexSnapshot, IndexState
from project_index.checkpoints import WorkspaceOwnership


class _IndexStub:
    def __init__(self, workspace_id: str, snapshot_id: str) -> None:
        self.workspace_id = workspace_id
        self.snapshot_id = snapshot_id
        self.assertions: list[tuple[str, str, tuple[str, ...]]] = []

    def assert_current(
        self,
        workspace_id: str,
        snapshot_id: str,
        required_paths: tuple[str, ...] | None = None,
    ) -> IndexSnapshot:
        scope = tuple(required_paths or ())
        self.assertions.append((workspace_id, snapshot_id, scope))
        if workspace_id != self.workspace_id or snapshot_id != self.snapshot_id:
            raise IndexError("INDEX_STALE", "snapshot is not current")
        return IndexSnapshot(
            snapshot_id,
            "",
            IndexState.INDEX_READY,
            1,
            1,
            0,
            1,
            0,
            0,
            "sha256:manifest",
            "sha256:parsers",
            None,
            workspace_id,
        )


class StrictProjectIndexWorkflowTests(unittest.TestCase):
    _WORKSPACE_ID = "sha256:" + "1" * 64

    def setUp(self) -> None:
        scratch = task_scratch("strict-project-index")
        self.directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "orchestrator.sqlite3"
        self.store = SQLiteStore(self.database)
        self.addCleanup(self.store.close)
        self.index = _IndexStub(self._WORKSPACE_ID, "sha256:input")
        self.service = OrchestratorService(self.store, index_service=self.index)
        self.service.create_workflow(
            Workflow("wf", WorkflowKind.DAG, "title", "summary")
        )

    def _register(
        self,
        task_id: str,
        *,
        write_scope: tuple[str, ...] = (),
        strict_index: bool = True,
    ) -> Task:
        task = Task(task_id, "wf", task_id, "writer", write_scope=write_scope)
        return self.service.register_task(
            task,
            card=f"card for {task_id}",
            strict_index=strict_index,
            workspace_id=self._WORKSPACE_ID,
            input_snapshot_id="sha256:input",
            task_node_ids=("sha256:task-node",),
            contract_node_ids=("sha256:contract-node",),
        )

    def _claim(self, task_id: str) -> tuple[Task, object]:
        self.service.ready_wave("wf")
        return self.service.claim_task(
            task_id,
            "owner",
            expires_at="2099-01-01T00:00:00+00:00",
        )

    def test_schema_v13_and_legacy_tasks_remain_unbound(self) -> None:
        self.assertEqual(13, self.store.schema_version())
        legacy = self.service.register_task(
            Task("legacy", "wf", "legacy", "writer"), card="legacy"
        )

        self.assertIsNone(self.store.get_index_binding(legacy.id))

    def test_strict_registration_rejects_path_aliases_without_writes(self) -> None:
        with self.assertRaises(TypeError):
            self.service.register_task(
                Task("path-alias", "wf", "path alias", "writer"),
                card="path alias",
                strict_index=True,
                workspace_root=str(self.workspace),
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )
        with self.assertRaises(TypeError):
            self.service.register_task(
                Task("workspace-alias", "wf", "workspace alias", "writer"),
                card="workspace alias",
                strict_index=True,
                workspace=str(self.workspace),
                input_snapshot_id="sha256:input",
                task_node_ids=("sha256:task-node",),
            )

        for task_id in ("path-alias", "workspace-alias"):
            with self.assertRaises(KeyError):
                self.store.get_task(task_id)
            self.assertIsNone(self.store.get_index_binding(task_id))

    def test_strict_registration_rejects_invalid_opaque_ids_without_writes(
        self,
    ) -> None:
        for suffix, workspace_id in (
            ("empty", ""),
            ("path", str(self.workspace)),
            ("pathlike", self.workspace),
        ):
            with self.subTest(workspace_id=workspace_id):
                task_id = f"invalid-{suffix}"
                with self.assertRaises(ServiceError) as rejected:
                    self.service.register_task(
                        Task(task_id, "wf", task_id, "writer"),
                        card=task_id,
                        strict_index=True,
                        workspace_id=workspace_id,  # type: ignore[arg-type]
                        input_snapshot_id="sha256:input",
                        task_node_ids=("sha256:task-node",),
                    )
                self.assertEqual("INDEX_UNAVAILABLE", rejected.exception.code)
                with self.assertRaises(KeyError):
                    self.store.get_task(task_id)
                self.assertIsNone(self.store.get_index_binding(task_id))

    def test_strict_ownership_exposes_only_the_opaque_workspace_id(self) -> None:
        self._register("owned")
        _, lease = self._claim("owned")

        ownership = self.service.strict_ownership(
            "wf", "owned", owner="owner", epoch=lease.epoch
        )

        self.assertIsInstance(ownership, WorkspaceOwnership)
        self.assertEqual(self._WORKSPACE_ID, ownership.workspace_id)
        self.assertFalse(hasattr(ownership, "workspace_root"))

    def test_read_only_strict_task_requires_query_and_matching_verification(
        self,
    ) -> None:
        self._register("read")
        task, lease = self._claim("read")

        with self.assertRaises(ServiceError) as missing_query:
            self.service.complete_task(
                "read",
                expected_version=task.version,
                owner="owner",
                epoch=lease.epoch,
            )
        self.assertEqual("QUERY_RECEIPT_REQUIRED", missing_query.exception.code)

        self.service.record_index_query(
            "wf",
            "read",
            owner="owner",
            epoch=lease.epoch,
            trace_id="sha256:query",
            snapshot_id="sha256:input",
            miss_escape_used=True,
        )
        with self.assertRaises(ServiceError) as missing_verification:
            self.service.complete_task(
                "read",
                expected_version=task.version,
                owner="owner",
                epoch=lease.epoch,
            )
        self.assertEqual(
            "VERIFICATION_EVIDENCE_REQUIRED", missing_verification.exception.code
        )

        artifact = self.service.register_artifact(
            "wf",
            "read",
            owner="owner",
            epoch=lease.epoch,
            kind="verification",
            content_hash="sha256:verification",
            safe_path="evidence/verification.json",
            size=10,
            redaction_version="r1",
            snapshot_id="sha256:input",
        )
        completed = self.service.complete_task(
            "read",
            expected_version=task.version,
            owner="owner",
            epoch=lease.epoch,
        )

        binding = self.store.get_index_binding("read")
        self.assertEqual("sha256:verification", artifact.content_hash)
        self.assertEqual("done", completed.state.value)
        self.assertEqual(1, binding.fallback_count)

    def test_write_task_enforces_checkpoint_output_diff_query_and_verification(
        self,
    ) -> None:
        self._register("write", write_scope=("src/app.py",))
        task, lease = self._claim("write")

        self.service.record_index_query(
            "wf",
            "write",
            owner="owner",
            epoch=lease.epoch,
            trace_id="sha256:input-query",
            snapshot_id="sha256:input",
            miss_escape_used=False,
        )
        with self.assertRaises(ServiceError) as missing_checkpoint:
            self.service.complete_task(
                "write", expected_version=task.version, owner="owner", epoch=lease.epoch
            )
        self.assertEqual("CHECKPOINT_REQUIRED", missing_checkpoint.exception.code)

        self.service.record_checkpoint(
            "write", owner="owner", epoch=lease.epoch, checkpoint_id="sha256:checkpoint"
        )
        with self.assertRaises(ServiceError) as missing_output:
            self.service.complete_task(
                "write", expected_version=task.version, owner="owner", epoch=lease.epoch
            )
        self.assertEqual("OUTPUT_SNAPSHOT_REQUIRED", missing_output.exception.code)

        self.index.snapshot_id = "sha256:output"
        self.service.record_output_snapshot(
            "write",
            owner="owner",
            epoch=lease.epoch,
            snapshot_id="sha256:output",
            diff_hash="sha256:diff",
        )
        with self.assertRaises(ServiceError) as missing_output_query:
            self.service.complete_task(
                "write", expected_version=task.version, owner="owner", epoch=lease.epoch
            )
        self.assertEqual("QUERY_RECEIPT_REQUIRED", missing_output_query.exception.code)

        self.service.record_index_query(
            "wf",
            "write",
            owner="owner",
            epoch=lease.epoch,
            trace_id="sha256:output-query",
            snapshot_id="sha256:output",
            miss_escape_used=False,
        )
        with self.assertRaises(ServiceError) as missing_verification:
            self.service.complete_task(
                "write", expected_version=task.version, owner="owner", epoch=lease.epoch
            )
        self.assertEqual(
            "VERIFICATION_EVIDENCE_REQUIRED", missing_verification.exception.code
        )

        self.service.register_artifact(
            "wf",
            "write",
            owner="owner",
            epoch=lease.epoch,
            kind="verification",
            content_hash="sha256:write-verification",
            safe_path="evidence/write-verification.json",
            size=10,
            redaction_version="r1",
            snapshot_id="sha256:output",
        )
        completed = self.service.complete_task(
            "write", expected_version=task.version, owner="owner", epoch=lease.epoch
        )
        self.assertEqual("done", completed.state.value)

    def test_running_write_task_recovers_against_recorded_output_snapshot(
        self,
    ) -> None:
        self._register("recover", write_scope=("src/app.py",))
        self.service.ready_wave("wf")
        task, lease = self.service.claim_task(
            "recover",
            "owner-a",
            expires_at="2026-07-24T00:10:00+00:00",
            now="2026-07-24T00:00:00+00:00",
        )
        self.service.record_index_query(
            "wf",
            task.id,
            owner=lease.owner,
            epoch=lease.epoch,
            trace_id="sha256:input-query",
            snapshot_id="sha256:input",
            miss_escape_used=False,
            now="2026-07-24T00:01:00+00:00",
        )
        self.service.record_checkpoint(
            task.id,
            owner=lease.owner,
            epoch=lease.epoch,
            checkpoint_id="sha256:checkpoint",
            now="2026-07-24T00:02:00+00:00",
        )
        self.index.snapshot_id = "sha256:output"
        self.service.record_output_snapshot(
            task.id,
            owner=lease.owner,
            epoch=lease.epoch,
            snapshot_id="sha256:output",
            diff_hash="sha256:diff",
            now="2026-07-24T00:03:00+00:00",
        )

        recovered_task, recovered_lease = self.service.claim_task(
            task.id,
            "owner-b",
            expires_at="2026-07-24T00:20:00+00:00",
            now="2026-07-24T00:11:00+00:00",
        )

        self.assertEqual("running", recovered_task.state.value)
        self.assertEqual("owner-b", recovered_lease.owner)
        self.assertGreater(recovered_lease.epoch, lease.epoch)
        self.assertEqual("sha256:output", self.index.assertions[-1][1])

    def test_write_task_never_resolves_or_inspects_a_workspace_path(self) -> None:
        registered = self._register("opaque-write", write_scope=("src/app.py",))

        binding = self.store.get_index_binding(registered.id)
        self.assertEqual(self._WORKSPACE_ID, binding.workspace_id)
        self.assertEqual(self._WORKSPACE_ID, self.index.assertions[-1][0])
        self.assertFalse(hasattr(binding, "workspace_root"))


if __name__ == "__main__":
    unittest.main()
