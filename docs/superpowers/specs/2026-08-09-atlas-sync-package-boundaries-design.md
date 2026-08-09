# Atlas Sync and Project Index Package Boundaries Design

## Goal

Make the RC4 source tree answer two operational questions with durable,
verifiable behavior:

1. An accepted code task reaches Atlas only through its immutable accepted-task
   evidence and has an explicit projected/pending/quarantined outbox state.
2. A Git-monorepo can be indexed once at its worktree root and then queried or
   checked at a deterministic package boundary without treating a path prefix
   as a package model.

The public MCP server remains a seventeen-tool server.  No filesystem watcher,
repository-root escape, caller-supplied acceptance evidence, or semantic
cross-ecosystem dependency resolver is introduced.

## Atlas: Explicit Drain, Not a Live Mirror

`project_index_sync` remains the sole operation that captures code bytes into
an immutable snapshot.  A host that owns a valid task lease must explicitly
bind the completed code snapshot as `output`; a watcher cannot infer the lease,
scope, checkpoint, or acceptance authority.

The resulting state machine is:

```text
accepted code task
  -> immutable acceptance + PENDING atlas_ingestion_outbox item
  -> explicit atlas_accept(workflow_id, code_task_id, acceptance_id, ingestion_key)
  -> rebuild and validate immutable evidence, output snapshot, receipt, and workspace binding
  -> project Atlas facts
     -> PROJECTED only after projection succeeds
     -> PENDING with bounded retry on unavailable evidence
     -> QUARANTINED on evidence conflict
```

Default runtime composition must construct a call-owned
`ProductionAcceptanceEvidenceReader` for a read/write Atlas UoW using:

- a schema-validated, already-bootstrapped orchestrator store and
  `OrchestratorService`;
- the same UoW's `ProjectIndexService` and controlled `CheckpointService`;
- `ReceiptRepository(config.data_root)`.

The runtime must never call the schema-creating `SQLiteStore(...)` constructor
from a normal invocation.  A prepared/open-existing factory verifies the
existing schema first and owns the resulting connection for the UoW lifetime.
Read-only Atlas UoWs retain no acceptance reader and do not create any durable
state.

The UoW exposes the drain action rather than having the server call
`uow.atlas.accept(...)` directly.  It marks the durable outbox only after the
Atlas projection succeeds.  If Atlas has already projected a deduplicated item
but a prior process stopped before the outbox transition, a safe retry repairs
the pending marker.  No success path may mark a different acceptance as
projected.

The public `atlas_accept` name, four opaque inputs, annotations, result schema,
and error boundary remain unchanged.

## Project Index: Snapshot-Bound Package Descriptors

The existing `include_paths` remains a byte-collection selector, not a package
boundary.  Every snapshot now deterministically discovers package manifests
among its indexed files and persists immutable descriptors:

```text
PackageDescriptor
  package_id      opaque SHA-256-derived identifier, bound to workspace,
                  root, manifest path, ecosystem, and manifest hash
  relative_root   package root relative to the registered workspace (`""` for root)
  manifest_path   indexed relative manifest path
  ecosystem       python | node | cargo
  name            declared manifest name when safely parseable, otherwise ""
  manifest_hash   captured manifest content hash
```

Supported boundary manifests are `pyproject.toml`, `package.json`, and
`Cargo.toml`.  Parsing uses only Python standard-library TOML/JSON readers;
unparseable or structurally invalid manifests create a deterministic coverage
gap, never a guessed package.  Descriptors are ordered by root, manifest path,
ecosystem, and package id.  Nested descriptors are allowed: a selected package
means its root-path closure, not an inferred ownership or dependency relation.

Descriptor rows live in `project_index_snapshot_packages`, are inserted in the
same transaction as the snapshot, participate in the snapshot manifest hash,
and are therefore unavailable across workspaces or snapshots.  Schema version
increments from 4 to 5.  Existing snapshots remain readable but have zero
descriptors; a fresh sync creates the first package-aware snapshot.

`project_index_sync` publishes the first bounded, snapshot-bound package page
with its snapshot result so a caller can obtain opaque package ids without an
unbounded result.  The default page size is 128 descriptors and a page records
`offset`, `limit`, `total_count`, `returned_count`, `packages`, and an optional
`next_offset`.  `project_index_status` can retrieve a continuation page only
when given that same explicit `snapshot_id` plus both page parameters.  Thus a
later sync cannot make a continuation drift to a new catalog.  Default
`project_index_status` output stays unchanged when no page is requested.

Descriptor identity fields remain public, but display fields are bounded.  A
safe name, relative root, or manifest path is emitted directly; an overlong or
unsafe value is replaced by a SHA-256/UTF-8-byte digest in `field_digests` and
the descriptor is marked `representation="digested"`.  An intentionally empty
declared name remains `""`; no host path or raw manifest text crosses the
boundary.

## Package-Scoped Public Behavior

No new MCP tool is added.  The following optional arguments are appended to
the existing signatures:

- `project_index_sync(..., package_page_limit: int = 128)`
- `project_index_status(..., package_ids: list[str] | None = None,
  package_page_offset: int | None = None, package_page_limit: int | None = None)`
- `project_index_query(..., task_lease=None, package_ids: list[str] | None = None)`

Page requests require an explicit nonempty snapshot id and both an offset and
limit; the limit is in `1..128`.  Invalid page input is rejected before a UoW
opens.  `package_ids` never act as a cursor.

`None` preserves every existing workspace-wide behavior.  A supplied selector
list must be nonempty, strictly sorted, unique, syntactically valid, and made
only of descriptor ids in the requested snapshot; bad order/duplicates fail as
`INVALID_QUERY`, and unknown descriptors fail as `NOT_FOUND`.

For status, selected package roots become required scopes (or constrain caller
supplied required paths).  For query, nodes, edges, verified source bytes,
windows, gaps, and the durable query receipt are limited to the union of the
selected roots.  A change in another package therefore does not make a
package-scoped query stale.  Selector ids are persisted in the query receipt
and contribute to its trace identity.

## Coverage Soundness

Before comparing file hashes, status proves that the snapshot's `include_paths`
fully covers every required scope.  An include can cover a required scope only
when it is the same path or an ancestor of it; an empty include set covers the
whole workspace.  Thus:

- `include_paths=("packages/foo",)` covers required `packages/foo`;
- it does **not** cover required `packages`;
- a package selector whose root is outside its snapshot include coverage is
  `INDEX_PARTIAL`, even when one file under that root happened to be indexed.

This closes the false `INDEX_READY` case in which a narrowed snapshot happened
to include one child file while missing the rest of a requested directory.

## Non-Goals

- Live filesystem mirroring or a background watcher.
- Automatic output-snapshot binding without host-owned lease authority.
- Registering a nested package as an independent Git worktree; checkpoints
  continue to use the monorepo worktree root plus a lease-verified write scope.
- Semantic Python/Node/Cargo import resolution, package-manager workspaces,
  lockfile interpretation, build graph inference, or cross-package dependency
  claims.
- Package paths/globs as public selectors or leakage of host absolute paths.

## Compatibility and Acceptance

- The server keeps exactly seventeen public MCP tools.
- `atlas_accept` keeps its exact four input fields and failure surface.
- Legacy no-selector index status/query responses retain their exact data keys.
- A first package page is prepared before a leased sync binds its output
  snapshot, so a public-projection failure cannot advance private lease state.
- Project Index schema migration is forward-only and fresh snapshots are
  deterministic across reopen.
- Atlas remains fail-closed when bootstrap evidence, output snapshot, receipt,
  workspace binding, or package coverage cannot be proven.
