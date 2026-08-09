"""Locked public contract tests for the production 17-tool MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.tool_result import RESULT_SCHEMA, TOOL_ANNOTATIONS
from project_index.models import (
    IndexSnapshot,
    IndexState,
    IndexStatus,
    IndexStatusResult,
    IndexSyncResult,
    PackageDescriptor,
    PackagePage,
)

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
        "fastlane_compile",
        "relay_start",
        "relay_status",
        "relay_handoff",
        "relay_integrate",
    }
)

EXPECTED_PARAMETERS = {
    "project_index_register": ("workspace_root",),
    "project_index_sync": (
        "workspace_id",
        "include_paths",
        "task_lease",
        "bind_as",
        "package_page_limit",
    ),
    "project_index_status": (
        "workspace_id",
        "snapshot_id",
        "required_paths",
        "package_ids",
        "package_page_offset",
        "package_page_limit",
    ),
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
        "package_ids",
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
    "fastlane_compile": ("request", "reasoning_effort", "enable"),
    "relay_start": ("request",),
    "relay_status": ("workflow_id",),
    "relay_handoff": ("request",),
    "relay_integrate": ("request",),
}


def _tools() -> dict[str, object]:
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def _properties(tool: object) -> dict[str, object]:
    return dict(getattr(tool, "inputSchema")["properties"])


def test_public_server_exposes_exact_locked_seventeen_tool_surface() -> None:
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
        "fastlane_compile": {"request"},
        "relay_start": {"request"},
        "relay_status": {"workflow_id"},
        "relay_handoff": {"request"},
        "relay_integrate": {"request"},
    }.items():
        assert (
            set(getattr(tools[name], "inputSchema").get("required", ()))
            == expected_required
        )


def test_package_scope_is_an_additive_public_index_contract(
    tmp_path, monkeypatch
) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    workspace = tmp_path / "workspace"
    package = workspace / "packages" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (package / "module.py").write_text(
        "def selected_package_function() -> str:\n    return 'demo'\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch),
        }
    )
    RuntimeBootstrap.run(config)
    root = RuntimeRoot(config)
    monkeypatch.setattr(server, "_RUNTIME_ROOT", root)

    try:
        registered = server.project_index_register(str(workspace))
        assert registered["ok"] is True
        workspace_id = registered["data"]["workspace_id"]
        assert isinstance(workspace_id, str)

        synced = server.project_index_sync(workspace_id)
        assert synced["ok"] is True
        sync_data = synced["data"]
        assert isinstance(sync_data, dict)
        assert "packages" not in sync_data
        page = sync_data["package_page"]
        assert isinstance(page, dict)
        assert page["offset"] == 0
        assert page["limit"] == 128
        assert page["total_count"] == 1
        assert page["returned_count"] == 1
        packages = page["packages"]
        assert isinstance(packages, list)
        descriptor = packages[0]
        assert descriptor["ecosystem"] == "node"
        assert descriptor["name"] == "demo"
        assert descriptor["relative_root"] == "packages/demo"
        assert descriptor["manifest_path"] == "packages/demo/package.json"
        package_id = descriptor["package_id"]
        assert isinstance(package_id, str)

        status = server.project_index_status(
            workspace_id,
            sync_data["snapshot_id"],
            package_ids=[package_id],
            package_page_offset=0,
            package_page_limit=1,
        )
        query = server.project_index_query(
            workspace_id,
            sync_data["snapshot_id"],
            "selected_package_function",
            package_ids=[package_id],
        )
    finally:
        root.shutdown()

    assert status["ok"] is True
    assert query["ok"] is True
    assert status["data"]["package_page"]["packages"][0]["package_id"] == package_id


def test_package_page_continuation_reads_the_original_snapshot(
    tmp_path, monkeypatch
) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    workspace = tmp_path / "workspace"
    for position in range(129):
        package = workspace / "packages" / f"pkg-{position:03d}"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": f"pkg-{position:03d}"}), encoding="utf-8"
        )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch),
        }
    )
    RuntimeBootstrap.run(config)
    root = RuntimeRoot(config)
    monkeypatch.setattr(server, "_RUNTIME_ROOT", root)

    try:
        registered = server.project_index_register(str(workspace))
        assert registered["ok"] is True
        workspace_id = registered["data"]["workspace_id"]
        assert isinstance(workspace_id, str)

        first_sync = server.project_index_sync(workspace_id)
        assert first_sync["ok"] is True
        first_data = first_sync["data"]
        first_page = first_data["package_page"]
        assert first_page["total_count"] == 129
        assert first_page["returned_count"] == 128
        assert first_page["next_offset"] == 128
        first_ids = {
            descriptor["package_id"] for descriptor in first_page["packages"]
        }

        added = workspace / "packages" / "post-snapshot"
        added.mkdir()
        (added / "package.json").write_text('{"name":"post-snapshot"}', encoding="utf-8")
        latest_sync = server.project_index_sync(workspace_id)
        assert latest_sync["ok"] is True
        assert latest_sync["data"]["package_page"]["total_count"] == 130

        continuation = server.project_index_status(
            workspace_id,
            first_data["snapshot_id"],
            package_page_offset=first_page["next_offset"],
            package_page_limit=128,
        )
    finally:
        root.shutdown()

    assert continuation["ok"] is True
    continuation_page = continuation["data"]["package_page"]
    continuation_ids = {
        descriptor["package_id"] for descriptor in continuation_page["packages"]
    }
    assert continuation_page["total_count"] == 129
    assert continuation_page["returned_count"] == 1
    assert "next_offset" not in continuation_page
    assert not first_ids & continuation_ids
    assert len(first_ids | continuation_ids) == 129
    assert "post-snapshot" not in str(continuation_page)


def test_package_selectors_preserve_none_and_explicit_empty(monkeypatch) -> None:
    observed: list[tuple[str, object]] = []

    class _ProjectIndex:
        def status(self, *_: object, package_ids: object) -> object:
            observed.append(("status", package_ids))
            return object()

    class _ProjectCheckpoint:
        project_index = _ProjectIndex()

        def query(self, *_: object, package_ids: object, **__: object) -> object:
            observed.append(("query", package_ids))
            return object()

    uow = SimpleNamespace(project_checkpoint=_ProjectCheckpoint())

    def invoke(_: str, *, operation, **__: object) -> dict[str, object]:
        operation(uow)
        return {"schema": RESULT_SCHEMA, "ok": True, "data": {}}

    monkeypatch.setattr(server, "_invoke", invoke)

    server.project_index_status("workspace", package_ids=None)
    server.project_index_status("workspace", package_ids=[])
    server.project_index_query("workspace", "snapshot", "needle", package_ids=None)
    server.project_index_query("workspace", "snapshot", "needle", package_ids=[])

    assert observed == [
        ("status", None),
        ("status", ()),
        ("query", None),
        ("query", ()),
    ]


def test_package_pages_are_explicit_snapshot_bound_and_prevalidated(monkeypatch) -> None:
    observed: list[tuple[object, ...]] = []
    packages = (
        PackageDescriptor(
            package_id="sha256:" + "1" * 64,
            ecosystem="node",
            name="package",
            root_path="packages/package",
            manifest_path="packages/package/package.json",
            manifest_hash="sha256:" + "2" * 64,
        ),
    )
    snapshot = IndexSnapshot(
        snapshot_id="snapshot-1",
        workspace="workspace-1",
        workspace_id="workspace-1",
        state=IndexState.INDEX_READY,
        file_count=1,
        blob_count=1,
        reused_blob_count=0,
        node_count=0,
        edge_count=0,
        gap_count=0,
        packages=packages,
    )
    page = PackagePage(
        snapshot_id="snapshot-1",
        offset=0,
        limit=32,
        total_count=1,
        packages=packages,
        next_offset=None,
    )
    status = IndexStatus(
        workspace="workspace-1",
        snapshot_id="snapshot-1",
        state=IndexState.INDEX_READY,
    )

    class _ProjectIndex:
        def sync(self, workspace_id: str, include_paths: object) -> IndexSnapshot:
            observed.append(("sync", workspace_id, include_paths))
            return snapshot

        def status(self, *args: object, **kwargs: object) -> IndexStatus:
            observed.append(("status", args, kwargs))
            return status

        def package_page(
            self,
            workspace_id: str,
            snapshot_id: str,
            *,
            offset: int,
            limit: int,
        ) -> PackagePage:
            observed.append(("page", workspace_id, snapshot_id, offset, limit))
            return page

    class _ProjectCheckpoint:
        project_index = _ProjectIndex()

    uow = SimpleNamespace(project_checkpoint=_ProjectCheckpoint())
    captured: list[object] = []

    def invoke(_: str, *, operation, **__: object) -> dict[str, object]:
        captured.append(operation(uow))
        return {"schema": RESULT_SCHEMA, "ok": True, "data": {}}

    monkeypatch.setattr(server, "_invoke", invoke)

    server.project_index_sync("workspace-1", package_page_limit=32)
    server.project_index_status(
        "workspace-1",
        "snapshot-1",
        package_page_offset=0,
        package_page_limit=32,
    )

    assert isinstance(captured[0], IndexSyncResult)
    assert isinstance(captured[1], IndexStatusResult)
    assert observed == [
        ("sync", "workspace-1", None),
        ("page", "workspace-1", "snapshot-1", 0, 32),
        ("status", ("workspace-1", "snapshot-1", None), {"package_ids": None}),
        ("page", "workspace-1", "snapshot-1", 0, 32),
    ]
    assert server.project_index_status(
        "workspace-1", "snapshot-1", package_page_offset=0
    )["error"]["code"] == "INVALID_QUERY"
    assert server.project_index_status(
        "workspace-1", package_page_offset=0, package_page_limit=32
    )["error"]["code"] == "INVALID_QUERY"
    assert server.project_index_status(
        "workspace-1", "", package_page_offset=0, package_page_limit=32
    )["error"]["code"] == "INVALID_QUERY"
    assert server.project_index_sync("workspace-1", package_page_limit=129)["error"][
        "code"
    ] == "INVALID_QUERY"


def test_sync_does_not_bind_output_until_its_package_page_is_ready(monkeypatch) -> None:
    snapshot = IndexSnapshot(
        snapshot_id="snapshot-1",
        workspace="workspace-1",
        workspace_id="workspace-1",
        state=IndexState.INDEX_READY,
        file_count=1,
        blob_count=1,
        reused_blob_count=0,
        node_count=0,
        edge_count=0,
        gap_count=0,
    )
    bindings: list[tuple[object, ...]] = []

    class _Authority:
        def bind_output_snapshot(self, *args: object, **kwargs: object) -> None:
            bindings.append((args, kwargs))

    class _ProjectIndex:
        def sync(self, *_: object) -> IndexSnapshot:
            return snapshot

        def package_page(self, *_: object, **__: object) -> PackagePage:
            raise RuntimeError("page load failed")

    uow = SimpleNamespace(
        project_checkpoint=SimpleNamespace(project_index=_ProjectIndex())
    )
    monkeypatch.setattr(server, "_TASK_LEASE_AUTHORITY", _Authority())
    monkeypatch.setattr(
        server,
        "_invoke",
        lambda _name, *, operation, **_kwargs: operation(uow),
    )

    with pytest.raises(RuntimeError, match="page load failed"):
        server.project_index_sync(
            "workspace-1",
            task_lease={
                "workflow_id": "workflow-1",
                "task_id": "task-1",
                "owner": "owner-1",
                "lease_epoch": 1,
            },
            bind_as="output",
        )

    assert bindings == []


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
