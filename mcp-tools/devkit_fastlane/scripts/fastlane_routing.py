"""Pure, deterministic Fast Lane v3 routing primitives.

The module deliberately has no model SDK, MCP client, host dispatch, network,
shell, subprocess, browser, or agent-spawn boundary.  Callers provide durable
facts and capability attestations; this module only validates and resolves a
canonical, cacheable route decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

REQUEST_SCHEMA: Final = "2718lab-devkit/fastlane-routing-request-v3"
TASK_SCHEMA: Final = "2718lab-devkit/task-routing-profile-v3"
DEPENDENCY_SCHEMA: Final = "2718lab-devkit/dependency-state-v1"
SCOPE_SCHEMA: Final = "2718lab-devkit/scope-state-v1"
SCHEDULER_SCHEMA: Final = "2718lab-devkit/scheduler-facts-v1"
HOST_SCHEMA: Final = "2718lab-devkit/host-capabilities-v1"
STRIKE_SCHEMA: Final = "2718lab-devkit/spark-strike-proof-v1"
OVERRIDE_SCHEMA: Final = "2718lab-devkit/host-route-override-v1"
LEGACY_SCHEMA: Final = "2718lab-devkit/legacy-route-input-v1"
GATE_EVIDENCE_SCHEMA: Final = "2718lab-devkit/gate-evidence-identity-v1"
POLICY_SCHEMA: Final = "2718lab-devkit/fastlane-routing-policy-v3"
RESULT_SCHEMA: Final = "2718lab-devkit/fastlane-routing-result-v3"
FINGERPRINT_SCHEMA: Final = "2718lab-devkit/task-route-fingerprint-v1"
REQUEST_SCHEMA_V4: Final = "2718lab-devkit/fastlane-routing-request-v4"
TASK_SCHEMA_V4: Final = "2718lab-devkit/task-routing-profile-v4"
POLICY_SCHEMA_V4: Final = "2718lab-devkit/fastlane-routing-policy-v4"
RESULT_SCHEMA_V4: Final = "2718lab-devkit/fastlane-routing-result-v4"
FINGERPRINT_SCHEMA_V4: Final = "2718lab-devkit/task-route-fingerprint-v4"
REQUEST_SCHEMA_V5: Final = "2718lab-devkit/fastlane-routing-request-v5"
TASK_SCHEMA_V5: Final = "2718lab-devkit/task-routing-profile-v5"
POLICY_SCHEMA_V5: Final = "2718lab-devkit/fastlane-routing-policy-v5"
RESULT_SCHEMA_V5: Final = "2718lab-devkit/fastlane-routing-result-v5"
FINGERPRINT_SCHEMA_V5: Final = "2718lab-devkit/task-route-fingerprint-v5"
REQUEST_BINDING_SCHEMA_V5: Final = "2718lab-devkit/child-route-request-binding-v1"
CHILD_ROUTE_ATTESTATION_SCHEMA: Final = (
    "2718lab-devkit/host-child-route-attestation-v1"
)
POLICY_PATH: Final = (
    Path(__file__).resolve().parents[1] / "assets" / "fastlane-routing-policy-v3.json"
)
POLICY_PATH_V4: Final = (
    Path(__file__).resolve().parents[1] / "assets" / "fastlane-routing-policy-v4.json"
)
POLICY_PATH_V5: Final = (
    Path(__file__).resolve().parents[1] / "assets" / "fastlane-routing-policy-v5.json"
)

MAX_31: Final = (2**31) - 1
MAX_63: Final = (2**63) - 1
SHA256_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
TASK_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
GIT_ID_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
UTC_Z_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")

ROLES: Final = frozenset(
    {
        "prewarm",
        "read_analysis",
        "documentation",
        "execution",
        "recovery",
        "verification",
        "review",
        "integration",
        "design",
        "acceptance",
    }
)
READ_ONLY_ROLES: Final = frozenset(
    {"prewarm", "read_analysis", "documentation", "design", "review", "acceptance"}
)
ACCESS_VALUES: Final = frozenset({"read_only", "workspace_write"})
BREADTH_VALUES: Final = frozenset(
    {"none", "single_file", "single_module", "multi_module", "repo_wide"}
)
OVERLAP_VALUES: Final = frozenset({"none", "potential", "active"})
CRITICALITY_VALUES: Final = frozenset({"low", "normal", "high", "critical"})
VERIFICATION_VALUES: Final = frozenset(
    {"none", "focused", "multi_gate", "full_regression", "long_regression"}
)
BLOCKER_VALUES: Final = frozenset({"none", "minor", "major", "severe"})
AUTHORIZATION_VALUES: Final = frozenset(
    {"not_required", "approved", "missing", "revoked"}
)
DISPATCH_CAUSES: Final = frozenset(
    {
        "task_ready",
        "dependency_ready",
        "recovery",
        "validation",
        "review",
        "cache_pressure",
        "capability_change",
        "default_refill",
    }
)
TRANSPORT_STATES: Final = frozenset({"connected", "degraded", "disconnected"})
EXECUTION_STATES: Final = frozenset(
    {"unknown", "starting", "active", "exited_success", "exited_failure"}
)
LEASE_STATES: Final = frozenset(
    {"unclaimed", "active", "renewable", "expired", "invalid", "released"}
)
EVIDENCE_STATES: Final = frozenset(
    {"none", "checkpoint", "dirty_owned", "commit", "test_result", "verified"}
)
HOST_STATUSES: Final = frozenset({"available", "temporarily_unavailable"})
ENTITLEMENTS: Final = frozenset({"spark_preview"})
LEGACY_COMPLEXITY_RANKS: Final = {
    "routine": 60,
    "moderate": 80,
    "complex": 80,
    "exceptional": 90,
}
LEGACY_ROUTE_RANKS: Final = {"Terra High": 60, "Terra Max": 80, "Sol High": 90}

REQUEST_FIELDS: Final = frozenset(
    {
        "schema",
        "policy_hash",
        "task",
        "dependency_state",
        "scope_state",
        "scheduler_facts",
        "host_capabilities",
        "override_receipt",
        "legacy",
    }
)
TASK_FIELDS: Final = frozenset(
    {
        "schema",
        "task_id",
        "role",
        "access",
        "write_scope_count",
        "write_scope_breadth",
        "read_scope_count",
        "read_scope_breadth",
        "overlap_risk",
        "overlap_count",
        "dependency_depth",
        "downstream_critical_count",
        "critical_path",
        "criticality",
        "cross_module",
        "database_work",
        "migration",
        "security_sensitive",
        "destructive",
        "external_boundary",
        "architecture_conflict",
        "design_ambiguity",
        "verification_cost",
        "blocker_severity",
        "authorization",
        "authorization_evidence_hash",
        "narrow_decoupling_eligible",
        "strike",
        "gate_matrix_hash",
        "profile_evidence_hash",
    }
)
DEPENDENCY_FIELDS: Final = frozenset(
    {
        "schema",
        "graph_epoch",
        "direct_dependency_ids",
        "completed_dependency_ids",
        "dependency_state_hash",
    }
)
SCOPE_FIELDS: Final = frozenset(
    {
        "schema",
        "scope_epoch",
        "owned_scope_hash",
        "conflicting_task_ids",
        "active_writer_task_ids",
    }
)
SCHEDULER_FIELDS: Final = frozenset(
    {
        "event_seq",
        "route_epoch",
        "override_epoch",
        "recovery_epoch",
        "ready_event_seq",
        "dispatch_cause",
        "transport_state",
        "execution_state",
        "lease_state",
        "evidence_state",
        "lease_epoch",
        "recovery_probe_count_epoch",
        "fence_count_epoch",
        "fenced_replacement_count_task",
    }
)
HOST_FIELDS: Final = frozenset(
    {
        "schema",
        "host_id_hash",
        "capability_epoch",
        "total_slots",
        "model_slot_limits",
        "models",
        "entitlements",
    }
)
HOST_MODEL_FIELDS: Final = frozenset({"model_id", "status", "efforts"})
REQUEST_FIELDS_V5: Final = frozenset(
    {
        "schema",
        "policy_hash",
        "task",
        "dependency_state",
        "scope_state",
        "scheduler_facts",
        "host_capabilities",
        "child_route_attestation",
        "legacy",
    }
)
CHILD_ROUTE_ATTESTATION_FIELDS: Final = frozenset(
    {
        "schema",
        "status",
        "request_binding_hash",
        "host_id_hash",
        "capability_epoch",
        "lease_epoch",
        "issued_event_seq",
        "expires_event_seq",
        "route",
        "inherit_current_session_model",
        "refusal_code",
        "attestation_hash",
    }
)
CHILD_ROUTE_FIELDS: Final = frozenset({"lane", "model", "effort", "rank"})
CHILD_ROUTE_LANES: Final = frozenset({"luna", "terra", "sol", "spark"})
STRIKE_FIELDS: Final = frozenset(
    {
        "schema",
        "kind",
        "feature_green",
        "single_bounded_acceptance_gate",
        "owned_file_count",
        "failing_command_count",
        "failing_scope_hash",
        "failing_commands_hash",
        "no_live_competing_writer",
        "exit_condition_hash",
        "max_changed_files",
        "max_focused_commands",
        "max_strike_minutes",
        "blocker_fingerprint",
        "prior_spark_attempts",
        "evidence_hash",
    }
)
OVERRIDE_FIELDS: Final = frozenset(
    {
        "schema",
        "task_fingerprint",
        "policy_hash",
        "lease_epoch",
        "issued_event_seq",
        "expires_event_seq",
        "requested_model",
        "requested_effort",
        "reason",
        "evidence_hash",
        "attester_role",
        "attester_endpoint_hash",
        "receipt_hash",
    }
)
LEGACY_FIELDS: Final = frozenset(
    {"schema", "compatibility_version", "complexity", "recommended_route"}
)
GATE_EVIDENCE_FIELDS: Final = frozenset(
    {
        "schema",
        "task_fingerprint",
        "gate_definition_hash",
        "base_or_integration_commit",
        "candidate_commit",
        "owned_diff_hash",
        "environment_lock_hashes",
        "command_argv_hash",
        "command_env_hash",
        "cache_root_id_hash",
        "exit_status",
        "started_at_utc_z",
        "finished_at_utc_z",
        "candidate_epoch",
    }
)


class RoutingError(ValueError):
    """Fail-closed error carrying exactly one public stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LegacyAdaptation:
    """A conservative profile plus the legacy compatibility floor it requires."""

    profile: dict[str, Any]
    floor_rank: int


@dataclass
class RouteCache:
    """A deterministic, bounded result cache with no dispatch side effects."""

    maximum: int = 128
    _entries: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self, task_id: str, fingerprint: str) -> dict[str, Any] | None:
        entry = self._entries.get((task_id, fingerprint))
        if entry is None:
            return None
        return json.loads(_canonical_json(entry))

    def put(self, result: Mapping[str, Any]) -> None:
        task_id = _safe_text(result.get("task_id"))
        fingerprint = _safe_text(result.get("task_fingerprint"))
        self._entries[(task_id, fingerprint)] = json.loads(_canonical_json(result))
        if len(self._entries) > self.maximum:
            oldest = min(
                self._entries,
                key=lambda key: (
                    int(self._entries[key].get("_computed_event_seq", 0)),
                    key[0],
                    key[1],
                ),
            )
            del self._entries[oldest]


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise RoutingError("invalid_schema", "value is not canonical JSON") from error


