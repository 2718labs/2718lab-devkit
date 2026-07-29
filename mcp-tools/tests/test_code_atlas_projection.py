"""Red-first contracts for deterministic accepted-code Atlas projection."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.canonical import canonical_hash, canonical_json
from code_atlas.extractors import BoundExecutionReceipt, ExtractionRequest
from code_atlas.models import AtlasError, AtlasStatus, EdgeRelation
from code_atlas.recipes import BundledRecipeLoader
from code_atlas.service import (
    AcceptedCodeProjectionEvidence,
    AcceptedCodeProjectionRequest,
    CodeAtlasService,
)
from code_atlas.store import AtlasStore, StoreConflictError
from project_index.checkpoints import CheckpointFile
from project_index.models import SnapshotFile
from project_index.service import ProjectIndexService


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "skills" / "code-atlas" / "assets"
_GOLDEN_BINDING_HASH = (
    "sha256:571cd221acb42b2b73630e2c1c28ba0abefbe838d9e59019c2e55d294c74f208"
)
_GOLDEN_BINDING_JSON = (
    '{"checkpoint_hash":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    '"checkpoint_id":"checkpoint-1","code_task_id":"code-task",'
    '"code_task_version":3,"execution_receipt_ids":['
    '"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
    '"sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"],'
    '"indexed_diff_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"input_snapshot_id":"input-snapshot","output_query_trace_id":"trace-output",'
    '"output_snapshot_id":"output-snapshot",'
    '"schema_version":"acceptance-evidence-binding/v1",'
    '"verification_artifact_hashes":['
    '"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
    '"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"],'
    '"workflow_id":"workflow"}'
)


def _hash(value: str | bytes) -> str:
    body = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _request(
    *,
    intent_id: str = "python.acceptance-projection",
    language: str = "python",
    code_task_id: str = "task-1",
) -> AcceptedCodeProjectionRequest:
    return AcceptedCodeProjectionRequest.create(
        workflow_id="workflow-1",
        code_task_id=code_task_id,
        code_task_version=3,
        input_snapshot_id=_hash("snapshot-before"),
        output_snapshot_id=_hash("snapshot-after"),
        indexed_diff_hash=_hash("indexed-diff"),
        intent_id=intent_id,
        language=language,
        framework="",
        checkpoint_id="checkpoint-1",
        checkpoint_hash=_hash("checkpoint"),
        output_query_trace_id=_hash("output-query"),
        verification_artifact_hashes=(_hash("verification-artifact"),),
        execution_receipt_ids=tuple(
            sorted((_hash("receipt-command"), _hash("receipt-write")))
        ),
    )


def test_evidence_binding_matches_the_frozen_10d_golden_vector() -> None:
    """Keep ATLAS-10B request hashing byte-for-byte compatible with ATLAS-10D."""

    artifact_hashes = (
        "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    )
    receipt_ids = (
        "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )
    payload = {
        "checkpoint_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "checkpoint_id": "checkpoint-1",
        "code_task_id": "code-task",
        "code_task_version": 3,
        "execution_receipt_ids": list(receipt_ids),
        "indexed_diff_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "input_snapshot_id": "input-snapshot",
        "output_query_trace_id": "trace-output",
        "output_snapshot_id": "output-snapshot",
        "schema_version": "acceptance-evidence-binding/v1",
        "verification_artifact_hashes": list(artifact_hashes),
        "workflow_id": "workflow",
    }
    request = AcceptedCodeProjectionRequest.create(
        workflow_id="workflow",
        code_task_id="code-task",
        code_task_version=3,
        input_snapshot_id="input-snapshot",
        output_snapshot_id="output-snapshot",
        indexed_diff_hash="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        intent_id="python.golden-vector",
        language="python",
        framework="",
        checkpoint_id="checkpoint-1",
        checkpoint_hash="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        output_query_trace_id="trace-output",
        verification_artifact_hashes=artifact_hashes,
        execution_receipt_ids=receipt_ids,
    )

    assert canonical_json(payload) == _GOLDEN_BINDING_JSON
    assert canonical_hash(payload) == _GOLDEN_BINDING_HASH
    assert request.evidence_binding_hash == _GOLDEN_BINDING_HASH


def _binding_hash(
    *,
    kind: str,
    request: ExtractionRequest,
    snapshot_id: str,
    files: tuple[CheckpointFile, ...] | tuple[SnapshotFile, ...],
) -> str:
    return canonical_hash(
        {
            "kind": kind,
            "workflow_id": request.workflow_id,
            "task_id": request.task_id,
            "acceptance_id": request.acceptance_id,
            "workspace_hash": request.workspace_hash,
            "checkpoint_id": request.checkpoint_id,
            "snapshot_id": snapshot_id,
            "write_scope": sorted(request.write_scope),
            "files": sorted([[item.path, item.content_hash] for item in files]),
        }
    )


def _evidence(
    request: AcceptedCodeProjectionRequest,
    *,
    after_path: str = "src/created.py",
    after_body: bytes = b"def created() -> int:\n    return 1\n",
) -> AcceptedCodeProjectionEvidence:
    after = (SnapshotFile(after_path, _hash(after_body), after_body),)
    raw = ExtractionRequest(
        workflow_id=request.workflow_id,
        task_id=request.code_task_id,
        acceptance_id=request.acceptance_id,
        task_kind="code",
        intent_id=request.intent_id,
        workspace_hash=_hash("workspace-1"),
        checkpoint_id=request.checkpoint_id,
        input_snapshot_id=request.input_snapshot_id,
        output_snapshot_id=request.output_snapshot_id,
        write_scope=("src",),
        before_files=(),
        after_files=after,
        changed_nodes=(),
        coverage_gaps=(),
        execution_receipts=(),
    )
    command_id = _hash("receipt-command")
    write_id = _hash("receipt-write")
    receipts_by_id = {
        command_id: BoundExecutionReceipt(
            receipt_id=command_id,
            kind="command",
            workflow_id=raw.workflow_id,
            task_id=raw.task_id,
            acceptance_id=raw.acceptance_id,
            workspace_hash=raw.workspace_hash,
            output_snapshot_id=raw.output_snapshot_id,
            command_spec=("python", "-m", "pytest"),
            command_spec_hash=canonical_hash(("python", "-m", "pytest")),
            input_hash=_binding_hash(
                kind="atlas-extraction-input-v1",
                request=raw,
                snapshot_id=raw.input_snapshot_id,
                files=raw.before_files,
            ),
            output_hash=_binding_hash(
                kind="atlas-extraction-output-v1",
                request=raw,
                snapshot_id=raw.output_snapshot_id,
                files=raw.after_files,
            ),
            exit_code=0,
            success=True,
        ),
        write_id: BoundExecutionReceipt(
            receipt_id=write_id,
            kind="write",
            workflow_id=raw.workflow_id,
            task_id=raw.task_id,
            acceptance_id=raw.acceptance_id,
            workspace_hash=raw.workspace_hash,
            output_snapshot_id=raw.output_snapshot_id,
            command_spec=(),
            command_spec_hash=canonical_hash(()),
            input_hash=_binding_hash(
                kind="atlas-extraction-input-v1",
                request=raw,
                snapshot_id=raw.input_snapshot_id,
                files=raw.before_files,
            ),
            output_hash=_binding_hash(
                kind="atlas-extraction-output-v1",
                request=raw,
                snapshot_id=raw.output_snapshot_id,
                files=raw.after_files,
            ),
            exit_code=0,
            success=True,
        ),
    }
    receipts = tuple(
        receipts_by_id[receipt_id] for receipt_id in request.execution_receipt_ids
    )
    return AcceptedCodeProjectionEvidence(
        code_task_version=request.code_task_version,
        language=request.language,
        framework=request.framework,
        checkpoint_hash=request.checkpoint_hash,
        indexed_diff_hash=request.indexed_diff_hash,
        output_query_trace_id=request.output_query_trace_id,
        verification_artifact_hashes=request.verification_artifact_hashes,
        extraction_request=replace(raw, execution_receipts=receipts),
    )


class _Reader:
    def __init__(self, evidence: AcceptedCodeProjectionEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def read(
        self, request: AcceptedCodeProjectionRequest
    ) -> AcceptedCodeProjectionEvidence:
        self.calls += 1
        return self.evidence


def _service_at(
    tmp_path: Path,
    reader: _Reader | None,
) -> tuple[CodeAtlasService, AtlasStore, ProjectIndexService]:
    store = AtlasStore(tmp_path / "atlas.sqlite", tmp_path / "cas")
    index = ProjectIndexService(tmp_path / "project-index.sqlite")
    service = CodeAtlasService(
        store,
        BundledRecipeLoader(ASSETS),
        index,
        acceptance_evidence_reader=reader,
    )
    return service, store, index


def _counts(store: AtlasStore) -> tuple[int, int, int, int, int]:
    return tuple(
        int(store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in (
            "atlas_nodes",
            "atlas_edges",
            "atlas_recipes",
            "atlas_blobs",
            "atlas_ingestion_receipts",
        )
    )


def test_project_acceptance_persists_a_recipe_once_and_redacts_output(
    tmp_path: Path,
) -> None:
    request = _request()
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)

    projection = service.project_acceptance(request)

    assert projection.acceptance_id == request.acceptance_id
    assert projection.code_task_id == request.code_task_id
    assert projection.output_snapshot_id == request.output_snapshot_id
    assert projection.atlas_ingest_state is AtlasStatus.READY
    assert projection.recipe_id
    assert projection.reasons == ()
    receipt = store.get_ingestion_receipt(request.ingestion_key)
    assert receipt is not None
    assert receipt.payload_hash == request.payload_hash
    assert receipt.recipe_id == projection.recipe_id
    assert _counts(store)[2:] == (1, 1, 1)
    assert "return 1" not in canonical_json(projection.to_dict())
    graph = store.graph_query(
        (projection.episode_id,),
        max_nodes=200,
        max_edges=400,
        max_depth=2,
        byte_budget=524_288,
        node_kinds=None,
        relations=None,
    )
    assert any(
        node.kind.value == "SourceEvidence"
        and request.evidence_binding_hash in node.source_hashes
        for node in graph.nodes
    )
    binding = next(
        node
        for node in graph.nodes
        if node.kind.value == "SourceEvidence"
        and request.evidence_binding_hash in node.source_hashes
    )
    assert any(
        edge.relation is EdgeRelation.CHANGES
        and edge.source_id == projection.episode_id
        and edge.target_id == binding.node_id
        for edge in graph.edges
    )
    assert any(
        edge.relation is EdgeRelation.DERIVED_FROM
        and edge.source_id == projection.recipe_id
        and edge.target_id == binding.node_id
        for edge in graph.edges
    )

    assert service.project_acceptance(request) == projection
    assert reader.calls == 2
    assert _counts(store)[2:] == (1, 1, 1)
    store.close()
    index.close()


def test_project_acceptance_revalidates_evidence_for_an_existing_receipt(
    tmp_path: Path,
) -> None:
    request = _request()
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)
    projection = service.project_acceptance(request)
    before = _counts(store)

    service._acceptance_evidence_reader = _Reader(
        replace(reader.evidence, checkpoint_hash=_hash("changed-checkpoint"))
    )
    with pytest.raises(AtlasError, match="acceptance_evidence_conflict"):
        service.project_acceptance(request)
    assert _counts(store) == before

    service._acceptance_evidence_reader = None
    with pytest.raises(AtlasError, match="acceptance_evidence_unavailable"):
        service.project_acceptance(request)
    assert _counts(store) == before
    receipt = store.get_ingestion_receipt(request.ingestion_key)
    assert receipt is not None
    assert (receipt.status, receipt.episode_id, receipt.recipe_id, receipt.reasons) == (
        projection.atlas_ingest_state,
        projection.episode_id,
        projection.recipe_id,
        projection.reasons,
    )
    store.close()
    index.close()


def test_project_acceptance_reuses_recipe_for_distinct_accepted_episodes(
    tmp_path: Path,
) -> None:
    first = _request()
    second = _request(code_task_id="task-2")
    first_reader = _Reader(_evidence(first))
    service, store, index = _service_at(tmp_path, first_reader)

    first_projection = service.project_acceptance(first)
    service._acceptance_evidence_reader = _Reader(_evidence(second))
    second_projection = service.project_acceptance(second)

    assert first_projection.recipe_id == second_projection.recipe_id
    assert first_projection.episode_id != second_projection.episode_id
    assert _counts(store)[2:] == (1, 1, 2)
    store.close()
    index.close()


def test_project_acceptance_records_episode_only_for_unsupported_language(
    tmp_path: Path,
) -> None:
    request = _request(language="typescript")
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)

    projection = service.project_acceptance(request)

    assert projection.atlas_ingest_state is AtlasStatus.UNSUPPORTED_LANGUAGE
    assert projection.recipe_id is None
    assert projection.reasons == ("UNSUPPORTED_LANGUAGE",)
    assert _counts(store)[2:] == (0, 0, 1)
    store.close()
    index.close()


def test_project_acceptance_conflicts_for_same_key_and_changed_payload(
    tmp_path: Path,
) -> None:
    request = _request()
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)
    service.project_acceptance(request)
    before = _counts(store)
    changed = replace(
        _request(intent_id="python.changed"), ingestion_key=request.ingestion_key
    )

    with pytest.raises(StoreConflictError, match="ingestion receipt"):
        service.project_acceptance(changed)
    changed_binding = AcceptedCodeProjectionRequest.create(
        workflow_id=request.workflow_id,
        code_task_id=request.code_task_id,
        code_task_version=request.code_task_version,
        input_snapshot_id=request.input_snapshot_id,
        output_snapshot_id=request.output_snapshot_id,
        indexed_diff_hash=request.indexed_diff_hash,
        intent_id=request.intent_id,
        language=request.language,
        framework=request.framework,
        checkpoint_id="checkpoint-other",
        checkpoint_hash=_hash("checkpoint-other"),
        output_query_trace_id=request.output_query_trace_id,
        verification_artifact_hashes=request.verification_artifact_hashes,
        execution_receipt_ids=request.execution_receipt_ids,
    )
    service._acceptance_evidence_reader = _Reader(_evidence(changed_binding))
    with pytest.raises(StoreConflictError, match="evidence binding"):
        service.project_acceptance(changed_binding)
    assert reader.calls == 1
    assert _counts(store) == before
    store.close()
    index.close()


def test_project_acceptance_rejects_untrusted_request_or_evidence(
    tmp_path: Path,
) -> None:
    request = _request()
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)

    with pytest.raises(AtlasError, match="invalid_acceptance_projection"):
        service.project_acceptance({"request": "raw-source-body"})  # type: ignore[arg-type]
    with pytest.raises(AtlasError, match="invalid_acceptance_projection"):
        service.project_acceptance(
            replace(
                request,
                execution_receipt_ids=("receipt-command", "receipt-write"),
            )
        )
    bad = replace(reader.evidence, checkpoint_hash=_hash("other-checkpoint"))
    service._acceptance_evidence_reader = _Reader(bad)
    with pytest.raises(AtlasError, match="acceptance_evidence_conflict"):
        service.project_acceptance(request)
    for evidence in (
        replace(reader.evidence, code_task_version=request.code_task_version + 1),
        replace(reader.evidence, language="typescript"),
        replace(reader.evidence, framework="fastapi"),
    ):
        service._acceptance_evidence_reader = _Reader(evidence)
        with pytest.raises(AtlasError, match="acceptance_evidence_conflict"):
            service.project_acceptance(request)
    assert _counts(store) == (0, 0, 0, 0, 0)
    store.close()
    index.close()

    unavailable_store = AtlasStore(
        tmp_path / "unavailable.sqlite", tmp_path / "unavailable-cas"
    )
    unavailable_index = ProjectIndexService(tmp_path / "unavailable-index.sqlite")
    unavailable = CodeAtlasService(
        unavailable_store,
        BundledRecipeLoader(ASSETS),
        unavailable_index,
    )
    with pytest.raises(AtlasError, match="acceptance_evidence_unavailable"):
        unavailable.project_acceptance(request)
    unavailable_store.close()
    unavailable_index.close()


def test_project_acceptance_rolls_back_the_bundle_on_interrupted_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    reader = _Reader(_evidence(request))
    service, store, index = _service_at(tmp_path, reader)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise StoreConflictError("forced projection failure")

    monkeypatch.setattr(store, "_bundle_insert_receipt", fail)
    with pytest.raises(StoreConflictError, match="forced projection failure"):
        service.project_acceptance(request)
    assert _counts(store) == (0, 0, 0, 0, 0)
    assert not tuple((tmp_path / "cas").rglob("*.tmp"))
    store.close()
    index.close()
