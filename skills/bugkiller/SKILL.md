---
name: bugkiller
description: Use when triaging, investigating, patching, or verifying a repository bug through the Bugkiller workflow.
---

# Bugkiller

Bugkiller is a specialized defect workflow on top of the DevKit shared execution layer.
It adds defect states, risk routing, and defect evidence
rules; it does not own project indexing, orchestration, approvals, or agents.

## 共享执行层

Use `work-methodology`, `2718lab-tools`, and the same shared roles available to
every other skill: `2718lab-triage`, `2718lab-investigator`,
`2718lab-doc-writer`, `2718lab-code-writer`, `2718lab-verifier`, and
`2718lab-risk-reviewer`.

## Start

1. Read the assigned task card and direct contracts only.
2. Route executable writes to `2718lab-code-writer` with dispatch model `gpt-5.6-sol` and reasoning `ultra`; all other shared roles remain read-only or documentation-only.
3. Follow [roles and routing](references/roles.md), then [workflow and delivery](references/workflow.md).
4. Enforce [safety and degraded operation](references/safety.md) before commands, writes, or escalation.

## Non-negotiables

- Only `2718lab-code-writer` performs approved executable writes;
  `2718lab-risk-reviewer` remains a separate read-only dangerous-review role.
- Do not automatically request a reviewer or Sol for ordinary, low-risk work.
- MCP coordinates lease-bound host targets, metadata, and durable mailbox references; it never starts agents, calls host collaboration tools, runs commands, or relays message bodies.
- Without MCP or host peer messaging, mark `DEGRADED_SKILL_ONLY` and work serially within the task card's scope.

## References

- [roles.md](references/roles.md): roles, model dispatch, risk routing, and Sol budget.
- [workflow.md](references/workflow.md): task states, artifact-backed peer delivery, inbox, acknowledgement, and TTL.
- [safety.md](references/safety.md): permission boundaries, tainted input, command discipline, and degraded modes.