def _hash_json(value: object) -> str:
    return (
        "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    )


def _safe_hash(value: object) -> str:
    try:
        return _hash_json(value)
    except RoutingError:
        return _hash_json({"invalid_value_type": type(value).__name__})


def _safe_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RoutingError("invalid_schema", f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        raise RoutingError("invalid_schema", f"{field} has invalid keys")


def _string(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RoutingError("invalid_bounds", f"{field} is not a bounded string")
    return value


def _enum(value: object, values: frozenset[str], field: str) -> str:
    text = _string(value, field)
    if text not in values:
        raise RoutingError("invalid_schema", f"{field} has an unknown value")
    return text


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RoutingError("invalid_bounds", f"{field} is outside its integer bound")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise RoutingError("invalid_bounds", f"{field} must be a JSON boolean")
    return value


def _hash(value: object, field: str) -> str:
    text = _string(value, field, maximum=71)
    if not SHA256_RE.fullmatch(text):
        raise RoutingError("invalid_bounds", f"{field} is not a canonical SHA-256")
    return text


def _optional_hash(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _hash(value, field)


def _task_id(value: object, field: str) -> str:
    text = _string(value, field, maximum=96)
    if not TASK_ID_RE.fullmatch(text):
        raise RoutingError("invalid_bounds", f"{field} is not a safe task id")
    return text


def _normalise_list(
    value: object,
    field: str,
    item: Any,
    maximum: int,
) -> list[Any]:
    if type(value) is not list or len(value) > maximum:
        raise RoutingError("invalid_bounds", f"{field} is not a bounded list")
    normalised = [
        item(member, f"{field}[{index}]") for index, member in enumerate(value)
    ]
    keys = [_canonical_json(member) for member in normalised]
    if len(keys) != len(set(keys)):
        raise RoutingError("invalid_bounds", f"{field} contains duplicates")
    return [
        value
        for _, value in sorted(
            zip(keys, normalised, strict=True), key=lambda item: item[0]
        )
    ]


def _normalise_task_ids(value: object, field: str, maximum: int) -> list[str]:
    return _normalise_list(value, field, _task_id, maximum)


def _normalise_hashes(value: object, field: str, maximum: int) -> list[str]:
    return _normalise_list(value, field, _hash, maximum)


def _policy_mapping(value: object, field: str) -> Mapping[str, Any]:
    return _mapping(value, field)


def _validate_policy(policy: object) -> dict[str, Any]:
    value = _policy_mapping(policy, "policy")
    expected = frozenset(
        {
            "schema",
            "version",
            "registry",
            "spark",
            "score_bands",
            "score",
            "role_floors",
            "risk_floors",
            "limits",
            "reason_codes",
        }
    )
    _exact_keys(value, expected, "policy")
    if value.get("schema") != POLICY_SCHEMA or value.get("version") != 3:
        raise RoutingError("invalid_schema", "policy schema/version is invalid")

    registry = value["registry"]
    if type(registry) is not list or len(registry) != 11:
        raise RoutingError("invalid_bounds", "policy registry size is invalid")
    expected_ranks = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110]
    normalised_registry: list[dict[str, Any]] = []
    for index, raw_pair in enumerate(registry):
        pair = _mapping(raw_pair, f"policy.registry[{index}]")
        _exact_keys(pair, frozenset({"rank", "lane", "model", "effort"}), "policy pair")
        rank = _integer(pair["rank"], "policy pair rank", 1, 110)
        if rank != expected_ranks[index]:
            raise RoutingError("invalid_schema", "policy registry ranks are invalid")
        lane = _enum(pair["lane"], frozenset({"luna", "terra", "sol"}), "policy lane")
        model = _string(pair["model"], "policy model", maximum=64)
        effort = _string(pair["effort"], "policy effort", maximum=16)
        expected_prefix = f"gpt-5.6-{lane}"
        if model != expected_prefix:
            raise RoutingError("invalid_schema", "policy model lane is invalid")
        normalised_registry.append(
            {"rank": rank, "lane": lane, "model": model, "effort": effort}
        )

    spark = _mapping(value["spark"], "policy spark")
    _exact_keys(
        spark,
        frozenset({"lane", "model", "effort", "required_entitlement"}),
        "policy spark",
    )
    if spark != {
        "lane": "spark",
        "model": "gpt-5.3-codex-spark",
        "effort": "medium",
        "required_entitlement": "spark_preview",
    }:
        raise RoutingError("invalid_schema", "policy spark pair is invalid")

    bands = value["score_bands"]
    if type(bands) is not list or len(bands) != 8:
        raise RoutingError("invalid_bounds", "policy score bands are invalid")
    normalised_bands: list[dict[str, Any]] = []
    expected_minimum = 0
    for index, raw_band in enumerate(bands):
        band = _mapping(raw_band, f"policy score_bands[{index}]")
        _exact_keys(
            band, frozenset({"minimum", "maximum", "rank", "reason"}), "policy band"
        )
        minimum = _integer(band["minimum"], "policy band minimum", 0, 100)
        maximum = _integer(band["maximum"], "policy band maximum", 0, 100)
        rank = _integer(band["rank"], "policy band rank", 10, 80)
        reason = _string(band["reason"], "policy band reason", maximum=64)
        if (
            minimum != expected_minimum
            or maximum < minimum
            or rank != expected_ranks[index]
        ):
            raise RoutingError("invalid_schema", "policy score band order is invalid")
        expected_minimum = maximum + 1
        normalised_bands.append(
            {"minimum": minimum, "maximum": maximum, "rank": rank, "reason": reason}
        )
    if expected_minimum != 101:
        raise RoutingError("invalid_schema", "policy score bands do not cover 0..100")

    score = _mapping(value["score"], "policy score")
    score_expected = frozenset(
        {
            "role",
            "write_scope_breadth",
            "read_scope_breadth",
            "overlap_risk",
            "criticality",
            "verification_cost",
            "blocker_severity",
        }
    )
    _exact_keys(score, score_expected, "policy score")
    for score_field, expected_values in (
        ("role", ROLES),
        ("write_scope_breadth", BREADTH_VALUES),
        ("read_scope_breadth", BREADTH_VALUES),
        ("overlap_risk", OVERLAP_VALUES),
        ("criticality", CRITICALITY_VALUES),
        ("verification_cost", VERIFICATION_VALUES),
        ("blocker_severity", BLOCKER_VALUES),
    ):
        points = _mapping(score[score_field], f"policy score {score_field}")
        if set(points) != expected_values:
            raise RoutingError(
                "invalid_schema", f"policy score {score_field} is incomplete"
            )
        for key, raw_points in points.items():
            _integer(raw_points, f"policy score {score_field}.{key}", 0, 100)

    role_floors = _mapping(value["role_floors"], "policy role floors")
    if set(role_floors) != ROLES:
        raise RoutingError("invalid_schema", "policy role floors are incomplete")
    for role, rank in role_floors.items():
        _integer(rank, f"policy role floor {role}", 10, 110)

    risk_floors = _mapping(value["risk_floors"], "policy risk floors")
    expected_risks = frozenset(
        {
            "cross_module",
            "database_work",
            "migration",
            "security_execution",
            "security_review",
            "destructive_execution",
            "destructive_review",
            "destructive_acceptance",
            "design_conflict",
            "acceptance",
        }
    )
    _exact_keys(risk_floors, expected_risks, "policy risk floors")
    for risk, rank in risk_floors.items():
        _integer(rank, f"policy risk floor {risk}", 10, 110)

    limits = _mapping(value["limits"], "policy limits")
    expected_limits = frozenset(
        {
            "maximum_request_bytes",
            "maximum_tasks",
            "maximum_cache_entries",
            "maximum_gate_reason_codes",
            "maximum_host_models",
            "maximum_total_slots",
            "maximum_scope_items",
            "maximum_dependency_items",
        }
    )
    _exact_keys(limits, expected_limits, "policy limits")
    expected_limit_values = {
        "maximum_request_bytes": 32768,
        "maximum_tasks": 64,
        "maximum_cache_entries": 128,
        "maximum_gate_reason_codes": 16,
        "maximum_host_models": 4,
        "maximum_total_slots": 8,
        "maximum_scope_items": 8,
        "maximum_dependency_items": 32,
    }
    if dict(limits) != expected_limit_values:
        raise RoutingError("invalid_schema", "policy limits are invalid")

    reason_codes = _normalise_list(
        value["reason_codes"], "policy reason_codes", _string, 128
    )
    if len(reason_codes) != len(value["reason_codes"]):
        raise RoutingError("invalid_bounds", "policy reason codes are duplicated")
    required_reasons = {
        "invalid_schema",
        "invalid_bounds",
        "invalid_policy_hash",
        "unknown_model_effort",
        "contradictory_profile",
        "access_scope_contradiction",
        "legacy_route_conflict",
        "legacy_profile_incomplete",
        "dependency_not_ready",
        "scope_conflict_active",
        "destructive_authorization_missing",
        "capability_unavailable",
        "spark_static_blocker",
        "spark_entitlement_unavailable",
    }
    if not required_reasons.issubset(reason_codes):
        raise RoutingError("invalid_schema", "policy reason registry is incomplete")

    return dict(value)


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the closed policy asset without making any availability claim."""

    policy_path = path or POLICY_PATH
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoutingError("invalid_schema", "policy asset cannot be parsed") from error
    return _validate_policy(raw)


def policy_hash(policy: Mapping[str, Any]) -> str:
    """Return the contract hash of the unmodified canonical policy object."""

    _validate_policy(policy)
    return _hash_json(policy)


def _v4_as_v3_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    converted = {
        key: value for key, value in policy.items() if key != "spark_alternate"
    }
    converted["schema"] = POLICY_SCHEMA
    converted["version"] = 3
    return converted


def _validate_policy_v4(policy: object) -> dict[str, Any]:
    value = _policy_mapping(policy, "policy")
    expected = frozenset(
        {
            "schema",
            "version",
            "registry",
            "spark",
            "spark_alternate",
            "score_bands",
            "score",
            "role_floors",
            "risk_floors",
            "limits",
            "reason_codes",
        }
    )
    _exact_keys(value, expected, "policy")
    if value.get("schema") != POLICY_SCHEMA_V4 or value.get("version") != 4:
        raise RoutingError("invalid_schema", "policy schema/version is invalid")
    _validate_policy(_v4_as_v3_policy(value))

    alternate = _mapping(value["spark_alternate"], "policy spark alternate")
    _exact_keys(
        alternate,
        frozenset(
            {
                "lane",
                "model",
                "required_entitlement",
                "maximum_write_scope_count",
                "required_write_scope_breadth",
                "allowed_verification_costs",
                "required_local_slot_count",
                "allowed_efforts",
                "effort_bands",
            }
        ),
        "policy spark alternate",
    )
    if (
        alternate["lane"] != "spark"
        or alternate["model"] != "gpt-5.3-codex-spark"
        or alternate["required_entitlement"] != "spark_preview"
        or alternate["maximum_write_scope_count"] != 1
        or alternate["required_write_scope_breadth"] != "single_file"
        or alternate["required_local_slot_count"] != 1
    ):
        raise RoutingError("invalid_schema", "policy spark alternate is invalid")
    verification_costs = _normalise_list(
        alternate["allowed_verification_costs"],
        "policy spark alternate verification costs",
        _string,
        2,
    )
    if set(verification_costs) != {"none", "focused"}:
        raise RoutingError(
            "invalid_schema", "policy spark alternate verification costs are invalid"
        )
    allowed_efforts = _normalise_list(
        alternate["allowed_efforts"],
        "policy spark alternate efforts",
        _string,
        4,
    )
    if set(allowed_efforts) != {"low", "medium", "high", "xhigh"}:
        raise RoutingError("invalid_schema", "policy spark alternate efforts are invalid")
    raw_bands = alternate["effort_bands"]
    if type(raw_bands) is not list or len(raw_bands) != 4:
        raise RoutingError("invalid_bounds", "policy spark alternate effort bands are invalid")
    expected_minimum = 0
    for index, raw_band in enumerate(raw_bands):
        band = _mapping(raw_band, f"policy spark alternate effort band[{index}]")
        _exact_keys(
            band,
            frozenset({"minimum", "maximum", "effort"}),
            "policy spark alternate effort band",
        )
        minimum = _integer(
            band["minimum"], "policy spark alternate band minimum", 0, 80
        )
        maximum = _integer(
            band["maximum"], "policy spark alternate band maximum", 0, 80
        )
        effort = _enum(
            band["effort"], frozenset(allowed_efforts), "policy spark alternate band"
        )
        if minimum != expected_minimum or maximum < minimum:
            raise RoutingError(
                "invalid_schema", "policy spark alternate effort band order is invalid"
            )
        if effort != ("low", "medium", "high", "xhigh")[index]:
            raise RoutingError(
                "invalid_schema", "policy spark alternate effort progression is invalid"
            )
        expected_minimum = maximum + 1
    if expected_minimum != 81:
        raise RoutingError(
            "invalid_schema", "policy spark alternate effort bands do not cover 0..80"
        )
    required_reasons = {
        "spark_alternate_eligible",
        "spark_alternate_capability_unavailable",
        "spark_alternate_slot_unavailable",
        "spark_alternate_scope_not_bounded",
        "spark_alternate_verification_unbounded",
        "spark_alternate_high_risk",
        "spark_alternate_sol_floor",
        "spark_alternate_baseline_unavailable",
    }
    if not required_reasons.issubset(value["reason_codes"]):
        raise RoutingError(
            "invalid_schema", "policy spark alternate reason registry is incomplete"
        )
    return dict(value)


def load_policy_v4(path: Path | None = None) -> dict[str, Any]:
    """Load the closed v4 alternate-routing policy without dispatching work."""

    policy_path = path or POLICY_PATH_V4
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoutingError("invalid_schema", "policy asset cannot be parsed") from error
    return _validate_policy_v4(raw)


def policy_hash_v4(policy: Mapping[str, Any]) -> str:
    """Return the v4 alternate-policy hash after strict validation."""

    _validate_policy_v4(policy)
    return _hash_json(policy)


def _pair_by_rank(policy: Mapping[str, Any], rank: int) -> dict[str, Any]:
    for raw_pair in policy["registry"]:
        pair = _mapping(raw_pair, "policy pair")
        if pair["rank"] == rank:
            return dict(pair)
    raise RoutingError("unknown_model_effort", "policy does not contain rank")


def _pair_by_model_effort(
    policy: Mapping[str, Any], model: str, effort: str
) -> dict[str, Any] | None:
    for raw_pair in policy["registry"]:
        pair = _mapping(raw_pair, "policy pair")
        if pair["model"] == model and pair["effort"] == effort:
            return dict(pair)
    spark = _mapping(policy["spark"], "policy spark")
    if spark["model"] == model and spark["effort"] == effort:
        return {"rank": None, "lane": spark["lane"], "model": model, "effort": effort}
    return None


def _normalise_task(value: object) -> dict[str, Any]:
    task = _mapping(value, "task")
    _exact_keys(task, TASK_FIELDS, "task")
    if task["schema"] != TASK_SCHEMA:
        raise RoutingError("invalid_schema", "task schema is invalid")
    role = _enum(task["role"], ROLES, "task.role")
    access = _enum(task["access"], ACCESS_VALUES, "task.access")
    write_count = _integer(task["write_scope_count"], "task.write_scope_count", 0, 32)
    write_breadth = _enum(
        task["write_scope_breadth"], BREADTH_VALUES, "task.write_scope_breadth"
    )
    read_count = _integer(task["read_scope_count"], "task.read_scope_count", 0, 64)
    read_breadth = _enum(
        task["read_scope_breadth"], BREADTH_VALUES, "task.read_scope_breadth"
    )
    if (write_count == 0) != (write_breadth == "none"):
        raise RoutingError(
            "contradictory_profile", "write scope count/breadth disagree"
        )
    if role in READ_ONLY_ROLES and access != "read_only":
        raise RoutingError(
            "access_scope_contradiction", "read-only role has write access"
        )
    if role in {"prewarm", "read_analysis"} and write_count != 0:
        raise RoutingError("access_scope_contradiction", "read role has write scope")

    destructive = _boolean(task["destructive"], "task.destructive")
    authorization = _enum(
        task["authorization"], AUTHORIZATION_VALUES, "task.authorization"
    )
    authorization_hash = _optional_hash(
        task["authorization_evidence_hash"], "task.authorization_evidence_hash"
    )
    if not destructive and (
        authorization != "not_required" or authorization_hash is not None
    ):
        raise RoutingError(
            "contradictory_profile", "non-destructive task has authorization"
        )

    strike_value = task["strike"]
    strike = None if strike_value is None else _normalise_strike(strike_value)
    return {
        "schema": TASK_SCHEMA,
        "task_id": _task_id(task["task_id"], "task.task_id"),
        "role": role,
        "access": access,
        "write_scope_count": write_count,
        "write_scope_breadth": write_breadth,
        "read_scope_count": read_count,
        "read_scope_breadth": read_breadth,
        "overlap_risk": _enum(
            task["overlap_risk"], OVERLAP_VALUES, "task.overlap_risk"
        ),
        "overlap_count": _integer(task["overlap_count"], "task.overlap_count", 0, 8),
        "dependency_depth": _integer(
            task["dependency_depth"], "task.dependency_depth", 0, 16
        ),
        "downstream_critical_count": _integer(
            task["downstream_critical_count"], "task.downstream_critical_count", 0, 32
        ),
        "critical_path": _boolean(task["critical_path"], "task.critical_path"),
        "criticality": _enum(
            task["criticality"], CRITICALITY_VALUES, "task.criticality"
        ),
        "cross_module": _boolean(task["cross_module"], "task.cross_module"),
        "database_work": _boolean(task["database_work"], "task.database_work"),
        "migration": _boolean(task["migration"], "task.migration"),
        "security_sensitive": _boolean(
            task["security_sensitive"], "task.security_sensitive"
        ),
        "destructive": destructive,
        "external_boundary": _boolean(
            task["external_boundary"], "task.external_boundary"
        ),
        "architecture_conflict": _boolean(
            task["architecture_conflict"], "task.architecture_conflict"
        ),
        "design_ambiguity": _boolean(task["design_ambiguity"], "task.design_ambiguity"),
        "verification_cost": _enum(
            task["verification_cost"], VERIFICATION_VALUES, "task.verification_cost"
        ),
        "blocker_severity": _enum(
            task["blocker_severity"], BLOCKER_VALUES, "task.blocker_severity"
        ),
        "authorization": authorization,
        "authorization_evidence_hash": authorization_hash,
        "narrow_decoupling_eligible": _boolean(
            task["narrow_decoupling_eligible"], "task.narrow_decoupling_eligible"
        ),
        "strike": strike,
        "gate_matrix_hash": _hash(task["gate_matrix_hash"], "task.gate_matrix_hash"),
        "profile_evidence_hash": _hash(
            task["profile_evidence_hash"], "task.profile_evidence_hash"
        ),
    }


def _normalise_strike(value: object) -> dict[str, Any]:
    strike = _mapping(value, "task.strike")
    _exact_keys(strike, STRIKE_FIELDS, "task.strike")
    if strike["schema"] != STRIKE_SCHEMA:
        raise RoutingError("invalid_schema", "Spark proof schema is invalid")
    return {
        "schema": STRIKE_SCHEMA,
        "kind": _string(strike["kind"], "task.strike.kind", maximum=64),
        "feature_green": _boolean(strike["feature_green"], "task.strike.feature_green"),
        "single_bounded_acceptance_gate": _boolean(
            strike["single_bounded_acceptance_gate"],
            "task.strike.single_bounded_acceptance_gate",
        ),
        "owned_file_count": _integer(
            strike["owned_file_count"], "task.strike.owned_file_count", 1, 4
        ),
        "failing_command_count": _integer(
            strike["failing_command_count"], "task.strike.failing_command_count", 1, 3
        ),
        "failing_scope_hash": _hash(
            strike["failing_scope_hash"], "task.strike.failing_scope_hash"
        ),
        "failing_commands_hash": _hash(
            strike["failing_commands_hash"], "task.strike.failing_commands_hash"
        ),
        "no_live_competing_writer": _boolean(
            strike["no_live_competing_writer"], "task.strike.no_live_competing_writer"
        ),
        "exit_condition_hash": _hash(
            strike["exit_condition_hash"], "task.strike.exit_condition_hash"
        ),
        "max_changed_files": _integer(
            strike["max_changed_files"], "task.strike.max_changed_files", 1, 4
        ),
        "max_focused_commands": _integer(
            strike["max_focused_commands"], "task.strike.max_focused_commands", 1, 3
        ),
        "max_strike_minutes": _integer(
            strike["max_strike_minutes"], "task.strike.max_strike_minutes", 1, 15
        ),
        "blocker_fingerprint": _hash(
            strike["blocker_fingerprint"], "task.strike.blocker_fingerprint"
        ),
        "prior_spark_attempts": _integer(
            strike["prior_spark_attempts"], "task.strike.prior_spark_attempts", 0, 3
        ),
        "evidence_hash": _hash(strike["evidence_hash"], "task.strike.evidence_hash"),
    }


def _normalise_dependency(value: object, maximum_items: int) -> dict[str, Any]:
    state = _mapping(value, "dependency_state")
    _exact_keys(state, DEPENDENCY_FIELDS, "dependency_state")
    if state["schema"] != DEPENDENCY_SCHEMA:
        raise RoutingError("invalid_schema", "dependency schema is invalid")
    normalised = {
        "schema": DEPENDENCY_SCHEMA,
        "graph_epoch": _integer(
            state["graph_epoch"], "dependency.graph_epoch", 0, MAX_31
        ),
        "direct_dependency_ids": _normalise_task_ids(
            state["direct_dependency_ids"],
            "dependency.direct_dependency_ids",
            maximum_items,
        ),
        "completed_dependency_ids": _normalise_task_ids(
            state["completed_dependency_ids"],
            "dependency.completed_dependency_ids",
            maximum_items,
        ),
    }
    supplied_hash = _hash(
        state["dependency_state_hash"], "dependency.dependency_state_hash"
    )
    if supplied_hash != _hash_json(normalised):
        raise RoutingError(
            "contradictory_profile", "dependency hash does not bind state"
        )
    normalised["dependency_state_hash"] = supplied_hash
    return normalised


def _normalise_scope(value: object, maximum_items: int) -> dict[str, Any]:
    state = _mapping(value, "scope_state")
    _exact_keys(state, SCOPE_FIELDS, "scope_state")
    if state["schema"] != SCOPE_SCHEMA:
        raise RoutingError("invalid_schema", "scope schema is invalid")
    return {
        "schema": SCOPE_SCHEMA,
        "scope_epoch": _integer(state["scope_epoch"], "scope.scope_epoch", 0, MAX_31),
        "owned_scope_hash": _hash(state["owned_scope_hash"], "scope.owned_scope_hash"),
        "conflicting_task_ids": _normalise_task_ids(
            state["conflicting_task_ids"], "scope.conflicting_task_ids", maximum_items
        ),
        "active_writer_task_ids": _normalise_task_ids(
            state["active_writer_task_ids"],
            "scope.active_writer_task_ids",
            maximum_items,
        ),
    }


def _normalise_scheduler(value: object) -> dict[str, Any]:
    facts = _mapping(value, "scheduler_facts")
    _exact_keys(facts, SCHEDULER_FIELDS, "scheduler_facts")
    event_seq = _integer(facts["event_seq"], "scheduler.event_seq", 1, MAX_63)
    ready_event_seq = _integer(
        facts["ready_event_seq"], "scheduler.ready_event_seq", 0, event_seq
    )
    return {
        "event_seq": event_seq,
        "route_epoch": _integer(
            facts["route_epoch"], "scheduler.route_epoch", 1, MAX_31
        ),
        "override_epoch": _integer(
            facts["override_epoch"], "scheduler.override_epoch", 0, MAX_31
        ),
        "recovery_epoch": _integer(
            facts["recovery_epoch"], "scheduler.recovery_epoch", 0, MAX_31
        ),
        "ready_event_seq": ready_event_seq,
        "dispatch_cause": _enum(
            facts["dispatch_cause"], DISPATCH_CAUSES, "scheduler.dispatch_cause"
        ),
        "transport_state": _enum(
            facts["transport_state"], TRANSPORT_STATES, "scheduler.transport_state"
        ),
        "execution_state": _enum(
            facts["execution_state"], EXECUTION_STATES, "scheduler.execution_state"
        ),
        "lease_state": _enum(
            facts["lease_state"], LEASE_STATES, "scheduler.lease_state"
        ),
        "evidence_state": _enum(
            facts["evidence_state"], EVIDENCE_STATES, "scheduler.evidence_state"
        ),
        "lease_epoch": _integer(
            facts["lease_epoch"], "scheduler.lease_epoch", 0, MAX_31
        ),
        "recovery_probe_count_epoch": _integer(
            facts["recovery_probe_count_epoch"],
            "scheduler.recovery_probe_count_epoch",
            0,
            1,
        ),
        "fence_count_epoch": _integer(
            facts["fence_count_epoch"], "scheduler.fence_count_epoch", 0, 1
        ),
        "fenced_replacement_count_task": _integer(
            facts["fenced_replacement_count_task"],
            "scheduler.fenced_replacement_count_task",
            0,
            3,
        ),
    }


def _normalise_host(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    host = _mapping(value, "host_capabilities")
    _exact_keys(host, HOST_FIELDS, "host_capabilities")
    if host["schema"] != HOST_SCHEMA:
        raise RoutingError("invalid_schema", "host schema is invalid")
    total_slots = _integer(host["total_slots"], "host.total_slots", 1, 8)
    raw_limits = _mapping(host["model_slot_limits"], "host.model_slot_limits")
    expected_lanes = frozenset({"luna", "terra", "sol", "spark"})
    _exact_keys(raw_limits, expected_lanes, "host.model_slot_limits")
    limits = {
        lane: _integer(
            raw_limits[lane], f"host.model_slot_limits.{lane}", 0, total_slots
        )
        for lane in sorted(expected_lanes)
    }
    raw_models = host["models"]
    if type(raw_models) is not list or len(raw_models) > 4:
        raise RoutingError("invalid_bounds", "host models are out of bounds")
    models: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for index, raw_model in enumerate(raw_models):
        model = _mapping(raw_model, f"host.models[{index}]")
        _exact_keys(model, HOST_MODEL_FIELDS, "host model")
        model_id = _string(model["model_id"], "host model id", maximum=64)
        if model_id in seen_models:
            raise RoutingError("invalid_bounds", "host models are duplicated")
        seen_models.add(model_id)
        status = _enum(model["status"], HOST_STATUSES, "host model status")
        raw_efforts = model["efforts"]
        if type(raw_efforts) is not list or len(raw_efforts) > 4:
            raise RoutingError("invalid_bounds", "host model efforts are invalid")
        efforts = _normalise_list(raw_efforts, "host model efforts", _string, 4)
        if any(
            _pair_by_model_effort(policy, model_id, effort) is None
            for effort in efforts
        ):
            raise RoutingError(
                "unknown_model_effort", "host reports an unregistered pair"
            )
        models.append({"model_id": model_id, "status": status, "efforts": efforts})
    models.sort(key=lambda model: model["model_id"])
    entitlements = _normalise_list(
        host["entitlements"], "host.entitlements", _string, 1
    )
    if any(entitlement not in ENTITLEMENTS for entitlement in entitlements):
        raise RoutingError("invalid_schema", "host entitlement is invalid")
    return {
        "schema": HOST_SCHEMA,
        "host_id_hash": _hash(host["host_id_hash"], "host.host_id_hash"),
        "capability_epoch": _integer(
            host["capability_epoch"], "host.capability_epoch", 0, MAX_31
        ),
        "total_slots": total_slots,
        "model_slot_limits": limits,
        "models": models,
        "entitlements": entitlements,
    }


def _normalise_legacy(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    legacy = _mapping(value, "legacy")
    _exact_keys(legacy, LEGACY_FIELDS, "legacy")
    if legacy["schema"] != LEGACY_SCHEMA:
        raise RoutingError("invalid_schema", "legacy schema is invalid")
    compatibility_version = _integer(
        legacy["compatibility_version"], "legacy.compatibility_version", 1, 2
    )
    complexity = legacy["complexity"]
    route = legacy["recommended_route"]
    if complexity is not None:
        complexity = _string(complexity, "legacy.complexity", maximum=16)
        if complexity not in LEGACY_COMPLEXITY_RANKS:
            raise RoutingError(
                "legacy_profile_incomplete", "legacy complexity is unknown"
            )
    if route is not None:
        route = _string(route, "legacy.recommended_route", maximum=32)
        if route not in LEGACY_ROUTE_RANKS:
            raise RoutingError("legacy_profile_incomplete", "legacy route is unknown")
    if complexity is None and route is None:
        raise RoutingError("legacy_profile_incomplete", "legacy input is empty")
    if (
        complexity is not None
        and route is not None
        and LEGACY_COMPLEXITY_RANKS[complexity] != LEGACY_ROUTE_RANKS[route]
    ):
        raise RoutingError("legacy_route_conflict", "legacy labels conflict")
    return {
        "schema": LEGACY_SCHEMA,
        "compatibility_version": compatibility_version,
        "complexity": complexity,
        "recommended_route": route,
    }


def _legacy_floor(legacy: Mapping[str, Any]) -> int:
    complexity = legacy["complexity"]
    route = legacy["recommended_route"]
    if complexity is not None:
        return LEGACY_COMPLEXITY_RANKS[complexity]
    if route is not None:
        return LEGACY_ROUTE_RANKS[route]
    raise RoutingError("legacy_profile_incomplete", "legacy input is empty")


def adapt_legacy_profile(
    legacy: Mapping[str, Any], *, task_id: str
) -> LegacyAdaptation:
    """Build a conservative full v3 profile from a legacy route label."""

    parsed = _normalise_legacy(legacy)
    if parsed is None:
        raise RoutingError("legacy_profile_incomplete", "legacy input is required")
    floor_rank = _legacy_floor(parsed)
    role = "execution" if floor_rank <= 80 else "design"
    access = "workspace_write" if role == "execution" else "read_only"
    profile = {
        "schema": TASK_SCHEMA,
        "task_id": _task_id(task_id, "legacy task_id"),
        "role": role,
        "access": access,
        "write_scope_count": 1 if role == "execution" else 0,
        "write_scope_breadth": "single_file" if role == "execution" else "none",
        "read_scope_count": 0,
        "read_scope_breadth": "none",
        "overlap_risk": "none",
        "overlap_count": 0,
        "dependency_depth": 0,
        "downstream_critical_count": 0,
        "critical_path": False,
        "criticality": "normal",
        "cross_module": False,
        "database_work": False,
        "migration": False,
        "security_sensitive": False,
        "destructive": False,
        "external_boundary": False,
        "architecture_conflict": False,
        "design_ambiguity": False,
        "verification_cost": "focused",
        "blocker_severity": "none",
        "authorization": "not_required",
        "authorization_evidence_hash": None,
        "narrow_decoupling_eligible": False,
        "strike": None,
        "gate_matrix_hash": _hash_json({"legacy": parsed, "kind": "gates"}),
        "profile_evidence_hash": _hash_json({"legacy": parsed, "kind": "profile"}),
    }
    return LegacyAdaptation(profile=profile, floor_rank=floor_rank)


def _normalise_request(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    request = _mapping(value, "request")
    _exact_keys(request, REQUEST_FIELDS, "request")
    if request["schema"] != REQUEST_SCHEMA:
        raise RoutingError("invalid_schema", "request schema is invalid")
    request_bytes = len(_canonical_json(request).encode("utf-8"))
    maximum_request_bytes = _mapping(policy["limits"], "policy limits")[
        "maximum_request_bytes"
    ]
    if request_bytes > maximum_request_bytes:
        raise RoutingError("invalid_bounds", "request exceeds the 32 KiB bound")
    if _hash(request["policy_hash"], "request.policy_hash") != policy_hash(policy):
        raise RoutingError("invalid_policy_hash", "request binds a different policy")
    limits = _mapping(policy["limits"], "policy limits")
    task = _normalise_task(request["task"])
    dependency = _normalise_dependency(
        request["dependency_state"], int(limits["maximum_dependency_items"])
    )
    scope = _normalise_scope(request["scope_state"], int(limits["maximum_scope_items"]))
    if task["overlap_count"] != len(scope["conflicting_task_ids"]):
        raise RoutingError("contradictory_profile", "overlap count does not bind scope")
    if bool(scope["active_writer_task_ids"]) != (task["overlap_risk"] == "active"):
        raise RoutingError("contradictory_profile", "active overlap facts disagree")
    if (
        not scope["active_writer_task_ids"]
        and task["overlap_count"] == 0
        and task["overlap_risk"] != "none"
    ):
        raise RoutingError("contradictory_profile", "overlap facts disagree")
    return {
        "schema": REQUEST_SCHEMA,
        "policy_hash": request["policy_hash"],
        "task": task,
        "dependency_state": dependency,
        "scope_state": scope,
        "scheduler_facts": _normalise_scheduler(request["scheduler_facts"]),
        "host_capabilities": _normalise_host(request["host_capabilities"], policy),
        "override_receipt": request["override_receipt"],
        "legacy": _normalise_legacy(request["legacy"]),
    }


def _v4_host_for_v3(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    host = dict(value)
    raw_models = host.get("models")
    if not isinstance(raw_models, list):
        return host
    models: list[object] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            models.append(raw_model)
            continue
        model = dict(raw_model)
        if model.get("model_id") == "gpt-5.3-codex-spark":
            raw_efforts = model.get("efforts")
            if isinstance(raw_efforts, list):
                model["efforts"] = ["medium"] if "medium" in raw_efforts else []
        models.append(model)
    host["models"] = models
    return host


def _normalise_host_v4(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    v3_policy = _v4_as_v3_policy(policy)
    normalised = _normalise_host(_v4_host_for_v3(value), v3_policy)
    host = _mapping(value, "host_capabilities")
    raw_models = host["models"]
    if not isinstance(raw_models, list):
        raise RoutingError("invalid_bounds", "host models are out of bounds")
    alternate = _mapping(policy["spark_alternate"], "policy spark alternate")
    allowed_efforts = frozenset(alternate["allowed_efforts"])
    actual_spark_efforts: list[str] | None = None
    for index, raw_model in enumerate(raw_models):
        model = _mapping(raw_model, f"host.models[{index}]")
        if model.get("model_id") != "gpt-5.3-codex-spark":
            continue
        efforts = _normalise_list(
            model.get("efforts"), "host Spark model efforts", _string, 4
        )
        if any(effort not in allowed_efforts for effort in efforts):
            raise RoutingError(
                "unknown_model_effort", "host reports an unregistered Spark effort"
            )
        actual_spark_efforts = efforts
    if actual_spark_efforts is None:
        return normalised
    models: list[dict[str, Any]] = []
    for raw_model in normalised["models"]:
        model = dict(_mapping(raw_model, "host model"))
        if model["model_id"] == "gpt-5.3-codex-spark":
            model["efforts"] = actual_spark_efforts
        models.append(model)
    return {**normalised, "models": models}


def _v4_request_as_v3(
    request: Mapping[str, Any], v3_policy: Mapping[str, Any]
) -> dict[str, Any]:
    converted = dict(request)
    converted["schema"] = REQUEST_SCHEMA
    converted["policy_hash"] = policy_hash(v3_policy)
    task = request.get("task")
    if isinstance(task, Mapping):
        converted_task = dict(task)
        converted_task["schema"] = TASK_SCHEMA
        converted["task"] = converted_task
    converted["host_capabilities"] = _v4_host_for_v3(
        request.get("host_capabilities")
    )
    return converted


def _normalise_request_v4(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    request = _mapping(value, "request")
    _exact_keys(request, REQUEST_FIELDS, "request")
    if request["schema"] != REQUEST_SCHEMA_V4:
        raise RoutingError("invalid_schema", "request schema is invalid")
    request_bytes = len(_canonical_json(request).encode("utf-8"))
    maximum_request_bytes = _mapping(policy["limits"], "policy limits")[
        "maximum_request_bytes"
    ]
    if request_bytes > maximum_request_bytes:
        raise RoutingError("invalid_bounds", "request exceeds the 32 KiB bound")
    if _hash(request["policy_hash"], "request.policy_hash") != policy_hash_v4(policy):
        raise RoutingError("invalid_policy_hash", "request binds a different policy")
    v3_policy = _v4_as_v3_policy(policy)
    parsed = _normalise_request(_v4_request_as_v3(request, v3_policy), v3_policy)
    task = dict(_mapping(parsed["task"], "task"))
    task["schema"] = TASK_SCHEMA_V4
    return {
        **parsed,
        "schema": REQUEST_SCHEMA_V4,
        "policy_hash": request["policy_hash"],
        "task": task,
        "host_capabilities": _normalise_host_v4(
            request["host_capabilities"], policy
        ),
    }


def task_fingerprint(request: Mapping[str, Any]) -> str:
    """Hash the normalized v3 identity, deliberately excluding legacy labels."""

    dependency = _mapping(request["dependency_state"], "dependency_state")
    scope = _mapping(request["scope_state"], "scope_state")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    return _hash_json(
        {
            "schema": FINGERPRINT_SCHEMA,
            "policy_hash": request["policy_hash"],
            "task": request["task"],
            "dependency_state_hash": dependency["dependency_state_hash"],
            "owned_scope_state_hash": scope["owned_scope_hash"],
            "host_capability_hash": _hash_json(host),
            "epochs": {
                "graph_epoch": dependency["graph_epoch"],
                "scope_epoch": scope["scope_epoch"],
                "capability_epoch": host["capability_epoch"],
                "route_epoch": scheduler["route_epoch"],
                "override_epoch": scheduler["override_epoch"],
                "recovery_epoch": scheduler["recovery_epoch"],
                "lease_epoch": scheduler["lease_epoch"],
            },
        }
    )


def _task_fingerprint_v4(request: Mapping[str, Any]) -> str:
    dependency = _mapping(request["dependency_state"], "dependency_state")
    scope = _mapping(request["scope_state"], "scope_state")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    return _hash_json(
        {
            "schema": FINGERPRINT_SCHEMA_V4,
            "policy_hash": request["policy_hash"],
            "task": request["task"],
            "dependency_state_hash": dependency["dependency_state_hash"],
            "owned_scope_state_hash": scope["owned_scope_hash"],
            "host_capability_hash": _hash_json(host),
            "epochs": {
                "graph_epoch": dependency["graph_epoch"],
                "scope_epoch": scope["scope_epoch"],
                "capability_epoch": host["capability_epoch"],
                "route_epoch": scheduler["route_epoch"],
                "override_epoch": scheduler["override_epoch"],
                "recovery_epoch": scheduler["recovery_epoch"],
                "lease_epoch": scheduler["lease_epoch"],
            },
        }
    )


def _score_components(
    task: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    score = _mapping(policy["score"], "policy score")
    components: list[dict[str, Any]] = []

    def add(code: str, points: int) -> None:
        if points:
            components.append({"code": code, "points": points})

    role = task["role"]
    add(f"role_{role}", int(_mapping(score["role"], "role points")[role]))
    write_breadth = task["write_scope_breadth"]
    add(
        f"scope_write_{write_breadth}",
        int(
            _mapping(score["write_scope_breadth"], "write breadth points")[
                write_breadth
            ]
        ),
    )
    add("scope_write_count", 2 * min(int(task["write_scope_count"]), 8))
    read_breadth = task["read_scope_breadth"]
    add(
        f"scope_read_{read_breadth}",
        int(_mapping(score["read_scope_breadth"], "read breadth points")[read_breadth]),
    )
    add("scope_read_count", (min(int(task["read_scope_count"]), 16) + 3) // 4)
    overlap = task["overlap_risk"]
    add(
        f"overlap_{overlap}",
        int(_mapping(score["overlap_risk"], "overlap points")[overlap]),
    )
    add("overlap_count", 2 * min(int(task["overlap_count"]), 4))
    add("dependency_depth", 2 * min(int(task["dependency_depth"]), 8))
    add("downstream_critical_count", min(int(task["downstream_critical_count"]), 8))
    if task["critical_path"]:
        add("critical_path", 6)
    criticality = task["criticality"]
    add(
        f"criticality_{criticality}",
        int(_mapping(score["criticality"], "criticality points")[criticality]),
    )
    for task_field, code, points in (
        ("cross_module", "cross_module", 6),
        ("database_work", "database_work", 8),
        ("migration", "migration", 12),
        ("security_sensitive", "security_sensitive", 14),
        ("destructive", "destructive", 18),
        ("external_boundary", "external_boundary", 6),
        ("architecture_conflict", "architecture_conflict", 20),
        ("design_ambiguity", "design_ambiguity", 16),
    ):
        if task[task_field]:
            add(code, points)
    verification = task["verification_cost"]
    add(
        f"verification_{verification}",
        int(_mapping(score["verification_cost"], "verification points")[verification]),
    )
    blocker = task["blocker_severity"]
    add(
        f"blocker_{blocker}",
        int(_mapping(score["blocker_severity"], "blocker points")[blocker]),
    )
    return components


def _effective_dispatch(task: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    if task["architecture_conflict"]:
        return "design", "read_only", ["architecture_conflict_design_probe"]
    if task["design_ambiguity"]:
        return "design", "read_only", ["design_ambiguity_probe"]
    return str(task["role"]), str(task["access"]), []


def _risk_floor_sources(
    task: Mapping[str, Any], effective_role: str, policy: Mapping[str, Any]
) -> list[tuple[str, int]]:
    risk = _mapping(policy["risk_floors"], "policy risk floors")
    sources: list[tuple[str, int]] = []
    if task["cross_module"]:
        sources.append(("floor_cross_module", int(risk["cross_module"])))
    if task["database_work"]:
        sources.append(("floor_database", int(risk["database_work"])))
    if task["migration"]:
        sources.append(("floor_migration", int(risk["migration"])))
    if task["security_sensitive"]:
        security_key = (
            "security_execution"
            if effective_role in {"execution", "integration"}
            else "security_review"
        )
        sources.append(("floor_security", int(risk[security_key])))
    if task["destructive"]:
        if effective_role == "acceptance":
            destructive_key = "destructive_acceptance"
        elif effective_role == "review":
            destructive_key = "destructive_review"
        elif effective_role in {"execution", "integration"}:
            destructive_key = "destructive_execution"
        else:
            raise RoutingError("contradictory_profile", "destructive role is invalid")
        sources.append(("floor_destructive", int(risk[destructive_key])))
    if task["architecture_conflict"] or task["design_ambiguity"]:
        sources.append(("floor_role", int(risk["design_conflict"])))
    if effective_role == "acceptance":
        sources.append(("floor_acceptance", int(risk["acceptance"])))
    return sources


def _floor(
    task: Mapping[str, Any],
    score_rank: int,
    effective_role: str,
    policy: Mapping[str, Any],
    compatibility_floor: int | None,
) -> tuple[dict[str, Any], list[str]]:
    role_floors = _mapping(policy["role_floors"], "policy role floors")
    sources = [("floor_role", int(role_floors[effective_role]))]
    sources.extend(_risk_floor_sources(task, effective_role, policy))
    if compatibility_floor is not None:
        sources.append(("legacy_profile_conservative", compatibility_floor))
    rank = max([score_rank, *[source_rank for _, source_rank in sources]])
    reasons = [code for code, source_rank in sources if source_rank >= score_rank]
    return _pair_by_rank(policy, rank), list(dict.fromkeys(reasons))


def _host_reports_exact(host: Mapping[str, Any], pair: Mapping[str, Any]) -> bool:
    for raw_model in host["models"]:
        model = _mapping(raw_model, "host model")
        if model["model_id"] != pair["model"]:
            continue
        return model["status"] == "available" and pair["effort"] in model["efforts"]
    return False


def _route_view(pair: Mapping[str, Any]) -> dict[str, str]:
    return {
        "lane": str(pair["lane"]),
        "model": str(pair["model"]),
        "effort": str(pair["effort"]),
    }


def _resolve_attested_candidate(
    host: Mapping[str, Any], preferred: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Intersect one floor with exact host attestations without weakening it."""

    requested = _route_view(preferred)
    if _host_reports_exact(host, preferred):
        return dict(preferred), {
            "state": "preferred",
            "requested": requested,
            "attestation_reason": "exact_attested",
        }
    if preferred["lane"] != "spark":
        for raw_pair in policy["registry"]:
            candidate = _mapping(raw_pair, "policy candidate")
            if (
                candidate["lane"] == preferred["lane"]
                and candidate["rank"] >= preferred["rank"]
                and _host_reports_exact(host, candidate)
            ):
                return dict(candidate), {
                    "state": "capability_fallback",
                    "requested": requested,
                    "attestation_reason": "preferred_tuple_unattested",
                }
    return None, {
        "state": "capability_unavailable",
        "requested": requested,
        "attestation_reason": "no_exact_attested_candidate",
    }


def _spark_reason(
    request: Mapping[str, Any],
    effective_role: str,
    effective_access: str,
    floor_rank: int,
    policy: Mapping[str, Any],
) -> str | None:
    task = _mapping(request["task"], "task")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    if (
        effective_role not in {"execution", "recovery"}
        or effective_access != "workspace_write"
    ):
        return "spark_role_excluded"
    if task["blocker_severity"] != "severe":
        return "spark_not_severe"
    if not task["critical_path"]:
        return "spark_not_critical_path"
    strike = task["strike"]
    if strike is None or strike["kind"] != "static_acceptance_blocker":
        return "spark_not_static_acceptance"
    if not (strike["feature_green"] or strike["single_bounded_acceptance_gate"]):
        return "spark_candidate_not_green"
    if not task["narrow_decoupling_eligible"]:
        return "spark_scope_not_narrow"
    if not strike["no_live_competing_writer"]:
        return "spark_competing_writer"
    if (
        strike["max_changed_files"] > 4
        or strike["max_focused_commands"] > 3
        or strike["max_strike_minutes"] > 15
    ):
        return "spark_exit_not_bounded"
    if strike["prior_spark_attempts"] != 0:
        return "spark_prior_attempt"
    if task["verification_cost"] in {"full_regression", "long_regression"}:
        return "spark_long_regression"
    if (
        task["migration"]
        or task["destructive"]
        or task["architecture_conflict"]
        or task["design_ambiguity"]
        or scheduler["dispatch_cause"] == "default_refill"
        or floor_rank > 80
    ):
        return "spark_architecture_or_migration"
    spark = _mapping(policy["spark"], "policy spark")
    if spark["required_entitlement"] not in host[
        "entitlements"
    ] or not _host_reports_exact(host, spark):
        return "spark_entitlement_unavailable"
    return None


def _normalise_override(value: object) -> dict[str, Any]:
    override = _mapping(value, "override_receipt")
    _exact_keys(override, OVERRIDE_FIELDS, "override_receipt")
    if override["schema"] != OVERRIDE_SCHEMA:
        raise RoutingError("invalid_schema", "override schema is invalid")
    receipt_without_hash = {
        key: item for key, item in override.items() if key != "receipt_hash"
    }
    receipt_hash = _hash(override["receipt_hash"], "override.receipt_hash")
    if receipt_hash != _hash_json(receipt_without_hash):
        raise RoutingError("contradictory_profile", "override receipt hash is invalid")
    reason = _string(override["reason"], "override.reason", maximum=64)
    if reason not in {
        "capability_exact_route_unavailable",
        "host_incident",
        "quota_exhausted",
    }:
        raise RoutingError("invalid_schema", "override reason is invalid")
    if override["attester_role"] != "coordinator":
        raise RoutingError(
            "contradictory_profile", "override was not coordinator-attested"
        )
    issued = _integer(
        override["issued_event_seq"], "override.issued_event_seq", 0, MAX_63
    )
    expires = _integer(
        override["expires_event_seq"], "override.expires_event_seq", issued, MAX_63
    )
    return {
        "schema": OVERRIDE_SCHEMA,
        "task_fingerprint": _hash(
            override["task_fingerprint"], "override.task_fingerprint"
        ),
        "policy_hash": _hash(override["policy_hash"], "override.policy_hash"),
        "lease_epoch": _integer(
            override["lease_epoch"], "override.lease_epoch", 0, MAX_31
        ),
        "issued_event_seq": issued,
        "expires_event_seq": expires,
        "requested_model": _string(
            override["requested_model"], "override.requested_model", maximum=64
        ),
        "requested_effort": _string(
            override["requested_effort"], "override.requested_effort", maximum=16
        ),
        "reason": reason,
        "evidence_hash": _hash(override["evidence_hash"], "override.evidence_hash"),
        "attester_role": "coordinator",
        "attester_endpoint_hash": _hash(
            override["attester_endpoint_hash"], "override.attester_endpoint_hash"
        ),
        "receipt_hash": receipt_hash,
    }


def _trusted_hashes(values: Iterable[str], field: str, maximum: int) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray)):
        raise RoutingError("invalid_schema", f"{field} must be a hash collection")
    normalised = [_hash(value, field) for value in values]
    if len(normalised) > maximum:
        raise RoutingError("invalid_bounds", f"{field} exceeds bound")
    return frozenset(normalised)


def _apply_override(
    request: Mapping[str, Any],
    fingerprint: str,
    floor: Mapping[str, Any],
    spark_allowed: bool,
    policy: Mapping[str, Any],
    trusted_override_receipt_hashes: Iterable[str],
    trusted_evidence_hashes: Iterable[str],
    coordinator_endpoint_hash: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    raw_override = request["override_receipt"]
    if raw_override is None:
        return None, None
    override = _normalise_override(raw_override)
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    trusted_receipts = _trusted_hashes(
        trusted_override_receipt_hashes, "trusted override receipt", 8
    )
    trusted_evidence = _trusted_hashes(trusted_evidence_hashes, "trusted evidence", 32)
    if coordinator_endpoint_hash is None:
        raise RoutingError("contradictory_profile", "override coordinator is unknown")
    endpoint_hash = _hash(coordinator_endpoint_hash, "coordinator_endpoint_hash")
    if (
        override["receipt_hash"] not in trusted_receipts
        or override["evidence_hash"] not in trusted_evidence
        or override["attester_endpoint_hash"] != endpoint_hash
        or override["task_fingerprint"] != fingerprint
        or override["policy_hash"] != request["policy_hash"]
        or override["lease_epoch"] != scheduler["lease_epoch"]
        or not override["issued_event_seq"]
        <= scheduler["event_seq"]
        <= override["expires_event_seq"]
    ):
        raise RoutingError("contradictory_profile", "override receipt is untrusted")
    pair = _pair_by_model_effort(
        policy, override["requested_model"], override["requested_effort"]
    )
    if pair is None:
        raise RoutingError("unknown_model_effort", "override requests unknown pair")
    if pair["lane"] == "spark":
        if not spark_allowed:
            raise RoutingError(
                "contradictory_profile", "override cannot bypass Spark gate"
            )
        return pair, "host_override_upward"
    if int(pair["rank"]) <= int(floor["rank"]):
        raise RoutingError("contradictory_profile", "override does not raise the floor")
    if not _host_reports_exact(_mapping(request["host_capabilities"], "host"), pair):
        raise RoutingError(
            "contradictory_profile", "override pair is not currently available"
        )
    return pair, "host_override_upward"


def _result(
    *,
    request: Mapping[str, Any],
    fingerprint: str,
    score: int,
    components: list[dict[str, Any]],
    floor: Mapping[str, Any],
    effective_role: str,
    effective_access: str,
    route_pair: Mapping[str, Any],
    capability_resolution: Mapping[str, Any],
    reason_codes: list[str],
    override_receipt_hash: str | None,
) -> dict[str, Any]:
    task = _mapping(request["task"], "task")
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": "resolved",
        "task_id": task["task_id"],
        "task_fingerprint": fingerprint,
        "policy_hash": request["policy_hash"],
        "score": score,
        "score_components": components,
        "safety_floor": dict(floor),
        "effective_role": effective_role,
        "access": effective_access,
        "route": _route_view(route_pair),
        "capability_resolution": dict(capability_resolution),
        "reason_codes": reason_codes,
        "override_receipt_hash": override_receipt_hash,
        "_computed_event_seq": _mapping(request["scheduler_facts"], "scheduler")[
            "event_seq"
        ],
    }
    result["render_hash"] = _hash_json(
        {
            key: value
            for key, value in result.items()
            if key not in {"render_hash", "_computed_event_seq"}
        }
    )
    return result


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "_computed_event_seq"}


