"""Pure deterministic compiler for bounded Relay work packages.

The compiler deliberately has no store dependency.  Callers inject a small
read-only registry resolver, which binds the opaque workspace, snapshot, and
Atlas packet identifiers before a plan can be produced.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from math import isfinite
from pathlib import PurePosixPath
from typing import Any, Protocol

from .canonical import canonical_hash

_REQUEST_SCHEMA = "2718lab-devkit/relay-compile-request-v1"
_PLAN_SCHEMA = "2718lab-devkit/relay-plan-v1"
_REQUEST_SCHEMA_V2 = "2718lab-devkit/relay-compile-request-v2"
_PLAN_SCHEMA_V2 = "2718lab-devkit/relay-plan-v2"
_REQUEST_SCHEMA_V3 = "2718lab-devkit/relay-compile-request-v3"
_PLAN_SCHEMA_V3 = "2718lab-devkit/relay-plan-v3"
_RUNTIME_POLICY_ID = "2718lab-devkit/relay-runtime-policy-v1"

_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "workflow_id",
        "workspace_id",
        "input_snapshot_id",
        "base_commit",
        "capacity",
        "tasks",
    }
)
_REQUEST_FIELDS_V2 = _REQUEST_FIELDS | frozenset({"project_binding"})
_REQUEST_FIELDS_V2_RECOMPILE = _REQUEST_FIELDS_V2 | frozenset(
    {"bootstrap_receipt"}
)
_REQUEST_FIELDS_V3 = _REQUEST_FIELDS_V2 | frozenset({"scheduler_topology"})
_REQUEST_FIELDS_V3_RECOMPILE = _REQUEST_FIELDS_V3 | frozenset(
    {"bootstrap_receipt"}
)
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "kind",
        "title",
        "objective",
        "priority",
        "dependencies",
        "write_scope",
        "route",
        "constraints",
        "acceptance_criteria",
        "atlas_packet_ids",
        "required_evidence",
        "prewarm_for_task_id",
        "retry_policy",
    }
)
_TASK_FIELDS_V2 = _TASK_FIELDS | frozenset(
    {
        "stage",
        "design_for_task_id",
        "split_policy",
        "split_parent_task_id",
        "split_depth",
        "split_verdict",
    }
)
_ROUTE_FIELDS = frozenset({"route_class", "model", "reasoning_effort"})
_RETRY_POLICY_FIELDS = frozenset({"max_attempts", "retryable_codes"})
_SCOPE_FIELDS = frozenset({"path", "kind"})
_CONSTRAINT_FIELDS = frozenset({"code", "detail"})
_CRITERION_FIELDS = frozenset({"criterion_id", "description"})
_EVIDENCE_FIELDS = frozenset({"kind", "selector"})
_BINDING_FIELDS = frozenset(
    {
        "workflow_id",
        "workspace_id",
        "input_snapshot_id",
        "atlas_packet_ids",
        "current",
    }
)
_PROJECT_BINDING_FIELDS = frozenset({"schema", "mode"})
_PROJECT_BOOTSTRAP_BINDING_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "attestation",
        "binding_hash",
    }
)
_BOOTSTRAP_ATTESTATION_FIELDS = frozenset(
    {
        "schema",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "initial_manifest_hash",
        "initial_entry_count",
        "state",
        "capability_epoch",
        "capability_hash",
        "attested_input_snapshot_id",
        "issued_at",
        "expires_at",
        "attestation_hash",
    }
)
_BOOTSTRAP_REGISTRY_BINDING_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "bootstrap_only",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "initial_manifest_hash",
        "initial_entry_count",
        "capability_epoch",
        "capability_hash",
        "attested_input_snapshot_id",
        "issued_at",
        "expires_at",
        "attestation_hash",
        "binding_hash",
    }
)
_BOOTSTRAP_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "attestation_hash",
        "workspace_id",
        "attested_input_snapshot_id",
        "initial_manifest_hash",
        "index_snapshot_id",
        "index_identity",
        "issued_at",
        "expires_at",
        "receipt_hash",
    }
)
_RECOMPILED_PROJECT_BINDING_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "bootstrap_binding",
        "bootstrap_receipt",
    }
)
_SPLIT_POLICY_FIELDS = frozenset({"mode", "max_depth", "child_scopes"})
_SCHEDULER_TOPOLOGY_FIELDS = frozenset(
    {
        "schema",
        "max_writers_per_scheduler",
        "max_parallel_writers",
        "groups",
    }
)
_SCHEDULER_GROUP_FIELDS = frozenset(
    {
        "scheduler_id",
        "coordinator_lease_id",
        "worktree_identity",
        "writer_task_ids",
        "prewarm_task_ids",
    }
)
_SCHEDULER_TOPOLOGY_SCHEMA = "2718lab-devkit/scheduler-topology-v1"

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_KINDS = frozenset({"implementation", "verification", "review", "prewarm"})
_V2_KINDS = _KINDS | frozenset({"design"})
_STAGES = frozenset({"a1_writer", "a2_design", "a3_prewarm"})
_ROUTES = {
    "terra_high": ("gpt-5.6-terra", "high"),
    "terra_max": ("gpt-5.6-terra", "max"),
    "sol_high": ("gpt-5.6-sol", "high"),
    "sol_ultra": ("gpt-5.6-sol", "ultra"),
    "luna_medium": ("gpt-5.6-luna", "medium"),
}

_MAX_TASKS = 64
_MAX_LIST_ITEMS = 32
_MAX_TEXT_LENGTH = 2_048
_MAX_TITLE_LENGTH = 256
_MAX_RETRY_ATTEMPTS = 3
_MAX_WRITERS_PER_SCHEDULER = 3
_MAX_PARALLEL_WRITERS = 9


class RelayPlanError(ValueError):
    """Stable validation failure raised by the pure compiler."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RegistryResolver(Protocol):
    """Read-only binding boundary used by :func:`compile_plan`."""

    def resolve(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        input_snapshot_id: str,
        atlas_packet_ids: tuple[str, ...],
    ) -> Mapping[str, object] | None:
        """Return the current binding, or ``None`` when it is unavailable."""


RegistryCallback = Callable[..., Mapping[str, object] | None]


