# ORCH-04 Peer Messaging and Durable Mailbox

Owner: terra-service
Depends on: ORCH-03, ORCH-04A

## Goal

Add authorized task-to-task peer discovery, direct online delivery hints and a crash-recoverable recipient mailbox without making the coordinator a message-body relay.

## Context

Read `contracts/orchestrator-api.md` and `contracts/host-boundaries.md`. Do not request sibling task cards: the service must derive authorization from registered dependency edges and contract subscriptions only.

## Write Scope

- `mcp-tools/orchestrator/service.py`
- `mcp-tools/tests/test_orchestrator_messaging.py`

## Steps

1. Write failing tests for allowed dependency peers and common contract subscribers; reject unrelated tasks and sender/recipient lease violations.
2. Send missing mailbox-store operation requirements directly to the ORCH-04A owner as signature-level deltas, then implement `workflow_artifact_register`, `workflow_peers`, `workflow_message_send`, `workflow_inbox` and `workflow_message_ack` with stable errors and recipient-only reads.
3. Register task-owned body/attachment metadata without accepting body content, then reference it by hash. Events, status and contexts retain only redacted metadata, delivery id, correlation id and artifact hash.
4. Return an online direct `send_message` instruction to the sending agent when applicable. The coordinator must never accept, relay or expose message bodies; a durable mailbox entry remains the recovery source.
5. Enforce bounded TTL and sender/recipient/workflow count and byte quotas. Ack is recipient-only, idempotent and auditable; expired messages are not delivered.
6. Ensure messages grant no card, contract, lease, role or write-scope access beyond the pre-existing dependency edge or contract subscription.

## Acceptance

Run `python -m unittest mcp-tools/tests/test_orchestrator_messaging.py -v`. Assert online delivery uses a returned direct instruction rather than coordinator relay; durable inbox survives service restart; body text is absent from events/status/context; authorization, artifact hash validation, correlation idempotency, TTL, quota and recipient-only ack are covered.

## Return

Changed files, tool signatures, test output, quota/TTL defaults and any host `send_message` limitation.
