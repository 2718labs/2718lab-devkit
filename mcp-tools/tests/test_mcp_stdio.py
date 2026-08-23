"""Real stdio protocol tests for the locked 17-tool MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.tool_result import RESULT_SCHEMA

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / ".codex-plugin" / "build_main_artifact.py"

EXPECTED_TOOL_NAMES = {
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


def _build_and_extract_primary_artifact(tmp_path: Path) -> Path:
    artifact_path = tmp_path / "primary-artifact.zip"
    extracted_root = tmp_path / "extracted-primary-artifact"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--plugin-root",
            str(ROOT),
            "--output",
            str(artifact_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(artifact_path) as archive:
        archive.extractall(extracted_root)
    assert (
        extracted_root / "mcp-tools" / "devkit_runtime" / "composition.py"
    ).is_file()
    assert (
        extracted_root / "mcp-tools" / "devkit_continuity" / "service.py"
    ).is_file()
    return extracted_root


def _result_payload(result: object) -> dict[str, object]:
    structured = getattr(result, "structuredContent")
    if type(structured) is dict:
        return structured
    content = getattr(result, "content")
    assert len(content) == 1
    text = getattr(content[0], "text")
    parsed = json.loads(text)
    assert type(parsed) is dict
    return parsed


def test_stdio_initialize_lists_exact_tools_and_returns_a_v1_result(tmp_path) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    data_root = tmp_path / "data"
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(data_root), "CODEX_TASK_TEMP": str(scratch)}
    )
    RuntimeBootstrap.run(config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plan.md").write_text("# stdio\n", encoding="utf-8")

    child_environment = os.environ.copy()
    child_environment.update(
        {
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch),
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            "PYTHONPYCACHEPREFIX": str(task_root / "pycache"),
            "UV_CACHE_DIR": str(task_root / "uv-cache"),
        }
    )
    for inherited in (
        "CODEX_HOME",
        "CODEX_DEVKIT_DATA_ROOT",
        "CODEX_PROJECT_ROOT",
        "CODEX_WORKSPACE_ROOT",
        "CODEX_PROJECT_ID",
        "CODEX_WORKSPACE_ID",
        "CODEX_THREAD_ID",
    ):
        child_environment.pop(inherited, None)
    server_path = Path(__file__).resolve().parents[1] / "server.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
        env=child_environment,
        cwd=server_path.parent,
    )

    async def exercise():
        async with stdio_client(parameters, errlog=stderr_log) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool(
                    "project_index_register", {"workspace_root": str(workspace)}
                )
                return initialized, listed, result

    with (tmp_path / "server.stderr").open("w+", encoding="utf-8") as stderr_log:
        initialized, listed, result = asyncio.run(exercise())
        stderr_log.seek(0)
        stderr_text = stderr_log.read()

    assert initialized.serverInfo.name == "2718lab-devkit"
    assert {tool.name for tool in listed.tools} == EXPECTED_TOOL_NAMES
    assert not result.isError
    payload = _result_payload(result)
    assert payload["schema"] == RESULT_SCHEMA
    assert payload["ok"] is True
    data = cast(dict[str, object], payload["data"])
    assert set(data) == {"workspace_id"}
    assert "Traceback" not in stderr_text
    assert "ERROR" not in stderr_text


def test_extracted_primary_artifact_starts_without_source_checkout_dependency(
    tmp_path: Path,
) -> None:
    task_root = Path(os.environ["CODEX_TASK_TEMP"]).resolve()
    assert tmp_path.resolve().is_relative_to(task_root)
    extracted_root = _build_and_extract_primary_artifact(tmp_path)
    artifact_mcp_root = extracted_root / "mcp-tools"
    scratch = tmp_path / "artifact-scratch"
    scratch.mkdir()
    data_root = tmp_path / "artifact-data"
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(data_root), "CODEX_TASK_TEMP": str(scratch)}
    )
    RuntimeBootstrap.run(config)
    workspace = tmp_path / "artifact-workspace"
    workspace.mkdir()
    (workspace / "artifact-plan.md").write_text("# packaged stdio\n", encoding="utf-8")

    child_environment = os.environ.copy()
    for inherited in (
        "CODEX_HOME",
        "CODEX_DEVKIT_DATA_ROOT",
        "PYTHONHOME",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
        "CODEX_PROJECT_ROOT",
        "CODEX_WORKSPACE_ROOT",
        "CODEX_PROJECT_ID",
        "CODEX_WORKSPACE_ID",
        "CODEX_THREAD_ID",
    ):
        child_environment.pop(inherited, None)
    child_environment.update(
        {
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch),
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            "PYTHONPYCACHEPREFIX": str(task_root / "pycache"),
            "UV_CACHE_DIR": str(task_root / "uv-cache"),
            "UV_PROJECT_ENVIRONMENT": str(artifact_mcp_root / ".venv"),
            "PYTHONPATH": str(artifact_mcp_root),
        }
    )
    assert str(ROOT) not in child_environment["PYTHONPATH"]
    configuration = json.loads((extracted_root / ".mcp.json").read_text("utf-8"))
    server_configuration = configuration["mcpServers"]["2718lab-devkit"]
    parameters = StdioServerParameters(
        command=server_configuration["command"],
        args=server_configuration["args"],
        env=child_environment,
        cwd=extracted_root / server_configuration["cwd"],
    )

    async def exercise():
        async with stdio_client(parameters, errlog=stderr_log) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                prompts = await session.list_prompts()
                resources = await session.list_resources()
                normal = await session.call_tool(
                    "project_index_register", {"workspace_root": str(workspace)}
                )
                rejected = await session.call_tool(
                    "project_index_status", {"workspace_id": "workspace-not-registered"}
                )
                missing_bridge = await session.call_tool(
                    "relay_start",
                    {
                        "request": {
                            "mode": "create",
                            "plan": {},
                            "idempotency_key": "artifact-start-once",
                        }
                    },
                )
                return (
                    initialized,
                    listed,
                    prompts,
                    resources,
                    normal,
                    rejected,
                    missing_bridge,
                )

    relay_database = data_root / "relay.sqlite3"
    relay_before = hashlib.sha256(relay_database.read_bytes()).hexdigest()
    with (tmp_path / "artifact-server.stderr").open(
        "w+", encoding="utf-8"
    ) as stderr_log:
        initialized, listed, prompts, resources, normal, rejected, missing_bridge = (
            asyncio.run(exercise())
        )
        stderr_log.seek(0)
        stderr_text = stderr_log.read()
    relay_after = hashlib.sha256(relay_database.read_bytes()).hexdigest()

    assert initialized.serverInfo.name == "2718lab-devkit"
    assert (artifact_mcp_root / ".venv").is_dir()
    assert {tool.name for tool in listed.tools} == EXPECTED_TOOL_NAMES
    assert prompts.prompts == []
    assert resources.resources == []
    assert not normal.isError
    normal_payload = _result_payload(normal)
    assert normal_payload["schema"] == RESULT_SCHEMA
    assert normal_payload["ok"] is True
    assert set(cast(dict[str, object], normal_payload["data"])) == {"workspace_id"}
    assert not rejected.isError
    assert _result_payload(rejected) == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {
            "code": "WORKSPACE_UNREGISTERED",
            "message": "request rejected",
        },
    }
    assert not missing_bridge.isError
    assert _result_payload(missing_bridge) == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {
            "code": "RELAY_CAPABILITY_BROKER_UNAVAILABLE",
            "message": "request rejected",
        },
    }
    assert relay_after == relay_before
    assert "Traceback" not in stderr_text
    assert "ERROR" not in stderr_text