def _exact_fields(value: object, expected: frozenset[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise RelayPlanError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise RelayPlanError(code)
    return value


def _hash_identifier(value: object, code: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise RelayPlanError(code)
    return value


def _opaque_identity(value: object, code: str) -> str:
    if _is_hash(value):
        return str(value)
    return _identifier(value, code)


def _commit_identifier(value: object) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise RelayPlanError("invalid_base_commit")
    return value


def _bounded_text(value: object, code: str, *, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if type(value) is not str or "\r" in value or "\n" in value:
        raise RelayPlanError(code)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise RelayPlanError(code)
    return normalized


def _text_list(
    value: object, code: str, *, maximum: int = _MAX_LIST_ITEMS
) -> list[str]:
    if type(value) is not list or len(value) > maximum:
        raise RelayPlanError(code)
    normalized = [_bounded_text(item, code) for item in value]
    if len(normalized) != len(set(normalized)):
        raise RelayPlanError(code)
    return sorted(normalized)


def _identifier_list(
    value: object,
    code: str,
    *,
    maximum: int = _MAX_LIST_ITEMS,
) -> list[str]:
    if type(value) is not list or len(value) > maximum:
        raise RelayPlanError(code)
    normalized = [_identifier(item, code) for item in value]
    if len(normalized) != len(set(normalized)):
        raise RelayPlanError(code)
    return sorted(normalized)


def _hash_list(
    value: object, code: str, *, maximum: int = _MAX_LIST_ITEMS
) -> list[str]:
    if type(value) is not list or len(value) > maximum:
        raise RelayPlanError(code)
    normalized = [_hash_identifier(item, code) for item in value]
    if len(normalized) != len(set(normalized)):
        raise RelayPlanError(code)
    return sorted(normalized)


def _scope_path(value: object) -> str:
    if type(value) is not str:
        raise RelayPlanError("invalid_write_scope")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > _MAX_TEXT_LENGTH
        or normalized.startswith("/")
        or normalized.startswith("~")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise RelayPlanError("invalid_write_scope")
    return path.as_posix().rstrip("/")


def _scope_list(value: object) -> list[dict[str, str]]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise RelayPlanError("invalid_write_scope")
    normalized: list[dict[str, str]] = []
    for item in value:
        scope = _exact_fields(item, _SCOPE_FIELDS, "invalid_write_scope")
        kind = scope["kind"]
        if type(kind) is not str or kind not in {"file", "tree"}:
            raise RelayPlanError("invalid_write_scope")
        normalized.append({"path": _scope_path(scope["path"]), "kind": kind})
    keys = {(scope["path"], scope["kind"]) for scope in normalized}
    if len(normalized) != len(keys):
        raise RelayPlanError("invalid_write_scope")
    return sorted(normalized, key=lambda scope: (scope["path"], scope["kind"]))


def _scopes_overlap(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    left_path = left["path"]
    right_path = right["path"]
    if left_path == right_path:
        return True
    if left["kind"] == "tree" and right_path.startswith(left_path + "/"):
        return True
    return right["kind"] == "tree" and left_path.startswith(right_path + "/")


def _constraint_list(value: object) -> list[dict[str, str]]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise RelayPlanError("invalid_constraints")
    normalized = []
    for item in value:
        constraint = _exact_fields(item, _CONSTRAINT_FIELDS, "invalid_constraints")
        normalized.append(
            {
                "code": _identifier(constraint["code"], "invalid_constraints"),
                "detail": _bounded_text(constraint["detail"], "invalid_constraints"),
            }
        )
    keys = {(item["code"], item["detail"]) for item in normalized}
    if len(keys) != len(normalized):
        raise RelayPlanError("invalid_constraints")
    return sorted(normalized, key=lambda item: (item["code"], item["detail"]))


def _criterion_list(value: object) -> list[dict[str, str]]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise RelayPlanError("invalid_acceptance_criteria")
    normalized = []
    for item in value:
        criterion = _exact_fields(
            item, _CRITERION_FIELDS, "invalid_acceptance_criteria"
        )
        normalized.append(
            {
                "criterion_id": _identifier(
                    criterion["criterion_id"], "invalid_acceptance_criteria"
                ),
                "description": _bounded_text(
                    criterion["description"], "invalid_acceptance_criteria"
                ),
            }
        )
    keys = {(item["criterion_id"], item["description"]) for item in normalized}
    if len(keys) != len(normalized):
        raise RelayPlanError("invalid_acceptance_criteria")
    return sorted(
        normalized, key=lambda item: (item["criterion_id"], item["description"])
    )


def _evidence_list(value: object) -> list[dict[str, str]]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise RelayPlanError("invalid_required_evidence")
    normalized = []
    for item in value:
        evidence = _exact_fields(item, _EVIDENCE_FIELDS, "invalid_required_evidence")
        normalized.append(
            {
                "kind": _identifier(evidence["kind"], "invalid_required_evidence"),
                "selector": _bounded_text(
                    evidence["selector"], "invalid_required_evidence"
                ),
            }
        )
    keys = {(item["kind"], item["selector"]) for item in normalized}
    if len(keys) != len(normalized):
        raise RelayPlanError("invalid_required_evidence")
    return sorted(normalized, key=lambda item: (item["kind"], item["selector"]))


def _normalize_route(value: object) -> dict[str, str]:
    route = _exact_fields(value, _ROUTE_FIELDS, "invalid_route")
    route_class = route["route_class"]
    if type(route_class) is not str or route_class not in _ROUTES:
        raise RelayPlanError("invalid_route")
    model, reasoning_effort = _ROUTES[route_class]
    if route["model"] != model or route["reasoning_effort"] != reasoning_effort:
        raise RelayPlanError("invalid_route")
    return {
        "route_class": route_class,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }


def _normalize_retry_policy(value: object) -> dict[str, object]:
    policy = _exact_fields(value, _RETRY_POLICY_FIELDS, "invalid_retry_policy")
    max_attempts = policy["max_attempts"]
    if type(max_attempts) is not int or not 1 <= max_attempts <= _MAX_RETRY_ATTEMPTS:
        raise RelayPlanError("invalid_retry_policy")
    return {
        "max_attempts": max_attempts,
        "retryable_codes": _identifier_list(
            policy["retryable_codes"], "invalid_retry_policy", maximum=8
        ),
    }


def _normalize_task(raw: object) -> dict[str, Any]:
    task = _exact_fields(raw, _TASK_FIELDS, "unknown_task_fields")
    task_id = _identifier(task["task_id"], "invalid_task_id")
    kind = task["kind"]
    if type(kind) is not str or kind not in _KINDS:
        raise RelayPlanError("invalid_task_kind")
    priority = task["priority"]
    if type(priority) is not int or not 1 <= priority <= 100:
        raise RelayPlanError("invalid_priority")

    dependencies = _identifier_list(task["dependencies"], "invalid_dependencies")
    write_scope = _scope_list(task["write_scope"])
    if kind == "implementation" and not write_scope:
        raise RelayPlanError("implementation_requires_write_scope")
    if kind == "prewarm" and write_scope:
        raise RelayPlanError("prewarm_has_write_scope")
    if kind in {"verification", "review"} and write_scope:
        raise RelayPlanError("readonly_task_has_write_scope")

    target = task["prewarm_for_task_id"]
    if kind == "prewarm":
        if dependencies:
            raise RelayPlanError("prewarm_has_dependencies")
        target = _identifier(target, "invalid_prewarm_target")
    elif target is not None:
        raise RelayPlanError("invalid_prewarm_target")

    return {
        "task_id": task_id,
        "kind": kind,
        "title": _bounded_text(
            task["title"], "invalid_task_title", maximum=_MAX_TITLE_LENGTH
        ),
        "objective": _bounded_text(task["objective"], "invalid_objective"),
        "priority": priority,
        "dependencies": dependencies,
        "write_scope": write_scope,
        "route": _normalize_route(task["route"]),
        "constraints": _constraint_list(task["constraints"]),
        "acceptance_criteria": _criterion_list(task["acceptance_criteria"]),
        "atlas_packet_ids": _hash_list(
            task["atlas_packet_ids"], "invalid_atlas_packet_ids", maximum=16
        ),
        "required_evidence": _evidence_list(task["required_evidence"]),
        "prewarm_for_task_id": target,
        "retry_policy": _normalize_retry_policy(task["retry_policy"]),
    }


def _validate_task_relations(tasks: list[dict[str, Any]]) -> None:
    task_by_id = {task["task_id"]: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise RelayPlanError("duplicate_task_id")
    known_ids = set(task_by_id)
    prewarm_ids = {task["task_id"] for task in tasks if task["kind"] == "prewarm"}

    for task in tasks:
        task_id = task["task_id"]
        dependencies = task["dependencies"]
        if task_id in dependencies or not set(dependencies) <= known_ids:
            raise RelayPlanError("invalid_dependencies")
        if set(dependencies) & prewarm_ids:
            raise RelayPlanError("prewarm_cannot_be_dependency")
        target = task["prewarm_for_task_id"]
        if target is not None and (
            target not in task_by_id or task_by_id[target]["kind"] == "prewarm"
        ):
            raise RelayPlanError("invalid_prewarm_target")


def _assert_acyclic(tasks: list[dict[str, Any]]) -> None:
    dependencies = {task["task_id"]: task["dependencies"] for task in tasks}
    states: dict[str, int] = {}

    def visit(task_id: str) -> None:
        state = states.get(task_id, 0)
        if state == 1:
            raise RelayPlanError("dependency_cycle")
        if state == 2:
            return
        states[task_id] = 1
        for dependency in dependencies[task_id]:
            visit(dependency)
        states[task_id] = 2

    for task_id in sorted(dependencies):
        visit(task_id)


def _ancestors(tasks: list[dict[str, Any]]) -> dict[str, frozenset[str]]:
    dependencies = {task["task_id"]: task["dependencies"] for task in tasks}
    cache: dict[str, frozenset[str]] = {}

    def resolve(task_id: str) -> frozenset[str]:
        if task_id in cache:
            return cache[task_id]
        direct = dependencies[task_id]
        result = frozenset(
            {*direct, *(ancestor for item in direct for ancestor in resolve(item))}
        )
        cache[task_id] = result
        return result

    return {task_id: resolve(task_id) for task_id in sorted(dependencies)}


def _conflict_orientation(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if left["priority"] != right["priority"]:
        return (left, right) if left["priority"] > right["priority"] else (right, left)
    return (left, right) if left["task_id"] < right["task_id"] else (right, left)


def _conflict_edges(tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    ancestors = _ancestors(tasks)
    writers = [task for task in tasks if task["kind"] == "implementation"]
    conflicts: list[dict[str, str]] = []
    for index, left in enumerate(writers):
        for right in writers[index + 1 :]:
            if (
                left["task_id"] in ancestors[right["task_id"]]
                or right["task_id"] in ancestors[left["task_id"]]
            ):
                continue
            if not any(
                _scopes_overlap(left_scope, right_scope)
                for left_scope in left["write_scope"]
                for right_scope in right["write_scope"]
            ):
                continue
            blocker, blocked = _conflict_orientation(left, right)
            conflicts.append(
                {
                    "from_task_id": blocker["task_id"],
                    "kind": "write_scope_conflict",
                    "to_task_id": blocked["task_id"],
                }
            )
    return sorted(
        conflicts,
        key=lambda edge: (edge["from_task_id"], edge["to_task_id"]),
    )


def _queue_order(tasks: list[dict[str, Any]]) -> list[str]:
    return [
        task["task_id"]
        for task in sorted(tasks, key=lambda task: (-task["priority"], task["task_id"]))
    ]


def _initial_queues(
    tasks: list[dict[str, Any]], conflicts: list[dict[str, str]]
) -> dict[str, list[str]]:
    prepared_prewarms = [task for task in tasks if task["kind"] == "prewarm"]
    candidates = [
        task for task in tasks if task["kind"] != "prewarm" and not task["dependencies"]
    ]
    candidate_ids = {task["task_id"] for task in candidates}
    withheld = {
        edge["to_task_id"]
        for edge in conflicts
        if edge["from_task_id"] in candidate_ids and edge["to_task_id"] in candidate_ids
    }
    ready = [task for task in candidates if task["task_id"] not in withheld]
    return {
        "prepared_prewarms": _queue_order(prepared_prewarms),
        "ready": _queue_order(ready),
        "running_slots": [],
        "review_integration": [],
        "terminal": [],
    }


def _registry_binding(
    resolver: RegistryResolver | RegistryCallback | object | None,
    *,
    workflow_id: str,
    workspace_id: str,
    input_snapshot_id: str,
    atlas_packet_ids: tuple[str, ...],
) -> dict[str, object]:
    if resolver is None:
        raise RelayPlanError("registry_binding_unavailable")
    try:
        resolve = getattr(resolver, "resolve", resolver)
    except RelayPlanError:
        raise
    except Exception as exc:
        raise RelayPlanError("registry_binding_unavailable") from exc
    if not callable(resolve):
        raise RelayPlanError("registry_binding_unavailable")
    try:
        binding = resolve(
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            input_snapshot_id=input_snapshot_id,
            atlas_packet_ids=atlas_packet_ids,
        )
    except RelayPlanError:
        raise
    except Exception as exc:
        raise RelayPlanError("registry_binding_unavailable") from exc
    if binding is None:
        raise RelayPlanError("registry_binding_unavailable")
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        raise RelayPlanError("registry_binding_corrupt")
    if (
        binding["current"] is not True
        or binding["workflow_id"] != workflow_id
        or binding["workspace_id"] != workspace_id
        or binding["input_snapshot_id"] != input_snapshot_id
    ):
        raise RelayPlanError("registry_binding_stale")
    try:
        registered_packets = _hash_list(
            binding["atlas_packet_ids"], "invalid_registry_binding"
        )
    except RelayPlanError as exc:
        raise RelayPlanError("registry_binding_corrupt") from exc
    if not set(atlas_packet_ids) <= set(registered_packets):
        raise RelayPlanError("registry_binding_stale")
    return {
        "workspace_id": workspace_id,
        "input_snapshot_id": input_snapshot_id,
        "atlas_packet_ids": list(atlas_packet_ids),
    }


def _compile_plan_v1(
    request: Mapping[str, Any],
    registry_resolver: RegistryResolver | RegistryCallback | object | None = None,
) -> dict[str, Any]:
    """Validate and canonically compile one explicit Relay v3 work package.

    Malformed caller input and missing, stale, or corrupt registry bindings
    raise :class:`RelayPlanError`.  Only the MCP adapter wraps those domain
    errors.  The compiler invokes only the injected resolver's read method and
    never opens a database, creates a worktree, or reserves a lease.
    """

    if type(request) is not dict:
        raise RelayPlanError("invalid_request")
    _exact_fields(request, _REQUEST_FIELDS, "unknown_request_fields")
    if request["schema"] != _REQUEST_SCHEMA:
        raise RelayPlanError("invalid_schema")
    workflow_id = _identifier(request["workflow_id"], "invalid_workflow_id")
    workspace_id = _hash_identifier(request["workspace_id"], "invalid_workspace_id")
    input_snapshot_id = _hash_identifier(
        request["input_snapshot_id"], "invalid_snapshot_id"
    )
    base_commit = _commit_identifier(request["base_commit"])
    capacity = request["capacity"]
    if type(capacity) is not int or not 1 <= capacity <= 3:
        raise RelayPlanError("invalid_capacity")
    if type(request["tasks"]) is not list or not request["tasks"]:
        raise RelayPlanError("invalid_tasks")
    if len(request["tasks"]) > _MAX_TASKS:
        raise RelayPlanError("too_many_tasks")

    tasks = sorted(
        (_normalize_task(item) for item in request["tasks"]),
        key=lambda task: task["task_id"],
    )
    _validate_task_relations(tasks)
    _assert_acyclic(tasks)
    atlas_packet_ids = tuple(
        sorted({packet_id for task in tasks for packet_id in task["atlas_packet_ids"]})
    )
    workspace_binding = _registry_binding(
        registry_resolver,
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        input_snapshot_id=input_snapshot_id,
        atlas_packet_ids=atlas_packet_ids,
    )
    conflicts = _conflict_edges(tasks)
    plan = {
        "schema": _PLAN_SCHEMA,
        "workflow_id": workflow_id,
        "workspace_binding": workspace_binding,
        "base_commit": base_commit,
        "capacity": capacity,
        "runtime_policy_id": _RUNTIME_POLICY_ID,
        "tasks": tasks,
        "dependencies": [
            {
                "from_task_id": task["task_id"],
                "kind": "depends_on",
                "to_task_id": dependency,
            }
            for task in tasks
            for dependency in task["dependencies"]
        ],
        "conflicts": conflicts,
        "queues": _initial_queues(tasks, conflicts),
    }
    return {**plan, "plan_hash": canonical_hash(plan)}


def _normalize_split_policy(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    policy = _exact_fields(value, _SPLIT_POLICY_FIELDS, "invalid_split_policy")
    if policy["mode"] != "declared_children":
        raise RelayPlanError("invalid_split_policy")
    max_depth = policy["max_depth"]
    if type(max_depth) is not int or not 1 <= max_depth <= 8:
        raise RelayPlanError("invalid_split_policy")
    raw_children = policy["child_scopes"]
    if type(raw_children) is not list or not 1 <= len(raw_children) <= _MAX_LIST_ITEMS:
        raise RelayPlanError("invalid_split_policy")
    children = [_scope_list(item) for item in raw_children]
    if any(not child for child in children):
        raise RelayPlanError("invalid_split_policy")
    keyed = sorted((canonical_hash(child), child) for child in children)
    if len({key for key, _ in keyed}) != len(keyed):
        raise RelayPlanError("invalid_split_policy")
    return {
        "mode": "declared_children",
        "max_depth": max_depth,
        "child_scopes": [child for _, child in keyed],
    }


def _normalize_task_v2(raw: object) -> dict[str, Any]:
    task = _exact_fields(raw, _TASK_FIELDS_V2, "unknown_task_fields")
    task_id = _identifier(task["task_id"], "invalid_task_id")
    kind = task["kind"]
    stage = task["stage"]
    if type(kind) is not str or kind not in _V2_KINDS:
        raise RelayPlanError("invalid_task_kind")
    if type(stage) is not str or stage not in _STAGES:
        raise RelayPlanError("invalid_task_stage")
    priority = task["priority"]
    if type(priority) is not int or not 1 <= priority <= 100:
        raise RelayPlanError("invalid_priority")
    dependencies = _identifier_list(task["dependencies"], "invalid_dependencies")
    write_scope = _scope_list(task["write_scope"])
    design_target = task["design_for_task_id"]
    prewarm_target = task["prewarm_for_task_id"]
    split_parent = task["split_parent_task_id"]
    split_depth = task["split_depth"]
    split_verdict = task["split_verdict"]
    if split_parent is not None or type(split_depth) is not int or split_depth != 0:
        raise RelayPlanError("invalid_split_provenance")
    if split_verdict is not None:
        raise RelayPlanError("invalid_split_provenance")

    if stage == "a1_writer":
        if kind != "implementation" or not write_scope:
            raise RelayPlanError("a1_writer_requires_implementation_scope")
        if design_target is not None or prewarm_target is not None:
            raise RelayPlanError("invalid_stage_target")
    elif stage == "a2_design":
        if kind != "design" or write_scope or dependencies or prewarm_target is not None:
            raise RelayPlanError("a2_design_must_be_readonly")
        design_target = _identifier(design_target, "invalid_design_target")
    else:
        if (
            kind != "prewarm"
            or write_scope
            or dependencies
            or design_target is not None
        ):
            raise RelayPlanError("a3_prewarm_must_be_readonly")
        prewarm_target = _identifier(prewarm_target, "invalid_prewarm_target")

    route = _normalize_route(task["route"])
    if stage == "a3_prewarm" and route != {
        "route_class": "luna_medium",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
    }:
        raise RelayPlanError("a3_prewarm_requires_luna_medium")
    split_policy = _normalize_split_policy(task["split_policy"])
    if stage != "a1_writer" and split_policy is not None:
        raise RelayPlanError("readonly_task_has_split_policy")
    return {
        "task_id": task_id,
        "kind": kind,
        "stage": stage,
        "title": _bounded_text(
            task["title"], "invalid_task_title", maximum=_MAX_TITLE_LENGTH
        ),
        "objective": _bounded_text(task["objective"], "invalid_objective"),
        "priority": priority,
        "dependencies": dependencies,
        "write_scope": write_scope,
        "route": route,
        "constraints": _constraint_list(task["constraints"]),
        "acceptance_criteria": _criterion_list(task["acceptance_criteria"]),
        "atlas_packet_ids": _hash_list(
            task["atlas_packet_ids"], "invalid_atlas_packet_ids", maximum=16
        ),
        "required_evidence": _evidence_list(task["required_evidence"]),
        "design_for_task_id": design_target,
        "prewarm_for_task_id": prewarm_target,
        "retry_policy": _normalize_retry_policy(task["retry_policy"]),
        "split_policy": split_policy,
        "split_parent_task_id": None,
        "split_depth": 0,
        "split_verdict": None,
    }


def _validate_task_relations_v2(tasks: list[dict[str, Any]]) -> None:
    task_by_id = {task["task_id"]: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise RelayPlanError("duplicate_task_id")
    known_ids = set(task_by_id)
    for task in tasks:
        task_id = task["task_id"]
        dependencies = task["dependencies"]
        if task_id in dependencies or not set(dependencies) <= known_ids:
            raise RelayPlanError("invalid_dependencies")
        if task["stage"] == "a2_design":
            target = task["design_for_task_id"]
            if target not in task_by_id or task_by_id[target]["stage"] != "a1_writer":
                raise RelayPlanError("invalid_design_target")
        if task["stage"] == "a3_prewarm":
            target = task["prewarm_for_task_id"]
            if target not in task_by_id or task_by_id[target]["stage"] != "a1_writer":
                raise RelayPlanError("invalid_prewarm_target")


def _scope_covers(parent: Mapping[str, str], child: Mapping[str, str]) -> bool:
    if parent["path"] == child["path"]:
        return parent["kind"] == "tree" or child["kind"] == "file"
    return parent["kind"] == "tree" and child["path"].startswith(parent["path"] + "/")


def _declared_split(
    tasks: list[dict[str, Any]], parent_id: str
) -> list[dict[str, Any]] | None:
    parent = next(task for task in tasks if task["task_id"] == parent_id)
    policy = parent["split_policy"]
    if policy is None or parent["split_depth"] >= policy["max_depth"]:
        return None
    if any(
        parent_id in task["dependencies"]
        or task.get("design_for_task_id") == parent_id
        or task.get("prewarm_for_task_id") == parent_id
        for task in tasks
        if task["task_id"] != parent_id
    ):
        return None
    child_scopes = policy["child_scopes"]
    if any(
        not any(_scope_covers(parent_scope, scope) for parent_scope in parent["write_scope"])
        for child in child_scopes
        for scope in child
    ):
        return None
    flattened = [scope for child in child_scopes for scope in child]
    if any(
        _scopes_overlap(left, right)
        for index, left in enumerate(flattened)
        for right in flattened[index + 1 :]
    ):
        return None
    children: list[dict[str, Any]] = []
    existing_ids = {task["task_id"] for task in tasks} - {parent_id}
    for child_scope in child_scopes:
        suffix = canonical_hash(child_scope)[7:19]
        child_id = f"{parent_id}-{suffix}"
        if child_id in existing_ids:
            return None
        existing_ids.add(child_id)
        children.append(
            {
                **parent,
                "task_id": child_id,
                "write_scope": child_scope,
                "split_policy": None,
                "split_parent_task_id": parent_id,
                "split_depth": parent["split_depth"] + 1,
                "split_verdict": "SPLIT_APPLIED",
            }
        )
    if len(tasks) - 1 + len(children) > _MAX_TASKS:
        return None
    replacement = [task for task in tasks if task["task_id"] != parent_id] + children
    replacement.sort(key=lambda task: task["task_id"])
    try:
        _validate_task_relations_v2(replacement)
        _assert_acyclic(replacement)
    except RelayPlanError:
        return None
    return replacement


def resolve_write_scope_conflicts(
    tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Use declared children only; a split must strictly reduce conflict edges."""

    current = list(tasks)
    conflicts = _conflict_edges(current)
    while conflicts:
        replacement: list[dict[str, Any]] | None = None
        for edge in conflicts:
            for task_id in (edge["to_task_id"], edge["from_task_id"]):
                proposed = _declared_split(current, task_id)
                if proposed is None:
                    continue
                if len(_conflict_edges(proposed)) < len(conflicts):
                    replacement = proposed
                    break
            if replacement is not None:
                break
        if replacement is None:
            unsplittable = sorted(
                {item for edge in conflicts for item in (edge["from_task_id"], edge["to_task_id"])}
            )
            marked = []
            for task in current:
                if task["task_id"] in unsplittable:
                    marked.append({**task, "split_verdict": "UNSPLITTABLE_SCOPE_CONFLICT"})
                else:
                    marked.append(task)
            return marked, conflicts, unsplittable
        current = replacement
        conflicts = _conflict_edges(current)
    return current, [], []


def _queue_order_v2(tasks: list[dict[str, Any]]) -> list[str]:
    return [
        task["task_id"]
        for task in sorted(tasks, key=lambda task: (-task["priority"], task["task_id"]))
    ]


def _initial_queues_v2(
    tasks: list[dict[str, Any]],
    conflicts: list[dict[str, str]],
    unsplittable: list[str],
    *,
    bootstrap: bool,
) -> dict[str, list[str]]:
    if bootstrap:
        return {
            "writer_ready": [],
            "design_ready": [],
            "prewarm_ready": [],
            "bootstrap_index": ["bootstrap-index"],
            "review_integration": [],
            "terminal": [],
            "unsplittable": [],
        }
    withheld = {edge["to_task_id"] for edge in conflicts}
    blocked_targets = set(unsplittable)
    writers = [
        task
        for task in tasks
        if task["stage"] == "a1_writer"
        and not task["dependencies"]
        and task["task_id"] not in withheld
        and task["task_id"] not in blocked_targets
    ]
    designs = [
        task
        for task in tasks
        if task["stage"] == "a2_design"
        and not task["dependencies"]
        and task["design_for_task_id"] not in blocked_targets
    ]
    prewarms = [
        task
        for task in tasks
        if task["stage"] == "a3_prewarm"
        and not task["dependencies"]
        and task["prewarm_for_task_id"] not in blocked_targets
    ]
    return {
        "writer_ready": _queue_order_v2(writers),
        "design_ready": _queue_order_v2(designs),
        "prewarm_ready": _queue_order_v2(prewarms),
        "bootstrap_index": [],
        "review_integration": [],
        "terminal": [],
        "unsplittable": sorted(unsplittable),
    }


def _resolver_value(
    resolver: RegistryResolver | RegistryCallback | object | None,
    *,
    workflow_id: str,
    workspace_id: str,
    input_snapshot_id: str,
    atlas_packet_ids: tuple[str, ...],
) -> object:
    if resolver is None:
        raise RelayPlanError("registry_binding_unavailable")
    try:
        resolve = getattr(resolver, "resolve", resolver)
        if not callable(resolve):
            raise RelayPlanError("registry_binding_unavailable")
        return resolve(
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            input_snapshot_id=input_snapshot_id,
            atlas_packet_ids=atlas_packet_ids,
        )
    except RelayPlanError:
        raise
    except Exception as exc:
        raise RelayPlanError("registry_binding_unavailable") from exc


def _bootstrap_workspace_binding(
    resolver: RegistryResolver | RegistryCallback | object | None,
    *,
    workflow_id: str,
    workspace_id: str,
    input_snapshot_id: str,
    project_binding: object,
) -> dict[str, object]:
    binding, attestation = _new_empty_project_binding(
        project_binding,
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        input_snapshot_id=input_snapshot_id,
    )
    if resolver is None:
        raise RelayPlanError("bootstrap_attestation_required")
    try:
        resolve = getattr(resolver, "resolve_new_empty_bootstrap")
        if not callable(resolve):
            raise RelayPlanError("bootstrap_attestation_required")
        registry_binding = resolve(binding)
    except RelayPlanError:
        raise
    except Exception as exc:
        raise RelayPlanError("bootstrap_attestation_required") from exc
    return _bootstrap_registry_binding(registry_binding, attestation=attestation)


def _new_empty_project_binding(
    value: object,
    *,
    workflow_id: str,
    workspace_id: str,
    input_snapshot_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate the exact Task 2 project-binding-v1 without importing runtime."""

    binding = _exact_fields(
        value, _PROJECT_BOOTSTRAP_BINDING_FIELDS, "bootstrap_attestation_required"
    )
    if (
        binding["schema"] != "2718lab-devkit/project-binding-v1"
        or binding["mode"] != "new_empty_bootstrap"
        or binding["workflow_id"] != workflow_id
        or binding["workspace_id"] != workspace_id
        or not _hash_matches(binding, "binding_hash")
    ):
        raise RelayPlanError("bootstrap_attestation_required")
    attestation = _exact_fields(
        binding["attestation"],
        _BOOTSTRAP_ATTESTATION_FIELDS,
        "bootstrap_attestation_required",
    )
    identity_fields = (
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
    )
    if (
        attestation["schema"]
        != "2718lab-devkit/new-project-bootstrap-attestation-v1"
        or any(binding[field] != attestation[field] for field in identity_fields)
        or not _hash_matches(attestation, "attestation_hash")
        or any(
            not _is_hash(attestation[field])
            for field in (
                "workspace_id",
                "repository_id",
                "project_id",
                "bootstrap_root_identity",
                "initial_manifest_hash",
                "capability_hash",
                "attested_input_snapshot_id",
            )
        )
        or attestation["state"] != "new_empty"
        or type(attestation["initial_entry_count"]) is not int
        or attestation["initial_entry_count"] != 0
        or type(attestation["capability_epoch"]) is not int
        or attestation["capability_epoch"] < 1
        or not _finite_timestamp(attestation["issued_at"])
        or not _finite_timestamp(attestation["expires_at"])
        or float(attestation["expires_at"]) <= float(attestation["issued_at"])
        or (
            input_snapshot_id is not None
            and attestation["attested_input_snapshot_id"] != input_snapshot_id
        )
    ):
        raise RelayPlanError("bootstrap_attestation_required")
    return dict(binding), dict(attestation)


def _bootstrap_registry_binding(
    value: object, *, attestation: Mapping[str, object]
) -> dict[str, object]:
    binding = _exact_fields(
        value, _BOOTSTRAP_REGISTRY_BINDING_FIELDS, "bootstrap_attestation_required"
    )
    expected = {
        "schema": "2718lab-devkit/project-registry-bootstrap-binding-v1",
        "mode": "new_empty_bootstrap",
        "bootstrap_only": True,
        "workflow_id": attestation["workflow_id"],
        "workspace_id": attestation["workspace_id"],
        "repository_id": attestation["repository_id"],
        "project_id": attestation["project_id"],
        "bootstrap_root_identity": attestation["bootstrap_root_identity"],
        "initial_manifest_hash": attestation["initial_manifest_hash"],
        "initial_entry_count": 0,
        "capability_epoch": attestation["capability_epoch"],
        "capability_hash": attestation["capability_hash"],
        "attested_input_snapshot_id": attestation["attested_input_snapshot_id"],
        "issued_at": attestation["issued_at"],
        "expires_at": attestation["expires_at"],
        "attestation_hash": attestation["attestation_hash"],
    }
    if (
        any(binding[key] != item for key, item in expected.items())
        or not _hash_matches(binding, "binding_hash")
    ):
        raise RelayPlanError("bootstrap_attestation_required")
    return dict(binding)


def _validate_bootstrap_recompile(
    resolver: RegistryResolver | RegistryCallback | object | None,
    *,
    workflow_id: str,
    workspace_id: str,
    input_snapshot_id: str,
    project_binding: object,
    receipt: object,
) -> tuple[dict[str, object], dict[str, object]]:
    binding, attestation = _new_empty_project_binding(
        project_binding,
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        input_snapshot_id=None,
    )
    requested_receipt = _bootstrap_receipt(receipt)
    if resolver is None:
        raise RelayPlanError("bootstrap_receipt_required")
    try:
        validate = getattr(resolver, "validate_bootstrap_recompile")
        if not callable(validate):
            raise RelayPlanError("bootstrap_receipt_required")
        validated_receipt = validate(
            project_binding=binding,
            receipt=requested_receipt,
        )
    except RelayPlanError:
        raise
    except Exception as exc:
        raise RelayPlanError("bootstrap_receipt_required") from exc
    verified_receipt = _bootstrap_receipt(validated_receipt)
    if (
        verified_receipt != requested_receipt
        or verified_receipt["attestation_hash"] != attestation["attestation_hash"]
        or verified_receipt["workspace_id"] != workspace_id
        or verified_receipt["attested_input_snapshot_id"]
        != attestation["attested_input_snapshot_id"]
        or verified_receipt["initial_manifest_hash"]
        != attestation["initial_manifest_hash"]
        or verified_receipt["index_snapshot_id"] != input_snapshot_id
        or verified_receipt["index_identity"]
        != canonical_hash(
            {
                "workspace_id": verified_receipt["workspace_id"],
                "attested_input_snapshot_id": verified_receipt[
                    "attested_input_snapshot_id"
                ],
                "initial_manifest_hash": verified_receipt["initial_manifest_hash"],
                "index_snapshot_id": verified_receipt["index_snapshot_id"],
            }
        )
    ):
        raise RelayPlanError("bootstrap_receipt_required")
    return binding, verified_receipt


def _bootstrap_receipt(value: object) -> dict[str, object]:
    receipt = _exact_fields(value, _BOOTSTRAP_RECEIPT_FIELDS, "bootstrap_receipt_required")
    if (
        receipt["schema"] != "2718lab-devkit/project-index-bootstrap-receipt-v1"
        or not _hash_matches(receipt, "receipt_hash")
        or any(
            not _is_hash(receipt[field])
            for field in (
                "attestation_hash",
                "workspace_id",
                "attested_input_snapshot_id",
                "initial_manifest_hash",
                "index_snapshot_id",
                "index_identity",
            )
        )
        or not _finite_timestamp(receipt["issued_at"])
        or not _finite_timestamp(receipt["expires_at"])
        or float(receipt["expires_at"]) <= float(receipt["issued_at"])
    ):
        raise RelayPlanError("bootstrap_receipt_required")
    return dict(receipt)


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _hash_matches(value: Mapping[str, object], field: str) -> bool:
    digest = value.get(field)
    if not _is_hash(digest):
        return False
    try:
        return digest == canonical_hash(
            {key: item for key, item in value.items() if key != field}
        )
    except (TypeError, ValueError):
        return False


def _finite_timestamp(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
    )


def _bootstrap_task() -> dict[str, object]:
    return {
        "task_id": "bootstrap-index",
        "kind": "bootstrap_index",
        "stage": "bootstrap_index",
        "priority": 100,
        "dependencies": [],
        "split_parent_task_id": None,
        "split_depth": 0,
        "split_verdict": None,
    }


def _compile_plan_v2(
    request: Mapping[str, Any],
    registry_resolver: RegistryResolver | RegistryCallback | object | None,
) -> dict[str, Any]:
    request_fields = (
        _REQUEST_FIELDS_V2_RECOMPILE
        if "bootstrap_receipt" in request
        else _REQUEST_FIELDS_V2
    )
    _exact_fields(request, request_fields, "unknown_request_fields")
    workflow_id = _identifier(request["workflow_id"], "invalid_workflow_id")
    workspace_id = _hash_identifier(request["workspace_id"], "invalid_workspace_id")
    input_snapshot_id = _hash_identifier(
        request["input_snapshot_id"], "invalid_snapshot_id"
    )
    base_commit = _commit_identifier(request["base_commit"])
    capacity = request["capacity"]
    if type(capacity) is not int or not 1 <= capacity <= 3:
        raise RelayPlanError("invalid_capacity")
    if type(request["tasks"]) is not list or not request["tasks"]:
        raise RelayPlanError("invalid_tasks")
    if len(request["tasks"]) > _MAX_TASKS:
        raise RelayPlanError("too_many_tasks")
    raw_project_binding = request["project_binding"]
    if type(raw_project_binding) is not dict:
        raise RelayPlanError("invalid_project_binding")
    mode = raw_project_binding.get("mode")
    if mode == "indexed":
        project_binding = _exact_fields(
            raw_project_binding, _PROJECT_BINDING_FIELDS, "invalid_project_binding"
        )
        if "bootstrap_receipt" in request:
            raise RelayPlanError("bootstrap_recompile_binding_required")
    elif mode == "new_empty_bootstrap":
        project_binding = raw_project_binding
    else:
        raise RelayPlanError("invalid_project_binding")
    if project_binding.get("schema") != "2718lab-devkit/project-binding-v1":
        raise RelayPlanError("invalid_project_binding")
    tasks = sorted(
        (_normalize_task_v2(item) for item in request["tasks"]),
        key=lambda task: task["task_id"],
    )
    _validate_task_relations_v2(tasks)
    _assert_acyclic(tasks)
    atlas_packet_ids = tuple(
        sorted({packet for task in tasks for packet in task["atlas_packet_ids"]})
    )
    if mode == "indexed":
        workspace_binding = _registry_binding(
            registry_resolver,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            input_snapshot_id=input_snapshot_id,
            atlas_packet_ids=atlas_packet_ids,
        )
        tasks, conflicts, unsplittable = resolve_write_scope_conflicts(tasks)
        bootstrap = False
        planned_project_binding: dict[str, object] = dict(project_binding)
    else:
        if "bootstrap_receipt" in request:
            bootstrap_binding, bootstrap_receipt = _validate_bootstrap_recompile(
                registry_resolver,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                input_snapshot_id=input_snapshot_id,
                project_binding=project_binding,
                receipt=request["bootstrap_receipt"],
            )
            workspace_binding = _registry_binding(
                registry_resolver,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                input_snapshot_id=input_snapshot_id,
                atlas_packet_ids=atlas_packet_ids,
            )
            tasks, conflicts, unsplittable = resolve_write_scope_conflicts(tasks)
            bootstrap = False
            planned_project_binding = {
                "schema": "2718lab-devkit/project-binding-v1",
                "mode": "indexed",
                "bootstrap_binding": bootstrap_binding,
                "bootstrap_receipt": bootstrap_receipt,
            }
        else:
            workspace_binding = _bootstrap_workspace_binding(
                registry_resolver,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                input_snapshot_id=input_snapshot_id,
                project_binding=project_binding,
            )
            conflicts = []
            unsplittable = []
            bootstrap = True
            planned_project_binding = dict(project_binding)
    plan_tasks: list[dict[str, Any] | dict[str, object]] = list(tasks)
    if bootstrap:
        plan_tasks.append(_bootstrap_task())
    plan = {
        "schema": _PLAN_SCHEMA_V2,
        "workflow_id": workflow_id,
        "workspace_binding": workspace_binding,
        "project_binding": planned_project_binding,
        "base_commit": base_commit,
        "capacity": capacity,
        "runtime_policy_id": _RUNTIME_POLICY_ID,
        "tasks": plan_tasks,
        "dependencies": [
            {
                "from_task_id": task["task_id"],
                "kind": "depends_on",
                "to_task_id": dependency,
            }
            for task in tasks
            for dependency in task["dependencies"]
        ],
        "conflicts": conflicts,
        "queues": _initial_queues_v2(
            tasks, conflicts, unsplittable, bootstrap=bootstrap
        ),
    }
    return {**plan, "plan_hash": canonical_hash(plan)}


def _normalize_scheduler_topology(
    value: object, *, tasks: list[Mapping[str, object]]
) -> dict[str, object]:
    """Bind V3's opaque scheduler groups to the compiled staged tasks."""

    topology = _exact_fields(
        value, _SCHEDULER_TOPOLOGY_FIELDS, "invalid_scheduler_topology"
    )
    if (
        topology["schema"] != _SCHEDULER_TOPOLOGY_SCHEMA
        or type(topology["max_writers_per_scheduler"]) is not int
        or topology["max_writers_per_scheduler"] != _MAX_WRITERS_PER_SCHEDULER
        or type(topology["max_parallel_writers"]) is not int
        or topology["max_parallel_writers"] != _MAX_PARALLEL_WRITERS
        or type(topology["groups"]) is not list
        or not topology["groups"]
        or len(topology["groups"]) > _MAX_LIST_ITEMS
    ):
        raise RelayPlanError("invalid_scheduler_topology")

    all_writers = {
        task["task_id"]: task
        for task in tasks
        if task.get("stage") == "a1_writer"
    }
    writers = {
        task_id: task
        for task_id, task in all_writers.items()
        if task.get("split_verdict") != "UNSPLITTABLE_SCOPE_CONFLICT"
    }
    unsplittable_writers = set(all_writers) - set(writers)
    prewarms = {
        task["task_id"]: task
        for task in tasks
        if task.get("stage") == "a3_prewarm"
    }
    split_children: dict[str, list[str]] = {}
    for task_id, task in writers.items():
        parent_id = task.get("split_parent_task_id")
        if type(parent_id) is str:
            split_children.setdefault(parent_id, []).append(task_id)

    normalized_groups: list[dict[str, object]] = []
    assigned_writers: set[str] = set()
    assigned_prewarms: set[str] = set()
    scheduler_ids: set[str] = set()
    lease_ids: set[str] = set()
    worktree_ids: set[str] = set()
    for raw_group in topology["groups"]:
        group = _exact_fields(raw_group, _SCHEDULER_GROUP_FIELDS, "invalid_scheduler_topology")
        scheduler_id = _opaque_identity(group["scheduler_id"], "invalid_scheduler_topology")
        lease_id = _opaque_identity(
            group["coordinator_lease_id"], "invalid_scheduler_topology"
        )
        worktree_identity = _opaque_identity(
            group["worktree_identity"], "invalid_scheduler_topology"
        )
        if (
            scheduler_id in scheduler_ids
            or lease_id in lease_ids
            or worktree_identity in worktree_ids
        ):
            raise RelayPlanError("invalid_scheduler_topology")
        scheduler_ids.add(scheduler_id)
        lease_ids.add(lease_id)
        worktree_ids.add(worktree_identity)
        writer_ids = _identifier_list(
            group["writer_task_ids"],
            "invalid_scheduler_topology",
            maximum=_MAX_WRITERS_PER_SCHEDULER,
        )
        prewarm_ids = _identifier_list(
            group["prewarm_task_ids"], "invalid_scheduler_topology"
        )
        expanded_writer_ids: list[str] = []
        for task_id in writer_ids:
            if task_id in unsplittable_writers:
                continue
            members = [task_id] if task_id in writers else split_children.get(task_id, [])
            if not members or any(member in assigned_writers for member in members):
                raise RelayPlanError("invalid_scheduler_topology")
            assigned_writers.update(members)
            expanded_writer_ids.extend(members)
        if len(expanded_writer_ids) > _MAX_WRITERS_PER_SCHEDULER:
            raise RelayPlanError("invalid_scheduler_topology")
        if any(task_id not in prewarms or task_id in assigned_prewarms for task_id in prewarm_ids):
            raise RelayPlanError("invalid_scheduler_topology")
        assigned_prewarms.update(prewarm_ids)
        normalized_groups.append(
            {
                "scheduler_id": scheduler_id,
                "coordinator_lease_id": lease_id,
                "worktree_identity": worktree_identity,
                "writer_task_ids": sorted(expanded_writer_ids),
                "prewarm_task_ids": prewarm_ids,
            }
        )
    if assigned_writers != set(writers) or assigned_prewarms != set(prewarms):
        raise RelayPlanError("invalid_scheduler_topology")
    if len(assigned_writers) > _MAX_PARALLEL_WRITERS:
        raise RelayPlanError("invalid_scheduler_topology")
    return {
        "schema": _SCHEDULER_TOPOLOGY_SCHEMA,
        "max_writers_per_scheduler": _MAX_WRITERS_PER_SCHEDULER,
        "max_parallel_writers": _MAX_PARALLEL_WRITERS,
        "groups": sorted(normalized_groups, key=lambda group: str(group["scheduler_id"])),
    }


def _compile_plan_v3(
    request: Mapping[str, Any],
    registry_resolver: RegistryResolver | RegistryCallback | object | None,
) -> dict[str, Any]:
    request_fields = (
        _REQUEST_FIELDS_V3_RECOMPILE
        if "bootstrap_receipt" in request
        else _REQUEST_FIELDS_V3
    )
    _exact_fields(request, request_fields, "unknown_request_fields")
    capacity = request.get("capacity")
    if type(capacity) is not int or not 1 <= capacity <= _MAX_PARALLEL_WRITERS:
        raise RelayPlanError("invalid_capacity")
    v2_request = {
        key: value
        for key, value in request.items()
        if key != "scheduler_topology"
    }
    v2_request["schema"] = _REQUEST_SCHEMA_V2
    # V2 keeps its legacy three-writer public contract.  V3 validates its
    # wider bounded capacity before borrowing the shared staged compiler.
    v2_request["capacity"] = min(capacity, _MAX_WRITERS_PER_SCHEDULER)
    compiled = _compile_plan_v2(v2_request, registry_resolver)
    plan_tasks = compiled["tasks"]
    if type(plan_tasks) is not list:
        raise RelayPlanError("invalid_scheduler_topology")
    topology = _normalize_scheduler_topology(
        request["scheduler_topology"], tasks=plan_tasks
    )
    plan = {
        **{key: value for key, value in compiled.items() if key != "plan_hash"},
        "schema": _PLAN_SCHEMA_V3,
        "capacity": capacity,
        "scheduler_topology": topology,
    }
    return {**plan, "plan_hash": canonical_hash(plan)}


def validate_stage_evidence(
    task: Mapping[str, object],
    *,
    input_snapshot_id: object,
    evidence: object,
) -> dict[str, str]:
    """Validate immutable A2/A3 evidence without scheduling or side effects."""

    stage = task.get("stage")
    if stage not in {"a2_design", "a3_prewarm"}:
        raise RelayPlanError("invalid_stage_evidence")
    expected = frozenset(
        {"task_id", "stage", "input_snapshot_id", "context_hash", "artifact_hash"}
    )
    value = _exact_fields(evidence, expected, "invalid_stage_evidence")
    stale_code = (
        "DESIGN_EVIDENCE_STALE" if stage == "a2_design" else "PREWARM_EVIDENCE_STALE"
    )
    if (
        value["task_id"] != task.get("task_id")
        or value["stage"] != stage
        or value["input_snapshot_id"] != input_snapshot_id
    ):
        raise RelayPlanError(stale_code)
    return {
        "task_id": _identifier(value["task_id"], "invalid_stage_evidence"),
        "stage": str(stage),
        "input_snapshot_id": _hash_identifier(
            value["input_snapshot_id"], "invalid_stage_evidence"
        ),
        "context_hash": _hash_identifier(value["context_hash"], "invalid_stage_evidence"),
        "artifact_hash": _hash_identifier(value["artifact_hash"], "invalid_stage_evidence"),
    }


def compile_plan(
    request: Mapping[str, Any],
    registry_resolver: RegistryResolver | RegistryCallback | object | None = None,
) -> dict[str, Any]:
    """Compile a legacy, staged, or hierarchy-bound Relay plan without effects."""

    if type(request) is not dict:
        raise RelayPlanError("invalid_request")
    schema = request.get("schema")
    if schema == _REQUEST_SCHEMA:
        return _compile_plan_v1(request, registry_resolver=registry_resolver)
    if schema == _REQUEST_SCHEMA_V2:
        return _compile_plan_v2(request, registry_resolver=registry_resolver)
    if schema == _REQUEST_SCHEMA_V3:
        return _compile_plan_v3(request, registry_resolver=registry_resolver)
    raise RelayPlanError("invalid_schema")
