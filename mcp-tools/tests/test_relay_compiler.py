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


def _request() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/relay-compile-request-v1",
        "workflow_id": "relay-contract",
        "input_snapshot_id": "sha256:" + "a" * 64,
        "capacity": 3,
        "tasks": [
            {
                "task_id": "atlas",
                "kind": "implementation",
                "title": "Wire Atlas",
                "dependencies": [],
                "write_scope": ["mcp-tools/devkit_atlas/"],
                "route": {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "max",
                },
                "required_evidence": ["pytest:atlas"],
            },
            {
                "task_id": "relay",
                "kind": "implementation",
                "title": "Build Relay",
                "dependencies": [],
                "write_scope": ["mcp-tools/devkit_relay/"],
                "route": {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "max",
                },
                "required_evidence": ["pytest:relay"],
            },
            {
                "task_id": "integration",
                "kind": "verification",
                "title": "Run integration",
                "dependencies": ["atlas", "relay"],
                "write_scope": [],
                "route": {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                },
                "required_evidence": ["pytest:e2e"],
            },
        ],
    }


def test_compile_is_deterministic_and_exposes_ready_and_prewarm_queues() -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"

    first = compiler.compile_plan(_request())
    reordered = deepcopy(_request())
    reordered["tasks"] = list(reversed(reordered["tasks"]))
    second = compiler.compile_plan(reordered)

    assert first == second
    assert first["schema"] == "2718lab-devkit/relay-plan-v1"
    assert first["plan_hash"].startswith("sha256:")
    assert [item["task_id"] for item in first["ready"]] == ["atlas", "relay"]
    assert [item["task_id"] for item in first["prepared_prewarms"]] == [
        "integration"
    ]
    assert first["capacity"] == 3


def test_compile_rejects_overlapping_ready_writer_scopes() -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"
    request = _request()
    request["tasks"][1]["write_scope"] = ["mcp-tools/devkit_atlas/service.py"]

    with pytest.raises(compiler.RelayPlanError, match="write_scope_conflict"):
        compiler.compile_plan(request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "do the task"),
        ("command", "pytest"),
        ("workspace", "D:/absolute/worktree"),
    ],
)
def test_compile_rejects_prompt_command_and_absolute_workspace_fields(
    field: str, value: str
) -> None:
    compiler = _compiler_module()
    assert compiler is not None, "devkit_relay.compiler must exist"
    request = _request()
    request["tasks"][0][field] = value

    with pytest.raises(compiler.RelayPlanError, match="unknown_task_fields"):
        compiler.compile_plan(request)
