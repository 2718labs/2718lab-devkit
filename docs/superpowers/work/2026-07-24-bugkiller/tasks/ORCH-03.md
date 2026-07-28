# ORCH-03 Service and Context Projection

Owner: terra-service
Depends on: ORCH-01, ORCH-03A

## Goal

Expose create/register/ready/claim/complete/status/context/cancel operations and minimal role projections.

## Context

Read `contracts/orchestrator-api.md` and `contracts/host-boundaries.md`.

## Write Scope

- `mcp-tools/orchestrator/service.py`
- `mcp-tools/tests/test_orchestrator_service.py`

## Steps

1. Write failing integration tests for DAG waves, write-scope conflicts, stale completion and cancellation.
2. Send any missing store-operation requirement directly to the ORCH-03A owner as a signature-level delta; do not relay it through the coordinator or request raw database access.
3. Implement structured errors with stable codes and safe messages.
4. Implement product, coordinator and agent context projections; agent view contains one card and direct contract hashes only.
5. Deduplicate artifacts and completed inputs by content hash without reusing stale policy versions.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_service.py -v`. Assert sibling card content is absent from agent context and downstream tasks unlock once.

## Return

Changed files, operation signatures, output evidence and any storage contract changes.
