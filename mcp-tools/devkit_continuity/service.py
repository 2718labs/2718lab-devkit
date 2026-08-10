"""Private Continuity orchestration; deliberately independent from Atlas/UoW."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .canonical import canonical_hash
from .cas import ContinuityCas
from .models import (
    BoundExecutionReceipt,
    ChangedNode,
    ContinuityAttempt,
    ContinuityError,
    ContinuityKey,
    ContinuityReceipt,
    CoverageGap,
    FrozenEntry,
    FrozenView,
    ReplayMetadata,
)
from .store import ContinuityStore, ContinuityStoreError, _ReplayState

if TYPE_CHECKING:
    from devkit_atlas.extractors import ExtractionRequest
    from devkit_atlas.service import (
        AcceptedAtlasProjectionEvidence,
        AcceptedAtlasProjectionRequest,
    )


@dataclass(frozen=True, slots=True)
class _ReplayMaterialization:
    """Private, fully typed input for a reader-free Atlas projection."""

    attempt: ContinuityAttempt
    view: FrozenView
    request: AcceptedAtlasProjectionRequest
    evidence: AcceptedAtlasProjectionEvidence
    extraction: ExtractionRequest


class ContinuityService:
    def __init__(self, store: ContinuityStore, cas: ContinuityCas) -> None:
        self.store, self.cas = store, cas

    def claim_or_reuse(self, key: ContinuityKey) -> ContinuityAttempt:
        return self.store.claim_or_reuse_atomic(key)

    def freeze(self, attempt: ContinuityAttempt, request: Any, evidence: Any) -> FrozenView:
        view = self._typed_view(attempt.key, request, evidence)
        if view.key.key_hash != attempt.key.key_hash:
            raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
        receipt = ContinuityReceipt.create(key=attempt.key, view_id=view.view_id, kind="frozen")
        self.store.freeze_attempt_atomic(attempt, view, receipt)
        return view

    def publish(self, attempt: ContinuityAttempt, frozen_view: FrozenView):
        current = self.store.current_attempt(attempt.key)
        if (
            attempt.state == "claimed"
            and attempt.view_id is None
            and attempt.receipt_hash is None
            and current is not None
            and current.fence_epoch == attempt.fence_epoch
            and current.state == "frozen"
            and current.view_id == frozen_view.view_id
        ):
            attempt = current
        return self.store.publish_attempt_atomic(attempt, frozen_view)

    def find_replay_candidate(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> ContinuityKey | None:
        """Locate exactly one frozen view using only public acceptance IDs."""

        return self.store.find_replay_candidate(
            workflow_id, code_task_id, acceptance_id, ingestion_key
        )

    def verify_replay(self, key: ContinuityKey) -> FrozenView:
        """Structurally verify one v2 frozen view and every referenced CAS body."""

        state, _bodies = self._verified_replay(key)
        self._verify_replay_state(key, state)
        return state.view

    def materialize_replay(self, key: ContinuityKey) -> _ReplayMaterialization:
        """Rebuild an Atlas-ready typed projection input without live evidence."""

        state, bodies = self._verified_replay(key)
        self._verify_replay_state(key, state)
        materialization = self._typed_replay_materialization(
            state.attempt, state.view, bodies
        )
        self._verify_replay_state(key, state)
        return materialization

    def _prove_materialized_replay(
        self,
        key: ContinuityKey,
        attempt: ContinuityAttempt,
        view: FrozenView,
    ) -> _ReplayState:
        """Recheck physical storage and bind a typed replay to its exact state."""

        if type(key) is not ContinuityKey or type(attempt) is not ContinuityAttempt:
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
        if type(view) is not FrozenView:
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
        state = self.store.replay_state_for(key)
        if state.attempt != attempt or state.view != view:
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
        return state

    def _verify_replay_state(self, key: ContinuityKey, expected: _ReplayState) -> None:
        """Reject a physical or durable state change after a replay observation."""

        if self.store.replay_state_for(key) != expected:
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")

    def _verified_replay(
        self, key: ContinuityKey
    ) -> tuple[_ReplayState, tuple[tuple[FrozenEntry, bytes], ...]]:
        state = self.store.replay_state_for(key)
        bodies = tuple(
            (
                entry,
                self.cas.read_verified(entry.content_hash, entry.byte_length),
            )
            for entry in state.view.entries
        )
        return state, bodies

    @staticmethod
    def _typed_replay_materialization(
        attempt: ContinuityAttempt,
        view: FrozenView,
        bodies: tuple[tuple[FrozenEntry, bytes], ...],
    ) -> _ReplayMaterialization:
        """Convert verified Continuity data to exact Atlas value objects only."""

        from devkit_atlas.extractors import (
            BoundExecutionReceipt as AtlasExecutionReceipt,
        )
        from devkit_atlas.extractors import ExtractionRequest
        from devkit_atlas.models import AtlasError
        from devkit_atlas.service import (
            AcceptedAtlasProjectionEvidence,
            AcceptedAtlasProjectionRequest,
            AtlasService,
        )
        from project_index.checkpoints import CheckpointFile
        from project_index.models import CoverageGap as IndexCoverageGap
        from project_index.models import IndexNode, SnapshotFile

        metadata = view.replay_metadata
        if not isinstance(metadata, ReplayMetadata):
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
        try:
            request = AcceptedAtlasProjectionRequest.create(
                workflow_id=view.key.workflow_id,
                code_task_id=view.key.code_task_id,
                code_task_version=view.key.code_task_version,
                input_snapshot_id=view.input_snapshot_ids[0],
                output_snapshot_id=view.output_snapshot_ids[0],
                indexed_diff_hash=metadata.indexed_diff_hash,
                intent_id=metadata.intent_id,
                language=metadata.language,
                framework=metadata.framework,
                checkpoint_id=view.checkpoint_ids[0],
                checkpoint_hash=metadata.checkpoint_hash,
                output_query_trace_id=view.query_ids[0],
                verification_artifact_hashes=view.verification_artifact_hashes,
                execution_receipt_ids=view.execution_receipt_ids,
            )
            if (
                request.ingestion_key != view.key.ingestion_key
                or request.payload_hash != view.key.payload_hash
                or request.acceptance_id != view.key.acceptance_id
                or request.evidence_binding_hash != view.key.evidence_binding_hash
                or view.request_hash != canonical_hash(_public_request(request))
            ):
                raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
            before_files: list[CheckpointFile] = []
            after_files: list[SnapshotFile] = []
            for entry, body in bodies:
                if entry.role == "before_file":
                    before_files.append(
                        CheckpointFile(entry.path, entry.content_hash, body)
                    )
                elif entry.role == "after_file":
                    after_files.append(SnapshotFile(entry.path, entry.content_hash, body))
                else:
                    raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
            extraction = ExtractionRequest(
                workflow_id=view.key.workflow_id,
                task_id=view.key.code_task_id,
                acceptance_id=view.key.acceptance_id,
                task_kind=metadata.task_kind,
                intent_id=metadata.intent_id,
                workspace_hash=metadata.workspace_hash,
                checkpoint_id=view.checkpoint_ids[0],
                input_snapshot_id=view.input_snapshot_ids[0],
                output_snapshot_id=view.output_snapshot_ids[0],
                write_scope=metadata.write_scope,
                before_files=tuple(before_files),
                after_files=tuple(after_files),
                changed_nodes=tuple(
                    IndexNode(
                        node_id=node.node_id,
                        kind=node.kind,
                        path=node.path,
                        name=node.name,
                        qualified_name=node.qualified_name,
                        start_line=node.start_line,
                        end_line=node.end_line,
                        content_hash=node.content_hash,
                        attributes=node.attributes,
                        extractor_id=node.extractor_id,
                        extractor_version=node.extractor_version,
                        provenance=node.provenance,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )
                    for node in view.changed_nodes
                ),
                coverage_gaps=tuple(
                    IndexCoverageGap(gap.path, gap.code, gap.message)
                    for gap in view.coverage_gaps
                ),
                execution_receipts=tuple(
                    AtlasExecutionReceipt(
                        receipt.receipt_id,
                        receipt.kind,
                        receipt.workflow_id,
                        receipt.task_id,
                        receipt.acceptance_id,
                        receipt.workspace_hash,
                        receipt.output_snapshot_id,
                        receipt.command_spec,
                        receipt.command_spec_hash,
                        receipt.input_hash,
                        receipt.output_hash,
                        receipt.exit_code,
                        receipt.success,
                    )
                    for receipt in view.execution_receipts
                ),
            )
            evidence = AcceptedAtlasProjectionEvidence(
                code_task_version=view.key.code_task_version,
                language=metadata.language,
                framework=metadata.framework,
                checkpoint_hash=metadata.checkpoint_hash,
                indexed_diff_hash=metadata.indexed_diff_hash,
                output_query_trace_id=view.query_ids[0],
                verification_artifact_hashes=view.verification_artifact_hashes,
                extraction_request=extraction,
            )
            if view.evidence_hash != canonical_hash(_evidence(evidence)):
                raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
            request = AtlasService._validate_accepted_projection_request(request)
            AtlasService._require_canonical_core_key(request)
            if AtlasService._validate_reader_evidence(request, evidence) != extraction:
                raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT")
        except ContinuityStoreError:
            raise
        except (AtlasError, AttributeError, IndexError, TypeError, ValueError) as error:
            raise ContinuityStoreError("CONTINUITY_REPLAY_CONFLICT") from error
        return _ReplayMaterialization(attempt, view, request, evidence, extraction)

    def _typed_view(self, key: ContinuityKey, request: Any, evidence: Any) -> FrozenView:
        from devkit_atlas.extractors import ExtractionRequest
        from devkit_atlas.models import AtlasError
        from devkit_atlas.service import (
            AcceptedAtlasProjectionEvidence,
            AcceptedAtlasProjectionRequest,
            AtlasService,
        )
        if (
            type(request) is not AcceptedAtlasProjectionRequest
            or type(evidence) is not AcceptedAtlasProjectionEvidence
            or type(evidence.extraction_request) is not ExtractionRequest
        ):
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID")
        try:
            request = AtlasService._validate_accepted_projection_request(request)
            AtlasService._require_canonical_core_key(request)
            extraction = AtlasService._validate_reader_evidence(request, evidence)
        except AtlasError as error:
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID") from error
        if (
            key.workflow_id != request.workflow_id
            or key.code_task_id != request.code_task_id
            or key.code_task_version != request.code_task_version
            or key.acceptance_id != request.acceptance_id
            or key.ingestion_key != request.ingestion_key
            or key.payload_hash != request.payload_hash
            or key.evidence_binding_hash != request.evidence_binding_hash
        ):
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID")
        if (
            evidence.code_task_version != request.code_task_version
            or evidence.language != request.language
            or evidence.framework != request.framework
            or evidence.checkpoint_hash != request.checkpoint_hash
            or evidence.indexed_diff_hash != request.indexed_diff_hash
            or evidence.output_query_trace_id != request.output_query_trace_id
            or evidence.verification_artifact_hashes != request.verification_artifact_hashes
            or extraction.workflow_id != request.workflow_id
            or extraction.task_id != request.code_task_id
            or extraction.acceptance_id != request.acceptance_id
            or not isinstance(extraction.task_kind, str)
            or not extraction.task_kind
            or extraction.intent_id != request.intent_id
            or extraction.checkpoint_id != request.checkpoint_id
            or extraction.input_snapshot_id != request.input_snapshot_id
            or extraction.output_snapshot_id != request.output_snapshot_id
            or tuple(item.receipt_id for item in extraction.execution_receipts)
            != request.execution_receipt_ids
            or any(
                item.workflow_id != request.workflow_id
                or item.task_id != request.code_task_id
                or item.acceptance_id != request.acceptance_id
                or item.workspace_hash != extraction.workspace_hash
                or item.output_snapshot_id != request.output_snapshot_id
                for item in extraction.execution_receipts
            )
        ):
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID")
        try:
            replay_metadata = ReplayMetadata(
                task_kind=extraction.task_kind,
                intent_id=request.intent_id,
                workspace_hash=extraction.workspace_hash,
                write_scope=extraction.write_scope,
                indexed_diff_hash=request.indexed_diff_hash,
                language=request.language,
                framework=request.framework,
                checkpoint_hash=request.checkpoint_hash,
            )
        except ContinuityError as error:
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID") from error
        try:
            entries: list[FrozenEntry] = []
            cas_inputs: list[tuple[str, int, bytes]] = []
            for role, files in (
                ("before_file", extraction.before_files),
                ("after_file", extraction.after_files),
            ):
                for item in files:
                    body = getattr(item, "body", None)
                    content_hash = getattr(item, "content_hash", None)
                    path = getattr(item, "path", None)
                    if type(body) is not bytes:
                        raise ContinuityError("CONTINUITY_INPUT_INVALID")
                    entry = FrozenEntry(role, path, content_hash, len(body))
                    if "sha256:" + hashlib.sha256(body).hexdigest() != content_hash:
                        raise ContinuityError("CONTINUITY_INPUT_INVALID")
                    entries.append(entry)
                    cas_inputs.append((entry.content_hash, entry.byte_length, body))
            entries.sort(key=lambda item: (item.role, item.path, item.content_hash))
            receipts = tuple(
                BoundExecutionReceipt(
                    item.receipt_id,
                    item.kind,
                    item.workflow_id,
                    item.task_id,
                    item.acceptance_id,
                    item.workspace_hash,
                    item.output_snapshot_id,
                    item.command_spec,
                    item.command_spec_hash,
                    item.input_hash,
                    item.output_hash,
                    item.exit_code,
                    item.success,
                )
                for item in extraction.execution_receipts
            )
            view = FrozenView.create(
                key=key,
                entries=tuple(entries),
                input_snapshot_ids=(request.input_snapshot_id,),
                output_snapshot_ids=(request.output_snapshot_id,),
                checkpoint_ids=(request.checkpoint_id,),
                query_ids=(request.output_query_trace_id,),
                verification_artifact_hashes=request.verification_artifact_hashes,
                execution_receipt_ids=request.execution_receipt_ids,
                request_hash=canonical_hash(_public_request(request)),
                evidence_hash=canonical_hash(_evidence(evidence)),
                replay_metadata=replay_metadata,
                changed_nodes=tuple(
                    ChangedNode.from_index_node(node) for node in extraction.changed_nodes
                ),
                coverage_gaps=tuple(
                    CoverageGap(gap.path, gap.code, gap.message)
                    for gap in extraction.coverage_gaps
                ),
                execution_receipts=receipts,
            )
        except (AttributeError, ContinuityError, TypeError, ValueError) as error:
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID") from error
        for content_hash, byte_length, body in cas_inputs:
            self.cas.put_verified(content_hash, byte_length, body)
        return view


def _public_request(request: Any) -> dict[str, Any]:
    return {name: getattr(request, name) for name in ("ingestion_key", "payload_hash", "acceptance_id", "workflow_id", "code_task_id", "code_task_version", "input_snapshot_id", "output_snapshot_id", "indexed_diff_hash", "intent_id", "language", "framework", "checkpoint_id", "checkpoint_hash", "output_query_trace_id", "verification_artifact_hashes", "execution_receipt_ids", "evidence_binding_hash")}


def _evidence(evidence: Any) -> dict[str, Any]:
    return {name: getattr(evidence, name) for name in ("code_task_version", "language", "framework", "checkpoint_hash", "indexed_diff_hash", "output_query_trace_id", "verification_artifact_hashes")}
