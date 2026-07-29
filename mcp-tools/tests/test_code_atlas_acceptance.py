"""Receipt-backed acceptance service coverage."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from code_atlas.receipts import HostCaptureContext, ReceiptRepository  # noqa: E402
import orchestrator.store as store_module  # noqa: E402
from orchestrator.models import (  # noqa: E402
    AtlasOutboxState,
    Task,
    TaskKind,
    Workflow,
    WorkflowKind,
    WorkflowState,
)
from orchestrator.service import OrchestratorService, ServiceError  # noqa: E402
from orchestrator.store import (  # noqa: E402
    AcceptanceConflictError,
    SQLiteStore,
    StoreError,
)
from project_index.checkpoints import CheckpointService  # noqa: E402
from project_index.service import ProjectIndexService  # noqa: E402
from temp_support import task_scratch  # noqa: E402


class CodeTaskAcceptanceServiceTests(unittest.TestCase):
    _NOW = "2026-07-29T01:00:00+00:00"

    def setUp(self) -> None:
        scratch = task_scratch("code-atlas-acceptance")
        self._temporary_directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.repository = self.root / "repository"
        self.workspace = self.root / "workspace"
        subprocess.run(
            ["git", "init", str(self.repository)], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "user.name",
                "acceptance test",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "user.email",
                "acceptance@example.invalid",
            ],
            check=True,
            capture_output=True,
        )
        source = self.repository / "src" / "app.py"
        source.parent.mkdir(parents=True)
        source.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repository), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-m", "initial"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "worktree",
                "add",
                "--detach",
                str(self.workspace),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        self.source = self.workspace / "src" / "app.py"

        self.index = ProjectIndexService(self.root / "project-index.sqlite")
        self.addCleanup(self.index.close)
        self.checkpoints = CheckpointService(
            self.root / "checkpoints.sqlite",
            self.root / "checkpoint-cas",
            self.index,
        )
        self.addCleanup(self.checkpoints.close)
        self.receipts = ReceiptRepository(self.root / "receipts")
        self.store = SQLiteStore(self.root / "orchestrator.sqlite")
        self.addCleanup(self.store.close)
        self.service = OrchestratorService(
            self.store,
            index_service=self.index,
            checkpoint_service=self.checkpoints,
            receipt_repository=self.receipts,
        )

        self.input_snapshot = self.index.sync(self.workspace)
        self.service.create_workflow(
            Workflow(
                "workflow",
                WorkflowKind.DAG,
                "workflow",
                "summary",
                WorkflowState.RUNNING,
            )
        )
        self.service.register_task(
            Task("coordinator", "workflow", "coordinator", "sol"),
            card="coordinator card",
        )
        self.service.register_task(
            Task(
                "code-task",
                "workflow",
                "code task",
                "worker",
                write_scope=("src/app.py",),
                task_kind=TaskKind.CODE,
                intent_id="intent-1",
                language="python",
                framework="pytest",
            ),
            card="code task card",
            strict_index=True,
            workspace_root=str(self.workspace),
            input_snapshot_id=self.input_snapshot.snapshot_id,
            task_node_ids=("sha256:task-node",),
            contract_node_ids=("sha256:contract-node",),
        )
        self.service.ready_wave("workflow")
        _, self.coordinator_lease = self.service.claim_task(
            "coordinator",
            "sol-owner",
            expires_at="2099-01-01T00:00:00+00:00",
            now=self._NOW,
        )
        self.running_task, self.worker_lease = self.service.claim_task(
            "code-task",
            "worker-owner",
            expires_at="2099-01-01T00:00:00+00:00",
            now=self._NOW,
        )
        self._complete_code_task()

    def _complete_code_task(self) -> None:
        input_query = self.index.query(
            self.workspace, self.input_snapshot.snapshot_id, "value"
        )
        self.service.record_index_query(
            "workflow",
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            trace_id=input_query.trace_id,
            snapshot_id=self.input_snapshot.snapshot_id,
            miss_escape_used=False,
            now=self._NOW,
        )
        ownership = self.service.strict_ownership(
            "workflow",
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            now=self._NOW,
        )
        checkpoint = self.checkpoints.create(ownership, self.input_snapshot.snapshot_id)
        self.checkpoint = checkpoint
        self.service.record_checkpoint(
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            checkpoint_id=checkpoint.checkpoint_id,
            now=self._NOW,
        )

        self.source.write_text("def value() -> int:\n    return 2\n", encoding="utf-8")
        self.output_snapshot = self.index.sync(self.workspace)
        indexed_diff = self.index.diff(
            self.input_snapshot.snapshot_id, self.output_snapshot.snapshot_id
        )
        self.service.record_output_snapshot(
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            snapshot_id=self.output_snapshot.snapshot_id,
            diff_hash=self._content_hash(indexed_diff),
            now=self._NOW,
        )
        output_query = self.index.query(
            self.workspace, self.output_snapshot.snapshot_id, "value"
        )
        self.service.record_index_query(
            "workflow",
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            trace_id=output_query.trace_id,
            snapshot_id=self.output_snapshot.snapshot_id,
            miss_escape_used=False,
            now=self._NOW,
        )

        self.execution_receipts = self._capture_receipts()
        receipt_ids = tuple(
            sorted(receipt.receipt_id for receipt in self.execution_receipts)
        )
        workspace_hashes = {
            receipt.workspace_hash for receipt in self.execution_receipts
        }
        self.assertEqual(1, len(workspace_hashes))
        expected_completed_version = self.running_task.version + 1
        self.receipt_attestation = SQLiteStore.build_code_task_receipt_attestation(
            workflow_id="workflow",
            code_task_id="code-task",
            code_task_version=expected_completed_version,
            input_snapshot_id=self.input_snapshot.snapshot_id,
            output_snapshot_id=self.output_snapshot.snapshot_id,
            workspace_hash=next(iter(workspace_hashes)),
            execution_receipt_ids=receipt_ids,
        )
        self.verification_hash = "sha256:" + "a" * 64
        self.service.register_artifact(
            "workflow",
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            kind="verification",
            content_hash=self.verification_hash,
            safe_path="evidence/verification.json",
            size=24,
            redaction_version="r1",
            snapshot_id=self.output_snapshot.snapshot_id,
            now=self._NOW,
        )
        self.service.register_artifact(
            "workflow",
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            kind="verification",
            content_hash=self.receipt_attestation.attestation_hash,
            safe_path="evidence/receipt-attestation.json",
            size=0,
            redaction_version="r1",
            snapshot_id=self.output_snapshot.snapshot_id,
            now=self._NOW,
        )
        self.completed_task = self.service.complete_task(
            "code-task",
            expected_version=self.running_task.version,
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            result_hash=self.verification_hash,
            now=self._NOW,
        )
        self.assertEqual(expected_completed_version, self.completed_task.version)

    def _capture_receipts(
        self,
        *,
        suffix: str = "1",
        command_exit_code: int = 0,
        workspace: str | None = None,
        include_patch: bool = True,
        include_command: bool = True,
    ):
        captured_workspace = str(self.workspace) if workspace is None else workspace
        context = HostCaptureContext(
            host="codex",
            session_id="session-1",
            turn_id="turn-1",
            workspace=captured_workspace,
            observed_at=self._NOW,
        )
        receipts = []
        if include_patch:
            patch = self.receipts.capture(
                {
                    "host": "codex",
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "workspace": captured_workspace,
                    "tool_name": "apply_patch",
                    "tool_use_id": f"patch-{suffix}",
                    "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
                    "tool_response": {"exit_code": 0, "stdout": "patch-applied"},
                    "observed_at": self._NOW,
                },
                capture_context=context,
            )
            self.assertEqual(1, len(patch))
            receipts.extend(patch)
        if include_command:
            command = self.receipts.capture(
                {
                    "host": "codex",
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "workspace": captured_workspace,
                    "tool_name": "shell_command",
                    "tool_use_id": f"command-{suffix}",
                    "tool_input": {"command": "python -m pytest -q"},
                    "tool_response": {
                        "exit_code": command_exit_code,
                        "stdout": "2 passed",
                    },
                    "observed_at": self._NOW,
                },
                capture_context=context,
            )
            self.assertEqual(1, len(command))
            receipts.extend(command)
        return tuple(receipts)

    def _accept(self, **overrides: object):
        code_task_id = overrides.pop("code_task_id", "code-task")
        arguments: dict[str, object] = {
            "expected_code_task_version": self.completed_task.version,
            "expected_output_snapshot_id": self.output_snapshot.snapshot_id,
            "coordinator_task_id": "coordinator",
            "coordinator_owner": "sol-owner",
            "coordinator_epoch": self.coordinator_lease.epoch,
            "execution_receipt_ids": [
                receipt.receipt_id for receipt in self.execution_receipts
            ],
            "now": self._NOW,
        }
        arguments.update(overrides)
        return self.service.accept_code_task("workflow", code_task_id, **arguments)

    def _assert_acceptance_rejected(
        self, code: str, **overrides: object
    ) -> ServiceError:
        with self.assertRaises(ServiceError) as raised:
            self._accept(**overrides)
        self.assertEqual(code, raised.exception.code)
        self.assertIsNone(self.store.acceptance_for_task("code-task"))
        self.assertEqual((), self.store.pending_atlas_outbox(limit=10))
        return raised.exception

    @staticmethod
    def _content_hash(value: object) -> str:
        import hashlib
        import json
        from dataclasses import asdict

        payload = asdict(value)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def test_accepts_current_receipt_bound_code_task_once(self) -> None:
        self.assertNotEqual(
            self.execution_receipts[0].output_hash,
            self.execution_receipts[1].output_hash,
        )
        self.assertNotIn(
            self.completed_task.result_hash,
            tuple(receipt.output_hash for receipt in self.execution_receipts),
        )
        producer_evidence = self.store.task_acceptance_evidence(
            "code-task", self.output_snapshot.snapshot_id
        )
        self.assertIn(
            self.receipt_attestation.attestation_hash,
            producer_evidence.verification_artifact_hashes,
        )
        acceptance, outbox = self._accept()

        self.assertEqual("code-task", acceptance.code_task_id)
        self.assertEqual(
            self.output_snapshot.snapshot_id, acceptance.output_snapshot_id
        )
        self.assertEqual(AtlasOutboxState.PENDING, outbox.state)
        self.assertEqual(acceptance, self.store.acceptance_for_task("code-task"))
        binding = self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        self.assertIsNotNone(binding)
        self.assertEqual(self.checkpoint.checkpoint_id, binding.checkpoint_id)
        self.assertEqual(self.checkpoint.manifest_hash, binding.checkpoint_hash)
        self.assertEqual(
            tuple(sorted(receipt.receipt_id for receipt in self.execution_receipts)),
            binding.execution_receipt_ids,
        )
        self.assertIn(
            self.receipt_attestation.attestation_hash,
            binding.verification_artifact_hashes,
        )
        self.assertEqual(
            binding, self.store.evidence_binding_for_ingestion(outbox.ingestion_key)
        )

    def test_receipt_attestation_hash_binds_every_task_evidence_dimension(self) -> None:
        arguments: dict[str, object] = {
            "workflow_id": "workflow",
            "code_task_id": "code-task",
            "code_task_version": 3,
            "input_snapshot_id": "input-snapshot",
            "output_snapshot_id": "output-snapshot",
            "workspace_hash": "sha256:" + "c" * 64,
            "execution_receipt_ids": (
                "sha256:" + "e" * 64,
                "sha256:" + "f" * 64,
            ),
        }
        attestation = SQLiteStore.build_code_task_receipt_attestation(**arguments)

        self.assertEqual("code-task-receipt-attestation/v1", attestation.schema_version)
        self.assertEqual(
            "sha256:5c84b593fa2b6ee9844ad7c825a94c23255438dbe655b7fc9db67cd11bfe27be",
            attestation.attestation_hash,
        )
        self.assertEqual(
            attestation, SQLiteStore.build_code_task_receipt_attestation(**arguments)
        )
        self.assertEqual(
            arguments["execution_receipt_ids"], attestation.execution_receipt_ids
        )
        variations = {
            "workflow_id": "other-workflow",
            "code_task_id": "other-task",
            "code_task_version": 4,
            "input_snapshot_id": "other-input",
            "output_snapshot_id": "other-output",
            "workspace_hash": "sha256:" + "d" * 64,
            "execution_receipt_ids": ("sha256:" + "e" * 64,),
        }
        for field_name, value in variations.items():
            changed = dict(arguments)
            changed[field_name] = value
            with self.subTest(field_name=field_name):
                self.assertNotEqual(
                    attestation.attestation_hash,
                    SQLiteStore.build_code_task_receipt_attestation(
                        **changed
                    ).attestation_hash,
                )

    def test_evidence_binding_matches_frozen_golden_vector(self) -> None:
        binding = SQLiteStore.build_code_task_evidence_binding(
            workflow_id="workflow",
            task_id="code-task",
            task_version=3,
            input_snapshot_id="input-snapshot",
            output_snapshot_id="output-snapshot",
            indexed_diff_hash="sha256:" + "a" * 64,
            checkpoint_id="checkpoint-1",
            checkpoint_hash="sha256:" + "b" * 64,
            output_query_trace_id="trace-output",
            verification_artifact_hashes=(
                "sha256:" + "c" * 64,
                "sha256:" + "d" * 64,
            ),
            execution_receipt_ids=(
                "sha256:" + "e" * 64,
                "sha256:" + "f" * 64,
            ),
        )

        self.assertEqual("acceptance-evidence-binding/v1", binding.schema_version)
        self.assertEqual(
            "sha256:571cd221acb42b2b73630e2c1c28ba0abefbe838d9e59019c2e55d294c74f208",
            binding.evidence_binding_hash,
        )

    def test_evidence_binding_uses_its_contract_json_encoding(self) -> None:
        original_dumps = json.dumps
        with mock.patch.object(
            store_module.json, "dumps", wraps=original_dumps
        ) as dumped:
            SQLiteStore.build_code_task_evidence_binding(
                workflow_id="workflow",
                task_id="code-task",
                task_version=3,
                input_snapshot_id="input-snapshot",
                output_snapshot_id="output-snapshot",
                indexed_diff_hash="sha256:" + "a" * 64,
                checkpoint_id="checkpoint-1",
                checkpoint_hash="sha256:" + "b" * 64,
                output_query_trace_id="trace-output",
                verification_artifact_hashes=("sha256:" + "c" * 64,),
                execution_receipt_ids=("sha256:" + "d" * 64,),
            )

        dumped.assert_called_once()
        self.assertFalse(dumped.call_args.kwargs["ensure_ascii"])

    def test_store_rejects_an_unbound_acceptance_atomically(self) -> None:
        """No public storage path may accept a task without its evidence binding."""

        binding = self.store.get_index_binding("code-task")
        self.assertIsNotNone(binding)
        with self.assertRaises(StoreError) as raised:
            self.store.insert_code_task_acceptance(
                workflow_id="workflow",
                task_id="code-task",
                task_version=self.completed_task.version,
                coordinator_task_id="coordinator",
                coordinator_owner="sol-owner",
                coordinator_epoch=self.coordinator_lease.epoch,
                input_snapshot_id=binding.input_snapshot_id,
                output_snapshot_id=binding.output_snapshot_id,
                indexed_diff_hash=binding.indexed_diff_hash,
                intent_id=self.completed_task.intent_id,
                language=self.completed_task.language,
                framework=self.completed_task.framework,
                created_at=self._NOW,
                now=self._NOW,
            )

        self.assertEqual("EVIDENCE_INCOMPLETE", raised.exception.code)
        self.assertIsNone(self.store.acceptance_for_task("code-task"))
        self.assertEqual((), self.store.pending_atlas_outbox(limit=10))

    def test_duplicate_acceptance_is_idempotent_and_changed_receipts_are_rejected(
        self,
    ) -> None:
        accepted = self._accept()
        self.assertEqual(accepted, self._accept())
        self.assertEqual(
            1, len(self.store.list_code_task_acceptances("workflow", limit=10))
        )
        self.assertEqual(1, len(self.store.pending_atlas_outbox(limit=10)))

        changed_receipts = self._capture_receipts(suffix="changed")
        with self.assertRaises(ServiceError) as raised:
            self._accept(
                execution_receipt_ids=[
                    receipt.receipt_id for receipt in changed_receipts
                ]
            )

        self.assertEqual("EVIDENCE_INCOMPLETE", raised.exception.code)
        self.assertEqual(accepted, self._accept())

    def test_authorization_gates_require_a_live_same_workflow_coordinator(self) -> None:
        self._assert_acceptance_rejected(
            "ACCEPTANCE_FORBIDDEN", coordinator_task_id="code-task"
        )
        self._assert_acceptance_rejected("STALE_LEASE", coordinator_owner="wrong-owner")
        self._assert_acceptance_rejected(
            "STALE_LEASE", coordinator_epoch=self.coordinator_lease.epoch + 1
        )
        self._assert_acceptance_rejected(
            "VERSION_CONFLICT",
            expected_code_task_version=self.completed_task.version + 1,
        )

        self.service.create_workflow(
            Workflow(
                "other-workflow",
                WorkflowKind.DAG,
                "other workflow",
                "summary",
                WorkflowState.RUNNING,
            )
        )
        self.service.register_task(
            Task("other-coordinator", "other-workflow", "coordinator", "sol"),
            card="other coordinator card",
        )
        self.service.ready_wave("other-workflow")
        _, foreign_lease = self.service.claim_task(
            "other-coordinator",
            "other-owner",
            expires_at="2099-01-01T00:00:00+00:00",
            now=self._NOW,
        )
        self._assert_acceptance_rejected(
            "ACCEPTANCE_FORBIDDEN",
            coordinator_task_id="other-coordinator",
            coordinator_owner="other-owner",
            coordinator_epoch=foreign_lease.epoch,
        )

    def test_missing_duplicate_and_tampered_receipts_block_acceptance(self) -> None:
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE", execution_receipt_ids=["sha256:" + "0" * 64]
        )
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[
                self.execution_receipts[0].receipt_id,
                self.execution_receipts[0].receipt_id,
            ],
        )

        self.receipts._path_for(self.execution_receipts[0].receipt_id).write_text(
            "{}", encoding="utf-8"
        )
        self._assert_acceptance_rejected("EVIDENCE_INCOMPLETE")

    def test_same_workspace_unrelated_receipts_without_attestation_are_rejected(
        self,
    ) -> None:
        unrelated = self._capture_receipts(suffix="unrelated")
        self.assertEqual(
            {receipt.workspace_hash for receipt in self.execution_receipts},
            {receipt.workspace_hash for receipt in unrelated},
        )

        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[receipt.receipt_id for receipt in unrelated],
        )

    def test_done_task_cannot_append_attestation_for_unrelated_receipts(
        self,
    ) -> None:
        unrelated = self._capture_receipts(suffix="post-completion")
        workspace_hashes = {receipt.workspace_hash for receipt in unrelated}
        self.assertEqual(1, len(workspace_hashes))
        unrelated_attestation = SQLiteStore.build_code_task_receipt_attestation(
            workflow_id="workflow",
            code_task_id="code-task",
            code_task_version=self.completed_task.version,
            input_snapshot_id=self.input_snapshot.snapshot_id,
            output_snapshot_id=self.output_snapshot.snapshot_id,
            workspace_hash=next(iter(workspace_hashes)),
            execution_receipt_ids=tuple(
                sorted(receipt.receipt_id for receipt in unrelated)
            ),
        )

        with self.assertRaises(ServiceError) as raised:
            self.service.register_artifact(
                "workflow",
                "code-task",
                owner="worker-owner",
                epoch=self.worker_lease.epoch,
                kind="verification",
                content_hash=unrelated_attestation.attestation_hash,
                safe_path="evidence/receipt-attestation.json",
                size=0,
                redaction_version="r1",
                snapshot_id=self.output_snapshot.snapshot_id,
                now=self._NOW,
            )

        self.assertEqual("INVALID_STATE", raised.exception.code)
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[receipt.receipt_id for receipt in unrelated],
        )

    def test_failed_cross_workspace_and_missing_tool_receipts_block_acceptance(
        self,
    ) -> None:
        failed = self._capture_receipts(suffix="failed", command_exit_code=1)
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[receipt.receipt_id for receipt in failed],
        )

        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        foreign = self._capture_receipts(
            suffix="foreign", workspace=str(other_workspace)
        )
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[
                self.execution_receipts[0].receipt_id,
                foreign[1].receipt_id,
            ],
        )

        patch_only = self._capture_receipts(suffix="patch-only", include_command=False)
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[receipt.receipt_id for receipt in patch_only],
        )
        shell_only = self._capture_receipts(suffix="shell-only", include_patch=False)
        self._assert_acceptance_rejected(
            "EVIDENCE_INCOMPLETE",
            execution_receipt_ids=[receipt.receipt_id for receipt in shell_only],
        )

    def test_stale_index_and_tampered_diff_block_acceptance(self) -> None:
        self.source.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")
        self._assert_acceptance_rejected("INDEX_STALE")

    def test_tampered_indexed_diff_block_acceptance(self) -> None:
        self.store._connection.execute(
            "UPDATE task_index_bindings SET indexed_diff_hash = ? WHERE task_id = ?",
            ("sha256:" + "f" * 64, "code-task"),
        )
        self._assert_acceptance_rejected("SNAPSHOT_MISMATCH")

    def test_checkpoint_query_and_artifact_mismatches_block_acceptance(self) -> None:
        checkpoint = type(
            "Checkpoint",
            (),
            {
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "kind": "checkpoint",
                "workflow_id": "workflow",
                "task_id": "other-task",
                "workspace_root": str(self.workspace),
                "snapshot_id": self.input_snapshot.snapshot_id,
                "write_scope": ("src/app.py",),
                "manifest_hash": self.checkpoint.manifest_hash,
            },
        )()
        with mock.patch.object(self.checkpoints, "status", return_value=checkpoint):
            self._assert_acceptance_rejected("SNAPSHOT_MISMATCH")

    def test_output_query_mismatch_blocks_acceptance(self) -> None:
        query_receipt = type(
            "QueryReceipt",
            (),
            {"snapshot_id": self.input_snapshot.snapshot_id},
        )()
        with mock.patch.object(
            self.index, "get_query_receipt", return_value=query_receipt
        ):
            self._assert_acceptance_rejected("SNAPSHOT_MISMATCH")

    def test_verification_artifact_mismatch_blocks_acceptance(self) -> None:
        self.store._connection.execute(
            "UPDATE artifacts SET kind = ? WHERE content_hash = ?",
            ("other", self.verification_hash),
        )
        self._assert_acceptance_rejected("EVIDENCE_INCOMPLETE")

    def test_outbox_failure_rolls_back_acceptance_and_binding_together(self) -> None:
        with mock.patch.object(
            self.store,
            "_insert_atlas_outbox",
            side_effect=sqlite3.OperationalError("injected outbox failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self._accept()

        self.assertIsNone(self.store.acceptance_for_task("code-task"))
        self.assertEqual((), self.store.pending_atlas_outbox(limit=10))
        event_count = self.store._connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            (SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE,),
        ).fetchone()[0]
        self.assertEqual(0, event_count)

    def test_binding_rehydrates_after_store_recovery_and_rechecks_duplicates(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        expected_binding = self.store.evidence_binding_for_acceptance(
            acceptance.acceptance_id
        )
        self.assertIsNotNone(expected_binding)
        self.store.close()

        reopened = SQLiteStore(self.root / "orchestrator.sqlite")
        self.addCleanup(reopened.close)
        recovered_binding = reopened.evidence_binding_for_ingestion(
            outbox.ingestion_key
        )
        self.assertEqual(expected_binding, recovered_binding)
        recovered_service = OrchestratorService(
            reopened,
            index_service=self.index,
            checkpoint_service=self.checkpoints,
            receipt_repository=self.receipts,
        )
        repeated = recovered_service.accept_code_task(
            "workflow",
            "code-task",
            expected_code_task_version=self.completed_task.version,
            expected_output_snapshot_id=self.output_snapshot.snapshot_id,
            coordinator_task_id="coordinator",
            coordinator_owner="sol-owner",
            coordinator_epoch=self.coordinator_lease.epoch,
            execution_receipt_ids=[
                receipt.receipt_id for receipt in self.execution_receipts
            ],
            now=self._NOW,
        )
        self.assertEqual((acceptance, outbox), repeated)

    def test_missing_binding_conflicts_on_duplicate_acceptance(self) -> None:
        self._accept()
        self.store._connection.execute(
            "DELETE FROM events WHERE event_type = ?",
            (SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE,),
        )

        with self.assertRaises(ServiceError) as raised:
            self._accept()
        self.assertEqual("ACCEPTANCE_CONFLICT", raised.exception.code)
        with self.assertRaises(AcceptanceConflictError):
            self.store.evidence_binding_for_acceptance(
                self.store.acceptance_for_task("code-task").acceptance_id
            )

    def test_corrupt_binding_conflicts_on_typed_recovery(self) -> None:
        acceptance, _ = self._accept()
        self.store._connection.execute(
            "UPDATE events SET redacted_payload = ? WHERE event_type = ?",
            ("{}", SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE),
        )

        with self.assertRaises(AcceptanceConflictError):
            self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        with self.assertRaises(ServiceError) as raised:
            self._accept()
        self.assertEqual("ACCEPTANCE_CONFLICT", raised.exception.code)

    def test_duplicate_binding_events_conflict_on_typed_recovery(self) -> None:
        acceptance, _ = self._accept()
        self.store._connection.execute(
            """
            INSERT INTO events
                (workflow_id, task_id, event_type, redacted_payload, payload_hash, created_at)
            SELECT workflow_id, task_id, event_type, redacted_payload, payload_hash, created_at
            FROM events
            WHERE event_type = ?
            """,
            (SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE,),
        )

        with self.assertRaises(AcceptanceConflictError):
            self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        with self.assertRaises(ServiceError) as raised:
            self._accept()
        self.assertEqual("ACCEPTANCE_CONFLICT", raised.exception.code)

    def test_status_is_bounded_and_does_not_leak_binding_payload(self) -> None:
        acceptance, _ = self._accept()
        binding = self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        self.assertIsNotNone(binding)
        with mock.patch.object(
            self.store,
            "list_code_task_acceptances",
            wraps=self.store.list_code_task_acceptances,
        ) as listed:
            status = self.service.status("workflow")

        listed.assert_called_once_with(
            "workflow", limit=OrchestratorService._MAX_STATUS_CODE_ACCEPTANCES
        )
        entries = status["code_acceptances"]
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(
            {
                "acceptance_id",
                "code_task_id",
                "output_snapshot_id",
                "outbox_state",
                "last_error_code",
                "reason_codes",
            },
            set(entry),
        )
        rendered = json.dumps(entry, sort_keys=True)
        self.assertNotIn(binding.evidence_binding_hash, rendered)
        self.assertNotIn(str(self.workspace), rendered)
        for receipt in self.execution_receipts:
            self.assertNotIn(receipt.receipt_id, rendered)
            self.assertNotIn(receipt.output_hash, rendered)

    def test_legacy_general_task_remains_unaccepted_and_status_compatible(self) -> None:
        legacy = self.service.register_task(
            Task("legacy", "workflow", "legacy", "worker"), card="legacy card"
        )
        self.assertEqual(TaskKind.GENERAL, legacy.task_kind)
        self.assertEqual("", legacy.intent_id)
        self.assertEqual("", legacy.language)
        self._assert_acceptance_rejected(
            "ACCEPTANCE_FORBIDDEN",
            code_task_id="legacy",
            expected_code_task_version=legacy.version,
        )
        self.assertEqual([], self.service.status("workflow")["code_acceptances"])

    def test_binding_rejects_duplicate_or_noncanonical_identifier_lists(self) -> None:
        arguments = {
            "workflow_id": "workflow",
            "task_id": "code-task",
            "task_version": 3,
            "input_snapshot_id": "input-snapshot",
            "output_snapshot_id": "output-snapshot",
            "indexed_diff_hash": "sha256:" + "a" * 64,
            "checkpoint_id": "checkpoint-1",
            "checkpoint_hash": "sha256:" + "b" * 64,
            "output_query_trace_id": "trace-output",
            "verification_artifact_hashes": ("sha256:" + "c" * 64,),
            "execution_receipt_ids": ("sha256:" + "e" * 64,) * 2,
        }
        with self.assertRaises(ValueError):
            SQLiteStore.build_code_task_evidence_binding(**arguments)
        arguments["execution_receipt_ids"] = (
            "sha256:" + "f" * 64,
            "sha256:" + "e" * 64,
        )
        with self.assertRaises(ValueError):
            SQLiteStore.build_code_task_evidence_binding(**arguments)


if __name__ == "__main__":
    unittest.main()
