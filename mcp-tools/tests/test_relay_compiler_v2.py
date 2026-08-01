"""Relay compiler's pure, deterministic planning contract."""

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
    write_scope: list[dict[str, str]] | None = None,
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
        "constraints": [
            {
                "code": "no_unbounded_side_effects",
                "detail": "Do not perform unbounded side effects.",
            }
        ],
        "acceptance_criteria": [
            {
                "criterion_id": f"{task_id}-acceptance",
                "description": f"Complete the bounded {task_id} contract.",
            }
        ],
        "atlas_packet_ids": ["sha256:" + "c" * 64],
        "required_evidence": [
            {
                "kind": "pytest",
                "selector": f"mcp-tools/tests/test_{task_id}.py",
            }
        ],
        "route": _route(),
        "prewarm_for_task_id": prewarm_for_task_id,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
    }


def _request() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-compile-request-v1",
        "workflow_id": "relay-v2-contract",
        "workspace_id": "sha256:" + "d" * 64,
        "input_snapshot_id": "sha256:" + "b" * 64,
        "base_commit": "a" * 40,
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
                write_scope=[{"path": "mcp-tools/devkit_relay", "kind": "tree"}],
            ),
            _task(
                "writer-child",
                priority=10,
                write_scope=[
                    {"path": "mcp-tools/devkit_relay/compiler.py", "kind": "file"}
                ],
            ),
            _task(
                "writer-safe",
                priority=50,
                write_scope=[{"path": "mcp-tools/devkit_atlas", "kind": "tree"}],
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
        "base_commit",
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
        ("relay-v2-contract", "sha256:" + "d" * 64, "sha256:" + "b" * 64)
    ]
    assert resolver.writes == 0
    assert plan["workspace_binding"] == {
        "workspace_id": "sha256:" + "d" * 64,
        "input_snapshot_id": "sha256:" + "b" * 64,
        "atlas_packet_ids": ["sha256:" + "c" * 64],
    }
    assert plan["base_commit"] == "a" * 40
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
    canonical = _request()
    canonical_tasks = canonical["tasks"]
    assert isinstance(canonical_tasks, list)
    writer = next(task for task in canonical_tasks if task["task_id"] == "writer-root")
    writer["constraints"].append(
        {"code": "bounded_output", "detail": "Keep output bounded."}
    )
    writer["acceptance_criteria"].append(
        {
            "criterion_id": "writer-root-evidence",
            "description": "Produce deterministic evidence.",
        }
    )
    writer["required_evidence"].append(
        {"kind": "ruff", "selector": "mcp-tools/devkit_relay/compiler.py"}
    )
    writer["retry_policy"]["retryable_codes"] = ["stale_lease", "timeout"]

    first = compile_plan(canonical, registry_resolver=resolver)
    reordered = deepcopy(canonical)
    tasks = reordered["tasks"]
    assert isinstance(tasks, list)
    tasks.reverse()
    for task in tasks:
        assert isinstance(task, dict)
        task["dependencies"].reverse()
        task["write_scope"].reverse()
        task["constraints"].reverse()
        task["acceptance_criteria"].reverse()
        task["required_evidence"].reverse()
        task["atlas_packet_ids"].reverse()
        task["retry_policy"]["retryable_codes"].reverse()

    second = compile_plan(reordered, registry_resolver=resolver)

    assert first == second


def test_v3_rejects_unregistered_binding_as_domain_error() -> None:
    class MissingRegistry:
        def resolve(self, **_: object) -> None:
            return None

    with pytest.raises(RelayPlanError, match="registry_binding_unavailable"):
        compile_plan(_request(), registry_resolver=MissingRegistry())


def test_v3_rejects_nonopaque_workspace_identifier_before_registry_lookup() -> None:
    request = _request()
    request["workspace_id"] = "workspace-main"
    resolver = RegistryResolver()

    with pytest.raises(RelayPlanError, match="invalid_workspace_id"):
        compile_plan(request, registry_resolver=resolver)

    assert resolver.calls == []


