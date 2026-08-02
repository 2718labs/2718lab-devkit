"""Metadata checks for the MCP-only primary plugin package."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class BugkillerMetadataTests(unittest.TestCase):
    def test_primary_plugin_manifest_is_rc1_and_mcp_only(self) -> None:
        codex = load_json(".codex-plugin/plugin.json")
        self.assertEqual("1.0.0-rc1", codex["version"])
        self.assertEqual("./.mcp.json", codex["mcpServers"])
        for legacy_surface in ("skills", "agents", "commands", "hooks"):
            self.assertNotIn(legacy_surface, codex)

        interface = codex["interface"]
        self.assertEqual([], interface["defaultPrompt"])
        advertised = json.dumps(codex, ensure_ascii=False).casefold()
        for legacy_marker in (
            "astrbot",
            "code atlas",
            "code-atlas",
            "skills",
            "agents",
            "commands",
            "hooks",
        ):
            self.assertNotIn(legacy_marker, advertised)

    def test_mcp_uses_portable_codex_stdio_configuration(self) -> None:
        metadata = load_json(".mcp.json")
        self.assertEqual({"mcpServers"}, set(metadata))
        self.assertEqual({"2718lab-devkit"}, set(metadata["mcpServers"]))
        server = metadata["mcpServers"]["2718lab-devkit"]
        self.assertEqual(server["command"], "uv")
        self.assertEqual(
            server["args"],
            ["run", "--locked", "--no-dev", "python", "server.py"],
        )
        self.assertEqual(server["cwd"], "mcp-tools")
        self.assertEqual(
            server["env_vars"],
            [
                "CODEX_DEVKIT_HOST_BRIDGE_FD",
                "CODEX_DEVKIT_HOST_BRIDGE_HANDLE",
                "CODEX_PROJECT_ROOT",
                "CODEX_WORKSPACE_ROOT",
                "CODEX_PROJECT_ID",
                "CODEX_WORKSPACE_ID",
                "CODEX_THREAD_ID",
            ],
        )
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", json.dumps(metadata))

    def test_codex_only_source_boundary(self) -> None:
        for removed_surface in (".claude-plugin", "hooks"):
            surface = ROOT / removed_surface
            self.assertFalse(surface.is_file(), removed_surface)
            self.assertFalse(
                surface.is_dir() and any(surface.iterdir()),
                removed_surface,
            )
        for relative_path in (
            "agents",
            "commands",
            "skills",
            "extensions",
        ):
            self.assertTrue((ROOT / relative_path).is_dir(), relative_path)


if __name__ == "__main__":
    unittest.main()
