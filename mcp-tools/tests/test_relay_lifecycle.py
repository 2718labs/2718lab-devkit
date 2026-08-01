"""Relay v3 worker handoff and Sol-only integration behavior."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.service import RelayService

from test_relay_runtime import (
    _BASE_COMMIT,
    bind_worker,
    issue_worker,
    plan,
    service,
    task,
    worker_request,
)


_EVIDENCE_HASH = "sha256:" + "d" * 64


def _record_evidence(
    relay: RelayService, action: dict[str, object], task_version: int
) -> int:
    result = relay.handoff(
        worker_request(
            action,
            lifecycle_action="evidence",
            capability=issue_worker(relay, action, lifecycle_action="evidence"),
            expected_task_version=task_version,
            evidence={
                "kind": "pytest",
                "selector": f"tests/{action['task_id']}.py",
                "digest": _EVIDENCE_HASH,
            },
        )
    )
    task_data = result["task"]
    assert isinstance(task_data, dict)
    return int(task_data["task_version"])


def test_candidate_handoff_releases_worker_slot_and_sol_integration_unblocks_dependency(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                priority=100,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-b",
                priority=90,
                write_scope=[{"path": "mcp-tools/b.py", "kind": "file"}],
            ),
            task(
                "writer-overlap",
                priority=95,
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            ),
            task(
                "writer-c",
                priority=80,
                write_scope=[{"path": "mcp-tools/c.py", "kind": "file"}],
            ),
            task(
                "writer-child",
                priority=70,
                dependencies=["writer-a"],
                write_scope=[{"path": "mcp-tools/child.py", "kind": "file"}],
            ),
        ),
        idempotency_key="candidate-create",
    )
    actions = {item["task_id"]: item for item in created["host_actions"]}
    writer = actions["writer-a"]
    assert isinstance(writer, dict)
    task_version = bind_worker(relay, writer)
    task_version = _record_evidence(relay, writer, task_version)

    handed_off = relay.handoff(
        worker_request(
            writer,
            lifecycle_action="candidate_handoff",
            capability=issue_worker(
                relay, writer, lifecycle_action="candidate_handoff"
            ),
            expected_task_version=task_version,
            candidate={
                "candidate_id": "candidate-a",
                "branch": "relay/candidate-a",
                "base_commit": _BASE_COMMIT,
                "head_commit": "e" * 40,
                "diff_hash": "sha256:" + "f" * 64,
                "evidence_hashes": [_EVIDENCE_HASH],
                "pr_reference": None,
            },
        )
    )
    candidate_task = handed_off["task"]
    assert isinstance(candidate_task, dict)
    assert candidate_task["state"] == "review_integration"
    assert candidate_task["scope_owner"] == "sol"

    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["review_integration"]] == [
        "writer-a"
    ]
    assert [item["task_id"] for item in status["queues"]["prepared_prewarms"]] == [
        "writer-child"
    ]
    directive = status["refill_directives"][0]
    assert directive["task_id"] == "writer-c"

    relay.start_refill(
        "relay-runtime-v3",
        str(directive["directive_id"]),
        expected_schedule_version=int(directive["expected_schedule_version"]),
        idempotency_key="candidate-refill",
    )

    lease = writer["lease"]
    assert isinstance(lease, dict)
    review = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-a",
            "action": "review",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-a",
                action="review",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "review_digest": "sha256:" + "1" * 64,
        }
    )
    assert review["candidate"]["status"] == "reviewed"

    integrated = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "writer-a",
            "action": "integrate",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": candidate_task["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="writer-a",
                action="integrate",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
            "candidate_id": "candidate-a",
            "integration_head": _BASE_COMMIT,
            "integration_commit": "2" * 40,
        }
    )
    assert integrated["task"]["state"] == "integrated"
    status = relay.status("relay-runtime-v3")
    assert [item["task_id"] for item in status["queues"]["ready"]] == [
        "writer-overlap",
        "writer-child",
    ]


def test_readonly_worker_needs_sol_approval_before_it_satisfies_dependencies(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task("verify", kind="verification", required_evidence=[]),
            task(
                "verify-child",
                kind="verification",
                dependencies=["verify"],
                required_evidence=[],
            ),
            capacity=1,
        ),
        idempotency_key="readonly-create",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    handed_off = relay.handoff(
        worker_request(
            action,
            lifecycle_action="terminal",
            capability=issue_worker(relay, action, lifecycle_action="terminal"),
            expected_task_version=task_version,
            outcome="completed",
        )
    )
    assert handed_off["task"]["state"] == "review_integration"
    assert relay.status("relay-runtime-v3")["queues"]["ready"] == []

    lease = action["lease"]
    assert isinstance(lease, dict)
    approved = relay.integrate(
        {
            "workflow_id": "relay-runtime-v3",
            "task_id": "verify",
            "action": "approve_readonly",
            "epoch": lease["epoch"],
            "endpoint": "sol-main",
            "expected_task_version": handed_off["task"]["task_version"],
            "capability": relay.issue_sol_capability(
                workflow_id="relay-runtime-v3",
                task_id="verify",
                action="approve_readonly",
                epoch=int(lease["epoch"]),
                endpoint="sol-main",
            ),
        }
    )
    assert approved["task"]["state"] == "completed"
    assert [
        item["task_id"] for item in relay.status("relay-runtime-v3")["queues"]["ready"]
    ] == ["verify-child"]
