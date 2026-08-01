"""Pure deterministic compiler for bounded Relay work packages.

The compiler deliberately has no store dependency.  Callers inject a small
read-only registry resolver, which binds the opaque workspace, snapshot, and
Atlas packet identifiers before a plan can be produced.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol

from .canonical import canonical_hash


_REQUEST_SCHEMA = "2718lab-devkit/relay-compile-request-v1"
_PLAN_SCHEMA = "2718lab-devkit/relay-plan-v1"
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

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_KINDS = frozenset({"implementation", "verification", "review", "prewarm"})
_ROUTES = {
    "terra_high": ("gpt-5.6-terra", "high"),
    "terra_max": ("gpt-5.6-terra", "max"),
    "sol_high": ("gpt-5.6-sol", "high"),
    "sol_ultra": ("gpt-5.6-sol", "ultra"),
}

_MAX_TASKS = 64
_MAX_LIST_ITEMS = 32
_MAX_TEXT_LENGTH = 2_048
_MAX_TITLE_LENGTH = 256
_MAX_RETRY_ATTEMPTS = 3


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


def _commit_identifier(value: object) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise RelayPlanError("invalid_base_commit")
    return value


def _bounded_text(value: object, code: str, *, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if type(value) is not str:
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


def compile_plan(
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
    if type(capacity) is not int or not 1 <= capacity <= 8:
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
