# DevKit Continuity Plane CP-A–D Design

## Goal

Make an accepted Atlas projection reproducible from a durable, content-addressed
FrozenView rather than from the current workspace.  The Continuity Plane records
which immutable accepted-task evidence was frozen, protects publication with a
fence and pointer CAS, and can structurally replay that record without adding a
public MCP tool or changing `atlas_accept`.

This design adopts only verified AstrContinuum principles: canonical content
identity, fences, immutable records, pointer CAS, fail-closed admission, and
idempotent replay.  It does not copy AstrContinuum schemas, Capsule semantics,
token accounting, provider workers, or experiment receipt formats.

## Scope and Non-Goals

This is CP-A through CP-D:

- CP-A: typed continuity identity, attempts, receipts, and pointers.
- CP-B: a private `continuity.sqlite3` plus an independent content-addressed
  `continuity-cas/` FrozenView store.
- CP-C: fence-checked publication and recovery inside the existing Atlas
  acceptance path.
- CP-D: deterministic structural replay and verification of a FrozenView.

CP-E is explicitly out of scope: no background runner, scheduler, watcher, or
automatic retry loop is enabled.  A later runner must claim and renew a
Continuity Plane fence; it cannot infer completion from filesystem changes.

The implementation must not:

- add an MCP tool, public result field, or `atlas_accept` parameter;
- add tables to `orchestrator.sqlite3`, `project-index.sqlite3`, or
  `atlas.sqlite3`;
- reuse the checkpoint CAS, Atlas CAS, or installation-keyed receipt repository
  as the Continuity content store;
- fetch a FrozenView from the live workspace during replay; or
- report an Atlas outbox item as projected before the Continuity pointer is
  published.

## Existing Boundary and Acceptance Flow

The current public operation remains:

```text
atlas_accept(workflow_id, code_task_id, acceptance_id, ingestion_key)
```

`ProductionAcceptanceEvidenceReader` already reconstructs a typed
`AcceptedAtlasProjectionRequest` and `AcceptedAtlasProjectionEvidence` from
those opaque identifiers.  Its request contains the immutable
`code_task_version`, output snapshot/checkpoint/query identities, verification
artifact hashes, execution receipt ids, and `evidence_binding_hash`.  The
Continuity Plane must consume this reconstructed evidence, never mutable outbox
payload JSON or caller-supplied source text.

`RuntimeUnitOfWork.accept_atlas()` becomes the only integration seam.  It must
split the existing internal Atlas acceptance path into a private preparation
operation and a private projection operation so it can freeze verified evidence
before Atlas writes occur.

```text
four opaque Atlas ids
        |
        v
rebuild + validate immutable evidence
        |
        v
prepare/reuse FrozenView in Continuity CAS
        |
        v
project Atlas facts
        |
        v
publish Continuity pointer with fence/CAS
        |
        v
mark matching Atlas outbox item PROJECTED
```

The four durable stores are not claimed to share one transaction.  A durable
pre-freeze record may survive a later Atlas failure.  This is safe because it is
content-addressed and idempotent.  If Atlas projection or pointer publication
fails, the outbox remains pending and a retry rebuilds the same evidence;
different evidence for the same immutable identity is a conflict.

## CP-A: Canonical Identity and Typed Records

Create `mcp-tools/devkit_continuity/` with the following records:

- `ContinuityKey`: `workflow_id`, `code_task_id`, `code_task_version`,
  `acceptance_id`, `ingestion_key`, `payload_hash`, and
  `evidence_binding_hash`.
- `FrozenEntry`: a relative path, content SHA-256, byte length, and stable
  logical role.  Paths are normalized relative paths only.
- `FrozenView`: `view_id`, `manifest_hash`, `cas_root_hash`, the
  `ContinuityKey`, and ordered entries.
- `ContinuityAttempt`: a `ContinuityKey`, monotonic `fence_epoch`, state,
  `view_id`, and immutable receipt hash.
- `ContinuityPointer`: the active `view_id` for one
  `(workflow_id, code_task_id, code_task_version)` key with a monotonically
  increasing `pointer_version`.

`view_id`, `manifest_hash`, and receipt hashes use canonical JSON with sorted
keys and stable field order.  The manifest includes a schema tag, the complete
key, ordered entries, output snapshot/checkpoint/query identities, verification
artifact hashes, and execution receipt ids.  Any mismatch between a previously
stored immutable record and the same calculated identity is
`CONTINUITY_CONFLICT`.

## CP-B: Independent SQLite and CAS FrozenView

`RuntimeConfig` gains only private paths:

```text
data_root/continuity.sqlite3
data_root/continuity-cas/
```

