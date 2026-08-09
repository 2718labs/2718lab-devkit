"""Private Continuity orchestration; deliberately independent from Atlas/UoW."""

from __future__ import annotations

from typing import Any

from .canonical import canonical_hash, canonical_json
from .cas import ContinuityCas
from .models import (
    BoundExecutionReceipt,
    ChangedNode,
    ContinuityAttempt,
    ContinuityKey,
    ContinuityReceipt,
    CoverageGap,
    FrozenEntry,
    FrozenView,
)
from .store import ContinuityStore, ContinuityStoreError


class ContinuityService:
    def __init__(self, store: ContinuityStore, cas: ContinuityCas) -> None:
        self.store, self.cas = store, cas

    def claim_or_reuse(self, key: ContinuityKey) -> ContinuityAttempt:
        current = self.store.current_attempt(key)
        if current is not None and current.state in {"frozen", "published"}:
            return current
        epoch = 1 if current is None else current.fence_epoch + 1
        return self.store.append_attempt_event(key, epoch, "claimed", None, None)

    def freeze(self, attempt: ContinuityAttempt, request: Any, evidence: Any) -> FrozenView:
        view = self._typed_view(attempt.key, request, evidence)
        if view.key.key_hash != attempt.key.key_hash:
            raise ContinuityStoreError("CONTINUITY_VIEW_CONFLICT")
        saved = self.store.insert_or_get_view(view, view.manifest_json)
        receipt = ContinuityReceipt.create(key=attempt.key, view_id=saved.view_id, kind="frozen")
        self.store.insert_or_get_receipt(receipt, canonical_json({"key": attempt.key.to_dict(), "view_id": saved.view_id, "kind": "frozen"}))
        current = self.store.current_attempt(attempt.key)
        if current is None or current.state == "claimed":
            self.store.append_attempt_event(attempt.key, attempt.fence_epoch, "frozen", saved.view_id, receipt.receipt_hash)
        return saved

    def publish(self, attempt: ContinuityAttempt, frozen_view: FrozenView):
        current = self.store.pointer_for(attempt.key)
        expected = 0 if current is None else current.pointer_version
        pointer = self.store.compare_and_swap_pointer(attempt.key, frozen_view, expected, attempt.fence_epoch)
        receipt = ContinuityReceipt.create(key=attempt.key, view_id=frozen_view.view_id, kind="published")
        self.store.insert_or_get_receipt(receipt, canonical_json({"key": attempt.key.to_dict(), "view_id": frozen_view.view_id, "kind": "published"}))
        self.store.append_attempt_event(attempt.key, attempt.fence_epoch, "published", frozen_view.view_id, receipt.receipt_hash)
        return pointer

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
        entries: list[FrozenEntry] = []
        for role, files in (("before_file", extraction.before_files), ("after_file", extraction.after_files)):
            for item in files:
                body = getattr(item, "body", None)
                content_hash = getattr(item, "content_hash", None)
                path = getattr(item, "path", None)
                self.cas.put_verified(content_hash, len(body) if type(body) is bytes else -1, body)
                entries.append(FrozenEntry(role, path, content_hash, len(body)))
        entries.sort(key=lambda item: (item.role, item.path, item.content_hash))
        receipts = tuple(BoundExecutionReceipt(item.receipt_id, item.kind, item.workflow_id, item.task_id, item.acceptance_id, item.workspace_hash, item.output_snapshot_id, item.command_spec, item.command_spec_hash, item.input_hash, item.output_hash, item.exit_code, item.success) for item in extraction.execution_receipts)
        return FrozenView.create(key=key, entries=tuple(entries), input_snapshot_ids=(request.input_snapshot_id,), output_snapshot_ids=(request.output_snapshot_id,), checkpoint_ids=(request.checkpoint_id,), query_ids=(request.output_query_trace_id,), verification_artifact_hashes=request.verification_artifact_hashes, execution_receipt_ids=request.execution_receipt_ids, request_hash=canonical_hash(_public_request(request)), evidence_hash=canonical_hash(_evidence(evidence)), changed_nodes=tuple(ChangedNode.from_index_node(node) for node in extraction.changed_nodes), coverage_gaps=tuple(CoverageGap(gap.path, gap.code, gap.message) for gap in extraction.coverage_gaps), execution_receipts=receipts)


def _public_request(request: Any) -> dict[str, Any]:
    return {name: getattr(request, name) for name in ("ingestion_key", "payload_hash", "acceptance_id", "workflow_id", "code_task_id", "code_task_version", "input_snapshot_id", "output_snapshot_id", "indexed_diff_hash", "intent_id", "language", "framework", "checkpoint_id", "checkpoint_hash", "output_query_trace_id", "verification_artifact_hashes", "execution_receipt_ids", "evidence_binding_hash")}


def _evidence(evidence: Any) -> dict[str, Any]:
    return {name: getattr(evidence, name) for name in ("code_task_version", "language", "framework", "checkpoint_hash", "indexed_diff_hash", "output_query_trace_id", "verification_artifact_hashes")}
