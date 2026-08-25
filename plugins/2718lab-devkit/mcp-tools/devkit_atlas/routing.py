"""Deterministic, offline host-routing policy for Atlas.

The resolver is deliberately not a dispatcher.  It reads only an explicit,
host-reported capability record and returns a policy decision; it cannot spawn
an agent, invoke a model, call a network, or select an unreported fallback.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import ASSET_ROOT


class RoutingStatus(str, Enum):  # noqa: UP042 - retain the public enum type
    """Stable outcomes for an offline routing decision."""

    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RoutingAttempt:
    """An actual dispatch attempt, retained for a future dispatcher boundary."""

    host: str
    model: str
    reasoning: str
    status: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "host": self.host,
            "model": self.model,
            "reasoning": self.reasoning,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """Canonical policy result; ``attempts`` never implies a model invocation."""

    status: RoutingStatus
    requested_host: str | None
    requested_role: str | None
    requested_model: str | None
    requested_reasoning: str | None
    effective_host: str | None
    effective_role: str | None
    effective_model: str | None
    effective_reasoning: str | None
    attempts: tuple[RoutingAttempt, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "requested_host": self.requested_host,
            "requested_role": self.requested_role,
            "requested_model": self.requested_model,
            "requested_reasoning": self.requested_reasoning,
            "effective_host": self.effective_host,
            "effective_role": self.effective_role,
            "effective_model": self.effective_model,
            "effective_reasoning": self.effective_reasoning,
            "attempts": [item.to_dict() for item in self.attempts],
            "reason": self.reason,
        }


_PROFILE_PATH = ASSET_ROOT / "host-profiles.json"
_REQUEST_FIELDS = frozenset(
    {
        "host",
        "role",
        "complexity",
        "exceptional",
        "model",
        "reasoning",
        "escalation_reason",
    }
)
_ROLE_ALIASES = {
    "codex": {
        "sol": "coordinator",
        "sol_coordinator": "coordinator",
        "terra": "code",
        "terra_high": "code",
        "terra_max": "code",
        "code_worker": "code",
        "coding": "code",
    },
}
_COMPLEXITY_ALIASES = {"routine": "normal", "bounded": "normal"}


def load_host_profiles(path: Path | str = _PROFILE_PATH) -> dict[str, Any]:
    """Load the bundled, versioned policy asset without consulting a service."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_host_profiles")
    return payload


