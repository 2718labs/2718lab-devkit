# Team Shapes and Routing

Sol owns architecture, decomposition, dispatch, review, integration, and final
acceptance. Execute in parallel only when cards have disjoint write scopes;
same-path work queues behind the active owner.

## Current host policy

- Terra High (`gpt-5.6-terra`, `high`) handles routine, bounded coding,
  testing, debugging, documentation, investigation, and validation.
- Terra Max (`gpt-5.6-terra`, `max`) handles moderately complex or harder
  implementation, integration, refactoring, security-sensitive work, and
  difficult regressions.
- Sol High (`gpt-5.6-sol`, `high`) is only for explicit exceptional bounded
  execution or deep investigation. Sol still owns final acceptance.
- Luna is unavailable: do not spawn Luna and do not name a substitute Luna.
- Claude routes are Opus coordinator, Sonnet code worker, Haiku light worker,
  and Fable only as an explicitly reasoned powerful/expensive escalation.

No execution worker merges another task, changes a sibling scope, or accepts
its own task. A candidate commit and evidence always return to Sol.

## GitHub-style local flow

`task card + base revision -> isolated task branch/worktree -> scoped commit +
evidence -> Sol review -> ordered integration/rebase -> CI gate -> release gate`

This is local Git discipline, not remote-push or pull-request authorization.
The integration record names candidate/source commit, base revision, accepted
evidence hash, and integration order.

## Durable MCP handoff

The source of truth is the task-owned artifact, not direct chat:

`workflow_artifact_register -> workflow_message_send -> workflow_inbox ->
workflow_artifact_resolve -> workflow_message_ack`

Use `collaboration.send_message` only as a compact wake-up after durable
delivery. It never grants task context, write scope, integration, or acceptance.

## Dispatch template

```text
Role: <Terra High | Terra Max | Sol High>
Task card: <absolute path>/tasks/<id>.md
Base revision: <commit>
Write scope: <exact files or directories>
Acceptance: <exact commands and expected result>
Return: scoped candidate commit, evidence hash, blockers.
Forbidden: sibling changes, direct merge/rebase, unreviewed acceptance.
```
