"""Plugin packaging contracts required for first-turn DevKit tool discovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MCP_SKILLS = ("fast-lane-routing", "code-atlas", "workflow-design")
MCP_SKILL_TREES = tuple(f"skills/{name}" for name in MCP_SKILLS)


def test_mcp_backed_skills_require_the_local_devkit_server() -> None:
    plugin = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    mcp_configuration = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert plugin["mcpServers"] == "./.mcp.json"
    assert set(mcp_configuration["mcpServers"]) == {"2718lab-devkit"}

    for skill_name in MCP_SKILLS:
        metadata = ROOT / "skills" / skill_name / "agents" / "openai.yaml"
        assert metadata.is_file(), f"missing Codex metadata for {skill_name}"
        document = yaml.safe_load(metadata.read_text(encoding="utf-8"))
        assert set(document) == {"interface", "dependencies"}
        assert set(document["interface"]) == {"display_name", "short_description"}
        assert document["dependencies"] == {
            "tools": [{"type": "mcp", "value": "2718lab-devkit"}]
        }


def test_release_and_marketplace_artifacts_include_skills_and_mcp_runtime() -> None:
    for allowlist_name in (
        "main-artifact-allowlist.json",
        "marketplace-artifact-allowlist.json",
    ):
        allowlist = json.loads(
            (ROOT / ".codex-plugin" / allowlist_name).read_text(encoding="utf-8")
        )
        selected = {*allowlist["files"], *allowlist["trees"]}
        assert ".mcp.json" in selected
        assert "mcp-tools/server.py" in selected
        for runtime_tree in (
            "mcp-tools/bugkiller",
            "mcp-tools/devkit_atlas",
            "mcp-tools/devkit_relay",
            "mcp-tools/devkit_runtime",
            "mcp-tools/devkit_continuity",
            "mcp-tools/orchestrator",
            "mcp-tools/project_index",
        ):
            assert runtime_tree in selected
        assert "skills" not in selected
        if allowlist_name == "main-artifact-allowlist.json":
            assert all(tree in selected for tree in MCP_SKILL_TREES)
            assert "skills/bugkiller" not in selected
        else:
            source_skill_trees = {
                f"skills/{path.name}"
                for path in (ROOT / "skills").iterdir()
                if path.is_dir()
            }
            assert source_skill_trees <= selected


def test_importing_server_registers_tools_without_loading_runtime_stores() -> None:
    mcp_root = ROOT / "mcp-tools"
    heavy_modules = (
        "devkit_runtime.bootstrap",
        "devkit_runtime.composition",
        "devkit_runtime.config",
        "devkit_runtime.relay_runtime",
        "devkit_runtime.uow",
        "devkit_relay.compiler",
        "devkit_relay.service",
        "project_index.checkpoints",
        "project_index.models",
    )
    probe = (
        "import asyncio,json,sys; import server; "
        "tools=asyncio.run(server.mcp.list_tools()); "
        f"heavy={heavy_modules!r}; "
        "print(json.dumps({'tool_count':len(tools),"
        "'loaded':[name for name in heavy if name in sys.modules]}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=mcp_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"tool_count": 17, "loaded": []}
