---
name: bugkiller
description: Use when triaging, investigating, patching, or verifying a repository bug through the Bugkiller workflow.
---

# Bugkiller

Coordinate a bounded bug fix using the workflow record as the source of truth. Keep role, state, routing, and safety detail in [references](references/).

## Start

1. Read the assigned task card and direct contracts only.
2. Route executable/code writes to the Sol code writer with dispatch model `gpt-5.6-sol` and reasoning `ultra`; Luna and Terra never write code.
3. Follow [roles and routing](references/roles.md), then [workflow and delivery](references/workflow.md).
4. Enforce [safety and degraded operation](references/safety.md) before commands, writes, or escalation.

## Non-negotiables

- Luna/Terra never write code. The Sol code writer performs approved executable writes; `bugkiller-sol-escalation` remains a separate read-only dangerous-review role.
- Do not automatically request a reviewer or Sol for ordinary, low-risk work.
- MCP coordinates lease-bound host targets, metadata, and durable mailbox references; it never starts agents, calls host collaboration tools, runs commands, or relays message bodies.
- Without MCP or host peer messaging, mark `DEGRADED_SKILL_ONLY` and work serially within the task card's scope.

## References

- [roles.md](references/roles.md): roles, model dispatch, risk routing, and Sol budget.
- [workflow.md](references/workflow.md): task states, artifact-backed peer delivery, inbox, acknowledgement, and TTL.
- [safety.md](references/safety.md): permission boundaries, tainted input, command discipline, and degraded modes.
