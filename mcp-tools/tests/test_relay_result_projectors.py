from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from devkit_runtime.relay_projectors import (
    project_relay_handoff,
    project_relay_integrate,
    project_relay_start,
    project_relay_status,
)
from devkit_runtime.tool_result import ResultContractError

Projector = Callable[[object], dict[str, object]]


def _data(result: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], result["data"])


def _assert_no_null(value: object) -> None:
    if value is None:
        raise AssertionError("public result contains null")
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_null(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_null(item)


def _start_result() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-start-result-v1",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "schedule_version": 3,
        "host_actions": [
            {
                "action_id": "action-1",
                "kind": "codex.spawn_agent",
                "workflow_id": "workflow-1",
                "task_id": "task-1",
                "lease": {"lease_id": "lease-1", "epoch": 1, "task_version": 1},
                "route": {
                    "route_class": "terra_high",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                },
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
            }
        ],
    }


def _task(*, candidate_id: str | None = None) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "kind": "implementation",
        "priority": 90,
        "state": "review_integration",
        "task_version": 2,
        "scope_owner": "sol" if candidate_id else None,
        "candidate_id": candidate_id,
        "last_lease_epoch": 1,
    }


def _candidate(*, integrated: bool = False) -> dict[str, object]:
    return {
        "candidate_id": "candidate-1",
        "task_id": "task-1",
        "originating_epoch": 1,
        "branch": "codex/candidate-1",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "diff_hash": "sha256:" + "c" * 64,
        "evidence_hashes": ["sha256:" + "d" * 64],
        "pr_reference": None,
        "status": "integrated" if integrated else "pending_review",
        "review_digest": "sha256:" + "e" * 64 if integrated else None,
        "integration_commit": "f" * 40 if integrated else None,
        "integration_tree": "1" * 40 if integrated else None,
        "integration_proof_id": "sha256:" + "3" * 64 if integrated else None,
    }


def _status(*, with_proof: bool = False) -> dict[str, object]:
    proof = {
        "proof_id": "sha256:" + "3" * 64,
        "expectation_hash": "sha256:" + "4" * 64,
        "task_id": "task-1",
        "candidate_id": "candidate-1",
        "integration_version": 1,
        "predecessor_commit": "a" * 40,
        "final_commit": "f" * 40,
        "final_tree": "1" * 40,
        "attestor_id": "host-attestor",
        "attestor_version": "v1",
    }
    return {
        "schema": "2718lab-devkit/relay-status-v2",
        "workflow_id": "workflow-1",
        "run": {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "plan_hash": "sha256:" + "5" * 64,
            "workspace_id": "workspace-1",
            "input_snapshot_id": "sha256:" + "6" * 64,
            "base_commit": "a" * 40,
            "integration_head": "f" * 40 if with_proof else "a" * 40,
            "integration_version": 1 if with_proof else 0,
            "capacity": 1,
            "schedule_version": 3,
        },
        "schedule_version": 3,
        "tasks": [_task(candidate_id="candidate-1" if with_proof else None)],
        "leases": [
            {
                "lease_id": "lease-1",
                "task_id": "task-1",
                "action_id": "action-1",
                "epoch": 1,
                "task_version": 2,
                "lease_kind": "worker",
                "endpoint": None,
                "state": "released",
                "released_at": "2026-08-01T00:00:00Z",
            }
        ],
        "candidates": [_candidate(integrated=with_proof)] if with_proof else [],
        "integration_proofs": [proof] if with_proof else [],
        "outstanding_action_ids": [],
        "refill_directives": [],
        "queues": {
            "prepared_prewarms": [],
            "ready": [],
            "running_slots": [],
            "review_integration": [_task(candidate_id="candidate-1")]
            if with_proof
            else [_task()],
            "terminal": [],
        },
    }


def test_start_projects_locked_schema_and_redacts_host_action_fields() -> None:
    projected = project_relay_start(_start_result())
    _assert_no_null(projected)

    assert set(_data(projected)) == {
        "workflow_id",
        "run_id",
        "schedule_version",
        "actions",
    }
    action = cast(list[object], _data(projected)["actions"])[0]
    assert set(cast(dict[str, object], action)) == {
        "action_id",
        "kind",
        "workflow_id",
        "task_id",
        "lease",
        "route",
        "model",
        "reasoning_effort",
    }
    assert "capability" not in str(projected)
    assert "endpoint" not in str(projected)


