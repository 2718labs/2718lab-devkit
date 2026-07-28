"""Contract tests for plugin-bundled Bugkiller skill assets."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
AGENTS = ROOT / "agents"
UI_METADATA = AGENTS / "openai.yaml"
VALIDATOR = ROOT / "skills" / "bugkiller" / "scripts" / "validate_bugkiller.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class BugkillerAssetTests(unittest.TestCase):
    def test_skill_is_thin_and_routes_to_references(self) -> None:
        text = read(SKILL)
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: bugkiller", text)
        self.assertIn("description: Use when", text)
        self.assertIn("\n# Bugkiller\n", text)
        self.assertLessEqual(len(text.splitlines()), 120)
        self.assertIn("references/", text)

    def test_references_preserve_mailbox_and_permission_boundaries(self) -> None:
        text = "\n".join(read(path) for path in REFERENCES.glob("*.md"))
        for required in (
            "DEGRADED_SKILL_ONLY",
            "workflow_artifact_register",
            "workflow_message_send",
            "collaboration.send_message",
            "workflow_inbox",
            "workflow_message_ack",
            "TTL",
            "does not grant",
        ):
            self.assertIn(required, text)
        self.assertNotIn("coordinator forwards", text.lower())
        roles = read(REFERENCES / "roles.md")
        for required in (
            "spawn",
            "model choices",
            "explicitly select Luna",
            "bugkiller-terra-doc-writer",
            "bugkiller-sol-code-writer",
            "gpt-5.6-sol",
            "ultra",
            "DEGRADED_TRIAGE",
        ):
            self.assertIn(required, roles)
        self.assertTrue(
            "Luna and Terra never write code" in roles
            or "Luna/Terra never write code" in roles
        )
        self.assertNotIn("only patch writer", roles)

    def test_agent_roles_and_escalation_policy_are_explicit(self) -> None:
        expected = {
            "bugkiller-luna-triage.md": ("Luna", "read-only", "DEGRADED_TRIAGE"),
            "bugkiller-terra-investigator.md": ("Terra", "read-only", "investigation"),
            "bugkiller-terra-doc-writer.md": ("Terra", "documentation"),
            "bugkiller-terra-verifier.md": ("Terra", "read-only", "verification"),
            "bugkiller-sol-code-writer.md": (
                "Sol",
                "code writer",
                "gpt-5.6-sol",
                "ultra",
            ),
            "bugkiller-sol-escalation.md": ("Sol", "read-only", "budget: 0"),
        }
        for filename, markers in expected.items():
            path = AGENTS / filename
            self.assertTrue(path.is_file(), f"missing agent asset: {filename}")
            text = read(path)
            self.assertTrue(text.startswith("---\n"), filename)
            self.assertIn("---", text[4:], filename)
            self.assertIn("\n# ", text, filename)
            for marker in markers:
                self.assertIn(marker, text, filename)
        self.assertFalse(
            (AGENTS / "bugkiller-terra-writer.md").exists(),
            "deprecated Terra code-writer asset must be removed",
        )
        for filename in (
            "bugkiller-luna-triage.md",
            "bugkiller-terra-investigator.md",
            "bugkiller-terra-doc-writer.md",
            "bugkiller-terra-verifier.md",
        ):
            text = read(AGENTS / filename).lower()
            self.assertTrue(
                any(
                    marker in text
                    for marker in (
                        "never write code",
                        "must not write code",
                        "do not write code",
                    )
                ),
                f"{filename} must explicitly prohibit code writes",
            )
        sol = read(AGENTS / "bugkiller-sol-escalation.md")
        self.assertIn("dangerous user approval", sol)
        self.assertIn("one call", sol)
        writer = read(AGENTS / "bugkiller-sol-code-writer.md")
        self.assertIn("dispatch", writer)
        self.assertIn("do not automatically request reviewer", writer)

    def test_strict_index_workflow_policy_names_every_gate_in_order(self) -> None:
        text = read(REFERENCES / "workflow.md")
        ordered = (
            "project_index_sync",
            "strict_index=true",
            "project_index_query",
            "trace_id",
            "worktree_checkpoint_create",
            'project_index_sync(bind_as="output")',
            "project_index_query",
            "trace_id",
            'workflow_artifact_register(kind="verification", snapshot_id=...)',
            "workflow_complete",
        )
        cursor = 0
        for marker in ordered:
            with self.subTest(marker=marker):
                cursor = text.find(marker, cursor)
                self.assertGreaterEqual(
                    cursor, 0, f"missing or out-of-order marker: {marker}"
                )
                cursor += len(marker)

    def test_plugin_ui_metadata_has_all_agent_entries(self) -> None:
        text = read(UI_METADATA)
        self.assertIn("interface:", text)
        self.assertIn("display_name:", text)
        self.assertIn("short_description:", text)
        self.assertIn("default_prompt:", text)
        self.assertNotIn("agents:", text)
        self.assertNotIn("model:", text)
        self.assertNotIn(".toml", text)

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
