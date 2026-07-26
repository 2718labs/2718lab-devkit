"""Policy tests for Bugkiller domain routing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bugkiller.models import BugState, ModelRole, RiskTrigger
from bugkiller.policy import route_case
from bugkiller.state_machine import CaseEvent, is_case_state, transition


class BugkillerPolicyTests(unittest.TestCase):
    def test_low_risk_case_completes_without_reviewer_or_sol(self) -> None:
        decision = route_case(())

        self.assertEqual(BugState.DONE, decision.target_state)
        self.assertFalse(decision.requires_user_gate)
        self.assertFalse(decision.request_reviewer)
        self.assertEqual(0, decision.model_budgets[ModelRole.SOL])

    def test_high_risk_case_requires_user_gate_before_escalation(self) -> None:
        blocked = route_case((RiskTrigger.CREDENTIALS,))
        approved = route_case((RiskTrigger.CREDENTIALS,), approved_escalation=True)

        self.assertEqual(BugState.HUMAN_GATE, blocked.target_state)
        self.assertTrue(blocked.requires_user_gate)
        self.assertFalse(blocked.request_reviewer)
        self.assertEqual(0, blocked.model_budgets[ModelRole.SOL])
        self.assertEqual(BugState.REVIEWING, approved.target_state)
        self.assertTrue(approved.request_reviewer)
        self.assertEqual(1, approved.model_budgets[ModelRole.SOL])

    def test_degraded_triage_is_flag_and_resumed_is_event(self) -> None:
        decision = route_case((), luna_available=False)

        self.assertTrue(decision.degraded_triage)
        self.assertEqual(0, decision.model_budgets[ModelRole.LUNA])
        self.assertGreaterEqual(decision.model_budgets[ModelRole.TERRA], 1)
        self.assertTrue(is_case_state(BugState.TRIAGED))
        self.assertFalse(is_case_state("DEGRADED_TRIAGE"))
        self.assertEqual(
            BugState.REPRODUCING, transition(BugState.TRIAGED, CaseEvent.RESUMED)
        )


if __name__ == "__main__":
    unittest.main()
