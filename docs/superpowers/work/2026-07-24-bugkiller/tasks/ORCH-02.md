# ORCH-02 SQLite Store

Owner: terra-store
Depends on: ORCH-01

## Goal

Persist workflows, tasks, dependencies, leases, events and artifacts with atomic compare-and-swap.

## Context

Read `contracts/orchestrator-api.md`. Use WAL, foreign keys, busy timeout, UTC timestamps and parameterized SQL.

## Write Scope

- `mcp-tools/orchestrator/store.py`
- `mcp-tools/tests/test_orchestrator_store.py`

## Steps

1. Write failing tests for schema creation, reopen persistence, unique dependency edges and append-only event order.
2. Add schema version 1 and transactional helpers.
3. Implement workflow/task CRUD, dependency reads, artifact hash reuse and version CAS.
4. Implement lease acquire/renew/takeover with monotonically increasing epoch.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_store.py -v`. Simulated stale owners must receive `STALE_LEASE`; reopening the DB preserves event order.

## Return

Changed files, schema tables/indexes, real test output and migration risks.
