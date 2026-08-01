"""Immutable-evidence reconstruction for the public Atlas acceptance path."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from hmac import compare_digest
from pathlib import PurePosixPath
from typing import NoReturn

from devkit_atlas.canonical import canonical_hash
from devkit_atlas.extractors import BoundExecutionReceipt, ExtractionRequest
from devkit_atlas.models import AtlasError
from devkit_atlas.receipts import (
    RawExecutionReceipt,
    ReceiptIntegrityError,
    ReceiptRepository,
)
from devkit_atlas.security import MAX_CHANGED_FILES, MAX_PACKET_BYTES
from devkit_atlas.service import (
    AcceptedAtlasProjectionEvidence,
    AcceptedAtlasProjectionRequest,
)
from orchestrator.service import OrchestratorService, ServiceError
from orchestrator.store import AcceptedCodeTaskEvidence
from project_index.checkpoints import Checkpoint, CheckpointFile, CheckpointService
from project_index.models import (
    IndexError,
    QueryReceipt,
    SnapshotDiff,
    SnapshotFacts,
    SnapshotFile,
)
from project_index.service import ProjectIndexService

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_RECEIPT_HASH_FIELDS = (
    "session_id_hash",
    "turn_id_hash",
    "command_spec_hash",
    "input_hash",
    "output_hash",
    "workspace_hash",
)


def _unavailable() -> NoReturn:
    raise AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")


def _conflict() -> NoReturn:
    raise AtlasError("ATLAS_EVIDENCE_CONFLICT")


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _require_identifier(value: object, *, hashed: bool = False) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _conflict()
    if hashed and not _is_hash(value):
        _conflict()
    return value


def _scope(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        _conflict()
    paths: list[str] = []
    for item in value:
        if type(item) is not str or "\\" in item:
            _conflict()
        candidate = PurePosixPath(item)
        if (
            not item
            or candidate.is_absolute()
            or ".." in candidate.parts
            or str(candidate) != item
        ):
            _conflict()
        paths.append(item)
    if tuple(sorted(paths)) != tuple(paths) or len(set(paths)) != len(paths):
        _conflict()
    return tuple(paths)


def _within_scope(path: object, write_scope: tuple[str, ...]) -> bool:
    if type(path) is not str or "\\" in path:
        return False
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        return False
    return any(
        candidate == PurePosixPath(scope) or PurePosixPath(scope) in candidate.parents
        for scope in write_scope
    )


def _diff_hash(indexed_diff: SnapshotDiff) -> str:
    try:
        payload = asdict(indexed_diff)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _extraction_binding_hash(
    *,
    kind: str,
    request: ExtractionRequest,
    snapshot_id: str,
    files: tuple[CheckpointFile | SnapshotFile, ...],
) -> str:
    try:
        file_values = sorted(
            [[str(item.path), str(item.content_hash)] for item in files],
            key=lambda item: (item[0], item[1]),
        )
    except AttributeError as error:
        raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error
    return canonical_hash(
        {
            "kind": kind,
            "workflow_id": request.workflow_id,
            "task_id": request.task_id,
            "acceptance_id": request.acceptance_id,
            "workspace_hash": request.workspace_hash,
            "checkpoint_id": request.checkpoint_id,
            "snapshot_id": snapshot_id,
            "write_scope": list(request.write_scope),
            "files": file_values,
        }
    )


class ProductionAcceptanceEvidenceReader:
    """Rebuild an Atlas projection request from immutable accepted-task evidence."""

    def __init__(
        self,
        orchestrator: OrchestratorService,
        project_index: ProjectIndexService,
        checkpoints: CheckpointService,
        receipts: ReceiptRepository,
    ) -> None:
        self._orchestrator = orchestrator
        self._project_index = project_index
        self._checkpoints = checkpoints
        self._receipts = receipts

    def rebuild(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> AcceptedAtlasProjectionRequest:
        """Construct the internal request from exactly four public identifiers."""

        request, _ = self._rebuild(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            acceptance_id=acceptance_id,
            ingestion_key=ingestion_key,
        )
        return request

    def read(
        self, request: AcceptedAtlasProjectionRequest
    ) -> AcceptedAtlasProjectionEvidence:
        """Re-read immutable evidence before the existing internal projection path."""

        if type(request) is not AcceptedAtlasProjectionRequest:
            raise AtlasError("acceptance_evidence_conflict")
        try:
            rebuilt, evidence = self._rebuild(
                workflow_id=request.workflow_id,
                code_task_id=request.code_task_id,
                acceptance_id=request.acceptance_id,
                ingestion_key=request.ingestion_key,
            )
        except AtlasError as error:
            if error.code == "ATLAS_EVIDENCE_UNAVAILABLE":
                raise AtlasError("acceptance_evidence_unavailable") from error
            raise AtlasError("acceptance_evidence_conflict") from error
        if rebuilt != request:
            raise AtlasError("acceptance_evidence_conflict")
        return evidence

    def _rebuild(
        self,
        *,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> tuple[AcceptedAtlasProjectionRequest, AcceptedAtlasProjectionEvidence]:
        _require_identifier(workflow_id)
        _require_identifier(code_task_id)
        _require_identifier(acceptance_id, hashed=True)
        _require_identifier(ingestion_key, hashed=True)
        durable = self._durable_evidence(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            acceptance_id=acceptance_id,
            ingestion_key=ingestion_key,
        )
        acceptance = durable.acceptance
        binding = durable.evidence_binding
        request = AcceptedAtlasProjectionRequest.create(
            workflow_id=acceptance.workflow_id,
            code_task_id=acceptance.code_task_id,
            code_task_version=acceptance.code_task_version,
            input_snapshot_id=acceptance.input_snapshot_id,
            output_snapshot_id=acceptance.output_snapshot_id,
            indexed_diff_hash=acceptance.indexed_diff_hash,
            intent_id=acceptance.intent_id,
            language=acceptance.language,
            framework=acceptance.framework,
            checkpoint_id=binding.checkpoint_id,
            checkpoint_hash=binding.checkpoint_hash,
            output_query_trace_id=binding.output_query_trace_id,
            verification_artifact_hashes=binding.verification_artifact_hashes,
            execution_receipt_ids=binding.execution_receipt_ids,
        )
        if (
            request.acceptance_id != acceptance_id
            or request.ingestion_key != ingestion_key
            or request.payload_hash != acceptance.payload_hash
            or request.evidence_binding_hash != binding.evidence_binding_hash
        ):
            _conflict()

        extraction = self._extraction_request(durable, request)
        evidence = AcceptedAtlasProjectionEvidence(
            code_task_version=request.code_task_version,
            language=request.language,
            framework=request.framework,
            checkpoint_hash=request.checkpoint_hash,
            indexed_diff_hash=request.indexed_diff_hash,
            output_query_trace_id=request.output_query_trace_id,
            verification_artifact_hashes=request.verification_artifact_hashes,
            extraction_request=extraction,
        )
        return request, evidence

    def _durable_evidence(
        self,
        *,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> AcceptedCodeTaskEvidence:
        if type(self._orchestrator) is not OrchestratorService:
            _conflict()
        try:
            evidence = self._orchestrator.accepted_code_task_evidence(
                workflow_id,
                code_task_id,
                acceptance_id,
                ingestion_key,
            )
        except ServiceError as error:
            if error.code in {"ACCEPTANCE_CONFLICT", "INDEX_CORRUPT"}:
                _conflict()
            _unavailable()
        except (TypeError, ValueError, KeyError):
            _conflict()
        if evidence is None:
            _unavailable()
        if type(evidence) is not AcceptedCodeTaskEvidence:
            _conflict()
        return evidence

    def _extraction_request(
        self,
        durable: AcceptedCodeTaskEvidence,
        request: AcceptedAtlasProjectionRequest,
    ) -> ExtractionRequest:
        if (
            type(self._project_index) is not ProjectIndexService
            or type(self._checkpoints) is not CheckpointService
            or type(self._receipts) is not ReceiptRepository
        ):
            _conflict()
        task = durable.task
        binding = durable.index_binding
        evidence_binding = durable.evidence_binding
        attestation = durable.receipt_attestation
        write_scope = _scope(task.write_scope)
        if (
            binding.input_snapshot_id != request.input_snapshot_id
            or binding.output_snapshot_id != request.output_snapshot_id
            or binding.indexed_diff_hash != request.indexed_diff_hash
            or evidence_binding.checkpoint_id != binding.checkpoint_id
            or attestation.workspace_hash is None
            or not _is_hash(attestation.workspace_hash)
        ):
            _conflict()
        workspace_id = _require_identifier(binding.workspace_id, hashed=True)
        expected_workspace_hash = self._receipt_workspace_hash(workspace_id)
        if not compare_digest(attestation.workspace_hash, expected_workspace_hash):
            _conflict()

        checkpoint = self._checkpoint(workspace_id, request, write_scope)
        facts, indexed_diff = self._index_facts(workspace_id, request, write_scope)
        changed_paths = self._changed_paths(indexed_diff, write_scope)
        before_files, after_files = self._source_files(
            workspace_id,
            checkpoint,
            request,
            changed_paths,
            facts,
        )
        self._query_receipt(request.output_query_trace_id, request.output_snapshot_id)

        raw = ExtractionRequest(
            workflow_id=request.workflow_id,
            task_id=request.code_task_id,
            acceptance_id=request.acceptance_id,
            task_kind="code",
            intent_id=request.intent_id,
            workspace_hash=attestation.workspace_hash,
            checkpoint_id=request.checkpoint_id,
            input_snapshot_id=request.input_snapshot_id,
            output_snapshot_id=request.output_snapshot_id,
            write_scope=write_scope,
            before_files=before_files,
            after_files=after_files,
            changed_nodes=tuple(
                node for node in facts.nodes if node.path in changed_paths
            ),
            coverage_gaps=tuple(
                gap for gap in facts.gaps if _within_scope(gap.path, write_scope)
            ),
            execution_receipts=(),
        )
        input_hash = _extraction_binding_hash(
            kind="atlas-extraction-input-v1",
            request=raw,
            snapshot_id=request.input_snapshot_id,
            files=tuple(before_files),
        )
        output_hash = _extraction_binding_hash(
            kind="atlas-extraction-output-v1",
            request=raw,
            snapshot_id=request.output_snapshot_id,
            files=tuple(after_files),
        )
        receipts = tuple(
            self._bound_receipt(
                receipt_id,
                request=request,
                workspace_hash=attestation.workspace_hash,
                input_hash=input_hash,
                output_hash=output_hash,
            )
            for receipt_id in request.execution_receipt_ids
        )
        if {receipt.kind for receipt in receipts} != {"command", "write"}:
            _conflict()
        return ExtractionRequest(
            workflow_id=raw.workflow_id,
            task_id=raw.task_id,
            acceptance_id=raw.acceptance_id,
            task_kind=raw.task_kind,
            intent_id=raw.intent_id,
            workspace_hash=raw.workspace_hash,
            checkpoint_id=raw.checkpoint_id,
            input_snapshot_id=raw.input_snapshot_id,
            output_snapshot_id=raw.output_snapshot_id,
            write_scope=raw.write_scope,
            before_files=raw.before_files,
            after_files=raw.after_files,
            changed_nodes=raw.changed_nodes,
            coverage_gaps=raw.coverage_gaps,
            execution_receipts=receipts,
        )

    def _receipt_workspace_hash(self, workspace_id: str) -> str:
        try:
            workspace_root = self._project_index.workspace_authority.resolve(
                workspace_id
            ).root
        except IndexError as error:
            self._raise_index_error(error)
        except (AttributeError, TypeError, ValueError):
            _conflict()
        try:
            workspace_hash = ReceiptRepository.workspace_hash_for(
                self._receipts, str(workspace_root)
            )
        except ReceiptIntegrityError as error:
            if str(error) in {"evidence_key_missing", "evidence_key_read_invalid"}:
                _unavailable()
            _conflict()
        if not _is_hash(workspace_hash):
            _conflict()
        return workspace_hash

    def _checkpoint(
        self,
        workspace_id: str,
        request: AcceptedAtlasProjectionRequest,
        write_scope: tuple[str, ...],
    ) -> Checkpoint:
        try:
            checkpoint = self._checkpoints.status_for_workspace(
                workspace_id, request.checkpoint_id
            )
        except IndexError as error:
            if error.code == "NOT_FOUND":
                self._classify_checkpoint_absence(request.checkpoint_id)
            self._raise_index_error(error)
        if type(checkpoint) is not Checkpoint:
            _conflict()
        if (
            checkpoint.checkpoint_id != request.checkpoint_id
            or checkpoint.kind != "checkpoint"
            or checkpoint.workflow_id != request.workflow_id
            or checkpoint.task_id != request.code_task_id
            or checkpoint.workspace_id != workspace_id
            or checkpoint.workspace_root != ""
            or checkpoint.snapshot_id != request.input_snapshot_id
            or checkpoint.write_scope != write_scope
            or checkpoint.manifest_hash != request.checkpoint_hash
            or not _is_hash(checkpoint.manifest_hash)
            or not _is_hash(checkpoint.cas_root_hash)
        ):
            _conflict()
        return checkpoint

    def _classify_checkpoint_absence(self, checkpoint_id: str) -> NoReturn:
        """Distinguish a missing checkpoint from one bound to another workspace."""

        try:
            checkpoint = self._checkpoints.status(checkpoint_id)
        except IndexError as error:
            self._raise_index_error(error)
        if type(checkpoint) is not Checkpoint:
            _conflict()
        _conflict()

    def _index_facts(
        self,
        workspace_id: str,
        request: AcceptedAtlasProjectionRequest,
        write_scope: tuple[str, ...],
    ) -> tuple[SnapshotFacts, SnapshotDiff]:
        try:
            self._project_index.assert_current(
                workspace_id,
                request.output_snapshot_id,
                required_paths=write_scope,
            )
            indexed_diff = self._project_index.diff(
                workspace_id,
                request.input_snapshot_id,
                request.output_snapshot_id,
            )
            input_facts = self._project_index.snapshot_facts(
                workspace_id, request.input_snapshot_id
            )
            output_facts = self._project_index.snapshot_facts(
                workspace_id, request.output_snapshot_id
            )
        except IndexError as error:
            self._raise_index_error(error)
        if (
            type(indexed_diff) is not SnapshotDiff
            or type(input_facts) is not SnapshotFacts
            or type(output_facts) is not SnapshotFacts
            or indexed_diff.from_snapshot_id != request.input_snapshot_id
            or indexed_diff.to_snapshot_id != request.output_snapshot_id
            or _diff_hash(indexed_diff) != request.indexed_diff_hash
            or input_facts.snapshot.snapshot_id != request.input_snapshot_id
            or output_facts.snapshot.snapshot_id != request.output_snapshot_id
            or input_facts.snapshot.workspace_id != workspace_id
            or output_facts.snapshot.workspace_id != workspace_id
        ):
            _conflict()
        return output_facts, indexed_diff

    @staticmethod
    def _changed_paths(
        indexed_diff: SnapshotDiff, write_scope: tuple[str, ...]
    ) -> tuple[str, ...]:
        paths = tuple(
            sorted(
                {
                    *indexed_diff.added_paths,
                    *indexed_diff.removed_paths,
                    *indexed_diff.changed_paths,
                }
            )
        )
        if (
            not paths
            or len(paths) > MAX_CHANGED_FILES
            or any(not _within_scope(path, write_scope) for path in paths)
        ):
            _conflict()
        return paths

    def _source_files(
        self,
        workspace_id: str,
        checkpoint: Checkpoint,
        request: AcceptedAtlasProjectionRequest,
        changed_paths: tuple[str, ...],
        output_facts: SnapshotFacts,
    ) -> tuple[tuple[CheckpointFile, ...], tuple[SnapshotFile, ...]]:
        input_facts = self._snapshot_facts(workspace_id, request.input_snapshot_id)
        input_paths = {path for path, _ in input_facts.file_hashes}
        output_paths = {path for path, _ in output_facts.file_hashes}
        before_paths = tuple(path for path in changed_paths if path in input_paths)
        after_paths = tuple(path for path in changed_paths if path in output_paths)
        try:
            before_files = (
                self._checkpoints.read_files_for_task(
                    checkpoint.checkpoint_id,
                    workflow_id=request.workflow_id,
                    task_id=request.code_task_id,
                    paths=before_paths,
                    byte_budget=MAX_PACKET_BYTES,
                )
                if before_paths
                else ()
            )
            remaining_bytes = MAX_PACKET_BYTES - sum(
                len(item.body) for item in before_files
            )
            if after_paths and remaining_bytes <= 0:
                _conflict()
            after_files = (
                self._project_index.read_snapshot_files(
                    workspace_id,
                    request.output_snapshot_id,
                    after_paths,
                    byte_budget=remaining_bytes,
                )
                if after_paths
                else ()
            )
        except IndexError as error:
            self._raise_index_error(error)
        except (AttributeError, TypeError, ValueError):
            _conflict()
        return tuple(before_files), tuple(after_files)

    def _snapshot_facts(self, workspace_id: str, snapshot_id: str) -> SnapshotFacts:
        try:
            facts = self._project_index.snapshot_facts(workspace_id, snapshot_id)
        except IndexError as error:
            self._raise_index_error(error)
        if (
            type(facts) is not SnapshotFacts
            or facts.snapshot.snapshot_id != snapshot_id
        ):
            _conflict()
        return facts

    def _query_receipt(self, trace_id: str, output_snapshot_id: str) -> None:
        try:
            receipt = self._project_index.get_query_receipt(trace_id)
        except IndexError as error:
            self._raise_index_error(error)
        if (
            type(receipt) is not QueryReceipt
            or receipt.trace_id != trace_id
            or receipt.snapshot_id != output_snapshot_id
        ):
            _conflict()

    def _bound_receipt(
        self,
        receipt_id: str,
        *,
        request: AcceptedAtlasProjectionRequest,
        workspace_hash: str,
        input_hash: str,
        output_hash: str,
    ) -> BoundExecutionReceipt:
        try:
            receipt = ReceiptRepository.read(self._receipts, receipt_id)
        except ReceiptIntegrityError as error:
            if str(error) == "receipt_read_invalid":
                _unavailable()
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error
        kind = (
            {"shell": "command", "patch": "write"}.get(receipt.canonical_tool)
            if type(receipt) is RawExecutionReceipt
            else None
        )
        if (
            kind is None
            or receipt.receipt_id != receipt_id
            or receipt.workspace_hash != workspace_hash
            or receipt.success is not True
            or type(receipt.exit_code) is not int
            or receipt.exit_code != 0
            or type(receipt.command_spec) is not tuple
            or any(type(part) is not str for part in receipt.command_spec)
            or receipt.command_spec_hash != canonical_hash(receipt.command_spec)
            or any(
                not _is_hash(getattr(receipt, field_name))
                for field_name in _RECEIPT_HASH_FIELDS
            )
        ):
            _conflict()
        return BoundExecutionReceipt(
            receipt_id=receipt.receipt_id,
            kind=kind,
            workflow_id=request.workflow_id,
            task_id=request.code_task_id,
            acceptance_id=request.acceptance_id,
            workspace_hash=workspace_hash,
            output_snapshot_id=request.output_snapshot_id,
            command_spec=receipt.command_spec,
            command_spec_hash=receipt.command_spec_hash,
            input_hash=input_hash,
            output_hash=output_hash,
            exit_code=receipt.exit_code,
            success=receipt.success,
        )

    @staticmethod
    def _raise_index_error(error: IndexError) -> NoReturn:
        if error.code in {"NOT_FOUND", "INDEX_UNAVAILABLE"}:
            _unavailable()
        _conflict()
