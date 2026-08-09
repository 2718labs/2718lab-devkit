# Atlas Sync and Package Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven-development and execute one task in an isolated worktree. Record a real RED command before production edits, then minimal GREEN evidence and a scope-only commit.

**Goal:** Wire the default RC4 Atlas acceptance path to immutable evidence and a durable outbox drain, then add sound snapshot-bound package boundaries to Project Index without adding an MCP tool.

**Architecture:** Atlas remains an explicit snapshot/outbox projection, not a watcher. Project Index persists deterministic manifest descriptors with the snapshot and applies opaque package selectors only to existing status/query operations. The normal monorepo unit remains the registered Git worktree root; package roots are logical index scopes, not checkout ownership roots.

**Tech Stack:** Python 3 standard library, SQLite, FastMCP v1, pytest, Ruff, existing 2718lab runtime composition.

## Routing Record

| Card | Durable identity | Lease | Scope / risk | Route |
|---|---|---|---|---|
| A | `rc4-atlas-acceptance-sync` | `NONE` (isolated source worktree) | lifecycle + evidence/outbox, high | High / Terra xhigh rubric; no host-attested Fast Lane receipt available |
| B | `rc4-project-index-packages` | `NONE` (isolated source worktree) | schema + public selector, high | High / Terra xhigh rubric; no host-attested Fast Lane receipt available |

Cards A and B have no mutable-file overlap.  Each worker must keep caches and
test temp under `D:\bun\tmp\codex\2718-devkit-rc4-atlas-monorepo-20260809`.
Neither card may modify release notes, plugin version, install cache, docs in
this plan/spec pair, or the other card's files.

---

### Task 1: Atlas Runtime Evidence Wiring and Explicit Outbox Drain

**Files:**
- Modify: `mcp-tools/devkit_runtime/uow.py`
- Modify: `mcp-tools/devkit_runtime/project_checkpoint.py`
- Modify: `mcp-tools/orchestrator/store.py`
- Modify: `mcp-tools/server.py`
- Test: `mcp-tools/tests/test_runtime_composition.py`
- Test: `mcp-tools/tests/test_atlas_acceptance_runtime.py`
- Test only if contract changes are accidentally implicated: `mcp-tools/tests/test_mcp_contract.py`

**Prohibited:** public `atlas_accept` parameter/schema changes; Atlas service
evidence bypasses; schema creation/migration during a normal UoW; filesystem
watchers; changes to Project Index package files.

- [ ] Add a focused default-runtime fixture that bootstraps once, writes a
  real accepted-code evidence record with a bound output snapshot, and proves
  the current default UoW fails RED with `ATLAS_EVIDENCE_UNAVAILABLE`.
- [ ] Add RED assertions for the durable state machine: success is the only
  way to `PROJECTED`; unavailable evidence stays `PENDING` with a bounded
  retry; conflicting evidence becomes `QUARANTINED`; and a read-only Atlas
  UoW creates no durable writes.
- [ ] Run the focused RED command and retain its exact failure output:
  `python -m pytest -q tests/test_runtime_composition.py tests/test_atlas_acceptance_runtime.py -k "default or outbox"`.
- [ ] Add a controlled checkpoint-service accessor and a schema-validated
  invocation-owned orchestrator store/service factory.  Close all reader and
  store dependencies with the UoW.
- [ ] Inject `ProductionAcceptanceEvidenceReader` only for read/write Atlas
  construction.  Route server `atlas_accept` through a UoW drain method that
  projects first, then transitions the matching outbox item; preserve its four
  opaque public inputs exactly.
- [ ] Run GREEN:
  `python -m pytest -q tests/test_runtime_composition.py tests/test_atlas_acceptance_runtime.py tests/test_atlas_acceptance.py tests/test_mcp_contract.py`.
- [ ] Run `python -m ruff check devkit_runtime/uow.py devkit_runtime/project_checkpoint.py orchestrator/store.py server.py tests/test_runtime_composition.py tests/test_atlas_acceptance_runtime.py`.
- [ ] Inspect `git diff --check`, stage only the listed files, and commit with
  a scoped message such as `wire atlas acceptance drain to durable evidence`.

### Task 2: Sound Coverage and Snapshot-Bound Package Descriptors

**Files:**
- Create: `mcp-tools/project_index/packages.py`
- Modify: `mcp-tools/project_index/models.py`
- Modify: `mcp-tools/project_index/store.py`
- Modify: `mcp-tools/project_index/service.py`
- Test: `mcp-tools/tests/test_project_index_core.py`

