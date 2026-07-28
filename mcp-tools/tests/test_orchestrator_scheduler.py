"""Tests for the pure orchestration domain scheduler."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import Task, TaskState, WorkflowKind
from orchestrator.scheduler import (
    CycleError,
    MissingDependencyError,
    TransitionError,
    ready_task_ids,
    transition_task,
)


def task(task_id: str, state: TaskState = TaskState.NEW) -> Task:
    return Task(
        id=task_id,
        workflow_id="workflow-1",
        title=task_id,
        owner_role="terra",
        state=state,
    )


class SchedulerTests(unittest.TestCase):
    def test_linear_progression_uses_an_explicit_transition_map(self) -> None:
        current = task("one")
        for expected in (
            TaskState.READY,
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.DONE,
        ):
            current = transition_task(current, expected)
            self.assertEqual(current.state, expected)

        with self.assertRaises(TransitionError):
            transition_task(current, TaskState.READY)

    def test_dag_task_is_ready_only_after_all_dependencies_are_done(self) -> None:
        tasks = [task("one", TaskState.DONE), task("two"), task("three")]
        dependencies = {"two": ("one",), "three": ("one", "two")}

        self.assertEqual(ready_task_ids(tasks, dependencies), ("two",))

        tasks[1] = transition_task(tasks[1], TaskState.READY)
        tasks[1] = transition_task(tasks[1], TaskState.RUNNING)
        tasks[1] = transition_task(tasks[1], TaskState.VERIFYING)
        tasks[1] = transition_task(tasks[1], TaskState.DONE)
        self.assertEqual(ready_task_ids(tasks, dependencies), ("three",))

    def test_failed_or_cancelled_dependency_never_makes_a_task_ready(self) -> None:
        for stop_state in (TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED):
            with self.subTest(stop_state=stop_state):
                tasks = [task("one", stop_state), task("two")]
                self.assertEqual(ready_task_ids(tasks, {"two": ("one",)}), ())

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for terminal in (
            TaskState.DONE,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ):
            with self.subTest(terminal=terminal):
                with self.assertRaises(TransitionError):
                    transition_task(task("one", terminal), TaskState.READY)

    def test_cycles_and_missing_dependencies_are_rejected(self) -> None:
        tasks = [task("one"), task("two")]
        with self.assertRaises(CycleError):
            ready_task_ids(tasks, {"one": ("two",), "two": ("one",)})
        with self.assertRaises(MissingDependencyError):
            ready_task_ids(tasks, {"one": ("absent",)})

    def test_models_are_frozen_string_enums(self) -> None:
        self.assertEqual(WorkflowKind.DAG.value, "dag")
        with self.assertRaises(AttributeError):
            task("one").state = TaskState.DONE


if __name__ == "__main__":
    unittest.main()
