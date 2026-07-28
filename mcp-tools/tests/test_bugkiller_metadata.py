"""Release metadata consistency checks for the Bugkiller plugin surface."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_json(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class BugkillerMetadataTests(unittest.TestCase):
    def test_plugin_manifests_share_bugkiller_release_version(self) -> None:
        codex = load_json(".codex-plugin/plugin.json")
        claude = load_json(".claude-plugin/plugin.json")
        self.assertEqual(codex["version"].split("+", 1)[0], "0.2.0")
        self.assertEqual(claude["version"], "0.2.0")
        self.assertTrue(
            codex["version"] == claude["version"]
            or codex["version"].startswith(f"{claude['version']}+codex.")
        )
        self.assertIn("Bugkiller", str(codex["description"]))
        self.assertIn("Bugkiller", str(claude["description"]))
        interface = codex["interface"]
        self.assertIn("Bugkiller", str(interface["longDescription"]))
        self.assertLessEqual(len(interface["defaultPrompt"]), 3)
        self.assertTrue(
            any("Bugkiller" in prompt for prompt in interface["defaultPrompt"])
        )

    def test_codex_hook_uses_supported_manifest_and_output_schema(self) -> None:
        hook = load_json("hooks/hooks.json")
        self.assertEqual({"hooks"}, set(hook))
        post_tool = hook["hooks"]["PostToolUse"][0]
        self.assertEqual("Edit|Write", post_tool["matcher"])

        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.yaml"
            metadata.write_text("version: 1.0\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "metadata_guard.py")],
                input=json.dumps({"tool_input": {"file_path": str(metadata)}}),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("systemMessage", output)
        self.assertNotIn("hookSpecificOutput", output)

    def test_mcp_uses_portable_codex_stdio_configuration(self) -> None:
        metadata = load_json(".mcp.json")
        self.assertEqual({"2718lab-tools"}, set(metadata["mcpServers"]))
        server = metadata["mcpServers"]["2718lab-tools"]
        self.assertEqual(server["command"], "python")
        self.assertEqual(server["args"], ["mcp-tools/server.py"])
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(
            server["env_vars"],
            ["BUGKILLER_HOME", "PLUGIN_DATA", "CODEX_HOME"],
        )
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", json.dumps(metadata))

    def test_readme_documents_runtime_and_human_gates(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in (
            "无 WebUI",
            "简单状态机",
            "复杂 DAG",
            "DEGRADED_TRIAGE",
            "Terra",
            "Sol",
            "危险审批",
            "commit、push、PR",
            "独立门",
            "运行时路由",
            "spawn",
            "Luna/Terra",
            "永不写代码",
            "bugkiller-terra-doc-writer",
            "bugkiller-sol-code-writer",
            "gpt-5.6-sol",
            "ultra",
            "strict_index=true",
            "project_index_sync",
            "project_index_query",
            "trace_id",
            "worktree_checkpoint_create",
            'project_index_sync(bind_as="output")',
            'workflow_artifact_register(kind="verification", snapshot_id=...)',
            "workflow_complete",
            "astrbot-plugin-dev",
            "mcp-server-dev",
            "python-engineering",
            "oss-repo-ops",
            "pidan-local-plugins",
            "2718lab-tools",
        ):
            self.assertIn(marker, text)

    def test_work_index_records_completed_cards_and_active_runtime(self) -> None:
        text = (
            ROOT / "docs" / "superpowers" / "work" / "2026-07-24-bugkiller" / "index.md"
        ).read_text(encoding="utf-8")
        for card in ("ORCH-04A", "ORCH-04", "BK-03", "BK-04"):
            self.assertIn(f"`tasks/{card}.md` | done", text)
        self.assertIn("`tasks/BK-05.md` | done", text)
        self.assertIn("`tasks/BK-06.md` | done", text)


if __name__ == "__main__":
    unittest.main()
