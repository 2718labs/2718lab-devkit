# 2718lab Project Index Design

## Goal

Build a deterministic, self-owned project index that makes the plugin-mediated
development workflow index-first across source code, Markdown plans and
contracts, configuration, tests, evidence, diffs, and rollback checkpoints.

## Product Boundary

- The index is a mandatory access plane for strict workflows, not the source of
  truth. Repository files and orchestrator SQLite records remain authoritative.
- Index facts are created only by hashers and deterministic parsers. Models may
  trigger refreshes and inspect coverage, but cannot submit nodes or edges.
- No natural-language summaries, embeddings, or inferred prose relationships.
- Automatic restore is limited to plugin-registered task-owned worktrees.
  Original workspaces, commits, remotes, and unrelated user changes are never
  modified automatically.
- Codex currently exposes no plugin hook that can block native shell or file
  tools. The MCP can reject workflow progression and restore operations, but it
  cannot claim OS-level prevention of bypass writes.

## Architecture

Use a unified typed graph in a separate `project-index.sqlite3` database. The
orchestrator remains the control plane; the project index is a rebuildable read
model. Immutable snapshots bind one workspace revision, parser set, and sorted
path/blob manifest. A local content-addressed store holds checkpoint blobs only.

Three implementation units stay independently testable:

1. Core index: scanning, parsers, graph storage, immutable snapshots, lexical
   query, graph expansion, impact query, exact bounded source windows.
2. Workflow integration: task/snapshot binding, query receipts, strict claim and
   completion gates, indexed Markdown work-package references.
3. Checkpoint/restore: scoped CAS capture, drift detection, rescue checkpoint,
   deterministic restore, and snapshot reactivation.

## Typed Graph

Node kinds:

- `repository`, `workspace`, `snapshot`, `file`
- `symbol` including test symbols
- `markdown_block` for headings, links, checkboxes, fences, and structured fields
- `config_entry`, `diff_hunk`, `workflow_ref`, `evidence`, `checkpoint`

Edge relations:

- `contains`, `references`, `depends_on`, `tests`, `configures`
- `changes`, `produces`, `verifies`, `captures`, `restores`

Every node and edge records exact provenance: blob hash, extractor id and
version, path, line/byte span, and one of `observed`, `resolved`, or `declared`.
Unsupported syntax and unresolved dynamic behavior are coverage gaps, never
guessed facts.

## Deterministic Adapters

- Python: standard-library AST for definitions, imports, references, and tests.
- Markdown: headings, links, frontmatter, checkboxes, code fences, and explicit
  workflow fields such as Owner, Depends on, and Write Scope.
- JSON/TOML: parsed key paths and scalar values. YAML uses a conservative
  structured adapter and reports unsupported constructs.
- Other text/code: file, hash, line, and lexical identifier indexing with
  `INDEX_PARTIAL`; adapters can be added without changing the graph contract.

Parser libraries are replaceable implementation dependencies. The graph schema,
incremental engine, query protocol, workflow gates, and checkpoint protocol are
2718lab-owned code.

## Snapshot And Query Rules

- `snapshot_id` is derived from workspace identity, HEAD when present, sorted
  path/blob manifest, index schema, and parser-set hash.
- Snapshots are immutable. Any content change creates a successor snapshot.
- Blob parser results are reused across snapshots; path-dependent edges are
  resolved again after rename or move.
- Query-time source windows re-hash the file. Mismatch returns `INDEX_STALE`.
- Queries are bounded by node count, edge depth, source lines, and byte budget.
- Each successful query records a `trace_id` with snapshot, filters, returned
  nodes, coverage gaps, and fallback reason.

Index states are `INDEX_READY`, `INDEX_PARTIAL`, `INDEX_STALE`,
`INDEX_UNAVAILABLE`, and `INDEX_CORRUPT`. Strict mutation workflows require a
fresh snapshot for every path in their write scope. `INDEX_MISS_ESCAPE` allows a
bounded, audited source read for parser gaps without pretending the index was
complete.

## Index-First Workflow

1. Bootstrap captures the user request and first immutable snapshot.
2. Product brief, plan, contracts, and task cards are indexed immediately.
3. Task registration binds the input snapshot and explicit task/contract nodes.
4. Agent context returns graph handles and bounded query affordances.
5. A write task must record a query trace and pre-write checkpoint.
6. After a write, the changed paths produce a successor snapshot and indexed
   diff. Impact traversal selects relevant tests and contracts.
7. Verification evidence binds the output snapshot. Strict completion rejects
   stale snapshots, missing traces, missing checkpoints, or evidence from a
   different snapshot.
8. Peer messages carry node ids, snapshot ids, and artifact hashes instead of
   copying document or source bodies.

## Checkpoint And Restore

Checkpoint identity includes workflow/task, lease epoch, canonical worktree,
input snapshot, write-scope hash, manifest hash, and CAS root hash. Restore first
requires the current tree to match the recorded expected snapshot, then creates
a rescue checkpoint before changing files. It writes or removes only registered
scope entries, rejects symlinks/reparse points and path escape, refreshes the
index, and verifies that the restored snapshot matches the target.

Drift returns `ROLLBACK_DRIFT` with zero restore writes. An unowned workspace
returns `WORKTREE_UNOWNED`. Original repositories and remote effects always
require separate user-controlled recovery.

## Open Source

The implementation remains in the AGPL-3.0 `2718lab-devkit` initially, with a
module boundary suitable for a later standalone repository. Third-party parser
licenses and notices must be recorded before bundling new parser packages.

## Acceptance

- Identical content and parser versions produce identical snapshots and graph ids.
- Markdown plans, task cards, contracts, code, config, and tests are queryable
  without model-generated summaries.
- Stale source, parser gaps, dynamic references, and unsupported formats are
  reported explicitly.
- Strict tasks cannot complete without a fresh output snapshot, query trace,
  checkpoint, indexed diff, and matching verification evidence.
- Restore returns task-owned worktrees byte-for-byte to the checkpoint and never
  changes the original workspace.
- Existing non-strict workflows and Bugkiller peer messaging remain compatible.

