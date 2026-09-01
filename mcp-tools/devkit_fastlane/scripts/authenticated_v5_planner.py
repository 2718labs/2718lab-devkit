"""Pure authenticated Fast Lane V5 request and compiler-evidence helpers."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_UNIT_FIELDS = frozenset(
    {
        "task",
        "dependency_state",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "predecessor_hash",
        "storage_budget",
    }
)
_LEGACY_UNIT_FIELDS = _UNIT_FIELDS - {"storage_budget"}
_BUDGET_UNIT_FIELDS = _LEGACY_UNIT_FIELDS | {"storage_budget"}
_CONTEXTUAL_UNIT_FIELDS = (_UNIT_FIELDS - {"predecessor_hash"}) | {"workflow_id_hash"}
_LEGACY_CONTEXTUAL_UNIT_FIELDS = _CONTEXTUAL_UNIT_FIELDS - {"storage_budget"}
_BUDGET_CONTEXTUAL_UNIT_FIELDS = _LEGACY_CONTEXTUAL_UNIT_FIELDS | {"storage_budget"}
_ATTESTATION_ITEM_FIELDS = frozenset({"task_id", "request_binding_hash", "attestation"})
_CONCURRENCY_MODES = frozenset({"parallel", "serial", "isolated_worktree"})
_STORAGE_INTENT_SCHEMA = "2718lab.storage.intent.v1"
_STORAGE_TARGET_SCHEMA = "2718lab.storage.target.v1"
_STORAGE_POLICY_MISSING = "STORAGE_POLICY_MISSING"
_STORAGE_TARGET_KEY_INVALID = "STORAGE_TARGET_KEY_INVALID"
_STORAGE_DESCRIPTOR_FIELDS = (
    "repository_identity",
    "workspace_manifest_hash",
    "cargo_lock_hash",
    "toolchain_digest",
    "target_triple",
    "profile",
    "features_hash",
    "build_env_class",
)
_STORAGE_PROFILE_FIELDS = frozenset(
    {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "task_id",
        "source_plan_hash",
        "index_attestation_hash",
        "execution_context_hash",
        *_STORAGE_DESCRIPTOR_FIELDS,
        "profile_hash",
        "attestation_hash",
    }
)
_STORAGE_PROFILE_SCHEMA = "2718lab-devkit/storage-profile-v1"
_STORAGE_PROFILE_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_PREPARATION_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SKELETON_FIELDS = frozenset(
    {
        "task_id",
        "routing_proof",
        "write_scope",
        "concurrency_mode",
        "dispatch_order",
        "index_context_hash",
        "predecessor_hash",
        "source_plan_hash",
    }
)
_PROFILE_EVIDENCE_SCHEMA = "team-efficiency/fast-lane-v5-profile-evidence-v1"
_PROFILE_UNIT_FIELDS = (
    "task",
    "dependency_state",
    "write_scope",
    "concurrency_mode",
    "dispatch_order",
    "index_context_hash",
    "workflow_id_hash",
)


def _storage_budget(
    source_unit: Mapping[str, Any],
) -> tuple[int, int]:
    budget = source_unit.get("storage_budget")
    if not isinstance(budget, Mapping):
        raise ValueError(_STORAGE_POLICY_MISSING)
    requested_bytes = budget.get("bytes")
    requested_files = budget.get("files")
    if (
        type(requested_bytes) is not int
        or requested_bytes <= 0
        or requested_bytes > (1 << 64) - 1
        or type(requested_files) is not int
        or requested_files <= 0
        or requested_files > (1 << 64) - 1
    ):
        raise ValueError(_STORAGE_POLICY_MISSING)
    return requested_bytes, requested_files


def _normalize_host_storage_profile(
    api: Any,
    value: object,
    *,
    task_id: str,
    source_plan_hash: str,
) -> dict[str, Any]:
    """Normalize one profile emitted by the private Host.

    The compiler deliberately accepts only the closed profile shape.  Host
    request/ref/routing bindings are validated by ``host_bridge`` before this
    helper is called; this second check protects callers that use the pure
    planner/compiler helper directly.
    """

    profile = api._mapping(value, f"Host storage profile {task_id}")
    if set(profile) != _STORAGE_PROFILE_FIELDS:
        raise ValueError(_STORAGE_POLICY_MISSING)
    if (
        profile.get("schema") != _STORAGE_PROFILE_SCHEMA
        or profile.get("task_id") != task_id
        or profile.get("source_plan_hash") != source_plan_hash
    ):
        raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    digest_fields = {
        "index_attestation_hash",
        "execution_context_hash",
        "repository_identity",
        "workspace_manifest_hash",
        "cargo_lock_hash",
        "toolchain_digest",
        "features_hash",
    }
    for field in (
        "index_attestation_hash",
        "execution_context_hash",
        *_STORAGE_DESCRIPTOR_FIELDS,
    ):
        item = profile.get(field)
        if type(item) is not str:
            raise ValueError(_STORAGE_POLICY_MISSING)
        if field in digest_fields:
            try:
                api._hash(item, f"Host storage profile {task_id}.{field}")
            except Exception as error:
                raise ValueError(_STORAGE_TARGET_KEY_INVALID) from error
        if field == "target_triple" and _STORAGE_PROFILE_SCALAR.fullmatch(item) is None:
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
        if field == "profile" and item != "dev":
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
        if field == "build_env_class" and item not in {
            "managed_read_only",
            "managed_workspace",
            "disabled",
            "external",
        }:
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    call_intent_hash = profile.get("call_intent_hash")
    preparation_id = profile.get("preparation_id")
    if (
        type(call_intent_hash) is not str
        or len(call_intent_hash) != 64
        or any(character not in "0123456789abcdef" for character in call_intent_hash)
        or type(preparation_id) is not str
        or _PREPARATION_ID.fullmatch(preparation_id) is None
    ):
        raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    for field in ("profile_hash", "attestation_hash"):
        item = profile.get(field)
        try:
            api._hash(item, f"Host storage profile {task_id}.{field}")
        except Exception as error:
            raise ValueError(_STORAGE_TARGET_KEY_INVALID) from error
    unsigned = {
        key: profile[key]
        for key in _STORAGE_PROFILE_FIELDS
        if key not in {"profile_hash", "attestation_hash"}
    }
    expected_binding = api._sha256_json(unsigned)
    if not hmac.compare_digest(str(profile["profile_hash"]), expected_binding):
        raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    # The complete attestation preimage is bound to the private request,
    # routing result, and index reference by ``host_bridge``.  This pure
    # compiler helper deliberately cannot recreate those Host-only facts; it
    # still requires the attestation to be a canonical digest and never treats
    # a caller-supplied descriptor as evidence.
    return {key: profile[key] for key in sorted(_STORAGE_PROFILE_FIELDS)}


def _make_storage_intent(
    api: Any,
    source_unit: Mapping[str, Any],
    *,
    task_id: str,
    source_plan_hash: object,
    profile: Mapping[str, Any],
) -> dict[str, object]:
    """Build one path-free intent from caller budget and Host facts."""

    source_hash = api._hash(source_plan_hash, "source_plan_hash")
    if set(profile) != _STORAGE_PROFILE_FIELDS:
        raise ValueError(_STORAGE_POLICY_MISSING)
    if profile.get("task_id") != task_id:
        raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    context_hash = api._hash(
        profile.get("execution_context_hash"),
        f"Host storage profile {task_id}.execution_context_hash",
    )
    requested_bytes, requested_files = _storage_budget(source_unit)
    descriptor = {
        "schema": _STORAGE_TARGET_SCHEMA,
        "artifact_kind": "fastlane-task",
        **{field: profile.get(field) for field in _STORAGE_DESCRIPTOR_FIELDS},
    }
    if any(descriptor[field] is None for field in _STORAGE_DESCRIPTOR_FIELDS):
        raise ValueError(_STORAGE_POLICY_MISSING)
    intent_without_hash = {
        "schema": _STORAGE_INTENT_SCHEMA,
        "task_id": task_id,
        "plan_binding": source_hash,
        "context_hash": context_hash,
        "requested_bytes": requested_bytes,
        "requested_files": requested_files,
        "target_descriptor": descriptor,
    }
    intent = {
        **intent_without_hash,
        "storage_intent_hash": api._sha256_json(
            {
                "target_descriptor": descriptor,
                "task_id": task_id,
                "plan_binding": source_hash,
                "context_hash": context_hash,
                "requested_bytes": requested_bytes,
                "requested_files": requested_files,
            }
        ),
    }
    try:
        from devkit_runtime.storage_intent import parse_storage_intent

        return parse_storage_intent(intent).to_dict()
    except Exception as error:
        code = getattr(error, "code", _STORAGE_TARGET_KEY_INVALID)
        raise ValueError(code) from error


def _unit_storage_intent(
    api: Any,
    unit: Mapping[str, Any],
    *,
    task_id: str,
    source_plan_hash: str,
    profile: Mapping[str, Any],
) -> dict[str, object]:
    # A pre-bound intent is not permission to invent a budget at compile time.
    # Every compiler unit must carry the explicit request/source-unit budget.
    _storage_budget(unit)
    return _make_storage_intent(
        api,
        unit,
        task_id=task_id,
        source_plan_hash=source_plan_hash,
        profile=profile,
    )


def _routing_profile_material(
    source_plan_hash: str, unit: Mapping[str, Any]
) -> dict[str, Any]:
    task = dict(unit["task"])
    task.pop("profile_evidence_hash", None)
    fields = _PROFILE_UNIT_FIELDS + (
        ("storage_budget",) if "storage_budget" in unit else ()
    )
    return {
        "schema": _PROFILE_EVIDENCE_SCHEMA,
        "source_plan_hash": source_plan_hash,
        "unit": {field: task if field == "task" else unit[field] for field in fields},
    }


def owned_scope_hash(api: Any, task_id: object, write_scope: object) -> str:
    normalized_task_id = api._task_id(task_id, "authenticated V5 task_id")
    normalized_scope = api._normalised_scopes(
        write_scope, "authenticated V5 write_scope"
    )
    return api._sha256_json(
        {
            "schema": "2718lab-devkit/owned-write-scope-v1",
            "task_id": normalized_task_id,
            "write_scope": normalized_scope,
        }
    )


def normalize_units(
    api: Any, units: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if (
        not isinstance(units, Sequence)
        or isinstance(units, (str, bytes, bytearray))
        or not 1 <= len(units) <= 16
    ):
        raise ValueError("authenticated V5 units are out of bounds")
    normalized: list[dict[str, Any]] = []
    for index, raw_unit in enumerate(units):
        unit = api._mapping(raw_unit, f"authenticated V5 units[{index}]")
        if set(unit) not in {
            _UNIT_FIELDS,
            _CONTEXTUAL_UNIT_FIELDS,
            _LEGACY_UNIT_FIELDS,
            _LEGACY_CONTEXTUAL_UNIT_FIELDS,
            _BUDGET_UNIT_FIELDS,
            _BUDGET_CONTEXTUAL_UNIT_FIELDS,
        }:
            raise ValueError(f"authenticated V5 units[{index}] has unsupported fields")
        task = dict(api._mapping(unit["task"], f"authenticated V5 units[{index}].task"))
        api._task_id(
            task.get("task_id"), f"authenticated V5 units[{index}].task.task_id"
        )
        write_scope = api._normalised_scopes(
            unit["write_scope"], f"authenticated V5 units[{index}].write_scope"
        )
        if (
            task.get("schema") != "2718lab-devkit/task-routing-profile-v5"
            or task.get("role") != "execution"
            or task.get("access") != "workspace_write"
            or task.get("write_scope_count") != len(write_scope)
            or task.get("overlap_risk") != "none"
            or task.get("overlap_count") != 0
        ):
            raise ValueError("authenticated V5 task is not an owned writer")
        concurrency_mode = api._text(
            unit["concurrency_mode"],
            f"authenticated V5 units[{index}].concurrency_mode",
            maximum=32,
        )
        dispatch_order = unit["dispatch_order"]
        if (
            concurrency_mode not in _CONCURRENCY_MODES
            or type(dispatch_order) is not int
            or not 0 <= dispatch_order < 16
        ):
            raise ValueError("authenticated V5 dispatch facts are invalid")
        normalized_unit = {
            "task": task,
            "dependency_state": json.loads(
                api._canonical_json(unit["dependency_state"])
            ),
            "write_scope": write_scope,
            "concurrency_mode": concurrency_mode,
            "dispatch_order": dispatch_order,
            "index_context_hash": api._hash(
                unit["index_context_hash"],
                f"authenticated V5 units[{index}].index_context_hash",
            ),
            "predecessor_hash": (
                api._hash(
                    unit["predecessor_hash"],
                    f"authenticated V5 units[{index}].predecessor_hash",
                )
                if "predecessor_hash" in unit
                else None
            ),
            "workflow_id_hash": (
                api._hash(
                    unit["workflow_id_hash"],
                    f"authenticated V5 units[{index}].workflow_id_hash",
                )
                if "workflow_id_hash" in unit
                else None
            ),
        }
        if "storage_budget" in unit:
            budget = unit["storage_budget"]
            if not isinstance(budget, Mapping) or set(budget) != {"bytes", "files"}:
                raise ValueError(_STORAGE_POLICY_MISSING)
            requested_bytes = budget.get("bytes")
            requested_files = budget.get("files")
            if (
                type(requested_bytes) is not int
                or not 0 < requested_bytes <= (1 << 64) - 1
                or type(requested_files) is not int
                or not 0 < requested_files <= (1 << 64) - 1
            ):
                raise ValueError(_STORAGE_POLICY_MISSING)
            normalized_unit["storage_budget"] = {
                "bytes": requested_bytes,
                "files": requested_files,
            }
        normalized.append(normalized_unit)
    task_ids = [str(item["task"]["task_id"]) for item in normalized]
    orders = [int(item["dispatch_order"]) for item in normalized]
    if (
        len(set(task_ids)) != len(task_ids)
        or len(set(orders)) != len(orders)
        or orders != sorted(orders)
    ):
        raise ValueError("authenticated V5 units contain duplicate bindings")
    return normalized


def prepare_requests(
    api: Any,
    units: Sequence[Mapping[str, Any]],
    *,
    source_plan_hash: object,
    host_capabilities: Mapping[str, Any],
    scheduler_facts: Mapping[str, Any],
) -> list[dict[str, Any]]:
    api._hash(source_plan_hash, "source_plan_hash")
    core = api._fast_lane_routing_core()
    if core is None:
        raise ValueError("authenticated V5 routing core is unavailable")
    normalized_units = normalize_units(api, units)
    policy = core.load_policy_v5()
    policy_hash = core.policy_hash_v5(policy)
    canonical_host = json.loads(api._canonical_json(host_capabilities))
    canonical_scheduler = json.loads(api._canonical_json(scheduler_facts))
    requests: list[dict[str, Any]] = []
    for unit in normalized_units:
        task_id = str(unit["task"]["task_id"])
        request = {
            "schema": "2718lab-devkit/fastlane-routing-request-v5",
            "policy_hash": policy_hash,
            "task": unit["task"],
            "dependency_state": unit["dependency_state"],
            "scope_state": {
                "schema": "2718lab-devkit/scope-state-v1",
                "scope_epoch": canonical_scheduler.get("route_epoch"),
                "owned_scope_hash": owned_scope_hash(api, task_id, unit["write_scope"]),
                "conflicting_task_ids": [],
                "active_writer_task_ids": [],
            },
            "scheduler_facts": canonical_scheduler,
            "host_capabilities": canonical_host,
            "child_route_attestation": None,
            "legacy": None,
        }
        try:
            normalized_request = core._normalise_request_v5(request, policy)
        except Exception as error:
            raise ValueError("authenticated V5 routing request is invalid") from error
        if request != normalized_request:
            raise ValueError("authenticated V5 routing request is not canonical")
        requests.append(json.loads(api._canonical_json(normalized_request)))
    return requests


def compile_skeletons(
    api: Any,
    units: Sequence[Mapping[str, Any]],
    *,
    source_plan_hash: object,
    routing_requests: Sequence[Mapping[str, Any]],
    attestation_items: Sequence[Mapping[str, Any]],
    storage_profiles: object = None,
) -> dict[str, object]:
    source_hash = api._hash(source_plan_hash, "source_plan_hash")
    normalized_units = normalize_units(api, units)
    if (
        not isinstance(routing_requests, Sequence)
        or isinstance(routing_requests, (str, bytes, bytearray))
        or len(routing_requests) != len(normalized_units)
        or not isinstance(attestation_items, Sequence)
        or isinstance(attestation_items, (str, bytes, bytearray))
        or len(attestation_items) != len(normalized_units)
    ):
        raise ValueError("authenticated V5 routing response is incomplete")
    core = api._fast_lane_routing_core()
    if core is None:
        raise ValueError("authenticated V5 routing core is unavailable")
    storage_required = any("storage_budget" in unit for unit in normalized_units)
    profiles_by_task: dict[str, dict[str, Any]] = {}
    if storage_profiles is not None:
        if not storage_required or any(
            "storage_budget" not in unit for unit in normalized_units
        ):
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
        if (
            not isinstance(storage_profiles, Sequence)
            or isinstance(storage_profiles, (str, bytes, bytearray))
            or len(storage_profiles) != len(normalized_units)
        ):
            raise ValueError(_STORAGE_POLICY_MISSING)
        for unit, raw_profile in zip(normalized_units, storage_profiles, strict=True):
            task_id = str(unit["task"]["task_id"])
            profile = _normalize_host_storage_profile(
                api,
                raw_profile,
                task_id=task_id,
                source_plan_hash=source_hash,
            )
            profiles_by_task[task_id] = profile
        if len(profiles_by_task) != len(normalized_units):
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    policy = core.load_policy_v5()
    request_by_task: dict[str, dict[str, Any]] = {}
    for raw_request in routing_requests:
        request = json.loads(api._canonical_json(raw_request))
        try:
            normalized_request = core._normalise_request_v5(request, policy)
        except Exception as error:
            raise ValueError("authenticated V5 routing request is invalid") from error
        task = api._mapping(normalized_request["task"], "authenticated V5 request.task")
        task_id = api._task_id(task.get("task_id"), "authenticated V5 request.task_id")
        if (
            request != normalized_request
            or request.get("child_route_attestation") is not None
            or task_id in request_by_task
        ):
            raise ValueError(
                "authenticated V5 pre-attestation request is not canonical"
            )
        request_by_task[task_id] = normalized_request

    attestation_by_task: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(attestation_items):
        item = api._mapping(raw_item, f"authenticated V5 attestations[{index}]")
        api._exact_keys(
            item,
            _ATTESTATION_ITEM_FIELDS,
            f"authenticated V5 attestations[{index}]",
        )
        task_id = api._task_id(
            item["task_id"], f"authenticated V5 attestations[{index}].task_id"
        )
        request = request_by_task.get(task_id)
        if request is None or task_id in attestation_by_task:
            raise ValueError("authenticated V5 attestation task binding is invalid")
        binding_hash = api._hash(
            item["request_binding_hash"],
            f"authenticated V5 attestations[{index}].request_binding_hash",
        )
        if not hmac.compare_digest(binding_hash, core.v5_request_binding_hash(request)):
            raise ValueError("authenticated V5 request binding is invalid")
        attestation = dict(
            api._mapping(
                item["attestation"],
                f"authenticated V5 attestations[{index}].attestation",
            )
        )
        supplied_hash = api._hash(
            attestation.get("attestation_hash"),
            f"authenticated V5 attestations[{index}].attestation_hash",
        )
        expected_hash = api._sha256_json(
            {
                key: value
                for key, value in attestation.items()
                if key != "attestation_hash"
            }
        )
        if not hmac.compare_digest(
            supplied_hash, expected_hash
        ) or not hmac.compare_digest(
            binding_hash, str(attestation.get("request_binding_hash"))
        ):
            raise ValueError("authenticated V5 Host attestation is invalid")
        attestation_by_task[task_id] = attestation

    skeletons: list[dict[str, Any]] = []
    storage_intents: list[dict[str, object]] = []
    route_pairs: set[tuple[str, str]] = set()
    for unit in normalized_units:
        task_id = str(unit["task"]["task_id"])
        profile = profiles_by_task.get(task_id)
        if storage_profiles is not None:
            _storage_budget(unit)
            if profile is None:
                raise ValueError(_STORAGE_POLICY_MISSING)
            storage_intents.append(
                _unit_storage_intent(
                    api,
                    unit,
                    task_id=task_id,
                    source_plan_hash=source_hash,
                    profile=profile,
                )
            )
        request = request_by_task.get(task_id)
        attestation = attestation_by_task.get(task_id)
        if request is None or attestation is None:
            raise ValueError("authenticated V5 routing response is incomplete")
        if request["task"] != unit["task"]:
            raise ValueError("authenticated V5 routing profile binding is invalid")
        profile_hash = api._sha256_json(
            _routing_profile_material(
                source_hash,
                unit,
            )
        )
        if not hmac.compare_digest(
            str(unit["task"].get("profile_evidence_hash")), profile_hash
        ):
            raise ValueError("authenticated V5 routing profile binding is invalid")
        attested_request = {**request, "child_route_attestation": attestation}
        try:
            normalized_request = core._normalise_request_v5(attested_request, policy)
            result = core.route_v5(normalized_request, policy=policy)
        except Exception as error:
            raise ValueError("authenticated V5 Host route is invalid") from error
        if (
            attested_request != normalized_request
            or result.get("schema") != "2718lab-devkit/fastlane-routing-result-v5"
            or result.get("status") != "resolved"
            or result.get("task_id") != task_id
        ):
            raise ValueError("authenticated V5 Host route did not resolve")
        route = api._mapping(result.get("route"), "authenticated V5 result.route")
        model = api._text(
            route.get("model"), "authenticated V5 result.route.model", maximum=64
        )
        effort = api._text(
            route.get("effort"), "authenticated V5 result.route.effort", maximum=16
        )
        if route.get("inherit_current_session_model") is not False or effort == "ultra":
            raise ValueError("authenticated V5 route is not explicit")
        context_hash = api._sha256_json(
            {
                "schema": "team-efficiency/fast-lane-routing-context-binding-v1",
                "source_plan_hash": source_hash,
                "task_id": task_id,
                "scheduler_role": normalized_request["task"]["role"],
                "routing_request_hash": api._sha256_json(normalized_request),
                "scheduler_facts_hash": api._sha256_json(
                    normalized_request["scheduler_facts"]
                ),
            }
        )
        result_hash = api._sha256_json(result)
        predecessor_hash = unit["predecessor_hash"]
        if predecessor_hash is None:
            predecessor_hash = api._sha256_json(
                {
                    "schema": "team-efficiency/fast-lane-external-lease-predecessor-v1",
                    "source_plan_hash": source_hash,
                    "workflow_id_hash": unit["workflow_id_hash"],
                    "task_id": task_id,
                    "role": normalized_request["task"]["role"],
                    "context_hash": context_hash,
                    "routing_result_hash": result_hash,
                }
            )
        skeletons.append(
            {
                "task_id": task_id,
                "routing_proof": {
                    "request": normalized_request,
                    "result": result,
                    "request_binding_hash": core.v5_request_binding_hash(request),
                    "attestation_hash": attestation["attestation_hash"],
                    "routing_context_hash": context_hash,
                    "routing_result_hash": result_hash,
                },
                "write_scope": unit["write_scope"],
                "concurrency_mode": unit["concurrency_mode"],
                "dispatch_order": unit["dispatch_order"],
                "index_context_hash": unit["index_context_hash"],
                "predecessor_hash": predecessor_hash,
                "source_plan_hash": source_hash,
            }
        )
        route_pairs.add((model, effort))
    result: dict[str, object] = {
        "assignment_skeletons": skeletons,
        "requested_route_pairs": [
            {"model": model, "effort": effort} for model, effort in sorted(route_pairs)
        ],
    }
    if storage_profiles is not None:
        result["storage_profiles"] = [
            profiles_by_task[str(unit["task"]["task_id"])] for unit in normalized_units
        ]
        result["storage_intents"] = storage_intents
    return result


def validate_skeleton_package(
    api: Any,
    source_plan_units: Sequence[Mapping[str, Any]],
    initial_skeletons: Sequence[Mapping[str, Any]],
    remaining_skeletons: Sequence[Mapping[str, Any]],
    *,
    source_plan_hash: object,
) -> str:
    """Validate the complete package before a wave is registered.

    A wave is allowed to be sparse, but the package boundary is not.  Keep
    this check separate from ``normalize_units`` because that helper is also
    used for the intentionally sparse initial/refill slices.
    """

    source_hash = api._hash(source_plan_hash, "source_plan_hash")
    source_ids: list[str] = []
    for index, raw_unit in enumerate(source_plan_units):
        unit = api._mapping(raw_unit, f"authenticated V5 source plan units[{index}]")
        task = api._mapping(
            unit.get("task"), f"authenticated V5 source plan units[{index}].task"
        )
        source_ids.append(
            api._task_id(
                task.get("task_id"),
                f"authenticated V5 source plan units[{index}].task.task_id",
            )
        )
    if not 1 <= len(source_ids) <= 16 or len(set(source_ids)) != len(source_ids):
        raise ValueError("authenticated V5 source plan task coverage is invalid")
    normalize_units(api, source_plan_units)

    combined: list[dict[str, Any]] = []
    for wave_name, wave in (
        ("initial", initial_skeletons),
        ("remaining", remaining_skeletons),
    ):
        if not isinstance(wave, Sequence) or isinstance(wave, (str, bytes, bytearray)):
            raise ValueError(f"authenticated V5 {wave_name} skeletons are invalid")
        for index, raw_skeleton in enumerate(wave):
            skeleton = dict(
                api._mapping(
                    raw_skeleton, f"authenticated V5 {wave_name} skeletons[{index}]"
                )
            )
            if set(skeleton) != _SKELETON_FIELDS:
                raise ValueError("authenticated V5 skeleton fields are invalid")
            if skeleton.get("source_plan_hash") != source_hash:
                raise ValueError("authenticated V5 skeleton source hash is invalid")
            api._task_id(
                skeleton.get("task_id"),
                f"authenticated V5 {wave_name} skeletons[{index}].task_id",
            )
            if "storage_intent" in skeleton:
                raise ValueError(
                    "authenticated V5 pre-host skeleton carries storage intent"
                )
            order = skeleton.get("dispatch_order")
            if type(order) is not int or not 0 <= order < len(source_ids):
                raise ValueError("authenticated V5 package dispatch order is invalid")
            combined.append(skeleton)

    if len(combined) != len(source_ids):
        raise ValueError("authenticated V5 skeleton package is incomplete")
    task_ids = [str(item["task_id"]) for item in combined]
    orders = [int(item["dispatch_order"]) for item in combined]
    if (
        len(set(task_ids)) != len(task_ids)
        or set(task_ids) != set(source_ids)
        or len(set(orders)) != len(orders)
        or set(orders) != set(range(len(source_ids)))
    ):
        raise ValueError("authenticated V5 skeleton package coverage is invalid")

    ordered = sorted(combined, key=lambda item: int(item["dispatch_order"]))
    return api._sha256_json(
        {
            "schema": "2718lab-devkit/authenticated-v5-skeleton-package-v1",
            "source_plan_hash": source_hash,
            "task_ids": source_ids,
            "assignment_skeletons": ordered,
        }
    )
