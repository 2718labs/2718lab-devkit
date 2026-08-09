# Continuity Plane CP-A–D Implementation Plan

> **Execution gate:** the CP-A–D design is approved; this document is the
> implementation plan.  Do not begin CP-E, add a public MCP surface, or make a
> background worker as part of this work.

## Outcome and Locked Boundary

Add a private, durable Continuity Plane to the existing Atlas acceptance path.
It freezes the already validated `ExtractionRequest` bodies into its own
content-addressed store, publishes a fenced/versioned pointer only after Atlas
projection succeeds, and structurally verifies a frozen view without reading a
live workspace.

The public boundary stays exactly as it is today:

```text
atlas_accept(workflow_id, code_task_id, acceptance_id, ingestion_key)
```

There must still be exactly 17 MCP tools.  Do not add a public result field,
parameter, route, database table in an existing store, or background runner.
Continuity is private to `devkit_runtime` and maps its failures to the existing
`ATLAS_EVIDENCE_UNAVAILABLE` / `ATLAS_EVIDENCE_CONFLICT` families.

Work only in:

```text
G:\2718lab\_codex\atlas-package-fresh-019fe667
branch: codex/atlas-sync-package-g-019fe667
```

Use this isolated test-artifact root for every command that can write temporary
state:

```powershell
$taskTemp = 'D:\bun\tmp\codex\2718-devkit-continuity-019fe667'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:CODEX_TASK_TEMP = $taskTemp
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:TMPDIR = $taskTemp
$env:PYTHONPYCACHEPREFIX = Join-Path $taskTemp 'pycache'
Set-Location 'G:\2718lab\_codex\atlas-package-fresh-019fe667\mcp-tools'
```

## Route and Task Cards

This is a High-risk, sequential storage/integration migration: it spans a new
SQLite schema/CAS, Atlas's immutable-evidence seam, outbox state transitions,
and slow acceptance tests.  Route each mutable card to one Terra-class writer
at high or xhigh reasoning, with independent read-only review between cards.
The cards share `devkit_runtime/uow.py` and the same durable contract, so they
must be integrated in order rather than written concurrently.  A Spark sprint
is not selected: there is no single reproducible, tightly bounded severe
blocker; using it for schema design or final acceptance would violate the
sprint gate.

| Card | Durable identity | Write scope | Depends on | Stop condition |
| --- | --- | --- | --- | --- |
| CP-A | `CP-A-019fe667` | `devkit_continuity/models.py`, `canonical.py`, `__init__.py`, `devkit_runtime/config.py`, model tests | approved design | canonical types and paths are green |
| CP-B | `CP-B-019fe667` | `devkit_continuity/cas.py`, `store.py`, `service.py`, `devkit_runtime/bootstrap.py`, store tests | CP-A commit | private CAS/SQLite is idempotent and fail-closed |
| CP-C | `CP-C-019fe667` | `devkit_atlas/service.py`, `devkit_runtime/uow.py`, composition tests, integration tests | CP-B commit | pointer precedes only the matching outbox transition |
| CP-D | `CP-D-019fe667` | `devkit_continuity/service.py`, `store.py`, replay tests, acceptance regressions | CP-C commit | replay proves frozen structure with no workspace read |

For every card, capture a focused RED before production edits, run the listed
GREEN command after the smallest implementation, run `git diff --check`, and
commit only the files named by that card.  Preserve unrelated work and never
reset, checkout, or remove prior task roots.

## Card CP-A — Canonical Identity, Records, and Private Paths

### Files

- Create `mcp-tools/devkit_continuity/__init__.py` as the narrow private export
  surface.
- Create `mcp-tools/devkit_continuity/models.py`.
- Create `mcp-tools/devkit_continuity/canonical.py`.
- Modify `mcp-tools/devkit_runtime/config.py`.
- Create `mcp-tools/tests/test_continuity_models.py`.

### RED

Write focused tests first.  They must construct two equivalent accepted-task
inputs and prove that their canonical key, manifest hash, and receipt hash are
byte-for-byte equal; then change each immutable identity component in turn and
assert a different identity or `CONTINUITY_CONFLICT`.  Cover invalid values:
absolute paths, `..`, empty/duplicate normalized paths, malformed
`sha256:<64 hex>` ids, non-monotonic versions, duplicate `(role, path)` entries,
and unordered entries.  The same relative path is valid in distinct
`before_file` and `after_file` roles.

