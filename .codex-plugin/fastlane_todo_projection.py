"""Pure stdlib fastlane TODO projector and state machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SOURCE_SCHEMA: Final = "2718lab-devkit/workflow-todo-source-v1"
STATE_SCHEMA: Final = "2718lab-devkit/fastlane-todo-state-v1"
DELTA_SCHEMA: Final = "2718lab-devkit/fastlane-todo-delta-v1"
EVENT_SCHEMA: Final = "2718lab-devkit/fastlane-todo-event-v1"


WORKFLOW_STATES: Final = (
    "new",
    "running",
    "done",
    "blocked",
    "failed",
    "cancelled",
)
TASK_STATES: Final = (
    "new",
    "ready",
    "running",
    "verifying",
    "done",
    "blocked",
    "failed",
    "cancelled",
)
WORKFLOW_REACHABLE: Final = {
    "new": frozenset({"running", "done", "blocked", "failed", "cancelled"}),
    "running": frozenset({"done", "blocked", "failed", "cancelled"}),
    "done": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
TASK_REACHABLE: Final = {
    "new": frozenset(
        {"ready", "running", "verifying", "done", "blocked", "failed", "cancelled"}
    ),
    "ready": frozenset(
        {"running", "verifying", "done", "blocked", "failed", "cancelled"}
    ),
    "running": frozenset({"verifying", "done", "blocked", "failed", "cancelled"}),
    "verifying": frozenset({"done", "blocked", "failed", "cancelled"}),
    "done": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
TERMINAL_TASK_STATES: Final = frozenset({"done", "blocked", "failed", "cancelled"})
BUCKET_ORDER: Final = ("attention", "active", "ready", "queued", "done", "closed")
BUDGET_STATES: Final = {
    "attention": {"blocked", "failed"},
    "active": {"running", "verifying"},
    "ready": {"ready"},
    "queued": {"new"},
    "done": {"done"},
    "closed": {"cancelled"},
}
BUDGET_STATUS: Final = {
    "attention": "pending",
    "active": "in_progress",
    "ready": "pending",
    "queued": "pending",
    "done": "completed",
    "closed": "completed",
}
MAX_TASKS: Final = 64
MAX_BYTES: Final = 64 * 1024
DEBOUNCE_DELAY: Final = 0.250
DEBOUNCE_CAP: Final = 1.0
MAX_TRANSITIONS: Final = 65
MAX_PLAN_ITEMS: Final = 6
MAX_STEP_TASKS: Final = 7
MAX_STEP_CHARS: Final = 512
MAX_TITLE_CHARS: Final = 128
HASH_PREFIX: Final = 12


WORKFLOW_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
CONTROL_CHAR_PATTERN: Final = re.compile(r"[\x00-\x1f\x7f]")


class FastlaneTodoError(ValueError):
    """Structured projector error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _require_nonempty_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FastlaneTodoError("INVALID_PAYLOAD", f"{path} is not an object")
    return value


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FastlaneTodoError("INVALID_PAYLOAD", f"{path} is not an object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise FastlaneTodoError("INVALID_PAYLOAD", f"{path} is not an array")
    return value


def _require_exact_fields(
    payload: Mapping[str, Any],
    expected: set[str],
    path: str,
    *,
    code: str = "INVALID_PAYLOAD",
) -> None:
    if set(payload.keys()) != expected:
        raise FastlaneTodoError(code, f"{path} has unexpected fields")


def _require_str(
    payload: Mapping[str, Any],
    key: str,
    path: str,
    *,
    code: str = "INVALID_PAYLOAD",
    allow_empty: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise FastlaneTodoError(code, f"{path}.{key} is not a string")
    if not allow_empty and len(value) == 0:
        raise FastlaneTodoError(code, f"{path}.{key} is empty")
    return value


def _require_int(
    payload: Mapping[str, Any], key: str, path: str, *, code: str = "INVALID_PAYLOAD"
) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise FastlaneTodoError(code, f"{path}.{key} is not an integer")
    if value < 0:
        raise FastlaneTodoError(code, f"{path}.{key} is negative")
    return value


def _require_id(value: str, path: str) -> str:
    if not WORKFLOW_ID_PATTERN.fullmatch(value):
        raise FastlaneTodoError("INVALID_PAYLOAD", f"{path} unsafe")
    return value


def _require_state(
    value: Mapping[str, Any],
    key: str,
    allowed: tuple[str, ...],
    path: str,
    *,
    code: str = "INVALID_PAYLOAD",
    allow_empty: bool = False,
) -> str:
    state = _require_str(value, key, path, code=code, allow_empty=allow_empty)
    if state not in allowed:
        raise FastlaneTodoError(code, f"{path}.{key} has invalid state")
    return state


def _sanitize_title(value: str) -> str:
    cleaned = CONTROL_CHAR_PATTERN.sub("", value)
    cleaned = cleaned.replace("\r", "").replace("\n", "")
    return cleaned[:MAX_TITLE_CHARS]


@dataclass(frozen=True)
class SourceTask:
    task_id: str
    title: str
    state: str
    version: int


@dataclass(frozen=True)
class SourceSnapshot:
    workflow_id: str
    workflow_state: str
    workflow_version: int
    tasks: tuple[SourceTask, ...]
    fingerprint: str


@dataclass(frozen=True)
class AckedTask:
    task_id: str
    state: str
    version: int


@dataclass(frozen=True)
class ObservedTask:
    task_id: str
    title: str
    state: str
    version: int


@dataclass(frozen=True)
class FastlaneTodoAckedState:
    fingerprint: str
    workflow_state: str
    tasks: tuple[AckedTask, ...]


@dataclass(frozen=True)
class FastlaneTodoObservedState:
    fingerprint: str
    workflow_state: str
    workflow_version: int
    tasks: tuple[ObservedTask, ...]


@dataclass(frozen=True)
class FastlaneTodoDebounceState:
    epoch: int
    first_seen_at: float
    due_at: float


@dataclass(frozen=True)
class Transition:
    kind: str
    item_id: str
    from_state: str
    to_state: str


@dataclass(frozen=True)
class PlanItem:
    step: str
    status: str


@dataclass(frozen=True)
class FastlaneTodoDelta:
    schema: str
    delta_id: str
    workflow_id: str
    from_fingerprint: str
    to_fingerprint: str
    reason: str
    transitions: tuple[Transition, ...]
    plan: tuple[PlanItem, ...]


@dataclass(frozen=True)
class FastlaneTodoState:
    schema: str
    workflow_id: str
    generation: int
    acked: FastlaneTodoAckedState
    observed: FastlaneTodoObservedState
    pending_delta: FastlaneTodoDelta | None = None
    debounce: FastlaneTodoDebounceState | None = None


def _is_reachable(
    allowed: Mapping[str, frozenset[str]], start: str, target: str
) -> bool:
    if start == target:
        return True
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for next_state in allowed.get(current, frozenset()):
            if next_state == target:
                return True
            if next_state in seen:
                continue
            seen.add(next_state)
            frontier.append(next_state)
    return False


def _fingerprint(
    workflow_id: str, workflow_state: str, tasks: tuple[tuple[str, str], ...]
) -> str:
    payload = {
        "workflow_id": workflow_id,
        "workflow_state": workflow_state,
        "tasks": [
            {"id": task_id, "state": task_state} for task_id, task_state in tasks
        ],
    }
    return _sha256_hex(_canonical_json(payload))


def _parse_source(payload: str) -> SourceSnapshot:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise FastlaneTodoError("INVALID_SOURCE", "source is not valid JSON") from error

    mapping = _require_nonempty_mapping(raw, "source")
    _require_exact_fields(
        mapping, {"schema", "workflow", "tasks"}, "source", code="INVALID_SOURCE"
    )

    if (
        _require_str(mapping, "schema", "source", code="INVALID_SOURCE")
        != SOURCE_SCHEMA
    ):
        raise FastlaneTodoError("INVALID_SOURCE", "unknown source schema")

    workflow = _require_nonempty_mapping(
        mapping["workflow"],
        "source.workflow",
    )
    _require_exact_fields(
        workflow,
        {"id", "state", "version"},
        "source.workflow",
        code="INVALID_SOURCE",
    )
    workflow_id = _require_id(
        _require_str(workflow, "id", "source.workflow", code="INVALID_SOURCE"),
        "source.workflow.id",
    )
    workflow_state = _require_state(
        workflow,
        "state",
        WORKFLOW_STATES,
        "source.workflow",
        code="INVALID_SOURCE",
    )
    workflow_version = _require_int(
        workflow, "version", "source.workflow", code="INVALID_SOURCE"
    )

    raw_tasks = _require_list(mapping["tasks"], "source.tasks")
    if len(raw_tasks) > MAX_TASKS:
        raise FastlaneTodoError("INVALID_SOURCE", "too many tasks")

    parsed_tasks = []
    for index, raw_task in enumerate(raw_tasks):
        task = _require_nonempty_mapping(raw_task, f"source.tasks[{index}]")
        _require_exact_fields(
            task,
            {"id", "title", "state", "version"},
            f"source.tasks[{index}]",
            code="INVALID_SOURCE",
        )
        task_id = _require_id(
            _require_str(
                task, "id", f"source.tasks[{index}].id", code="INVALID_SOURCE"
            ),
            f"source.tasks[{index}].id",
        )
        parsed_tasks.append(
            SourceTask(
                task_id=task_id,
                title=_sanitize_title(
                    _require_str(
                        task,
                        "title",
                        f"source.tasks[{index}].title",
                        code="INVALID_SOURCE",
                    )
                ),
                state=_require_state(
                    task,
                    "state",
                    TASK_STATES,
                    f"source.tasks[{index}]",
                    code="INVALID_SOURCE",
                ),
                version=_require_int(
                    task, "version", f"source.tasks[{index}]", code="INVALID_SOURCE"
                ),
            )
        )
    parsed_tasks.sort(key=lambda item: item.task_id)
    task_ids = [task.task_id for task in parsed_tasks]
    if len(task_ids) != len(set(task_ids)):
        raise FastlaneTodoError("INVALID_SOURCE", "duplicate task id")
    snapshot_tasks = tuple(parsed_tasks)
    task_pairs = tuple((item.task_id, item.state) for item in snapshot_tasks)
    return SourceSnapshot(
        workflow_id=workflow_id,
        workflow_state=workflow_state,
        workflow_version=workflow_version,
        tasks=snapshot_tasks,
        fingerprint=_fingerprint(
            workflow_id, workflow_state, tuple(sorted(task_pairs))
        ),
    )


def _source_to_observed(snapshot: SourceSnapshot) -> FastlaneTodoObservedState:
    return FastlaneTodoObservedState(
        fingerprint=snapshot.fingerprint,
        workflow_state=snapshot.workflow_state,
        workflow_version=snapshot.workflow_version,
        tasks=tuple(
            ObservedTask(
                task_id=task.task_id,
                title=task.title,
                state=task.state,
                version=task.version,
            )
            for task in snapshot.tasks
        ),
    )


def _parse_acked(payload: Mapping[str, Any], path: str) -> FastlaneTodoAckedState:
    _require_exact_fields(
        payload,
        {"fingerprint", "workflow_state", "tasks"},
        path,
        code="FASTLANE_STATE_CORRUPT",
    )
    raw_tasks = _require_list(payload["tasks"], f"{path}.tasks")
    tasks = []
    for index, raw_task in enumerate(raw_tasks):
        task = _require_nonempty_mapping(raw_task, f"{path}.tasks[{index}]")
        _require_exact_fields(
            task,
            {"id", "state", "version"},
            f"{path}.tasks[{index}]",
            code="FASTLANE_STATE_CORRUPT",
        )
        task_id = _require_id(
            _require_str(
                task, "id", f"{path}.tasks[{index}].id", code="FASTLANE_STATE_CORRUPT"
            ),
            f"{path}.tasks[{index}].id",
        )
        tasks.append(
            AckedTask(
                task_id=task_id,
                state=_require_state(
                    task,
                    "state",
                    (*TASK_STATES, ""),
                    f"{path}.tasks[{index}]",
                    code="FASTLANE_STATE_CORRUPT",
                ),
                version=_require_int(
                    task,
                    "version",
                    f"{path}.tasks[{index}]",
                    code="FASTLANE_STATE_CORRUPT",
                ),
            )
        )
    tasks.sort(key=lambda item: item.task_id)
    if len(tasks) != len({task.task_id for task in tasks}):
        raise FastlaneTodoError(
            "FASTLANE_STATE_CORRUPT", f"{path}.tasks has duplicates"
        )
    return FastlaneTodoAckedState(
        fingerprint=_require_str(
            payload,
            "fingerprint",
            path,
            code="FASTLANE_STATE_CORRUPT",
            allow_empty=True,
        ),
        workflow_state=_require_state(
            payload,
            "workflow_state",
            (*WORKFLOW_STATES, ""),
            path,
            code="FASTLANE_STATE_CORRUPT",
            allow_empty=True,
        ),
        tasks=tuple(tasks),
    )


def _parse_observed(payload: Mapping[str, Any], path: str) -> FastlaneTodoObservedState:
    _require_exact_fields(
        payload,
        {"fingerprint", "workflow_state", "workflow_version", "tasks"},
        path,
        code="FASTLANE_STATE_CORRUPT",
    )
    raw_tasks = _require_list(payload["tasks"], f"{path}.tasks")
    tasks = []
    for index, raw_task in enumerate(raw_tasks):
        task = _require_nonempty_mapping(raw_task, f"{path}.tasks[{index}]")
        _require_exact_fields(
            task,
            {"id", "title", "state", "version"},
            f"{path}.tasks[{index}]",
            code="FASTLANE_STATE_CORRUPT",
        )
        task_id = _require_id(
            _require_str(
                task, "id", f"{path}.tasks[{index}].id", code="FASTLANE_STATE_CORRUPT"
            ),
            f"{path}.tasks[{index}].id",
        )
        tasks.append(
            ObservedTask(
                task_id=task_id,
                title=_sanitize_title(
                    _require_str(
                        task,
                        "title",
                        f"{path}.tasks[{index}].title",
                        code="FASTLANE_STATE_CORRUPT",
                    )
                ),
                state=_require_state(
                    task,
                    "state",
                    TASK_STATES,
                    f"{path}.tasks[{index}]",
                    code="FASTLANE_STATE_CORRUPT",
                ),
                version=_require_int(
                    task,
                    "version",
                    f"{path}.tasks[{index}]",
                    code="FASTLANE_STATE_CORRUPT",
                ),
            )
        )
    tasks.sort(key=lambda item: item.task_id)
    if len(tasks) != len({task.task_id for task in tasks}):
        raise FastlaneTodoError(
            "FASTLANE_STATE_CORRUPT", f"{path}.tasks has duplicates"
        )
    return FastlaneTodoObservedState(
        fingerprint=_require_str(
            payload,
            "fingerprint",
            path,
            code="FASTLANE_STATE_CORRUPT",
            allow_empty=True,
        ),
        workflow_state=_require_state(
            payload,
            "workflow_state",
            (*WORKFLOW_STATES, ""),
            path,
            code="FASTLANE_STATE_CORRUPT",
        ),
        workflow_version=_require_int(
            payload,
            "workflow_version",
            f"{path}.workflow_version",
            code="FASTLANE_STATE_CORRUPT",
        ),
        tasks=tuple(tasks),
    )


def _parse_debounce(payload: Mapping[str, Any], path: str) -> FastlaneTodoDebounceState:
    _require_exact_fields(
        payload,
        {"epoch", "first_seen_at", "due_at"},
        path,
        code="FASTLANE_STATE_CORRUPT",
    )
    epoch = _require_int(
        payload, "epoch", f"{path}.epoch", code="FASTLANE_STATE_CORRUPT"
    )
    first_seen_at = float(payload["first_seen_at"])
    due_at = float(payload["due_at"])
    if not isinstance(payload["first_seen_at"], int | float) or not isinstance(
        payload["due_at"], int | float
    ):
        raise FastlaneTodoError(
            "FASTLANE_STATE_CORRUPT", f"{path} timing field invalid"
        )
    return FastlaneTodoDebounceState(
        epoch=epoch, first_seen_at=first_seen_at, due_at=due_at
    )


def _parse_delta(payload: Mapping[str, Any], path: str) -> FastlaneTodoDelta:
    _require_exact_fields(
        payload,
        {
            "schema",
            "delta_id",
            "workflow_id",
            "from_fingerprint",
            "to_fingerprint",
            "reason",
            "transitions",
            "plan",
        },
        path,
        code="FASTLANE_STATE_CORRUPT",
    )
    if (
        _require_str(payload, "schema", path, code="FASTLANE_STATE_CORRUPT")
        != DELTA_SCHEMA
    ):
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", f"{path}.schema invalid")
    if (
        _require_str(payload, "reason", path, code="FASTLANE_STATE_CORRUPT")
        != "state_transition"
    ):
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", f"{path}.reason invalid")
    if not _require_str(
        payload, "delta_id", path, code="FASTLANE_STATE_CORRUPT"
    ).startswith("sha256:"):
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", f"{path}.delta_id invalid")

    raw_transitions = _require_list(payload["transitions"], f"{path}.transitions")
    transitions = []
    for index, raw_transition in enumerate(raw_transitions):
        transition = _require_nonempty_mapping(
            raw_transition, f"{path}.transitions[{index}]"
        )
        _require_exact_fields(
            transition,
            {"kind", "id", "from_state", "to_state"},
            f"{path}.transitions[{index}]",
            code="FASTLANE_STATE_CORRUPT",
        )
        kind = _require_str(
            transition,
            "kind",
            f"{path}.transitions[{index}]",
            code="FASTLANE_STATE_CORRUPT",
        )
        if kind not in {"workflow", "task"}:
            raise FastlaneTodoError(
                "FASTLANE_STATE_CORRUPT", f"{path}.transitions[{index}].kind invalid"
            )
        transitions.append(
            Transition(
                kind=kind,
                item_id=_require_id(
                    _require_str(
                        transition,
                        "id",
                        f"{path}.transitions[{index}].id",
                        code="FASTLANE_STATE_CORRUPT",
                    ),
                    f"{path}.transitions[{index}].id",
                ),
                from_state=_require_str(
                    transition,
                    "from_state",
                    f"{path}.transitions[{index}].from_state",
                    code="FASTLANE_STATE_CORRUPT",
                    allow_empty=True,
                ),
                to_state=_require_str(
                    transition,
                    "to_state",
                    f"{path}.transitions[{index}].to_state",
                    code="FASTLANE_STATE_CORRUPT",
                ),
            )
        )
    raw_plan = _require_list(payload["plan"], f"{path}.plan")
    plan = []
    for index, raw_plan_item in enumerate(raw_plan):
        plan_item = _require_nonempty_mapping(raw_plan_item, f"{path}.plan[{index}]")
        _require_exact_fields(
            plan_item,
            {"step", "status"},
            f"{path}.plan[{index}]",
            code="FASTLANE_STATE_CORRUPT",
        )
        step = _require_str(
            plan_item,
            "step",
            f"{path}.plan[{index}].step",
            code="FASTLANE_STATE_CORRUPT",
        )
        status = _require_str(
            plan_item,
            "status",
            f"{path}.plan[{index}].status",
            code="FASTLANE_STATE_CORRUPT",
        )
        if status not in {"pending", "in_progress", "completed"}:
            raise FastlaneTodoError(
                "FASTLANE_STATE_CORRUPT", f"{path}.plan[{index}].status invalid"
            )
        plan.append(PlanItem(step=step, status=status))
    return FastlaneTodoDelta(
        schema=DELTA_SCHEMA,
        delta_id=_require_str(payload, "delta_id", path, code="FASTLANE_STATE_CORRUPT"),
        workflow_id=_require_id(
            _require_str(
                payload,
                "workflow_id",
                f"{path}.workflow_id",
                code="FASTLANE_STATE_CORRUPT",
            ),
            f"{path}.workflow_id",
        ),
        from_fingerprint=_require_str(
            payload,
            "from_fingerprint",
            f"{path}.from_fingerprint",
            code="FASTLANE_STATE_CORRUPT",
            allow_empty=True,
        ),
        to_fingerprint=_require_str(
            payload,
            "to_fingerprint",
            f"{path}.to_fingerprint",
            code="FASTLANE_STATE_CORRUPT",
        ),
        reason=_require_str(
            payload, "reason", f"{path}.reason", code="FASTLANE_STATE_CORRUPT"
        ),
        transitions=tuple(transitions),
        plan=tuple(plan),
    )


def _parse_state(payload: Any, workflow_id: str) -> FastlaneTodoState:
    raw = _require_nonempty_mapping(payload, "state")
    allowed = {"schema", "workflow_id", "generation", "acked", "observed"}
    optional = {"pending_delta", "debounce"}
    if set(raw) - allowed - optional:
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", "state has unexpected fields")
    if not allowed.issubset(raw):
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", "state missing fields")
    if (
        _require_str(raw, "schema", "state", code="FASTLANE_STATE_CORRUPT")
        != STATE_SCHEMA
    ):
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", "state schema invalid")
    actual_workflow_id = _require_id(
        _require_str(raw, "workflow_id", "state", code="FASTLANE_STATE_CORRUPT"),
        "state.workflow_id",
    )
    if actual_workflow_id != workflow_id:
        raise FastlaneTodoError("FASTLANE_STATE_CORRUPT", "state workflow id mismatch")
    acked = _parse_acked(_require_mapping(raw["acked"], "state.acked"), "state.acked")
    observed = _parse_observed(
        _require_mapping(raw["observed"], "state.observed"), "state.observed"
    )
    if observed.workflow_version < 0:
        raise FastlaneTodoError(
            "FASTLANE_STATE_CORRUPT", "state observed.workflow_version invalid"
        )
    pending = None
    if raw.get("pending_delta") is not None:
        pending = _parse_delta(
            _require_mapping(raw["pending_delta"], "state.pending_delta"),
            "state.pending_delta",
        )
    debounce = None
    if raw.get("debounce") is not None:
        debounce = _parse_debounce(
            _require_mapping(raw["debounce"], "state.debounce"), "state.debounce"
        )
    return FastlaneTodoState(
        schema=STATE_SCHEMA,
        workflow_id=actual_workflow_id,
        generation=_require_int(
            raw, "generation", "state", code="FASTLANE_STATE_CORRUPT"
        ),
        acked=acked,
        observed=observed,
        pending_delta=pending,
        debounce=debounce,
    )


def _default_state(workflow_id: str) -> FastlaneTodoState:
    return FastlaneTodoState(
        schema=STATE_SCHEMA,
        workflow_id=workflow_id,
        generation=0,
        acked=FastlaneTodoAckedState(fingerprint="", workflow_state="", tasks=()),
        observed=FastlaneTodoObservedState(
            fingerprint="",
            workflow_state="",
            workflow_version=0,
            tasks=(),
        ),
        pending_delta=None,
        debounce=None,
    )


def _serialize_state(state: FastlaneTodoState) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": state.schema,
        "workflow_id": state.workflow_id,
        "generation": state.generation,
        "acked": {
            "fingerprint": state.acked.fingerprint,
            "workflow_state": state.acked.workflow_state,
            "tasks": [
                {"id": task.task_id, "state": task.state, "version": task.version}
                for task in state.acked.tasks
            ],
        },
        "observed": {
            "fingerprint": state.observed.fingerprint,
            "workflow_state": state.observed.workflow_state,
            "workflow_version": state.observed.workflow_version,
            "tasks": [
                {
                    "id": task.task_id,
                    "title": task.title,
                    "state": task.state,
                    "version": task.version,
                }
                for task in state.observed.tasks
            ],
        },
    }
    if state.pending_delta is not None:
        payload["pending_delta"] = _serialize_delta(state.pending_delta)
    if state.debounce is not None:
        payload["debounce"] = {
            "epoch": state.debounce.epoch,
            "first_seen_at": state.debounce.first_seen_at,
            "due_at": state.debounce.due_at,
        }
    return payload


def _serialize_delta(delta: FastlaneTodoDelta) -> dict[str, Any]:
    return {
        "schema": delta.schema,
        "delta_id": delta.delta_id,
        "workflow_id": delta.workflow_id,
        "from_fingerprint": delta.from_fingerprint,
        "to_fingerprint": delta.to_fingerprint,
        "reason": delta.reason,
        "transitions": [
            {
                "kind": transition.kind,
                "id": transition.item_id,
                "from_state": transition.from_state,
                "to_state": transition.to_state,
            }
            for transition in delta.transitions
        ],
        "plan": [{"step": item.step, "status": item.status} for item in delta.plan],
    }


def _serialize_event(kind: str, workflow_id: str, payload: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "kind": kind,
        "workflow_id": workflow_id,
    }
    if kind == "delta":
        base["delta"] = payload
    if kind == "error":
        base["error"] = payload
    return base


def _build_plan_item(bucket: str, tasks: tuple[ObservedTask, ...]) -> PlanItem:
    bucket_tasks = sorted(tasks, key=lambda item: item.task_id)
    labels = [f"{item.task_id}: {item.title}" for item in bucket_tasks[:MAX_STEP_TASKS]]
    omitted = len(bucket_tasks) - len(labels)
    if omitted > 0:
        omitted_ids = tuple(task.task_id for task in bucket_tasks[MAX_STEP_TASKS:])
        suffix = _sha256_hex(_canonical_json(omitted_ids))[7 : 7 + HASH_PREFIX]
        labels.append(f"+{omitted} omitted [sha256:{suffix}]")
    text = f"{bucket}: " + "; ".join(labels)
    if len(text) > MAX_STEP_CHARS:
        text = text[:MAX_STEP_CHARS]
    return PlanItem(step=text, status=BUDGET_STATUS[bucket])


def _bucket_for_task(state: str) -> str:
    for bucket, states in BUDGET_STATES.items():
        if state in states:
            return bucket
    raise FastlaneTodoError("INVALID_PAYLOAD", f"unsupported task state {state}")


def _build_plan(
    workflow_state: str, tasks: tuple[ObservedTask, ...]
) -> tuple[PlanItem, ...]:
    buckets: dict[str, list[ObservedTask]] = {bucket: [] for bucket in BUCKET_ORDER}
    for task in tasks:
        buckets[_bucket_for_task(task.state)].append(task)

    plan: list[PlanItem] = []
    for bucket in BUCKET_ORDER:
        batch = tuple(buckets[bucket])
        if not batch:
            continue
        plan.append(_build_plan_item(bucket, batch))

    if not plan:
        status = "completed" if workflow_state in {"done", "cancelled"} else "pending"
        plan.append(PlanItem(step=f"workflow:{workflow_state}", status=status))
    if len(plan) > MAX_PLAN_ITEMS:
        raise FastlaneTodoError("INVALID_PLAN", "too many plan items")
    return tuple(plan)


def _validate_progressions(
    previous: FastlaneTodoObservedState, observed: FastlaneTodoObservedState
) -> None:
    if previous.workflow_version:
        if observed.workflow_version < previous.workflow_version:
            raise FastlaneTodoError("INVALID_TRANSITION", "workflow version regressed")
        if (
            observed.workflow_version == previous.workflow_version
            and observed.workflow_state != previous.workflow_state
        ):
            raise FastlaneTodoError("INVALID_TRANSITION", "workflow version collision")

    if previous.workflow_state and not _is_reachable(
        WORKFLOW_REACHABLE, previous.workflow_state, observed.workflow_state
    ):
        raise FastlaneTodoError("INVALID_TRANSITION", "workflow transition invalid")

    previous_tasks = {task.task_id: task for task in previous.tasks}
    observed_tasks = {task.task_id: task for task in observed.tasks}
    for task_id in previous_tasks:
        if task_id not in observed_tasks:
            raise FastlaneTodoError("INVALID_TRANSITION", "task removed")

    for task_id, observed_task in observed_tasks.items():
        previous_task = previous_tasks.get(task_id)
        if previous_task is None:
            continue
        if observed_task.version < previous_task.version:
            raise FastlaneTodoError("INVALID_TRANSITION", "task version regressed")
        if (
            observed_task.version == previous_task.version
            and observed_task.state != previous_task.state
        ):
            raise FastlaneTodoError("INVALID_TRANSITION", "task version collision")
        if (
            previous_task.state in TERMINAL_TASK_STATES
            and observed_task.state != previous_task.state
        ):
            raise FastlaneTodoError("INVALID_TRANSITION", "terminal task mutated")
        if not _is_reachable(TASK_REACHABLE, previous_task.state, observed_task.state):
            raise FastlaneTodoError("INVALID_TRANSITION", "task transition invalid")


def _build_delta(
    workflow_id: str, acked: FastlaneTodoAckedState, observed: FastlaneTodoObservedState
) -> FastlaneTodoDelta:
    transitions: list[Transition] = []
    if acked.workflow_state != observed.workflow_state:
        transitions.append(
            Transition(
                kind="workflow",
                item_id=workflow_id,
                from_state=acked.workflow_state or "",
                to_state=observed.workflow_state,
            )
        )

    acked_map = {task.task_id: task for task in acked.tasks}
    for task in observed.tasks:
        before = acked_map.get(task.task_id)
        if before is None:
            transitions.append(
                Transition(
                    kind="task",
                    item_id=task.task_id,
                    from_state="absent",
                    to_state=task.state,
                )
            )
            continue
        if before.state != task.state:
            transitions.append(
                Transition(
                    kind="task",
                    item_id=task.task_id,
                    from_state=before.state,
                    to_state=task.state,
                )
            )

    if not transitions:
        raise FastlaneTodoError("INVALID_TRANSITION", "no transitions")
    if len(transitions) > MAX_TRANSITIONS:
        raise FastlaneTodoError("INVALID_TRANSITION", "too many transitions")

    plan = _build_plan(observed.workflow_state, observed.tasks)
    if len(plan) > MAX_TRANSITIONS:
        raise FastlaneTodoError("INVALID_PLAN", "too many plan items")

    delta_id = _sha256_hex(
        _canonical_json(
            {
                "workflow_id": workflow_id,
                "from_fingerprint": acked.fingerprint,
                "to_fingerprint": observed.fingerprint,
                "plan": [_serialize_plan_item(item) for item in plan],
            }
        )
    )
    return FastlaneTodoDelta(
        schema=DELTA_SCHEMA,
        delta_id=delta_id,
        workflow_id=workflow_id,
        from_fingerprint=acked.fingerprint,
        to_fingerprint=observed.fingerprint,
        reason="state_transition",
        transitions=tuple(transitions),
        plan=plan,
    )


def _serialize_plan_item(item: PlanItem) -> dict[str, str]:
    return {"step": item.step, "status": item.status}


def _serialized_state_size_ok(serialized: str) -> None:
    if len(serialized.encode("utf-8")) > MAX_BYTES:
        raise FastlaneTodoError("INVALID_PAYLOAD", "serialized payload too large")


def _write_json_atomically(path: Path, payload: Any) -> None:
    serialized = _canonical_json(payload)
    _serialized_state_size_ok(serialized)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    with open(tmp_path, "wb") as stream:
        stream.write(serialized.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp_path, path)
    if os.name != "nt":
        with open(path.parent, "rb") as directory:
            os.fsync(directory.fileno())


def _load_state(path: Path, workflow_id: str) -> FastlaneTodoState:
    if not path.exists():
        return _default_state(workflow_id)
    try:
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise FastlaneTodoError(
            "FASTLANE_STATE_CORRUPT", "state JSON corrupt"
        ) from error
    return _parse_state(payload, workflow_id=workflow_id)


def _write_state(path: Path, state: FastlaneTodoState) -> None:
    _write_json_atomically(path, _serialize_state(state))


def _write_pending_delta(path: Path, delta: FastlaneTodoDelta | None) -> None:
    if delta is None:
        if path.exists():
            path.unlink()
        return
    _write_json_atomically(path, _serialize_delta(delta))


def _next_epoch(existing: FastlaneTodoDebounceState | None) -> int:
    if existing is None:
        return 1
    return existing.epoch + 1


def _advance_debounce(
    current: FastlaneTodoDebounceState | None,
    now: float,
) -> FastlaneTodoDebounceState:
    if current is None:
        return FastlaneTodoDebounceState(
            epoch=1, first_seen_at=now, due_at=now + DEBOUNCE_DELAY
        )
    due = min(current.first_seen_at + DEBOUNCE_CAP, now + DEBOUNCE_DELAY)
    return FastlaneTodoDebounceState(
        epoch=current.epoch,
        first_seen_at=current.first_seen_at,
        due_at=due,
    )


def _has_debounce_expired(current: FastlaneTodoDebounceState, now: float) -> bool:
    return now - current.first_seen_at >= DEBOUNCE_CAP


def _resolve_data_root(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    for env_name in (
        "FASTLANE_TODO_DATA_ROOT",
        "FASTLANE_DATA_ROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    ):
        value = os.environ.get(env_name)
        if value:
            return Path(value).resolve()
    return (Path(__file__).resolve().parent / "fastlane-data").resolve()


@contextmanager
def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import msvcrt

        with open(path, "a+b") as stream:
            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.001)
            try:
                yield
            finally:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    else:
        import fcntl

        with open(path, "a+b") as stream:
            stream.seek(0)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _path_set(root: Path, workflow_id: str) -> tuple[Path, Path, Path]:
    workflow_root = root / "fastlane-todo-v1" / workflow_id
    return (
        workflow_root / "state.json",
        workflow_root / "pending-delta.json",
        workflow_root / "projection.lock",
    )


class FastlaneTodoProjector:
    """Deterministic durable projector."""

    def __init__(
        self, data_root: Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._data_root = data_root
        self._clock = clock

    def _load(self, workflow_id: str) -> FastlaneTodoState:
        state_path, _, _ = _path_set(self._data_root, workflow_id)
        return _load_state(state_path, workflow_id)

    def _write(
        self,
        workflow_id: str,
        state: FastlaneTodoState,
        pending: FastlaneTodoDelta | None,
    ) -> FastlaneTodoState:
        state_path, pending_path, _ = _path_set(self._data_root, workflow_id)
        _write_state(state_path, state)
        _write_pending_delta(pending_path, pending)
        return state

    def observe(
        self, source_payload: str, *, now: float | None = None
    ) -> dict[str, Any]:
        snapshot = _parse_source(source_payload)
        observed = _source_to_observed(snapshot)
        now_value = float(now if now is not None else self._clock())

        state_path, _, lock_path = _path_set(self._data_root, snapshot.workflow_id)
        with _acquire_lock(lock_path):
            state = _load_state(state_path, snapshot.workflow_id)
            _validate_progressions(state.observed, observed)

            # Preserve latest observed only when no unacked delta exists.
            if state.pending_delta is not None:
                state = FastlaneTodoState(
                    schema=state.schema,
                    workflow_id=state.workflow_id,
                    generation=state.generation + 1,
                    acked=state.acked,
                    observed=state.observed,
                    pending_delta=state.pending_delta,
                    debounce=_advance_debounce(
                        state.debounce if state.debounce is not None else None,
                        now_value,
                    ),
                )
                self._write(snapshot.workflow_id, state, state.pending_delta)
                return _serialize_event("deferred", snapshot.workflow_id, None)

            if observed.fingerprint == state.acked.fingerprint:
                state = FastlaneTodoState(
                    schema=state.schema,
                    workflow_id=state.workflow_id,
                    generation=state.generation + 1,
                    acked=state.acked,
                    observed=observed,
                    pending_delta=None,
                    debounce=None,
                )
                self._write(snapshot.workflow_id, state, None)
                return _serialize_event("noop", snapshot.workflow_id, None)

            if state.debounce is None:
                debounce = FastlaneTodoDebounceState(
                    epoch=_next_epoch(state.debounce),
                    first_seen_at=now_value,
                    due_at=now_value + DEBOUNCE_DELAY,
                )
            elif state.observed == observed:
                debounce = state.debounce
            else:
                debounce = _advance_debounce(state.debounce, now_value)
            if _has_debounce_expired(debounce, now_value):
                delta = _build_delta(snapshot.workflow_id, state.acked, observed)
                state = FastlaneTodoState(
                    schema=state.schema,
                    workflow_id=state.workflow_id,
                    generation=state.generation + 1,
                    acked=state.acked,
                    observed=observed,
                    pending_delta=delta,
                    debounce=None,
                )
                self._write(snapshot.workflow_id, state, delta)
                return _serialize_event(
                    "delta", snapshot.workflow_id, _serialize_delta(delta)
                )

            if now_value < debounce.due_at:
                state = FastlaneTodoState(
                    schema=state.schema,
                    workflow_id=state.workflow_id,
                    generation=state.generation + 1,
                    acked=state.acked,
                    observed=observed,
                    pending_delta=None,
                    debounce=FastlaneTodoDebounceState(
                        epoch=debounce.epoch,
                        first_seen_at=debounce.first_seen_at,
                        due_at=debounce.due_at,
                    ),
                )
                self._write(snapshot.workflow_id, state, None)
                return _serialize_event("deferred", snapshot.workflow_id, None)

            delta = _build_delta(snapshot.workflow_id, state.acked, observed)
            state = FastlaneTodoState(
                schema=state.schema,
                workflow_id=state.workflow_id,
                generation=state.generation + 1,
                acked=state.acked,
                observed=observed,
                pending_delta=delta,
                debounce=None,
            )
            self._write(snapshot.workflow_id, state, delta)
            return _serialize_event(
                "delta", snapshot.workflow_id, _serialize_delta(delta)
            )

    def recover(self, workflow_id: str) -> dict[str, Any]:
        workflow_id = _require_id(workflow_id, "workflow_id")
        state_path, _, lock_path = _path_set(self._data_root, workflow_id)
        with _acquire_lock(lock_path):
            state = _load_state(state_path, workflow_id)
            if state.pending_delta is not None:
                return _serialize_event(
                    "delta", workflow_id, _serialize_delta(state.pending_delta)
                )

            if state.observed.fingerprint == state.acked.fingerprint:
                return _serialize_event("noop", workflow_id, None)

            delta = _build_delta(workflow_id, state.acked, state.observed)
            state = FastlaneTodoState(
                schema=state.schema,
                workflow_id=state.workflow_id,
                generation=state.generation + 1,
                acked=state.acked,
                observed=state.observed,
                pending_delta=delta,
                debounce=None,
            )
            self._write(workflow_id, state, delta)
            return _serialize_event("delta", workflow_id, _serialize_delta(delta))

    def ack(self, workflow_id: str, delta_id: str) -> dict[str, Any]:
        workflow_id = _require_id(workflow_id, "workflow_id")
        if not delta_id.startswith("sha256:") or len(delta_id) != 71:
            raise FastlaneTodoError("INVALID_INPUT", "delta id malformed")

        state_path, _, lock_path = _path_set(self._data_root, workflow_id)
        with _acquire_lock(lock_path):
            state = _load_state(state_path, workflow_id)
            if state.pending_delta is None:
                return _serialize_event("noop", workflow_id, None)
            if state.pending_delta.delta_id != delta_id:
                raise FastlaneTodoError("ACK_UNKNOWN", "delta id unknown")

            acked = FastlaneTodoAckedState(
                fingerprint=state.pending_delta.to_fingerprint,
                workflow_state=state.observed.workflow_state,
                tasks=tuple(
                    AckedTask(
                        task_id=item.task_id,
                        state=item.state,
                        version=item.version,
                    )
                    for item in state.observed.tasks
                ),
            )
            state = FastlaneTodoState(
                schema=state.schema,
                workflow_id=state.workflow_id,
                generation=state.generation + 1,
                acked=acked,
                observed=state.observed,
                pending_delta=None,
                debounce=None,
            )
            self._write(workflow_id, state, None)
            return _serialize_event("noop", workflow_id, None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fastlane-todo-projection")
    parser.add_argument("--data-root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("observe")
    recover = commands.add_parser("recover")
    recover.add_argument("workflow_id")
    ack = commands.add_parser("ack")
    ack.add_argument("workflow_id")
    ack.add_argument("--delta-id", required=True, dest="delta_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_root = _resolve_data_root(args.data_root)
    projector = FastlaneTodoProjector(data_root)
    try:
        if args.command == "observe":
            event = projector.observe(sys.stdin.read())
        elif args.command == "recover":
            event = projector.recover(args.workflow_id)
        elif args.command == "ack":
            event = projector.ack(args.workflow_id, args.delta_id)
        else:
            raise FastlaneTodoError("INVALID_INPUT", f"unknown command {args.command}")
    except FastlaneTodoError as error:
        workflow_id = getattr(args, "workflow_id", "")
        event = _serialize_event(
            "error",
            workflow_id,
            {"code": error.code, "message": str(error)},
        )
        print(_canonical_json(event))
        return 2
    print(_canonical_json(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
