"""Locked public contract tests for the production 16-tool MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.tool_result import RESULT_SCHEMA, TOOL_ANNOTATIONS

EXPECTED_TOOL_NAMES = frozenset(
    {
        "project_index_register",
        "project_index_sync",
        "project_index_status",
        "project_index_query",
        "worktree_checkpoint_create",
        "worktree_checkpoint_status",
        "worktree_checkpoint_restore",
        "atlas_query",
        "atlas_prepare",
        "atlas_render",
        "atlas_accept",
        "relay_compile",
        "relay_start",
        "relay_status",
        "relay_handoff",
        "relay_integrate",
    }
)

EXPECTED_PARAMETERS = {
    "project_index_register": ("workspace_root",),
    "project_index_sync": ("workspace_id", "include_paths", "task_lease", "bind_as"),
    "project_index_status": ("workspace_id", "snapshot_id", "required_paths"),
    "project_index_query": (
        "workspace_id",
        "snapshot_id",
        "query",
        "mode",
        "node_kinds",
        "relations",
        "max_nodes",
        "max_depth",
        "source_lines",
        "byte_budget",
        "allow_miss_escape",
        "task_lease",
    ),
    "worktree_checkpoint_create": ("workspace_id", "task_lease", "snapshot_id"),
    "worktree_checkpoint_status": ("workspace_id", "checkpoint_id"),
    "worktree_checkpoint_restore": (
        "workspace_id",
        "task_lease",
        "checkpoint_id",
        "expected_current_snapshot_id",
    ),
    "atlas_query": (
        "root_node_ids",
        "node_kinds",
        "relations",
        "intent_id",
        "max_nodes",
        "max_edges",
        "max_depth",
        "byte_budget",
    ),
    "atlas_prepare": (
        "workspace_id",
        "snapshot_id",
        "intent_id",
        "language",
        "framework",
        "target_paths",
        "target_symbols",
        "max_candidates",
        "byte_budget",
    ),
    "atlas_render": ("workspace_id", "snapshot_id", "packet_id", "bindings"),
    "atlas_accept": ("workflow_id", "code_task_id", "acceptance_id", "ingestion_key"),
    "relay_compile": ("request",),
    "relay_start": ("request",),
    "relay_status": ("workflow_id",),
    "relay_handoff": ("request",),
    "relay_integrate": ("request",),
}


def _tools() -> dict[str, object]:
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def _properties(tool: object) -> dict[str, object]:
    return dict(getattr(tool, "inputSchema")["properties"])


def test_public_server_exposes_exact_locked_sixteen_tool_surface() -> None:
    tools = _tools()

    assert server.mcp.name == "2718lab-devkit"
    assert frozenset(tools) == EXPECTED_TOOL_NAMES
    assert asyncio.run(server.mcp.list_prompts()) == []
    assert asyncio.run(server.mcp.list_resources()) == []


def test_all_tool_annotations_match_the_locked_table() -> None:
    tools = _tools()

    assert frozenset(TOOL_ANNOTATIONS) == EXPECTED_TOOL_NAMES
    for name, (
        read_only,
        destructive,
        idempotent,
        open_world,
    ) in TOOL_ANNOTATIONS.items():
        annotations = getattr(tools[name], "annotations")
        assert annotations.readOnlyHint is read_only, name
        assert annotations.destructiveHint is destructive, name
        assert annotations.idempotentHint is idempotent, name
        assert annotations.openWorldHint is open_world, name


def test_tool_signatures_and_top_level_input_schemas_are_exact() -> None:
    tools = _tools()

    for name, expected in EXPECTED_PARAMETERS.items():
        function = getattr(server, name)
        assert tuple(inspect.signature(function).parameters) == expected
        assert tuple(_properties(tools[name])) == expected

    for name, expected_required in {
        "project_index_register": {"workspace_root"},
        "project_index_sync": {"workspace_id"},
        "project_index_status": {"workspace_id"},
        "project_index_query": {"workspace_id", "snapshot_id", "query"},
        "worktree_checkpoint_create": {"workspace_id", "task_lease", "snapshot_id"},
        "worktree_checkpoint_status": {"workspace_id", "checkpoint_id"},
        "worktree_checkpoint_restore": {
            "workspace_id",
            "task_lease",
            "checkpoint_id",
            "expected_current_snapshot_id",
        },
        "atlas_render": {"workspace_id", "snapshot_id", "packet_id", "bindings"},
        "atlas_accept": {
            "workflow_id",
            "code_task_id",
            "acceptance_id",
            "ingestion_key",
        },
        "relay_compile": {"request"},
        "relay_start": {"request"},
        "relay_status": {"workflow_id"},
        "relay_handoff": {"request"},
        "relay_integrate": {"request"},
    }.items():
        assert (
            set(getattr(tools[name], "inputSchema").get("required", ()))
            == expected_required
        )


def test_lease_and_relay_integrate_boundaries_reject_unknown_or_retired_fields() -> (
    None
):
    malformed_lease = {
        "workflow_id": "workflow",
        "task_id": "task",
        "owner": "writer",
        "lease_epoch": 1,
        "extra": "not allowed",
    }
    lease_result = server.project_index_sync(
        "sha256:" + "a" * 64,
        task_lease=malformed_lease,
        bind_as="output",
    )
    assert lease_result == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {"code": "INVALID_REQUEST", "message": "request rejected"},
    }

    retired_integrate_field = {
        "workflow_id": "workflow",
        "task_id": "task",
        "action": "integrate",
        "epoch": 1,
        "endpoint": "private-endpoint",
        "expected_task_version": 1,
        "capability": "opaque-capability",
        "candidate_id": "candidate",
        "integration_proof_id": "sha256:" + "b" * 64,
        "integration_head": "deadbeef",
    }
    integrate_result = server.relay_integrate(retired_integrate_field)
    assert integrate_result == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {"code": "RELAY_REQUEST_INVALID", "message": "request rejected"},
    }
    integrate_schema = json.dumps(
        getattr(_tools()["relay_integrate"], "inputSchema"), sort_keys=True
    )
    assert "integration_head" not in integrate_schema
    assert "integration_commit" not in integrate_schema


def test_public_results_are_always_the_exact_v1_envelope() -> None:
    result = server.relay_status("not-a-real-workflow")

    assert set(result) in ({"schema", "ok", "data"}, {"schema", "ok", "error"})
    assert result["schema"] == RESULT_SCHEMA
    if result["ok"]:
        assert type(result["data"]) is dict
    else:
        assert set(result["error"]) == {"code", "message"}
        assert result["error"]["message"] == "request rejected"


def test_relay_start_without_private_broker_is_v1_and_leaves_relay_unchanged(
    tmp_path, monkeypatch
) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch),
        }
    )
    RuntimeBootstrap.run(config)
    before = hashlib.sha256(config.relay_database.read_bytes()).hexdigest()
    root = RuntimeRoot(config)
    monkeypatch.setattr(server, "_RUNTIME_ROOT", root)

    try:
        result = server.relay_start(
            {"mode": "create", "plan": {}, "idempotency_key": "start-once"}
        )
    finally:
        root.shutdown()

    after = hashlib.sha256(config.relay_database.read_bytes()).hexdigest()
    assert result == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {
            "code": "RELAY_CAPABILITY_BROKER_UNAVAILABLE",
            "message": "request rejected",
        },
    }
    assert after == before