Run before implementation:

```powershell
python -m pytest -q tests/test_continuity_models.py
```

Expected RED: imports and canonical constructors do not exist yet.

### GREEN implementation

1. In `models.py`, add a private stable `ContinuityError(code)` and frozen,
   slot-based records:

   ```python
   ContinuityKey(
       workflow_id, code_task_id, code_task_version, acceptance_id,
       ingestion_key, payload_hash, evidence_binding_hash,
   )
   FrozenEntry(role, path, content_hash, byte_length)
   FrozenView(view_id, manifest_hash, cas_root_hash, key, entries)
   ContinuityAttempt(key, fence_epoch, state, view_id, receipt_hash)
   ContinuityPointer(workflow_id, code_task_id, code_task_version,
                     view_id, pointer_version, fence_epoch)
   ContinuityReceipt(key, view_id, receipt_hash, kind)
   ```

   Validate all identifiers and require that `code_task_version >= 0`,
   `fence_epoch > 0`, `pointer_version >= 1`, entries are sorted by
   `(role, path, content_hash)`, and every stored path is a normalized relative
   path.  Keep bodies out of these model records.

2. In `canonical.py`, implement one Continuity-owned canonical JSON encoder
   (UTF-8, sorted keys, compact separators) and a `sha256:`-prefixed hash
   helper.  Build explicit payload functions for the key, ordered manifest, CAS
   root, and receipt.  The manifest schema tag must be fixed (for example,
   `continuity-frozen-view/v1`) and include the full `ContinuityKey`, entries,
   input/output snapshot ids, checkpoint/query ids, verification artifact
   hashes, and execution receipt ids.  Do not import or mutate an Atlas or
   Project Index database to calculate it.

3. In `config.py`, add read-only properties only:

   ```python
   @property
   def continuity_database(self) -> Path:
       return self.data_root / 'continuity.sqlite3'

   @property
   def continuity_cas_root(self) -> Path:
       return self.data_root / 'continuity-cas'
   ```

   `RuntimeConfig.load()` remains pure: merely resolving either property must
   not create a file or directory.

4. Export only the types/services required by private runtime imports from
   `devkit_continuity/__init__.py`; do not add them to `server.py`, tool-result
   projection, or a public package list.

### Verification and commit

```powershell
python -m pytest -q tests/test_continuity_models.py
python -m ruff check --ignore UP042 devkit_continuity devkit_runtime/config.py tests/test_continuity_models.py
git diff --check
git add devkit_continuity/__init__.py devkit_continuity/models.py devkit_continuity/canonical.py devkit_runtime/config.py tests/test_continuity_models.py
git commit -m "feat(continuity): add canonical frozen view identity"
```

## Card CP-B — Independent SQLite/CAS FrozenView Foundation

### Files

- Create `mcp-tools/devkit_continuity/cas.py`.
- Create `mcp-tools/devkit_continuity/store.py`.
- Create `mcp-tools/devkit_continuity/service.py`.
- Modify `mcp-tools/devkit_runtime/bootstrap.py`.
- Create `mcp-tools/tests/test_continuity_store.py`.
- Extend `mcp-tools/tests/test_runtime_composition.py` only for bootstrap and
  read-only non-creation coverage.

### RED

Add tests that use synthetic typed `AcceptedAtlasProjectionRequest`,
`AcceptedAtlasProjectionEvidence`, `CheckpointFile`, and `SnapshotFile`
instances; the test fixture must give them verified `body`, `content_hash`, and
logical input/output roles without registering a real workspace.  The tests
must show that:

- `RuntimeBootstrap.run()` creates `continuity.sqlite3` and
  `continuity-cas/`, while `RuntimeConfig` and a read-only UoW create neither;
- wrong bytes for a declared hash are rejected and an already verified CAS blob
  remains untouched;
- equal freezes reuse the same immutable `FrozenView`, while a different
  manifest for the same `ContinuityKey` conflicts;
- direct `UPDATE`/`DELETE` attempts against views, entries, attempts, and
  receipts are rejected by SQLite triggers; and
- a pointer update succeeds only with the expected pointer version and matching
  attempt fence.

Run before production code:

