"""Local Atlas activation through the default MCP process root."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

import test_atlas_acceptance as acceptance_module  # noqa: E402
from test_atlas_acceptance import CodeTaskAcceptanceFixture  # noqa: E402

import server as server_module  # noqa: E402
from devkit_atlas.models import AtlasStatus  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402


class TestAtlasLocalActivation(CodeTaskAcceptanceFixture):
    """Exercise fresh default startup and the accepted local learning loop."""

    _COMPLETE_IN_SETUP = True

    def setUp(self) -> None:
        task_type = acceptance_module.Task

        def generic_framework_task(*args: object, **kwargs: object) -> object:
            if kwargs.get("framework") == "pytest":
                kwargs["framework"] = ""
            return task_type(*args, **kwargs)

        with mock.patch.object(
            acceptance_module, "Task", side_effect=generic_framework_task
        ):
            super().setUp()
        self._previous_root = server_module._RUNTIME_ROOT  # noqa: SLF001
        server_module._RUNTIME_ROOT = None  # noqa: SLF001
        self.addCleanup(self._restore_server_root)

    def _restore_server_root(self) -> None:
        server_module._shutdown_runtime()  # noqa: SLF001
        server_module._RUNTIME_ROOT = self._previous_root  # noqa: SLF001

    def _prepare_code_task_completion(self) -> None:
        """Create an extractor-supported append-only Python acceptance."""

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
        self.checkpoint = self.checkpoints.create(
            ownership, self.input_snapshot.snapshot_id
        )
        self.service.record_checkpoint(
            "code-task",
            owner="worker-owner",
            epoch=self.worker_lease.epoch,
            checkpoint_id=self.checkpoint.checkpoint_id,
            now=self._NOW,
        )
        self.source.write_text(
            "def value() -> int:\n    return 1\n\n\ndef added() -> int:\n    return 2\n",
            encoding="utf-8",
        )
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
            self.workspace_id, self.output_snapshot.snapshot_id, "added"
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
            self.store.build_code_task_receipt_attestation(
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

    @contextmanager
    def _server_environment(self, config: RuntimeConfig):
        scoped_names = (
            "CODEX_DEVKIT_DATA_ROOT",
            "CODEX_PROJECT_ROOT",
            "CODEX_THREAD_ID",
        )
        removed = {
            name: os.environ.pop(name) for name in scoped_names if name in os.environ
        }
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "PLUGIN_DATA": str(config.data_root),
                    "CODEX_TASK_TEMP": str(config.scratch_root),
                },
                clear=False,
            ):
                yield
        finally:
            os.environ.update(removed)

    def _runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(self.root / "runtime-data"),
                "CODEX_TASK_TEMP": str(self.root / "runtime-scratch"),
            }
        )

    def _publish_accepted_evidence_to_runtime(self, config: RuntimeConfig) -> None:
        target = sqlite3.connect(config.orchestrator_database)
        try:
            self.store._connection.backup(target)  # noqa: SLF001 - fixture source
        finally:
            target.close()
        shutil.copy2(self.receipts.evidence_key_path, config.data_root)
        shutil.copytree(
            self.receipts.receipt_root,
            config.data_root / "atlas-receipts" / "sha256",
            dirs_exist_ok=True,
        )

    def test_default_startup_bootstraps_local_atlas_storage(self) -> None:
        data_root = self.root / "fresh-default-data"
        scratch_root = self.root / "fresh-default-scratch"
        scratch_root.mkdir()
        config = RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_root),
                "CODEX_TASK_TEMP": str(scratch_root),
            }
        )

        with self._server_environment(config):
            root = server_module._default_runtime_root()  # noqa: SLF001

        assert root is not None
        assert config.atlas_database.is_file()

    def test_accepted_recipe_is_ready_after_default_runtime_reopen(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)

        with self._server_environment(config):
            first = server_module.atlas_prepare(
                self.workspace_id,
                self.output_snapshot.snapshot_id,
                "intent-1",
                "python",
                None,
                ["src/app.py"],
                [],
            )
            accepted = server_module.atlas_accept(
                "workflow",
                "code-task",
                acceptance.acceptance_id,
                outbox.ingestion_key,
            )
            server_module._shutdown_runtime()  # noqa: SLF001
            server_module._RUNTIME_ROOT = None  # noqa: SLF001
            second = server_module.atlas_prepare(
                self.workspace_id,
                self.output_snapshot.snapshot_id,
                "intent-1",
                "python",
                None,
                ["src/app.py"],
                [],
            )

        assert first["ok"] is True, first
        assert first["data"]["status"] == AtlasStatus.NO_VERIFIED_RECIPE.value, first
        assert accepted["ok"] is True, accepted
        assert second["ok"] is True, second
        assert second["data"]["status"] == AtlasStatus.READY.value, second
