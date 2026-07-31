"""Pure deterministic compiler for explicit Relay work packages."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from .canonical import canonical_hash


_REQUEST_FIELDS = {
    "schema",
    "workflow_id",
    "input_snapshot_id",
    "capacity",
    "tasks",
}
_TASK_FIELDS = {
    "task_id",
    "kind",
    "title",
    "dependencies",
    "write_scope",
    "route",
    "required_evidence",
}
_ROUTE_FIELDS = {"model", "reasoning_effort"}
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
_KINDS = {"implementation", "verification", "review", "prewarm"}


class RelayPlanError(ValueError):
    """Stable validation failure raised by the pure compiler."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _exact_fields(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise RelayPlanError(code)


def _identifier(value: object, code: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise RelayPlanError(code)
    return value


def _strings(value: object, code: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise RelayPlanError(code)
    if len(value) != len(set(value)):
        raise RelayPlanError(code)
    return tuple(sorted(value))


def _scope_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in path.parts
        or "." in path.parts
    ):
        raise RelayPlanError("invalid_write_scope")
    return path.as_posix().rstrip("/") + ("/" if normalized.endswith("/") else "")


def _scopes_overlap(left: str, right: str) -> bool:
    left_base = left.rstrip("/")
    right_base = right.rstrip("/")
    return (
        left_base == right_base
        or right_base.startswith(left_base + "/")
        or left_base.startswith(right_base + "/")
    )


def _normalize_task(raw: object) -> dict[str, Any]:
    if type(raw) is not dict:
        raise RelayPlanError("invalid_task")
    _exact_fields(raw, _TASK_FIELDS, "unknown_task_fields")
    task_id = _identifier(raw["task_id"], "invalid_task_id")
    if raw["kind"] not in _KINDS:
        raise RelayPlanError("invalid_task_kind")
    if type(raw["title"]) is not str or not raw["title"].strip():
        raise RelayPlanError("invalid_task_title")
    dependencies = _strings(raw["dependencies"], "invalid_dependencies")
    write_scope = tuple(
        sorted(_scope_path(item) for item in _strings(raw["write_scope"], "invalid_write_scope"))
    )
    if raw["kind"] != "implementation" and write_scope:
        raise RelayPlanError("readonly_task_has_write_scope")
    route = raw["route"]
    if type(route) is not dict:
        raise RelayPlanError("invalid_route")
    _exact_fields(route, _ROUTE_FIELDS, "invalid_route")
    if type(route["model"]) is not str or not route["model"]:
        raise RelayPlanError("invalid_route")
    if route["reasoning_effort"] not in _EFFORTS:
        raise RelayPlanError("invalid_route")
    required_evidence = _strings(raw["required_evidence"], "invalid_evidence")
    return {
        "task_id": task_id,
        "kind": raw["kind"],
        "title": raw["title"].strip(),
        "dependencies": list(dependencies),
        "write_scope": list(write_scope),
        "route": {
            "model": route["model"],
            "reasoning_effort": route["reasoning_effort"],
        },
        "required_evidence": list(required_evidence),
    }


def compile_plan(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonically compile one explicit Relay package."""

    if type(request) is not dict:
        raise RelayPlanError("invalid_request")
    _exact_fields(request, _REQUEST_FIELDS, "unknown_request_fields")
    if request["schema"] != "2718lab-devkit/relay-compile-request-v1":
        raise RelayPlanError("invalid_schema")
    workflow_id = _identifier(request["workflow_id"], "invalid_workflow_id")
    if type(request["input_snapshot_id"]) is not str or _HASH.fullmatch(
        request["input_snapshot_id"]
    ) is None:
        raise RelayPlanError("invalid_snapshot_id")
    capacity = request["capacity"]
    if type(capacity) is not int or not 1 <= capacity <= 8:
        raise RelayPlanError("invalid_capacity")
    if type(request["tasks"]) is not list or not request["tasks"]:
        raise RelayPlanError("invalid_tasks")
    tasks = tuple(sorted((_normalize_task(item) for item in request["tasks"]), key=lambda item: item["task_id"]))
    task_ids = [task["task_id"] for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise RelayPlanError("duplicate_task_id")
    known_ids = set(task_ids)
    for task in tasks:
        if task["task_id"] in task["dependencies"] or not set(task["dependencies"]) <= known_ids:
            raise RelayPlanError("invalid_dependencies")
    ready = tuple(task for task in tasks if not task["dependencies"])
    ready_writers = tuple(task for task in ready if task["kind"] == "implementation")
    for index, left in enumerate(ready_writers):
        for right in ready_writers[index + 1 :]:
            if any(
                _scopes_overlap(left_scope, right_scope)
                for left_scope in left["write_scope"]
                for right_scope in right["write_scope"]
            ):
                raise RelayPlanError("write_scope_conflict")
    prepared_prewarms = tuple(task for task in tasks if task not in ready)
    plan = {
        "schema": "2718lab-devkit/relay-plan-v1",
        "workflow_id": workflow_id,
        "input_snapshot_id": request["input_snapshot_id"],
        "capacity": capacity,
        "tasks": list(tasks),
        "ready": list(ready),
        "prepared_prewarms": list(prepared_prewarms),
    }
    return {**plan, "plan_hash": canonical_hash(plan)}
