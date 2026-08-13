# Atomic Atlas Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an Atlas acceptance durably final only when its Continuity published state and its exact matching Atlas outbox row become durable in one crash-atomic SQLite transaction.

**Architecture:** Atlas ingestion remains an idempotent precondition in its existing database. The orchestrator database is migrated from WAL to rollback-journal DELETE mode, receives an immutable finalization certificate, and becomes the main database of a short-lived finalization connection. That connection attaches the Continuity database, validates both prepared schemas and physical journal invariants under a write transaction, writes the Continuity published receipt/pointer/attempt and the exact outbox PROJECTED transition plus certificate, then commits once. All recovery paths use the same finalizer; no UoW path may separately call Continuity publish and outbox mark.

**Tech Stack:** Python 3, SQLite, existing DevKit Atlas/Continuity/Runtime stores, pytest, Ruff.

---

## Contract

The durable acceptance states are:

| State | Atlas receipt | Continuity | matching outbox |
|---|---|---|---|
| prepared | absent or exact idempotent receipt | absent/claimed/frozen | PENDING |
| final | exact idempotent receipt | PUBLISHED with exact key/view/fence | PROJECTED with immutable certificate |

The following pairs are forbidden after an attempted finalization returns or rolls back:

- Continuity PUBLISHED with matching outbox PENDING.
- Continuity absent/claimed/frozen with matching outbox PROJECTED.
- Outbox PROJECTED without a matching immutable finalization certificate.

The certificate binds: ingestion key, acceptance ID, payload hash, Continuity key hash, view ID, fence epoch, pointer version, published Continuity receipt hash, exact Atlas receipt digest, and a canonical finalization hash.

Physical invariant: both attached database files must be on the same local volume, use rollback-journal DELETE mode, and have no `-wal`/`-shm` sidecars. Finalization uses `BEGIN IMMEDIATE` plus `synchronous=FULL`; failed proof, migration, or commit preparation rolls back both durable state changes.

## Work items

- [ ] **A — Orchestrator DELETE migration and immutable certificate schema**

  **Write scope:** `mcp-tools/devkit_orchestrator/models.py`, `mcp-tools/devkit_orchestrator/store.py`, `mcp-tools/tests/test_orchestrator_store.py`, `mcp-tools/tests/test_orchestrator_typed_tasks.py`.

  1. Add schema v13 migration from the strict v12 shape. Quiesce WAL with an exact checkpoint before switching to DELETE; reject an uncheckpointable WAL state.
  2. Add `atlas_finalizations` with immutable insert-only rows and exact certificate validation. Expose typed read/validation helpers but no public MCP surface.
  3. Replace the prepared-store WAL requirement with the DELETE/no-sidecar invariant. Preserve all v12 rows and strict schema verification.
  4. Add real RED tests for WAL migration/rejection, certificate immutability, malformed certificate rejection, and v12 data preservation. Make them GREEN with the minimum migration.
  5. Verify with focused pytest, Ruff, and `git diff --check`. Commit only this scope.

- [ ] **B — Continuity attached publication primitive**

  **Write scope:** `mcp-tools/devkit_continuity/store.py`, `mcp-tools/devkit_continuity/service.py`, `mcp-tools/tests/test_continuity_store.py`, `mcp-tools/tests/test_continuity_replay.py`.

  1. Add private attached-connection helpers that, under a caller-owned `BEGIN IMMEDIATE`, strictly prove the exact current claimed/frozen or published Continuity state and write/reuse the exact published receipt, pointer, and attempt through qualified attached-table SQL.
  2. Preserve current single-store APIs, but make the new primitive require an exact key/view/fence proof and reject stale/mismatched/physical-invalid state.
  3. Add a real RED proving a manual attached transaction can atomically publish only the exact frozen view and that an exception leaves no pointer/receipt/attempt prefix.
  4. Add fail-closed tests for WAL/sidecar, receipt/view/fence mismatch, and idempotent same-view recovery. Make them GREEN, then commit only this scope.

- [ ] **C — Runtime atomic finalizer, recovery, and legacy repair**

  **Write scope:** `mcp-tools/devkit_runtime/atlas_finalization.py` (new), `mcp-tools/devkit_runtime/uow.py`, `mcp-tools/devkit_runtime/bootstrap.py`, `mcp-tools/tests/test_atlas_finalization.py` (new), `mcp-tools/tests/test_continuity_integration.py`, `mcp-tools/tests/test_atlas_acceptance_runtime.py`, `mcp-tools/tests/test_runtime_composition.py`.

  1. Build a private `AtomicAtlasFinalizer` that opens the orchestrator DB as main, attaches Continuity, validates both prepared schemas and physical invariants in the same transaction, calls the two attached primitives, and commits once.
  2. Make every Atlas UoW success/recovery path use this finalizer after the existing idempotent Atlas projection. Remove the separate Continuity-publish/outbox-mark sequence.
  3. Add bounded legacy repair at bootstrap: a pre-existing exact published/pending or projected/uncertified pair is verified against its Atlas receipt and either atomically certified/finalized or rejected before serving writes. Never silently bless malformed/mixed pairs.
  4. Establish genuine REDs for the former split-pair crash window, a hard child-process exit after both writes but before commit, concurrent same-view recovery, certificate absence, and an unrelated outbox. Make them GREEN.
  5. Keep the 17-tool MCP table and `atlas_accept` four-field public signature unchanged. Commit only this scope.

- [ ] **D — Independent acceptance**

  **Write scope:** none (read-only).

  1. Fresh specification review: state machine, migration, exact binding, error classification, and no public-surface expansion.
  2. Fresh quality review in a clean archive/worktree: targeted RED replay, full slow controlled gate with stdout/stderr/exit, Ruff, and diff checks.
  3. Coordinator inspects the final graph, worktree cleanliness, test evidence, and any remaining cross-database limitation before declaring completion.

## Required verification commands

Run from `G:\2718lab\_codex\atlas-package-fresh-019fe667\mcp-tools` with all temporary state under `D:\bun\tmp\codex\2718-devkit-continuity-019fe667`:

```powershell
python -m pytest -q -p no:cacheprovider tests/test_orchestrator_store.py tests/test_continuity_store.py tests/test_atlas_finalization.py tests/test_continuity_integration.py
python -m pytest -q -p no:cacheprovider tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_atlas_projection.py tests/test_tool_result_contract.py tests/test_mcp_contract.py
python -m ruff check --ignore UP042 devkit_orchestrator devkit_continuity devkit_runtime tests
git diff --check
```

The slow combined gate must run through a controlled task-local runner that preserves stdout, stderr, elapsed time, and exit code. A host timeout alone is not test evidence.
