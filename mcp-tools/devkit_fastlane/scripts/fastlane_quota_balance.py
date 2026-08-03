"""Pure, signed Fast Lane quota admission and coordinator-kernel gates."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HashResolver = Callable[[str], bytes | None]

_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "fastlane-quota-balance-policy-v1.json"
)
_POLICY_PATH_V2 = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "fastlane-quota-balance-policy-v2.json"
)
_HASH_PREFIX = "sha256:"
_MAX_CANDIDATES = 8
_MAX_REASON_CODES = 16
_MAX_SNAPSHOT_TTL_SECONDS = 120
_LOCAL_SCHEDULER_CAP = 3
_VALID_OPERATIONS = frozenset(
    {
        "intake",
        "ledger",
        "lease",
        "queue",
        "admission",
        "dispatch",
        "mechanical_integration",
        "final_acceptance",
        "archive",
    }
)
_SEMANTIC_FLAGS = (
    "cross_contract_semantics",
    "security_semantics",
    "recovery_semantics",
    "model_policy_exception",
    "quota_policy_exception",
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: object) -> str:
    return (
        _HASH_PREFIX
        + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    )


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(_HASH_PREFIX) + 64
        and value.startswith(_HASH_PREFIX)
        and all(
            character in "0123456789abcdef" for character in value[len(_HASH_PREFIX) :]
        )
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected object")
    return value


def _int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer is outside its allowed range")
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be strict UTC-Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("timestamp is invalid") from error
    if parsed.tzinfo != UTC:
        raise ValueError("timestamp must be UTC")
    return parsed


def _policy() -> Mapping[str, Any]:
    try:
        value = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("quota policy asset is unavailable") from error
    policy = _mapping(value)
    if set(policy) != {"schema", "version", "main", "spark"}:
        raise ValueError("quota policy has unsupported fields")
    if policy["schema"] != "2718lab-devkit/fastlane-quota-balance-policy-v1":
        raise ValueError("quota policy schema is invalid")
    if policy["version"] != 1:
        raise ValueError("quota policy version is invalid")
    return policy


def _policy_v2() -> Mapping[str, Any]:
    try:
        value = json.loads(_POLICY_PATH_V2.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("quota policy asset is unavailable") from error
    policy = _mapping(value)
    if set(policy) != {"schema", "version", "main", "spark", "spark_alternate"}:
        raise ValueError("quota policy has unsupported fields")
    if policy["schema"] != "2718lab-devkit/fastlane-quota-balance-policy-v2":
        raise ValueError("quota policy schema is invalid")
    if policy["version"] != 2:
        raise ValueError("quota policy version is invalid")
    main = _mapping(policy["main"])
    spark = _mapping(policy["spark"])
    alternate = _mapping(policy["spark_alternate"])
    if set(main) != {
        "normal_used_lt_ppm",
        "normal_slope_lt_ppm_300s",
        "warm_used_lt_ppm",
        "warm_slope_lt_ppm_300s",
        "tight_used_lt_ppm",
        "tight_slope_lt_ppm_300s",
        "critical_used_lt_ppm",
        "normal_target",
        "warm_target",
        "tight_target",
        "critical_target",
        "paused_target",
    }:
        raise ValueError("quota main policy is invalid")
    if set(spark) != {
        "usage_deadband_ppm",
        "slope_deadband_ppm_300s",
        "absolute_cap_ppm",
        "global_concurrency_cap",
        "host_concurrency_cap",
    }:
        raise ValueError("quota spark policy is invalid")
    if alternate != {"required_local_slot_count": 1, "single_card_cap": 1}:
        raise ValueError("quota spark alternate policy is invalid")
    return policy


def _normalized_request_hash(request: Mapping[str, Any]) -> str:
    body = {key: value for key, value in request.items() if key != "request_hash"}
    candidates = body.get("candidates")
    receipts = body.get("receipts")
    if isinstance(candidates, list):
        body["candidates"] = sorted(
            candidates,
            key=lambda value: (
                str(value.get("candidate_id", "")) if isinstance(value, Mapping) else ""
            ),
        )
    if isinstance(receipts, list):
        body["receipts"] = sorted(
            receipts,
            key=lambda value: (
                str(value.get("receipt_hash", "")) if isinstance(value, Mapping) else ""
            ),
        )
    return _hash(body)


def _candidate_binding_hash(candidate: Mapping[str, Any]) -> str:
    return _hash(
        {
            "candidate_id": candidate["candidate_id"],
            "workflow_key": candidate["workflow_key"],
            "task_key": candidate["task_key"],
            "task_lease_epoch": candidate["task_lease_epoch"],
            "assignment_epoch": candidate["assignment_epoch"],
            "assignment_token": candidate["assignment_token"],
            "host_id_hash": candidate["host_id_hash"],
            "local_slot_id": candidate["local_slot_id"],
            "write_scope_hash": candidate["write_scope_hash"],
            "input_snapshot_id": candidate["input_snapshot_id"],
        }
    )


def _candidate_ids(candidates: object) -> list[str]:
    if not isinstance(candidates, list):
        return []
    identifiers = {
        str(candidate.get("candidate_id"))
        for candidate in candidates
        if isinstance(candidate, Mapping) and _is_hash(candidate.get("candidate_id"))
    }
    return sorted(identifiers)


def _base_decision(
    *,
    status: str,
    snapshot_hash: str | None,
    ledger_epoch: int | None,
    main_pressure: str,
    global_main_target: int,
    main_proposal_ids: Iterable[str] = (),
    spark_proposal_ids: Iterable[str] = (),
    admitted_candidate_ids: Iterable[str] = (),
    held_candidate_ids: Iterable[str] = (),
    route_lock_hashes: Iterable[str] = (),
    reason_codes: Iterable[str] = (),
    policy_hash: str | None = None,
    prior_audit_hash: str | None = None,
    receipt_hashes: Iterable[str] = (),
    result_schema: str = "2718lab-devkit/fastlane-quota-balance-result-v1",
    authorization_schema: str = (
        "2718lab-devkit/fastlane-quota-balance-authorization-v1"
    ),
    audit_schema: str = "2718lab-devkit/quota-audit-event-v1",
) -> dict[str, Any]:
    main_proposals = sorted(set(main_proposal_ids))
    spark_proposals = sorted(set(spark_proposal_ids))
    admitted = sorted(set(admitted_candidate_ids))
    held = sorted(set(held_candidate_ids))
    routes = sorted(set(route_lock_hashes))
    reasons = sorted(set(reason_codes))[:_MAX_REASON_CODES]
    authorization = {
        "schema": authorization_schema,
        "policy_hash": policy_hash,
        "snapshot_hash": snapshot_hash,
        "ledger_epoch": ledger_epoch,
        "main_pressure": main_pressure,
        "global_main_target": global_main_target,
        "main_proposal_ids": main_proposals,
        "spark_proposal_ids": spark_proposals,
        "route_lock_hashes": routes,
        "prior_audit_hash": prior_audit_hash,
    }
    decision_hash = _hash(authorization)
    audit = {
        "schema": audit_schema,
        "policy_hash": policy_hash,
        "snapshot_hash": snapshot_hash,
        "ledger_epoch": ledger_epoch,
        "main_pressure": main_pressure,
        "global_main_target": global_main_target,
        "main_proposal_ids": main_proposals,
        "spark_proposal_ids": spark_proposals,
        "admitted_candidate_ids": admitted,
        "held_candidate_ids": held,
        "route_lock_hashes": routes,
        "reason_codes": reasons,
        "receipt_hashes": sorted(set(receipt_hashes)),
        "prior_audit_hash": prior_audit_hash,
        "decision_hash": decision_hash,
    }
    return {
        "schema": result_schema,
        "status": status,
        "snapshot_hash": snapshot_hash,
        "ledger_epoch": ledger_epoch,
        "main_pressure": main_pressure,
        "global_main_target": global_main_target,
        "main_proposal_ids": main_proposals,
        "spark_proposal_ids": spark_proposals,
        "admitted_candidate_ids": admitted,
        "held_candidate_ids": held,
        "route_lock_hashes": routes,
        "reason_codes": reasons,
        "audit_event_hash": _hash(audit),
        "decision_hash": decision_hash,
    }


def _fail_closed(
    request: Mapping[str, Any],
    *,
    policy_hash: str | None,
    reason: str,
) -> dict[str, Any]:
    return _base_decision(
        status="usage_unknown",
        snapshot_hash=None,
        ledger_epoch=None,
        main_pressure="unknown",
        global_main_target=6,
        held_candidate_ids=_candidate_ids(request.get("candidates")),
        reason_codes=(reason,),
        policy_hash=policy_hash,
        prior_audit_hash=(
            request.get("prior_audit_hash")
            if _is_hash(request.get("prior_audit_hash"))
            else None
        ),
    )


def _fail_closed_v2(
    request: Mapping[str, Any],
    *,
    policy_hash: str | None,
    reason: str,
) -> dict[str, Any]:
    return _base_decision(
        status="usage_unknown",
        snapshot_hash=None,
        ledger_epoch=None,
        main_pressure="unknown",
        global_main_target=6,
        held_candidate_ids=_candidate_ids(request.get("candidates")),
        reason_codes=(reason,),
        policy_hash=policy_hash,
        prior_audit_hash=(
            request.get("prior_audit_hash")
            if _is_hash(request.get("prior_audit_hash"))
            else None
        ),
        result_schema="2718lab-devkit/fastlane-quota-balance-result-v2",
        authorization_schema="2718lab-devkit/fastlane-quota-balance-authorization-v2",
        audit_schema="2718lab-devkit/quota-audit-event-v2",
    )


def _base_decision_v2(**kwargs: Any) -> dict[str, Any]:
    return _base_decision(
        result_schema="2718lab-devkit/fastlane-quota-balance-result-v2",
        authorization_schema="2718lab-devkit/fastlane-quota-balance-authorization-v2",
        audit_schema="2718lab-devkit/quota-audit-event-v2",
        **kwargs,
    )


def _verified_snapshot(
    value: object,
    *,
    trusted_key_resolver: HashResolver,
    evaluation_time_utc_z: str,
) -> tuple[Mapping[str, Any], datetime]:
    snapshot = _mapping(value)
    expected = {
        "schema",
        "source",
        "snapshot_seq",
        "observed_at_utc_z",
        "valid_until_utc_z",
        "sample_window_seconds",
        "main",
        "spark",
        "capacity",
        "snapshot_hash",
        "signature",
    }
    if set(snapshot) != expected:
        raise ValueError("snapshot has unsupported fields")
    if snapshot["schema"] != "2718lab-devkit/host-quota-snapshot-v1":
        raise ValueError("snapshot schema is invalid")
    source = _mapping(snapshot["source"])
    if set(source) != {"kind", "source_id_hash", "key_id"}:
        raise ValueError("snapshot source is invalid")
    if source["kind"] != "codex_host_usage_snapshot":
        raise PermissionError("snapshot source is untrusted")
    if not _is_hash(source["source_id_hash"]) or not _is_hash(source["key_id"]):
        raise ValueError("snapshot source hash is invalid")
    _int(snapshot["snapshot_seq"], minimum=1, maximum=2**63 - 1)
    if snapshot["sample_window_seconds"] != 300:
        raise ValueError("snapshot sample window is invalid")
    observed = _utc(snapshot["observed_at_utc_z"])
    valid_until = _utc(snapshot["valid_until_utc_z"])
    evaluation = _utc(evaluation_time_utc_z)
    ttl_seconds = int((valid_until - observed).total_seconds())
    if ttl_seconds < 0 or ttl_seconds > _MAX_SNAPSHOT_TTL_SECONDS:
        raise TimeoutError("snapshot freshness window is invalid")
    if not observed <= evaluation <= valid_until:
        raise TimeoutError("snapshot is stale")
    for pool_name in ("main", "spark"):
        pool = _mapping(snapshot[pool_name])
        if set(pool) != {"period_id_hash", "used_ppm", "delta_ppm_300s"}:
            raise ValueError("snapshot pool is invalid")
        if not _is_hash(pool["period_id_hash"]):
            raise ValueError("snapshot period is invalid")
        _int(pool["used_ppm"], minimum=0, maximum=1_000_000)
        _int(pool["delta_ppm_300s"], minimum=0, maximum=1_000_000)
    capacity = _mapping(snapshot["capacity"])
    expected_capacity = {
        "ledger_epoch",
        "global_main_active",
        "global_spark_active",
        "host_main_active",
        "host_spark_active",
        "host_main_cap",
        "host_spark_cap",
        "active_lease_set_hash",
    }
    if set(capacity) != expected_capacity:
        raise ValueError("snapshot capacity is invalid")
    _int(capacity["ledger_epoch"], minimum=0, maximum=2**63 - 1)
    _int(capacity["global_main_active"], minimum=0, maximum=12)
    _int(capacity["global_spark_active"], minimum=0, maximum=1)
    _int(capacity["host_main_active"], minimum=0, maximum=8)
    _int(capacity["host_spark_active"], minimum=0, maximum=1)
    _int(capacity["host_main_cap"], minimum=0, maximum=8)
    _int(capacity["host_spark_cap"], minimum=0, maximum=1)
    if not _is_hash(capacity["active_lease_set_hash"]):
        raise ValueError("snapshot lease set is invalid")
    unsigned = {
        key: item
        for key, item in snapshot.items()
        if key not in {"snapshot_hash", "signature"}
    }
    if snapshot["snapshot_hash"] != _hash(unsigned):
        raise PermissionError("snapshot hash is invalid")
    signature = _mapping(snapshot["signature"])
    if (
        set(signature) != {"algorithm", "value"}
        or signature["algorithm"] != "hmac-sha256"
    ):
        raise PermissionError("snapshot signature is invalid")
    key = trusted_key_resolver(str(source["key_id"]))
    if not isinstance(key, bytes) or not key:
        raise PermissionError("snapshot key is unavailable")
    signed = {**unsigned, "snapshot_hash": snapshot["snapshot_hash"]}
    expected_signature = hmac.new(
        key, _canonical_json(signed).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not isinstance(signature["value"], str) or not hmac.compare_digest(
        expected_signature, signature["value"]
    ):
        raise PermissionError("snapshot signature is invalid")
    return snapshot, evaluation


def _main_pressure(
    snapshot: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[str, int]:
    main = _mapping(snapshot["main"])
    limits = _mapping(policy["main"])
    used = int(main["used_ppm"])
    slope = int(main["delta_ppm_300s"])
    level = "normal"
    if used >= int(limits["critical_used_lt_ppm"]):
        level = "paused"
    elif used >= int(limits["tight_used_lt_ppm"]):
        level = "critical"
    elif used >= int(limits["warm_used_lt_ppm"]):
        level = "tight"
    elif used >= int(limits["normal_used_lt_ppm"]):
        level = "warm"
    slope_band = "normal"
    if slope >= int(limits["tight_slope_lt_ppm_300s"]):
        slope_band = "critical"
    elif slope >= int(limits["warm_slope_lt_ppm_300s"]):
        slope_band = "tight"
    elif slope >= int(limits["normal_slope_lt_ppm_300s"]):
        slope_band = "warm"
    rank = {"normal": 0, "warm": 1, "tight": 2, "critical": 3, "paused": 4}
    pressure = level if rank[level] >= rank[slope_band] else slope_band
    target_by_pressure = {
        "normal": int(limits["normal_target"]),
        "warm": int(limits["warm_target"]),
        "tight": int(limits["tight_target"]),
        "critical": int(limits["critical_target"]),
        "paused": int(limits["paused_target"]),
    }
    return pressure, target_by_pressure[pressure]


def _candidate(
    value: object,
    *,
    trusted_routes: set[str],
    trusted_bindings: set[str],
) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        candidate = _mapping(value)
        expected = {
            "candidate_id",
            "workflow_key",
            "task_key",
            "pool",
            "scheduler_role",
            "route_lock",
            "task_lease_epoch",
            "assignment_epoch",
            "assignment_token",
            "host_id_hash",
            "local_slot_id",
            "write_scope_hash",
            "input_snapshot_id",
            "spark_binding",
        }
        if set(candidate) != expected:
            raise ValueError("candidate fields are invalid")
        for field in (
            "candidate_id",
            "workflow_key",
            "task_key",
            "assignment_token",
            "host_id_hash",
            "write_scope_hash",
            "input_snapshot_id",
        ):
            if not _is_hash(candidate[field]):
                raise ValueError("candidate hash is invalid")
        if candidate["pool"] not in {"main", "spark"}:
            raise ValueError("candidate pool is invalid")
        if not isinstance(candidate["scheduler_role"], str) or not isinstance(
            candidate["local_slot_id"], str
        ):
            raise TypeError("candidate label is invalid")
        _int(candidate["task_lease_epoch"], minimum=1, maximum=2**31 - 1)
        _int(candidate["assignment_epoch"], minimum=1, maximum=2**31 - 1)
        route = _mapping(candidate["route_lock"])
        if set(route) != {
            "result_hash",
            "task_fingerprint",
            "lane",
            "model",
            "effort",
            "safety_floor_rank",
        }:
            raise ValueError("candidate route is invalid")
        if not _is_hash(route["result_hash"]) or not _is_hash(
            route["task_fingerprint"]
        ):
            raise ValueError("candidate route hash is invalid")
        _int(route["safety_floor_rank"], minimum=10, maximum=110)
        if (
            not isinstance(route["lane"], str)
            or not isinstance(route["model"], str)
            or not isinstance(route["effort"], str)
        ):
            raise TypeError("candidate route label is invalid")
        if _candidate_binding_hash(candidate) not in trusted_bindings:
            return None, "lease_scope_binding_invalid"
        if route["result_hash"] not in trusted_routes:
            return None, "quota_receipt_invalid"
        return candidate, None
    except (KeyError, TypeError, ValueError):
        return None, "quota_receipt_invalid"


def _valid_spark_binding(candidate: Mapping[str, Any]) -> bool:
    route = _mapping(candidate["route_lock"])
    binding = candidate["spark_binding"]
    if candidate["pool"] != "spark" or route["lane"] != "spark":
        return False
    if not isinstance(binding, Mapping) or set(binding) != {
        "spark_proof_hash",
        "parent_main_route_hash",
        "parent_admission_id",
        "writer_handoff_hash",
    }:
        return False
    return all(_is_hash(binding[key]) for key in binding)


def _v2_spark_binding_kind(
    candidate: Mapping[str, Any], *, trusted_alternate_bindings: set[str]
) -> tuple[str | None, str | None]:
    route = _mapping(candidate["route_lock"])
    if (
        candidate["pool"] != "spark"
        or route["lane"] != "spark"
        or route["model"] != "gpt-5.3-codex-spark"
        or route["effort"] not in {"low", "medium", "high", "xhigh"}
    ):
        return None, "spark_alternate_binding_invalid"
    if _valid_spark_binding(candidate):
        return "static", None
    try:
        binding = _mapping(candidate["spark_binding"])
        expected = {
            "schema",
            "route",
            "capability_hash",
            "task_hash",
            "lease_epoch",
            "context_hash",
            "scope_hash",
            "binding_hash",
        }
        if set(binding) != expected:
            raise ValueError("alternate binding fields are invalid")
        if binding["schema"] != "2718lab-devkit/spark-alternate-binding-v1":
            raise ValueError("alternate binding schema is invalid")
        unsigned = {key: value for key, value in binding.items() if key != "binding_hash"}
        if not _is_hash(binding["binding_hash"]) or binding["binding_hash"] != _hash(
            unsigned
        ):
            raise ValueError("alternate binding hash is invalid")
        if binding["binding_hash"] not in trusted_alternate_bindings:
            raise PermissionError("alternate binding is untrusted")
        binding_route = _mapping(binding["route"])
        if set(binding_route) != {"lane", "model", "effort"} or dict(
            binding_route
        ) != {
            "lane": route["lane"],
            "model": route["model"],
            "effort": route["effort"],
        }:
            raise ValueError("alternate route binding is invalid")
        for key in (
            "capability_hash",
            "task_hash",
            "context_hash",
            "scope_hash",
        ):
            if not _is_hash(binding[key]):
                raise ValueError("alternate binding hash is invalid")
        if (
            type(binding["lease_epoch"]) is not int
            or binding["lease_epoch"] != candidate["task_lease_epoch"]
            or binding["capability_hash"] != candidate["capability_binding_hash"]
            or binding["task_hash"] != candidate["task_key"]
            or binding["context_hash"] != candidate["context_binding_hash"]
            or binding["scope_hash"] != candidate["write_scope_hash"]
        ):
            raise ValueError("alternate binding does not match candidate")
    except (KeyError, TypeError, ValueError, PermissionError):
        return None, "spark_alternate_binding_invalid"
    return "alternate", None


def _candidate_v2(
    value: object,
    *,
    trusted_routes: set[str],
    trusted_bindings: set[str],
    trusted_alternate_bindings: set[str],
) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        candidate = _mapping(value)
        expected = {
            "candidate_id",
            "workflow_key",
            "task_key",
            "pool",
            "scheduler_role",
            "route_lock",
            "task_lease_epoch",
            "assignment_epoch",
            "assignment_token",
            "host_id_hash",
            "local_slot_id",
            "write_scope_hash",
            "input_snapshot_id",
            "capability_binding_hash",
            "context_binding_hash",
            "spark_binding",
        }
        if set(candidate) != expected:
            raise ValueError("candidate fields are invalid")
        for field in (
            "candidate_id",
            "workflow_key",
            "task_key",
            "assignment_token",
            "host_id_hash",
            "write_scope_hash",
            "input_snapshot_id",
            "capability_binding_hash",
            "context_binding_hash",
        ):
            if not _is_hash(candidate[field]):
                raise ValueError("candidate hash is invalid")
        if candidate["pool"] not in {"main", "spark"}:
            raise ValueError("candidate pool is invalid")
        if not isinstance(candidate["scheduler_role"], str) or not isinstance(
            candidate["local_slot_id"], str
        ):
            raise TypeError("candidate label is invalid")
        _int(candidate["task_lease_epoch"], minimum=1, maximum=2**31 - 1)
        _int(candidate["assignment_epoch"], minimum=1, maximum=2**31 - 1)
        route = _mapping(candidate["route_lock"])
        if set(route) != {
            "result_hash",
            "task_fingerprint",
            "lane",
            "model",
            "effort",
            "safety_floor_rank",
        }:
            raise ValueError("candidate route is invalid")
        if not _is_hash(route["result_hash"]) or not _is_hash(
            route["task_fingerprint"]
        ):
            raise ValueError("candidate route hash is invalid")
        _int(route["safety_floor_rank"], minimum=10, maximum=110)
        if (
            not isinstance(route["lane"], str)
            or not isinstance(route["model"], str)
            or not isinstance(route["effort"], str)
        ):
            raise TypeError("candidate route label is invalid")
        if _candidate_binding_hash(candidate) not in trusted_bindings:
            return None, "lease_scope_binding_invalid"
        if route["result_hash"] not in trusted_routes:
            return None, "quota_receipt_invalid"
        if candidate["pool"] == "main":
            if candidate["spark_binding"] is not None:
                return None, "quota_receipt_invalid"
            return candidate, None
        kind, reason = _v2_spark_binding_kind(
            candidate, trusted_alternate_bindings=trusted_alternate_bindings
        )
        if kind is None:
            return None, reason
        return candidate, None
    except (KeyError, TypeError, ValueError):
        return None, "quota_receipt_invalid"


def _receipt_matches(
    receipt_value: object,
    *,
    candidate: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    decision_hash: str,
    evaluation: datetime,
    trusted_key_resolver: HashResolver,
) -> tuple[bool, str | None]:
    try:
        receipt = _mapping(receipt_value)
        expected = {
            "schema",
            "admission_id",
            "candidate_id",
            "decision_hash",
            "pool",
            "ledger_epoch_before",
            "ledger_epoch_after",
            "active_lease_set_hash_before",
            "active_lease_set_hash_after",
            "route_result_hash",
            "task_lease_epoch",
            "assignment_epoch",
            "assignment_token",
            "host_id_hash",
            "local_slot_id",
            "write_scope_hash",
            "input_snapshot_id",
            "issued_at_utc_z",
            "expires_at_utc_z",
            "prior_receipt_hash",
            "receipt_hash",
            "signature",
        }
        if (
            set(receipt) != expected
            or receipt["schema"] != "2718lab-devkit/global-admission-receipt-v1"
        ):
            raise ValueError("receipt is invalid")
        unsigned = {
            key: item
            for key, item in receipt.items()
            if key not in {"receipt_hash", "signature"}
        }
        if receipt["receipt_hash"] != _hash(unsigned):
            raise ValueError("receipt hash is invalid")
        signature = _mapping(receipt["signature"])
        if (
            set(signature) != {"algorithm", "key_id", "value"}
            or signature["algorithm"] != "hmac-sha256"
        ):
            raise ValueError("receipt signature is invalid")
        key = trusted_key_resolver(str(signature["key_id"]))
        if not isinstance(key, bytes) or not key:
            raise ValueError("receipt key is invalid")
        signed = {**unsigned, "receipt_hash": receipt["receipt_hash"]}
        expected_signature = hmac.new(
            key, _canonical_json(signed).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not isinstance(signature["value"], str) or not hmac.compare_digest(
            expected_signature, signature["value"]
        ):
            raise ValueError("receipt signature is invalid")
        capacity = _mapping(snapshot["capacity"])
        route = _mapping(candidate["route_lock"])
        exact_pairs = {
            "candidate_id": candidate["candidate_id"],
            "decision_hash": decision_hash,
            "pool": candidate["pool"],
            "ledger_epoch_before": capacity["ledger_epoch"],
            "ledger_epoch_after": int(capacity["ledger_epoch"]) + 1,
            "active_lease_set_hash_before": capacity["active_lease_set_hash"],
            "route_result_hash": route["result_hash"],
            "task_lease_epoch": candidate["task_lease_epoch"],
            "assignment_epoch": candidate["assignment_epoch"],
            "assignment_token": candidate["assignment_token"],
            "host_id_hash": candidate["host_id_hash"],
            "local_slot_id": candidate["local_slot_id"],
            "write_scope_hash": candidate["write_scope_hash"],
            "input_snapshot_id": candidate["input_snapshot_id"],
        }
        if any(receipt[key] != value for key, value in exact_pairs.items()):
            raise ValueError("receipt binding is invalid")
        issued = _utc(receipt["issued_at_utc_z"])
        expires = _utc(receipt["expires_at_utc_z"])
        if not issued <= evaluation <= expires:
            raise ValueError("receipt is expired")
        if not _is_hash(receipt["admission_id"]) or not _is_hash(
            receipt["active_lease_set_hash_after"]
        ):
            raise ValueError("receipt hashes are invalid")
        return True, str(receipt["receipt_hash"])
    except (KeyError, TypeError, ValueError):
        return False, None


def compile_quota_balance(
    request: Mapping[str, Any],
    *,
    trusted_key_resolver: HashResolver,
    evaluation_time_utc_z: str,
    verified_route_result_hashes: Iterable[str],
    verified_lease_scope_bindings: Iterable[str],
) -> dict[str, Any]:
    """Compile deterministic quota proposals without dispatching or changing routes."""

    try:
        source = _mapping(request)
    except (TypeError, ValueError):
        return _base_decision(
            status="rejected",
            snapshot_hash=None,
            ledger_epoch=None,
            main_pressure="unknown",
            global_main_target=6,
            reason_codes=("quota_usage_unknown",),
        )
    policy = _policy()
    policy_hash = _hash(policy)
    if (
        source.get("schema") != "2718lab-devkit/fastlane-quota-balance-request-v1"
        or source.get("policy_hash") != policy_hash
        or not isinstance(source.get("candidates"), list)
        or not isinstance(source.get("receipts"), list)
        or len(source["candidates"]) > _MAX_CANDIDATES
        or len(source["receipts"]) > _MAX_CANDIDATES
        or source.get("request_hash") != _normalized_request_hash(source)
    ):
        return _fail_closed(
            source, policy_hash=policy_hash, reason="quota_usage_unknown"
        )
    try:
        snapshot, evaluation = _verified_snapshot(
            source.get("snapshot"),
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
        )
    except TimeoutError:
        return _fail_closed(
            source, policy_hash=policy_hash, reason="quota_snapshot_stale"
        )
    except PermissionError:
        return _fail_closed(
            source, policy_hash=policy_hash, reason="quota_snapshot_untrusted"
        )
    except (KeyError, TypeError, ValueError):
        return _fail_closed(
            source, policy_hash=policy_hash, reason="quota_usage_unknown"
        )

    trusted_routes = {
        value for value in verified_route_result_hashes if _is_hash(value)
    }
    trusted_bindings = {
        value for value in verified_lease_scope_bindings if _is_hash(value)
    }
    valid_candidates: list[Mapping[str, Any]] = []
    route_hashes: list[str] = []
    reasons: list[str] = []
    seen_ids: set[str] = set()
    for raw_candidate in source["candidates"]:
        candidate, reason = _candidate(
            raw_candidate,
            trusted_routes=trusted_routes,
            trusted_bindings=trusted_bindings,
        )
        if candidate is None:
            if reason is not None:
                reasons.append(reason)
            continue
        candidate_id = str(candidate["candidate_id"])
        if candidate_id in seen_ids:
            reasons.append("quota_receipt_invalid")
            continue
        seen_ids.add(candidate_id)
        valid_candidates.append(candidate)
        route_hashes.append(str(_mapping(candidate["route_lock"])["result_hash"]))
    valid_candidates.sort(key=lambda item: str(item["candidate_id"]))

    pressure, target = _main_pressure(snapshot, policy)
    capacity = _mapping(snapshot["capacity"])
    main_candidates = [
        candidate
        for candidate in valid_candidates
        if candidate["pool"] == "main"
        and _mapping(candidate["route_lock"])["lane"] in {"luna", "terra", "sol"}
        and candidate["spark_binding"] is None
    ]
    main_proposals: list[str] = []
    if pressure == "paused":
        reasons.append("quota_main_paused")
    else:
        global_free = max(0, target - int(capacity["global_main_active"]))
        host_free = max(
            0,
            min(_LOCAL_SCHEDULER_CAP, int(capacity["host_main_cap"]))
            - int(capacity["host_main_active"]),
        )
        grant_count = min(global_free, host_free, len(main_candidates))
        main_proposals = [
            str(candidate["candidate_id"])
            for candidate in main_candidates[:grant_count]
        ]
        if main_candidates and global_free == 0:
            reasons.append("quota_global_capacity_exhausted")
        if main_candidates and host_free == 0:
            reasons.append("quota_host_capacity_exhausted")
    reasons.append(f"quota_main_{pressure}")

    spark_proposals: list[str] = []
    spark_candidates = [
        candidate for candidate in valid_candidates if candidate["pool"] == "spark"
    ]
    if spark_candidates:
        spark = _mapping(snapshot["spark"])
        main = _mapping(snapshot["main"])
        spark_limits = _mapping(policy["spark"])
        eligible_spark = [
            candidate
            for candidate in spark_candidates
            if _valid_spark_binding(candidate)
        ]
        if not eligible_spark:
            reasons.append("spark_candidate_ineligible")
        elif int(capacity["global_spark_active"]) >= int(
            spark_limits["global_concurrency_cap"]
        ) or int(capacity["host_spark_active"]) >= int(
            spark_limits["host_concurrency_cap"]
        ):
            reasons.append("spark_cap_hold")
        elif int(main["used_ppm"]) - int(spark["used_ppm"]) <= int(
            spark_limits["usage_deadband_ppm"]
        ):
            reasons.append("spark_deadband_hold")
        elif int(main["delta_ppm_300s"]) - int(spark["delta_ppm_300s"]) < -int(
            spark_limits["slope_deadband_ppm_300s"]
        ):
            reasons.append("spark_slope_hold")
        elif int(spark["used_ppm"]) >= min(
            int(spark_limits["absolute_cap_ppm"]),
            int(main["used_ppm"]) + int(spark_limits["usage_deadband_ppm"]),
        ):
            reasons.append("spark_cap_hold")
        else:
            spark_proposals = [str(eligible_spark[0]["candidate_id"])]
            reasons.append("spark_follow_lag")

    provisional = _base_decision(
        status="resolved",
        snapshot_hash=str(snapshot["snapshot_hash"]),
        ledger_epoch=int(capacity["ledger_epoch"]),
        main_pressure=pressure,
        global_main_target=target,
        main_proposal_ids=main_proposals,
        spark_proposal_ids=spark_proposals,
        held_candidate_ids=_candidate_ids(source["candidates"]),
        route_lock_hashes=route_hashes,
        reason_codes=reasons,
        policy_hash=policy_hash,
        prior_audit_hash=(
            source.get("prior_audit_hash")
            if _is_hash(source.get("prior_audit_hash"))
            else None
        ),
    )
    proposed_ids = set(main_proposals) | set(spark_proposals)
    candidate_by_id = {
        str(candidate["candidate_id"]): candidate for candidate in valid_candidates
    }
    admitted: list[str] = []
    receipt_hashes: list[str] = []
    invalid_receipt = False
    for receipt in source["receipts"]:
        if not isinstance(receipt, Mapping):
            invalid_receipt = True
            continue
        candidate_id = receipt.get("candidate_id")
        candidate = candidate_by_id.get(str(candidate_id))
        if candidate is None or str(candidate_id) not in proposed_ids:
            invalid_receipt = True
            continue
        valid, receipt_hash = _receipt_matches(
            receipt,
            candidate=candidate,
            snapshot=snapshot,
            decision_hash=str(provisional["decision_hash"]),
            evaluation=evaluation,
            trusted_key_resolver=trusted_key_resolver,
        )
        if not valid or receipt_hash is None or str(candidate_id) in admitted:
            invalid_receipt = True
            continue
        admitted.append(str(candidate_id))
        receipt_hashes.append(receipt_hash)
    if proposed_ids and len(admitted) < len(proposed_ids):
        reasons.append("quota_receipt_required")
    if invalid_receipt:
        reasons.append("quota_receipt_invalid")
    all_ids = _candidate_ids(source["candidates"])
    return _base_decision(
        status="resolved",
        snapshot_hash=str(snapshot["snapshot_hash"]),
        ledger_epoch=int(capacity["ledger_epoch"]),
        main_pressure=pressure,
        global_main_target=target,
        main_proposal_ids=main_proposals,
        spark_proposal_ids=spark_proposals,
        admitted_candidate_ids=admitted,
        held_candidate_ids=(
            candidate_id for candidate_id in all_ids if candidate_id not in admitted
        ),
        route_lock_hashes=route_hashes,
        reason_codes=reasons,
        policy_hash=policy_hash,
        prior_audit_hash=(
            source.get("prior_audit_hash")
            if _is_hash(source.get("prior_audit_hash"))
            else None
        ),
        receipt_hashes=receipt_hashes,
    )


def compile_quota_balance_v2(
    request: Mapping[str, Any],
    *,
    trusted_key_resolver: HashResolver,
    evaluation_time_utc_z: str,
    verified_route_result_hashes: Iterable[str],
    verified_lease_scope_bindings: Iterable[str],
    verified_spark_alternate_binding_hashes: Iterable[str],
) -> dict[str, Any]:
    """Compile v2 quota proposals with a bounded normal Spark alternate."""

    try:
        source = _mapping(request)
    except (TypeError, ValueError):
        return _base_decision_v2(
            status="rejected",
            snapshot_hash=None,
            ledger_epoch=None,
            main_pressure="unknown",
            global_main_target=6,
            reason_codes=("quota_usage_unknown",),
        )
    policy = _policy_v2()
    policy_hash = _hash(policy)
    if (
        source.get("schema") != "2718lab-devkit/fastlane-quota-balance-request-v2"
        or source.get("policy_hash") != policy_hash
        or not isinstance(source.get("candidates"), list)
        or not isinstance(source.get("receipts"), list)
        or len(source["candidates"]) > _MAX_CANDIDATES
        or len(source["receipts"]) > _MAX_CANDIDATES
        or source.get("request_hash") != _normalized_request_hash(source)
    ):
        return _fail_closed_v2(
            source, policy_hash=policy_hash, reason="quota_usage_unknown"
        )
    try:
        snapshot, evaluation = _verified_snapshot(
            source.get("snapshot"),
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
        )
    except TimeoutError:
        return _fail_closed_v2(
            source, policy_hash=policy_hash, reason="quota_snapshot_stale"
        )
    except PermissionError:
        return _fail_closed_v2(
            source, policy_hash=policy_hash, reason="quota_snapshot_untrusted"
        )
    except (KeyError, TypeError, ValueError):
        return _fail_closed_v2(
            source, policy_hash=policy_hash, reason="quota_usage_unknown"
        )

    trusted_routes = {
        value for value in verified_route_result_hashes if _is_hash(value)
    }
    trusted_bindings = {
        value for value in verified_lease_scope_bindings if _is_hash(value)
    }
    trusted_alternate_bindings = {
        value
        for value in verified_spark_alternate_binding_hashes
        if _is_hash(value)
    }
    valid_candidates: list[Mapping[str, Any]] = []
    route_hashes: list[str] = []
    reasons: list[str] = []
    seen_ids: set[str] = set()
    for raw_candidate in source["candidates"]:
        candidate, reason = _candidate_v2(
            raw_candidate,
            trusted_routes=trusted_routes,
            trusted_bindings=trusted_bindings,
            trusted_alternate_bindings=trusted_alternate_bindings,
        )
        if candidate is None:
            if reason is not None:
                reasons.append(reason)
            continue
        candidate_id = str(candidate["candidate_id"])
        if candidate_id in seen_ids:
            reasons.append("quota_receipt_invalid")
            continue
        seen_ids.add(candidate_id)
        valid_candidates.append(candidate)
        route_hashes.append(str(_mapping(candidate["route_lock"])["result_hash"]))
    valid_candidates.sort(key=lambda item: str(item["candidate_id"]))

    pressure, target = _main_pressure(snapshot, policy)
    capacity = _mapping(snapshot["capacity"])
    main_candidates = [
        candidate
        for candidate in valid_candidates
        if candidate["pool"] == "main"
        and _mapping(candidate["route_lock"])["lane"] in {"luna", "terra", "sol"}
        and candidate["spark_binding"] is None
    ]
    main_proposals: list[str] = []
    if pressure == "paused":
        reasons.append("quota_main_paused")
    else:
        global_free = max(0, target - int(capacity["global_main_active"]))
        host_free = max(
            0,
            min(_LOCAL_SCHEDULER_CAP, int(capacity["host_main_cap"]))
            - int(capacity["host_main_active"]),
        )
        grant_count = min(global_free, host_free, len(main_candidates))
        main_proposals = [
            str(candidate["candidate_id"])
            for candidate in main_candidates[:grant_count]
        ]
        if main_candidates and global_free == 0:
            reasons.append("quota_global_capacity_exhausted")
        if main_candidates and host_free == 0:
            reasons.append("quota_host_capacity_exhausted")
    reasons.append(f"quota_main_{pressure}")

    spark_proposals: list[str] = []
    spark_candidates: list[tuple[Mapping[str, Any], str]] = []
    for candidate in valid_candidates:
        if candidate["pool"] != "spark":
            continue
        kind, reason = _v2_spark_binding_kind(
            candidate, trusted_alternate_bindings=trusted_alternate_bindings
        )
        if kind is None:
            if reason is not None:
                reasons.append(reason)
            continue
        spark_candidates.append((candidate, kind))
    spark_candidates.sort(
        key=lambda item: (
            0 if item[1] == "static" else 1,
            str(item[0]["candidate_id"]),
        )
    )
    if spark_candidates:
        spark = _mapping(snapshot["spark"])
        main = _mapping(snapshot["main"])
        spark_limits = _mapping(policy["spark"])
        alternate_limits = _mapping(policy["spark_alternate"])
        has_static = any(kind == "static" for _, kind in spark_candidates)
        has_alternate = any(kind == "alternate" for _, kind in spark_candidates)
        if has_static and has_alternate:
            reasons.append("spark_static_priority")
        selected, selected_kind = spark_candidates[0]
        if (
            selected_kind == "alternate"
            and int(capacity["host_spark_cap"])
            != int(alternate_limits["required_local_slot_count"])
        ):
            reasons.append("spark_alternate_slot_hold")
        elif int(capacity["global_spark_active"]) >= int(
            spark_limits["global_concurrency_cap"]
        ) or int(capacity["host_spark_active"]) >= int(
            spark_limits["host_concurrency_cap"]
        ):
            reasons.append("spark_cap_hold")
        elif int(main["used_ppm"]) - int(spark["used_ppm"]) <= int(
            spark_limits["usage_deadband_ppm"]
        ):
            reasons.append("spark_deadband_hold")
        elif int(main["delta_ppm_300s"]) - int(spark["delta_ppm_300s"]) < -int(
            spark_limits["slope_deadband_ppm_300s"]
        ):
            reasons.append("spark_slope_hold")
        elif int(spark["used_ppm"]) >= min(
            int(spark_limits["absolute_cap_ppm"]),
            int(main["used_ppm"]) + int(spark_limits["usage_deadband_ppm"]),
        ):
            reasons.append("spark_cap_hold")
        else:
            cap = int(alternate_limits["single_card_cap"])
            spark_proposals = [str(selected["candidate_id"])][:cap]
            reasons.append(
                "spark_follow_lag"
                if selected_kind == "static"
                else "spark_alternate_headroom"
            )

    provisional = _base_decision_v2(
        status="resolved",
        snapshot_hash=str(snapshot["snapshot_hash"]),
        ledger_epoch=int(capacity["ledger_epoch"]),
        main_pressure=pressure,
        global_main_target=target,
        main_proposal_ids=main_proposals,
        spark_proposal_ids=spark_proposals,
        held_candidate_ids=_candidate_ids(source["candidates"]),
        route_lock_hashes=route_hashes,
        reason_codes=reasons,
        policy_hash=policy_hash,
        prior_audit_hash=(
            source.get("prior_audit_hash")
            if _is_hash(source.get("prior_audit_hash"))
            else None
        ),
    )
    proposed_ids = set(main_proposals) | set(spark_proposals)
    candidate_by_id = {
        str(candidate["candidate_id"]): candidate for candidate in valid_candidates
    }
    admitted: list[str] = []
    receipt_hashes: list[str] = []
    invalid_receipt = False
    for receipt in source["receipts"]:
        if not isinstance(receipt, Mapping):
            invalid_receipt = True
            continue
        candidate_id = receipt.get("candidate_id")
        candidate = candidate_by_id.get(str(candidate_id))
        if candidate is None or str(candidate_id) not in proposed_ids:
            invalid_receipt = True
            continue
        valid, receipt_hash = _receipt_matches(
            receipt,
            candidate=candidate,
            snapshot=snapshot,
            decision_hash=str(provisional["decision_hash"]),
            evaluation=evaluation,
            trusted_key_resolver=trusted_key_resolver,
        )
        if not valid or receipt_hash is None or str(candidate_id) in admitted:
            invalid_receipt = True
            continue
        admitted.append(str(candidate_id))
        receipt_hashes.append(receipt_hash)
    if proposed_ids and len(admitted) < len(proposed_ids):
        reasons.append("quota_receipt_required")
    if invalid_receipt:
        reasons.append("quota_receipt_invalid")
    all_ids = _candidate_ids(source["candidates"])
    return _base_decision_v2(
        status="resolved",
        snapshot_hash=str(snapshot["snapshot_hash"]),
        ledger_epoch=int(capacity["ledger_epoch"]),
        main_pressure=pressure,
        global_main_target=target,
        main_proposal_ids=main_proposals,
        spark_proposal_ids=spark_proposals,
        admitted_candidate_ids=admitted,
        held_candidate_ids=(
            candidate_id for candidate_id in all_ids if candidate_id not in admitted
        ),
        route_lock_hashes=route_hashes,
        reason_codes=reasons,
        policy_hash=policy_hash,
        prior_audit_hash=(
            source.get("prior_audit_hash")
            if _is_hash(source.get("prior_audit_hash"))
            else None
        ),
        receipt_hashes=receipt_hashes,
    )


def _main_capacity_evidence(
    *,
    status: str,
    snapshot_hash: str | None,
    decision_hash: str | None,
    ledger_epoch: int | None,
    global_main_target: int | None,
    global_main_active: int | None,
    host_main_active: int | None,
    active_lease_set_hash: str | None,
    reason_codes: Iterable[str],
) -> dict[str, Any]:
    reasons = sorted(set(reason_codes))[:_MAX_REASON_CODES]
    global_main_free = (
        None
        if global_main_target is None or global_main_active is None
        else max(0, global_main_target - global_main_active)
    )
    evidence = {
        "schema": "2718lab-devkit/fastlane-main-capacity-evidence-v1",
        "status": status,
        "snapshot_hash": snapshot_hash,
        "decision_hash": decision_hash,
        "ledger_epoch": ledger_epoch,
        "global_main_target": global_main_target,
        "global_main_active": global_main_active,
        "global_main_free": global_main_free,
        "host_main_active": host_main_active,
        "active_lease_set_hash": active_lease_set_hash,
        "reason_codes": reasons,
    }
    return {**evidence, "evidence_hash": _hash(evidence)}


def compile_main_capacity_evidence(
    request: Mapping[str, Any],
    *,
    trusted_key_resolver: HashResolver,
    evaluation_time_utc_z: str,
    verified_route_result_hashes: Iterable[str],
    verified_lease_scope_bindings: Iterable[str],
) -> dict[str, Any]:
    """Return only verified main-pool capacity facts for inert host planning."""

    try:
        decision = compile_quota_balance(
            request,
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
            verified_route_result_hashes=verified_route_result_hashes,
            verified_lease_scope_bindings=verified_lease_scope_bindings,
        )
    except (OSError, TypeError, ValueError):
        return _main_capacity_evidence(
            status="blocked",
            snapshot_hash=None,
            decision_hash=None,
            ledger_epoch=None,
            global_main_target=None,
            global_main_active=None,
            host_main_active=None,
            active_lease_set_hash=None,
            reason_codes=("quota_usage_unknown",),
        )

    reasons = decision.get("reason_codes", [])
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        reasons = ["quota_usage_unknown"]
    if decision.get("status") != "resolved":
        return _main_capacity_evidence(
            status="blocked",
            snapshot_hash=None,
            decision_hash=None,
            ledger_epoch=None,
            global_main_target=None,
            global_main_active=None,
            host_main_active=None,
            active_lease_set_hash=None,
            reason_codes=reasons,
        )

    try:
        snapshot, _ = _verified_snapshot(
            request.get("snapshot"),
            trusted_key_resolver=trusted_key_resolver,
            evaluation_time_utc_z=evaluation_time_utc_z,
        )
        capacity = _mapping(snapshot["capacity"])
        target = decision["global_main_target"]
        ledger_epoch = decision["ledger_epoch"]
        snapshot_hash = decision["snapshot_hash"]
        decision_hash = decision["decision_hash"]
        if (
            target not in {6, 8, 10, 12}
            or type(ledger_epoch) is not int
            or not _is_hash(snapshot_hash)
            or not _is_hash(decision_hash)
            or snapshot_hash != snapshot["snapshot_hash"]
            or ledger_epoch != capacity["ledger_epoch"]
        ):
            raise ValueError("main capacity evidence is inconsistent")
    except (KeyError, TypeError, ValueError, PermissionError, TimeoutError):
        return _main_capacity_evidence(
            status="blocked",
            snapshot_hash=None,
            decision_hash=None,
            ledger_epoch=None,
            global_main_target=None,
            global_main_active=None,
            host_main_active=None,
            active_lease_set_hash=None,
            reason_codes=("quota_usage_unknown",),
        )

    blocked_reasons = {
        "quota_main_paused",
        "quota_receipt_invalid",
        "quota_receipt_required",
        "quota_snapshot_stale",
        "quota_snapshot_untrusted",
        "quota_usage_unknown",
    }
    return _main_capacity_evidence(
        status="blocked" if blocked_reasons.intersection(reasons) else "resolved",
        snapshot_hash=snapshot_hash,
        decision_hash=decision_hash,
        ledger_epoch=ledger_epoch,
        global_main_target=target,
        global_main_active=capacity["global_main_active"],
        host_main_active=capacity["host_main_active"],
        active_lease_set_hash=capacity["active_lease_set_hash"],
        reason_codes=reasons,
    )


def evaluate_coordinator_kernel(
    input_value: Mapping[str, Any],
    trusted_capability_hashes: Iterable[str],
    trusted_sol_lock_hashes: Iterable[str],
    event_seq: int,
) -> dict[str, Any]:
    """Apply the closed Luna Max mechanical gate without spawning or escalating."""

    source = _mapping(input_value)
    operation = source.get("operation")
    operation_hash = source.get("operation_input_hash")
    profile = source.get("profile")
    capability_hashes = {
        value for value in trusted_capability_hashes if _is_hash(value)
    }
    profile_ok = (
        isinstance(profile, Mapping)
        and profile.get("model") == "gpt-5.6-luna"
        and profile.get("effort") == "max"
        and profile.get("capability_attestation_hash") in capability_hashes
    )
    if not profile_ok:
        return _kernel_decision(
            "PROFILE_UNAVAILABLE",
            operation,
            operation_hash,
            None,
            ("coordinator_profile_unavailable",),
        )
    if operation not in _VALID_OPERATIONS or not _is_hash(operation_hash):
        return _kernel_decision(
            "NEEDS_SOL_LOCK", operation, operation_hash, None, ("needs_sol_lock",)
        )
    needs_lock = (
        source.get("intake_shape") != "structured"
        or any(source.get(flag) is True for flag in _SEMANTIC_FLAGS)
        or source.get("mechanical_binding_complete") is not True
        or operation in {"final_acceptance", "archive"}
    )
    if not needs_lock:
        return _kernel_decision(
            "MECHANICAL_ALLOWED", operation, operation_hash, None, ()
        )
    allowed, reason, lock_hash = _valid_sol_lock(
        source.get("sol_lock"),
        operation=str(operation),
        operation_hash=str(operation_hash),
        trusted_lock_hashes={
            value for value in trusted_sol_lock_hashes if _is_hash(value)
        },
        event_seq=event_seq,
    )
    return _kernel_decision(
        "MECHANICAL_ALLOWED" if allowed else "NEEDS_SOL_LOCK",
        operation,
        operation_hash,
        lock_hash if allowed else None,
        () if allowed else (reason,),
    )


def _valid_sol_lock(
    value: object,
    *,
    operation: str,
    operation_hash: str,
    trusted_lock_hashes: set[str],
    event_seq: int,
) -> tuple[bool, str, str | None]:
    if not isinstance(value, Mapping):
        return False, "needs_sol_lock", None
    required = {
        "schema",
        "operation",
        "operation_input_hash",
        "contract_set_hash",
        "policy_set_hash",
        "evidence_set_hash",
        "decision",
        "reviewer_model",
        "reviewer_role",
        "issued_event_seq",
        "expires_event_seq",
        "predecessor_hash",
        "signature_receipt_hash",
        "lock_hash",
    }
    if set(value) != required:
        return False, "sol_lock_binding_invalid", None
    lock_hash = value.get("lock_hash")
    unsigned = {key: item for key, item in value.items() if key != "lock_hash"}
    if (
        not _is_hash(lock_hash)
        or lock_hash not in trusted_lock_hashes
        or lock_hash != _hash(unsigned)
        or value.get("operation") != operation
        or value.get("operation_input_hash") != operation_hash
        or value.get("decision") != "approved"
        or value.get("reviewer_model") != "gpt-5.6-sol"
        or value.get("reviewer_role") != "independent_sol"
    ):
        return False, "sol_lock_binding_invalid", None
    if (
        type(value.get("issued_event_seq")) is not int
        or type(value.get("expires_event_seq")) is not int
        or not value["issued_event_seq"] <= event_seq <= value["expires_event_seq"]
    ):
        return False, "sol_lock_stale", None
    for key in (
        "contract_set_hash",
        "policy_set_hash",
        "evidence_set_hash",
        "signature_receipt_hash",
    ):
        if not _is_hash(value.get(key)):
            return False, "sol_lock_binding_invalid", None
    if value["predecessor_hash"] is not None and not _is_hash(
        value["predecessor_hash"]
    ):
        return False, "sol_lock_binding_invalid", None
    return True, "", str(lock_hash)


def _kernel_decision(
    status: str,
    operation: object,
    operation_hash: object,
    lock_hash: str | None,
    reason_codes: Iterable[str],
) -> dict[str, Any]:
    decision = {
        "schema": "2718lab-devkit/coordinator-kernel-result-v1",
        "status": status,
        "operation": operation,
        "operation_input_hash": operation_hash,
        "sol_lock_hash": lock_hash,
        "reason_codes": sorted(set(reason_codes)),
    }
    return {**decision, "decision_hash": _hash(decision)}
