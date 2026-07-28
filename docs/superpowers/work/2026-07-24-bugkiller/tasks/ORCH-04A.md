# ORCH-04A Mailbox Store

Owner: terra-mailbox-store
Depends on: ORCH-03A

## Goal

Persist contract subscriptions, authorized peer relationships, message delivery records, TTL, quotas and recipient acknowledgements as public store operations.

## Context

Read `contracts/orchestrator-api.md`. Accept signature-level deltas directly from the ORCH-04 owner; do not read its task body or message contents.

## Write Scope

- `mcp-tools/orchestrator/store.py`
- `mcp-tools/tests/test_orchestrator_store_messaging.py`

## Steps

1. Write failing tests for dependency/common-contract peers, idempotent correlation, TTL, count/byte quotas and recipient-only ack.
2. Add transaction-safe tables and public operations; message rows store artifact hashes and redacted metadata, never body content.
3. Reply directly to ORCH-04 with signatures and stable error codes only.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_store_messaging.py -v`; reopen preserves unacknowledged inbox state and events contain no body.

## Return

Changed files, schema delta, RED/GREEN output, signatures and migration limits.
