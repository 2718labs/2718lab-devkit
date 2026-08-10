"""Production acceptance reconstruction coverage."""

from __future__ import annotations

import inspect
import json
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from test_atlas_acceptance import CodeTaskAcceptanceFixture  # noqa: E402

import server as server_module  # noqa: E402
from devkit_atlas import ASSET_ROOT  # noqa: E402
from devkit_atlas.canonical import canonical_json  # noqa: E402
from devkit_atlas.models import AtlasError  # noqa: E402
from devkit_atlas.recipes import BundledRecipeLoader  # noqa: E402
from devkit_atlas.service import AtlasService  # noqa: E402
from devkit_atlas.store import AtlasStore  # noqa: E402
from devkit_continuity.models import ContinuityKey  # noqa: E402
from devkit_continuity.service import ContinuityService  # noqa: E402
from devkit_continuity.store import ContinuityStore, ContinuityStoreError  # noqa: E402
from devkit_runtime.atlas_acceptance import (  # noqa: E402
    ProductionAcceptanceEvidenceReader,
)
from devkit_runtime.composition import RuntimeRoot  # noqa: E402
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from devkit_runtime.uow import DEFAULT_RUNTIME_ADAPTER_FACTORIES  # noqa: E402
from orchestrator.store import SQLiteStore  # noqa: E402
from project_index.service import ProjectIndexService  # noqa: E402


def test_public_rebuild_has_exact_four_acceptance_fields() -> None:
    """The public reader cannot accept a caller-built evidence request."""

    assert tuple(
        inspect.signature(ProductionAcceptanceEvidenceReader.rebuild).parameters
    ) == (
        "self",
        "workflow_id",
        "code_task_id",
        "acceptance_id",
        "ingestion_key",
    )
    assert tuple(inspect.signature(AtlasService.accept).parameters) == (
        "self",
        "workflow_id",
        "code_task_id",
        "acceptance_id",
        "ingestion_key",
    )


