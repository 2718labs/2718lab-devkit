"""Relay service validation for Scheduler Topology V1."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.canonical import canonical_hash
from devkit_relay.service import RelayError, RelayService

_DIGEST = "sha256:" + "a" * 64


class CaptureStore:
    """Capture the service's validated plan without owning durable scheduling."""

    def __init__(self) -> None:
        self.plan: dict[str, object] | None = None

    def start_create(
        self, plan: dict[str, object], *, idempotency_key: str
    ) -> dict[str, object]:
        self.plan = plan
        return {"idempotency_key": idempotency_key}


def _task(
    task_id: str,
    *,
    kind: str = "implementation",
    stage: str = "a1_writer",
    write_scope: list[dict[str, str]] | None = None,
    prewarm_for_task_id: str | None = None,
    split_parent_task_id: str | None = None,
    split_depth: int = 0,
    split_verdict: str | None = None,
) -> dict[str, object]:
    route = {
        "route_class": "terra_max",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "max",
    }
    if kind == "prewarm":
        route = {
            "route_class": "luna_medium",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
        }
    return {
        "task_id": task_id,
        "kind": kind,
        "title": f"{task_id} title",
        "objective": f"Complete {task_id} safely.",
        "priority": 50,
        "dependencies": [],
        "write_scope": [] if write_scope is None else write_scope,
        "route": route,
        "constraints": [{"code": "bounded", "detail": "Remain bounded."}],
        "acceptance_criteria": [{"criterion_id": "green", "description": "Pass."}],
        "atlas_packet_ids": [_DIGEST],
        "required_evidence": [{"kind": "pytest", "selector": "tests/focused.py"}],
        "prewarm_for_task_id": prewarm_for_task_id,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
        "stage": stage,
        "design_for_task_id": None,
        "split_policy": None,
        "split_parent_task_id": split_parent_task_id,
        "split_depth": split_depth,
        "split_verdict": split_verdict,
    }


def _plan(*, groups: list[dict[str, object]], capacity: int = 2) -> dict[str, object]:
    tasks = sorted(
        [
            _task(
                "writer-alpha",
                write_scope=[{"path": "mcp-tools/alpha.py", "kind": "file"}],
            ),
            _task(
                "writer-beta",
                write_scope=[{"path": "mcp-tools/beta.py", "kind": "file"}],
            ),
            _task(
                "prewarm-alpha",
                kind="prewarm",
                stage="a3_prewarm",
                prewarm_for_task_id="writer-alpha",
            ),
        ],
        key=lambda task: str(task["task_id"]),
    )
    body: dict[str, object] = {
        "schema": "2718lab-devkit/relay-plan-v3",
        "workflow_id": "hierarchy-service",
        "workspace_binding": {
            "workspace_id": _DIGEST,
            "input_snapshot_id": _DIGEST,
            "atlas_packet_ids": [_DIGEST],
        },
        "project_binding": {
            "schema": "2718lab-devkit/project-binding-v1",
            "mode": "indexed",
        },
        "base_commit": "b" * 40,
        "capacity": capacity,
        "runtime_policy_id": "2718lab-devkit/relay-runtime-policy-v1",
        "tasks": tasks,
        "dependencies": [],
        "conflicts": [],
        "queues": {
            "writer_ready": ["writer-alpha", "writer-beta"],
            "design_ready": [],
            "prewarm_ready": ["prewarm-alpha"],
            "bootstrap_index": [],
            "review_integration": [],
            "terminal": [],
            "unsplittable": [],
        },
        "scheduler_topology": {
            "schema": "2718lab-devkit/scheduler-topology-v1",
            "max_writers_per_scheduler": 3,
            "max_parallel_writers": 9,
            "groups": groups,
        },
    }
    return {**body, "plan_hash": canonical_hash(body)}


def _group(
    scheduler_id: str,
    writer_task_ids: list[str],
    prewarm_task_ids: list[str],
) -> dict[str, object]:
    worktree_identity = (
        "sha256:" + ("a" if scheduler_id.endswith("alpha") else "b") * 64
    )
    return {
        "scheduler_id": scheduler_id,
        "coordinator_lease_id": f"lease-{scheduler_id}",
        "worktree_identity": worktree_identity,
        "writer_task_ids": writer_task_ids,
        "prewarm_task_ids": prewarm_task_ids,
    }


