"""Contract tests for current Bugkiller routing assets."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
AGENTS = ROOT / "agents"
PROFILE = ROOT / "skills" / "code-atlas" / "assets" / "host-profiles.json"
UI_METADATA = AGENTS / "openai.yaml"
VALIDATOR = ROOT / "skills" / "bugkiller" / "scripts" / "validate_bugkiller.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class BugkillerAssetTests(unittest.TestCase):
    def test_skill_is_thin_and_names_current_routing_policy(self) -> None:
        text = read(SKILL)

        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: bugkiller", text)
        self.assertIn("\n# Bugkiller\n", text)
        self.assertLessEqual(len(text.splitlines()), 120)
        for marker in ("Terra High", "Terra Max", "Sol High", "Luna is unavailable"):
            self.assertIn(marker, text)

    def test_references_preserve_durable_handoff_and_scope_boundaries(self) -> None:
        text = "\n".join(read(path) for path in REFERENCES.glob("*.md"))
        cursor = 0
        for marker in (
            "workflow_artifact_register",
            "workflow_message_send",
            "workflow_inbox",
            "workflow_artifact_resolve",
            "workflow_message_ack",
        ):
            cursor = text.find(marker, cursor)
            self.assertGreaterEqual(cursor, 0, marker)
            cursor += len(marker)
        for marker in ("does not grant", "candidate commit", "final acceptance"):
            self.assertIn(marker, text)
        roles = read(REFERENCES / "roles.md")
        for marker in (
            "Sol coordinator",
            "gpt-5.6-terra",
            "Terra High",
            "Terra Max",
            "Sol High",
            "Luna",
        ):
            self.assertIn(marker, roles)

    def test_agent_assets_match_current_roles_and_remove_obsolete_assets(self) -> None:
        expected = {
            "bugkiller-sol-coordinator.md": ("Sol", "final acceptance"),
            "bugkiller-terra-investigator.md": ("Terra High", "gpt-5.6-terra"),
            "bugkiller-terra-doc-writer.md": ("Terra High", "documentation-only"),
            "bugkiller-terra-verifier.md": ("Terra High", "read-only"),
            "bugkiller-sol-escalation.md": ("Sol High", "exceptional"),
        }
        for filename, markers in expected.items():
            content = read(AGENTS / filename)
            self.assertTrue(content.startswith("---\n"), filename)
            self.assertIn("\n# ", content, filename)
            for marker in markers:
                self.assertIn(marker, content, filename)

        for filename in (
            "-".join(("bugkiller", "sol", "code", "writer")) + ".md",
            "-".join(("bugkiller", "luna", "triage")) + ".md",
        ):
            self.assertFalse((AGENTS / filename).exists(), filename)

    def test_host_profile_encodes_exact_routes_and_luna_unavailability(self) -> None:
        profile = json.loads(read(PROFILE))
        roles = profile["hosts"]["codex"]["roles"]

        self.assertEqual(
            {"model": "gpt-5.6-terra", "reasoning": "high"},
            {key: roles["code"]["normal"][key] for key in ("model", "reasoning")},
        )
        self.assertEqual(
            {"model": "gpt-5.6-terra", "reasoning": "max"},
            {key: roles["code"]["complex"][key] for key in ("model", "reasoning")},
        )
        self.assertEqual("unavailable", roles["luna"]["status"])
        self.assertEqual({"codex"}, set(profile["hosts"]))

    def test_plugin_ui_metadata_has_only_ui_fields(self) -> None:
        text = read(UI_METADATA)
        self.assertIn("interface:", text)
        for marker in ("display_name:", "short_description:", "default_prompt:"):
            self.assertIn(marker, text)
        self.assertNotIn("agents:", text)
        self.assertNotIn("model:", text)

    def test_validator_accepts_all_assets(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Bugkiller assets valid", result.stdout)


if __name__ == "__main__":
    unittest.main()
