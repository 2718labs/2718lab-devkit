"""Contract tests for the shared DevKit execution layer."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "agents"
WORK_METHODOLOGY = ROOT / "skills" / "work-methodology"
VALIDATOR = WORK_METHODOLOGY / "scripts" / "validate_shared_runtime.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SharedPlatformAssetTests(unittest.TestCase):
    def test_shared_agent_catalog_is_not_owned_by_bugkiller(self) -> None:
        expected = {
            "2718lab-triage.md": ("read-only", "triage"),
            "2718lab-investigator.md": ("read-only", "investigation"),
            "2718lab-doc-writer.md": ("documentation",),
            "2718lab-verifier.md": ("read-only", "verification"),
            "2718lab-code-writer.md": ("code writer", "gpt-5.6-sol", "ultra"),
            "2718lab-risk-reviewer.md": ("read-only", "dangerous user approval"),
        }
        for filename, markers in expected.items():
            path = AGENTS / filename
            self.assertTrue(path.is_file(), f"missing shared agent: {filename}")
            text = read(path)
            self.assertNotIn("Bugkiller", text, filename)
            for marker in markers:
                self.assertIn(marker, text, filename)

    def test_legacy_bugkiller_agent_names_are_thin_compatibility_aliases(self) -> None:
        aliases = {
            "bugkiller-luna-triage.md": "2718lab-triage",
            "bugkiller-terra-investigator.md": "2718lab-investigator",
            "bugkiller-terra-doc-writer.md": "2718lab-doc-writer",
            "bugkiller-terra-verifier.md": "2718lab-verifier",
            "bugkiller-sol-code-writer.md": "2718lab-code-writer",
            "bugkiller-sol-escalation.md": "2718lab-risk-reviewer",
        }
        for filename, target in aliases.items():
            text = read(AGENTS / filename)
            with self.subTest(alias=filename):
                self.assertIn("Compatibility alias", text)
                self.assertIn(target, text)
                self.assertLessEqual(len(text.splitlines()), 24)

    def test_every_domain_skill_can_use_shared_runtime_and_agents(self) -> None:
        for skill_name in (
            "astrbot-plugin-dev",
            "mcp-server-dev",
            "python-engineering",
            "oss-repo-ops",
            "bugkiller",
        ):
            text = read(ROOT / "skills" / skill_name / "SKILL.md")
            with self.subTest(skill=skill_name):
                self.assertIn("共享执行层", text)
                self.assertIn("work-methodology", text)
                self.assertIn("2718lab-tools", text)
                self.assertIn("2718lab-code-writer", text)

    def test_shared_methodology_owns_agent_routing(self) -> None:
        methodology = read(WORK_METHODOLOGY / "SKILL.md")
        team_patterns = read(WORK_METHODOLOGY / "references" / "team-patterns.md")
        runtime = read(WORK_METHODOLOGY / "references" / "orchestration-runtime.md")
        combined = "\n".join((methodology, team_patterns, runtime))

        for marker in (
            "2718lab-triage",
            "2718lab-investigator",
            "2718lab-doc-writer",
            "2718lab-verifier",
            "2718lab-code-writer",
            "2718lab-risk-reviewer",
        ):
            self.assertIn(marker, combined)
        self.assertNotIn("bugkiller-sol-code-writer", combined)
        self.assertNotIn("bugkiller-terra-doc-writer", combined)

    def test_plugin_ui_presents_devkit_instead_of_bugkiller(self) -> None:
        text = read(AGENTS / "openai.yaml")
        self.assertIn("display_name: 2718lab DevKit", text)
        self.assertIn("shared engineering", text)
        self.assertNotIn("display_name: Bugkiller", text)

    def test_bugkiller_is_only_a_specialized_overlay(self) -> None:
        text = read(ROOT / "skills" / "bugkiller" / "SKILL.md")
        self.assertIn("specialized defect workflow", text)
        self.assertIn("shared execution layer", text)
        self.assertNotIn("bugkiller-sol-code-writer", text)

    def test_shared_runtime_validator_accepts_platform_assets(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Shared runtime assets valid", result.stdout)


if __name__ == "__main__":
    unittest.main()