`RuntimeBootstrap` initializes the database and directory before normal UoWs.
The Continuity schema has a version metadata row plus separate immutable
`continuity_views`, `continuity_entries`, `continuity_attempts`, and
`continuity_receipts` relations, and a mutable versioned
`continuity_pointers` relation.  SQLite constraints and triggers reject updates
or deletes to immutable rows.  Pointer updates are allowed only through a
compare-and-swap predicate on the expected pointer version and fence epoch.

Frozen content is written under hash-sharded paths in `continuity-cas/`.  A
write validates the bytes against the declared hash, writes a private temporary
file under the Continuity root, atomically publishes only an absent hash, then
re-verifies the stored bytes.  Existing bytes for a hash are verified before
reuse; a mismatch is `CONTINUITY_CONFLICT`.  The Continuity service never
accepts an absolute path, symlink, or unverified body.

Freeze input is the already verified Atlas extraction evidence.  The service
creates a canonical manifest from those typed entries, copies each verified
body into its own CAS, records immutable rows in one Continuity transaction,
and returns the existing equal FrozenView on retry.  It never calls
`ProjectIndexService.read_snapshot_files()` during replay because that API
correctly rechecks the live workspace and is not a content archive.

## CP-C: Fence, Publication, and Recovery

For every acceptance invocation, the Continuity store creates or recovers a
single attempt keyed by the immutable `ContinuityKey`.  It increments a private
`fence_epoch` only when a previous attempt is expired or terminally abandoned.
A current attempt can publish only when its key, epoch, and expected pointer
version all match the stored row.

The UoW sequence is fixed:

1. Rebuild and validate immutable acceptance evidence through the existing
   reader.
2. Claim or reuse the matching Continuity attempt and freeze/revalidate its
   FrozenView.
3. Project Atlas using the exact prepared request/evidence.
4. Publish the Continuity pointer with the attempt's fence epoch and expected
   pointer version; write an immutable publish receipt.
5. Mark only the matching Atlas outbox row `PROJECTED`.

If step 2 fails, Atlas is untouched.  If step 3 or 4 fails, the outbox remains
pending.  A retry must produce the same FrozenView or fail closed.  If step 4
succeeds but the process stops before step 5, the next invocation verifies the
published receipt and repairs only the matching pending outbox transition.

Continuity errors remain private implementation details but map at the Atlas
boundary to the existing fail-closed families: availability/storage failures to
`ATLAS_EVIDENCE_UNAVAILABLE`, and identity/fence/CAS conflicts to
`ATLAS_EVIDENCE_CONFLICT`.  No raw path, content, SQL detail, or internal
attempt id crosses the public envelope.

## CP-D: Structural Replay

`ContinuityService.verify_replay(key)` is private.  It reloads a FrozenView
solely from `continuity.sqlite3` and `continuity-cas/`, verifies each blob hash
and byte length, rebuilds the canonical manifest and receipt hash, and checks
the active pointer's view id/version against the supplied immutable key.  It
does not run source code, contact providers, query the live workspace, or alter
the pointer.

The normal acceptance retry path invokes this verification before reusing a
FrozenView.  CP-D therefore proves the structure and provenance of the frozen
input, not that a historical code execution can be reproduced on a different
machine.

## Error and Concurrency Rules

- Missing/invalid schema, missing CAS bytes, bad hashes, stale fences, pointer
  CAS loss, or immutable-row mismatches fail closed.
- A second concurrent invocation may reuse an equal view but cannot publish
  with an older fence or pointer version.
- An expired attempt is recoverable only after the store records its terminal
  expiry and issues a higher epoch; it is never silently overwritten.
- A pre-frozen orphan is retained as evidence and may be reused only if its
  canonical key and manifest still verify.
- Read-only runtime UoWs do not initialize, mutate, or expose Continuity
  storage.

## Verification Contract

Focused tests must prove:

1. canonical identity is stable for equal evidence and conflicts on a changed
   immutable field;
2. CAS rejects wrong bytes and preserves an existing verified blob;
3. a FrozenView replays without accessing the workspace;
4. only one current fence publishes a pointer; stale/expired fence attempts
   fail closed;
5. pointer CAS loss leaves the outbox pending and retries are idempotent;
6. injected failures before/after Atlas projection never mark the wrong outbox
   row projected;
7. existing 17-tool schemas and `atlas_accept`'s four fields are unchanged;
8. runtime bootstrap creates the Continuity store only for a normal configured
   data root, and read-only UoWs make no Continuity writes.

## Delivery Boundaries

CP-A and CP-B form the durable data foundation.  CP-C integrates the private
acceptance seam only after CP-A/B pass their isolated tests.  CP-D adds replay
verification and crash/retry matrix last.  CP-E remains a separate, explicit
future specification.