```powershell
python -m pytest -q tests/test_continuity_store.py tests/test_runtime_composition.py -k "continuity or bootstrap"
```

Expected RED: Continuity bootstrap/service/store APIs do not exist.

### GREEN implementation

1. In `cas.py`, isolate all Continuity-owned CAS operations: hash-sharded path
   construction, reparse/symlink-safe directory traversal, bytes/length/hash
   verification, private same-root staging, no-replace publication, and
   identity-checked stage cleanup.  Do not reuse Atlas's `os.replace()` CAS
   publisher or the checkpoint CAS writer because neither owns the required
   no-replace contract.

2. In `store.py`, implement `ContinuityStore` with three distinct lifecycle
   operations:

   ```python
   ContinuityStore.bootstrap(database, cas_root, scratch_root)
   ContinuityStore.open_readonly(database, cas_root, scratch_root)
   ContinuityStore.open_readwrite(database, cas_root, scratch_root)
   ```

   Only `bootstrap()` may create/migrate state.  The read-write opener must
   first verify an existing schema in `mode=rw`; it may not call a constructor
   that implicitly creates tables.  The read-only opener must use a verified
   SQLite snapshot / read-only connection and must not create CAS directories.

3. Create the independent schema with `continuity_metadata`,
   `continuity_views`, `continuity_entries`, `continuity_attempts`,
   `continuity_receipts`, and `continuity_pointers`.  Store canonical manifests
   and receipt payloads as canonical JSON plus their hashes.  Add foreign keys,
   unique keys on immutable identities, and `BEFORE UPDATE OR DELETE` abort
   triggers on the four immutable relations.  Make `continuity_attempts` an
   append-only state-event relation keyed by the immutable key hash,
   `fence_epoch`, and a monotonic `sequence`; derive its current state from the
   last event instead of updating a row.  `continuity_pointers` is the only
   mutable relation; update it through an SQL CAS predicate that checks both
   the expected `pointer_version` and the exact `fence_epoch`.  Validate the
   prepared schema's metadata, tables, keys, foreign keys, and triggers on
   every normal open, not merely its schema-version row.

4. Have `store.py` call the isolated CAS module to write blobs below a
   hash-sharded `continuity-cas/` path.  For every blob:
   verify its declared SHA-256 and byte length, write an exclusive temporary
   file below the Continuity root, fsync, atomically publish only an absent
   target, then reread and verify the published target.  If an existing blob
   has different bytes or an unexpected length/hash, raise
   `CONTINUITY_CONFLICT`; never overwrite it.  Reject absolute paths,
   symlinks/reparse points, and content supplied only by a host pathname.

5. In `service.py`, add the private service API used by the runtime:

   ```python
   claim_or_reuse(key) -> ContinuityAttempt
   freeze(attempt, request, evidence) -> FrozenView
   publish(attempt, frozen_view) -> ContinuityPointer
   ```

   `freeze()` derives entries solely from the validated
   `evidence.extraction_request.before_files` and `.after_files`, carries
   `execution_receipt_ids` as manifest identity, and persists immutable rows in
   one Continuity transaction after CAS blobs are verified.  The canonical
   manifest must also retain the non-body `changed_nodes`, `coverage_gaps`, and
   bound execution-receipt fields needed to reconstruct the same typed
   extraction request later.  It is safe for an
   equal pre-freeze orphan to remain and be reused; a non-equal one conflicts.
   Do not call `ProjectIndexService.read_snapshot_files()` here or anywhere in
   Continuity.

6. In `bootstrap.py`, import the Continuity store lazily inside
   `_bootstrap_stores`, bootstrap it alongside the other independent stores,
   and close it in the existing `finally` cleanup.  Do not add a schema to
   `orchestrator.sqlite3`, `project-index.sqlite3`, or `atlas.sqlite3`.

### Verification and commit

```powershell
python -m pytest -q tests/test_continuity_models.py tests/test_continuity_store.py tests/test_runtime_composition.py -k "continuity or bootstrap or runtime_config"
python -m ruff check --ignore UP042 devkit_continuity devkit_runtime/bootstrap.py devkit_runtime/config.py tests/test_continuity_models.py tests/test_continuity_store.py tests/test_runtime_composition.py
git diff --check
git add devkit_continuity/cas.py devkit_continuity/store.py devkit_continuity/service.py devkit_runtime/bootstrap.py tests/test_continuity_store.py tests/test_runtime_composition.py
git commit -m "feat(continuity): persist frozen views in private CAS"
```

