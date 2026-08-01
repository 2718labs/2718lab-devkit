"""Durable Relay v3 records and lifecycle states.

These models carry only protocol data.  Host prompts, absolute paths, bearer
capabilities, and host-process handles are intentionally excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RelayTaskState(str, Enum):
    """Persisted task states projected into Relay's five queues."""

    PREPARED = "prepared"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    REVIEW_INTEGRATION = "review_integration"
    COMPLETED = "completed"
    INTEGRATED = "integrated"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


ACTIVE_TASK_STATES = frozenset({RelayTaskState.LEASED, RelayTaskState.RUNNING})
TERMINAL_TASK_STATES = frozenset(
    {
        RelayTaskState.COMPLETED,
        RelayTaskState.INTEGRATED,
        RelayTaskState.REJECTED,
        RelayTaskState.BLOCKED,
        RelayTaskState.CANCELLED,
    }
)


@dataclass(frozen=True)
class RelayRun:
    """One durable execution of an immutable compiled Relay plan."""

    run_id: str
    workflow_id: str
    plan_hash: str
    workspace_id: str
    input_snapshot_id: str
    base_commit: str
    capacity: int
    schedule_version: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "plan_hash": self.plan_hash,
            "workspace_id": self.workspace_id,
            "input_snapshot_id": self.input_snapshot_id,
            "base_commit": self.base_commit,
            "capacity": self.capacity,
            "schedule_version": self.schedule_version,
        }


@dataclass(frozen=True)
class RelayTask:
    """A task and its current durable scheduling authority."""

    run_id: str
    task_id: str
    ordinal: int
    kind: str
    priority: int
    contract: dict[str, Any]
    state: RelayTaskState
    task_version: int
    scope_owner: str | None
    candidate_id: str | None
    last_lease_epoch: int

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "priority": self.priority,
            "state": self.state.value,
            "task_version": self.task_version,
            "scope_owner": self.scope_owner,
            "candidate_id": self.candidate_id,
            "last_lease_epoch": self.last_lease_epoch,
        }

    def task_contract(self) -> dict[str, object]:
        """Return the immutable, host-safe task contract."""

        return dict(self.contract)


@dataclass(frozen=True)
class RelayLease:
    """One epoch-bound dispatch lease, not proof of a host spawn."""

    lease_id: str
    run_id: str
    task_id: str
    action_id: str
    epoch: int
    task_version: int
    lease_kind: str
    endpoint: str | None
    state: str
    created_at: str
    released_at: str | None

    def to_public_tuple(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "epoch": self.epoch,
            "task_version": self.task_version,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "epoch": self.epoch,
            "task_version": self.task_version,
            "lease_kind": self.lease_kind,
            "endpoint": self.endpoint,
            "state": self.state,
            "released_at": self.released_at,
        }


@dataclass(frozen=True)
class RefillDirective:
    """A single-use, compare-and-swap refill instruction."""

    directive_id: str
    run_id: str
    workflow_id: str
    task_id: str
    expected_schedule_version: int
    route: dict[str, str]
    state: str

    def to_dict(self) -> dict[str, object]:
        return {
            "directive_id": self.directive_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "expected_schedule_version": self.expected_schedule_version,
            "route": dict(self.route),
            "relay_start_request": {
                "mode": "refill",
                "workflow_id": self.workflow_id,
                "refill_directive_id": self.directive_id,
                "expected_schedule_version": self.expected_schedule_version,
            },
        }
