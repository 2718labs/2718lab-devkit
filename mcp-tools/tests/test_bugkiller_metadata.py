"""Metadata checks for the MCP-only primary plugin package."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANUAL_SKILLS = {
    "astrbot-plugin-dev",
    "bugkiller",
    "code-atlas",
    "devkit-overview",
    "fast-lane-routing",
    "mcp-server-dev",
    "oss-repo-ops",
    "python-engineering",
    "workflow-design",
}
RETIRED_MANUAL_SURFACE = re.compile(
    r"(?i)(?:^|[^a-z0-9_-])(?:agents|assets|commands|scripts)[\\/]|"
    r"bugkiller-(?:sol|terra)-"
)


def load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class BugkillerMetadataTests(unittest.TestCase):
    def test_primary_plugin_manifest_is_rc3_and_mcp_only(self) -> None:
        codex = load_json(".codex-plugin/plugin.json")
        self.assertEqual("1.0.0-rc3", codex["version"])
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
                "CODEX_FASTLANE_TASK_ROOT",
            ],
        )
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", json.dumps(metadata))

    def test_codex_only_source_boundary(self) -> None:
        for removed_surface in (".claude-plugin", "hooks", "agents", "commands"):
            surface = ROOT / removed_surface
            self.assertFalse(surface.is_file(), removed_surface)
            self.assertFalse(
                surface.is_dir() and any(surface.iterdir()),
                removed_surface,
            )
        for relative_path in ("skills", "extensions"):
            self.assertTrue((ROOT / relative_path).is_dir(), relative_path)

    def test_local_skill_bundle_contains_only_reference_manuals(self) -> None:
        skill_root = ROOT / "skills"
        self.assertEqual(
            MANUAL_SKILLS,
            {path.name for path in skill_root.iterdir() if path.is_dir()},
        )
        for skill_name in MANUAL_SKILLS:
            with self.subTest(skill_name=skill_name):
                skill_root_path = skill_root / skill_name
                skill_file = skill_root_path / "SKILL.md"
                content = skill_file.read_text(encoding="utf-8")
                self.assertTrue(content.startswith("---\n"))
                self.assertIn(f"name: {skill_name}", content)
                self.assertIn("manual", content.casefold())
                self.assertIsNone(
                    RETIRED_MANUAL_SURFACE.search(content),
                    "skill entry point refers to a retired execution surface",
                )
                for path in skill_root_path.rglob("*"):
                    self.assertFalse(path.is_symlink(), path)
                files = {
                    path.relative_to(skill_root_path).as_posix()
                    for path in skill_root_path.rglob("*")
                    if path.is_file()
                }
                self.assertIn("SKILL.md", files)
                for relative_path in files:
                    content = (skill_root_path / relative_path).read_text(
                        encoding="utf-8"
                    )
                    self.assertIsNone(
                        RETIRED_MANUAL_SURFACE.search(content),
                        relative_path,
                    )
                self.assertTrue(
                    all(
                        relative == "SKILL.md"
                        or (
                            relative.startswith("references/")
                            and relative.endswith(".md")
                        )
                        for relative in files
                    ),
                    files,
                )


if __name__ == "__main__":
    unittest.main()
