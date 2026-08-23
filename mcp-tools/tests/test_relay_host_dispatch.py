"""Relay host admission happens before any worker capability delivery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.service import RelayError
from devkit_runtime.relay_runtime import RelayRuntime


class _RelayService:
    """Minimal relay boundary whose capability method records any delivery."""

    def __init__(self) -> None:
        self.capability_issues: list[dict[str, object]] = []
        self.host_actions = [
            {
                "action_id": "action-a",
                "workflow_id": "workflow-a",
                "task_id": "writer-a",
                "lease": {"epoch": 1},
                "relay_host_scheduler_slot": {},
            },
            {
                "action_id": "action-b",
                "workflow_id": "workflow-a",
                "task_id": "reader-a",
                "lease": {"epoch": 1},
            },
        ]

    def start(self, _request: object) -> dict[str, object]:
        return {"host_actions": [dict(action) for action in self.host_actions]}

    def issue_worker_capability(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
    ) -> str:
        self.capability_issues.append(
            {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "action": action,
                "epoch": epoch,
                "endpoint": endpoint,
            }
        )
        return f"capability:{action}"


class _Broker:
    def __init__(self) -> None:
        self.deliveries: list[dict[str, object]] = []

    @property
    def is_available(self) -> bool:
        return True

    def prepare_capability(self, **kwargs: object) -> object:
        self.deliveries.append(dict(kwargs))
        return object()


class _HostAdmission:
    def __init__(self, *, admitted: bool) -> None:
        self._admitted = admitted
        self.actions: list[dict[str, object]] = []

    @property
    def is_available(self) -> bool:
        return True

    def admit_relay_actions(self, actions: object) -> bool:
        if type(actions) is not list:
            return False
        self.actions = [dict(action) for action in actions if type(action) is dict]
        return self._admitted and len(self.actions) == len(actions)


def test_runtime_host_admission_precedes_all_capability_delivery() -> None:
    rejected_relay = _RelayService()
    rejected_broker = _Broker()
    rejected_host = _HostAdmission(admitted=False)
    rejected_runtime = RelayRuntime(
        rejected_relay,
        capability_broker=rejected_broker,
        host_session=rejected_host,
    )

    with pytest.raises(RelayError) as rejected:
        rejected_runtime.start({"mode": "create"})

    assert rejected.value.code == "RELAY_HOST_ACTION_REJECTED"
    assert rejected_host.actions == [rejected_relay.host_actions[0]]
    assert rejected_relay.capability_issues == []
    assert rejected_broker.deliveries == []

    admitted_relay = _RelayService()
    admitted_broker = _Broker()
    admitted_host = _HostAdmission(admitted=True)
    result = RelayRuntime(
        admitted_relay,
        capability_broker=admitted_broker,
        host_session=admitted_host,
    ).start({"mode": "create"})

    assert admitted_host.actions == [result["host_actions"][0]]
    assert len(admitted_relay.capability_issues) == 10
    assert len(admitted_broker.deliveries) == 2
