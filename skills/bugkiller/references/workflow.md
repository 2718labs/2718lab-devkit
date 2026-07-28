# Workflow and Peer Delivery

Advance the task through `NEW -> TRIAGED -> REPRODUCING -> LOCALIZING -> DESIGNING -> PATCHING -> VERIFYING -> DONE`. Low-risk verification can finish at `DONE`; `REVIEWING` requires a dangerous user gate. Stop at `BLOCKED`, `FAILED`, `CANCELLED`, or another documented conditional state when policy requires it.

## Strict index write gate

Legacy tasks retain `strict_index=false`. For a strict write task, keep this exact order: `project_index_sync` -> `strict_index=true` -> `project_index_query` -> `trace_id` -> `worktree_checkpoint_create` -> `project_index_sync(bind_as="output")` -> `project_index_query` -> `trace_id` -> `workflow_artifact_register(kind="verification", snapshot_id=...)` -> `workflow_complete`. The first query validates the input snapshot; checkpoint creation occurs before any write; the output sync and second query validate the produced snapshot before verification is registered and completion is allowed.

If the lease expires, reclaim against the task's current phase snapshot: input before an output is bound, otherwise the bound output. Never move valid task-owned output aside merely to reacquire a lease; after reclaim, repeat only lease-bound receipts and evidence registration.

Bind every live worker before peer delivery. The coordinator takes the exact target returned by `spawn_agent`: depending on the Codex host this is an agent id or a canonical task name such as `/root/investigator`. Supply that value to the worker for atomic `workflow_claim(..., host_target=...)`, or let the worker claim mailbox-only and then call `workflow_endpoint_bind` with its current `owner` and `lease_epoch`. Never invent or derive a target from the workflow task id. A lease without a bound target is mailbox-only, not online; takeover by a new lease epoch clears the old target.

For an authorized peer message, preserve this exact order:

1. Register the redacted, task-owned body reference with `workflow_artifact_register`.
2. Enqueue the mailbox metadata with `workflow_message_send` using the sender's explicit owner and lease epoch, valid peer capability, correlation id, hash, redacted metadata, and a positive bounded TTL.
3. If `direct_instruction` is not null, the sender itself calls host-injected `collaboration.send_message` with the returned `arguments` unchanged. MCP cannot execute that host call. The compact wake-up JSON contains only delivery/workflow/sender ids, correlation id, and artifact hash; it contains no body or log summary.
4. On wake-up or recovery, the recipient supplies its own owner and lease epoch to call `workflow_inbox`, then `workflow_artifact_resolve` for the selected delivery. Read the returned safe artifact path directly; do not ask the coordinator to relay it.
5. Call `workflow_message_ack` only after the artifact has been processed.

The coordinator never receives or forwards body content. It stores only hashes, safe artifact metadata, lease-bound host targets, and redacted metadata, and it does not retry by inventing a new peer permission. A failed or stale host wake-up does not remove the durable mailbox entry; inbox is authoritative for offline and recovery delivery. TTL expiry prevents resolve or acknowledgement processing; artifact, quota, authorization, and hash failures stay errors for the sender to handle within its own task.

Messaging does not grant permission to claim a task, inspect a card or contract, alter write scope, access a repository, or perform any other workflow action. With no MCP or no host peer messaging, mark `DEGRADED_SKILL_ONLY`; do not emulate the protocol or pass message bodies through the coordinator. Work serially from the assigned card and record the missing capability as evidence.