def test_v3_rejects_malformed_registry_data_as_corrupt() -> None:
    class MalformedRegistry:
        def resolve(self, **_: object) -> dict[str, object]:
            return {
                "workflow_id": "relay-v2-contract",
                "workspace_id": "sha256:" + "d" * 64,
                "input_snapshot_id": "sha256:" + "b" * 64,
                "atlas_packet_ids": ["not-a-packet-id"],
                "current": True,
            }

    with pytest.raises(RelayPlanError, match="registry_binding_corrupt"):
        compile_plan(_request(), registry_resolver=MalformedRegistry())


def test_v3_rejects_registry_failures_as_unavailable() -> None:
    class FailingRegistry:
        def resolve(self, **_: object) -> None:
            raise RuntimeError("registry offline")

    with pytest.raises(RelayPlanError, match="registry_binding_unavailable"):
        compile_plan(_request(), registry_resolver=FailingRegistry())


def test_v3_rejects_registry_resolver_lookup_failure_as_unavailable() -> None:
    class FailingRegistry:
        @property
        def resolve(self) -> object:
            raise RuntimeError("registry resolver unavailable")

    with pytest.raises(RelayPlanError, match="registry_binding_unavailable"):
        compile_plan(_request(), registry_resolver=FailingRegistry())


@pytest.mark.parametrize(
    "code",
    [
        "registry_binding_unavailable",
        "registry_binding_stale",
        "registry_binding_corrupt",
    ],
)
def test_v3_propagates_stable_registry_domain_errors(code: str) -> None:
    class DomainRegistry:
        def resolve(self, **_: object) -> None:
            raise RelayPlanError(code)

    with pytest.raises(RelayPlanError) as caught:
        compile_plan(_request(), registry_resolver=DomainRegistry())

    assert caught.value.code == code


def test_v3_rejects_noncurrent_registry_binding_as_stale() -> None:
    class StaleRegistry(RegistryResolver):
        def resolve(self, **kwargs: object) -> dict[str, object]:
            binding = super().resolve(**kwargs)
            binding["current"] = False
            return binding

    with pytest.raises(RelayPlanError, match="registry_binding_stale"):
        compile_plan(_request(), registry_resolver=StaleRegistry())


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
        (
            "writer-safe",
            "write_scope",
            [{"path": "D:/outside", "kind": "tree"}],
            "invalid_write_scope",
        ),
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
    tasks[0]["write_scope"] = [{"path": "mcp-tools/devkit_atlas", "kind": "tree"}]

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