class ProductionAcceptanceEvidenceReaderTests(CodeTaskAcceptanceFixture):
    _COMPLETE_IN_SETUP = True

    def _reader(self) -> ProductionAcceptanceEvidenceReader:
        return ProductionAcceptanceEvidenceReader(
            self.service,
            self.index,
            self.checkpoints,
            self.receipts,
        )

    def _accepted_identifiers(self) -> tuple[object, object]:
        return self._accept()

    def _atlas(self, reader: ProductionAcceptanceEvidenceReader) -> AtlasService:
        store = AtlasStore(self.root / "atlas.sqlite", self.root / "atlas-cas")
        self.addCleanup(store.close)
        return AtlasService(
            store,
            BundledRecipeLoader(ASSET_ROOT),
            self.index,
            acceptance_evidence_reader=reader,
        )

    def test_rebuilds_and_projects_the_canonical_immutable_acceptance(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        reader = self._reader()

        request = reader.rebuild(
            workflow_id="workflow",
            code_task_id="code-task",
            acceptance_id=acceptance.acceptance_id,
            ingestion_key=outbox.ingestion_key,
        )

        binding = self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        assert binding is not None
        assert request.acceptance_id == acceptance.acceptance_id
        assert request.ingestion_key == outbox.ingestion_key
        assert request.payload_hash == acceptance.payload_hash
        assert request.code_task_version == acceptance.code_task_version
        assert request.input_snapshot_id == acceptance.input_snapshot_id
        assert request.output_snapshot_id == acceptance.output_snapshot_id
        assert request.evidence_binding_hash == binding.evidence_binding_hash

        projection = self._atlas(reader).accept(
            workflow_id="workflow",
            code_task_id="code-task",
            acceptance_id=acceptance.acceptance_id,
            ingestion_key=outbox.ingestion_key,
        )
        assert projection.acceptance_id == acceptance.acceptance_id
        assert projection.code_task_id == "code-task"

    def test_mutable_outbox_payload_and_state_are_not_evidence_truth(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            """
            UPDATE atlas_ingestion_outbox
            SET payload_json = ?, state = ?, last_error_code = ?, reason_codes_json = ?
            WHERE acceptance_id = ?
            """,
            (
                '{"forged":"mutable-outbox-payload"}',
                "projected",
                "FORGED_OUTBOX",
                '["FORGED_OUTBOX"]',
                acceptance.acceptance_id,
            ),
        )

        request = self._reader().rebuild(
            workflow_id="workflow",
            code_task_id="code-task",
            acceptance_id=acceptance.acceptance_id,
            ingestion_key=outbox.ingestion_key,
        )

        assert request.payload_hash == acceptance.payload_hash
        assert request.acceptance_id == acceptance.acceptance_id

    def test_forged_acceptance_binding_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            """
            UPDATE events
            SET payload_hash = ?
            WHERE workflow_id = ? AND task_id = ? AND event_type = ?
            """,
            (
                "sha256:" + "0" * 64,
                "workflow",
                "code-task",
                SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE,
            ),
        )

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_forged_receipt_hash_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        receipt = self.execution_receipts[0]
        receipt_path = self.receipts.receipt_root / f"{receipt.receipt_id[7:]}.json"
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["output_hash"] = "sha256:" + "0" * 64
        receipt_path.write_text(canonical_json(payload), encoding="utf-8")

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_missing_immutable_evidence_is_unavailable(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "DELETE FROM events WHERE workflow_id = ? AND task_id = ? AND event_type = ?",
            ("workflow", "code-task", SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE),
        )

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"

    def test_forged_public_identity_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        for field in ("acceptance_id", "ingestion_key"):
            with self.subTest(field=field):
                arguments = {
                    "workflow_id": "workflow",
                    "code_task_id": "code-task",
                    "acceptance_id": acceptance.acceptance_id,
                    "ingestion_key": outbox.ingestion_key,
                }
                arguments[field] = "sha256:" + "0" * 64

                with pytest.raises(AtlasError) as raised:
                    self._reader().rebuild(**arguments)

                assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_known_acceptance_cannot_be_rebound_to_another_task(self) -> None:
        acceptance, outbox = self._accepted_identifiers()

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="other-code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_cross_workspace_receipt_attestation_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        foreign_workspace_id = self.index.project_index_register(self.repository)
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "UPDATE task_index_bindings SET workspace_id = ? WHERE task_id = ?",
            (foreign_workspace_id, "code-task"),
        )
        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )
        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "UPDATE task_index_bindings SET workspace_id = ? WHERE task_id = ?",
            (self.workspace_id, "code-task"),
        )

        foreign_receipts = self._capture_receipts(
            suffix="foreign-receipt-pair", workspace=str(self.repository)
        )
        foreign_receipt_ids = tuple(
            sorted(receipt.receipt_id for receipt in foreign_receipts)
        )
        foreign_workspace_hashes = {
            receipt.workspace_hash for receipt in foreign_receipts
        }
        assert len(foreign_workspace_hashes) == 1
        foreign_workspace_hash = next(iter(foreign_workspace_hashes))
        expected_workspace_hash = self.receipts.workspace_hash_for(
            str(self.index.workspace_authority.resolve(self.workspace_id).root)
        )
        assert foreign_workspace_hash != expected_workspace_hash

        foreign_attestation = SQLiteStore.build_code_task_receipt_attestation(
            workflow_id="workflow",
            code_task_id="code-task",
            code_task_version=self.completed_task.version,
            input_snapshot_id=self.input_snapshot.snapshot_id,
            output_snapshot_id=self.output_snapshot.snapshot_id,
            workspace_hash=foreign_workspace_hash,
            execution_receipt_ids=foreign_receipt_ids,
        )
        existing_binding = self.store.evidence_binding_for_acceptance(
            acceptance.acceptance_id
        )
        assert existing_binding is not None
        foreign_binding = SQLiteStore.build_code_task_evidence_binding(
            workflow_id=existing_binding.workflow_id,
            task_id=existing_binding.code_task_id,
            task_version=existing_binding.code_task_version,
            input_snapshot_id=existing_binding.input_snapshot_id,
            output_snapshot_id=existing_binding.output_snapshot_id,
            indexed_diff_hash=existing_binding.indexed_diff_hash,
            checkpoint_id=existing_binding.checkpoint_id,
            checkpoint_hash=existing_binding.checkpoint_hash,
            output_query_trace_id=existing_binding.output_query_trace_id,
            verification_artifact_hashes=tuple(
                sorted({self.verification_hash, foreign_attestation.attestation_hash})
            ),
            execution_receipt_ids=foreign_receipt_ids,
        )

        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "DELETE FROM code_task_receipt_owners WHERE task_id = ?",
            ("code-task",),
        )
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            """
            UPDATE code_task_receipt_attestations
            SET workspace_hash = ?, execution_receipt_ids = ?, attestation_hash = ?
            WHERE task_id = ?
            """,
            (
                foreign_attestation.workspace_hash,
                json.dumps(
                    foreign_attestation.execution_receipt_ids, separators=(",", ":")
                ),
                foreign_attestation.attestation_hash,
                "code-task",
            ),
        )
        self.store._connection.executemany(  # noqa: SLF001 - adversarial durability test
            """
            INSERT INTO code_task_receipt_owners
                (receipt_id, task_id, code_task_version, attestation_hash)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    receipt_id,
                    "code-task",
                    self.completed_task.version,
                    foreign_attestation.attestation_hash,
                )
                for receipt_id in foreign_receipt_ids
            ],
        )
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            """
            UPDATE events
            SET redacted_payload = ?, payload_hash = ?
            WHERE workflow_id = ? AND task_id = ? AND event_type = ?
            """,
            (
                SQLiteStore._code_task_evidence_binding_json(foreign_binding),
                foreign_binding.evidence_binding_hash,
                "workflow",
                "code-task",
                SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE,
            ),
        )

        reader = self._reader()
        atlas = self._atlas(reader)
        for rebuild in (
            lambda: reader.rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            ),
            lambda: atlas.accept(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            ),
        ):
            with pytest.raises(AtlasError) as raised:
                rebuild()

            assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_cross_snapshot_binding_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "UPDATE task_index_bindings SET output_snapshot_id = ? WHERE task_id = ?",
            (self.input_snapshot.snapshot_id, "code-task"),
        )

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"

    def test_partial_verification_evidence_is_unavailable(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "DELETE FROM task_index_verification_artifacts WHERE task_id = ?",
            ("code-task",),
        )

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"

    def test_partial_receipt_attestation_is_unavailable(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "DELETE FROM code_task_receipt_owners WHERE task_id = ?",
            ("code-task",),
        )
        self.store._connection.execute(  # noqa: SLF001 - adversarial durability test
            "DELETE FROM code_task_receipt_attestations WHERE task_id = ?",
            ("code-task",),
        )

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"

    def test_stale_workspace_snapshot_is_an_evidence_conflict(self) -> None:
        acceptance, outbox = self._accepted_identifiers()
        self.source.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")

        with pytest.raises(AtlasError) as raised:
            self._reader().rebuild(
                workflow_id="workflow",
                code_task_id="code-task",
                acceptance_id=acceptance.acceptance_id,
                ingestion_key=outbox.ingestion_key,
            )

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"


class DefaultRuntimeAcceptanceEvidenceTests(CodeTaskAcceptanceFixture):
    """The production UoW must rebuild, rather than receive, acceptance evidence."""

    _COMPLETE_IN_SETUP = True

    def _runtime_config(self) -> RuntimeConfig:
        scratch_root = self.root / "runtime-scratch"
        return RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(self.root / "runtime-data"),
                "CODEX_TASK_TEMP": str(scratch_root),
            }
        )

    def _publish_accepted_evidence_to_runtime(self, config: RuntimeConfig) -> None:
        """Seed the already-bootstrapped runtime with real immutable evidence."""

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

    @staticmethod
    def _outbox_state(config: RuntimeConfig, ingestion_key: str) -> tuple[str, int, str]:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            row = connection.execute(
                """
                SELECT state, attempt_count, last_error_code
                FROM atlas_ingestion_outbox
                WHERE ingestion_key = ?
                """,
                (ingestion_key,),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return (str(row[0]), int(row[1]), str(row[2]))

    @staticmethod
    def _delete_runtime_evidence_binding(config: RuntimeConfig) -> None:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            connection.execute(
                "DELETE FROM events WHERE workflow_id = ? AND task_id = ? AND event_type = ?",
                ("workflow", "code-task", SQLiteStore._EVIDENCE_BINDING_EVENT_TYPE),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _reset_runtime_outbox_pending(config: RuntimeConfig, ingestion_key: str) -> None:
        """Model the crash window after a durable pointer and before outbox drain."""

        connection = sqlite3.connect(config.orchestrator_database)
        try:
            cursor = connection.execute(
                "UPDATE atlas_ingestion_outbox SET state = ? WHERE ingestion_key = ?",
                ("pending", ingestion_key),
            )
            assert cursor.rowcount == 1
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _default_accept(
        config: RuntimeConfig,
        acceptance_id: str,
        ingestion_key: str,
    ) -> object:
        root = RuntimeRoot(config)
        try:
            with root.open_uow(read_only=False) as uow:
                return uow.accept_atlas(
                    "workflow", "code-task", acceptance_id, ingestion_key
                )
        finally:
            root.shutdown()

    @staticmethod
    def _freeze_runtime_evidence(
        config: RuntimeConfig,
        acceptance_id: str,
        ingestion_key: str,
    ) -> ContinuityKey:
        """Create a real frozen v2 view without projecting or publishing it."""

        root = RuntimeRoot(config)
        try:
            with root.open_uow(read_only=False) as uow:
                prepared = uow.atlas._prepare_accepted_projection(  # noqa: SLF001
                    "workflow", "code-task", acceptance_id, ingestion_key
                )
                key = uow._continuity_key(  # noqa: SLF001
                    prepared,
                    "workflow",
                    "code-task",
                    acceptance_id,
                    ingestion_key,
                )
                attempt = uow._continuity_service().claim_or_reuse(key)  # noqa: SLF001
                view, already_published = uow._freeze_or_recover(  # noqa: SLF001
                    uow._continuity_service(), attempt, prepared  # noqa: SLF001
                )
        finally:
            root.shutdown()

        assert key == attempt.key
        assert view.key == key
        assert already_published is False
        return key

    def test_default_write_uow_projects_accepted_evidence(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)

        projection = self._default_accept(
            config, acceptance.acceptance_id, outbox.ingestion_key
        )

        assert projection.acceptance_id == acceptance.acceptance_id
        assert self._outbox_state(config, outbox.ingestion_key) == ("projected", 0, "")

    def test_default_write_uow_replays_frozen_v2_without_live_dependencies(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        key = self._freeze_runtime_evidence(
            config, acceptance.acceptance_id, outbox.ingestion_key
        )
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("replay must not construct live evidence dependencies")

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    projection = uow.accept_atlas(
                        "workflow",
                        "code-task",
                        acceptance.acceptance_id,
                        outbox.ingestion_key,
                    )
        finally:
            root.shutdown()

        continuity = ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
        try:
            attempt = continuity.current_attempt(key)
            pointer = continuity.pointer_for(key)
        finally:
            continuity.close()

        assert projection.acceptance_id == acceptance.acceptance_id
        assert live_calls == []
        assert attempt is not None and attempt.state == "published"
        assert pointer is not None and pointer.view_id == attempt.view_id
        assert self._outbox_state(config, outbox.ingestion_key) == ("projected", 0, "")

    def test_default_write_uow_replays_published_v2_to_one_pending_outbox(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)
        binding = self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        assert binding is not None
        key = ContinuityKey(
            "workflow",
            "code-task",
            acceptance.code_task_version,
            acceptance.acceptance_id,
            outbox.ingestion_key,
            acceptance.payload_hash,
            binding.evidence_binding_hash,
        )
        continuity = ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
        try:
            before_attempt = continuity.current_attempt(key)
            before_pointer = continuity.pointer_for(key)
        finally:
            continuity.close()
        assert before_attempt is not None and before_attempt.state == "published"
        assert before_pointer is not None
        self._reset_runtime_outbox_pending(config, outbox.ingestion_key)
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("published replay must not consult live state or republish")

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
                mock.patch.object(ContinuityService, "publish", side_effect=deny_live),
            ):
                with root.open_uow(read_only=False) as uow:
                    projection = uow.accept_atlas(
                        "workflow",
                        "code-task",
                        acceptance.acceptance_id,
                        outbox.ingestion_key,
                    )
        finally:
            root.shutdown()

        continuity = ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
        try:
            after_attempt = continuity.current_attempt(key)
            after_pointer = continuity.pointer_for(key)
        finally:
            continuity.close()

        assert projection.acceptance_id == acceptance.acceptance_id
        assert live_calls == []
        assert after_attempt == before_attempt
        assert after_pointer == before_pointer
        assert self._outbox_state(config, outbox.ingestion_key) == ("projected", 0, "")

    def test_published_replay_stops_before_projection_when_wal_appears_after_materialize(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)
        self._reset_runtime_outbox_pending(config, outbox.ingestion_key)
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("published replay must not consult live evidence")

        original_materialize = ContinuityService.materialize_replay
        materialize_calls = 0

        def switch_to_wal_after_materialize(
            continuity: ContinuityService, key: ContinuityKey
        ) -> object:
            nonlocal materialize_calls
            replay = original_materialize(continuity, key)
            materialize_calls += 1
            if materialize_calls == 1:
                connection = sqlite3.connect(config.continuity_database)
                try:
                    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                        "wal",
                    )
                finally:
                    connection.close()
            return replay

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
                mock.patch.object(
                    ContinuityService,
                    "materialize_replay",
                    new=switch_to_wal_after_materialize,
                ),
                mock.patch.object(
                    AtlasService,
                    "_project_prepared_acceptance",
                    side_effect=AssertionError("proof must fail before Atlas projection"),
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    with pytest.raises(AtlasError) as raised:
                        uow.accept_atlas(
                            "workflow",
                            "code-task",
                            acceptance.acceptance_id,
                            outbox.ingestion_key,
                        )
        finally:
            root.shutdown()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert materialize_calls == 1
        assert live_calls == []
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_forced_candidate_miss_live_published_path_rechecks_after_projection_wal(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)
        self._reset_runtime_outbox_pending(config, outbox.ingestion_key)
        original_project = AtlasService._project_prepared_acceptance
        project_calls = 0

        def switch_to_wal_during_projection(
            atlas: AtlasService, prepared: object
        ) -> object:
            nonlocal project_calls
            projection = original_project(atlas, prepared)
            project_calls += 1
            connection = sqlite3.connect(config.continuity_database)
            try:
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                    "wal",
                )
            finally:
                connection.close()
            return projection

        root = RuntimeRoot(config)
        try:
            with (
                mock.patch.object(
                    ContinuityService, "find_replay_candidate", return_value=None
                ),
                mock.patch.object(
                    ContinuityService,
                    "publish",
                    side_effect=AssertionError("published path must not republish"),
                ),
                mock.patch.object(
                    AtlasService,
                    "_project_prepared_acceptance",
                    new=switch_to_wal_during_projection,
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    with pytest.raises(AtlasError) as raised:
                        uow.accept_atlas(
                            "workflow",
                            "code-task",
                            acceptance.acceptance_id,
                            outbox.ingestion_key,
                        )
        finally:
            root.shutdown()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert project_calls == 1
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_frozen_replay_stops_before_projection_when_wal_appears_after_materialize(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self._freeze_runtime_evidence(
            config, acceptance.acceptance_id, outbox.ingestion_key
        )
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("frozen replay must not consult live evidence")

        original_materialize = ContinuityService.materialize_replay
        materialize_calls = 0

        def switch_to_wal_after_materialize(
            continuity: ContinuityService, key: ContinuityKey
        ) -> object:
            nonlocal materialize_calls
            replay = original_materialize(continuity, key)
            materialize_calls += 1
            if materialize_calls == 1:
                connection = sqlite3.connect(config.continuity_database)
                try:
                    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                        "wal",
                    )
                finally:
                    connection.close()
            return replay

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
                mock.patch.object(
                    ContinuityService,
                    "materialize_replay",
                    new=switch_to_wal_after_materialize,
                ),
                mock.patch.object(
                    AtlasService,
                    "_project_prepared_acceptance",
                    side_effect=AssertionError("proof must fail before Atlas projection"),
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    with pytest.raises(AtlasError) as raised:
                        uow.accept_atlas(
                            "workflow",
                            "code-task",
                            acceptance.acceptance_id,
                            outbox.ingestion_key,
                        )
        finally:
            root.shutdown()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert materialize_calls == 1
        assert live_calls == []
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_frozen_replay_refuses_to_publish_when_projection_switches_to_wal(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        key = self._freeze_runtime_evidence(
            config, acceptance.acceptance_id, outbox.ingestion_key
        )
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("frozen replay must not consult live evidence")

        original_project = AtlasService._project_prepared_acceptance
        project_calls = 0

        def switch_to_wal_during_projection(
            atlas: AtlasService, prepared: object
        ) -> object:
            nonlocal project_calls
            projection = original_project(atlas, prepared)
            project_calls += 1
            connection = sqlite3.connect(config.continuity_database)
            try:
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                    "wal",
                )
            finally:
                connection.close()
            return projection

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
                mock.patch.object(
                    AtlasService,
                    "_project_prepared_acceptance",
                    new=switch_to_wal_during_projection,
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    with pytest.raises(AtlasError) as raised:
                        uow.accept_atlas(
                            "workflow",
                            "code-task",
                            acceptance.acceptance_id,
                            outbox.ingestion_key,
                        )
        finally:
            root.shutdown()

        connection = sqlite3.connect(config.continuity_database)
        try:
            attempt = connection.execute(
                "SELECT state FROM attempts WHERE key_hash=? "
                "ORDER BY sequence DESC LIMIT 1",
                (key.key_hash,),
            ).fetchone()
            pointer = connection.execute(
                "SELECT view_id FROM pointers WHERE key_hash=?", (key.key_hash,)
            ).fetchone()
        finally:
            connection.close()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert project_calls == 1
        assert live_calls == []
        assert attempt == ("frozen",)
        assert pointer is None
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_frozen_replay_same_view_race_fails_closed_when_wal_precedes_recovery_proof(
        self,
    ) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        key = self._freeze_runtime_evidence(
            config, acceptance.acceptance_id, outbox.ingestion_key
        )
        live_calls: list[str] = []

        def deny_live(*_args: object, **_kwargs: object) -> object:
            live_calls.append("live dependency")
            raise AssertionError("frozen replay must not consult live evidence")

        original_publish = ContinuityService.publish
        original_project = AtlasService._project_prepared_acceptance
        project_calls = 0
        publish_calls = 0

        def publish_same_view_then_switch_to_wal(
            continuity: ContinuityService, attempt: object, view: object
        ) -> object:
            nonlocal publish_calls
            original_publish(continuity, attempt, view)
            publish_calls += 1
            connection = sqlite3.connect(config.continuity_database)
            try:
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                    "wal",
                )
            finally:
                connection.close()
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")

        def count_projection(atlas: AtlasService, prepared: object) -> object:
            nonlocal project_calls
            projection = original_project(atlas, prepared)
            project_calls += 1
            return projection

        factories = replace(
            DEFAULT_RUNTIME_ADAPTER_FACTORIES,
            open_project_checkpoint=deny_live,
            build_atlas=deny_live,
        )
        root = RuntimeRoot(config, adapter_factories=factories)
        try:
            with (
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "rebuild", side_effect=deny_live
                ),
                mock.patch.object(
                    ProductionAcceptanceEvidenceReader, "read", side_effect=deny_live
                ),
                mock.patch.object(
                    ProjectIndexService, "read_snapshot_files", side_effect=deny_live
                ),
                mock.patch.object(
                    ContinuityService,
                    "publish",
                    new=publish_same_view_then_switch_to_wal,
                ),
                mock.patch.object(
                    AtlasService,
                    "_project_prepared_acceptance",
                    new=count_projection,
                ),
            ):
                with root.open_uow(read_only=False) as uow:
                    with pytest.raises(AtlasError) as raised:
                        uow.accept_atlas(
                            "workflow",
                            "code-task",
                            acceptance.acceptance_id,
                            outbox.ingestion_key,
                        )
        finally:
            root.shutdown()

        connection = sqlite3.connect(config.continuity_database)
        try:
            attempt = connection.execute(
                "SELECT state FROM attempts WHERE key_hash=? "
                "ORDER BY sequence DESC LIMIT 1",
                (key.key_hash,),
            ).fetchone()
            pointer = connection.execute(
                "SELECT view_id FROM pointers WHERE key_hash=?", (key.key_hash,)
            ).fetchone()
        finally:
            connection.close()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert project_calls == 1
        assert publish_calls == 1
        assert live_calls == []
        assert attempt == ("published",)
        assert pointer is not None
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_default_write_uow_publishes_a_fenced_continuity_view_first(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        binding = self.store.evidence_binding_for_acceptance(acceptance.acceptance_id)
        assert binding is not None

        self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)

        key = ContinuityKey(
            "workflow",
            "code-task",
            acceptance.code_task_version,
            acceptance.acceptance_id,
            outbox.ingestion_key,
            acceptance.payload_hash,
            binding.evidence_binding_hash,
        )
        continuity = ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
        try:
            attempt = continuity.current_attempt(key)
            pointer = continuity.pointer_for(key)
        finally:
            continuity.close()

        assert attempt is not None and attempt.state == "published"
        assert pointer is not None
        assert pointer.view_id == attempt.view_id
        assert pointer.fence_epoch == attempt.fence_epoch
        assert self._outbox_state(config, outbox.ingestion_key) == ("projected", 0, "")

    def test_default_write_uow_retries_unavailable_evidence(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self._delete_runtime_evidence_binding(config)

        with pytest.raises(AtlasError) as raised:
            self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "pending",
            1,
            "ATLAS_EVIDENCE_UNAVAILABLE",
        )

    def test_default_write_uow_quarantines_conflicting_evidence(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        self.source.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")

        with pytest.raises(AtlasError) as raised:
            self._default_accept(config, acceptance.acceptance_id, outbox.ingestion_key)

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
        assert self._outbox_state(config, outbox.ingestion_key) == (
            "quarantined",
            0,
            "ATLAS_EVIDENCE_CONFLICT",
        )

    def test_default_write_uow_does_not_construct_orchestrator_schema(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)

        with mock.patch.object(
            SQLiteStore,
            "__init__",
            side_effect=AssertionError("ordinary UoW must not create schema"),
        ):
            projection = self._default_accept(
                config, acceptance.acceptance_id, outbox.ingestion_key
            )

        assert projection.acceptance_id == acceptance.acceptance_id

    def test_default_write_uow_does_not_transition_an_unknown_ingestion_key(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        unknown_key = "sha256:" + "0" * 64

        with pytest.raises(AtlasError) as raised:
            self._default_accept(config, acceptance.acceptance_id, unknown_key)

        assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
        assert self._outbox_state(config, outbox.ingestion_key) == ("pending", 0, "")

    def test_default_read_uow_keeps_accepted_outbox_unchanged(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)

        root = RuntimeRoot(config)
        try:
            with root.open_uow(read_only=True) as uow:
                with pytest.raises(AtlasError) as raised:
                    uow.atlas.accept(
                        "workflow",
                        "code-task",
                        acceptance.acceptance_id,
                        outbox.ingestion_key,
                    )
        finally:
            root.shutdown()

        assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
        assert self._outbox_state(config, outbox.ingestion_key) == ("pending", 0, "")

    def test_default_uow_closes_its_orchestrator_store(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        root = RuntimeRoot(config)
        uow = root.open_uow(read_only=False)
        try:
            projection = uow.accept_atlas(
                "workflow", "code-task", acceptance.acceptance_id, outbox.ingestion_key
            )
            store = uow._orchestrator_store  # noqa: SLF001 - ownership regression
            reader = uow._acceptance_evidence_reader  # noqa: SLF001 - ownership regression
            assert reader._orchestrator._store is store  # noqa: SLF001
            assert store._connection is not None  # noqa: SLF001
        finally:
            uow.close()
            root.shutdown()

        assert projection.acceptance_id == acceptance.acceptance_id
        assert store._connection is None  # noqa: SLF001

    def test_public_atlas_accept_drains_the_matching_outbox(self) -> None:
        acceptance, outbox = self._accept()
        config = self._runtime_config()
        self._publish_accepted_evidence_to_runtime(config)
        root = RuntimeRoot(config)
        try:
            with mock.patch.object(server_module, "_runtime_root", return_value=root):
                result = server_module.atlas_accept(
                    "workflow",
                    "code-task",
                    acceptance.acceptance_id,
                    outbox.ingestion_key,
                )
        finally:
            root.shutdown()

        assert result["ok"] is True
        assert result["data"]["acceptance_id"] == acceptance.acceptance_id
        assert self._outbox_state(config, outbox.ingestion_key) == ("projected", 0, "")
