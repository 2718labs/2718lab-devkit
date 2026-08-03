from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEAM_PATTERNS = ROOT / "references" / "team-patterns.md"
ORCHESTRATION = ROOT / "references" / "orchestration-runtime.md"
INTEGRATION = ROOT / "references" / "github-parallel-integration.md"
EFFICIENCY = ROOT / "references" / "efficiency-automation.md"
CONTRACT = ROOT / "FASTLANE_CONTRACT.md"


class MethodologyPolicyTests(unittest.TestCase):
    def test_current_host_policy_routes_terra_sol_and_attested_luna(self) -> None:
        text = TEAM_PATTERNS.read_text(encoding="utf-8")

        for required in (
            "Terra High",
            "gpt-5.6-terra",
            "Terra Max",
            "Sol High",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "exact requested pair",
        ):
            self.assertIn(required, text)

    def test_local_integration_contract_protects_disjoint_scopes_and_review(
        self,
    ) -> None:
        text = INTEGRATION.read_text(encoding="utf-8")

        for required in (
            "isolated task branch/worktree",
            "scoped commit + evidence",
            "independent review when routed",
            "ordered integration/rebase",
            "CI gate",
            "release gate",
            "disjoint",
            "must not overwrite",
            "must not merge",
            "remote push",
            "candidate/source commit",
            "base revision",
            "accepted evidence hash",
            "integration order",
        ):
            self.assertIn(required, text)

    def test_mcp_handoff_is_durable_and_ordered(self) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8") for path in (INTEGRATION, ORCHESTRATION)
        )
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
        self.assertIn("Direct chat", text)
        self.assertIn("not the source of truth", text)

    def test_runtime_keeps_coordinator_as_acceptance_owner(self) -> None:
        text = ORCHESTRATION.read_text(encoding="utf-8")

        self.assertIn(
            "Only the coordinator may call the acceptance completion gate", text
        )
        self.assertIn("does not transfer that ownership", text)
        self.assertNotIn("Only Sol may call the acceptance completion gate", text)
        self.assertIn("gpt-5.6-luna", text)
        self.assertIn("capability report", text)
        self.assertIn("Bugkiller remains a separate policy surface", text)

    def test_fast_lane_docs_lock_the_core_adapter_and_host_boundaries(self) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (CONTRACT, EFFICIENCY, ORCHESTRATION, TEAM_PATTERNS)
        )

        for required in (
            "--host-status",
            "(task_id, scheduler_role)",
            "routing_context_hash",
            "routing_result_hash",
            "NO_SAFE_WORK",
            "recommended_route",
            "worker effort 禁止 `ultra`",
            "Prewarm remains a separate read-only evidence role",
            "after coordinator-lane acceptance",
            "C-drive temporary roots are forbidden",
            "host_dispatch",
            "inherit_current_session_model",
            "index_context",
            "worker only consumes",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_efficiency_amendment_locks_resume_handoff_and_lanes(self) -> None:
        integration = INTEGRATION.read_text(encoding="utf-8")
        runtime = ORCHESTRATION.read_text(encoding="utf-8")
        normalized_integration = " ".join(integration.split())

        for required in (
            "bounded, redacted crash-resume packet",
            "workflow/task identity",
            "lease epoch",
            "current endpoint",
            "base and candidate commit ids",
            "branch/worktree identifier",
            "write-scope hash",
            "latest RED/GREEN command and result summary",
            "registered contract/evidence hashes",
            "one explicit next action",
            "raw stdout/stderr",
            "credentials",
            "source bodies",
            "environment values",
            "unbounded chat history",
            "public task interface freezes",
            "small redacted contract artifact",
            "hash plus minimal kind metadata",
            "before the producer's complete branch is integrated",
            "core",
            "extended",
            "platform",
            "core failure blocks acceptance",
            "English-first bilingual documentation",
        ):
            self.assertIn(required, normalized_integration)

        cursor = 0
        for marker in (
            "workflow_endpoint_bind",
            "workflow_inbox",
            "workflow_artifact_resolve",
            "workflow_message_ack",
            "recorded next action",
        ):
            cursor = integration.find(marker, cursor)
            self.assertGreaterEqual(cursor, 0, marker)
            cursor += len(marker)

        self.assertIn("workflow_artifact_register", runtime)
        self.assertIn("workflow_message_send", runtime)


if __name__ == "__main__":
    unittest.main()
