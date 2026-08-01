"""Relay v3 capability and evidence authority boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.service import RelayError

from test_relay_runtime import (
    bind_worker,
    issue_worker,
    plan,
    service,
    task,
    worker_request,
)


def test_capabilities_bind_worker_action_epoch_endpoint_and_scope(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="capability-create",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    evidence_token = issue_worker(relay, action, lifecycle_action="evidence")

    with pytest.raises(RelayError, match="RELAY_CAPABILITY_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="evidence",
                capability=evidence_token,
                expected_task_version=task_version,
                endpoint="wrong-endpoint",
                evidence={
                    "kind": "pytest",
                    "selector": "tests/writer-a.py",
                    "digest": "sha256:" + "d" * 64,
                },
            )
        )

    with pytest.raises(RelayError, match="RELAY_CAPABILITY_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="heartbeat",
                capability=evidence_token,
                expected_task_version=task_version,
            )
        )

    wrong_epoch = worker_request(
        action,
        lifecycle_action="evidence",
        capability=evidence_token,
        expected_task_version=task_version,
        evidence={
            "kind": "pytest",
            "selector": "tests/writer-a.py",
            "digest": "sha256:" + "d" * 64,
        },
    )
    wrong_epoch["epoch"] = int(action["lease"]["epoch"]) + 1
    with pytest.raises(RelayError, match="RELAY_CAPABILITY_INVALID"):
        relay.handoff(wrong_epoch)

    tampered = evidence_token[:-1] + ("0" if evidence_token[-1] != "0" else "1")
    with pytest.raises(RelayError, match="RELAY_CAPABILITY_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="evidence",
                capability=tampered,
                expected_task_version=task_version,
                evidence={
                    "kind": "pytest",
                    "selector": "tests/writer-a.py",
                    "digest": "sha256:" + "d" * 64,
                },
            )
        )

    with pytest.raises(RelayError, match="RELAY_CAPABILITY_SCOPE"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="review",
                capability=relay.issue_worker_capability(
                    workflow_id="relay-runtime-v3",
                    task_id="writer-a",
                    action="review",
                    epoch=int(action["lease"]["epoch"]),
                    endpoint="worker-a",
                ),
                expected_task_version=task_version,
            )
        )

    with pytest.raises(RelayError, match="RELAY_CAPABILITY_SCOPE"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="evidence",
                capability=relay.issue_sol_capability(
                    workflow_id="relay-runtime-v3",
                    task_id="writer-a",
                    action="evidence",
                    epoch=int(action["lease"]["epoch"]),
                    endpoint="worker-a",
                ),
                expected_task_version=task_version,
                evidence={
                    "kind": "pytest",
                    "selector": "tests/writer-a.py",
                    "digest": "sha256:" + "d" * 64,
                },
            )
        )

    saved = relay.handoff(
        worker_request(
            action,
            lifecycle_action="evidence",
            capability=evidence_token,
            expected_task_version=task_version,
            evidence={
                "kind": "pytest",
                "selector": "tests/writer-a.py",
                "digest": "sha256:" + "d" * 64,
            },
        )
    )
    assert saved["evidence"]["kind"] == "pytest"


def test_malformed_lifecycle_endpoint_and_capability_stay_in_relay_errors(
    tmp_path: Path,
) -> None:
    relay, _store = service(tmp_path)
    created = relay.start_create(
        plan(
            task(
                "writer-a",
                write_scope=[{"path": "mcp-tools/a.py", "kind": "file"}],
            )
        ),
        idempotency_key="malformed-lifecycle",
    )
    action = created["host_actions"][0]
    assert isinstance(action, dict)
    task_version = bind_worker(relay, action)
    token = issue_worker(relay, action, lifecycle_action="heartbeat")

    with pytest.raises(RelayError, match="RELAY_REQUEST_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="heartbeat",
                capability=token,
                expected_task_version=task_version,
                endpoint="invalid\nendpoint",
            )
        )

    with pytest.raises(RelayError, match="RELAY_CAPABILITY_INVALID"):
        relay.handoff(
            worker_request(
                action,
                lifecycle_action="heartbeat",
                capability={"not": "a-token"},
                expected_task_version=task_version,
            )
        )
