"""Relay V3 scheduler-topology compiler contract."""

from __future__ import annotations

from copy import deepcopy

import pytest

from devkit_relay.compiler import RelayPlanError, compile_plan


class RegistryResolver:
    def resolve(self, **kwargs: object) -> dict[str, object]:
        return {
            "workflow_id": kwargs["workflow_id"],
            "workspace_id": kwargs["workspace_id"],
            "input_snapshot_id": kwargs["input_snapshot_id"],
            "atlas_packet_ids": list(kwargs["atlas_packet_ids"]),
            "current": True,
        }


def _task(task_id: str, *, scope: str | None = None) -> dict[str, object]:
    implementation = scope is not None
    return {
        "task_id": task_id,
        "kind": "implementation" if implementation else "prewarm",
        "stage": "a1_writer" if implementation else "a3_prewarm",
        "title": task_id,
        "objective": task_id,
        "priority": 50,
        "dependencies": [],
        "write_scope": [{"path": scope, "kind": "file"}] if scope else [],
        "route": {
            "route_class": "terra_high" if implementation else "luna_medium",
            "model": "gpt-5.6-terra" if implementation else "gpt-5.6-luna",
            "reasoning_effort": "high" if implementation else "medium",
        },
        "constraints": [],
        "acceptance_criteria": [],
        "atlas_packet_ids": [],
        "required_evidence": [],
        "design_for_task_id": None,
        "prewarm_for_task_id": "writer-a" if not implementation else None,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
        "split_policy": None,
        "split_parent_task_id": None,
        "split_depth": 0,
        "split_verdict": None,
    }


def _topology(*, writer_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/scheduler-topology-v1",
        "max_writers_per_scheduler": 3,
        "max_parallel_writers": 9,
        "groups": [
            {
                "scheduler_id": "sha256:" + "1" * 64,
                "coordinator_lease_id": "sha256:" + "2" * 64,
                "worktree_identity": "sha256:" + "3" * 64,
                "writer_task_ids": writer_ids or ["writer-a"],
                "prewarm_task_ids": ["prewarm-a"],
            }
        ],
    }


def _request() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-compile-request-v3",
        "workflow_id": "hierarchy-v3",
        "workspace_id": "sha256:" + "a" * 64,
        "input_snapshot_id": "sha256:" + "b" * 64,
        "base_commit": "c" * 40,
        "capacity": 3,
        "project_binding": {
            "schema": "2718lab-devkit/project-binding-v1", "mode": "indexed"
        },
        "scheduler_topology": _topology(),
        "tasks": [_task("writer-a", scope="src/a.py"), _task("prewarm-a")],
    }


def test_v3_compiles_exact_opaque_topology_and_hashes_its_identities() -> None:
    request = _request()

    first = compile_plan(request, registry_resolver=RegistryResolver())
    reordered = deepcopy(request)
    topology = reordered["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    group = groups[0]
    assert isinstance(group, dict)
    group["coordinator_lease_id"] = "sha256:" + "4" * 64
    second = compile_plan(reordered, registry_resolver=RegistryResolver())

    assert first["schema"] == "2718lab-devkit/relay-plan-v3"
    assert first["scheduler_topology"] == _topology()
    assert first["plan_hash"] != second["plan_hash"]


@pytest.mark.parametrize("capacity", [1, 9])
def test_v3_accepts_the_full_one_to_nine_writer_capacity_range(
    capacity: int,
) -> None:
    request = _request()
    request["capacity"] = capacity

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["capacity"] == capacity


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda topology: topology.__setitem__("max_parallel_writers", 8),
            "invalid_scheduler_topology",
        ),
        (
            lambda topology: topology["groups"][0].__setitem__(
                "scheduler_id", "G:/unsafe/scheduler"
            ),
            "invalid_scheduler_topology",
        ),
        (
            lambda topology: topology["groups"][0].__setitem__(
                "writer_task_ids", ["writer-a"] * 4
            ),
            "invalid_scheduler_topology",
        ),
    ],
)
def test_v3_rejects_noncontract_topology(mutate: object, code: str) -> None:
    request = _request()
    topology = request["scheduler_topology"]
    assert isinstance(topology, dict)
    assert callable(mutate)
    mutate(topology)

    with pytest.raises(RelayPlanError, match=code):
        compile_plan(request, registry_resolver=RegistryResolver())


def test_v3_rejects_a_declared_split_that_exceeds_its_scheduler_writer_limit() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    writer = tasks[0]
    assert isinstance(writer, dict)
    writer["write_scope"] = [{"path": "src", "kind": "tree"}]
    writer["split_policy"] = {
        "mode": "declared_children",
        "max_depth": 1,
        "child_scopes": [
            [{"path": "src/alpha.py", "kind": "file"}],
            [{"path": "src/beta.py", "kind": "file"}],
            [{"path": "src/delta.py", "kind": "file"}],
            [{"path": "src/eta.py", "kind": "file"}],
        ],
    }
    tasks.pop()
    tasks.append(_task("writer-b", scope="src/gamma.py"))
    topology = request["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["prewarm_task_ids"] = []
    groups.append(
        {
            "scheduler_id": "sha256:" + "5" * 64,
            "coordinator_lease_id": "sha256:" + "6" * 64,
            "worktree_identity": "sha256:" + "7" * 64,
            "writer_task_ids": ["writer-b"],
            "prewarm_task_ids": [],
        }
    )

    with pytest.raises(RelayPlanError, match="invalid_scheduler_topology"):
        compile_plan(request, registry_resolver=RegistryResolver())


def test_v3_rebinds_a_cross_group_declared_split_to_its_original_scheduler() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    writer = tasks[0]
    assert isinstance(writer, dict)
    writer["write_scope"] = [{"path": "src", "kind": "tree"}]
    writer["split_policy"] = {
        "mode": "declared_children",
        "max_depth": 1,
        "child_scopes": [
            [{"path": "src/alpha.py", "kind": "file"}],
            [{"path": "src/beta.py", "kind": "file"}],
        ],
    }
    tasks.pop()
    tasks.append(_task("writer-b", scope="src/gamma.py"))
    topology = request["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["prewarm_task_ids"] = []
    groups.append(
        {
            "scheduler_id": "sha256:" + "5" * 64,
            "coordinator_lease_id": "sha256:" + "6" * 64,
            "worktree_identity": "sha256:" + "7" * 64,
            "writer_task_ids": ["writer-b"],
            "prewarm_task_ids": [],
        }
    )

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["conflicts"] == []
    first_group = plan["scheduler_topology"]["groups"][0]
    assert first_group["scheduler_id"] == "sha256:" + "1" * 64
    assert len(first_group["writer_task_ids"]) == 2


def test_v3_unsplittable_writers_are_removed_from_groups_and_ready_dispatch() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    writer = tasks[0]
    assert isinstance(writer, dict)
    writer["write_scope"] = [{"path": "src", "kind": "tree"}]
    tasks.append(_task("writer-b", scope="src/child.py"))
    topology = request["scheduler_topology"]
    assert isinstance(topology, dict)
    groups = topology["groups"]
    assert isinstance(groups, list)
    groups[0]["writer_task_ids"] = ["writer-a", "writer-b"]

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["queues"]["writer_ready"] == []
    assert plan["queues"]["unsplittable"] == ["writer-a", "writer-b"]
    assert plan["scheduler_topology"]["groups"][0]["writer_task_ids"] == []