def test_status_v2_projects_optional_nulls_as_omissions_and_proof_summary() -> None:
    projected = project_relay_status(_status(with_proof=True))
    _assert_no_null(projected)
    data = _data(projected)

    assert set(data) == {
        "workflow_id",
        "run",
        "schedule_version",
        "tasks",
        "leases",
        "candidates",
        "integration_proofs",
        "outstanding_action_ids",
        "refill_directives",
        "queues",
    }
    run = cast(dict[str, object], data["run"])
    assert set(run) == {
        "run_id",
        "workflow_id",
        "plan_hash",
        "workspace_id",
        "input_snapshot_id",
        "base_commit",
        "integration_head",
        "integration_version",
        "capacity",
        "schedule_version",
    }
    candidate = cast(list[object], data["candidates"])[0]
    assert "pr_reference" not in cast(dict[str, object], candidate)
    proof = cast(list[object], data["integration_proofs"])[0]
    assert set(cast(dict[str, object], proof)) == {
        "proof_id",
        "expectation_hash",
        "task_id",
        "candidate_id",
        "integration_version",
        "predecessor_commit",
        "final_commit",
        "final_tree",
        "attestor_id",
        "attestor_version",
    }
    assert "receipt" not in str(projected).casefold()
    assert 'expectation": {' not in str(projected).casefold()


@pytest.mark.parametrize("projector", [project_relay_start, project_relay_status])
def test_projectors_reject_unknown_fields_and_wrong_schema(
    projector: Projector,
) -> None:
    value = _start_result() if projector is project_relay_start else _status()
    value["unknown"] = "nope"
    with pytest.raises(ResultContractError):
        projector(value)

    wrong = deepcopy(value)
    wrong["schema"] = "2718lab-devkit/relay-status-v1"
    with pytest.raises(ResultContractError):
        projector(wrong)


def test_status_rejects_proof_identity_predecessor_and_chain_contradictions() -> None:
    invalid = _status(with_proof=True)
    cast(dict[str, object], cast(list[object], invalid["integration_proofs"])[0])[
        "predecessor_commit"
    ] = "9" * 40
    with pytest.raises(ResultContractError):
        project_relay_status(invalid)

    invalid = _status(with_proof=True)
    cast(dict[str, object], cast(list[object], invalid["integration_proofs"])[0])[
        "candidate_id"
    ] = "other"
    with pytest.raises(ResultContractError):
        project_relay_status(invalid)


@pytest.mark.parametrize(
    "projector, value",
    [
        (
            project_relay_start,
            {
                "schema": "2718lab-devkit/relay-start-result-v1",
                "workflow_id": "w",
                "run_id": "r",
                "schedule_version": 0,
                "host_actions": [],
            },
        ),
        (project_relay_status, _status()),
    ],
)
def test_projectors_reject_bearers_paths_and_oversize_arrays(
    projector: Projector, value: dict[str, object]
) -> None:
    leaked = deepcopy(value)
    if projector is project_relay_start:
        leaked["host_actions"] = [{"capability": "Bearer secret"}]
    else:
        leaked["tasks"] = [_task() for _ in range(65)]
    with pytest.raises(ResultContractError):
        projector(leaked)


@pytest.mark.parametrize(
    "result",
    [
        {"workflow_id": "workflow-1", "schedule_version": 1, "task": _task()},
        {
            "workflow_id": "workflow-1",
            "schedule_version": 2,
            "task": _task(candidate_id="candidate-1"),
            "candidate": _candidate(),
        },
        {
            "workflow_id": "workflow-1",
            "schedule_version": 3,
            "task": _task(),
            "evidence": {
                "evidence_id": "evidence-1",
                "kind": "test",
                "selector": "tests/unit",
                "digest": "sha256:" + "7" * 64,
            },
        },
    ],
)
def test_handoff_projects_exact_lifecycle_variants_without_nulls(
    result: dict[str, object],
) -> None:
    projected = project_relay_handoff(result)
    _assert_no_null(projected)
    assert "capability" not in str(projected).casefold()
    assert "endpoint" not in str(projected).casefold()


def test_integrate_projects_proof_identity_without_receipt_body() -> None:
    result = {
        "workflow_id": "workflow-1",
        "schedule_version": 4,
        "task": _task(candidate_id="candidate-1"),
        "candidate": _candidate(integrated=True),
    }
    projected = project_relay_integrate(result)
    _assert_no_null(projected)
    candidate = cast(dict[str, object], _data(projected)["candidate"])
    assert candidate["integration_proof_id"] == "sha256:" + "3" * 64
    assert "receipt" not in str(projected).casefold()
    assert "proof_body" not in str(projected).casefold()


@pytest.mark.parametrize("projector", [project_relay_handoff, project_relay_integrate])
def test_mutation_projectors_reject_unknown_or_null_public_fields(
    projector: Projector,
) -> None:
    value = {"workflow_id": "workflow-1", "schedule_version": 1, "task": _task()}
    value["endpoint"] = "https://host-private"
    with pytest.raises(ResultContractError):
        projector(value)

    value = {"workflow_id": "workflow-1", "schedule_version": 1, "task": _task()}
    cast(dict[str, object], value["task"])["unexpected"] = None
    with pytest.raises(ResultContractError):
        projector(value)
