"""Relay host admission happens before any worker capability delivery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.service import RelayError
from devkit_runtime.relay_runtime import RelayRuntime


class _RelayService:
    """Minimal relay boundary whose capability method records any delivery."""

    def __init__(self) -> None:
        self.capability_issues: list[dict[str, object]] = []
        self.events: list[str] = []
        self.attempt_state = "prepared"
        self.delivery_actions: list[dict[str, object]] = []
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
        self.events.append("start")
        return {"host_actions": [dict(action) for action in self.host_actions]}

    def start_attempt(self, _idempotency_key: object) -> dict[str, object]:
        self.events.append("load_attempt")
        return {"attempt_id": "attempt-a", "state": self.attempt_state}

    def mark_start_admitted(self, _attempt_id: object) -> dict[str, object]:
        self.events.append("mark_admitted")
        self.attempt_state = "admitted"
        return {"attempt_id": "attempt-a", "state": self.attempt_state}

    def capability_key_id(self) -> str:
        return "sha256:" + "9" * 64

    def initialize_start_delivery(
        self, _attempt_id: object, facts: object
    ) -> dict[str, object]:
        assert type(facts) is list
        self.events.append("mark_admitted")
        self.attempt_state = "admitted"
        self.delivery_actions = [{**fact, "delivered": False} for fact in facts]
        return {
            "attempt_id": "attempt-a",
            "state": self.attempt_state,
            "actions": [dict(fact) for fact in self.delivery_actions],
        }

    def start_delivery(self, _attempt_id: object) -> dict[str, object]:
        return {
            "attempt_id": "attempt-a",
            "state": self.attempt_state,
            "actions": [dict(fact) for fact in self.delivery_actions],
        }

    def record_start_action_delivery(
        self, _attempt_id: object, action_id: object, _receipt: object
    ) -> dict[str, object]:
        for fact in self.delivery_actions:
            if fact["action_id"] == action_id:
                fact["delivered"] = True
                return dict(fact)
        raise AssertionError("unknown action")

    def mark_start_delivered(self, _attempt_id: object) -> dict[str, object]:
        self.events.append("mark_delivered")
        self.attempt_state = "delivered"
        return {"attempt_id": "attempt-a", "state": self.attempt_state}

    def abort_start_attempt(
        self, _attempt_id: object, *, error_code: object
    ) -> dict[str, object]:
        self.events.append(f"abort:{error_code}")
        self.attempt_state = "aborted"
        return {"attempt_id": "attempt-a", "state": self.attempt_state}

    def issue_worker_capability(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
        expires_at: int | None = None,
    ) -> str:
        self.capability_issues.append(
            {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "action": action,
                "epoch": epoch,
                "endpoint": endpoint,
                "expires_at": expires_at,
            }
        )
        return f"capability:{action}"


class _Broker:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.deliveries: list[dict[str, object]] = []

    @property
    def is_available(self) -> bool:
        return True

    def prepare_capability(self, **kwargs: object) -> object:
        self._events.append("broker")
        self.deliveries.append(dict(kwargs))
        capabilities = kwargs["capabilities"]
        assert isinstance(capabilities, dict)
        return {
            "action_id": kwargs["action_id"],
            "endpoint": kwargs["endpoint"],
            "bundle_hash": canonical_hash(dict(sorted(capabilities.items()))),
        }


class _HostAdmission:
    def __init__(self, events: list[str], *, admitted: bool) -> None:
        self._events = events
        self._admitted = admitted
        self.actions: list[dict[str, object]] = []

    @property
    def is_available(self) -> bool:
        return True

    def admit_relay_actions(self, actions: object) -> bool:
        self._events.append("host")
        if type(actions) is not list:
            return False
        self.actions = [dict(action) for action in actions if type(action) is dict]
        return self._admitted and len(self.actions) == len(actions)


def test_runtime_host_admission_precedes_all_capability_delivery() -> None:
    rejected_relay = _RelayService()
    rejected_broker = _Broker(rejected_relay.events)
    rejected_host = _HostAdmission(rejected_relay.events, admitted=False)
    rejected_runtime = RelayRuntime(
        rejected_relay,
        capability_broker=rejected_broker,
        host_session=rejected_host,
    )

    with pytest.raises(RelayError) as rejected:
        rejected_runtime.start(
            {"mode": "create", "idempotency_key": "rejected-attempt"}
        )

    assert rejected.value.code == "RELAY_HOST_ACTION_REJECTED"
    assert rejected_host.actions == [rejected_relay.host_actions[0]]
    assert rejected_relay.capability_issues == []
    assert rejected_broker.deliveries == []
    assert rejected_relay.events == [
        "start",
        "load_attempt",
        "host",
        "abort:RELAY_HOST_ACTION_REJECTED",
    ]

    admitted_relay = _RelayService()
    admitted_broker = _Broker(admitted_relay.events)
    admitted_host = _HostAdmission(admitted_relay.events, admitted=True)
    result = RelayRuntime(
        admitted_relay,
        capability_broker=admitted_broker,
        host_session=admitted_host,
    ).start({"mode": "create", "idempotency_key": "admitted-attempt"})

    assert admitted_host.actions == [result["host_actions"][0]]
    assert len(admitted_relay.capability_issues) == 10
    assert len(admitted_broker.deliveries) == 2
    assert admitted_relay.events == [
        "start",
        "load_attempt",
        "host",
        "mark_admitted",
        "broker",
        "broker",
        "mark_delivered",
    ]