def test_v3_topology_preserves_group_isolated_writer_assignments() -> None:
    store = CaptureStore()
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    submitted = _plan(
        groups=[
            _group("scheduler-alpha", ["writer-alpha"], ["prewarm-alpha"]),
            _group("scheduler-beta", ["writer-beta"], []),
        ]
    )

    relay.start_create(submitted, idempotency_key="hierarchy-create")

    assert store.plan is not None
    assert store.plan == submitted
    persisted_topology = store.plan["scheduler_topology"]
    assert persisted_topology == submitted["scheduler_topology"]


def test_v3_rejects_prewarm_bound_to_another_scheduler_group() -> None:
    relay = RelayService(CaptureStore(), capability_secret=b"hierarchy-test-secret")
    submitted = _plan(
        groups=[
            _group("scheduler-alpha", ["writer-alpha"], []),
            _group("scheduler-beta", ["writer-beta"], ["prewarm-alpha"]),
        ]
    )

    with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
        relay.start_create(submitted, idempotency_key="cross-group-prewarm")


def test_v3_accepts_sha256_opaque_group_identities_without_rewriting_the_plan() -> None:
    store = CaptureStore()
    relay = RelayService(store, capability_secret=b"hierarchy-test-secret")
    alpha = "sha256:" + "1" * 64
    beta = "sha256:" + "2" * 64
    submitted = _plan(
        groups=[
            {
                "scheduler_id": alpha,
                "coordinator_lease_id": "sha256:" + "3" * 64,
                "worktree_identity": "sha256:" + "4" * 64,
                "writer_task_ids": ["writer-alpha"],
                "prewarm_task_ids": ["prewarm-alpha"],
            },
            {
                "scheduler_id": beta,
                "coordinator_lease_id": "sha256:" + "5" * 64,
                "worktree_identity": "sha256:" + "6" * 64,
                "writer_task_ids": ["writer-beta"],
                "prewarm_task_ids": [],
            },
        ]
    )

    relay.start_create(submitted, idempotency_key="opaque-topology")

    assert store.plan == submitted


def test_v3_rejects_capacity_above_the_nine_writer_relay_limit() -> None:
    relay = RelayService(CaptureStore(), capability_secret=b"hierarchy-test-secret")
    submitted = _plan(
        capacity=10,
        groups=[
            _group("scheduler-alpha", ["writer-alpha"], ["prewarm-alpha"]),
            _group("scheduler-beta", ["writer-beta"], []),
        ],
    )

    with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
        relay.start_create(submitted, idempotency_key="capacity-over-nine")


def test_v3_topology_rejects_more_than_three_writers_per_scheduler() -> None:
    relay = RelayService(CaptureStore(), capability_secret=b"hierarchy-test-secret")
    submitted = _plan(
        groups=[
            _group(
                "scheduler-alpha",
                ["writer-alpha", "writer-beta", "writer-gamma", "writer-delta"],
                ["prewarm-alpha"],
            )
        ]
    )

    with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
        relay.start_create(submitted, idempotency_key="too-many-writers")


def test_v3_topology_rejects_cross_group_overlapping_writers_without_child_split() -> (
    None
):
    relay = RelayService(CaptureStore(), capability_secret=b"hierarchy-test-secret")
    submitted = _plan(
        groups=[
            _group("scheduler-alpha", ["writer-alpha"], ["prewarm-alpha"]),
            _group("scheduler-beta", ["writer-beta"], []),
        ]
    )
    tasks = submitted["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["write_scope"] = [{"path": "mcp-tools", "kind": "tree"}]
    submitted["conflicts"] = [
        {
            "from_task_id": "writer-alpha",
            "kind": "write_scope_conflict",
            "to_task_id": "writer-beta",
        }
    ]
    body = {key: value for key, value in submitted.items() if key != "plan_hash"}
    submitted["plan_hash"] = canonical_hash(body)

    with pytest.raises(RelayError, match="RELAY_PLAN_INVALID"):
        relay.start_create(submitted, idempotency_key="cross-group-overlap")
