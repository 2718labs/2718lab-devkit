"""Mechanical raw Fast Lane request projection into authenticated V5 units."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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
_STORAGE_REQUEST_FIELDS = frozenset({"storage_budgets"})
_STORAGE_CONTEXT_FIELDS = frozenset(
    {"execution_context_hash", *_STORAGE_DESCRIPTOR_FIELDS}
)
_STORAGE_PUBLIC_DESCRIPTOR_KEYS = _STORAGE_CONTEXT_FIELDS | frozenset(
    {
        "storage_context",
        "storage_contexts",
        "storage_descriptor",
        "storage_profile",
        "storage_profiles",
        "target_descriptor",
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


def _storage_request_without_extensions(value: Mapping[str, Any], api: Any) -> None:
    if "storage_contexts" in value:
        raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    base = {
        key: item for key, item in value.items() if key not in _STORAGE_REQUEST_FIELDS
    }
    api._exact_keys(base, api._FAST_LANE_REQUEST_FIELDS, "fast-lane request")


def _reject_public_storage_facts(value: object) -> None:
    """Reject descriptor facts supplied through the public request."""

    if isinstance(value, Mapping):
        if set(value).intersection(_STORAGE_PUBLIC_DESCRIPTOR_KEYS):
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
        for item in value.values():
            _reject_public_storage_facts(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_public_storage_facts(item)


def _validated_storage_budget(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"bytes", "files"}:
        raise ValueError(_STORAGE_POLICY_MISSING)
    requested_bytes = value.get("bytes")
    requested_files = value.get("files")
    if (
        type(requested_bytes) is not int
        or requested_bytes <= 0
        or requested_bytes > (1 << 64) - 1
        or type(requested_files) is not int
        or requested_files <= 0
        or requested_files > (1 << 64) - 1
    ):
        raise ValueError(_STORAGE_POLICY_MISSING)
    return {"bytes": requested_bytes, "files": requested_files}


def _validated_request_storage_budgets(
    value: object, task_ids: Sequence[str]
) -> dict[str, dict[str, int]]:
    """Accept only exact per-task public budgets; no default or surplus task."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) != set(task_ids):
        raise ValueError(_STORAGE_POLICY_MISSING)
    return {task_id: _validated_storage_budget(value[task_id]) for task_id in task_ids}


def _attach_storage_budget(
    source_unit: Mapping[str, Any],
    request_budgets: Mapping[str, Mapping[str, int]],
    *,
    task_id: str,
) -> dict[str, Any]:
    result = dict(source_unit)
    source_budget = source_unit.get("storage_budget")
    request_budget = request_budgets.get(task_id)
    if source_budget is not None:
        source_budget = _validated_storage_budget(source_budget)
    if request_budget is not None:
        request_budget = _validated_storage_budget(request_budget)
    if source_budget is not None and request_budget is not None:
        if source_budget != request_budget:
            raise ValueError(_STORAGE_TARGET_KEY_INVALID)
    elif source_budget is None:
        source_budget = request_budget
    if source_budget is None:
        return result
    result["storage_budget"] = _validated_storage_budget(source_budget)
    return result