## Card CP-C — Atlas Preparation, Fenced Publish, and Outbox Order

### Files

- Modify `mcp-tools/devkit_atlas/service.py`.
- Modify `mcp-tools/devkit_runtime/uow.py`.
- Extend `mcp-tools/tests/test_atlas_acceptance_runtime.py`.
- Create `mcp-tools/tests/test_continuity_integration.py`.
- Extend `mcp-tools/tests/test_runtime_composition.py` for the new private UoW
  factory and lifetime.
- Extend `mcp-tools/tests/test_atlas_projection.py` to lock a single evidence
  read for the private prepared projection seam.
- Extend `mcp-tools/tests/test_tool_result_contract.py` to prove a Continuity
  internal exception maps to an existing Atlas envelope without leaking a path,
  attempt id, CAS hash, or SQLite detail.
- Keep `mcp-tools/tests/test_mcp_contract.py` unchanged unless a regression
  exposes an accidental public contract drift; it is a required verifier, not
  an implementation target.

### RED

Use the existing `DefaultRuntimeAcceptanceEvidenceTests` fixture to prove the
required sequence and failure matrix before code changes:

1. Inject a failure after Continuity freeze but before Atlas projection:
   assert no Atlas ingestion receipt and a pending matching outbox, while the
   frozen view is retained.
2. Inject a pointer CAS/fence failure after Atlas projection:
   assert the outbox remains pending; retrying the exact evidence does not
   create a second frozen view and can complete the matching transition.
3. Inject a Continuity availability failure and a non-recoverable
   identity/manifest conflict:
   assert the public exception is respectively
   `ATLAS_EVIDENCE_UNAVAILABLE` and `ATLAS_EVIDENCE_CONFLICT`, with existing
   retry/quarantine behavior applied only to the matching outbox row.
4. Inject a stale pointer CAS loss separately: assert that it does not overwrite
   a newer pointer or mark the outbox projected; if the newer pointer names the
   same view, the retry repairs only the matching pending outbox.  If it names a
   different view, it fails closed as an identity conflict.
5. Complete the same acceptance twice and assert the same view/pointer is
   reused, the existing Atlas receipt is reused, and no unrelated pending
   outbox row changes.
6. Open a read-only UoW and assert it does not open, create, or expose a
   Continuity writer.

Run before implementation:

```powershell
python -m pytest -q tests/test_continuity_integration.py tests/test_atlas_acceptance_runtime.py -k "continuity or default_write_uow"
```

Expected RED: the private preparation/frozen-publish seam does not exist.

### GREEN implementation

1. In `devkit_atlas/service.py`, introduce a module-private frozen prepared
   object, for example:

   ```python
   @dataclass(frozen=True, slots=True)
   class _PreparedAcceptedProjection:
       request: AcceptedAtlasProjectionRequest
       evidence: AcceptedAtlasProjectionEvidence
       extraction: ExtractionRequest
   ```

   Extract the current `accept()` sequence into three private operations:
   `_prepare_accepted_projection(four_ids)`,
   `_prepare_projection_from_request(request, evidence)`, and
   `_project_prepared_acceptance(prepared)`.  The first rebuilds the four opaque
   identifiers once and calls `reader.read(request)` once.  The second validates
   typed evidence through the existing `_validate_reader_evidence`.  The last
   projects the exact prepared extraction without rereading live index state.
   Keep the public signatures of `AtlasService.accept()` and
   `AtlasService.project_acceptance()` unchanged; their compatibility path may
   retain the existing revalidation behavior, while only the UoW uses the
   private prepared projection seam.

2. In `uow.py`, extend `RuntimeAdapterFactories` with one private
   `open_continuity(config, read_only)` factory and add a lazy, owned continuity
   service only for `read_only=False`.  The default factory must open the
   bootstrapped Continuity schema in read-write mode after verifying it; never
   migrate it in an ordinary UoW.  Update test factories in
   `test_runtime_composition.py` to supply this new callable explicitly.

