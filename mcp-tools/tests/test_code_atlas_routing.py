"""Focused contract tests for the offline Code Atlas host router."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.routing import RoutingStatus, resolve_role


def _codex_capabilities() -> dict[str, object]:
    return {
        "host": "codex",
        "models": {
            "gpt-5.6-sol": {"reasoning": ["high"]},
            "gpt-5.6-terra": {"reasoning": ["high", "max"]},
        },
    }


def _claude_capabilities() -> dict[str, object]:
    return {
        "host": "claude",
        "models": {
            "opus": {"reasoning": ["coordinator"]},
            "sonnet": {"reasoning": ["standard"]},
            "haiku": {"reasoning": ["light"]},
            "fable": {"reasoning": ["high"]},
        },
    }


def test_normal_complex_and_exceptional_codex_routes_are_exact() -> None:
    normal = resolve_role(
        {"host": "codex", "role": "code", "complexity": "normal"},
        _codex_capabilities(),
    )
    complex_result = resolve_role(
        {"host": "codex", "role": "code", "complexity": "complex"},
        _codex_capabilities(),
    )
    exceptional = resolve_role(
        {"host": "codex", "role": "code", "exceptional": True},
        _codex_capabilities(),
    )

    assert normal.status is RoutingStatus.RESOLVED
    assert (normal.requested_model, normal.requested_reasoning) == (
        "gpt-5.6-terra",
        "high",
    )
    assert (normal.effective_model, normal.effective_reasoning) == (
        "gpt-5.6-terra",
        "high",
    )
    assert (complex_result.effective_model, complex_result.effective_reasoning) == (
        "gpt-5.6-terra",
        "max",
    )
    assert (exceptional.effective_model, exceptional.effective_reasoning) == (
        "gpt-5.6-sol",
        "high",
    )
    assert normal.attempts == ()


def test_luna_is_explicitly_unavailable_without_a_spawn_attempt() -> None:
    result = resolve_role({"host": "codex", "role": "luna"}, _codex_capabilities())

    assert result.status is RoutingStatus.UNAVAILABLE
    assert result.reason == "luna_unavailable"
    assert result.effective_model is None
    assert result.attempts == ()


def test_router_fails_closed_for_unknown_capability_and_substitution() -> None:
    unavailable = resolve_role(
        {"host": "codex", "role": "code", "complexity": "complex"},
        {
            "host": "codex",
            "models": {"gpt-5.6-terra": {"reasoning": ["high"]}},
        },
    )
    mismatch = resolve_role(
        {
            "host": "codex",
            "role": "code",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
        },
        _codex_capabilities(),
    )
    unknown = resolve_role({"host": "unknown", "role": "code"}, _codex_capabilities())

    assert unavailable.status is RoutingStatus.UNAVAILABLE
    assert unavailable.reason == "capability_unavailable"
    assert mismatch.status is RoutingStatus.REJECTED
    assert mismatch.reason == "requested_route_conflicts_with_policy"
    assert unknown.status is RoutingStatus.REJECTED
    assert unknown.reason == "unknown_host"


def test_empty_or_invalid_custom_profiles_never_fall_back_to_defaults() -> None:
    empty = resolve_role(
        {"host": "codex", "role": "code"},
        _codex_capabilities(),
        profiles={},
    )
    malformed = resolve_role(
        {"host": "codex", "role": "code"},
        _codex_capabilities(),
        profiles={"hosts": []},
    )
    wrong_type = resolve_role(
        {"host": "codex", "role": "code"},
        _codex_capabilities(),
        profiles=[],  # type: ignore[arg-type]
    )

    assert empty.status is RoutingStatus.REJECTED
    assert empty.reason == "invalid_policy"
    assert empty.effective_model is None
    assert malformed.status is RoutingStatus.REJECTED
    assert malformed.reason == "invalid_policy"
    assert wrong_type.status is RoutingStatus.REJECTED
    assert wrong_type.reason == "invalid_policy"


def test_claude_profiles_and_fable_escalation_are_explicit() -> None:
    coordinator = resolve_role(
        {"host": "claude", "role": "coordinator"}, _claude_capabilities()
    )
    code = resolve_role({"host": "claude", "role": "code"}, _claude_capabilities())
    light = resolve_role({"host": "claude", "role": "light"}, _claude_capabilities())
    denied_fable = resolve_role(
        {"host": "claude", "role": "fable"}, _claude_capabilities()
    )
    fable = resolve_role(
        {
            "host": "claude",
            "role": "fable",
            "escalation_reason": "cross-boundary security investigation",
        },
        _claude_capabilities(),
    )

    assert coordinator.effective_model == "opus"
    assert code.effective_model == "sonnet"
    assert light.effective_model == "haiku"
    assert denied_fable.status is RoutingStatus.REJECTED
    assert denied_fable.reason == "explicit_escalation_reason_required"
    assert fable.status is RoutingStatus.RESOLVED
    assert fable.effective_model == "fable"


@pytest.mark.parametrize(
    "route_request",
    (
        {"host": "codex", "role": "unknown"},
        {"host": "codex", "role": "code", "complexity": "invalid"},
        {"host": "codex", "role": "code", "unexpected": True},
    ),
)
def test_bad_requests_fail_closed(route_request: dict[str, object]) -> None:
    result = resolve_role(route_request, _codex_capabilities())

    assert result.status is RoutingStatus.REJECTED
    assert result.effective_model is None
