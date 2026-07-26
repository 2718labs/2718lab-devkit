# Project Index Runtime Contract

## Storage Boundary

- `ProjectIndexService(database_path)` owns the rebuildable index database.
- `CheckpointService(database_path, cas_root, index_service)` stores metadata in
  the index database and file bodies below `cas_root`.
- Neither service writes inside a workspace except an authorized checkpoint
  restore. Both expose `close()` and support deterministic reopen.

## Core Python API

`project_index` exports:

- `IndexError(code, message)` with stable `code`.
- `IndexState`: `INDEX_READY`, `INDEX_PARTIAL`, `INDEX_STALE`,
  `INDEX_UNAVAILABLE`, `INDEX_CORRUPT`.
- `ProjectIndexService.sync(workspace, include_paths=None)`.
- `ProjectIndexService.status(workspace, snapshot_id=None,
  required_paths=None)`.
- `ProjectIndexService.query(workspace, snapshot_id, query, mode="lexical",
  node_kinds=(), relations=(), max_nodes=50, max_depth=1,
  source_lines=12, byte_budget=32768, allow_miss_escape=False)`.
- `ProjectIndexService.diff(from_snapshot_id, to_snapshot_id)`.
- `ProjectIndexService.assert_current(workspace, snapshot_id,
  required_paths=None)`.

Return values are frozen dataclasses and must be accepted by `server._json_safe`.
Snapshot and graph identifiers are deterministic `sha256:` strings. Query
results include a deterministic `trace_id`, state, nodes, edges, exact bounded
source windows, gaps, and a `truncated` flag. No result contains a model-authored
summary.

## Checkpoint Python API

`project_index.checkpoints` exports:

- `WorktreeOwnership(workflow_id, task_id, owner, lease_epoch,
  workspace_root, write_scope)`.
- `CheckpointService.create(ownership, snapshot_id)`.
- `CheckpointService.status(checkpoint_id)`.
- `CheckpointService.restore(ownership, checkpoint_id,
  expected_current_snapshot_id)`.

Create mechanically verifies a linked Git worktree, exact canonical workspace,
scope containment, current snapshot, and absence of symlink/reparse entries.
Restore performs compare-and-swap against `expected_current_snapshot_id`, makes
a rescue checkpoint before writes, changes only registered scope paths, and
never invokes `git reset` or `git checkout`.

Stable errors include `WORKTREE_UNOWNED`, `SCOPE_ESCAPE`,
`UNSAFE_PATH_TYPE`, `INDEX_STALE`, `ROLLBACK_DRIFT`, and `NOT_FOUND`.

## MCP Envelope

All six tools return the existing envelope:

```text
{"ok": true, "data": ...}
{"ok": false, "error": {"code": "STABLE_CODE", "message": "request rejected"}}
```

Tool names are `project_index_sync`, `project_index_status`,
`project_index_query`, `worktree_checkpoint_create`,
`worktree_checkpoint_status`, and `worktree_checkpoint_restore`.

Read-only annotations apply to status/query/checkpoint-status. Sync/create are
idempotent mutations. Restore is destructive and requires workflow/task lease
fields plus expected-current-snapshot compare-and-swap.

## Determinism And Safety

- Parsers/hashers alone create facts; model input cannot create nodes or edges.
- Python uses `ast`; JSON/TOML use standard parsers; Markdown and conservative
  YAML extract only explicit structure. Other text gets lexical/file facts and
  a coverage gap.
- Ignore `.git`, plugin/index databases, CAS roots, common generated caches, and
  paths outside the canonical workspace. Do not follow symlinks or reparse
  points.
- Source windows re-hash files before returning bytes. Mismatch is
  `INDEX_STALE`, never best-effort output.
