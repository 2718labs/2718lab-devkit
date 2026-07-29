"""Immutable domain records used by the pure workflow scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
