# ORCH-03A Service Store API

Owner: terra-store-api
Depends on: ORCH-02

## Goal

Add the minimal public store operations required by orchestration service code without exposing the private SQLite connection.

## Context

Read `contracts/orchestrator-api.md` and accept incremental API needs directly from the ORCH-03 owner. Do not read the ORCH-03 task body or sibling cards.

## Write Scope

- `mcp-tools/orchestrator/store.py`
- `mcp-tools/tests/test_orchestrator_store_service_api.py`

## Steps

1. Write failing tests for workflow cancellation, task result/state CAS, artifact lookup and durable projection reads.
2. Add only transaction-safe public methods needed by ORCH-03; never expose `_connection` or raw cursors.
3. Reply directly to the ORCH-03 owner with operation signatures and stable errors, not source or long logs.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_store_service_api.py -v`; private connection access is absent from service-facing examples.

## Return

Changed files, RED/GREEN output, public signatures and unresolved service needs.
