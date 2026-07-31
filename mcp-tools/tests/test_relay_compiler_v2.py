"""Relay compiler v2's pure, deterministic planning contract."""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_relay.compiler import RelayPlanError, compile_plan


class RegistryResolver:
    """Small read-only registry fake used to prove compiler binding behavior."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.writes = 0

    def resolve(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        input_snapshot_id: str,
        atlas_packet_ids: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append((workflow_id, workspace_id, input_snapshot_id))
        return {
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
            "input_snapshot_id": input_snapshot_id,
            "atlas_packet_ids": list(atlas_packet_ids),
            "current": True,
        }


def _route() -> dict[str, str]:
    return {
        "route_class": "terra_max",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "max",
    }


def _task(
    task_id: str,
    *,
    kind: str = "implementation",
    priority: int = 50,
    dependencies: list[str] | None = None,
    write_scope: list[str] | None = None,
    prewarm_for_task_id: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "kind": kind,
        "title": f"{task_id} title",
        "objective": f"Complete the bounded {task_id} task.",
        "priority": priority,
        "dependencies": [] if dependencies is None else dependencies,
        "write_scope": [] if write_scope is None else write_scope,
        "constraints": ["no_unbounded_side_effects"],
        "acceptance_criteria": [f"{task_id} acceptance"],
        "atlas_packet_ids": ["sha256:" + "c" * 64],
        "required_evidence": [f"pytest:mcp-tools/tests/test_{task_id}.py"],
        "route": _route(),
        "prewarm_for_task_id": prewarm_for_task_id,
        "retry_policy": {"max_attempts": 1, "retryable_reasons": []},
    }


def _request() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-compile-request-v1",
        "workflow_id": "relay-v2-contract",
        "workspace_id": "workspace-main",
        "input_snapshot_id": "sha256:" + "b" * 64,
        "capacity": 3,
        "tasks": [
            _task(
                "prewarm-atlas",
                kind="prewarm",
                priority=75,
                prewarm_for_task_id="writer-root",
            ),
            _task(
                "writer-root",
                priority=100,
                write_scope=["mcp-tools/devkit_relay/"],
            ),
            _task(
                "writer-child",
                priority=10,
                write_scope=["mcp-tools/devkit_relay/compiler.py"],
            ),
            _task(
                "writer-safe",
                priority=50,
                write_scope=["mcp-tools/code_atlas/"],
            ),
            _task(
                "verify",
                kind="verification",
                dependencies=["writer-root"],
            ),
        ],
    }


def test_v2_compiles_registered_workspace_into_five_deterministic_queues() -> None:
    resolver = RegistryResolver()

    plan = compile_plan(_request(), registry_resolver=resolver)

    assert set(plan) == {
        "schema",
        "workflow_id",
        "workspace_binding",
        "capacity",
        "runtime_policy_id",
        "tasks",
        "dependencies",
        "conflicts",
        "queues",
        "plan_hash",
    }
    assert plan["schema"] == "2718lab-devkit/relay-plan-v1"
    assert resolver.calls == [
        ("relay-v2-contract", "workspace-main", "sha256:" + "b" * 64)
    ]
    assert resolver.writes == 0
    assert plan["workspace_binding"] == {
        "workspace_id": "workspace-main",
        "input_snapshot_id": "sha256:" + "b" * 64,
        "atlas_packet_ids": ["sha256:" + "c" * 64],
    }
    assert plan["runtime_policy_id"] == "2718lab-devkit/relay-runtime-policy-v1"
    assert set(plan["queues"]) == {
        "prepared_prewarms",
        "ready",
        "running_slots",
        "review_integration",
        "terminal",
    }
    assert plan["queues"]["prepared_prewarms"] == ["prewarm-atlas"]
    assert plan["queues"]["ready"] == [
        "writer-root",
        "writer-safe",
    ]
    assert plan["queues"]["running_slots"] == []
    assert plan["queues"]["review_integration"] == []
    assert plan["queues"]["terminal"] == []
    assert plan["conflicts"] == [
        {
            "from_task_id": "writer-root",
            "kind": "write_scope_conflict",
            "to_task_id": "writer-child",
        }
    ]
    assert plan["dependencies"] == [
        {
            "from_task_id": "verify",
            "kind": "depends_on",
            "to_task_id": "writer-root",
        }
    ]
    assert "writer-child" not in plan["queues"]["prepared_prewarms"]
    assert "writer-child" not in plan["queues"]["ready"]
    prewarm = next(task for task in plan["tasks"] if task["task_id"] == "prewarm-atlas")
    assert prewarm["kind"] == "prewarm"
    assert prewarm["write_scope"] == []
    assert "worktree" not in prewarm
    assert "lease" not in prewarm
    assert plan["plan_hash"].startswith("sha256:")


def test_v2_is_canonical_under_allowed_task_and_list_reordering() -> None:
    resolver = RegistryResolver()
    first = compile_plan(_request(), registry_resolver=resolver)
    reordered = deepcopy(_request())
    tasks = reordered["tasks"]
    assert isinstance(tasks, list)
    tasks.reverse()
    for task in tasks:
        assert isinstance(task, dict)
        task["dependencies"].reverse()
        task["write_scope"].reverse()
        task["acceptance_criteria"].reverse()
        task["required_evidence"].reverse()
        task["atlas_packet_ids"].reverse()

    second = compile_plan(reordered, registry_resolver=resolver)

    assert first == second


def test_v2_rejects_unregistered_or_noncurrent_binding_as_domain_result() -> None:
    class MissingRegistry:
        def resolve(self, **_: object) -> None:
            return None

    result = compile_plan(_request(), registry_resolver=MissingRegistry())

    assert result == {
        "schema": "2718lab-devkit/relay-compile-result-v1",
        "status": "rejected",
        "reasons": ["registry_binding_unavailable"],
    }


def test_v2_turns_malformed_registry_data_into_a_domain_rejection() -> None:
    class MalformedRegistry:
        def resolve(self, **_: object) -> dict[str, object]:
            return {
                "workflow_id": "relay-v2-contract",
                "workspace_id": "workspace-main",
                "input_snapshot_id": "sha256:" + "b" * 64,
                "atlas_packet_ids": ["not-a-packet-id"],
                "current": True,
            }

    assert compile_plan(_request(), registry_resolver=MalformedRegistry()) == {
        "schema": "2718lab-devkit/relay-compile-result-v1",
        "status": "rejected",
        "reasons": ["registry_binding_unavailable"],
    }


def test_v2_turns_registry_failures_into_a_domain_rejection() -> None:
    class FailingRegistry:
        def resolve(self, **_: object) -> None:
            raise RuntimeError("registry offline")

    assert compile_plan(_request(), registry_resolver=FailingRegistry()) == {
        "schema": "2718lab-devkit/relay-compile-result-v1",
        "status": "rejected",
        "reasons": ["registry_binding_unavailable"],
    }


def test_v2_treats_ancestor_writer_overlap_as_safe_ordering() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    tasks[2]["dependencies"] = ["writer-root"]

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["conflicts"] == []
    assert plan["queues"]["ready"] == ["writer-root", "writer-safe"]


@pytest.mark.parametrize(
    ("task_id", "field", "value", "code"),
    [
        ("writer-safe", "write_scope", ["D:/outside"], "invalid_write_scope"),
        ("writer-safe", "priority", float("nan"), "invalid_priority"),
        ("verify", "dependencies", ["prewarm-atlas"], "prewarm_cannot_be_dependency"),
    ],
)
def test_v2_rejects_unsafe_or_nonfinite_task_values(
    task_id: str, field: str, value: object, code: str
) -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    task = next(item for item in tasks if item["task_id"] == task_id)
    task[field] = value

    with pytest.raises(RelayPlanError, match=code):
        compile_plan(request, registry_resolver=RegistryResolver())


def test_v2_rejects_cycles_and_unknown_fields() -> None:
    cyclic = _request()
    tasks = cyclic["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["dependencies"] = ["writer-safe"]
    tasks[3]["dependencies"] = ["writer-root"]

    with pytest.raises(RelayPlanError, match="dependency_cycle"):
        compile_plan(cyclic, registry_resolver=RegistryResolver())

    invalid = _request()
    invalid["workspace_path"] = "D:/not-a-workspace-id"
    with pytest.raises(RelayPlanError, match="unknown_request_fields"):
        compile_plan(invalid, registry_resolver=RegistryResolver())


def test_v2_rejects_prewarm_writer_scope_and_unclosed_route_triple() -> None:
    invalid_prewarm = _request()
    tasks = invalid_prewarm["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["write_scope"] = ["mcp-tools/code_atlas/"]

    with pytest.raises(RelayPlanError, match="prewarm_has_write_scope"):
        compile_plan(invalid_prewarm, registry_resolver=RegistryResolver())

    invalid_route = _request()
    tasks = invalid_route["tasks"]
    assert isinstance(tasks, list)
    route = tasks[1]["route"]
    assert isinstance(route, dict)
    route["reasoning_effort"] = "ultra"

    with pytest.raises(RelayPlanError, match="invalid_route"):
        compile_plan(invalid_route, registry_resolver=RegistryResolver())
