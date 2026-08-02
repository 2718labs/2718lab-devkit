"""Deterministic Relay compiler contract."""

from __future__ import annotations

import importlib
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _compiler_module():
    try:
        return importlib.import_module("devkit_relay.compiler")
    except ModuleNotFoundError:
        return None


class RegistryResolver:
    """Read-only registry fake for the canonical compiler boundary."""

    def resolve(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        input_snapshot_id: str,
        atlas_packet_ids: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
            "input_snapshot_id": input_snapshot_id,
            "atlas_packet_ids": list(atlas_packet_ids),
            "current": True,
        }


def _route(reasoning_effort: str) -> dict[str, str]:
    route_class = f"terra_{reasoning_effort}"
    return {
        "route_class": route_class,
        "model": "gpt-5.6-terra",
        "reasoning_effort": reasoning_effort,
    }


def _task(
    task_id: str,
    *,
    kind: str,
    priority: int,
    dependencies: list[str],
    write_scope: list[dict[str, str]],
    reasoning_effort: str,
    prewarm_for_task_id: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "kind": kind,
        "title": f"{task_id} title",
        "objective": f"Complete the bounded {task_id} task.",
        "priority": priority,
        "dependencies": dependencies,
        "write_scope": write_scope,
        "route": _route(reasoning_effort),
        "constraints": [
            {
                "code": "bounded_execution",
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
            {"kind": "pytest", "selector": f"tests/test_{task_id}.py"}
        ],
        "prewarm_for_task_id": prewarm_for_task_id,
        "retry_policy": {"max_attempts": 1, "retryable_codes": []},
    }


def _request() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-compile-request-v1",
        "workflow_id": "relay-contract",
        "workspace_id": "sha256:" + "d" * 64,
        "input_snapshot_id": "sha256:" + "a" * 64,
        "base_commit": "b" * 40,
        "capacity": 3,
        "tasks": [
            _task(
                "atlas",
                kind="implementation",
                priority=90,
                dependencies=[],
                write_scope=[{"path": "mcp-tools/devkit_atlas", "kind": "tree"}],
                reasoning_effort="max",
            ),
            _task(
                "relay",
                kind="implementation",
                priority=80,
                dependencies=[],
                write_scope=[{"path": "mcp-tools/devkit_relay", "kind": "tree"}],
                reasoning_effort="max",
            ),
            _task(
                "integration",
                kind="verification",
                priority=70,
                dependencies=["atlas", "relay"],
                write_scope=[],
                reasoning_effort="high",
            ),
            _task(
                "prewarm-integration",
                kind="prewarm",
                priority=60,
                dependencies=[],
                write_scope=[],
                reasoning_effort="high",
                prewarm_for_task_id="integration",
            ),
        ],
    }


def test_compile_is_deterministic_and_exposes_ready_and_prewarm_queues() -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"

    first = compiler.compile_plan(_request(), registry_resolver=RegistryResolver())
    reordered = deepcopy(_request())
    reordered["tasks"] = list(reversed(reordered["tasks"]))
    second = compiler.compile_plan(reordered, registry_resolver=RegistryResolver())

    assert first == second
    assert first["schema"] == "2718lab-devkit/relay-plan-v1"
    assert first["plan_hash"].startswith("sha256:")
    assert first["base_commit"] == "b" * 40
    assert first["queues"]["ready"] == ["atlas", "relay"]
    assert first["queues"]["prepared_prewarms"] == ["prewarm-integration"]
    assert first["capacity"] == 3


def test_compile_projects_overlapping_ready_writer_scopes_as_a_conflict() -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"
    request = _request()
    request["tasks"][1]["write_scope"] = [
        {"path": "mcp-tools/devkit_atlas/service.py", "kind": "file"}
    ]

    plan = compiler.compile_plan(request, registry_resolver=RegistryResolver())

    assert plan["conflicts"] == [
        {
            "from_task_id": "atlas",
            "kind": "write_scope_conflict",
            "to_task_id": "relay",
        }
    ]
    assert plan["queues"]["ready"] == ["atlas"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "do the task"),
        ("command", "pytest"),
        ("workspace", "D:/absolute/worktree"),
        ("lease", {"epoch": 1}),
        ("reservation", {"scope": "mcp-tools/devkit_relay"}),
    ],
)
def test_compile_rejects_prompt_command_and_absolute_workspace_fields(
    field: str, value: object
) -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"
    request = _request()
    request["tasks"][0][field] = value

    with pytest.raises(compiler.RelayPlanError, match="unknown_task_fields"):
        compiler.compile_plan(request, registry_resolver=RegistryResolver())
