# Strict Index Workflow Contract

## Compatibility

Existing tasks default to `strict_index=false` and retain their current
register, claim, artifact, complete, messaging, and context behavior. Strict
rules apply only when task registration explicitly opts in.

## Task Binding

`workflow_register_task` adds optional fields:

- `strict_index: bool = false`
- `workspace_root: str = ""`
- `input_snapshot_id: str = ""`
- `task_node_ids: list[str] | None = None`
- `contract_node_ids: list[str] | None = None`

A strict task requires a canonical workspace, input snapshot, and at least one
task node. A strict task with non-empty write scope must bind a linked,
plugin-registered Git worktree; an original checkout is never auto-restorable.

The orchestrator schema version advances without changing existing task rows.
New tables record one task binding, task/query receipts, snapshot-bound
verification artifacts, and append-only binding events. Long bodies remain in
registered artifacts, not SQLite event payloads.

## Six MCP Tools

`project_index_sync`:

- inputs: `workspace`, optional `include_paths`; optional complete task lease
  tuple `workflow_id/task_id/owner/lease_epoch`; `bind_as` is empty or
  `output`.
- output: immutable snapshot; `output` also records the input/output indexed
  diff under the current lease.

`project_index_status`:

- inputs: `workspace`, optional `snapshot_id`, optional `required_paths`.
- read-only output: freshness, coverage gaps, missing and changed paths.

`project_index_query`:

- core query fields from the project-index contract plus optional complete task
  lease tuple.
- read-only output: bounded graph/source result. With a task lease, the
  orchestrator records the persisted core `trace_id` against that task and
  snapshot. `INDEX_MISS_ESCAPE` increments the binding fallback count.

`worktree_checkpoint_create`:

- inputs: `workflow_id`, `task_id`, `owner`, `lease_epoch`, `snapshot_id`.
- derives canonical workspace and write scope from the strict binding; callers
  cannot supply a wider scope. Records the resulting checkpoint id.

`worktree_checkpoint_status`:

- input: `checkpoint_id`; read-only metadata only, never CAS file bodies.

`worktree_checkpoint_restore`:

- inputs: `workflow_id`, `task_id`, `owner`, `lease_epoch`, `checkpoint_id`,
  `expected_current_snapshot_id`.
- destructive annotation; applies checkpoint compare-and-swap and rescue rules.

Sync and checkpoint create are idempotent mutations. Query/status/checkpoint
status are read-only. Restore is destructive. Every wrapper uses the existing
safe JSON envelope and stable errors without tracebacks or source bodies in
errors.

## Existing Tool Extensions

`workflow_artifact_register` adds optional `snapshot_id`. A strict
`kind="verification"` artifact requires it and must match the recorded output
snapshot.

`workflow_claim` for a strict task requires the input snapshot to be current
for the task scope. `INDEX_PARTIAL` is allowed only when no reported gap
intersects that scope; stale, corrupt, unavailable, or uncovered scope blocks.

`workflow_complete` keeps its existing input schema. For a strict write task it
atomically refuses completion unless all are present and mutually consistent:

1. a persisted query receipt for the bound input/output snapshot;
2. a pre-write checkpoint owned by the current task;
3. a current output snapshot;
4. an indexed diff from input to output;
5. verification evidence bound to that output snapshot.

Read-only strict tasks do not require a checkpoint or diff, but still require a
current snapshot, query receipt, and matching verification evidence.

## Errors

Use stable codes: `INDEX_UNAVAILABLE`, `INDEX_CORRUPT`, `INDEX_STALE`,
`INDEX_COVERAGE_GAP`, `QUERY_RECEIPT_REQUIRED`, `CHECKPOINT_REQUIRED`,
`OUTPUT_SNAPSHOT_REQUIRED`, `INDEXED_DIFF_REQUIRED`,
`VERIFICATION_EVIDENCE_REQUIRED`, `SNAPSHOT_MISMATCH`, `STALE_LEASE`,
`WORKTREE_UNOWNED`, and checkpoint contract errors.

## Host Boundary

The MCP rejects strict state progression and unsafe restore. It does not claim
to intercept Codex native shell/read/write tools. Bypass remains visible through
freshness checks, query receipts, fallback count, and completion refusal.