@pytest.mark.parametrize(
    ("route_class", "model", "reasoning_effort"),
    [
        ("terra_high", "gpt-5.6-terra", "high"),
        ("terra_max", "gpt-5.6-terra", "max"),
        ("sol_high", "gpt-5.6-sol", "high"),
        ("sol_ultra", "gpt-5.6-sol", "ultra"),
    ],
)
def test_v3_accepts_closed_routes(
    route_class: str, model: str, reasoning_effort: str
) -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    route = tasks[1]["route"]
    assert isinstance(route, dict)
    route.update(
        {
            "route_class": route_class,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
    )

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    writer = next(task for task in plan["tasks"] if task["task_id"] == "writer-root")
    assert writer["route"] == route


@pytest.mark.parametrize("base_commit", ["a" * 39, "a" * 41, "g" * 40])
def test_v3_rejects_invalid_base_commit(base_commit: str) -> None:
    request = _request()
    request["base_commit"] = base_commit

    with pytest.raises(RelayPlanError, match="invalid_base_commit"):
        compile_plan(request, registry_resolver=RegistryResolver())


def test_v3_accepts_sha256_base_commit() -> None:
    request = _request()
    request["base_commit"] = "d" * 64

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["base_commit"] == "d" * 64


@pytest.mark.parametrize(
    ("field", "extra", "code"),
    [
        ("write_scope", {"owner": "worker"}, "invalid_write_scope"),
        ("constraints", {"owner": "worker"}, "invalid_constraints"),
        (
            "acceptance_criteria",
            {"owner": "worker"},
            "invalid_acceptance_criteria",
        ),
        (
            "required_evidence",
            {"owner": "worker"},
            "invalid_required_evidence",
        ),
    ],
)
def test_v3_rejects_unknown_fields_in_rich_contract_objects(
    field: str, extra: dict[str, str], code: str
) -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    entry = tasks[1][field][0]
    assert isinstance(entry, dict)
    entry.update(extra)

    with pytest.raises(RelayPlanError, match=code):
        compile_plan(request, registry_resolver=RegistryResolver())


def test_v3_file_scopes_with_shared_text_prefix_do_not_overlap() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["write_scope"] = [
        {"path": "mcp-tools/devkit_relay/service.py", "kind": "file"}
    ]
    tasks[2]["write_scope"] = [
        {"path": "mcp-tools/devkit_relay/service.py.bak", "kind": "file"}
    ]

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["conflicts"] == []


def test_v3_file_scope_does_not_claim_descendant_paths() -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["write_scope"] = [
        {"path": "mcp-tools/devkit_relay/service.py", "kind": "file"}
    ]
    tasks[2]["write_scope"] = [
        {"path": "mcp-tools/devkit_relay/service.py/generated", "kind": "file"}
    ]

    plan = compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["conflicts"] == []


@pytest.mark.parametrize(
    ("capacity", "accepted"),
    [
        (1, True),
        (2, True),
        (3, True),
        (4, False),
        (8, False),
        (0, False),
        (-1, False),
        (True, False),
        (1.5, False),
        ("3", False),
        (None, False),
    ],
)
def test_v3_enforces_host_child_capacity_boundary(
    capacity: object, accepted: bool
) -> None:
    request = _request()
    request["capacity"] = capacity

    if accepted:
        plan = compile_plan(request, registry_resolver=RegistryResolver())

        assert plan["capacity"] == capacity
        return

    with pytest.raises(RelayPlanError) as caught:
        compile_plan(request, registry_resolver=RegistryResolver())

    assert caught.value.code == "invalid_capacity"


@pytest.mark.parametrize("kind", ["blob", None, ["tree"]])
def test_v3_rejects_invalid_scope_kind_with_domain_error(kind: object) -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["write_scope"] = [{"path": "mcp-tools/devkit_relay", "kind": kind}]

    with pytest.raises(RelayPlanError, match="invalid_write_scope"):
        compile_plan(request, registry_resolver=RegistryResolver())


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("write_scope", "invalid_write_scope"),
        ("constraints", "invalid_constraints"),
        ("acceptance_criteria", "invalid_acceptance_criteria"),
        ("required_evidence", "invalid_required_evidence"),
    ],
)
def test_v3_rejects_unbounded_rich_contract_lists(field: str, code: str) -> None:
    request = _request()
    tasks = request["tasks"]
    assert isinstance(tasks, list)
    task = tasks[1]
    if field == "write_scope":
        value = [
            {"path": f"mcp-tools/devkit_relay/file-{index}.py", "kind": "file"}
            for index in range(33)
        ]
    elif field == "constraints":
        value = [
            {"code": f"constraint-{index}", "detail": f"Constraint {index}."}
            for index in range(33)
        ]
    elif field == "acceptance_criteria":
        value = [
            {
                "criterion_id": f"criterion-{index}",
                "description": f"Criterion {index}.",
            }
            for index in range(33)
        ]
    else:
        value = [
            {"kind": "pytest", "selector": f"tests/test_case_{index}.py"}
            for index in range(33)
        ]
    task[field] = value

    with pytest.raises(RelayPlanError, match=code):
        compile_plan(request, registry_resolver=RegistryResolver())