3. In CP-C, keep evidence reconstruction on the current live acceptance path
   and insert the frozen publication gate with this strict sequence:

   ```text
   prepare + validate immutable Atlas evidence
   -> claim/reuse Continuity attempt and freeze/verify FrozenView
   -> project the prepared Atlas extraction
   -> publish Continuity pointer using attempt fence + expected pointer version
   -> mark the exact matching Atlas outbox PROJECTED
   ```

   At this card, an equal repeat may still use the existing reader to establish
   its key and then reuse the equal frozen view; tests must cover that it cannot
   create a different view or pointer.  Do not implement a replay-only retry
   branch here: CP-D adds it after immutable reconstruction has test coverage.
   Do not call `_mark_atlas_acceptance_projected()` until `publish()` returns a
   pointer bound to the same key/view.

4. Translate only non-recoverable Continuity errors at this private seam:
   storage/schema/CAS absence becomes `AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")`;
   malformed immutable state and a different-key/different-view publication
   become `AtlasError("ATLAS_EVIDENCE_CONFLICT")`.  Treat a same-view pointer
   race or stale attempt as private recoverable contention: reread the pointer
   first; if it is the same view, repair the matching outbox without invoking
   `_record_atlas_acceptance_failure()`.  This prevents the current generic
   conflict handler from incorrectly quarantining a retryable pointer CAS loss.
   Only after this classifier decides the condition is
   non-recoverable may `_record_atlas_acceptance_failure()` retain its existing
   retry/quarantine behavior.  Never expose internal path, SQL, attempt, or
   content details.

5. The remaining failure mapping must be exact:

   ```text
   unavailable storage/schema/CAS -> ATLAS_EVIDENCE_UNAVAILABLE
   canonical identity/manifest mismatch -> ATLAS_EVIDENCE_CONFLICT
   different-view pointer conflict -> ATLAS_EVIDENCE_CONFLICT
   same-view pointer race -> verify and continue; no public error if repair succeeds
   ```

   Do not change public parameters/results or the server's tool table.

### Verification and commit

```powershell
python -m pytest -q tests/test_continuity_models.py tests/test_continuity_store.py tests/test_continuity_integration.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_mcp_contract.py tests/test_tool_result_contract.py
python -m ruff check --ignore UP042 devkit_continuity devkit_atlas/service.py devkit_runtime/uow.py devkit_runtime/bootstrap.py devkit_runtime/config.py tests/test_continuity_integration.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_tool_result_contract.py
git diff --check
git add devkit_atlas/service.py devkit_runtime/uow.py tests/test_continuity_integration.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_tool_result_contract.py
git commit -m "feat(continuity): fence atlas acceptance publication"
```

## Card CP-D — Structural Replay and Recovery Checks

### Files

- Modify `mcp-tools/devkit_continuity/service.py`.
- Modify `mcp-tools/devkit_continuity/store.py` only for read-side verification
  queries needed by the service.
- Modify `mcp-tools/devkit_continuity/cas.py` only for reparse-safe read and
  identity verification helpers.
- Create `mcp-tools/tests/test_continuity_replay.py`.
- Extend `mcp-tools/tests/test_continuity_integration.py` and
  `mcp-tools/tests/test_atlas_acceptance_runtime.py` for retry/recovery cases.

### RED

Add tests that call a private `verify_replay(key)` after a successful freeze and
then prove all of the following:

- monkeypatch `ProjectIndexService.read_snapshot_files`,
  `ProductionAcceptanceEvidenceReader`, and workspace access to fail; replay
  still succeeds because it only reads `continuity.sqlite3` and
  `continuity-cas/`;
- remove or alter one CAS blob, change its byte length, corrupt the canonical
  manifest or immutable receipt hash, or point at a different view; replay
  fails closed;
- a stale attempt/fence cannot publish over a newer pointer; and
- replay causes no SQLite changes, CAS writes, pointer changes, Atlas writes,
  provider calls, or code execution.

Add recovery coverage for the post-pointer/pre-outbox window: a retry first
verifies the equal frozen view/receipt, then moves only the exact pending
outbox row to `PROJECTED`.

Run before implementation:

```powershell
python -m pytest -q tests/test_continuity_replay.py tests/test_continuity_integration.py -k "replay or recovery or stale"
```

Expected RED: replay verification and recovery validation are unavailable.

### GREEN implementation

