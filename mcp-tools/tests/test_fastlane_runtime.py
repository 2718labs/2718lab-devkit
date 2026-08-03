from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-tools"))

import server  # noqa: E402
from devkit_fastlane.scripts import team_efficiency  # noqa: E402

sys.path.insert(0, str(ROOT / "mcp-tools" / "devkit_fastlane" / "tests"))


def _sample_request() -> tuple[object, dict[str, object], object]:
    """Build one real contract request without importing its test class at collection."""

    from importlib import import_module

    tests = import_module("test_team_efficiency")
    helper = tests.TeamEfficiencyTests("runTest")
    helper.setUp()
    return helper, helper.fast_lane_request(team_efficiency), tests


def test_work_methodology_is_not_a_discoverable_skill() -> None:
    """Fast Lane contract lives in the MCP runtime, not prompt-discovered skills."""

    assert not (ROOT / "skills" / "work-methodology" / "SKILL.md").exists()
    runtime = ROOT / "mcp-tools" / "devkit_fastlane"
    assert (runtime / "__init__.py").is_file()
    assert (runtime / "scripts" / "team_efficiency.py").is_file()


def test_fastlane_tool_rejects_host_private_inputs() -> None:
    """Host attestations stay private; public MCP receives only an inert request."""

    helper, request, _ = _sample_request()
    try:
        request["host_status"] = {"workflow_id": "foreign"}
        result = server.fastlane_compile(
            request=request, reasoning_effort="ultra", enable=True
        )
    finally:
        helper.tearDown()
    assert result["ok"] is False
    assert result["error"]["code"] == "FASTLANE_REQUEST_INVALID"


def test_fastlane_tool_never_spawns_or_executes() -> None:
    """The MCP compiler emits descriptors; the host owns execution and refill."""

    helper, request, _ = _sample_request()
    try:
        result = server.fastlane_compile(
            request=request, reasoning_effort="ultra", enable=True
        )
    finally:
        helper.tearDown()
    assert result["ok"] is True
    data = result["data"]
    assert data["schema"] == "team-efficiency/fast-lane-plan-v1"
    assert "host_actions" not in data
    assert data["workflow_policy"]["dispatch_protocol"] == {
        "schema": "team-efficiency/fast-lane-dispatch-protocol-v1",
        "tool": "collaboration.spawn_agent",
        "model_source": "assignment.host_dispatch.model",
        "reasoning_effort_source": "assignment.host_dispatch.reasoning_effort",
        "inherit_current_session_model": False,
        "require_explicit_route": True,
        "missing_route_action": "reject",
    }
