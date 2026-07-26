# IDX-03 Strict Index Workflow And MCP

Owner: sol-ultra-strict-runtime
Depends on: IDX-01A core API

## Goal

Enforce the strict index contract in the durable orchestrator and expose the
six index/checkpoint MCP tools without changing legacy task behavior.

## Context

- Read `../contracts/project-index-api.md` and
  `../contracts/strict-index-workflow.md` only.
- The core and checkpoint public APIs are fixed; do not edit them.
- Existing tasks default to `strict_index=false`.
- The server uses official SDK v1 `mcp.server.fastmcp`.

## Write Scope

- `mcp-tools/orchestrator/store.py`
- `mcp-tools/orchestrator/service.py`
- `mcp-tools/server.py`
- `mcp-tools/tests/test_project_index_workflow.py`
- `mcp-tools/tests/test_mcp_contract.py`
- `mcp-tools/tests/test_orchestrator_store.py`

## Steps

1. Write failing tests first for schema migration, strict registration, claim,
   query receipts, checkpoint ownership, snapshot-bound verification, strict
   completion gates, and all six MCP envelopes/annotations.
2. Preserve every non-strict workflow test and public wrapper default.
3. Keep strict completion checks transactional with lease fencing.
4. Derive checkpoint workspace/scope from the stored binding; callers cannot
   widen it.
5. Run focused workflow/MCP tests, then all `mcp-tools/tests`.

## Acceptance

Strict write and read-only tasks enforce the contract; legacy workflows remain
compatible; all six tools work through the standard JSON envelope.

## Return

Changed files, RED/GREEN output, schema migration notes, and blockers. Do not
spawn a reviewer, commit, push, or create a PR.
