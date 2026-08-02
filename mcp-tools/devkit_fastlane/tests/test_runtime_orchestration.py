from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "FASTLANE_CONTRACT.md"
RUNTIME = ROOT / "references" / "orchestration-runtime.md"


class RuntimeOrchestrationPolicyTests(unittest.TestCase):
    def test_multi_agent_work_prefers_executable_orchestrator(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