def project_units(
    api: Any,
    request: Mapping[str, Any],
    *,
    index_context_hash: object,
    scheduler_facts: Mapping[str, Any],
    host_capabilities: Mapping[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Project the first host-admitted wave.

    The public helper keeps its historical two-value return shape.  The
    authenticated server path uses :func:`project_units_with_waves` so route
    attestations can cover the whole bounded package while dispatch remains
    limited to the live first-wave capacity.
    """

    source_plan_hash, initial, _remaining = project_units_with_waves(
        api,
        request,
        index_context_hash=index_context_hash,
        scheduler_facts=scheduler_facts,
        host_capabilities=host_capabilities,
    )
    return source_plan_hash, initial


def project_units_with_waves(
    api: Any,
    request: Mapping[str, Any],
    *,
    index_context_hash: object,
    scheduler_facts: Mapping[str, Any],
    host_capabilities: Mapping[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Project a package into an initial wave and a host-owned refill queue.

    The package is bounded to sixteen units.  The first wave is capped at
    ``min(host_available, config_capacity, 3)``.  Each emitted slice retains
    its source-plan dispatch coordinates; the complete package remains the
    authority for checking a hole-free global order.
    """

    candidate = api._mapping(request, "fast-lane request")
    _storage_request_without_extensions(candidate, api)
    if (
        candidate.get("schema") != "team-efficiency/fast-lane-request-v1"
        or len(api._json_bytes(candidate)) > api.MAX_MANIFEST_INPUT_BYTES
        or candidate.get("remediation_request") is not None
    ):
        raise ValueError("authenticated V5 raw request is unsupported")
    source_plan = api.decompose(candidate["work_package"])
    raw_execution_contexts = candidate["execution_contexts"]
    _reject_public_storage_facts(raw_execution_contexts)
    source_plan_hash = api._sha256_json(source_plan)
    if source_plan.get("status") != "planned":
        raise ValueError("authenticated V5 source plan is not schedulable")
    project_binding = api._validated_project_binding(candidate["project_binding"])
    project_authority = api._mapping(
        source_plan.get("project_authority"), "source plan.project_authority"
    )
    attestation = api._mapping(
        project_binding["attestation"], "project binding.attestation"
    )
    if (
        project_binding["mode"] != "indexed"
        or project_binding["project_id"]
        != f"sha256:{project_authority.get('project_id')}"
        or project_binding["workspace_id"] != project_authority.get("workspace_id")
        or attestation["attested_input_snapshot_id"]
        != project_authority.get("input_snapshot_id")
    ):
        raise ValueError("authenticated V5 project binding is stale")
    target_gates = api._validated_fast_lane_target_gates(
        candidate["target_gates"], source_plan
    )
    execution_contexts, read_contexts = api._validated_fast_lane_contexts(
        candidate["execution_contexts"],
        candidate["read_contexts"],
        source_plan,
        candidate["scheduler_state"],
    )
    state, remediation = api._validated_fast_lane_scheduler_state(
        candidate["scheduler_state"],
        source_plan=source_plan,
        source_plan_hash=source_plan_hash,
        execution_contexts=execution_contexts,
        read_contexts=read_contexts,
        target_gates=target_gates,
        remediation_request_value=None,
        routing_context={},
    )
    if remediation is not None or state["phase"] != "execution":
        raise ValueError("authenticated V5 scheduler phase is unsupported")

    raw_units_by_task = api._fast_lane_unit_index(source_plan)
    request_budgets = _validated_request_storage_budgets(
        candidate.get("storage_budgets"), tuple(raw_units_by_task)
    )
    source_budget_task_ids = {
        task_id
        for task_id, source_unit in raw_units_by_task.items()
        if "storage_budget" in source_unit
    }
    if source_budget_task_ids and source_budget_task_ids != set(raw_units_by_task):
        raise ValueError(_STORAGE_POLICY_MISSING)
    units_by_task = {
        task_id: _attach_storage_budget(
            source_unit,
            request_budgets,
            task_id=task_id,
        )
        for task_id, source_unit in raw_units_by_task.items()
    }
    if not 1 <= len(units_by_task) <= 16:
        raise ValueError("authenticated V5 source plan exceeds the bounded queue")
    config_capacity = source_plan.get("capacity")
    if type(config_capacity) is not int or not 1 <= config_capacity <= 16:
        raise ValueError("authenticated V5 source plan capacity is invalid")
    host_capacity = _host_available_capacity(
        scheduler_facts, host_capabilities=host_capabilities
    )
    first_wave_capacity = min(3, config_capacity, host_capacity)
    if first_wave_capacity < 1:
        raise ValueError("authenticated V5 Host has no available writer slot")

    completed = api._fast_lane_completed_ids(state)
    running = frozenset(item["task_id"] for item in state["running_assignments"])
    candidates = frozenset(item["task_id"] for item in state["review_ready_candidates"])
    reviewed = frozenset(item["task_id"] for item in state["reviewed_candidates"])
    graph = api._fast_lane_conflict_graph(
        units_by_task,
        lane0_scopes=state["lane0_state"]["owned_write_scopes"],
        running=state["running_assignments"],
    )
    ready = api._fast_lane_ready_items(
        units_by_task,
        completed,
        frozenset(state["blocked_task_ids"]),
        running,
        candidates,
        reviewed,
        conflict_graph=graph,
    )
    selected_ids = api._maximal_ready_wave(ready, graph, first_wave_capacity)
    if not selected_ids:
        raise ValueError("authenticated V5 has no ready owned writers")
    package_order = api._fast_lane_topology_index(units_by_task)
    ordered_selected_ids = sorted(selected_ids, key=package_order.__getitem__)
    selected_set = set(ordered_selected_ids)
    ordered_remaining_ids = [
        task_id
        for task_id, _order in sorted(package_order.items(), key=lambda item: item[1])
        if (
            task_id not in selected_set
            and task_id not in completed
            and task_id not in running
        )
    ]
    index_hash = api._hash(index_context_hash, "index_context_hash")
    target_by_task = {item["task_id"]: item for item in target_gates}
    workflow_hash = api._sha256_json({"workflow_id": project_binding["workflow_id"]})

    ancestor_cache: dict[str, int] = {}

    def ancestor_depth(task_id: str) -> int:
        cached = ancestor_cache.get(task_id)
        if cached is not None:
            return cached
        dependencies = list(units_by_task[task_id].get("depends_on", []))
        depth = (
            0
            if not dependencies
            else 1 + max(ancestor_depth(item) for item in dependencies)
        )
        ancestor_cache[task_id] = depth
        return depth

    downstream_cache: dict[str, frozenset[str]] = {}

    def downstream(task_id: str) -> frozenset[str]:
        cached = downstream_cache.get(task_id)
        if cached is not None:
            return cached
        direct = {
            other_id
            for other_id, other in units_by_task.items()
            if task_id in other.get("depends_on", [])
        }
        result = frozenset(
            direct | {item for child in direct for item in downstream(child)}
        )
        downstream_cache[task_id] = result
        return result

    maximum_downstream = max(len(downstream(task_id)) for task_id in units_by_task)

    def project_slice(task_ids: Sequence[str]) -> list[dict[str, Any]]:
        projected_slice: list[dict[str, Any]] = []
        for task_id in task_ids:
            # dispatch_order is a source-plan coordinate, not a batch-local
            # ordinal.  A refill wave can therefore contain a legitimate
            # sparse slice (for example [1, 3]) while the concatenated plan
            # remains the complete 0..N-1 sequence.
            dispatch_order = package_order[task_id]
            source_unit = units_by_task[task_id]
            if target_by_task.get(task_id) is None:
                raise ValueError("authenticated V5 execution context is incomplete")
            target = target_by_task[task_id]
            write_scope = api._normalised_scopes(source_unit.get("write_scope", []))
            top_levels = {path.split("/", 1)[0] for path in write_scope}
            breadth = (
                "single_file"
                if len(write_scope) == 1
                else "single_module"
                if len(top_levels) == 1
                else "multi_module"
            )
            direct_dependencies = sorted(source_unit.get("depends_on", []))
            dependency_without_hash = {
                "schema": "2718lab-devkit/dependency-state-v1",
                "graph_epoch": scheduler_facts.get("route_epoch"),
                "direct_dependency_ids": direct_dependencies,
                "completed_dependency_ids": sorted(
                    set(direct_dependencies).intersection(completed)
                ),
            }
            dependency_state = {
                **dependency_without_hash,
                "dependency_state_hash": api._sha256_json(dependency_without_hash),
            }
            criticality = {
                "Terra High": "normal",
                "Terra Max": "high",
                "Sol High": "critical",
            }.get(str(source_unit.get("recommended_route")), "critical")
            task = {
                "schema": "2718lab-devkit/task-routing-profile-v5",
                "task_id": task_id,
                "role": "execution",
                "access": "workspace_write",
                "write_scope_count": len(write_scope),
                "write_scope_breadth": breadth,
                "read_scope_count": 0,
                "read_scope_breadth": "none",
                "overlap_risk": "none",
                "overlap_count": 0,
                "dependency_depth": ancestor_depth(task_id),
                "downstream_critical_count": len(downstream(task_id)),
                "critical_path": maximum_downstream > 0
                and len(downstream(task_id)) == maximum_downstream,
                "criticality": criticality,
                "cross_module": len(top_levels) > 1,
                "database_work": False,
                "migration": False,
                "security_sensitive": False,
                "destructive": False,
                "external_boundary": False,
                "architecture_conflict": False,
                "design_ambiguity": False,
                "verification_cost": "focused"
                if len(target["gates"]) == 1
                else "multi_gate",
                "blocker_severity": "none",
                "authorization": "not_required",
                "authorization_evidence_hash": None,
                "narrow_decoupling_eligible": False,
                "strike": None,
                "gate_matrix_hash": api._sha256_json(target),
            }
            profile_unit: dict[str, Any] = {
                "task": task,
                "dependency_state": dependency_state,
                "write_scope": write_scope,
                "concurrency_mode": "parallel",
                "dispatch_order": dispatch_order,
                "index_context_hash": index_hash,
                "workflow_id_hash": workflow_hash,
            }
            if "storage_budget" in source_unit:
                profile_unit["storage_budget"] = source_unit["storage_budget"]
            profile_material = _routing_profile_material(source_plan_hash, profile_unit)
            task["profile_evidence_hash"] = api._sha256_json(profile_material)
            projected_unit = dict(profile_unit)
            projected_unit["task"] = task
            projected_slice.append(projected_unit)
        return projected_slice

    return (
        source_plan_hash,
        project_slice(ordered_selected_ids),
        project_slice(ordered_remaining_ids),
    )


def _host_available_capacity(
    scheduler_facts: Mapping[str, Any],
    *,
    host_capabilities: Mapping[str, Any] | None,
) -> int:
    """Extract an attested available writer count without guessing a route."""

    fields = (
        "available_writer_slots",
        "available_slots",
        "host_available",
        "writer_capacity",
        "available_capacity",
    )
    for facts in (scheduler_facts, host_capabilities):
        if not isinstance(facts, Mapping):
            continue
        for field in fields:
            value = facts.get(field)
            if type(value) is int and 0 <= value <= 16:
                return value
        if facts is host_capabilities:
            value = facts.get("total_slots")
            if type(value) is int and 0 <= value <= 16:
                return value
    # The pure helper predates the bridge capability argument.  Its callers do
    # not have a Host snapshot, so retain a conservative test-only three-slot
    # cap; the authenticated server always supplies the Host snapshot.
    return 3
