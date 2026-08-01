"""Real stdio protocol tests for the locked 16-tool MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.tool_result import RESULT_SCHEMA

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
    "relay_start",
    "relay_status",
    "relay_handoff",
    "relay_integrate",
}


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
    child_environment.pop("CODEX_HOME", None)
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