**Prohibited:** a new MCP tool; package-root registration as a Git worktree;
path/glob selectors; semantic dependency or package-manager workspace
resolution; edits to runtime adapters, server, result projectors, or Atlas
runtime files.  The coordinator owns those shared public-adapter files after
both disjoint worker commits are reviewed.

- [ ] Write RED tests for the existing coverage hole: a snapshot restricted to
  `packages/foo` must be `INDEX_PARTIAL` for required `packages`, including
  when a new sibling is absent from the captured file set.
- [ ] Write RED tests for deterministic discovery of Python, Node, and Cargo
  descriptors; root/nested packages; unsupported/invalid manifest gaps;
  descriptor persistence across reopen; manifest-hash/snapshot-id sensitivity;
  and schema-v4-to-v5 migration.
- [ ] Write RED domain tests for ordered valid package ids limiting
  nodes/edges/windows/receipt and ignoring an unselected package change;
  unordered, duplicate, unknown, and out-of-coverage selector requests fail
  closed.  Public adapter/schema assertions are coordinator-owned in Task 3.
- [ ] Run focused RED and retain exact failure evidence:
  `python -m pytest -q tests/test_project_index_core.py -k "coverage or package"`.
- [ ] Implement pure manifest discovery and a `PackageDescriptor`; fold its
  canonical payload into the snapshot manifest hash.
- [ ] Bump Project Index schema from 4 to 5 and add only the append-only
  `project_index_snapshot_packages` relation plus compatible reader/writer and
  migration checks.  Persist selector ids in the query-receipt schema and
  trace identity.
- [ ] Enforce include-root containment before status compares file hashes.
  Implement package-scoped status/query as a union of descriptor roots and
  verify only those source bytes.  Keep default `IndexStatus` keys unchanged;
  publish descriptors as the intentional additive sync result field.
- [ ] Run GREEN:
  `python -m pytest -q tests/test_project_index_core.py tests/test_project_index_checkpoints.py tests/test_project_index_workflow.py`.
- [ ] Run `python -m ruff check project_index tests/test_project_index_core.py`.
- [ ] Inspect `git diff --check`, stage only the listed files, and commit with
  a scoped message such as `add snapshot-bound project index package scopes`.

### Task 3: Integration, Public Adapter, Independent Review, and Release-Contract Verification

**Files:**
- Begin by cherry-picking the two reviewed task commits into the coordinator
  branch; resolve no unrelated work.
- Modify after both cherry-picks, owned only by the coordinator:
  `mcp-tools/devkit_runtime/project_checkpoint.py`,
  `mcp-tools/devkit_runtime/tool_result.py`, `mcp-tools/server.py`,
  `mcp-tools/tests/test_mcp_contract.py`, and
  `mcp-tools/tests/test_tool_result_contract.py`.
- Optional documentation follow-up only after verification: the design/spec
  and this plan already committed by the coordinator.

- [ ] Perform a fresh read-only spec review of each worker diff against this
  plan.  Return only defects, or `SPEC OK`.
- [ ] Perform a fresh read-only quality review of each accepted diff.  Check
  failure-state transitions, migration safety, public result shape, and test
  adequacy.  Return only defects, or `QUALITY OK`.
- [ ] Cherry-pick only review-approved commits in dependency order; do not
  overwrite unrelated work or resolve scope drift by reset/checkout.
- [ ] Write one adapter-level RED test for the additive sync descriptor result
  and appended `package_ids` status/query parameters, while no-selector status
  retains its exact data keys and the tool inventory remains seventeen.
- [ ] Wire the reviewed domain package API through the shared runtime adapter,
  result projector, and server.  Do not change Atlas's four-field accept
  contract while touching `server.py`.
- [ ] Run the new adapter test GREEN, then targeted public-contract checks.
- [ ] Run integrated focused verification:
  `python -m pytest -q tests/test_runtime_composition.py tests/test_atlas_acceptance_runtime.py tests/test_atlas_acceptance.py tests/test_project_index_core.py tests/test_project_index_checkpoints.py tests/test_project_index_workflow.py tests/test_mcp_contract.py tests/test_tool_result_contract.py`.
- [ ] Run source/lint checks, `git diff --check`, `git status --short`, and
  inspect the final diff/stat.  Report any unrun full suite explicitly.
- [ ] Make one coordinator integration commit only if cherry-pick metadata
  does not already provide the needed auditable commit boundary.  Do not push,
  publish, install a plugin cache, create a pull request, or archive a task.
