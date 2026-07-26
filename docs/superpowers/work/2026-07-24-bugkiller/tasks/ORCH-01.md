# ORCH-01 Domain Scheduler

Owner: terra-core
Depends on: none

## Goal

Implement pure workflow/task types and deterministic linear/DAG readiness rules.

## Context

Read `contracts/orchestrator-api.md`. No FastMCP or SQLite imports in this card.

## Write Scope

- `mcp-tools/orchestrator/__init__.py`
- `mcp-tools/orchestrator/models.py`
- `mcp-tools/orchestrator/scheduler.py`
- `mcp-tools/tests/test_orchestrator_scheduler.py`

## Steps

1. Write failing tests for linear progression, DAG readiness, dependency failure, cancellation and terminal states.
2. Add string enums and frozen records for workflow/task states and kinds.
3. Implement `ready_task_ids(tasks, dependencies)` without database or filesystem access.
4. Reject cycles, missing dependencies and transitions not present in the explicit map.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_scheduler.py -v`; all tests pass and import only the standard library.

## Return

List changed files, test command/output, public functions and unresolved contract conflicts.
