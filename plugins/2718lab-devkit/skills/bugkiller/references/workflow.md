# Workflow and Durable Peer Delivery

The coordinator advances a task through `NEW -> TRIAGED -> PATCHING -> VERIFYING ->
SOL_REVIEW -> INTEGRATING -> CI_GATE -> RELEASE_GATE`. `SOL_REVIEW` is the
stable name of the independent terminal-review stage; it does not mandate Sol
for every task. A worker can provide evidence but cannot move its own result to
accepted or released.

## Strict local write gate

For a strict task use this evidence order:
`project_index_sync -> strict_index=true -> project_index_query -> trace_id ->
worktree_checkpoint_create -> project_index_sync(bind_as="output") ->
project_index_query -> trace_id -> workflow_artifact_register(kind="verification", snapshot_id=...)`.
The coordinator checks the candidate evidence, with an independent review when
the route requires it, before `workflow_complete`.

## GitHub-style local integration

Use `task card + base revision -> isolated task branch/worktree -> scoped
commit + evidence -> independent review when routed -> ordered integration/rebase -> CI gate ->
release gate`. This local protocol does not authorize a remote push, pull
request, network action, or release publication. Workers must not merge or
rebase a sibling task.

## Durable MCP handoff

Use this exact durable sequence:

`workflow_artifact_register -> workflow_message_send -> workflow_inbox ->
workflow_artifact_resolve -> workflow_message_ack`

1. Register a redacted, immutable, task-owned artifact. Keep logs and large
   evidence in that artifact rather than in the message.
2. Send only bounded metadata, an artifact hash, delivery id, correlation id,
   and bounded TTL with `workflow_message_send`.
3. The sender may execute returned `collaboration.send_message` arguments to
   wake a bound peer. That direct chat is not the durable source of truth.
4. The recipient reads `workflow_inbox`, resolves its permitted artifact, then
   acknowledges with `workflow_message_ack` after processing it.

This delivery does not grant a task claim, access to another card, write-scope
expansion, integration authority, or acceptance. If MCP is unavailable, record
`DEGRADED_SKILL_ONLY` and preserve a scoped serial evidence trail.
