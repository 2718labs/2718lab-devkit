# Orchestrator API Contract

## Shapes

- `linear`: one task advances through `NEW`, `READY`, `RUNNING`, `VERIFYING`, `DONE`.
- `dag`: registered tasks become `READY` only when every dependency is `DONE`.
- Stop states: `BLOCKED`, `FAILED`, `CANCELLED`.

## Records

- Workflow: id, kind, title, product summary, state, version, policy version, timestamps.
- Task: id, workflow id, title, owner role, state, dependencies, write scope, card hash, result hash, version.
- Lease: task id, owner, epoch, expiry, heartbeat, and optional epoch-bound Codex host target (agent id or canonical task name).
- Event: append-only sequence, workflow/task, type, redacted payload, payload hash.
- Artifact: kind, content hash, safe path, size, redaction version.
- Message: id, workflow id, sender/recipient task ids, correlation id, artifact hash, redacted metadata, created/expiry timestamps, delivery state and ack timestamp.

## Service Operations

- create workflow
- register task
- list ready wave
- claim task with lease epoch
- bind or replace the current lease's host target
- renew or release lease
- complete/fail/block task with expected version and epoch
- cancel workflow
- return product/coordinator/agent context projections
- register a task-owned redacted artifact reference
- list authorized peers for a task
- enqueue a peer message and read/ack the recipient mailbox
- resolve a recipient-owned delivery to its registered artifact metadata

All mutations use SQLite transactions and compare-and-swap. Task completion with a stale epoch returns `STALE_LEASE`.

## MCP Tools

The plugin registers this general coordination server as `2718lab-tools`; Bugkiller is a consumer workflow, not the MCP server identity.

Expose `workflow_create`, `workflow_register_task`, `workflow_ready`, `workflow_claim`, `workflow_endpoint_bind`, `workflow_complete`, `workflow_status`, `workflow_context`, `workflow_artifact_register`, `workflow_peers`, `workflow_message_send`, `workflow_inbox`, `workflow_artifact_resolve`, `workflow_message_ack`, and `workflow_cancel` as thin JSON wrappers. MCP never starts models, calls host collaboration tools, or executes repository commands.

### Peer message tools

- `workflow_artifact_register({workflow_id, task_id, owner, lease_epoch, kind, artifact_hash, safe_path, size, redaction_version})` requires the task's valid lease and records a task-owned artifact reference without accepting or returning body content. Paths must remain inside the configured evidence root; the same hash is idempotent only when ownership and metadata match.
- `workflow_peers({workflow_id, task_id})` returns only permitted peer task ids, the qualifying relationship (`dependency_edge` or `contract_subscriber`), and a scoped delivery capability. Registration fixes contract subscriptions; a message cannot create one.
- `workflow_claim(..., host_target?)` may atomically bind the exact agent id or canonical task name returned by `spawn_agent`. `workflow_endpoint_bind({workflow_id, task_id, owner, lease_epoch, host_target})` binds or replaces it after claim. An unbound lease is mailbox-only; lease takeover clears the previous epoch's target.
- `workflow_message_send({workflow_id, sender_task_id, recipient_task_id, owner, lease_epoch, correlation_id, artifact_hash, metadata, ttl_seconds})` requires the sender's explicit valid lease and a peer capability. It validates artifact ownership/redaction, positive bounded TTL, and per-sender/per-recipient/workflow count and byte quotas, then creates one durable recipient-mailbox entry idempotently by sender + correlation id + artifact hash. If the recipient has a current bound target, it returns an executable `collaboration.send_message` envelope with `arguments.target` and a compact fixed-field `arguments.message`; the sender, not MCP or the coordinator, invokes it.
- `workflow_inbox({workflow_id, recipient_task_id, owner, lease_epoch, cursor, limit})` requires the recipient's explicit lease and returns that recipient's unexpired, unacknowledged entries and artifact references. It never returns another task's mailbox or message bodies through coordinator context.
- `workflow_artifact_resolve({workflow_id, recipient_task_id, owner, lease_epoch, delivery_id})` requires the recipient's current lease and an unexpired owned delivery, and returns only the registered artifact kind, hash, safe path, size, and redaction version.
- `workflow_message_ack({workflow_id, recipient_task_id, owner, lease_epoch, delivery_id})` requires the recipient's explicit lease, is idempotent, and records acknowledgement without deleting the audit summary.

Message bodies are protected artifacts addressed by `artifact_hash`; mailbox rows and events carry only hashes plus redacted metadata. The coordinator neither receives nor relays body content. A host wake-up failure leaves the durable entry intact; inbox is authoritative for offline/recovery delivery. TTL expiry prevents resolve/ack processing, and quota/authorization/hash failures return stable errors.

Permission to message is not permission to claim a task, inspect a card, access a contract, alter a write scope, or perform any other workflow action.

## Context Projection

- product: direction, overall state, current blockers, next user gate.
- coordinator: DAG states, current wave, write conflicts, budgets, artifact hashes.
- agent: one task card, direct contracts, required evidence, exact write scope and acceptance.

Sibling card bodies and unrelated logs never appear in agent context.
