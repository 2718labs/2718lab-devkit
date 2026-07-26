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
