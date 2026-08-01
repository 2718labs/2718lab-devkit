"""Relay v3 durable start, status, and refill behavior."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelayStore


_BASE_COMMIT = "a" * 40
_INPUT_SNAPSHOT = "sha256:" + "b" * 64
_ATLAS_PACKET = "sha256:" + "c" * 64


def task(
    task_id: str,
    *,
    kind: str = "implementation",
    priority: int = 50,
    dependencies: list[str] | None = None,
    write_scope: list[dict[str, str]] | None = None,
    required_evidence: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build one canonical Relay v3 task for the runtime tests."""

    return {
        "task_id": task_id,
        "kind": kind,
        "title": f"{task_id} title",
        "objective": f"Complete the bounded {task_id} task.",
        "priority": priority,
        "dependencies": [] if dependencies is None else dependencies,
        "write_scope": [] if write_scope is None else write_scope,
        "route": {
            "route_class": "terra_max",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
        },
        "constraints": [
            {
                "code": "bounded",
                "detail": "Keep all work inside the declared contract.",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": f"{task_id}-accepted",
                "description": "The focused checks pass.",
            }
        ],
        "atlas_packet_ids": [_ATLAS_PACKET],
        "required_evidence": (
            [{"kind": "pytest", "selector": f"tests/{task_id}.py"}]
            if required_evidence is None
            else required_evidence
        ),
        "prewarm_for_task_id": None,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
    }


def plan(*raw_tasks: dict[str, object], capacity: int = 2) -> dict[str, object]:
    """Construct the immutable compiled-plan shape consumed by RelayService."""

    tasks = sorted(raw_tasks, key=lambda item: str(item["task_id"]))
    ready = sorted(
        (
            item
            for item in tasks
            if item["kind"] != "prewarm" and not item["dependencies"]
        ),
        key=lambda item: (-int(item["priority"]), str(item["task_id"])),
    )
    prewarms = sorted(
        (item for item in tasks if item["kind"] == "prewarm"),
        key=lambda item: (-int(item["priority"]), str(item["task_id"])),
    )
    body: dict[str, object] = {
        "schema": "2718lab-devkit/relay-plan-v1",
        "workflow_id": "relay-runtime-v3",
        "workspace_binding": {
            "workspace_id": "workspace-main",
            "input_snapshot_id": _INPUT_SNAPSHOT,
            "atlas_packet_ids": [_ATLAS_PACKET],
        },
        "base_commit": _BASE_COMMIT,
        "capacity": capacity,
        "runtime_policy_id": "2718lab-devkit/relay-runtime-policy-v1",
        "tasks": tasks,
        "dependencies": sorted(
            (
                {
                    "from_task_id": str(item["task_id"]),
                    "kind": "depends_on",
                    "to_task_id": dependency,
                }
                for item in tasks
                for dependency in item["dependencies"]
            ),
            key=lambda item: (str(item["from_task_id"]), str(item["to_task_id"])),
        ),
        "conflicts": [],
        "queues": {
            "prepared_prewarms": [item["task_id"] for item in prewarms],
            "ready": [item["task_id"] for item in ready],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        },
    }
    return {**body, "plan_hash": canonical_hash(body)}


def service(tmp_path: Path) -> tuple[RelayService, RelayStore]:
    store = RelayStore(tmp_path / "relay.sqlite3")
    return RelayService(store, capability_secret=b"relay-v3-test-secret"), store


def issue_worker(
    relay: RelayService,
    action: dict[str, object],
    *,
    lifecycle_action: str,
    endpoint: str = "worker-a",
) -> str:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return relay.issue_worker_capability(
        workflow_id="relay-runtime-v3",
        task_id=str(action["task_id"]),
        action=lifecycle_action,
        epoch=int(lease["epoch"]),
        endpoint=endpoint,
    )


def worker_request(
    action: dict[str, object],
    *,
    lifecycle_action: str,
    capability: str,
    expected_task_version: int,
    endpoint: str = "worker-a",
    **extra: object,
) -> dict[str, object]:
    lease = action["lease"]
    assert isinstance(lease, dict)
    return {
        "workflow_id": "relay-runtime-v3",
        "task_id": action["task_id"],
        "action": lifecycle_action,
        "epoch": lease["epoch"],
        "endpoint": endpoint,
        "expected_task_version": expected_task_version,
        "capability": capability,
        **extra,
    }


def bind_worker(relay: RelayService, action: dict[str, object]) -> int:
    lease = action["lease"]
    assert isinstance(lease, dict)
    result = relay.handoff(
        worker_request(
            action,
            lifecycle_action="bind_endpoint",
            capability=issue_worker(relay, action, lifecycle_action="bind_endpoint"),
            expected_task_version=int(lease["task_version"]),
        )
    )
    task_data = result["task"]
    assert isinstance(task_data, dict)
    return int(task_data["task_version"])


def test_start_is_idempotent_and_status_is_read_only(tmp_path: Path) -> None:
    relay, store = service(tmp_path)
    compiled = plan(
        task(
            "writer-a",
            priority=100,
            write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
        ),
        task(
            "writer-b",
            priority=50,
            write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
        ),
    )

    created = relay.start_create(compiled, idempotency_key="create-runtime")

    assert relay.start_create(compiled, idempotency_key="create-runtime") == created
    actions = created["host_actions"]
    assert isinstance(actions, list)
    assert [action["task_id"] for action in actions] == ["writer-a", "writer-b"]
    for action in actions:
        assert action["kind"] == "codex.spawn_agent"
        assert "capability" not in action
        assert "prompt" not in action
        assert "D:\\" not in repr(action)
        assert "C:\\" not in repr(action)

    before = store.database_fingerprint()
    status = relay.status("relay-runtime-v3")
    after = store.database_fingerprint()

    assert before == after
    assert status["schedule_version"] == created["schedule_version"]
    assert [item["task_id"] for item in status["queues"]["running_slots"]] == [
        "writer-a",
        "writer-b",
    ]

    changed = dict(compiled)
    changed["capacity"] = 1
    changed["plan_hash"] = canonical_hash(
        {key: value for key, value in changed.items() if key != "plan_hash"}
    )
    with pytest.raises(RelayError, match="RELAY_IDEMPOTENCY_CONFLICT"):
        relay.start_create(changed, idempotency_key="create-runtime")


def test_terminal_event_releases_slot_and_persists_refill_in_same_transition(
    tmp_path: Path,
) -> None:
    relay, store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                priority=100,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-b",
                priority=50,
                write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
            ),
            capacity=1,
        ),
        idempotency_key="create-terminal",
    )
    first = created["host_actions"][0]
    assert isinstance(first, dict)
    version = bind_worker(relay, first)

    terminal = relay.handoff(
        worker_request(
            first,
            lifecycle_action="terminal",
            capability=issue_worker(relay, first, lifecycle_action="terminal"),
            expected_task_version=version,
            outcome="blocked",
        )
    )

    assert terminal["schedule_version"] == created["schedule_version"] + 1
    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["terminal"]] == ["writer-a"]
    directive = status["refill_directives"][0]
    assert directive["task_id"] == "writer-b"
    assert directive["expected_schedule_version"] == terminal["schedule_version"]

    refilled = relay.start_refill(
        "relay-runtime-v3",
        str(directive["directive_id"]),
        expected_schedule_version=int(directive["expected_schedule_version"]),
        idempotency_key="refill-writer-b",
    )
    assert [action["task_id"] for action in refilled["host_actions"]] == ["writer-b"]
    assert store.database_fingerprint() != ""
