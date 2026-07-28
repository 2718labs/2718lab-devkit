"""Risk routing that keeps ordinary Bugkiller cases cheap and direct."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import BugState, ModelBudgets, ModelRole, RiskTrigger


@dataclass(frozen=True)
class RouteDecision:
    target_state: BugState
    requires_user_gate: bool
    request_reviewer: bool
    model_budgets: ModelBudgets
    degraded_triage: bool = False
    reason: str = ""


def _coerce_triggers(triggers: Iterable[RiskTrigger | str]) -> frozenset[RiskTrigger]:
    values: set[RiskTrigger] = set()
    for trigger in triggers:
        if isinstance(trigger, RiskTrigger):
            values.add(trigger)
        else:
            try:
                values.add(RiskTrigger(trigger))
            except ValueError as error:
                raise ValueError(
                    f"unknown Bugkiller risk trigger: {trigger}"
                ) from error
    return frozenset(values)


def _budgets(
    *,
    luna_available: bool = True,
    terra_available: bool = True,
    sol_calls: int = 0,
) -> ModelBudgets:
    return ModelBudgets(
        {
            ModelRole.LUNA: int(luna_available),
            ModelRole.TERRA: int(terra_available),
            ModelRole.SOL: sol_calls,
        }
    )


def route_case(
    risk_triggers: Iterable[RiskTrigger | str],
    *,
    luna_available: bool = True,
    terra_available: bool = True,
    approved_escalation: bool = False,
) -> RouteDecision:
    """Route a case without treating model output as authorization.

    An approved escalation is the canonical explicit record supplied by the
    caller after its user gate. It is the only condition that grants one Sol
    call and reviewer routing for a risky case.
    """

    triggers = _coerce_triggers(risk_triggers)
    if not terra_available:
        return RouteDecision(
            BugState.BLOCKED,
            False,
            False,
            _budgets(luna_available=luna_available, terra_available=False),
            reason="terra_unavailable",
        )
    if triggers:
        if not luna_available:
            return RouteDecision(
                BugState.BLOCKED,
                False,
                False,
                _budgets(luna_available=False),
                reason="luna_unavailable",
            )
        if not approved_escalation:
            return RouteDecision(
                BugState.HUMAN_GATE,
                True,
                False,
                _budgets(luna_available=luna_available),
                reason="risk_gate_required",
            )
        return RouteDecision(
            BugState.REVIEWING,
            False,
            True,
            _budgets(luna_available=luna_available, sol_calls=1),
            reason="approved_dangerous_escalation",
        )
    return RouteDecision(
        BugState.DONE,
        False,
        False,
        _budgets(luna_available=luna_available),
        degraded_triage=not luna_available,
        reason="low_risk_direct_completion",
    )
