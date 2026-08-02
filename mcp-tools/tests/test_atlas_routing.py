"""Focused contract tests for the offline Atlas host router."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas.routing import HOST_PROFILES, RoutingStatus, resolve_role


def _codex_capabilities() -> dict[str, object]:
    return {
        "host": "codex",
        "models": {
            "gpt-5.6-sol": {"reasoning": ["high"]},
            "gpt-5.6-terra": {"reasoning": ["high", "max"]},
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
    assert result.reason == "capability_unavailable"
    assert (result.requested_model, result.requested_reasoning) == (
        "gpt-5.6-luna",
        "medium",
    )
    assert result.effective_model is None
    assert result.attempts == ()


def test_luna_resolves_only_for_an_attested_exact_capability_pair() -> None:
    result = resolve_role(
        {"host": "codex", "role": "luna"},
        {
            "host": "codex",
            "models": {"gpt-5.6-luna": {"reasoning": ["medium"]}},
        },
    )

    assert result.status is RoutingStatus.RESOLVED
    assert (result.effective_model, result.effective_reasoning) == (
        "gpt-5.6-luna",
        "medium",
    )
    assert result.reason == "policy_route_resolved"
    assert result.attempts == ()


def test_luna_preserves_an_attested_non_default_effort() -> None:
    result = resolve_role(
        {"host": "codex", "role": "luna", "reasoning": "high"},
        {
            "host": "codex",
            "models": {
                "gpt-5.6-luna": {"reasoning": ["low", "medium", "high", "xhigh"]}
            },
        },
    )

    assert result.status is RoutingStatus.RESOLVED
    assert (result.effective_model, result.effective_reasoning) == (
        "gpt-5.6-luna",
        "high",
    )


def test_luna_rejects_an_unattested_effort_without_substitution() -> None:
    result = resolve_role(
        {"host": "codex", "role": "luna", "reasoning": "xhigh"},
        {
            "host": "codex",
            "models": {"gpt-5.6-luna": {"reasoning": ["low", "medium", "high"]}},
        },
    )

    assert result.status is RoutingStatus.UNAVAILABLE
    assert result.reason == "capability_unavailable"
    assert result.effective_model is None
    assert result.effective_reasoning is None
    assert result.attempts == ()


def test_luna_policy_exposes_an_effort_envelope_separate_from_attestation() -> None:
    luna_policy = HOST_PROFILES["hosts"]["codex"]["roles"]["luna"]

    assert luna_policy["reasoning"] == ["low", "medium", "high", "xhigh"]
    assert luna_policy["default_reasoning"] == "medium"


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


def test_router_exposes_only_the_codex_host_profile() -> None:
    assert set(HOST_PROFILES["hosts"]) == {"codex"}
    removed_host = resolve_role(
        {"host": "claude", "role": "coordinator"}, _codex_capabilities()
    )

    assert removed_host.status is RoutingStatus.REJECTED
    assert removed_host.reason == "unknown_host"


def test_custom_policy_cannot_restore_a_removed_host() -> None:
    restored_host = resolve_role(
        {"host": "claude", "role": "coordinator"},
        {
            "host": "claude",
            "models": {"test-model": {"reasoning": "high"}},
        },
        profiles={
            "hosts": {
                "claude": {
                    "roles": {
                        "coordinator": {
                            "model": "test-model",
                            "reasoning": "high",
                        }
                    }
                }
            }
        },
    )

    assert restored_host.status is RoutingStatus.REJECTED
    assert restored_host.reason == "unknown_host"
    assert restored_host.effective_model is None


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
