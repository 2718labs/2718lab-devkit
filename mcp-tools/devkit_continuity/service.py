"""Private Continuity orchestration; deliberately independent from Atlas/UoW."""

from __future__ import annotations

import hashlib
from typing import Any

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
from .store import ContinuityStore, ContinuityStoreError


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

    def _typed_view(self, key: ContinuityKey, request: Any, evidence: Any) -> FrozenView:
        from devkit_atlas.extractors import ExtractionRequest
        from devkit_atlas.service import (
            AcceptedAtlasProjectionEvidence,
            AcceptedAtlasProjectionRequest,
        )
        if not isinstance(request, AcceptedAtlasProjectionRequest) or not isinstance(evidence, AcceptedAtlasProjectionEvidence) or not isinstance(evidence.extraction_request, ExtractionRequest):
            raise ContinuityStoreError("CONTINUITY_INPUT_INVALID")
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
        extraction = evidence.extraction_request
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
