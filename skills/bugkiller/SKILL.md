---
name: bugkiller
description: Use for a bounded 2718lab DevKit repository fix through the Bugkiller workflow. Require an explicit DevKit task or user request; never load it for an unrelated project.
---

# Bugkiller

范围门：仅使用本任务已绑定的 DevKit 任务卡、工作树和证据；不把它们带入其他项目。

The coordinator owns dispatch, integration, and final acceptance. Use a Sol
lane only when the exact route warrants architecture, a hard diagnosis, or an
independent terminal review.
Read the assigned task card and direct contracts, then follow
[roles and routing](references/roles.md), [workflow and delivery](references/workflow.md),
and [safety](references/safety.md).

## Start

1. Route routine/bounded execution to Terra High: `gpt-5.6-terra`, `high`.
2. Route moderately complex or harder execution to Terra Max:
   `gpt-5.6-terra`, `max`.
3. Use Sol High: `gpt-5.6-sol`, `high`, only for an explicitly exceptional
   bounded execution, deep investigation, architecture, or terminal review.
4. Bugkiller's separately validated profile currently does not admit Luna;
   never rename a substitute as Luna. This restriction does not override a
   Fast Lane route whose host capability report attests Luna for another module.
5. A worker creates scoped evidence and a candidate commit; an independent
   reviewer is used only when the route requires one, and the coordinator
   orders integration and accepts.

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
