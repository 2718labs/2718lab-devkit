"""Focused acceptance tests for the pure Fast Lane routing core."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = ROOT / "mcp-tools" / "devkit_fastlane" / "scripts" / "fastlane_routing.py"
POLICY_PATH = (
    ROOT
    / "mcp-tools"
    / "devkit_fastlane"
    / "assets"
    / "fastlane-routing-policy-v3.json"
)
HASH_A = "sha256:" + ("a" * 64)
HASH_B = "sha256:" + ("b" * 64)
HASH_C = "sha256:" + ("c" * 64)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_core() -> Any:
    assert CORE_PATH.exists(), "the v3 pure routing core is absent"
    spec = importlib.util.spec_from_file_location("fastlane_routing", CORE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_routing"] = module
    spec.loader.exec_module(module)
    return module


def _policy_hash() -> str:
    assert POLICY_PATH.exists(), "the v3 routing policy asset is absent"
    return _canonical_hash(json.loads(POLICY_PATH.read_text(encoding="utf-8")))


def _request() -> dict[str, object]:
    dependency_without_hash = {
        "schema": "2718lab-devkit/dependency-state-v1",
        "graph_epoch": 1,
        "direct_dependency_ids": [],
        "completed_dependency_ids": [],
    }
    return {
        "schema": "2718lab-devkit/fastlane-routing-request-v3",
        "policy_hash": _policy_hash(),
        "task": {
            "schema": "2718lab-devkit/task-routing-profile-v3",
            "task_id": "TASK-1",
            "role": "execution",
            "access": "workspace_write",
            "write_scope_count": 0,
            "write_scope_breadth": "none",
            "read_scope_count": 0,
            "read_scope_breadth": "none",
            "overlap_risk": "none",
            "overlap_count": 0,
            "dependency_depth": 0,
            "downstream_critical_count": 0,
            "critical_path": False,
            "criticality": "low",
            "cross_module": False,
            "database_work": False,
            "migration": False,
            "security_sensitive": False,
            "destructive": False,
            "external_boundary": False,
            "architecture_conflict": False,
            "design_ambiguity": False,
            "verification_cost": "none",
            "blocker_severity": "none",
            "authorization": "not_required",
            "authorization_evidence_hash": None,
            "narrow_decoupling_eligible": False,
            "strike": None,
            "gate_matrix_hash": HASH_A,
            "profile_evidence_hash": HASH_B,
        },
        "dependency_state": {
            **dependency_without_hash,
            "dependency_state_hash": _canonical_hash(dependency_without_hash),
        },
        "scope_state": {
            "schema": "2718lab-devkit/scope-state-v1",
            "scope_epoch": 1,
            "owned_scope_hash": HASH_C,
            "conflicting_task_ids": [],
            "active_writer_task_ids": [],
        },
        "scheduler_facts": {
            "event_seq": 1,
            "route_epoch": 1,
            "override_epoch": 0,
            "recovery_epoch": 0,
            "ready_event_seq": 1,
            "dispatch_cause": "task_ready",
            "transport_state": "connected",
            "execution_state": "unknown",
            "lease_state": "unclaimed",
            "evidence_state": "none",
            "lease_epoch": 0,
            "recovery_probe_count_epoch": 0,
            "fence_count_epoch": 0,
            "fenced_replacement_count_task": 0,
        },
        "host_capabilities": {
            "schema": "2718lab-devkit/host-capabilities-v1",
            "host_id_hash": HASH_A,
            "capability_epoch": 1,
            "total_slots": 4,
            "model_slot_limits": {"luna": 4, "terra": 3, "sol": 1, "spark": 1},
            "models": [
                {
                    "model_id": "gpt-5.6-luna",
                    "status": "available",
                    "efforts": ["low", "medium", "high", "xhigh"],
                }
            ],
            "entitlements": [],
        },
        "override_receipt": None,
        "legacy": None,
    }


def _all_capabilities() -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/host-capabilities-v1",
        "host_id_hash": HASH_A,
        "capability_epoch": 1,
        "total_slots": 8,
        "model_slot_limits": {"luna": 4, "terra": 4, "sol": 4, "spark": 1},
        "models": [
            {
                "model_id": "gpt-5.3-codex-spark",
                "status": "available",
                "efforts": ["medium"],
            },
            {
                "model_id": "gpt-5.6-luna",
                "status": "available",
                "efforts": ["low", "medium", "high", "xhigh"],
            },
            {
                "model_id": "gpt-5.6-sol",
                "status": "available",
                "efforts": ["high", "xhigh", "max"],
            },
            {
                "model_id": "gpt-5.6-terra",
                "status": "available",
                "efforts": ["medium", "high", "xhigh", "max"],
            },
        ],
        "entitlements": ["spark_preview"],
    }


def _dependency_hash(request: dict[str, object]) -> None:
    dependency = request["dependency_state"]
    assert isinstance(dependency, dict)
    dependency["dependency_state_hash"] = _canonical_hash(
        {
            key: value
            for key, value in dependency.items()
            if key != "dependency_state_hash"
        }
    )


def _spark_request() -> dict[str, object]:
    request = _request()
    request["host_capabilities"] = _all_capabilities()
    task = request["task"]
    assert isinstance(task, dict)
    task.update(
        {
            "critical_path": True,
            "blocker_severity": "severe",
            "narrow_decoupling_eligible": True,
            "strike": {
                "schema": "2718lab-devkit/spark-strike-proof-v1",
                "kind": "static_acceptance_blocker",
                "feature_green": True,
                "single_bounded_acceptance_gate": False,
                "owned_file_count": 2,
                "failing_command_count": 1,
                "failing_scope_hash": HASH_A,
                "failing_commands_hash": HASH_B,
                "no_live_competing_writer": True,
                "exit_condition_hash": HASH_C,
                "max_changed_files": 4,
                "max_focused_commands": 3,
                "max_strike_minutes": 15,
                "blocker_fingerprint": "sha256:" + ("d" * 64),
                "prior_spark_attempts": 0,
                "evidence_hash": "sha256:" + ("e" * 64),
            },
        }
    )
    return request


def test_structured_luna_capability_routes_without_fixed_legacy_table() -> None:
    """A v3 profile must resolve from facts and an attested exact capability."""

    core = _load_core()

    result = core.route(_request())

    assert result["status"] == "resolved"
    assert result["route"] == {
        "lane": "luna",
        "model": "gpt-5.6-luna",
        "effort": "medium",
    }
    assert "floor_role" in result["reason_codes"]


def test_unknown_profile_field_is_a_stable_rejection_not_a_best_effort_route() -> None:
    core = _load_core()
    request = _request()
    task = request["task"]
    assert isinstance(task, dict)
    task["surprise"] = "not permitted"

    result = core.route(request)

    assert result["status"] == "rejected"
    assert result["reason_codes"] == ["invalid_schema"]
    assert result["route"] is None


def test_permuted_nested_lists_have_a_byte_identical_cached_route_result() -> None:
    core = _load_core()
    cache = core.RouteCache()
    left = _request()
    left["host_capabilities"] = _all_capabilities()
    right = deepcopy(left)
    right_host = right["host_capabilities"]
    assert isinstance(right_host, dict)
    right_host["models"].reverse()
    for model in right_host["models"]:
        model["efforts"].reverse()
    right_host["entitlements"].reverse()

    left_result = core.route(left, cache=cache)
    right_result = core.route(right, cache=cache)

    assert left_result == right_result
    assert cache.size == 1


def test_active_writer_scope_conflict_blocks_before_model_selection() -> None:
    core = _load_core()
    request = _request()
    request["host_capabilities"] = _all_capabilities()
    task = request["task"]
    scope = request["scope_state"]
    assert isinstance(task, dict)
    assert isinstance(scope, dict)
    task["overlap_risk"] = "active"
    task["overlap_count"] = 1
    scope["active_writer_task_ids"] = ["TASK-OTHER"]
    scope["conflicting_task_ids"] = ["TASK-OTHER"]

    result = core.route(request)

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["scope_conflict_active"]
    assert result["route"] is None


def test_bounds_and_json_boolean_confusion_fail_closed() -> None:
    core = _load_core()
    request = _request()
    task = request["task"]
    assert isinstance(task, dict)
    task["write_scope_count"] = True

    result = core.route(request)

    assert result["status"] == "rejected"
    assert result["reason_codes"] == ["invalid_bounds"]


def test_unmet_dependency_and_missing_destructive_authorization_are_hard_gates() -> (
    None
):
    core = _load_core()
    dependency_request = _request()
    dependency = dependency_request["dependency_state"]
    assert isinstance(dependency, dict)
    dependency["direct_dependency_ids"] = ["TASK-0"]
    _dependency_hash(dependency_request)

    dependency_result = core.route(dependency_request)

    assert dependency_result["status"] == "blocked"
    assert dependency_result["reason_codes"] == ["dependency_not_ready"]

    destructive_request = _request()
    destructive_task = destructive_request["task"]
    assert isinstance(destructive_task, dict)
    destructive_task["destructive"] = True
    destructive_task["authorization"] = "missing"

    destructive_result = core.route(destructive_request)

    assert destructive_result["status"] == "blocked"
    assert destructive_result["reason_codes"] == ["destructive_authorization_missing"]


def test_score_and_immutable_floor_are_monotonic_when_risk_increases() -> None:
    core = _load_core()
    base = _request()
    base["host_capabilities"] = _all_capabilities()
    cross_module = deepcopy(base)
    cross_task = cross_module["task"]
    assert isinstance(cross_task, dict)
    cross_task["cross_module"] = True
    security = deepcopy(cross_module)
    security_task = security["task"]
    assert isinstance(security_task, dict)
    security_task["security_sensitive"] = True

    base_result = core.route(base)
    cross_result = core.route(cross_module)
    security_result = core.route(security)

    assert base_result["score"] <= cross_result["score"] <= security_result["score"]
    assert (
        base_result["safety_floor"]["rank"]
        <= cross_result["safety_floor"]["rank"]
        <= security_result["safety_floor"]["rank"]
    )
    assert security_result["route"]["effort"] == "max"


def test_exact_sol_capability_resolves_and_never_falls_back() -> None:
    core = _load_core()
    available = _request()
    available["host_capabilities"] = _all_capabilities()
    task = available["task"]
    assert isinstance(task, dict)
    task.update(
        {
            "role": "design",
            "access": "read_only",
            "write_scope_count": 0,
            "write_scope_breadth": "none",
        }
    )

    resolved = core.route(available)

    assert resolved["route"] == {
        "lane": "sol",
        "model": "gpt-5.6-sol",
        "effort": "high",
    }

    unavailable = deepcopy(available)
    host = unavailable["host_capabilities"]
    assert isinstance(host, dict)
    host["models"] = [
        model for model in host["models"] if model["model_id"] != "gpt-5.6-sol"
    ]

    no_fallback = core.route(unavailable)

    assert no_fallback["status"] == "unavailable"
    assert no_fallback["reason_codes"] == ["capability_unavailable"]


def test_medium_only_luna_host_uses_only_attested_medium_as_a_safe_fallback() -> None:
    core = _load_core()
    request = _request()
    task = request["task"]
    assert isinstance(task, dict)
    task.update(
        {
            "role": "read_analysis",
            "access": "read_only",
            "write_scope_count": 0,
            "write_scope_breadth": "none",
        }
    )
    host = _all_capabilities()
    host["models"] = [
        {
            "model_id": "gpt-5.6-luna",
            "status": "available",
            "efforts": ["medium"],
        }
    ]
    host["entitlements"] = []
    request["host_capabilities"] = host

    result = core.route(request)

    assert result["route"] == {
        "lane": "luna",
        "model": "gpt-5.6-luna",
        "effort": "medium",
    }
    assert result["capability_resolution"] == {
        "state": "capability_fallback",
        "requested": {
            "lane": "luna",
            "model": "gpt-5.6-luna",
            "effort": "low",
        },
        "attestation_reason": "preferred_tuple_unattested",
    }


def test_unattested_lower_effort_cannot_weaken_the_capability_floor() -> None:
    core = _load_core()
    request = _request()
    host = _all_capabilities()
    host["models"] = [
        {
            "model_id": "gpt-5.6-luna",
            "status": "available",
            "efforts": ["low"],
        }
    ]
    host["entitlements"] = []
    request["host_capabilities"] = host

    result = core.route(request)

    assert result["status"] == "unavailable"
    assert result["route"] is None
    assert result["safety_floor"] == {
        "rank": 20,
        "lane": "luna",
        "model": "gpt-5.6-luna",
        "effort": "medium",
    }
    assert result["capability_resolution"] == {
        "state": "capability_unavailable",
        "requested": {
            "lane": "luna",
            "model": "gpt-5.6-luna",
            "effort": "medium",
        },
        "attestation_reason": "no_exact_attested_candidate",
    }


def test_multi_effort_luna_host_selects_different_exact_efforts_as_score_changes() -> (
    None
):
    core = _load_core()
    low = _request()
    low_task = low["task"]
    assert isinstance(low_task, dict)
    low_task.update(
        {
            "role": "read_analysis",
            "access": "read_only",
            "write_scope_count": 0,
            "write_scope_breadth": "none",
        }
    )
    high = _request()
    high_task = high["task"]
    assert isinstance(high_task, dict)
    high_task.update(
        {
            "read_scope_count": 16,
            "read_scope_breadth": "repo_wide",
            "dependency_depth": 8,
        }
    )
    luna_host = _all_capabilities()
    luna_host["models"] = [
        {
            "model_id": "gpt-5.6-luna",
            "status": "available",
            "efforts": ["low", "medium", "high", "xhigh"],
        }
    ]
    luna_host["entitlements"] = []
    low["host_capabilities"] = deepcopy(luna_host)
    high["host_capabilities"] = deepcopy(luna_host)

    low_result = core.route(low)
    high_result = core.route(high)

    assert low_result["route"]["effort"] == "low"
    assert low_result["capability_resolution"]["state"] == "preferred"
    assert high_result["route"]["effort"] == "high"
    assert high_result["capability_resolution"]["state"] == "preferred"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda request: request["task"].__setitem__("blocker_severity", "major"),
            "spark_not_severe",
        ),
        (
            lambda request: request["task"].__setitem__("critical_path", False),
            "spark_not_critical_path",
        ),
        (
            lambda request: request["task"].__setitem__(
                "narrow_decoupling_eligible", False
            ),
            "spark_scope_not_narrow",
        ),
        (
            lambda request: request["task"]["strike"].__setitem__(
                "feature_green", False
            ),
            "spark_candidate_not_green",
        ),
        (
            lambda request: request["task"]["strike"].__setitem__(
                "no_live_competing_writer", False
            ),
            "spark_competing_writer",
        ),
        (
            lambda request: request["task"]["strike"].__setitem__(
                "prior_spark_attempts", 1
            ),
            "spark_prior_attempt",
        ),
        (
            lambda request: request["task"].__setitem__(
                "verification_cost", "full_regression"
            ),
            "spark_long_regression",
        ),
        (
            lambda request: request["task"].__setitem__("migration", True),
            "spark_architecture_or_migration",
        ),
    ],
)
def test_each_critical_spark_predicate_falls_back_to_normal_routing(
    mutate: Any, reason: str
) -> None:
    core = _load_core()
    request = _spark_request()
    mutate(request)

    result = core.route(request)

    assert result["route"] != {
        "lane": "spark",
        "model": "gpt-5.3-codex-spark",
        "effort": "medium",
    }
    assert reason in result["reason_codes"]


def test_spark_requires_the_complete_conjunction_and_exact_entitlement() -> None:
    core = _load_core()

    routed = core.route(_spark_request())

    assert routed["route"] == {
        "lane": "spark",
        "model": "gpt-5.3-codex-spark",
        "effort": "medium",
    }
    assert routed["reason_codes"] == ["spark_static_blocker"]

    without_entitlement = _spark_request()
    host = without_entitlement["host_capabilities"]
    assert isinstance(host, dict)
    host["entitlements"] = []

    fallback = core.route(without_entitlement)

    assert fallback["route"]["model"] != "gpt-5.3-codex-spark"
    assert "spark_entitlement_unavailable" in fallback["reason_codes"]


def _legacy(*, complexity: str | None, route: str | None) -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/legacy-route-input-v1",
        "compatibility_version": 1,
        "complexity": complexity,
        "recommended_route": route,
    }


def test_legacy_adapter_is_conservative_and_hints_cannot_override_v3_facts() -> None:
    core = _load_core()
    legacy = _legacy(complexity="routine", route="Terra High")
    adaptation = core.adapt_legacy_profile(legacy, task_id="TASK-LEGACY")
    request = _request()
    request["task"] = adaptation.profile
    request["legacy"] = legacy
    request["host_capabilities"] = _all_capabilities()

    compatibility_result = core.route(
        request, compatibility_floor=adaptation.floor_rank
    )

    assert compatibility_result["route"] == {
        "lane": "terra",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }
    assert "legacy_profile_conservative" in compatibility_result["reason_codes"]

    structured = _request()
    structured["legacy"] = legacy
    structured["host_capabilities"] = _all_capabilities()
    structured_result = core.route(structured)

    assert structured_result["route"]["lane"] == "luna"
    assert "legacy_hint_ignored" in structured_result["reason_codes"]

    with pytest.raises(core.RoutingError) as exc:
        core.adapt_legacy_profile(
            _legacy(complexity="complex", route="Terra High"),
            task_id="TASK-CONFLICT",
        )
    assert exc.value.code == "legacy_route_conflict"


def test_only_a_trusted_upward_override_can_raise_a_route() -> None:
    core = _load_core()
    request = _request()
    request["host_capabilities"] = _all_capabilities()
    base = core.route(request)
    receipt = {
        "schema": "2718lab-devkit/host-route-override-v1",
        "task_fingerprint": base["task_fingerprint"],
        "policy_hash": request["policy_hash"],
        "lease_epoch": 0,
        "issued_event_seq": 1,
        "expires_event_seq": 2,
        "requested_model": "gpt-5.6-terra",
        "requested_effort": "max",
        "reason": "host_incident",
        "evidence_hash": HASH_B,
        "attester_role": "coordinator",
        "attester_endpoint_hash": HASH_A,
    }
    receipt["receipt_hash"] = _canonical_hash(receipt)
    request["override_receipt"] = receipt

    elevated = core.route(
        request,
        trusted_override_receipt_hashes=[receipt["receipt_hash"]],
        trusted_evidence_hashes=[HASH_B],
        coordinator_endpoint_hash=HASH_A,
    )

    assert elevated["route"] == {
        "lane": "terra",
        "model": "gpt-5.6-terra",
        "effort": "max",
    }
    assert "host_override_upward" in elevated["reason_codes"]


def test_gate_evidence_identity_is_canonical_and_strict_utc_z() -> None:
    core = _load_core()
    identity = {
        "schema": "2718lab-devkit/gate-evidence-identity-v1",
        "task_fingerprint": HASH_A,
        "gate_definition_hash": HASH_B,
        "base_or_integration_commit": "a" * 40,
        "candidate_commit": None,
        "owned_diff_hash": HASH_C,
        "environment_lock_hashes": ["sha256:" + ("e" * 64), "sha256:" + ("d" * 64)],
        "command_argv_hash": "sha256:" + ("f" * 64),
        "command_env_hash": "sha256:" + ("0" * 64),
        "cache_root_id_hash": "sha256:" + ("1" * 64),
        "exit_status": 0,
        "started_at_utc_z": "2026-08-01T00:00:00Z",
        "finished_at_utc_z": "2026-08-01T00:00:01Z",
        "candidate_epoch": 3,
    }
    permuted = deepcopy(identity)
    permuted["environment_lock_hashes"].reverse()

    assert core.gate_evidence_identity(identity) == core.gate_evidence_identity(
        permuted
    )

    invalid = deepcopy(identity)
    invalid["finished_at_utc_z"] = "2026-08-01T00:00:01+00:00"
    with pytest.raises(core.RoutingError) as exc:
        core.gate_evidence_identity(invalid)
    assert exc.value.code == "invalid_bounds"


def test_routing_core_ast_has_no_outbound_model_or_spawn_surface() -> None:
    tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
    forbidden_modules = {
        "subprocess",
        "socket",
        "requests",
        "http",
        "httpx",
        "urllib",
        "mcp",
        "openai",
    }
    forbidden_calls = {"spawn_agent", "create_thread", "update_plan"}
    imports: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module.split(".")[0])
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, ast.Attribute):
            names.add(node.attr)

    assert not (imports & forbidden_modules)
    assert not (names & forbidden_calls)


def test_efficiency_automation_documents_exact_attested_dynamic_fallback() -> None:
    documentation = (
        ROOT
        / "mcp-tools"
        / "devkit_fastlane"
        / "references"
        / "efficiency-automation.md"
    ).read_text(encoding="utf-8")

    assert "exact host-attested model/effort tuple" in documentation
    assert "capability_fallback" in documentation
    assert "capability_unavailable" in documentation
