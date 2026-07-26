from __future__ import annotations

import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
TEAM_PATTERNS = Path(__file__).resolve().parents[1] / "references" / "team-patterns.md"
WORK_PACKAGES = Path(__file__).resolve().parents[1] / "references" / "work-packages.md"


class MethodologyPolicyTests(unittest.TestCase):
    def test_low_risk_work_does_not_require_reviewer(self) -> None:
        text = SKILL.read_text(encoding="utf-8")

        self.assertIn("低风险任务不自动创建审查代理", text)
        self.assertNotIn("开 team 时必须有一个专职唱反调成员", text)

    def test_dangerous_work_asks_user_before_review_or_escalation(self) -> None:
        text = SKILL.read_text(encoding="utf-8")

        self.assertIn("先向用户说明具体风险并询问", text)
        self.assertIn("用户明确同意后", text)

    def test_minimum_sufficient_engineering_stops_scope_growth(self) -> None:
        text = SKILL.read_text(encoding="utf-8")

        for required in (
            "最小充分工程",
            "不得重复扫描全仓",
            "不得扩展当前 scope",
            "立即停止并交付",
        ):
            self.assertIn(required, text)

    def test_team_patterns_separate_dispatch_from_optional_review(self) -> None:
        text = TEAM_PATTERNS.read_text(encoding="utf-8")

        self.assertIn("审查不是默认 team 成员", text)
        self.assertIn("危险门禁", text)

    def test_code_dispatch_is_sol_ultra_and_separate_from_dangerous_review(
        self,
    ) -> None:
        text = TEAM_PATTERNS.read_text(encoding="utf-8")

        for required in (
            "2718lab-code-writer",
            "gpt-5.6-sol",
            "ultra",
            "2718lab-doc-writer",
            "2718lab-risk-reviewer",
            "永不写代码",
        ):
            self.assertIn(required, text)
        self.assertNotIn("bugkiller-sol-code-writer", text)
        self.assertNotIn("bugkiller-terra-doc-writer", text)
        self.assertIn("危险审查", text)
        self.assertIn("不自动", text)

    def test_strict_work_package_policy_names_every_index_gate(self) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, TEAM_PATTERNS, WORK_PACKAGES)
        )

        for required in (
            "strict_index=true",
            "project_index_sync",
            "workflow_register_task",
            "project_index_query",
            "trace_id",
            "worktree_checkpoint_create",
            'project_index_sync(bind_as="output")',
            'workflow_artifact_register(kind="verification", snapshot_id=...)',
            "workflow_complete",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