HOST_PROFILES = load_host_profiles()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _request_values(
    request: Mapping[str, object],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    host = _text(request.get("host"))
    role = _text(request.get("role"))
    model = _text(request.get("model"))
    reasoning = _text(request.get("reasoning"))
    escalation_reason = _text(request.get("escalation_reason"))
    return host, role, model, reasoning, escalation_reason


def _result(
    status: RoutingStatus,
    *,
    requested_host: str | None,
    requested_role: str | None,
    requested_model: str | None,
    requested_reasoning: str | None,
    reason: str,
    effective_host: str | None = None,
    effective_role: str | None = None,
    effective_model: str | None = None,
    effective_reasoning: str | None = None,
) -> RoutingResult:
    return RoutingResult(
        status=status,
        requested_host=requested_host,
        requested_role=requested_role,
        requested_model=requested_model,
        requested_reasoning=requested_reasoning,
        effective_host=effective_host,
        effective_role=effective_role,
        effective_model=effective_model,
        effective_reasoning=effective_reasoning,
        attempts=(),
        reason=reason,
    )


def _host_capability_report(
    host: str,
    capabilities: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Accept a single-host report or an explicitly keyed host-report mapping."""

    direct_host = _text(capabilities.get("host"))
    if direct_host is not None:
        return capabilities if direct_host == host else None
    nested = capabilities.get(host)
    return nested if isinstance(nested, Mapping) else None


def _reports_capability(
    report: Mapping[str, object], model: str, reasoning: str
) -> bool:
    models = report.get("models")
    if not isinstance(models, Mapping):
        return False
    model_report = models.get(model)
    if not isinstance(model_report, Mapping):
        return False
    reported_reasoning = model_report.get("reasoning")
    if isinstance(reported_reasoning, str):
        return reported_reasoning == reasoning
    if not isinstance(reported_reasoning, Sequence):
        return False
    return reasoning in reported_reasoning and all(
        isinstance(value, str) for value in reported_reasoning
    )


def _route_reasoning_options(route: Mapping[str, object]) -> tuple[str, ...] | None:
    raw_reasoning = route.get("reasoning")
    if isinstance(raw_reasoning, str):
        return (raw_reasoning,) if raw_reasoning else None
    if not isinstance(raw_reasoning, Sequence) or isinstance(
        raw_reasoning, (str, bytes)
    ):
        return None
    options = tuple(raw_reasoning)
    if not options or not all(isinstance(value, str) and value for value in options):
        return None
    if len(set(options)) != len(options):
        return None
    return options


def _profile_route(
    policy: Mapping[str, object],
    host: str,
    role: str,
    complexity: str,
) -> tuple[Mapping[str, object] | None, str]:
    hosts = policy.get("hosts")
    if not isinstance(hosts, Mapping):
        return None, "invalid_policy"
    host_profile = hosts.get(host)
    if not isinstance(host_profile, Mapping):
        return None, "unknown_host"
    roles = host_profile.get("roles")
    if not isinstance(roles, Mapping):
        return None, "invalid_policy"
    role_profile = roles.get(role)
    if not isinstance(role_profile, Mapping):
        return None, "unknown_role"
    if role == "code" and host == "codex":
        route = role_profile.get(complexity)
        if not isinstance(route, Mapping):
            return None, "unknown_complexity"
        return route, ""
    return role_profile, ""


def resolve_role(
    request: Mapping[str, object],
    host_capabilities: Mapping[str, object],
    *,
    profiles: Mapping[str, object] | None = None,
) -> RoutingResult:
    """Resolve one route from policy and host facts, failing closed on uncertainty.

    ``host_capabilities`` is mandatory and must be a host-reported model map.
    The function deliberately records no dispatch attempt: routing is not spawning.
    """

    if not isinstance(request, Mapping) or not isinstance(host_capabilities, Mapping):
        return _result(
            RoutingStatus.REJECTED,
            requested_host=None,
            requested_role=None,
            requested_model=None,
            requested_reasoning=None,
            reason="invalid_request",
        )
    host, raw_role, supplied_model, supplied_reasoning, escalation_reason = (
        _request_values(request)
    )
    if set(request).difference(_REQUEST_FIELDS) or host is None or raw_role is None:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_request",
        )
    if not isinstance(request.get("exceptional", False), bool):
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_request",
        )

    if host != "codex":
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="unknown_host",
        )

    role = _ROLE_ALIASES.get(host, {}).get(raw_role, raw_role)
    raw_complexity = request.get("complexity", "normal")
    complexity = _text(raw_complexity)
    if complexity is None:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_request",
        )
    complexity = _COMPLEXITY_ALIASES.get(complexity, complexity)
    if request["exceptional"] if "exceptional" in request else False:
        complexity = "exceptional"
    if raw_role == "terra_high":
        complexity = "normal"
    if raw_role == "terra_max":
        complexity = "complex"
    if raw_role == "sol_high":
        role = "code"
        complexity = "exceptional"

    policy = HOST_PROFILES if profiles is None else profiles
    if not isinstance(policy, Mapping):
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_policy",
        )
    route, route_error = _profile_route(policy, host, role, complexity)
    if route is None:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason=route_error,
        )
    if route.get("status") == "unavailable":
        return _result(
            RoutingStatus.UNAVAILABLE,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason=str(route.get("reason", "route_unavailable")),
            effective_host=host,
            effective_role=role,
        )

    model = _text(route.get("model"))
    reasoning_options = _route_reasoning_options(route)
    default_reasoning = _text(route.get("default_reasoning"))
    if model is None or reasoning_options is None:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_policy",
        )
    if default_reasoning is None:
        default_reasoning = reasoning_options[0]
    if default_reasoning not in reasoning_options:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="invalid_policy",
        )
    if supplied_reasoning is not None and supplied_reasoning not in reasoning_options:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="requested_route_conflicts_with_policy",
        )
    reasoning = supplied_reasoning or default_reasoning
    if route.get("requires_escalation_reason") is True and escalation_reason is None:
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="explicit_escalation_reason_required",
        )
    if (supplied_model not in (None, model)) or (
        supplied_reasoning not in (None, reasoning)
    ):
        return _result(
            RoutingStatus.REJECTED,
            requested_host=host,
            requested_role=raw_role,
            requested_model=supplied_model,
            requested_reasoning=supplied_reasoning,
            reason="requested_route_conflicts_with_policy",
        )

    report = _host_capability_report(host, host_capabilities)
    if report is None or not _reports_capability(report, model, reasoning):
        return _result(
            RoutingStatus.UNAVAILABLE,
            requested_host=host,
            requested_role=raw_role,
            requested_model=model,
            requested_reasoning=reasoning,
            reason="capability_unavailable",
            effective_host=host,
            effective_role=role,
        )
    return _result(
        RoutingStatus.RESOLVED,
        requested_host=host,
        requested_role=raw_role,
        requested_model=model,
        requested_reasoning=reasoning,
        effective_host=host,
        effective_role=role,
        effective_model=model,
        effective_reasoning=reasoning,
        reason="policy_route_resolved",
    )
