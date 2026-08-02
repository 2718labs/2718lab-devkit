---
name: fast-lane-routing
description: Execute a compiled 2718lab Fast Lane plan with explicit model/effort routing, automatic cross-session selection, and bounded index context. Use when a Fast Lane assignment, cross-session projection, or host dispatch packet is present.
---

# Fast Lane Routing

Use the compiler output as an executable dispatch contract. Do not turn it into
a prose task list or ask the LLM to rediscover routing.

## Dispatch

1. For every `action="start"`, call the host dispatch tool with the exact
   `assignment.host_dispatch.model` and `.reasoning_effort`.
2. Reject a dispatch when `require_explicit_route` is false, the route is
   missing, or `inherit_current_session_model` is true. Never let the current
   conversation model fill either argument.
3. Treat `cross_session_dispatch_projection.status="external_session_required"`
   as an automatic decision: dispatch every listed assignment to an isolated
   independent session/worktree. Do not ask the LLM to choose local versus
   cross-session, and do not create a session when the status is
   `not_required` or `blocked`.
4. Preserve the assignment's lease, context, route, and predecessor fences.
   A missing or mismatched fence is fail-closed; it is not a reason to retry
   with an unrelated start.

## Index context

- Prepare/consume exactly one `assignment.index_context` packet per assignment.
- The host performs the bounded input query at dispatch and the output query at
  the terminal boundary. The worker consumes the packet's scope and anchors.
- Do not call `project_index_register`, `project_index_sync`,
  `project_index_status`, or `project_index_query` from inside an item, and do
  not poll commentary or status.
- Stop when the packet is missing, over-bound, or hash-invalid.

## Completion

Refill only after a validated terminal event. Keep retained slots retained;
never respawn them from commentary. Report the exact model, effort, dispatch
mode, worktree, and verification evidence.
