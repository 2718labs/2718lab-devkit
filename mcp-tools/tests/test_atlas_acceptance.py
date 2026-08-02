"""Receipt-backed acceptance service coverage."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from temp_support import task_scratch  # noqa: E402

import orchestrator.store as store_module  # noqa: E402
from devkit_atlas.receipts import (  # noqa: E402
    HostCaptureContext,
    RawExecutionReceipt,
    ReceiptRepository,
)
from devkit_runtime.bootstrap import RuntimeBootstrap  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from devkit_runtime.project_checkpoint import open_project_checkpoint_rw  # noqa: E402
from orchestrator.models import (  # noqa: E402
    AtlasOutboxState,
    Task,
    TaskKind,
    TaskState,
    Workflow,
    WorkflowKind,
    WorkflowState,
)
from orchestrator.service import OrchestratorService, ServiceError  # noqa: E402
from orchestrator.store import (  # noqa: E402
    AcceptanceConflictError,
    AcceptanceEvidenceError,
    SQLiteStore,
    StoreError,
)


class _StaticReceiptRepository:
    def __init__(self, records: dict[str, object]) -> None:
        self._records = records

    def read(self, receipt_id: str) -> object:
        return self._records[receipt_id]


class _RawExecutionReceiptSubclass(RawExecutionReceipt):
    pass


class CodeTaskAcceptanceFixture(unittest.TestCase):
    _NOW = "2026-07-29T01:00:00+00:00"
    _COMPLETE_IN_SETUP = False

    def setUp(self) -> None:
        self._receipt_read_patcher = None
        self.addCleanup(self._stop_receipt_read_patch)
        scratch = task_scratch("atlas-acceptance")
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

        runtime_scratch = self.root / "runtime-scratch"
        runtime_scratch.mkdir()
        runtime_config = RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(self.root / "runtime-data"),
                "CODEX_TASK_TEMP": str(runtime_scratch),
            }
        )
        RuntimeBootstrap.run(runtime_config)
        self.project_runtime = open_project_checkpoint_rw(
            runtime_config.project_index_database,
            runtime_config.checkpoint_cas_root,
            scratch_root=runtime_config.scratch_root,
        )
        self.addCleanup(self.project_runtime.close)
        self.index = self.project_runtime.project_index
        self.checkpoints = self.project_runtime._checkpoints
        self.receipts = ReceiptRepository(self.root / "receipts")
        self.store = SQLiteStore(self.root / "orchestrator.sqlite")
        self.addCleanup(self.store.close)
        self.service = OrchestratorService(
            self.store,
            index_service=self.index,
            checkpoint_service=self.checkpoints,
            receipt_repository=self.receipts,
        )

        self.workspace_id = self.index.project_index_register(self.workspace)
        self.input_snapshot = self.index.sync(self.workspace_id)
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
            workspace_id=self.workspace_id,
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
        self._prepare_code_task_completion()
        if self._COMPLETE_IN_SETUP:
            self._complete_code_task()

    def _prepare_code_task_completion(self) -> None:
        input_query = self.index.query(
            self.workspace_id, self.input_snapshot.snapshot_id, "value"
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
        self.output_snapshot = self.index.sync(self.workspace_id)
        indexed_diff = self.index.diff(
            self.workspace_id,
            self.input_snapshot.snapshot_id,
            self.output_snapshot.snapshot_id,
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
            self.workspace_id, self.output_snapshot.snapshot_id, "value"
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
        self.receipt_ids = tuple(
            sorted(receipt.receipt_id for receipt in self.execution_receipts)
        )
        self.receipts_by_id = {
            receipt.receipt_id: receipt for receipt in self.execution_receipts
        }
        workspace_hashes = {
            receipt.workspace_hash for receipt in self.execution_receipts
        }
        self.assertEqual(1, len(workspace_hashes))
        self.expected_receipt_attestation = (
            SQLiteStore.build_code_task_receipt_attestation(
                workflow_id="workflow",
                code_task_id="code-task",
                code_task_version=self.running_task.version + 1,
                input_snapshot_id=self.input_snapshot.snapshot_id,
                output_snapshot_id=self.output_snapshot.snapshot_id,
                workspace_hash=next(iter(workspace_hashes)),
                execution_receipt_ids=self.receipt_ids,
            )
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

    def _complete_code_task(self) -> None:
        self.completed_task = self.service.complete_task(
            "code-task",
            expected_version=self.running_task.version,
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            result_hash=self.verification_hash,
            execution_receipt_ids=list(self.receipt_ids),
            now=self._NOW,
        )
        self.assertEqual(self.running_task.version + 1, self.completed_task.version)
        self.receipt_attestation = self.store.code_task_receipt_attestation_for_task(
            "code-task"
        )
        self.assertEqual(self.expected_receipt_attestation, self.receipt_attestation)

    def _use_receipt_records(self, records: dict[str, object]) -> None:
        self._stop_receipt_read_patch()
        self._receipt_read_patcher = mock.patch.object(
            ReceiptRepository,
            "read",
            autospec=True,
            side_effect=lambda _repository, receipt_id: records[receipt_id],
        )
        self._receipt_read_patcher.start()

    def _use_untrusted_receipt_repository(self, records: dict[str, object]) -> None:
        self.service = OrchestratorService(
            self.store,
            index_service=self.index,
            checkpoint_service=self.checkpoints,
            receipt_repository=_StaticReceiptRepository(records),  # type: ignore[arg-type]
        )

    def _stop_receipt_read_patch(self) -> None:
        if self._receipt_read_patcher is not None:
            self._receipt_read_patcher.stop()
            self._receipt_read_patcher = None

    @staticmethod
    def _duck_receipt(receipt: RawExecutionReceipt, *, exit_code: object) -> object:
        return SimpleNamespace(
            canonical_tool=receipt.canonical_tool,
            workspace_hash=receipt.workspace_hash,
            success=True,
            exit_code=exit_code,
        )

    @staticmethod
    def _forged_raw_receipt(receipt: RawExecutionReceipt) -> RawExecutionReceipt:
        return replace(
            receipt,
            schema_version="forged-schema",
            host="forged-host",
            tool_use_id="unsafe tool id",
            command_spec=(42,),  # type: ignore[arg-type]
            observed_at="not-a-timestamp",
        )

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


class CodeTaskAcceptanceServiceTests(CodeTaskAcceptanceFixture):
    _COMPLETE_IN_SETUP = True

    def test_lookup_requires_the_exact_workflow_and_code_task_pair(self) -> None:
        acceptance, _ = self._accept()

        assert (
            self.store.acceptance_for_workflow_task("workflow", "code-task")
            == acceptance
        )
        assert (
            self.store.acceptance_for_workflow_task("other-workflow", "code-task")
            is None
        )

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
        self.assertEqual(
            self.receipt_attestation,
            self.store.code_task_receipt_attestation_for_acceptance(
                acceptance.acceptance_id
            ),
        )
        self.assertIsNone(
            self.store.get_artifact(self.receipt_attestation.attestation_hash)
        )
        durable_binding = self.store.get_index_binding("code-task")
        self.assertEqual(self.workspace_id, durable_binding.workspace_id)
        self.assertFalse(hasattr(durable_binding, "workspace_root"))

    def test_acceptance_diff_is_bound_to_the_same_opaque_workspace_id(self) -> None:
        with mock.patch.object(self.index, "diff", wraps=self.index.diff) as diff:
            self._accept()

        diff.assert_called_once_with(
            self.workspace_id,
            self.input_snapshot.snapshot_id,
            self.output_snapshot.snapshot_id,
        )

    def test_cross_workspace_snapshot_registration_fails_before_store_writes(
        self,
    ) -> None:
        other_workspace = self.root / "foreign-index-workspace"
        other_workspace.mkdir()
        (other_workspace / "foreign.py").write_text("value = 1\n", encoding="utf-8")
        other_workspace_id = self.index.project_index_register(other_workspace)
        foreign_snapshot = self.index.sync(other_workspace_id)

        with self.assertRaises(ServiceError) as rejected:
            self.service.register_task(
                Task("foreign-snapshot-task", "workflow", "foreign", "worker"),
                card="foreign snapshot card",
                strict_index=True,
                workspace_id=self.workspace_id,
                input_snapshot_id=foreign_snapshot.snapshot_id,
                task_node_ids=("sha256:foreign-task-node",),
            )

        self.assertEqual("NOT_FOUND", rejected.exception.code)
        with self.assertRaises(KeyError):
            self.store.get_task("foreign-snapshot-task")
        self.assertIsNone(self.store.get_index_binding("foreign-snapshot-task"))

    def test_acceptance_rejects_duck_receipts_after_real_completion(self) -> None:
        records = dict(self.receipts_by_id)
        replaced_id = self.receipt_ids[0]
        records[replaced_id] = self._duck_receipt(
            self.receipts_by_id[replaced_id], exit_code=False
        )
        self._use_receipt_records(records)

        error = self._assert_acceptance_rejected("EVIDENCE_INCOMPLETE")

        self.assertEqual("execution evidence is incomplete", str(error))

    def test_acceptance_rejects_exact_but_unverified_raw_receipts(self) -> None:
        records = dict(self.receipts_by_id)
        replaced_id = self.receipt_ids[0]
        records[replaced_id] = self._forged_raw_receipt(
            self.receipts_by_id[replaced_id]
        )
        self._use_untrusted_receipt_repository(records)

        error = self._assert_acceptance_rejected("EVIDENCE_INCOMPLETE")

        self.assertEqual("execution evidence is incomplete", str(error))

    def test_generic_verification_artifact_cannot_replace_typed_attestation(
        self,
    ) -> None:
        with self.store._transaction() as cursor:
            cursor.execute(
                "DELETE FROM code_task_receipt_owners WHERE task_id = ?",
                ("code-task",),
            )
            cursor.execute(
                "DELETE FROM code_task_receipt_attestations WHERE task_id = ?",
                ("code-task",),
            )
            cursor.execute(
                """
                INSERT INTO artifacts (
                    content_hash, kind, safe_path, size, redaction_version, created_at
                ) VALUES (?, 'verification', ?, 0, 'r1', ?)
                """,
                (
                    self.receipt_attestation.attestation_hash,
                    "evidence/legacy-receipt-attestation.json",
                    self._NOW,
                ),
            )
            cursor.execute(
                """
                INSERT INTO artifact_owners (content_hash, task_id)
                VALUES (?, ?)
                """,
                (self.receipt_attestation.attestation_hash, "code-task"),
            )
            cursor.execute(
                """
                INSERT INTO task_index_verification_artifacts (
                    task_id, content_hash, snapshot_id
                ) VALUES (?, ?, ?)
                """,
                (
                    "code-task",
                    self.receipt_attestation.attestation_hash,
                    self.output_snapshot.snapshot_id,
                ),
            )

        self.assertIsNotNone(
            self.store.get_artifact(self.receipt_attestation.attestation_hash)
        )
        self._assert_acceptance_rejected("EVIDENCE_INCOMPLETE")

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

    def test_typed_attestation_recovery_rejects_field_hash_and_set_tampering(
        self,
    ) -> None:
        acceptance, _ = self._accept()
        row = self.store._connection.execute(
            "SELECT * FROM code_task_receipt_attestations WHERE task_id = ?",
            ("code-task",),
        ).fetchone()
        originals = {
            "workspace_hash": str(row["workspace_hash"]),
            "attestation_hash": str(row["attestation_hash"]),
            "execution_receipt_ids": str(row["execution_receipt_ids"]),
        }
        mutations = {
            "workspace_hash": "sha256:" + "d" * 64,
            "attestation_hash": "sha256:" + "e" * 64,
            "execution_receipt_ids": json.dumps(
                [self.receipt_ids[0]], separators=(",", ":")
            ),
        }

        self.store._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            for field_name, value in mutations.items():
                self.store._connection.execute(
                    f"""
                    UPDATE code_task_receipt_attestations
                    SET {field_name} = ?
                    WHERE task_id = ?
                    """,
                    (value, "code-task"),
                )
                with self.subTest(field_name=field_name):
                    with self.assertRaises(AcceptanceConflictError):
                        self.store.code_task_receipt_attestation_for_task("code-task")
                    with self.assertRaises(AcceptanceConflictError):
                        self.store.code_task_receipt_attestation_for_acceptance(
                            acceptance.acceptance_id
                        )
                self.store._connection.execute(
                    f"""
                    UPDATE code_task_receipt_attestations
                    SET {field_name} = ?
                    WHERE task_id = ?
                    """,
                    (originals[field_name], "code-task"),
                )
        finally:
            for field_name, value in originals.items():
                self.store._connection.execute(
                    f"""
                    UPDATE code_task_receipt_attestations
                    SET {field_name} = ?
                    WHERE task_id = ?
                    """,
                    (value, "code-task"),
                )
            self.store._connection.execute("PRAGMA foreign_keys = ON")

        self.assertEqual(
            self.receipt_attestation,
            self.store.code_task_receipt_attestation_for_acceptance(
                acceptance.acceptance_id
            ),
        )

    def test_typed_attestation_recovery_rejects_owner_row_tampering(self) -> None:
        acceptance, _ = self._accept()
        removed_receipt_id = self.receipt_ids[0]
        self.store._connection.execute(
            "DELETE FROM code_task_receipt_owners WHERE receipt_id = ?",
            (removed_receipt_id,),
        )

        with self.assertRaises(AcceptanceConflictError):
            self.store.code_task_receipt_attestation_for_task("code-task")
        with self.assertRaises(AcceptanceConflictError):
            self.store.code_task_receipt_attestation_for_acceptance(
                acceptance.acceptance_id
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

        with self.assertRaises(AcceptanceConflictError):
            self.store.complete_task(
                "code-task",
                TaskState.DONE,
                self.running_task.version,
                "worker-owner",
                self.worker_lease.epoch,
                result_hash=self.verification_hash,
                receipt_attestation=unrelated_attestation,
                now=self._NOW,
            )
        self.assertEqual(
            self.receipt_attestation,
            self.store.code_task_receipt_attestation_for_task("code-task"),
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
                "workspace_root": "",
                "workspace_id": self.workspace_id,
                "snapshot_id": self.input_snapshot.snapshot_id,
                "write_scope": ("src/app.py",),
                "manifest_hash": self.checkpoint.manifest_hash,
            },
        )()
        with mock.patch.object(self.checkpoints, "status", return_value=checkpoint):
            self._assert_acceptance_rejected("SNAPSHOT_MISMATCH")

    def test_cross_workspace_checkpoint_binding_blocks_acceptance(self) -> None:
        checkpoint = type(
            "Checkpoint",
            (),
            {
                "checkpoint_id": self.checkpoint.checkpoint_id,
                "kind": "checkpoint",
                "workflow_id": "workflow",
                "task_id": "code-task",
                "workspace_root": "",
                "workspace_id": "sha256:" + "2" * 64,
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


class CodeTaskCompletionAttestationTests(CodeTaskAcceptanceFixture):
    def _assert_completion_evidence_rejected(
        self, execution_receipt_ids: list[str]
    ) -> ServiceError:
        with self.assertRaises(ServiceError) as raised:
            self.service.complete_task(
                "code-task",
                expected_version=self.running_task.version,
                owner="worker-owner",
                epoch=self.worker_lease.epoch,
                result_hash=self.verification_hash,
                execution_receipt_ids=execution_receipt_ids,
                now=self._NOW,
            )

        self.assertEqual("EVIDENCE_INCOMPLETE", raised.exception.code)
        self.assertEqual(TaskState.RUNNING, self.store.get_task("code-task").state)
        self.assertIsNone(
            self.store.code_task_receipt_attestation_for_task("code-task")
        )
        owner_count = self.store._connection.execute(
            "SELECT COUNT(*) FROM code_task_receipt_owners WHERE task_id = ?",
            ("code-task",),
        ).fetchone()[0]
        self.assertEqual(0, owner_count)
        return raised.exception

    def test_duck_receipt_records_cannot_complete_code_tasks(self) -> None:
        records = dict(self.receipts_by_id)
        replaced_id = self.receipt_ids[0]
        records[replaced_id] = self._duck_receipt(
            self.receipts_by_id[replaced_id], exit_code=False
        )
        self._use_receipt_records(records)

        error = self._assert_completion_evidence_rejected(list(self.receipt_ids))

        self.assertEqual("execution evidence is incomplete", str(error))

    def test_exact_but_unverified_raw_receipts_cannot_complete_code_tasks(
        self,
    ) -> None:
        records = dict(self.receipts_by_id)
        replaced_id = self.receipt_ids[0]
        records[replaced_id] = self._forged_raw_receipt(
            self.receipts_by_id[replaced_id]
        )
        self._use_untrusted_receipt_repository(records)

        error = self._assert_completion_evidence_rejected(list(self.receipt_ids))

        self.assertEqual("execution evidence is incomplete", str(error))

    def test_noncanonical_receipt_status_fields_are_rejected(self) -> None:
        for field_name, value in (
            ("exit_code", False),
            ("exit_code", True),
            ("success", 1),
            ("canonical_tool", "apply_patch"),
        ):
            with self.subTest(field_name=field_name, value=value):
                records = {
                    receipt_id: replace(receipt, **{field_name: value})
                    for receipt_id, receipt in self.receipts_by_id.items()
                }
                self._use_receipt_records(records)

                error = self._assert_completion_evidence_rejected(
                    list(self.receipt_ids)
                )

                self.assertEqual("execution evidence is incomplete", str(error))

    def test_receipt_ids_must_match_each_requested_repository_key(self) -> None:
        first_id, second_id = self.receipt_ids
        records = {
            first_id: self.receipts_by_id[second_id],
            second_id: self.receipts_by_id[first_id],
        }
        self._use_receipt_records(records)

        error = self._assert_completion_evidence_rejected(list(self.receipt_ids))

        self.assertEqual("execution evidence is incomplete", str(error))

    def test_noncanonical_receipt_hashes_are_rejected_without_value_disclosure(
        self,
    ) -> None:
        malicious_value = "not-a-sha256-or-a-safe-path"
        for field_name in (
            "session_id_hash",
            "turn_id_hash",
            "command_spec_hash",
            "input_hash",
            "output_hash",
            "workspace_hash",
        ):
            with self.subTest(field_name=field_name):
                records = dict(self.receipts_by_id)
                receipt_id = self.receipt_ids[0]
                records[receipt_id] = replace(
                    self.receipts_by_id[receipt_id],
                    **{field_name: malicious_value},
                )
                self._use_receipt_records(records)

                error = self._assert_completion_evidence_rejected(
                    list(self.receipt_ids)
                )

                self.assertEqual("execution evidence is incomplete", str(error))
                self.assertNotIn(malicious_value, str(error))

    def test_mapping_and_receipt_subclass_substitutes_are_rejected(self) -> None:
        receipt_id = self.receipt_ids[0]
        original = self.receipts_by_id[receipt_id]
        substitutes = (
            original.to_dict(),
            _RawExecutionReceiptSubclass(**original.to_dict()),
        )
        for substitute in substitutes:
            with self.subTest(substitute_type=type(substitute).__name__):
                records = dict(self.receipts_by_id)
                records[receipt_id] = substitute
                self._use_receipt_records(records)

                error = self._assert_completion_evidence_rejected(
                    list(self.receipt_ids)
                )

                self.assertEqual("execution evidence is incomplete", str(error))

    def test_service_code_completion_requires_receipt_ids_atomically(self) -> None:
        with self.assertRaises(ServiceError) as raised:
            self.service.complete_task(
                "code-task",
                expected_version=self.running_task.version,
                owner="worker-owner",
                epoch=self.worker_lease.epoch,
                result_hash=self.verification_hash,
                now=self._NOW,
            )

        self.assertEqual("EVIDENCE_INCOMPLETE", raised.exception.code)
        self.assertEqual(TaskState.RUNNING, self.store.get_task("code-task").state)
        self.assertIsNone(
            self.store.code_task_receipt_attestation_for_task("code-task")
        )

    def test_store_code_completion_requires_typed_attestation_atomically(self) -> None:
        with self.assertRaises(AcceptanceEvidenceError):
            self.store.complete_task(
                "code-task",
                TaskState.DONE,
                self.running_task.version,
                "worker-owner",
                self.worker_lease.epoch,
                result_hash=self.verification_hash,
                now=self._NOW,
            )

        self.assertEqual(TaskState.RUNNING, self.store.get_task("code-task").state)
        self.assertIsNone(
            self.store.code_task_receipt_attestation_for_task("code-task")
        )

    def test_invalid_raw_receipts_roll_back_code_completion_and_attestation(
        self,
    ) -> None:
        failed = self._capture_receipts(suffix="completion-failed", command_exit_code=1)
        self._assert_completion_evidence_rejected(
            [receipt.receipt_id for receipt in failed]
        )

        other_workspace = self.root / "completion-other-workspace"
        other_workspace.mkdir()
        foreign = self._capture_receipts(
            suffix="completion-foreign", workspace=str(other_workspace)
        )
        self._assert_completion_evidence_rejected(
            [receipt.receipt_id for receipt in foreign]
        )
        self._assert_completion_evidence_rejected(
            [
                self.execution_receipts[0].receipt_id,
                foreign[1].receipt_id,
            ]
        )

        patch_only = self._capture_receipts(
            suffix="completion-patch-only", include_command=False
        )
        self._assert_completion_evidence_rejected(
            [receipt.receipt_id for receipt in patch_only]
        )
        shell_only = self._capture_receipts(
            suffix="completion-shell-only", include_patch=False
        )
        self._assert_completion_evidence_rejected(
            [receipt.receipt_id for receipt in shell_only]
        )

    def test_successful_completion_atomically_persists_typed_attestation(
        self,
    ) -> None:
        self._complete_code_task()

        self.assertEqual(TaskState.DONE, self.completed_task.state)
        self.assertEqual(self.expected_receipt_attestation, self.receipt_attestation)
        owner_rows = self.store._connection.execute(
            """
            SELECT receipt_id, task_id, code_task_version, attestation_hash
            FROM code_task_receipt_owners
            WHERE task_id = ?
            ORDER BY receipt_id
            """,
            ("code-task",),
        ).fetchall()
        self.assertEqual(
            self.receipt_ids, tuple(row["receipt_id"] for row in owner_rows)
        )
        self.assertTrue(
            all(
                row["task_id"] == "code-task"
                and row["code_task_version"] == self.completed_task.version
                and row["attestation_hash"] == self.receipt_attestation.attestation_hash
                for row in owner_rows
            )
        )
        attestation_row = self.store._connection.execute(
            "SELECT * FROM code_task_receipt_attestations WHERE task_id = ?",
            ("code-task",),
        ).fetchone()
        rendered_trust_rows = json.dumps(
            {
                "attestation": dict(attestation_row),
                "owners": [dict(row) for row in owner_rows],
            },
            sort_keys=True,
        )
        self.assertNotIn(str(self.workspace), rendered_trust_rows)
        self.assertNotIn("*** Begin Patch", rendered_trust_rows)
        self.assertNotIn("python -m pytest -q", rendered_trust_rows)
        self.assertNotIn("patch-applied", rendered_trust_rows)
        self.assertNotIn("2 passed", rendered_trust_rows)
        for receipt in self.execution_receipts:
            self.assertNotIn(receipt.input_hash, rendered_trust_rows)
            self.assertNotIn(receipt.output_hash, rendered_trust_rows)

    def test_same_completion_retry_is_idempotent_but_wrong_owner_is_rejected(
        self,
    ) -> None:
        self._complete_code_task()

        repeated = self.service.complete_task(
            "code-task",
            expected_version=self.running_task.version,
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            result_hash=self.verification_hash,
            execution_receipt_ids=list(reversed(self.receipt_ids)),
            now=self._NOW,
        )
        self.assertEqual(self.completed_task, repeated)
        with self.assertRaises(ServiceError) as raised:
            self.service.complete_task(
                "code-task",
                expected_version=self.running_task.version,
                owner="not-worker-owner",
                epoch=self.worker_lease.epoch,
                result_hash=self.verification_hash,
                execution_receipt_ids=list(self.receipt_ids),
                now=self._NOW,
            )
        self.assertEqual("STALE_LEASE", raised.exception.code)

    def test_same_receipts_cannot_complete_a_second_code_task(self) -> None:
        self._complete_code_task()
        second = self.store.register_task(
            Task(
                "code-task-2",
                "workflow",
                "second code task",
                "worker",
                state=TaskState.RUNNING,
                write_scope=("src/second.py",),
                task_kind=TaskKind.CODE,
                intent_id="intent-2",
                language="python",
                framework="pytest",
            ),
            strict_index=True,
            workspace_id=self.workspace_id,
            input_snapshot_id=self.input_snapshot.snapshot_id,
            task_node_ids=("sha256:second-task-node",),
            contract_node_ids=("sha256:second-contract-node",),
        )
        second_lease = self.store.acquire_lease(
            second.id,
            "second-worker",
            "2099-01-01T00:00:00+00:00",
            now=self._NOW,
        )
        self.store.record_checkpoint(
            second.id,
            "second-worker",
            second_lease.epoch,
            "sha256:second-checkpoint",
            now=self._NOW,
        )
        self.store.record_output_snapshot(
            second.id,
            "second-worker",
            second_lease.epoch,
            snapshot_id=self.output_snapshot.snapshot_id,
            diff_hash="sha256:second-diff",
            now=self._NOW,
        )
        self.store.record_index_query(
            second.id,
            "second-worker",
            second_lease.epoch,
            trace_id="sha256:second-output-query",
            snapshot_id=self.output_snapshot.snapshot_id,
            miss_escape_used=False,
            now=self._NOW,
        )
        self.store.register_task_artifact(
            second.id,
            "second-worker",
            second_lease.epoch,
            kind="verification",
            content_hash="sha256:" + "b" * 64,
            safe_path="evidence/second-verification.json",
            size=10,
            redaction_version="r1",
            snapshot_id=self.output_snapshot.snapshot_id,
            now=self._NOW,
        )
        second_attestation = SQLiteStore.build_code_task_receipt_attestation(
            workflow_id="workflow",
            code_task_id=second.id,
            code_task_version=second.version + 1,
            input_snapshot_id=self.input_snapshot.snapshot_id,
            output_snapshot_id=self.output_snapshot.snapshot_id,
            workspace_hash=self.receipt_attestation.workspace_hash,
            execution_receipt_ids=self.receipt_ids,
        )

        with self.assertRaises(AcceptanceConflictError):
            self.store.complete_task(
                second.id,
                TaskState.DONE,
                second.version,
                "second-worker",
                second_lease.epoch,
                receipt_attestation=second_attestation,
                now=self._NOW,
            )

        self.assertEqual(TaskState.RUNNING, self.store.get_task(second.id).state)
        self.assertIsNone(self.store.code_task_receipt_attestation_for_task(second.id))


if __name__ == "__main__":
    unittest.main()
