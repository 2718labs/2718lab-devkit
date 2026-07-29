"""Application service for durable workflow orchestration.

The service owns role-scoped projections.  It deliberately delegates every
durable mutation to :class:`SQLiteStore`, which is the transaction and CAS
boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .models import Task, TaskKind, TaskState, Workflow
from .store import Artifact, Lease, SQLiteStore, StoreError


class ServiceError(RuntimeError):
    """Stable, safe error returned by the orchestration service."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OrchestratorService:
    """Coordinate workflow operations and expose minimal role projections."""

    _SAFE_METADATA_KEYS = frozenset({"kind", "summary", "category", "priority"})
    _MAX_METADATA_KEY_LENGTH = 32
    _MAX_METADATA_VALUE_LENGTH = 128
    _MAX_METADATA_BYTES = 512
    _MAX_ACCEPTANCE_RECEIPTS = 32
    _MAX_STATUS_CODE_ACCEPTANCES = 100
    _SAFE_ACCEPTANCE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
    _SAFE_RECEIPT_IDENTIFIER = re.compile(r"sha256:[0-9a-f]{64}\Z")

    def __init__(
        self,
        store: SQLiteStore,
        *,
        index_service: Any | None = None,
        checkpoint_service: Any | None = None,
        receipt_repository: Any | None = None,
        evidence_root: str = "evidence",
        mailbox_count_limit: int = 100,
        mailbox_byte_limit: int = 1_048_576,
        max_ttl_seconds: int = 86_400,
    ) -> None:
        self._store = store
        self._index_service = index_service
        self._checkpoint_service = checkpoint_service
        self._receipt_repository = receipt_repository
        self._evidence_root = PurePosixPath(evidence_root.replace("\\", "/"))
        self._mailbox_count_limit = mailbox_count_limit
        self._mailbox_byte_limit = mailbox_byte_limit
        self._max_ttl_seconds = max_ttl_seconds

    def create_workflow(self, workflow: Workflow) -> Workflow:
        return self._call(self._store.create_workflow, workflow)

    def register_task(
        self,
        task: Task,
        *,
        card: str,
        direct_contract_hashes: tuple[str, ...] = (),
        required_evidence: tuple[str, ...] = (),
        input_hash: str = "",
        strict_index: bool = False,
        workspace_root: str = "",
        input_snapshot_id: str = "",
        task_node_ids: tuple[str, ...] = (),
        contract_node_ids: tuple[str, ...] = (),
    ) -> Task:
        canonical_workspace = ""
        if strict_index:
            if not input_snapshot_id or not task_node_ids:
                raise ServiceError(
                    "INDEX_UNAVAILABLE", "strict index binding is incomplete"
                )
            canonical = self._canonical_workspace(workspace_root)
            if task.write_scope:
                git_marker = canonical / ".git"
                if not git_marker.is_file() or git_marker.is_symlink():
                    raise ServiceError(
                        "WORKTREE_UNOWNED", "strict write task is not a linked worktree"
                    )
            self._assert_index_current(canonical, input_snapshot_id, task.write_scope)
            canonical_workspace = str(canonical)
        card_hash = f"sha256:{sha256(card.encode('utf-8')).hexdigest()}"
        registered = self._call(
            self._store.register_task,
            replace(task, card_hash=card_hash),
            dependencies=task.dependencies,
            card_body=card,
            contract_subscriptions=tuple(direct_contract_hashes),
            required_evidence=tuple(required_evidence),
            strict_index=strict_index,
            workspace_root=canonical_workspace,
            input_snapshot_id=input_snapshot_id,
            task_node_ids=tuple(task_node_ids),
            contract_node_ids=tuple(contract_node_ids),
        )
        if input_hash:
            self._call(self._store.record_task_input, task.id, input_hash)
        return registered

    def ready_wave(self, workflow_id: str) -> tuple[Task, ...]:
        return self._call(self._store.promote_ready_tasks, workflow_id)

    def claim_task(
        self,
        task_id: str,
        owner: str,
        *,
        expires_at: str,
        host_target: str | None = None,
        now: str | None = None,
    ) -> tuple[Task, Lease]:
        binding = self._call(self._store.get_index_binding, task_id)
        if binding is not None:
            task = self._call(self._store.get_task, task_id)
            snapshot_id = binding.input_snapshot_id
            if task.state == TaskState.RUNNING and binding.output_snapshot_id:
                snapshot_id = binding.output_snapshot_id
            self._assert_index_current(
                Path(binding.workspace_root),
                snapshot_id,
                task.write_scope,
            )
        return self._call(
            self._store.claim_task,
            task_id,
            owner,
            expires_at,
            host_target=host_target,
            now=now,
        )

    def bind_endpoint(
        self,
        workflow_id: str,
        task_id: str,
        *,
        owner: str,
        epoch: int,
        host_target: str,
        now: str | None = None,
    ) -> Lease:
        task = self._call(self._store.get_task, task_id)
        if task.workflow_id != workflow_id:
            raise ServiceError("NOT_FOUND", "task is not in this workflow")
        return self._call(
            self._store.bind_host_target,
            task_id,
            owner,
            epoch,
            host_target,
            now=now,
        )

    def complete_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        owner: str,
        epoch: int,
        result_hash: str = "",
        execution_receipt_ids: list[str] | tuple[str, ...] | None = None,
        now: str | None = None,
    ) -> Task:
        task = self._call(self._store.get_task, task_id)
        binding = self._call(self._store.get_index_binding, task_id)
        if binding is not None:
            snapshot_id = binding.output_snapshot_id or binding.input_snapshot_id
            self._assert_index_current(
                Path(binding.workspace_root), snapshot_id, task.write_scope
            )
        receipt_attestation = None
        if task.task_kind is TaskKind.CODE:
            if execution_receipt_ids is None:
                raise ServiceError(
                    "EVIDENCE_INCOMPLETE",
                    "code task execution receipt identifiers are required",
                )
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or not 0 <= expected_version < 2**63 - 1
            ):
                raise ServiceError("INVALID_REQUEST", "task version is invalid")
            if binding is None:
                raise ServiceError(
                    "INDEX_UNAVAILABLE", "code task has no strict index binding"
                )
            receipt_ids = self._validate_execution_receipt_ids(execution_receipt_ids)
            workspace_hash = self._validate_receipt_evidence(receipt_ids)
            receipt_attestation = self._call(
                self._store.build_code_task_receipt_attestation,
                workflow_id=task.workflow_id,
                code_task_id=task.id,
                code_task_version=expected_version + 1,
                input_snapshot_id=binding.input_snapshot_id,
                output_snapshot_id=binding.output_snapshot_id,
                workspace_hash=workspace_hash,
                execution_receipt_ids=receipt_ids,
            )
        return self._call(
            self._store.complete_task,
            task_id,
            TaskState.DONE,
            expected_version,
            owner,
            epoch,
            result_hash=result_hash or None,
            receipt_attestation=receipt_attestation,
            now=now,
        )

    def fail_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        owner: str,
        epoch: int,
        now: str | None = None,
    ) -> Task:
        return self._finish_task(
            task_id,
            TaskState.FAILED,
            expected_version=expected_version,
            owner=owner,
            epoch=epoch,
            now=now,
        )

    def block_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        owner: str,
        epoch: int,
        now: str | None = None,
    ) -> Task:
        return self._finish_task(
            task_id,
            TaskState.BLOCKED,
            expected_version=expected_version,
            owner=owner,
            epoch=epoch,
            now=now,
        )

    def cancel_workflow(
        self, workflow_id: str, *, expected_version: int | None = None
    ) -> Workflow:
        workflow, _ = self._call(
            self._store.cancel_workflow, workflow_id, expected_version=expected_version
        )
        return workflow

    def status(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._call(self._store.get_workflow, workflow_id)
        code_acceptances = self._call(
            self._store.list_code_task_acceptances,
            workflow_id,
            limit=self._MAX_STATUS_CODE_ACCEPTANCES,
        )
        acceptance_status: list[dict[str, Any]] = []
        for acceptance in code_acceptances:
            outbox = self._call(
                self._store.atlas_outbox_for_acceptance, acceptance.acceptance_id
            )
            if outbox is None:
                acceptance_status.append(
                    {
                        "acceptance_id": acceptance.acceptance_id,
                        "code_task_id": acceptance.code_task_id,
                        "output_snapshot_id": acceptance.output_snapshot_id,
                        "outbox_state": "missing",
                        "last_error_code": "OUTBOX_MISSING",
                        "reason_codes": [],
                    }
                )
                continue
            acceptance_status.append(
                {
                    "acceptance_id": acceptance.acceptance_id,
                    "code_task_id": acceptance.code_task_id,
                    "output_snapshot_id": acceptance.output_snapshot_id,
                    "outbox_state": outbox.state.value,
                    "last_error_code": outbox.last_error_code,
                    "reason_codes": list(outbox.reason_codes),
                }
            )
        return {
            "workflow": workflow,
            "tasks": self._call(self._store.list_tasks, workflow_id),
            "code_acceptances": acceptance_status,
        }

    def accept_code_task(
        self,
        workflow_id: str,
        code_task_id: str,
        *,
        expected_code_task_version: int,
        expected_output_snapshot_id: str,
        coordinator_task_id: str,
        coordinator_owner: str,
        coordinator_epoch: int,
        execution_receipt_ids: list[str] | tuple[str, ...],
        now: str | None = None,
    ) -> tuple[Any, Any]:
        """Validate immutable code-task evidence before one atomic acceptance write."""

        accepted_at = self._acceptance_timestamp(now)
        receipt_ids = self._validate_acceptance_request(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            expected_code_task_version=expected_code_task_version,
            expected_output_snapshot_id=expected_output_snapshot_id,
            coordinator_task_id=coordinator_task_id,
            coordinator_owner=coordinator_owner,
            coordinator_epoch=coordinator_epoch,
            execution_receipt_ids=execution_receipt_ids,
        )
        self._call(self._store.get_workflow, workflow_id)
        coordinator = self._call(self._store.get_task, coordinator_task_id)
        self._require_current_coordinator(
            coordinator,
            workflow_id=workflow_id,
            coordinator_owner=coordinator_owner,
            coordinator_epoch=coordinator_epoch,
            now=accepted_at,
        )
        task = self._call(self._store.get_task, code_task_id)
        self._require_current_code_task(
            task,
            workflow_id=workflow_id,
            expected_version=expected_code_task_version,
        )
        binding = self._call(self._store.get_index_binding, code_task_id)
        if binding is None:
            raise ServiceError(
                "INDEX_UNAVAILABLE", "code task has no strict index binding"
            )
        if binding.output_snapshot_id != expected_output_snapshot_id:
            raise ServiceError(
                "ACCEPTANCE_CONFLICT", "code task output snapshot is not current"
            )
        self._validate_current_index_evidence(task, binding)
        checkpoint = self._validate_checkpoint_evidence(
            workflow_id=workflow_id,
            task=task,
            binding=binding,
        )
        task_evidence = self._call(
            self._store.task_acceptance_evidence,
            code_task_id,
            binding.output_snapshot_id,
        )
        self._validate_output_query_evidence(
            task_evidence.output_query_trace_id, binding.output_snapshot_id
        )
        persisted_attestation = self._call(
            self._store.code_task_receipt_attestation_for_task, code_task_id
        )
        if persisted_attestation is None:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "code task receipt attestation is unavailable"
            )
        if persisted_attestation.execution_receipt_ids != receipt_ids:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE",
                "execution receipts do not match code task completion",
            )
        workspace_hash = self._validate_receipt_evidence(receipt_ids)
        receipt_attestation = self._call(
            self._store.build_code_task_receipt_attestation,
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            code_task_version=task.version,
            input_snapshot_id=binding.input_snapshot_id,
            output_snapshot_id=binding.output_snapshot_id,
            workspace_hash=workspace_hash,
            execution_receipt_ids=receipt_ids,
        )
        if receipt_attestation != persisted_attestation:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "execution receipt attestation does not verify"
            )
        self._validate_verification_artifacts(
            task,
            task_evidence.verification_artifact_hashes,
            receipt_attestation_hash=receipt_attestation.attestation_hash,
        )
        evidence_binding = self._call(
            self._store.build_code_task_evidence_binding,
            workflow_id=workflow_id,
            task_id=code_task_id,
            task_version=task.version,
            input_snapshot_id=binding.input_snapshot_id,
            output_snapshot_id=binding.output_snapshot_id,
            indexed_diff_hash=binding.indexed_diff_hash,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_hash=checkpoint.manifest_hash,
            output_query_trace_id=task_evidence.output_query_trace_id,
            verification_artifact_hashes=task_evidence.verification_artifact_hashes,
            execution_receipt_ids=receipt_ids,
        )

        return self._call(
            self._store.insert_code_task_acceptance,
            workflow_id=workflow_id,
            task_id=code_task_id,
            task_version=task.version,
            coordinator_task_id=coordinator_task_id,
            coordinator_owner=coordinator_owner,
            coordinator_epoch=coordinator_epoch,
            input_snapshot_id=binding.input_snapshot_id,
            output_snapshot_id=binding.output_snapshot_id,
            indexed_diff_hash=binding.indexed_diff_hash,
            intent_id=task.intent_id,
            language=task.language,
            framework=task.framework,
            evidence_binding=evidence_binding,
            created_at=accepted_at,
            now=accepted_at,
        )

    def write_scope_conflicts(
        self, workflow_id: str
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return self._call(self._store.write_scope_conflicts, workflow_id)

    def register_artifact(
        self,
        workflow_id: str,
        task_id: str | None = None,
        *,
        owner: str,
        epoch: int,
        kind: str,
        content_hash: str,
        safe_path: str,
        size: int,
        redaction_version: str,
        snapshot_id: str | None = None,
        now: str | None = None,
    ) -> Artifact:
        task_id = task_id or workflow_id
        if task_id != workflow_id:
            task = self._call(self._store.get_task, task_id)
            if task.workflow_id != workflow_id:
                raise ServiceError("NOT_FOUND", "task is not in this workflow")
        if not self._is_evidence_path(safe_path):
            raise ServiceError(
                "EVIDENCE_PATH_INVALID", "artifact path is outside evidence root"
            )
        return self._call(
            self._store.register_task_artifact,
            task_id,
            owner,
            epoch,
            kind=kind,
            content_hash=content_hash,
            safe_path=safe_path,
            size=size,
            redaction_version=redaction_version,
            snapshot_id=snapshot_id,
            now=now,
        )

    def record_index_query(
        self,
        workflow_id: str,
        task_id: str,
        *,
        owner: str,
        epoch: int,
        trace_id: str,
        snapshot_id: str,
        miss_escape_used: bool,
        now: str | None = None,
    ) -> Any:
        task = self._call(self._store.get_task, task_id)
        if task.workflow_id != workflow_id:
            raise ServiceError("NOT_FOUND", "task is not in this workflow")
        getter = getattr(self._index_service, "get_query_receipt", None)
        if getter is not None:
            try:
                receipt = getter(trace_id)
            except Exception as error:
                self._raise_index_error(error)
            if receipt.snapshot_id != snapshot_id:
                raise ServiceError(
                    "SNAPSHOT_MISMATCH", "query receipt snapshot differs"
                )
            miss_escape_used = bool(receipt.miss_escape_used)
        return self._call(
            self._store.record_index_query,
            task_id,
            owner,
            epoch,
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            miss_escape_used=miss_escape_used,
            now=now,
        )

    def record_checkpoint(
        self,
        task_id: str,
        *,
        owner: str,
        epoch: int,
        checkpoint_id: str,
        now: str | None = None,
    ) -> Any:
        return self._call(
            self._store.record_checkpoint,
            task_id,
            owner,
            epoch,
            checkpoint_id,
            now=now,
        )

    def record_output_snapshot(
        self,
        task_id: str,
        *,
        owner: str,
        epoch: int,
        snapshot_id: str,
        diff_hash: str,
        now: str | None = None,
    ) -> Any:
        binding = self._call(self._store.get_index_binding, task_id)
        if binding is None:
            raise ServiceError("INDEX_UNAVAILABLE", "task has no strict index binding")
        task = self._call(self._store.get_task, task_id)
        self._assert_index_current(
            Path(binding.workspace_root), snapshot_id, task.write_scope
        )
        return self._call(
            self._store.record_output_snapshot,
            task_id,
            owner,
            epoch,
            snapshot_id=snapshot_id,
            diff_hash=diff_hash,
            now=now,
        )

    def strict_ownership(
        self,
        workflow_id: str,
        task_id: str,
        *,
        owner: str,
        epoch: int,
        now: str | None = None,
    ) -> Any:
        from project_index.checkpoints import WorktreeOwnership

        task, binding = self._call(
            self._store.strict_task_context,
            task_id,
            owner,
            epoch,
            now=now,
        )
        if task.workflow_id != workflow_id:
            raise ServiceError("NOT_FOUND", "task is not in this workflow")
        return WorktreeOwnership(
            workflow_id,
            task_id,
            owner,
            epoch,
            binding.workspace_root,
            task.write_scope,
        )

    def peers(self, workflow_id: str, task_id: str) -> tuple[dict[str, str], ...]:
        peers = self._call(self._store.list_authorized_peers, workflow_id, task_id)
        return tuple(
            {
                "task_id": peer.task_id,
                "relationship": peer.relationship,
                "capability": peer.capability,
            }
            for peer in peers
        )

    def send_message(
        self,
        workflow_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        *,
        owner: str,
        epoch: int,
        correlation_id: str,
        artifact_hash: str,
        metadata: Mapping[str, str],
        ttl_seconds: int,
        now: str | None = None,
    ) -> dict[str, Any]:
        sanitized_metadata = self._sanitize_metadata(metadata)
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 0 < ttl_seconds <= self._max_ttl_seconds
        ):
            raise ServiceError(
                "TTL_INVALID", "message TTL is outside the permitted range"
            )
        capability = next(
            (
                peer["capability"]
                for peer in self.peers(workflow_id, sender_task_id)
                if peer["task_id"] == recipient_task_id
            ),
            None,
        )
        if capability is None:
            raise ServiceError("PEER_FORBIDDEN", "recipient is not an authorized peer")
        message = self._call(
            self._store.enqueue_message,
            workflow_id,
            sender_task_id,
            recipient_task_id,
            owner,
            epoch,
            capability=capability,
            correlation_id=correlation_id,
            artifact_hash=artifact_hash,
            metadata=sanitized_metadata,
            ttl_seconds=ttl_seconds,
            now=now,
            max_count=self._mailbox_count_limit,
            max_bytes=self._mailbox_byte_limit,
        )
        instruction = None
        host_target = self._call(
            self._store.get_task_host_target, recipient_task_id, now=now
        )
        if host_target is not None:
            wakeup = json.dumps(
                {
                    "delivery_id": message.delivery_id,
                    "workflow_id": message.workflow_id,
                    "sender_task_id": message.sender_task_id,
                    "artifact_hash": message.artifact_hash,
                    "correlation_id": message.correlation_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            instruction = {
                "operation": "collaboration.send_message",
                "arguments": {"target": host_target, "message": wakeup},
            }
        return {"delivery_id": message.delivery_id, "direct_instruction": instruction}

    def inbox(
        self,
        workflow_id: str,
        recipient_task_id: str,
        *,
        owner: str,
        epoch: int,
        cursor: str | None = None,
        limit: int = 50,
        now: str | None = None,
    ) -> dict[str, Any]:
        messages = self._call(
            self._store.read_inbox,
            workflow_id,
            recipient_task_id,
            owner,
            epoch,
            cursor=cursor,
            limit=limit,
            now=now,
        )
        return {
            "entries": tuple(self._message_projection(message) for message in messages)
        }

    def resolve_artifact(
        self,
        workflow_id: str,
        recipient_task_id: str,
        *,
        owner: str,
        epoch: int,
        delivery_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        artifact = self._call(
            self._store.resolve_message_artifact,
            workflow_id,
            recipient_task_id,
            owner,
            epoch,
            delivery_id,
            now=now,
        )
        return {
            "kind": artifact.kind,
            "content_hash": artifact.content_hash,
            "safe_path": artifact.safe_path,
            "size": artifact.size,
            "redaction_version": artifact.redaction_version,
        }

    def ack_message(
        self,
        workflow_id: str,
        recipient_task_id: str,
        delivery_id: str,
        *,
        owner: str,
        epoch: int,
        now: str | None = None,
    ) -> dict[str, Any]:
        message = self._call(
            self._store.ack_message,
            workflow_id,
            recipient_task_id,
            owner,
            epoch,
            delivery_id,
            now=now,
        )
        return self._message_projection(message)

    def completed_input(self, workflow_id: str, input_hash: str) -> Task | None:
        workflow = self._call(self._store.get_workflow, workflow_id)
        return self._call(
            self._store.find_completed_task,
            workflow_id,
            input_hash=input_hash,
            policy_version=workflow.policy_version,
        )

    def context(
        self, workflow_id: str, *, role: str, task_id: str | None = None
    ) -> dict[str, Any]:
        if role == "product":
            return self._product_context(workflow_id)
        if role == "coordinator":
            return self._coordinator_context(workflow_id)
        if role == "agent":
            if task_id is None:
                raise ServiceError(
                    "INVALID_REQUEST", "agent context requires a task id"
                )
            return self._agent_context(workflow_id, task_id)
        raise ServiceError("INVALID_REQUEST", "unknown context role")

    def _product_context(self, workflow_id: str) -> dict[str, Any]:
        status = self.status(workflow_id)
        tasks = status["tasks"]
        blockers = tuple(
            task.id
            for task in tasks
            if task.state in {TaskState.BLOCKED, TaskState.FAILED}
        )
        return {
            "direction": status["workflow"].product_summary,
            "overall_state": status["workflow"].state.value,
            "current_blockers": blockers,
            "next_user_gate": "none" if not blockers else "resolve blocker",
        }

    def _coordinator_context(self, workflow_id: str) -> dict[str, Any]:
        status = self.status(workflow_id)
        tasks = status["tasks"]
        return {
            "workflow_id": workflow_id,
            "overall_state": status["workflow"].state.value,
            "dag_states": tuple((task.id, task.state.value) for task in tasks),
            "current_wave": tuple(
                task.id for task in tasks if task.state is TaskState.READY
            ),
            "write_conflicts": self.write_scope_conflicts(workflow_id),
            "budgets": {},
            "artifact_hashes": tuple(
                task.result_hash for task in tasks if task.result_hash
            ),
        }

    def _agent_context(self, workflow_id: str, task_id: str) -> dict[str, Any]:
        task = self._call(self._store.get_task, task_id)
        if task.workflow_id != workflow_id:
            raise ServiceError("NOT_FOUND", "task is not in this workflow")
        requirements = self._call(self._store.get_task_context_requirements, task_id)
        return {
            "task": {"id": task.id, "title": task.title, "owner_role": task.owner_role},
            "card": self._call(self._store.get_task_card, task_id),
            "direct_contract_hashes": requirements.direct_contract_hashes,
            "required_evidence": requirements.required_evidence,
            "write_scope": task.write_scope,
            "acceptance": requirements.required_evidence,
        }

    def _is_evidence_path(self, safe_path: str) -> bool:
        path = PurePosixPath(safe_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            return False
        try:
            path.relative_to(self._evidence_root)
        except ValueError:
            return False
        return True

    def _sanitize_metadata(self, metadata: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(metadata, Mapping):
            raise ServiceError(
                "METADATA_INVALID", "message metadata must be a string mapping"
            )
        sanitized: dict[str, str] = {}
        total_bytes = 0
        for key, value in metadata.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or key not in self._SAFE_METADATA_KEYS
                or len(key) > self._MAX_METADATA_KEY_LENGTH
                or len(value) > self._MAX_METADATA_VALUE_LENGTH
            ):
                raise ServiceError(
                    "METADATA_INVALID", "message metadata is not a safe summary"
                )
            total_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            if total_bytes > self._MAX_METADATA_BYTES:
                raise ServiceError(
                    "METADATA_INVALID", "message metadata exceeds the summary limit"
                )
            sanitized[key] = value
        return sanitized

    @staticmethod
    def _message_projection(message: Any) -> dict[str, Any]:
        return {
            "delivery_id": message.delivery_id,
            "correlation_id": message.correlation_id,
            "artifact_hash": message.artifact_hash,
            "metadata": dict(message.redacted_metadata),
            "created_at": message.created_at,
            "expires_at": message.expires_at,
            "delivery_state": message.delivery_state,
        }

    def _finish_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        expected_version: int,
        owner: str,
        epoch: int,
        now: str | None,
    ) -> Task:
        return self._call(
            self._store.complete_task,
            task_id,
            state,
            expected_version,
            owner,
            epoch,
            now=now,
        )

    def _require_current_coordinator(
        self,
        coordinator: Task,
        *,
        workflow_id: str,
        coordinator_owner: str,
        coordinator_epoch: int,
        now: str,
    ) -> None:
        if (
            coordinator.workflow_id != workflow_id
            or coordinator.owner_role not in {"sol", "opus"}
            or coordinator.state is not TaskState.RUNNING
        ):
            raise ServiceError(
                "ACCEPTANCE_FORBIDDEN", "coordinator is not authorized for acceptance"
            )
        lease = self._call(self._store.get_lease, coordinator.id)
        if (
            lease is None
            or lease.owner != coordinator_owner
            or lease.epoch != coordinator_epoch
            or lease.expires_at <= now
        ):
            raise ServiceError("STALE_LEASE", "coordinator lease is not current")

    @staticmethod
    def _require_current_code_task(
        task: Task,
        *,
        workflow_id: str,
        expected_version: int,
    ) -> None:
        if task.workflow_id != workflow_id or task.task_kind is not TaskKind.CODE:
            raise ServiceError(
                "ACCEPTANCE_FORBIDDEN", "task is not an accepted code task"
            )
        if task.version != expected_version:
            raise ServiceError("VERSION_CONFLICT", "code task version is not current")
        if task.state is not TaskState.DONE:
            raise ServiceError("INVALID_STATE", "code task is not complete")
        if not task.write_scope or not task.intent_id or not task.language:
            raise ServiceError(
                "ACCEPTANCE_FORBIDDEN", "code task metadata is incomplete"
            )

    def _validate_current_index_evidence(self, task: Task, binding: Any) -> None:
        if (
            not binding.input_snapshot_id
            or not binding.output_snapshot_id
            or not binding.indexed_diff_hash
        ):
            raise ServiceError(
                "INDEXED_DIFF_REQUIRED", "strict output evidence is missing"
            )
        self._assert_index_current(
            Path(binding.workspace_root), binding.output_snapshot_id, task.write_scope
        )
        if self._index_service is None:
            raise ServiceError(
                "INDEX_UNAVAILABLE", "project index service is unavailable"
            )
        try:
            indexed_diff = self._index_service.diff(
                binding.input_snapshot_id, binding.output_snapshot_id
            )
            computed_hash = self._index_diff_hash(indexed_diff)
        except ServiceError:
            raise
        except Exception as error:
            self._raise_index_error(error)
        if computed_hash != binding.indexed_diff_hash:
            raise ServiceError("SNAPSHOT_MISMATCH", "indexed diff is not current")

    def _validate_checkpoint_evidence(
        self,
        *,
        workflow_id: str,
        task: Task,
        binding: Any,
    ) -> Any:
        if not binding.checkpoint_id:
            raise ServiceError("CHECKPOINT_REQUIRED", "code task checkpoint is missing")
        if self._checkpoint_service is None:
            raise ServiceError("INDEX_UNAVAILABLE", "checkpoint service is unavailable")
        try:
            checkpoint = self._checkpoint_service.status(binding.checkpoint_id)
        except Exception as error:
            self._raise_index_error(error)
        if (
            checkpoint.checkpoint_id != binding.checkpoint_id
            or checkpoint.kind != "checkpoint"
            or checkpoint.workflow_id != workflow_id
            or checkpoint.task_id != task.id
            or checkpoint.workspace_root != binding.workspace_root
            or checkpoint.snapshot_id != binding.input_snapshot_id
            or tuple(checkpoint.write_scope) != task.write_scope
        ):
            raise ServiceError("SNAPSHOT_MISMATCH", "checkpoint binding is not current")
        return checkpoint

    def _validate_output_query_evidence(
        self, trace_id: str, output_snapshot_id: str
    ) -> None:
        if self._index_service is None:
            raise ServiceError(
                "INDEX_UNAVAILABLE", "project index service is unavailable"
            )
        try:
            receipt = self._index_service.get_query_receipt(trace_id)
        except Exception as error:
            self._raise_index_error(error)
        if receipt.snapshot_id != output_snapshot_id:
            raise ServiceError("SNAPSHOT_MISMATCH", "output query is not current")

    def _validate_verification_artifacts(
        self,
        task: Task,
        artifact_hashes: tuple[str, ...],
        *,
        receipt_attestation_hash: str,
    ) -> None:
        if not task.result_hash:
            raise ServiceError("EVIDENCE_INCOMPLETE", "task output evidence is missing")
        if task.result_hash not in artifact_hashes:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "task output evidence is not verified"
            )
        if receipt_attestation_hash not in artifact_hashes:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "execution receipt attestation is unavailable"
            )
        for artifact_hash in artifact_hashes:
            if (
                artifact_hash == receipt_attestation_hash
                and artifact_hash != task.result_hash
            ):
                continue
            artifact = self._call(self._store.get_artifact, artifact_hash)
            if artifact is None or artifact.kind != "verification":
                raise ServiceError(
                    "EVIDENCE_INCOMPLETE", "verification evidence is unavailable"
                )

    def _validate_receipt_evidence(self, receipt_ids: tuple[str, ...]) -> str:
        if self._receipt_repository is None:
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "receipt repository is unavailable"
            )
        receipts: list[Any] = []
        unreadable = False
        for receipt_id in receipt_ids:
            try:
                receipts.append(self._receipt_repository.read(receipt_id))
            except Exception:
                unreadable = True
        if unreadable or len(receipts) != len(receipt_ids):
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "execution receipt is unavailable"
            )
        workspace_hashes: set[str] = set()
        for receipt in receipts:
            workspace_hash = getattr(receipt, "workspace_hash", None)
            if (
                not isinstance(workspace_hash, str)
                or self._SAFE_RECEIPT_IDENTIFIER.fullmatch(workspace_hash) is None
            ):
                raise ServiceError(
                    "EVIDENCE_INCOMPLETE", "execution workspace is unavailable"
                )
            workspace_hashes.add(workspace_hash)
        if (
            len(workspace_hashes) != 1
            or any(
                getattr(receipt, "success", None) is not True
                or getattr(receipt, "exit_code", None) != 0
                for receipt in receipts
            )
            or not any(
                getattr(receipt, "canonical_tool", None) == "patch"
                for receipt in receipts
            )
            or not any(
                getattr(receipt, "canonical_tool", None) == "shell"
                for receipt in receipts
            )
        ):
            raise ServiceError(
                "EVIDENCE_INCOMPLETE", "execution evidence is incomplete"
            )
        return next(iter(workspace_hashes))

    def _validate_acceptance_request(
        self,
        *,
        workflow_id: str,
        code_task_id: str,
        expected_code_task_version: int,
        expected_output_snapshot_id: str,
        coordinator_task_id: str,
        coordinator_owner: str,
        coordinator_epoch: int,
        execution_receipt_ids: list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        for field_name, value in (
            ("workflow_id", workflow_id),
            ("code_task_id", code_task_id),
            ("expected_output_snapshot_id", expected_output_snapshot_id),
            ("coordinator_task_id", coordinator_task_id),
            ("coordinator_owner", coordinator_owner),
        ):
            self._require_acceptance_identifier(field_name, value)
        if (
            isinstance(expected_code_task_version, bool)
            or not isinstance(expected_code_task_version, int)
            or expected_code_task_version < 0
        ):
            raise ServiceError("INVALID_REQUEST", "code task version is invalid")
        if (
            isinstance(coordinator_epoch, bool)
            or not isinstance(coordinator_epoch, int)
            or coordinator_epoch < 1
        ):
            raise ServiceError("INVALID_REQUEST", "coordinator epoch is invalid")
        return self._validate_execution_receipt_ids(execution_receipt_ids)

    def _validate_execution_receipt_ids(
        self, execution_receipt_ids: list[str] | tuple[str, ...]
    ) -> tuple[str, ...]:
        if not isinstance(execution_receipt_ids, (list, tuple)):
            raise ServiceError("INVALID_REQUEST", "receipt identifiers must be a list")
        receipt_ids = tuple(execution_receipt_ids)
        if not 1 <= len(receipt_ids) <= self._MAX_ACCEPTANCE_RECEIPTS:
            raise ServiceError("EVIDENCE_INCOMPLETE", "receipt count is invalid")
        if any(not isinstance(receipt_id, str) for receipt_id in receipt_ids) or len(
            set(receipt_ids)
        ) != len(receipt_ids):
            raise ServiceError("EVIDENCE_INCOMPLETE", "receipt identifiers are invalid")
        if any(
            self._SAFE_RECEIPT_IDENTIFIER.fullmatch(receipt_id) is None
            for receipt_id in receipt_ids
        ):
            raise ServiceError("EVIDENCE_INCOMPLETE", "receipt identifiers are invalid")
        return tuple(sorted(receipt_ids))

    @classmethod
    def _require_acceptance_identifier(cls, field_name: str, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or cls._SAFE_ACCEPTANCE_IDENTIFIER.fullmatch(value) is None
        ):
            raise ServiceError("INVALID_REQUEST", f"{field_name} is invalid")

    @staticmethod
    def _acceptance_timestamp(now: str | None) -> str:
        if now is None:
            return datetime.now(UTC).isoformat()
        if not isinstance(now, str):
            raise ServiceError("INVALID_REQUEST", "acceptance time is invalid")
        try:
            parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as error:
            raise ServiceError(
                "INVALID_REQUEST", "acceptance time is invalid"
            ) from error
        if parsed.tzinfo is None:
            raise ServiceError("INVALID_REQUEST", "acceptance time is invalid")
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _index_diff_hash(indexed_diff: Any) -> str:
        if not is_dataclass(indexed_diff) or isinstance(indexed_diff, type):
            raise ServiceError("INDEX_CORRUPT", "indexed diff is invalid")
        try:
            payload = asdict(indexed_diff)
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ServiceError("INDEX_CORRUPT", "indexed diff is invalid") from error
        return f"sha256:{sha256(encoded).hexdigest()}"

    @staticmethod
    def _canonical_workspace(workspace_root: str) -> Path:
        if not isinstance(workspace_root, str) or not workspace_root.strip():
            raise ServiceError("INDEX_UNAVAILABLE", "strict workspace is missing")
        supplied = Path(workspace_root).expanduser()
        if not supplied.is_absolute():
            raise ServiceError("INDEX_UNAVAILABLE", "strict workspace is not absolute")
        try:
            canonical = supplied.resolve(strict=True)
        except OSError as error:
            raise ServiceError(
                "INDEX_UNAVAILABLE", "strict workspace is unavailable"
            ) from error
        if not canonical.is_dir():
            raise ServiceError(
                "INDEX_UNAVAILABLE", "strict workspace is not a directory"
            )
        return canonical

    def _assert_index_current(
        self,
        workspace: Path,
        snapshot_id: str,
        required_paths: tuple[str, ...],
    ) -> Any:
        if self._index_service is None:
            raise ServiceError(
                "INDEX_UNAVAILABLE", "project index service is unavailable"
            )
        try:
            return self._index_service.assert_current(
                workspace,
                snapshot_id,
                required_paths=tuple(required_paths) or None,
            )
        except Exception as error:
            self._raise_index_error(error)

    @staticmethod
    def _raise_index_error(error: Exception) -> None:
        code = str(getattr(error, "code", "INDEX_UNAVAILABLE"))
        raise ServiceError(code, "project index operation rejected") from error

    @staticmethod
    def _call(operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except StoreError as error:
            raise ServiceError(
                getattr(error, "code", "STORE_ERROR"), "workflow operation rejected"
            ) from error
        except KeyError as error:
            raise ServiceError(
                "NOT_FOUND", "workflow resource was not found"
            ) from error
        except ValueError as error:
            raise ServiceError(
                "INVALID_REQUEST", "workflow request is invalid"
            ) from error
