"""Contract tests for the specialized Bugkiller overlay."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "bugkiller" / "SKILL.md"
REFERENCES = ROOT / "skills" / "bugkiller" / "references"
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
            "2718lab-doc-writer",
            "2718lab-code-writer",
            "2718lab-risk-reviewer",
            "gpt-5.6-sol",
            "ultra",
            "DEGRADED_TRIAGE",
        ):
            self.assertIn(required, roles)
        self.assertIn("read-only roles never write code", " ".join(roles.split()))
        self.assertNotIn("only patch writer", roles)

    def test_bugkiller_reuses_shared_agents(self) -> None:
        skill = read(SKILL)
        roles = read(REFERENCES / "roles.md")
        combined = "\n".join((skill, roles))
        for agent in (
            "2718lab-triage",
            "2718lab-investigator",
            "2718lab-doc-writer",
            "2718lab-verifier",
            "2718lab-code-writer",
            "2718lab-risk-reviewer",
        ):
            self.assertIn(agent, combined)
        self.assertNotIn("bugkiller-sol-code-writer", combined)
        self.assertNotIn("bugkiller-terra-doc-writer", combined)

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
