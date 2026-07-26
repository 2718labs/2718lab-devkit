from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
RUNTIME = ROOT / "references" / "orchestration-runtime.md"


class RuntimeOrchestrationPolicyTests(unittest.TestCase):
    def test_multi_agent_work_prefers_executable_orchestrator(self) -> None:
        text = SKILL.read_text(encoding="utf-8")

        self.assertIn("多代理任务优先使用 `2718lab-tools` 的可执行编排", text)
        self.assertIn("DEGRADED_SKILL_ONLY", text)

    def test_runtime_contract_has_state_dag_and_context_projection(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")

        for required in (
            "SQLite",
            "linear state machine",
            "DAG wave",
            "ready",
            "claim",
            "complete",
            "role-scoped context",
            "content hash",
        ):
            self.assertIn(required, text)

    def test_runtime_exposes_shared_adapter_and_approval_tools(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")

        for required in (
            "workflow_detect_adapters",
            "workflow_approval_prepare",
            "workflow_approval_grant",
            "workflow_approval_deny",
            "workflow_approval_claim",
        ):
            self.assertIn(required, text)

    def test_skill_only_fallback_is_explicitly_non_authoritative(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")

        self.assertIn("filesystem Markdown is a projection", text)
        self.assertIn("not the authoritative scheduler state", text)

    def test_agents_communicate_without_coordinator_relay(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")

        for required in (
            "workflow_peers",
            "workflow_message_send",
            "workflow_inbox",
            "workflow_message_ack",
            "direct `send_message`",
            "coordinator does not relay message bodies",
            "artifact hash",
        ):
            self.assertIn(required, text)

    def test_strict_index_runtime_policy_orders_write_and_completion_gates(
        self,
    ) -> None:
        text = RUNTIME.read_text(encoding="utf-8")
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

    def test_planned_new_paths_keep_exact_write_scope(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")

        for required in (
            "尚不存在、准备新建的文件",
            "INDEX_STALE",
            "不得为",
            "精确文件范围扩大成整个目录",
            "输出快照与完成门禁必须覆盖实际产物",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
