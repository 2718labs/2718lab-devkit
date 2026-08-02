# Team Shapes and Routing

The coordinator owns decomposition, dispatch, integration, and final
acceptance. It may perform mechanical integration directly. Use a Sol lane only
when its exact host-attested route justifies architecture, a hard cross-boundary
diagnosis, or independent terminal review. Execute in parallel only when cards
have disjoint write scopes; same-path work queues behind the active owner.

## Current host policy

- Terra High (`gpt-5.6-terra`, `high`) handles routine, bounded coding,
  testing, debugging, documentation, investigation, and validation.
- Terra Max (`gpt-5.6-terra`, `max`) handles moderately complex or harder
  implementation, integration, refactoring, security-sensitive work, and
  difficult regressions.
- Sol High (`gpt-5.6-sol`, `high`) is only for explicit exceptional bounded
  execution, deep investigation, architecture, or independent terminal review.
  Its evidence informs the coordinator; it does not transfer acceptance ownership.
- Luna (`gpt-5.6-luna`, `low`/`medium`/`high`/`xhigh`) is eligible only when
  the current Codex host capability report attests the exact requested pair;
  otherwise the route is unavailable and no model is silently substituted. A
  request without an effort uses the profile's `medium` default only when that
  exact pair is attested. This shared Codex host policy does not alter
  Bugkiller, which remains a separate policy surface.

## Fast Lane v3 worker selection

Fast Lane does not promote the host policy above into a fixed scheduler table.
For each `(task_id, scheduler_role)`, the host attests a complete routing-core
request; the adapter may emit only the core-resolved, exact model/effort pair
and its bounded route receipt. A missing, duplicate, unknown, mismatched, or
capability-unavailable entry has no `recommended_route` fallback and makes the
entire dispatch matrix `NO_SAFE_WORK`, with no worker assignment or queue. This permits an exactly attested Luna pair where appropriate
while preserving Terra and Sol safety floors without scheduler-side guessing.

`ultra` is lane-0 activation, never a worker effort. Lane 0 is the coordinator
lane, not a fixed model selection; a Sol route is used only when the exact
host-attested decision requires it. Prewarm is a read-only evidence role, not
an execution role. Route receipts bind the dispatch event and historical core
input so later lease/capability facts cannot rewrite an already-issued task.
The rendered assignment's `host_dispatch.model` and
`host_dispatch.reasoning_effort` are mandatory arguments to
`collaboration.spawn_agent`; `inherit_current_session_model` is false and a
missing route is rejected. The host never lets the active conversation (even
if its UI says Luna) fill those arguments implicitly.
Each assignment also carries one bounded `index_context`; the host prepares
its input/output query at lifecycle boundaries and the worker only consumes the
packet, with no index polling or hand-written query choreography.
The host archives only after coordinator acceptance and final evidence binding.
Fast Lane task temporary roots stay below the current compiler-approved
`D:\bun\tmp\codex\<project-or-thread>` root. The quota sample cache remains a
separately user-configurable path, and C-drive temporary roots remain forbidden.

No execution worker merges another task, changes a sibling scope, or accepts
its own task. A candidate commit and evidence always return to the coordinator.

## GitHub-style local flow

`task card + base revision -> isolated task branch/worktree -> scoped commit +
evidence -> independent review when routed -> ordered integration/rebase -> CI gate -> release gate`

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
Role: <exact host-attested model / reasoning-effort>
Task card: <absolute path>/tasks/<id>.md
Base revision: <commit>
Write scope: <exact files or directories>
Acceptance: <exact commands and expected result>
Return: scoped candidate commit, evidence hash, blockers.
Forbidden: sibling changes, direct merge/rebase, unreviewed acceptance.
```
