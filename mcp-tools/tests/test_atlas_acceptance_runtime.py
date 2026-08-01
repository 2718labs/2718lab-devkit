"""Production acceptance reconstruction coverage."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from test_atlas_acceptance import CodeTaskAcceptanceFixture  # noqa: E402

from devkit_atlas import ASSET_ROOT  # noqa: E402
from devkit_atlas.canonical import canonical_json  # noqa: E402
from devkit_atlas.models import AtlasError  # noqa: E402
from devkit_atlas.recipes import BundledRecipeLoader  # noqa: E402
from devkit_atlas.service import AtlasService  # noqa: E402
from devkit_atlas.store import AtlasStore  # noqa: E402
from devkit_runtime.atlas_acceptance import (  # noqa: E402
    ProductionAcceptanceEvidenceReader,
)
from orchestrator.store import SQLiteStore  # noqa: E402


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

    def test_cross_workspace_binding_is_an_evidence_conflict(self) -> None:
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
