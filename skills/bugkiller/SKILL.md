---
name: bugkiller
description: Use when triaging, implementing, validating, or integrating a bounded repository fix through the Bugkiller workflow.
---

# Bugkiller

Sol owns architecture, dispatch, review, integration, and final acceptance.
Read the assigned task card and direct contracts, then follow
[roles and routing](references/roles.md), [workflow and delivery](references/workflow.md),
and [safety](references/safety.md).

## Start

1. Route routine/bounded execution to Terra High: `gpt-5.6-terra`, `high`.
2. Route moderately complex or harder execution to Terra Max:
   `gpt-5.6-terra`, `max`.
3. Use Sol High: `gpt-5.6-sol`, `high`, only for an explicitly exceptional
   bounded execution or deep investigation.
4. Luna is unavailable. Never attempt a Luna spawn or rename a substitute.
5. A worker creates scoped evidence and a candidate commit; Sol alone reviews,
   orders integration, and accepts.

## Non-negotiables

- Do not silently substitute a model or reasoning level.
- A worker never merges another task, expands its write scope, or accepts its
  own work.
- MCP stores lease-bound metadata and durable artifacts; it never starts an
  agent, calls host collaboration tools, or relays large message bodies.
- Durable delivery is `workflow_artifact_register -> workflow_message_send ->
  workflow_inbox -> workflow_artifact_resolve -> workflow_message_ack`.
- Without MCP, record `DEGRADED_SKILL_ONLY` and use the card's scoped serial
  fallback. Do not pretend direct chat is durable task context.