def _terminal_result(
    raw_request: object,
    *,
    status: str,
    reason: str,
    parsed: Mapping[str, Any] | None = None,
    fingerprint: str | None = None,
    score: int = 0,
    score_components: Sequence[Mapping[str, Any]] = (),
    safety_floor: Mapping[str, Any] | None = None,
    effective_role: str | None = None,
    effective_access: str | None = None,
    capability_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if parsed is not None:
        task = _mapping(parsed["task"], "task")
        task_id = task["task_id"]
        policy_value = parsed["policy_hash"]
    else:
        raw_task = raw_request.get("task") if isinstance(raw_request, Mapping) else None
        task_id = raw_task.get("task_id") if isinstance(raw_task, Mapping) else ""
        if not isinstance(task_id, str):
            task_id = ""
        policy_value = (
            raw_request.get("policy_hash") if isinstance(raw_request, Mapping) else ""
        )
        if not isinstance(policy_value, str):
            policy_value = ""
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "task_id": task_id,
        "task_fingerprint": fingerprint or _safe_hash(raw_request),
        "policy_hash": policy_value,
        "score": score,
        "score_components": [dict(component) for component in score_components],
        "safety_floor": None if safety_floor is None else dict(safety_floor),
        "effective_role": effective_role,
        "access": effective_access,
        "route": None,
        "capability_resolution": capability_resolution,
        "reason_codes": [reason],
        "override_receipt_hash": None,
    }
    result["render_hash"] = _hash_json(
        {key: value for key, value in result.items() if key != "render_hash"}
    )
    return result


