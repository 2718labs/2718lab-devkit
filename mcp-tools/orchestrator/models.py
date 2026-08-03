"""Immutable domain records used by the pure workflow scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum


class WorkflowKind(str, Enum):
    LINEAR = "linear"
    DAG = "dag"


class WorkflowState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(str, Enum):
    NEW = "new"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskKind(str, Enum):
    GENERAL = "general"
    CODE = "code"


class AtlasOutboxState(str, Enum):
    PENDING = "pending"
    PROJECTED = "projected"
    QUARANTINED = "quarantined"


class ExternalBootstrapState(StrEnum):
    """The repository-only bootstrap slice never advances beyond pending."""

    PENDING = "pending"


class RoleEnvelopeDirection(Enum):
    """The three bounded directions supported by durable role messaging."""

    COORDINATOR_TO_WORKER = "coordinator_to_worker"
    WORKER_TO_COORDINATOR = "worker_to_coordinator"
    PEER_TO_PEER = "peer_to_peer"


@dataclass(frozen=True)
class Workflow:
    id: str
    kind: WorkflowKind
    title: str
    product_summary: str
    state: WorkflowState = WorkflowState.NEW
    version: int = 0
    policy_version: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Task:
    id: str
    workflow_id: str
    title: str
    owner_role: str
    state: TaskState = TaskState.NEW
    dependencies: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    card_hash: str = ""
    result_hash: str = ""
    version: int = 0
    task_kind: TaskKind = TaskKind.GENERAL
    intent_id: str = ""
    language: str = ""
    framework: str = ""


@dataclass(frozen=True)
class CodeTaskAcceptance:
    """Content-addressed, immutable metadata for a code-task acceptance."""

    acceptance_id: str
    workflow_id: str
    code_task_id: str
    code_task_version: int
    input_snapshot_id: str
    output_snapshot_id: str
    indexed_diff_hash: str
    intent_id: str
    language: str
    framework: str
    payload_hash: str
    created_at: str

    @property
    def task_id(self) -> str:
        """Return the accepted task id using the generic task naming."""
        return self.code_task_id

    @property
    def task_version(self) -> int:
        """Return the accepted task version using the generic task naming."""
        return self.code_task_version


@dataclass(frozen=True)
class AtlasOutboxItem:
    """Durable, privacy-bounded ingestion work for an accepted code task."""

    ingestion_key: str
    acceptance_id: str
    payload_hash: str
    state: AtlasOutboxState
    attempts: int
    last_error_code: str
    reason_codes: tuple[str, ...]
    created_at: str
    updated_at: str

    @property
    def attempt_count(self) -> int:
        """Return the durable retry count using the storage column naming."""
        return self.attempts

    @property
    def reasons(self) -> tuple[str, ...]:
        """Return stable reason codes using the projection-facing naming."""
        return self.reason_codes


@dataclass(frozen=True)
class RoleRiskItem:
    """One bounded, reference-only risk carried by a worker terminal packet."""

    code: str
    severity: str
    evidence_hash: str


@dataclass(frozen=True)
class RoleEnvelope:
    """Durable, role-scoped mailbox entry with no transcript-bearing fields."""

    delivery_id: str
    sequence: int
    direction: RoleEnvelopeDirection
    workflow_id: str
    sender_task_id: str
    sender_role: str
    sender_epoch: int
    recipient_task_id: str
    recipient_role: str
    recipient_epoch: int
    correlation_id: str
    assignment_token_hash: str
    dispatch_context_hash: str
    route_provenance_hash: str
    coordinator_task_id: str
    coordinator_epoch: int
    correlation_fence_hash: str
    task_card_hash: str
    contract_hashes: tuple[str, ...]
    index_evidence_hashes: tuple[str, ...]
    terminal_result_hash: str
    evidence_hashes: tuple[str, ...]
    dependency_hashes: tuple[str, ...]
    recipient_capability_hash: str
    risk_items: tuple[RoleRiskItem, ...]
    issued_at: str
    expires_at: str
    delivery_state: str
    acknowledged_at: str | None
    envelope_hash: str


@dataclass(frozen=True)
class HostOperationReceipt:
    """A reference-only host-operation report; it is never a task transition."""

    operation_id: str
    workflow_id: str
    task_id: str
    operation: str
    lease_epoch: int
    assignment_token_hash: str
    dispatch_context_hash: str
    route_provenance_hash: str
    coordinator_task_id: str
    coordinator_epoch: int
    errno: int
    status_code: str
    outcome: str
    receipt_hash: str
    reported_at: str


@dataclass(frozen=True)
class ExternalSourceDescriptor:
    """Hash-only identity of a source checkout; no local path or source body."""

    descriptor_hash: str
    source_hash: str
    repository_hash: str
    common_dir_hash: str
    project_hash: str
    task_root_hash: str
    ref_hash: str
    commit_hash: str
    tree_hash: str


@dataclass(frozen=True)
class ExternalBootstrapBatchItem:
    """One immutable, reference-only assignment in a bootstrap batch."""

    item_index: int
    workflow_id: str
    task_id: str
    lease_epoch: int
    plan_hash: str
    projection_hash: str
    assignment_hash: str
    predecessor_hash: str
    quota_hash: str
    route_hash: str


@dataclass(frozen=True)
class ExternalBootstrapBatch:
    """Canonical batch bound to one external source descriptor."""

    batch_hash: str
    descriptor_hash: str
    idempotency_key: str
    items: tuple[ExternalBootstrapBatchItem, ...]
    expires_at: str
    state: ExternalBootstrapState = ExternalBootstrapState.PENDING
    availability: str = "HOST_API_UNAVAILABLE"


@dataclass(frozen=True)
class ExternalBootstrapOutboxItem:
    """Retained descriptor for a future Host owner; it is not an enqueue request."""

    batch_hash: str
    descriptor_hash: str
    state: ExternalBootstrapState
    availability: str
    created_at: str


@dataclass(frozen=True)
class ExternalDispatchGrant:
    """One-shot, hash-bound dispatch authority with no Host capability."""

    grant_id: str
    descriptor_hash: str
    batch_hash: str
    assignment_hash: str
    expires_at: str
    state: ExternalBootstrapState = ExternalBootstrapState.PENDING
    availability: str = "HOST_API_UNAVAILABLE"
    consumed_at: str | None = None
