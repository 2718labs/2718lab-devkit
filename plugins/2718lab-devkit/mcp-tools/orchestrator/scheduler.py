"""Deterministic, side-effect-free readiness and transition rules."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from .models import Task, TaskState


class SchedulerError(ValueError):
    """Base class for invalid scheduling input."""


class MissingDependencyError(SchedulerError):
    """Raised when an edge refers to a task that was not registered."""


class CycleError(SchedulerError):
    """Raised when dependency edges do not form a DAG."""


class TransitionError(SchedulerError):
    """Raised when a requested task transition is not explicitly allowed."""


_ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.NEW: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset(
        {TaskState.DONE, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.DONE: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def transition_task(task: Task, next_state: TaskState) -> Task:
    """Return a new task after an explicit valid state transition."""
    if next_state not in _ALLOWED_TRANSITIONS[task.state]:
        raise TransitionError(
            f"cannot transition {task.id!r} from {task.state.value} to {next_state.value}"
        )
    return replace(task, state=next_state, version=task.version + 1)


def ready_task_ids(
    tasks: Sequence[Task], dependencies: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """Return NEW task ids whose complete dependency set is DONE.

    The input task order is preserved, making the ready wave deterministic. The
    dependencies mapping is validated as a complete DAG before readiness is read.
    """
    tasks_by_id = _task_map(tasks)
    graph = _validated_graph(tasks_by_id, dependencies)
    return tuple(
        task.id
        for task in tasks
        if task.state is TaskState.NEW
        and all(
            tasks_by_id[dependency_id].state is TaskState.DONE
            for dependency_id in graph[task.id]
        )
    )


def _task_map(tasks: Sequence[Task]) -> Mapping[str, Task]:
    tasks_by_id = {task.id: task for task in tasks}
    if len(tasks_by_id) != len(tasks):
        raise SchedulerError("task ids must be unique")
    return tasks_by_id


def _validated_graph(
    tasks_by_id: Mapping[str, Task], dependencies: Mapping[str, Sequence[str]]
) -> Mapping[str, tuple[str, ...]]:
    graph = {task_id: tuple(dependencies.get(task_id, ())) for task_id in tasks_by_id}
    unknown_task_ids = set(dependencies).difference(tasks_by_id)
    if unknown_task_ids:
        raise MissingDependencyError(
            f"dependencies declared for unknown tasks: {sorted(unknown_task_ids)!r}"
        )
    for task_id, dependency_ids in graph.items():
        missing = set(dependency_ids).difference(tasks_by_id)
        if missing:
            raise MissingDependencyError(
                f"task {task_id!r} has missing dependencies: {sorted(missing)!r}"
            )
    _reject_cycles(graph)
    return graph


def _reject_cycles(graph: Mapping[str, Sequence[str]]) -> None:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in active:
            raise CycleError(f"dependency cycle includes {task_id!r}")
        if task_id in visited:
            return
        active.add(task_id)
        for dependency_id in graph[task_id]:
            visit(dependency_id)
        active.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)