def route(
    request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    cache: RouteCache | None = None,
    trusted_authorization_evidence_hashes: Iterable[str] = (),
    trusted_override_receipt_hashes: Iterable[str] = (),
    trusted_evidence_hashes: Iterable[str] = (),
    coordinator_endpoint_hash: str | None = None,
    compatibility_floor: int | None = None,
) -> dict[str, Any]:
    """Resolve one strict v3 routing request, failing closed on any ambiguity."""

    if (
        isinstance(request, Mapping)
        and request.get("schema") == REQUEST_SCHEMA
        and _has_quota_field(request)
    ):
        return _terminal_result(
            request,
            status="rejected",
            reason="FASTLANE_SCHEMA_UPGRADE_REQUIRED",
        )
    try:
        active_policy = (
            _validate_policy(policy) if policy is not None else load_policy()
        )
        parsed = _normalise_request(request, active_policy)
        fingerprint = task_fingerprint(parsed)
        task = _mapping(parsed["task"], "task")
        if cache is not None:
            cached = cache.get(task["task_id"], fingerprint)
            if cached is not None:
                return _public_result(cached)

        dependency = _mapping(parsed["dependency_state"], "dependency")
        scope = _mapping(parsed["scope_state"], "scope")
        if not set(dependency["direct_dependency_ids"]).issubset(
            dependency["completed_dependency_ids"]
        ):
            return _terminal_result(
                request,
                status="blocked",
                reason="dependency_not_ready",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        if scope["active_writer_task_ids"]:
            return _terminal_result(
                request,
                status="blocked",
                reason="scope_conflict_active",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        trusted_authorizations = _trusted_hashes(
            trusted_authorization_evidence_hashes, "trusted authorization", 32
        )
        if task["destructive"] and (
            task["authorization"] != "approved"
            or task["authorization_evidence_hash"] is None
            or task["authorization_evidence_hash"] not in trusted_authorizations
        ):
            return _terminal_result(
                request,
                status="blocked",
                reason="destructive_authorization_missing",
                parsed=parsed,
                fingerprint=fingerprint,
            )

        if compatibility_floor is not None:
            compatibility_floor = _integer(
                compatibility_floor, "compatibility_floor", 10, 110
            )
            legacy = parsed["legacy"]
            if legacy is None or _legacy_floor(legacy) != compatibility_floor:
                raise RoutingError(
                    "contradictory_profile", "compatibility floor is unbound"
                )
            if task["migration"] or task["security_sensitive"] or task["destructive"]:
                raise RoutingError(
                    "contradictory_profile", "legacy profile cannot clear risk"
                )

        components = _score_components(task, active_policy)
        score = min(100, sum(component["points"] for component in components))
        score_band = next(
            band
            for band in active_policy["score_bands"]
            if band["minimum"] <= score <= band["maximum"]
        )
        effective_role, effective_access, projection_reasons = _effective_dispatch(task)
        floor, floor_reasons = _floor(
            task,
            int(score_band["rank"]),
            effective_role,
            active_policy,
            compatibility_floor,
        )
        floor_rank = int(floor["rank"])

        spark_reason = (
            "spark_role_excluded"
            if compatibility_floor is not None
            else _spark_reason(
                parsed, effective_role, effective_access, floor_rank, active_policy
            )
        )
        spark_allowed = spark_reason is None
        host = _mapping(parsed["host_capabilities"], "host")
        if spark_allowed:
            preferred_pair = dict(_mapping(active_policy["spark"], "policy spark"))
            reason_codes = ["spark_static_blocker"]
        else:
            preferred_pair = floor
            reason_codes = [
                *projection_reasons,
                *floor_reasons,
                str(score_band["reason"]),
            ]
            if compatibility_floor is None and parsed["legacy"] is not None:
                reason_codes.append("legacy_hint_ignored")
            if compatibility_floor is not None:
                reason_codes.append("legacy_profile_conservative")
            reason_codes.append(spark_reason)

        override_pair, override_reason = _apply_override(
            parsed,
            fingerprint,
            floor,
            spark_allowed,
            active_policy,
            trusted_override_receipt_hashes,
            trusted_evidence_hashes,
            coordinator_endpoint_hash,
        )
        if override_reason is not None:
            assert override_pair is not None
            route_pair = override_pair
            capability_resolution = {
                "state": "preferred",
                "requested": _route_view(override_pair),
                "attestation_reason": "exact_attested",
            }
            if "spark_static_blocker" not in reason_codes:
                reason_codes.append(override_reason)
        else:
            route_pair, capability_resolution = _resolve_attested_candidate(
                host, preferred_pair, active_policy
            )

        if route_pair is None:
            return _terminal_result(
                request,
                status="unavailable",
                reason="capability_unavailable",
                parsed=parsed,
                fingerprint=fingerprint,
                score=score,
                score_components=components,
                safety_floor=floor,
                effective_role=effective_role,
                effective_access=effective_access,
                capability_resolution=capability_resolution,
            )

        override_hash = None
        if parsed["override_receipt"] is not None:
            override_hash = _normalise_override(parsed["override_receipt"])[
                "receipt_hash"
            ]
        result = _result(
            request=parsed,
            fingerprint=fingerprint,
            score=score,
            components=components,
            floor=floor,
            effective_role=effective_role,
            effective_access=effective_access,
            route_pair=route_pair,
            capability_resolution=capability_resolution,
            reason_codes=list(dict.fromkeys(reason_codes)),
            override_receipt_hash=override_hash,
        )
        if cache is not None:
            cache.put(result)
        return _public_result(result)
    except RoutingError as error:
        return _terminal_result(request, status="rejected", reason=error.code)
    except (KeyError, TypeError, ValueError):
        return _terminal_result(request, status="rejected", reason="invalid_schema")


def _spark_alternate_pair(
    policy: Mapping[str, Any], score: int, floor_rank: int
) -> dict[str, str]:
    alternate = _mapping(policy["spark_alternate"], "policy spark alternate")
    envelope = max(score, floor_rank)
    for raw_band in alternate["effort_bands"]:
        band = _mapping(raw_band, "policy spark alternate effort band")
        if int(band["minimum"]) <= envelope <= int(band["maximum"]):
            return {
                "lane": str(alternate["lane"]),
                "model": str(alternate["model"]),
                "effort": str(band["effort"]),
            }
    raise RoutingError(
        "invalid_schema", "policy spark alternate has no effort for the envelope"
    )


def _spark_alternate_reason(
    request: Mapping[str, Any],
    *,
    effective_role: str,
    effective_access: str,
    score: int,
    floor_rank: int,
    policy: Mapping[str, Any],
) -> str | None:
    task = _mapping(request["task"], "task")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    alternate = _mapping(policy["spark_alternate"], "policy spark alternate")
    if floor_rank > 80:
        return "spark_alternate_sol_floor"
    if any(
        bool(task[field])
        for field in (
            "cross_module",
            "database_work",
            "migration",
            "security_sensitive",
            "destructive",
            "external_boundary",
            "architecture_conflict",
            "design_ambiguity",
        )
    ):
        return "spark_alternate_high_risk"
    if effective_role != "execution" or effective_access != "workspace_write":
        return "spark_alternate_scope_not_bounded"
    if (
        not task["narrow_decoupling_eligible"]
        or not 1
        <= int(task["write_scope_count"])
        <= int(alternate["maximum_write_scope_count"])
        or task["write_scope_breadth"]
        != alternate["required_write_scope_breadth"]
    ):
        return "spark_alternate_scope_not_bounded"
    if task["verification_cost"] not in alternate["allowed_verification_costs"]:
        return "spark_alternate_verification_unbounded"
    if host["model_slot_limits"]["spark"] != alternate["required_local_slot_count"]:
        return "spark_alternate_slot_unavailable"
    pair = _spark_alternate_pair(policy, score, floor_rank)
    if (
        alternate["required_entitlement"] not in host["entitlements"]
        or not _host_reports_exact(host, pair)
    ):
        return "spark_alternate_capability_unavailable"
    return None


def _spark_alternate_binding(
    request: Mapping[str, Any], pair: Mapping[str, Any]
) -> dict[str, Any]:
    task = _mapping(request["task"], "task")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    scope = _mapping(request["scope_state"], "scope_state")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    binding = {
        "schema": "2718lab-devkit/spark-alternate-binding-v1",
        "route": _route_view(pair),
        "capability_hash": _hash_json(host),
        "task_hash": _hash_json(task),
        "lease_epoch": scheduler["lease_epoch"],
        "context_hash": _hash_json(scheduler),
        "scope_hash": scope["owned_scope_hash"],
    }
    return {**binding, "binding_hash": _hash_json(binding)}


def _terminal_result_v4(
    raw_request: object,
    *,
    status: str,
    reason: str,
    parsed: Mapping[str, Any] | None = None,
    fingerprint: str | None = None,
    score: int = 0,
    score_components: Sequence[Mapping[str, Any]] = (),
    safety_floor: Mapping[str, Any] | None = None,
    effective_role: str | None = None,
    effective_access: str | None = None,
    capability_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = _terminal_result(
        raw_request,
        status=status,
        reason=reason,
        parsed=parsed,
        fingerprint=fingerprint,
        score=score,
        score_components=score_components,
        safety_floor=safety_floor,
        effective_role=effective_role,
        effective_access=effective_access,
        capability_resolution=capability_resolution,
    )
    result["schema"] = RESULT_SCHEMA_V4
    result["spark_alternate"] = None
    result["render_hash"] = _hash_json(
        {key: value for key, value in result.items() if key != "render_hash"}
    )
    return result


def route_v4(
    request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    cache: RouteCache | None = None,
    trusted_authorization_evidence_hashes: Iterable[str] = (),
    trusted_override_receipt_hashes: Iterable[str] = (),
    trusted_evidence_hashes: Iterable[str] = (),
    coordinator_endpoint_hash: str | None = None,
    compatibility_floor: int | None = None,
) -> dict[str, Any]:
    """Compile a v4 Spark alternate without replacing its baseline route."""

    if (
        isinstance(request, Mapping)
        and request.get("schema") == REQUEST_SCHEMA_V4
        and _has_quota_field(request)
    ):
        return _terminal_result_v4(
            request,
            status="rejected",
            reason="FASTLANE_SCHEMA_UPGRADE_REQUIRED",
        )
    parsed: dict[str, Any] | None = None
    fingerprint: str | None = None
    try:
        active_policy = (
            _validate_policy_v4(policy) if policy is not None else load_policy_v4()
        )
        parsed = _normalise_request_v4(request, active_policy)
        fingerprint = _task_fingerprint_v4(parsed)
        task = _mapping(parsed["task"], "task")
        if cache is not None:
            cached = cache.get(task["task_id"], fingerprint)
            if cached is not None:
                return _public_result(cached)

        v3_policy = _v4_as_v3_policy(active_policy)
        baseline = route(
            _v4_request_as_v3(request, v3_policy),
            policy=v3_policy,
            trusted_authorization_evidence_hashes=trusted_authorization_evidence_hashes,
            trusted_override_receipt_hashes=trusted_override_receipt_hashes,
            trusted_evidence_hashes=trusted_evidence_hashes,
            coordinator_endpoint_hash=coordinator_endpoint_hash,
            compatibility_floor=compatibility_floor,
        )
        result = dict(baseline)
        result["schema"] = RESULT_SCHEMA_V4
        result["policy_hash"] = parsed["policy_hash"]
        result["task_fingerprint"] = fingerprint
        result["spark_alternate"] = None

        route_value = result.get("route")
        is_static_spark = (
            isinstance(route_value, Mapping) and route_value.get("lane") == "spark"
        )
        reason_codes = result.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(code, str) for code in reason_codes
        ):
            raise RoutingError("invalid_schema", "baseline reason codes are invalid")
        if is_static_spark:
            result["reason_codes"] = reason_codes
        elif result.get("status") != "resolved" or not isinstance(route_value, Mapping):
            result["reason_codes"] = [
                *[code for code in reason_codes if not code.startswith("spark_")],
                "spark_alternate_baseline_unavailable",
            ]
        else:
            safety_floor = _mapping(result.get("safety_floor"), "safety_floor")
            score = result.get("score")
            if type(score) is not int:
                raise RoutingError("invalid_schema", "baseline score is invalid")
            alternate_reason = _spark_alternate_reason(
                parsed,
                effective_role=str(result.get("effective_role")),
                effective_access=str(result.get("access")),
                score=score,
                floor_rank=int(safety_floor["rank"]),
                policy=active_policy,
            )
            result["reason_codes"] = [
                code for code in reason_codes if not code.startswith("spark_")
            ]
            if alternate_reason is None:
                pair = _spark_alternate_pair(
                    active_policy, score, int(safety_floor["rank"])
                )
                result["spark_alternate"] = {
                    "route": _route_view(pair),
                    "binding": _spark_alternate_binding(parsed, pair),
                }
                result["reason_codes"].append("spark_alternate_eligible")
            else:
                result["reason_codes"].append(alternate_reason)

        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"]))
        result["_computed_event_seq"] = _mapping(
            parsed["scheduler_facts"], "scheduler_facts"
        )["event_seq"]
        result["render_hash"] = _hash_json(
            {
                key: value
                for key, value in result.items()
                if key not in {"render_hash", "_computed_event_seq"}
            }
        )
        if cache is not None:
            cache.put(result)
        return _public_result(result)
    except RoutingError as error:
        return _terminal_result_v4(
            request,
            status="rejected",
            reason=error.code,
            parsed=parsed,
            fingerprint=fingerprint,
        )
    except (KeyError, TypeError, ValueError):
        return _terminal_result_v4(
            request,
            status="rejected",
            reason="invalid_schema",
            parsed=parsed,
            fingerprint=fingerprint,
        )


def _validate_policy_v5(policy: object) -> dict[str, Any]:
    """Validate V5 safety floors without maintaining a model registry."""

    value = _policy_mapping(policy, "policy")
    expected = frozenset(
        {
            "schema",
            "version",
            "role_floors",
            "risk_floors",
            "limits",
            "spark_gate",
            "reason_codes",
        }
    )
    _exact_keys(value, expected, "policy")
    if value.get("schema") != POLICY_SCHEMA_V5 or value.get("version") != 5:
        raise RoutingError("invalid_schema", "policy schema/version is invalid")

    role_floors = _mapping(value["role_floors"], "policy role floors")
    if set(role_floors) != ROLES:
        raise RoutingError("invalid_schema", "policy role floors are incomplete")
    for role, rank in role_floors.items():
        _integer(rank, f"policy role floor {role}", 10, 110)

    risk_floors = _mapping(value["risk_floors"], "policy risk floors")
    expected_risks = frozenset(
        {
            "cross_module",
            "database_work",
            "migration",
            "security_execution",
            "security_review",
            "destructive_execution",
            "destructive_review",
            "destructive_acceptance",
            "design_conflict",
            "acceptance",
        }
    )
    _exact_keys(risk_floors, expected_risks, "policy risk floors")
    for risk, rank in risk_floors.items():
        _integer(rank, f"policy risk floor {risk}", 10, 110)

    limits = _mapping(value["limits"], "policy limits")
    expected_limits = frozenset(
        {
            "maximum_request_bytes",
            "maximum_tasks",
            "maximum_cache_entries",
            "maximum_gate_reason_codes",
            "maximum_host_models",
            "maximum_total_slots",
            "maximum_scope_items",
            "maximum_dependency_items",
        }
    )
    _exact_keys(limits, expected_limits, "policy limits")
    expected_limit_values = {
        "maximum_request_bytes": 32768,
        "maximum_tasks": 64,
        "maximum_cache_entries": 128,
        "maximum_gate_reason_codes": 16,
        "maximum_host_models": 8,
        "maximum_total_slots": 8,
        "maximum_scope_items": 8,
        "maximum_dependency_items": 32,
    }
    if dict(limits) != expected_limit_values:
        raise RoutingError("invalid_schema", "policy limits are invalid")

    spark_gate = _mapping(value["spark_gate"], "policy spark gate")
    _exact_keys(spark_gate, frozenset({"required_entitlement"}), "policy spark gate")
    if spark_gate["required_entitlement"] != "spark_preview":
        raise RoutingError("invalid_schema", "policy Spark gate is invalid")

    reason_codes = _normalise_list(
        value["reason_codes"], "policy reason_codes", _string, 128
    )
    if len(reason_codes) != len(value["reason_codes"]):
        raise RoutingError("invalid_bounds", "policy reason codes are duplicated")
    required_reasons = {
        "dependency_not_ready",
        "scope_conflict_active",
        "destructive_authorization_missing",
        "lease_unavailable",
        "capability_unavailable",
        "host_model_policy_denied",
        "host_child_route_below_safety_floor",
        "spark_not_severe",
        "spark_entitlement_unavailable",
        "FASTLANE_SCHEMA_UPGRADE_REQUIRED",
    }
    if not required_reasons.issubset(reason_codes):
        raise RoutingError("invalid_schema", "policy reason registry is incomplete")
    return dict(value)


def load_policy_v5(path: Path | None = None) -> dict[str, Any]:
    """Load the V5 safety-floor policy without contacting a host."""

    policy_path = path or POLICY_PATH_V5
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoutingError("invalid_schema", "policy asset cannot be parsed") from error
    return _validate_policy_v5(raw)


def policy_hash_v5(policy: Mapping[str, Any]) -> str:
    """Return the canonical hash of a V5 safety-floor policy."""

    _validate_policy_v5(policy)
    return _hash_json(policy)


def _normalise_host_v5(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate host availability facts without recognizing only fixed models."""

    host = _mapping(value, "host_capabilities")
    _exact_keys(host, HOST_FIELDS, "host_capabilities")
    if host["schema"] != HOST_SCHEMA:
        raise RoutingError("invalid_schema", "host schema is invalid")
    limits = _mapping(policy["limits"], "policy limits")
    total_slots = _integer(
        host["total_slots"],
        "host.total_slots",
        1,
        int(limits["maximum_total_slots"]),
    )
    raw_limits = _mapping(host["model_slot_limits"], "host.model_slot_limits")
    expected_lanes = frozenset({"luna", "terra", "sol", "spark"})
    _exact_keys(raw_limits, expected_lanes, "host.model_slot_limits")
    model_slot_limits = {
        lane: _integer(
            raw_limits[lane], f"host.model_slot_limits.{lane}", 0, total_slots
        )
        for lane in sorted(expected_lanes)
    }
    raw_models = host["models"]
    if type(raw_models) is not list or len(raw_models) > int(limits["maximum_host_models"]):
        raise RoutingError("invalid_bounds", "host models are out of bounds")
    models: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for index, raw_model in enumerate(raw_models):
        model = _mapping(raw_model, f"host.models[{index}]")
        _exact_keys(model, HOST_MODEL_FIELDS, "host model")
        model_id = _string(model["model_id"], "host model id", maximum=64)
        if model_id in seen_models:
            raise RoutingError("invalid_bounds", "host models are duplicated")
        seen_models.add(model_id)
        raw_efforts = model["efforts"]
        if type(raw_efforts) is not list or len(raw_efforts) > 8:
            raise RoutingError("invalid_bounds", "host model efforts are invalid")
        models.append(
            {
                "model_id": model_id,
                "status": _enum(model["status"], HOST_STATUSES, "host model status"),
                "efforts": _normalise_list(
                    raw_efforts, "host model efforts", _string, 8
                ),
            }
        )
    models.sort(key=lambda item: item["model_id"])
    entitlements = _normalise_list(
        host["entitlements"], "host.entitlements", _string, 1
    )
    if any(entitlement not in ENTITLEMENTS for entitlement in entitlements):
        raise RoutingError("invalid_schema", "host entitlement is invalid")
    return {
        "schema": HOST_SCHEMA,
        "host_id_hash": _hash(host["host_id_hash"], "host.host_id_hash"),
        "capability_epoch": _integer(
            host["capability_epoch"], "host.capability_epoch", 0, MAX_31
        ),
        "total_slots": total_slots,
        "model_slot_limits": model_slot_limits,
        "models": models,
        "entitlements": entitlements,
    }


def _normalise_task_v5(value: object) -> dict[str, Any]:
    task = dict(_mapping(value, "task"))
    _exact_keys(task, TASK_FIELDS, "task")
    if task.get("schema") != TASK_SCHEMA_V5:
        raise RoutingError("invalid_schema", "task schema is invalid")
    task["schema"] = TASK_SCHEMA
    normalised = _normalise_task(task)
    normalised["schema"] = TASK_SCHEMA_V5
    return normalised


def _normalise_request_v5(value: object, policy: Mapping[str, Any]) -> dict[str, Any]:
    request = _mapping(value, "request")
    _exact_keys(request, REQUEST_FIELDS_V5, "request")
    if request["schema"] != REQUEST_SCHEMA_V5:
        raise RoutingError("invalid_schema", "request schema is invalid")
    if len(_canonical_json(request).encode("utf-8")) > int(
        _mapping(policy["limits"], "policy limits")["maximum_request_bytes"]
    ):
        raise RoutingError("invalid_bounds", "request exceeds the 32 KiB bound")
    if _hash(request["policy_hash"], "request.policy_hash") != policy_hash_v5(policy):
        raise RoutingError("invalid_policy_hash", "request binds a different policy")
    limits = _mapping(policy["limits"], "policy limits")
    task = _normalise_task_v5(request["task"])
    dependency = _normalise_dependency(
        request["dependency_state"], int(limits["maximum_dependency_items"])
    )
    scope = _normalise_scope(request["scope_state"], int(limits["maximum_scope_items"]))
    if task["overlap_count"] != len(scope["conflicting_task_ids"]):
        raise RoutingError("contradictory_profile", "overlap count does not bind scope")
    if bool(scope["active_writer_task_ids"]) != (task["overlap_risk"] == "active"):
        raise RoutingError("contradictory_profile", "active overlap facts disagree")
    if (
        not scope["active_writer_task_ids"]
        and task["overlap_count"] == 0
        and task["overlap_risk"] != "none"
    ):
        raise RoutingError("contradictory_profile", "overlap facts disagree")
    return {
        "schema": REQUEST_SCHEMA_V5,
        "policy_hash": request["policy_hash"],
        "task": task,
        "dependency_state": dependency,
        "scope_state": scope,
        "scheduler_facts": _normalise_scheduler(request["scheduler_facts"]),
        "host_capabilities": _normalise_host_v5(request["host_capabilities"], policy),
        "child_route_attestation": request["child_route_attestation"],
        "legacy": _normalise_legacy(request["legacy"]),
    }


def _v5_request_binding_hash(request: Mapping[str, Any]) -> str:
    dependency = _mapping(request["dependency_state"], "dependency_state")
    scope = _mapping(request["scope_state"], "scope_state")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    return _hash_json(
        {
            "schema": REQUEST_BINDING_SCHEMA_V5,
            "policy_hash": request["policy_hash"],
            "task": request["task"],
            "dependency_state_hash": dependency["dependency_state_hash"],
            "owned_scope_state_hash": scope["owned_scope_hash"],
            "scheduler_facts": scheduler,
            "host_capability_hash": _hash_json(host),
            "legacy": request["legacy"],
        }
    )


def v5_request_binding_hash(value: Mapping[str, Any]) -> str:
    """Return the pre-attestation V5 binding a host must sign before routing."""

    policy = load_policy_v5()
    return _v5_request_binding_hash(_normalise_request_v5(value, policy))


def _normalise_child_route_attestation_v5(
    value: object,
    *,
    binding_hash: str,
    host: Mapping[str, Any],
    scheduler: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a host-issued route tuple and its freshness fence.

    All malformed, stale, or unavailable attestations collapse to the public
    capability-unavailable state.  A syntactically valid explicit host refusal
    remains distinguishable so callers do not misrepresent it as a dispatch.
    """

    try:
        attestation = _mapping(value, "child_route_attestation")
        _exact_keys(
            attestation,
            CHILD_ROUTE_ATTESTATION_FIELDS,
            "child_route_attestation",
        )
        if attestation["schema"] != CHILD_ROUTE_ATTESTATION_SCHEMA:
            raise RoutingError("invalid_schema", "child route schema is invalid")
        supplied_hash = _hash(
            attestation["attestation_hash"], "child route attestation hash"
        )
        unhashed = {
            key: item for key, item in attestation.items() if key != "attestation_hash"
        }
        if supplied_hash != _hash_json(unhashed):
            raise RoutingError("contradictory_profile", "child route hash is invalid")
        status = _enum(
            attestation["status"],
            frozenset({"attested", "refused"}),
            "child route status",
        )
        if _hash(
            attestation["request_binding_hash"], "child route request binding"
        ) != binding_hash:
            raise RoutingError("contradictory_profile", "child route binding is stale")
        if _hash(attestation["host_id_hash"], "child route host id") != host["host_id_hash"]:
            raise RoutingError("contradictory_profile", "child route host is stale")
        if _integer(
            attestation["capability_epoch"], "child route capability epoch", 0, MAX_31
        ) != host["capability_epoch"]:
            raise RoutingError("contradictory_profile", "child route capability is stale")
        if _integer(
            attestation["lease_epoch"], "child route lease epoch", 0, MAX_31
        ) != scheduler["lease_epoch"]:
            raise RoutingError("contradictory_profile", "child route lease is stale")
        issued = _integer(
            attestation["issued_event_seq"], "child route issued event", 0, MAX_63
        )
        expires = _integer(
            attestation["expires_event_seq"], "child route expiry event", issued, MAX_63
        )
        if not issued <= scheduler["event_seq"] <= expires:
            raise RoutingError("contradictory_profile", "child route event is stale")
        if _boolean(
            attestation["inherit_current_session_model"],
            "child route inherits current session model",
        ):
            raise RoutingError("contradictory_profile", "child route cannot inherit")
        if status == "refused":
            if (
                attestation["route"] is not None
                or attestation["refusal_code"] != "host_model_policy_denied"
            ):
                raise RoutingError("invalid_schema", "host refusal is invalid")
            return {
                "status": status,
                "route": None,
                "attestation_hash": supplied_hash,
            }
        if attestation["refusal_code"] is not None:
            raise RoutingError("invalid_schema", "attested route has a refusal")
        route = _mapping(attestation["route"], "child route")
        _exact_keys(route, CHILD_ROUTE_FIELDS, "child route")
        return {
            "status": status,
            "route": {
                "lane": _enum(route["lane"], CHILD_ROUTE_LANES, "child route lane"),
                "model": _string(route["model"], "child route model", maximum=64),
                "effort": _string(route["effort"], "child route effort", maximum=16),
                "rank": _integer(route["rank"], "child route rank", 1, 110),
            },
            "attestation_hash": supplied_hash,
        }
    except RoutingError as error:
        raise RoutingError("capability_unavailable", "child route attestation unavailable") from error


def _task_fingerprint_v5(
    request: Mapping[str, Any], attestation: Mapping[str, Any]
) -> str:
    return _hash_json(
        {
            "schema": FINGERPRINT_SCHEMA_V5,
            "request_binding_hash": _v5_request_binding_hash(request),
            "attestation_hash": attestation["attestation_hash"],
        }
    )


def _floor_v5(
    task: Mapping[str, Any], effective_role: str, policy: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    role_floors = _mapping(policy["role_floors"], "policy role floors")
    sources = [("floor_role", int(role_floors[effective_role]))]
    sources.extend(_risk_floor_sources(task, effective_role, policy))
    rank = max(source_rank for _, source_rank in sources)
    reasons = [code for code, source_rank in sources if source_rank == rank]
    return {"rank": rank}, list(dict.fromkeys(reasons))


def _host_reports_exact_v5(host: Mapping[str, Any], route: Mapping[str, Any]) -> bool:
    if host["model_slot_limits"][route["lane"]] < 1:
        return False
    for raw_model in host["models"]:
        model = _mapping(raw_model, "host model")
        if model["model_id"] != route["model"]:
            continue
        return model["status"] == "available" and route["effort"] in model["efforts"]
    return False


def _spark_reason_v5(
    request: Mapping[str, Any],
    effective_role: str,
    effective_access: str,
    floor_rank: int,
    policy: Mapping[str, Any],
) -> str | None:
    task = _mapping(request["task"], "task")
    scheduler = _mapping(request["scheduler_facts"], "scheduler_facts")
    host = _mapping(request["host_capabilities"], "host_capabilities")
    if effective_role not in {"execution", "recovery"} or effective_access != "workspace_write":
        return "spark_role_excluded"
    if task["blocker_severity"] != "severe":
        return "spark_not_severe"
    if not task["critical_path"]:
        return "spark_not_critical_path"
    strike = task["strike"]
    if strike is None or strike["kind"] != "static_acceptance_blocker":
        return "spark_not_static_acceptance"
    if not (strike["feature_green"] or strike["single_bounded_acceptance_gate"]):
        return "spark_candidate_not_green"
    if not task["narrow_decoupling_eligible"]:
        return "spark_scope_not_narrow"
    if not strike["no_live_competing_writer"]:
        return "spark_competing_writer"
    if (
        strike["max_changed_files"] > 4
        or strike["max_focused_commands"] > 3
        or strike["max_strike_minutes"] > 15
    ):
        return "spark_exit_not_bounded"
    if strike["prior_spark_attempts"] != 0:
        return "spark_prior_attempt"
    if task["verification_cost"] in {"full_regression", "long_regression"}:
        return "spark_long_regression"
    if (
        task["migration"]
        or task["destructive"]
        or task["architecture_conflict"]
        or task["design_ambiguity"]
        or scheduler["dispatch_cause"] == "default_refill"
        or floor_rank > 80
    ):
        return "spark_architecture_or_migration"
    spark_gate = _mapping(policy["spark_gate"], "policy spark gate")
    if spark_gate["required_entitlement"] not in host["entitlements"]:
        return "spark_entitlement_unavailable"
    return None


def _terminal_result_v5(
    raw_request: object,
    *,
    status: str,
    reason: str,
    parsed: Mapping[str, Any] | None = None,
    fingerprint: str | None = None,
    safety_floor: Mapping[str, Any] | None = None,
    effective_role: str | None = None,
    effective_access: str | None = None,
    capability_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if parsed is not None:
        task_id = _mapping(parsed["task"], "task")["task_id"]
        policy_hash = parsed["policy_hash"]
    else:
        raw_task = raw_request.get("task") if isinstance(raw_request, Mapping) else None
        task_id = raw_task.get("task_id") if isinstance(raw_task, Mapping) else ""
        policy_hash = raw_request.get("policy_hash") if isinstance(raw_request, Mapping) else ""
        if not isinstance(task_id, str):
            task_id = ""
        if not isinstance(policy_hash, str):
            policy_hash = ""
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA_V5,
        "status": status,
        "task_id": task_id,
        "task_fingerprint": fingerprint or _safe_hash(raw_request),
        "policy_hash": policy_hash,
        "safety_floor": None if safety_floor is None else dict(safety_floor),
        "effective_role": effective_role,
        "access": effective_access,
        "route": None,
        "dispatch": {"state": "not_dispatched", "requires_host_execution": True},
        "capability_resolution": capability_resolution,
        "reason_codes": [reason],
    }
    result["render_hash"] = _hash_json(
        {key: value for key, value in result.items() if key != "render_hash"}
    )
    return result


def _result_v5(
    *,
    request: Mapping[str, Any],
    fingerprint: str,
    floor: Mapping[str, Any],
    effective_role: str,
    effective_access: str,
    route: Mapping[str, Any],
    floor_reasons: Sequence[str],
    attestation_hash: str,
) -> dict[str, Any]:
    task = _mapping(request["task"], "task")
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA_V5,
        "status": "resolved",
        "task_id": task["task_id"],
        "task_fingerprint": fingerprint,
        "policy_hash": request["policy_hash"],
        "safety_floor": dict(floor),
        "effective_role": effective_role,
        "access": effective_access,
        "route": {**dict(route), "inherit_current_session_model": False},
        "dispatch": {"state": "not_dispatched", "requires_host_execution": True},
        "capability_resolution": {
            "state": "host_attested",
            "attestation_hash": attestation_hash,
        },
        "reason_codes": list(dict.fromkeys(floor_reasons)),
        "_computed_event_seq": _mapping(request["scheduler_facts"], "scheduler")[
            "event_seq"
        ],
    }
    result["render_hash"] = _hash_json(
        {
            key: value
            for key, value in result.items()
            if key not in {"render_hash", "_computed_event_seq"}
        }
    )
    return result


def _has_quota_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            "quota" in str(key).casefold() or _has_quota_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_quota_field(item) for item in value)
    return False


def route_v5(
    request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    cache: RouteCache | None = None,
    trusted_authorization_evidence_hashes: Iterable[str] = (),
) -> dict[str, Any]:
    """Compile a host-attested V5 child route without dispatching a session."""

    if (
        isinstance(request, Mapping)
        and request.get("schema") in {REQUEST_SCHEMA, REQUEST_SCHEMA_V4}
        and _has_quota_field(request)
    ):
        return _terminal_result_v5(
            request,
            status="rejected",
            reason="FASTLANE_SCHEMA_UPGRADE_REQUIRED",
        )
    parsed: dict[str, Any] | None = None
    fingerprint: str | None = None
    try:
        active_policy = (
            _validate_policy_v5(policy) if policy is not None else load_policy_v5()
        )
        parsed = _normalise_request_v5(request, active_policy)
        scheduler = _mapping(parsed["scheduler_facts"], "scheduler_facts")
        host = _mapping(parsed["host_capabilities"], "host_capabilities")
        attestation = _normalise_child_route_attestation_v5(
            parsed["child_route_attestation"],
            binding_hash=_v5_request_binding_hash(parsed),
            host=host,
            scheduler=scheduler,
        )
        fingerprint = _task_fingerprint_v5(parsed, attestation)
        task = _mapping(parsed["task"], "task")
        if cache is not None:
            cached = cache.get(task["task_id"], fingerprint)
            if cached is not None:
                return _public_result(cached)
        if attestation["status"] == "refused":
            return _terminal_result_v5(
                request,
                status="rejected",
                reason="host_model_policy_denied",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        dependency = _mapping(parsed["dependency_state"], "dependency_state")
        if not set(dependency["direct_dependency_ids"]).issubset(
            dependency["completed_dependency_ids"]
        ):
            return _terminal_result_v5(
                request,
                status="blocked",
                reason="dependency_not_ready",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        scope = _mapping(parsed["scope_state"], "scope_state")
        if scope["active_writer_task_ids"]:
            return _terminal_result_v5(
                request,
                status="blocked",
                reason="scope_conflict_active",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        trusted_authorizations = _trusted_hashes(
            trusted_authorization_evidence_hashes, "trusted authorization", 32
        )
        if task["destructive"] and (
            task["authorization"] != "approved"
            or task["authorization_evidence_hash"] is None
            or task["authorization_evidence_hash"] not in trusted_authorizations
        ):
            return _terminal_result_v5(
                request,
                status="blocked",
                reason="destructive_authorization_missing",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        if scheduler["lease_state"] in {"expired", "invalid", "released"}:
            return _terminal_result_v5(
                request,
                status="blocked",
                reason="lease_unavailable",
                parsed=parsed,
                fingerprint=fingerprint,
            )
        effective_role, effective_access, projection_reasons = _effective_dispatch(task)
        floor, floor_reasons = _floor_v5(task, effective_role, active_policy)
        route = _mapping(attestation["route"], "attested child route")
        if int(route["rank"]) < int(floor["rank"]):
            return _terminal_result_v5(
                request,
                status="blocked",
                reason="host_child_route_below_safety_floor",
                parsed=parsed,
                fingerprint=fingerprint,
                safety_floor=floor,
                effective_role=effective_role,
                effective_access=effective_access,
            )
        if route["lane"] == "spark":
            spark_reason = _spark_reason_v5(
                parsed,
                effective_role,
                effective_access,
                int(floor["rank"]),
                active_policy,
            )
            if spark_reason is not None:
                return _terminal_result_v5(
                    request,
                    status="blocked",
                    reason=spark_reason,
                    parsed=parsed,
                    fingerprint=fingerprint,
                    safety_floor=floor,
                    effective_role=effective_role,
                    effective_access=effective_access,
                )
        if not _host_reports_exact_v5(host, route):
            return _terminal_result_v5(
                request,
                status="unavailable",
                reason="capability_unavailable",
                parsed=parsed,
                fingerprint=fingerprint,
                safety_floor=floor,
                effective_role=effective_role,
                effective_access=effective_access,
                capability_resolution={
                    "state": "capability_unavailable",
                    "attestation_reason": "host_capability_no_longer_exact",
                },
            )
        result = _result_v5(
            request=parsed,
            fingerprint=fingerprint,
            floor=floor,
            effective_role=effective_role,
            effective_access=effective_access,
            route=route,
            floor_reasons=[*projection_reasons, *floor_reasons],
            attestation_hash=attestation["attestation_hash"],
        )
        if cache is not None:
            cache.put(result)
        return _public_result(result)
    except RoutingError as error:
        status = "unavailable" if error.code == "capability_unavailable" else "rejected"
        return _terminal_result_v5(
            request,
            status=status,
            reason=error.code,
            parsed=parsed,
            fingerprint=fingerprint,
        )
    except (KeyError, TypeError, ValueError):
        return _terminal_result_v5(
            request,
            status="rejected",
            reason="invalid_schema",
            parsed=parsed,
            fingerprint=fingerprint,
        )


def _utc_z(value: object, field: str) -> str:
    text = _string(value, field, maximum=32)
    if not UTC_Z_RE.fullmatch(text):
        raise RoutingError("invalid_bounds", f"{field} is not strict UTC-Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise RoutingError(
            "invalid_bounds", f"{field} is not a valid instant"
        ) from error
    return text


def gate_evidence_identity(value: Mapping[str, Any]) -> str:
    """Validate and hash the reusable gate-evidence identity without executing it."""

    payload = _mapping(value, "gate evidence")
    _exact_keys(payload, GATE_EVIDENCE_FIELDS, "gate evidence")
    if payload["schema"] != GATE_EVIDENCE_SCHEMA:
        raise RoutingError("invalid_schema", "gate evidence schema is invalid")
    base_commit = _string(
        payload["base_or_integration_commit"], "base commit", maximum=40
    )
    if not GIT_ID_RE.fullmatch(base_commit):
        raise RoutingError("invalid_bounds", "base commit is invalid")
    candidate_commit = payload["candidate_commit"]
    if candidate_commit is not None:
        candidate_commit = _string(candidate_commit, "candidate commit", maximum=40)
        if not GIT_ID_RE.fullmatch(candidate_commit):
            raise RoutingError("invalid_bounds", "candidate commit is invalid")
    started = _utc_z(payload["started_at_utc_z"], "started_at_utc_z")
    finished = _utc_z(payload["finished_at_utc_z"], "finished_at_utc_z")
    if finished < started:
        raise RoutingError("invalid_bounds", "gate evidence ends before it starts")
    return _hash_json(
        {
            "schema": GATE_EVIDENCE_SCHEMA,
            "task_fingerprint": _hash(payload["task_fingerprint"], "task_fingerprint"),
            "gate_definition_hash": _hash(
                payload["gate_definition_hash"], "gate_definition_hash"
            ),
            "base_or_integration_commit": base_commit,
            "candidate_commit": candidate_commit,
            "owned_diff_hash": _hash(payload["owned_diff_hash"], "owned_diff_hash"),
            "environment_lock_hashes": _normalise_hashes(
                payload["environment_lock_hashes"], "environment_lock_hashes", 32
            ),
            "command_argv_hash": _hash(
                payload["command_argv_hash"], "command_argv_hash"
            ),
            "command_env_hash": _hash(payload["command_env_hash"], "command_env_hash"),
            "cache_root_id_hash": _hash(
                payload["cache_root_id_hash"], "cache_root_id_hash"
            ),
            "exit_status": _integer(payload["exit_status"], "exit_status", 0, 255),
            "started_at_utc_z": started,
            "finished_at_utc_z": finished,
            "candidate_epoch": _integer(
                payload["candidate_epoch"], "candidate_epoch", 0, MAX_31
            ),
        }
    )