1. Add `ContinuityService.verify_replay(key) -> FrozenView`.  It must reload
   immutable rows by the complete `ContinuityKey`, re-read every CAS blob,
   check each content hash and byte length, recreate the canonical ordered
   manifest and receipt hashes, and verify that the active pointer's
   `(view_id, pointer_version, fence_epoch)` is compatible with the supplied
   immutable key.  It must make no writes.

2. Add a private `materialize_replay(key)` beside verification.  It rebuilds the
   typed request/evidence/extraction data required by
   `_prepare_projection_from_request()` exclusively from the verified manifest
   and CAS bodies.  It must preserve the stored `before_file`/`after_file`
   roles, changed-node/gap metadata, and bound receipt fields; it must not
   accept a workspace path or call a reader/index service.

3. Add only the read-side Store queries needed by `verify_replay()` and
   `materialize_replay()`; do not relax immutable triggers or use the live
   Project Index snapshot APIs.  Missing schema/blobs and I/O failures are
   availability errors; canonical, pointer, receipt, or fence mismatches are
   conflicts.

4. Replace CP-C's reader-first repeat path with two explicit recovery branches
   before any live evidence reader is opened: find a FrozenView by the four
   public identifiers; if found, invoke `verify_replay()` and
   `materialize_replay()`, then reuse the Atlas receipt or run the prepared
   projection.  If its pointer is already published, repair only the matching
   pending outbox.  If its pointer is not yet published, publish it with the
   current fence/CAS rule, then mark the matching outbox.  Only when no frozen
   view exists may the normal first-freeze path call the evidence reader.
   Preserve the order: verification/replay -> Atlas projection or receipt reuse
   -> pointer validation/publication -> matching outbox mark.

5. Keep replay private.  Do not add a `continuity_*` MCP tool, background
   runner, task poller, watcher, or auto-retry loop.

### Verification and commit

```powershell
python -m pytest -q tests/test_continuity_models.py tests/test_continuity_store.py tests/test_continuity_integration.py tests/test_continuity_replay.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_mcp_contract.py tests/test_tool_result_contract.py
python -m ruff check --ignore UP042 devkit_continuity devkit_atlas/service.py devkit_runtime/uow.py devkit_runtime/bootstrap.py devkit_runtime/config.py tests/test_continuity_models.py tests/test_continuity_store.py tests/test_continuity_integration.py tests/test_continuity_replay.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_tool_result_contract.py
git diff --check
git add devkit_continuity/cas.py devkit_continuity/store.py devkit_continuity/service.py tests/test_continuity_replay.py tests/test_continuity_integration.py tests/test_atlas_acceptance_runtime.py
git commit -m "feat(continuity): verify frozen replay structure"
```

## Final Acceptance Matrix

Run the following from `mcp-tools` after CP-D, retaining the exact outputs in
the delivery evidence.  Do not call a result green if any command times out,
is interrupted, or is skipped for an unavailable dependency.

```powershell
python -m pytest -q tests/test_continuity_models.py tests/test_continuity_store.py tests/test_continuity_integration.py tests/test_continuity_replay.py
python -m pytest -q tests/test_mcp_contract.py tests/test_mcp_stdio.py tests/test_tool_result_contract.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py
python -m pytest -q tests/test_atlas_acceptance.py
python -m ruff check --ignore UP042 devkit_continuity devkit_atlas/service.py devkit_runtime/uow.py devkit_runtime/bootstrap.py devkit_runtime/config.py tests/test_continuity_models.py tests/test_continuity_store.py tests/test_continuity_integration.py tests/test_continuity_replay.py tests/test_atlas_projection.py tests/test_atlas_acceptance_runtime.py tests/test_runtime_composition.py tests/test_tool_result_contract.py
git diff --check
git status --short
```

The existing `UP042` finding for `IndexState(str, Enum)` predates this work;
the scoped Ruff command intentionally ignores only that known baseline.  No
commit is complete until the focused new tests, public contract lock, slow Atlas
acceptance suite, scoped lint, and diff check are current green.

## Handoff Evidence Required Per Card

Each completed card must return: commit SHA, changed paths, RED command/output,
GREEN command/output, lint/diff results, any remaining failure injection or
recovery concern, and confirmation that the public 17-tool contract did not
change.  The coordinator independently re-runs the next card's prerequisites
before accepting its dependency.
