# Storage Lease Ledger and Owned Cleanup 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the protected storage broker one durable family/member/control accounting authority, keep the Host as its authenticated client, then add explicitly authorized, bounded generated-cache cleanup.

**Architecture:** Plan 1 supplies strict intents, verified group/member authority, and a single Host service per runtime. Plan 2 extends the root-shared transactional ledger owned by `codex-storage-broker-windows`: each protected-root transaction runs inside the broker under its OS fence, reloads current state, rechecks epoch/owners, transitions and atomically persists. The Host is only an authenticated, path-free client and has no local writer fallback. Admission is one transition, not an admission followed by another reservation. Independently owned control allocations keep bootstrap/queue metadata bounded across waves without holding Cargo family leases. Cleanup is a later handle-fenced broker transaction over proven disposable objects, not a consequence of release, expiry, size, or age.

**Tech Stack:** Existing Rust `serde`/`serde_json`/`sha2`, platform filesystem/process handles, existing durable replacement helpers, and the authenticated bridge; Python is a path-free projection only.

**Revision status (2026-08-30):** This replaces the unpublished flat lease-v1 design with internal snapshot-v2 aligned to Host `937ae14` kernel/service symbols and Plan 1's Task 6 map. It does not claim implementation, migration, cleanup, or activation is complete. Preserve admission-v1 exact5, existing intent/target/profile contracts, SHA-256 admission/family IDs, and the strict `fast_lane_storage` root-plus-eight policy shape. No new artifact kind, public admission field, or user control-budget field is introduced.

The main thread records Host `841fdaf` three-crate compile exit 0 and DevKit
`b2aff` three selected final tests exit 0 for the existing slices. Retain that
scope of evidence; it does not prove the new Plan 2 ledger/cleanup exists.
Production cleanup and the 1.1.3 release remain incomplete.

**Protected-broker handoff (2026-08-31):** Storage broker Tasks 1-6 are a
prerequisite, not work completed by this plan revision. This edit claims no
Task 7 probe result, elevated provisioning/acceptance, or Bazel completion.

**Compile first:** Reuse only
`G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target`,
with `CARGO_INCREMENTAL=0` and `--locked -j1`. Retain prior compile evidence;
compile changed crates before at most the two core boundary probes below.
Do not create a ledger-specific Cargo target or repeatedly run full suites.
Commands here are implementation instructions, not commands to run during
this documentation-only revision. No live cleanup/configuration is authorized.

---

## Dependency order and bounded file map

Use this exact implementation dependency order:

```text
storage broker Tasks 1-6
  -> Plan 2 P2-base Task 1 control transactions
  -> Plan 1 Task 6 durable refill/lifecycle wiring
  -> Plan 2 remaining P2-base owner recovery
  -> Plan 2 P2-apply preview and generated cleanup
```

Broker completion unblocks only Plan 2 P2-base Task 1; it does not authorize
cleanup or live activation. Plan 1 does not depend on **P2-apply** (Tasks 4-5),
while P2-apply depends on Plan 1's real process/descendant terminal fence and
a correct family postcheck plus the remaining P2-base owner recovery. Never
unblock a dependency with free metadata writes or fake proof. The root and
eight policy values are still awaiting the user's confirmation; this plan
requires no additional control-budget choice and fills in no values on the
user's behalf.

All Host-repository paths below are relative to
`G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2`.
DevKit paths are relative to
`G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery`.
Re-read current symbols before editing; unrelated dirty work stays untouched.
Protected-root writer modules resolve within
`codex-rs/storage-broker-windows/src/`; Host client modules resolve within
`codex-rs/core/src/fast_lane_host_dispatch/`. Do not recreate a writer in core.

| Slice | Files and ownership |
| --- | --- |
| Base state/transactions | Extend broker-owned `codex-rs/storage-broker-windows/src/{ledger,ledger_codec,service}.rs` for typed snapshots, bounded commit/recovery and control ownership. The broker transaction mutex and OS fence/reload remain authoritative; no second compilable writer may remain in `codex-core`. |
| Real OS boundary | Extend broker-owned `codex-rs/storage-broker-windows/src/root_fs.rs` for root/process exclusion, owned directory/file handles, bounded durable replacement and later deletion primitives; no permissive path-string fallback. |
| Existing kernel integration | Modify core `storage_firewall.rs` to evaluate transitions through the broker-backed operation boundary instead of its own independent `Mutex<State>`; keep `storage_service.rs::HostStorageService` plus `storage_broker.rs` as one authenticated client entry point per runtime, with no local ledger/root handles or fallback. |
| Runtime/base initialization | Modify `codex-rs/core/src/thread_manager.rs` and existing service/session construction only as required to initialize the shared base-Config client service once. Registries do not construct a broker client or writer per session. |
| Authority/queue lifecycle | Modify `fast_lane_host_dispatch/registry.rs` at initial admission, `consume_batch`, `register_refill_queue`, `consume_refill_queue` and queue persistence; modify `codex_adapter.rs`/`coordinator.rs` only at lifecycle settlement seams. Keep original route/lease hashes unchanged. |
| Later cleanup | Add the candidate classification/manifest and apply state machine under `codex-rs/storage-broker-windows/src/`, reusing broker `root_fs.rs` and Plan 1 terminal/postcheck evidence; core exposes only the authenticated path-free client operation. |
| Later wire projection | Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs` and its `envelope.rs`/`session.rs`/`pump.rs` only for status/preview/apply; keep the single authenticated writer/receiver arrangement. |
| Later DevKit projection | Create `mcp-tools/devkit_runtime/storage_ledger.py`; modify existing `host_bridge.py`, `host_session.py`, `mcp-tools/server.py` and `devkit_runtime/tool_metadata.py` for typed read-only status/preview and explicitly destructive apply. |
| Bounded verification | Put protected-root transaction/apply cases in `codex-storage-broker-windows`; reuse core firewall/client tests only for Host policy and fail-closed client behavior. Add only the necessary exact Python request/tool-annotation assertion in `mcp-tools/tests/test_storage_ledger.py` / `test_mcp_contract.py`. |

No source/session deletion, GitHub reachability, CAS, compression, remote sync,
new dependency, or live configuration change belongs to this revision.
Unknown existing directories are protected, not automatically imported.

## Internal snapshot-v2: family, group, member, control

The existing kernel has `Family { lease, observed }`,
`FamilyLease { owner, lease_id, reserved, allowance }`,
`Group { authority_subject, sealed, members }` and
`MemberPhase::{Reserved, Consumed, Released}`. Preserve those semantics;
do not flatten them into one task per family lease or invent an `Active`
phase that changes the consume-before-prepare boundary.

The following are **planned private persistence records**, not wire authority
types. Decode with exact fields, bounded lengths/counts, checked arithmetic
and duplicate-key rejection. `Usage` contains `bytes: u64, files: u64`.

| Record / unique key | Required contents |
| --- | --- |
| `LedgerSnapshotV2` / one approved root | `schema = "2718lab.storage.ledger-snapshot.v2"`, root-wide `ledger_epoch`, root identity, policy hash, owner/family/group/member/control collections and one bounded optional pending operation. Restart generations belong to individual owners, not a global takeover generation. |
| `FamilySnapshot` / `target_key` | Canonical descriptor/root identity, retained `observed: Usage` and optional `FamilyLeaseSnapshot`. Shared Cargo data is observed once per family, not once per member. Every existing artifact kind retains its common `cargo-target` child. |
| `FamilyLeaseSnapshot` / active `target_family_lease_id` | One `owner_id`, `reserved: Usage` and `allowance: Usage`. One family has at most one active lease/owner; many same-owner members may reference it. IDs remain strict lowercase SHA-256 digests. |
| `GroupSnapshot` / `owner_id` | Original Host batch/selected-wave authority subject and provenance binding, exact Host-owner reference, optional exact sealed task set, authority deadline/epoch, and recovery disposition. It is not keyed by each task's distinct Sent profile hash. |
| `MemberSnapshot` / (`owner_id`, `task_id`) | Original intent, exact decision/receipt identity including admission/family IDs, `StorageAssignmentBinding`, derived private path identities, `MemberPhase`, and terminal/postcheck evidence references. Released records retain replay tombstones. |
| `ControlSnapshot` / `control_id` | Internal purpose (`Bootstrap` or `RefillQueue`, not an artifact kind), verified queue/Host provenance, exact Host-owner reference and independent lifecycle owner, safe control-path identity, bounded payload identity, committed observation, reserved physical footprint/growth and state. No Cargo family lease or fabricated task ID. |
| `OwnerBinding` / (`host_instance_id`, `owner_epoch`) | Host instance identity, PID plus creation identity, this owner's restart generation and recovery disposition. Mutations require the live root transaction guard and matching owner evidence; serialized fields alone never substitute for process handles. |
| `PendingOperation` / at most one root transaction | Operation/expected epoch, exact affected identities, before/after state hashes, reserved metadata footprint and stage/cleanup progress. No append-only unbounded journal. |

`RecoveryDisposition::{Current, RecoveryPending, Quarantined}` is orthogonal
to member phase. A persisted `Released` tombstone does not become executable
again after restart. Duplicate member keys and inconsistent family references
are invalid; repeated references to the **same** family lease from distinct
members are expected. A different owner cannot reference that active lease.
Released tombstones retain their historical lease/receipt identities even
after the family has no lease or a later owner has acquired a new one.

Private paths are derived/reopened against the approved root and checked OS
identities. Deserializing a subject hash must not call
`StorageAdmissionAuthority::from_verified_batch` as if provenance had been
verified. Rehydration must pass the service's recovery boundary first.
Do not persist or deserialize live handle/termination capabilities.

### Single ownership and arithmetic

The broker's `RootWriter::apply`/ledger transaction is the sole
state-mutation/commit boundary. Every transaction acquires the broker's real
cross-process root fence, reloads the latest bounded snapshot, rechecks
epoch/policy/owners, computes a transition and atomically persists before
releasing the fence. `HostStorageService` remains one authenticated client
entry point per runtime, constructed from its manager's base Config and shared
across its sessions. Multiple Host processes coordinate through the broker,
not separate cached counters or local writers. The kernel evaluates only the
current broker transaction state. A Host-side cached snapshot is
non-authoritative for reservation, release, recovery or deletion.

Move the current `State` into the broker's durable authority rather than
mirroring it in core. Do not expose a new `ledger.reserve(receipt)` after
`reserve_member_once`. Receipt delivery/replay, group sealing and member
attachment do not reserve again. Kernel public wrappers must issue sealed,
path-free authenticated broker operations into the same transaction, never
open the protected root or recursively lock the old firewall.

Compute/validate aggregate caches from authoritative records at load and
commit; never trust a serialized global total independently:

- Family reserved = sum of original budgets of its non-Released members.
- Global reserved = those member budgets plus independently reserved control
  footprints, each physical control extent counted once.
- Family observed remains after the lease is removed; private member counts
  are not added to it again. Observed family bytes already occupy disk and
  are not newly added to global growth reservation.
- A new member adds its budget once to family reserved, family allowance and
  global reserved. Same member/intent replay returns the original grant;
  changed intent or a Released member cannot reserve again.
- For unused `Reserved` cancellation, remove that member's budget from the
  prior allowance before postcheck. For `Consumed` settlement, require the
  real termination evidence and bounded scan; then retire unused allowance:
  `next_allowance = min(adjusted_allowance, observed + remaining_reserved)`
  componentwise with checked arithmetic. Never lend a released allowance to
  another writer. When no members remain, remove the active family lease but
  retain observed usage and tombstones.
- Over-budget postcheck updates conservative known usage without releasing
  ownership/counters. Preserve Plan 1's all-members-quiet barrier for the
  final shared-family observation; a mutex is not a filesystem writer fence.

Capacity uses the existing root-plus-eight policy. Windows physical free-file
capacity remains `None`, not infinity; enforce logical file budgets from
owned observations/reservations. Metadata fits under the same global and
free-space checks. Below emergency pressure, no new positive reservation is
allowed; already-reserved bounded recovery/settlement space remains usable.
No pressure state authorizes killing a process or deleting data.

## Real authority and filesystem interfaces

Implement these interfaces as private RAII/handle types in broker-owned
`codex-rs/storage-broker-windows/src/root_fs.rs`; the names describe planned
Plan 2 capability, not work completed by the broker dependency handoff:

```rust
trait RootOwnershipProvider {
    fn acquire(&self, root: &ApprovedRoot) -> Result<RootTransactionGuard, StorageLedgerError>;
}
trait OwnedFilesystem {
    fn open_child_no_follow(
        &self, parent: &OwnedDirectoryHandle, child: &SingleComponent,
    ) -> Result<OwnedEntryHandle, StorageLedgerError>;
    fn begin_mutation(
        &self, root: &RootTransactionGuard, subject: &OwnedEntryHandle,
    ) -> Result<NamespaceMutationFence, StorageLedgerError>;
    fn remove_verified_tree(
        &self, fence: &NamespaceMutationFence, entry: &OwnedEntryHandle,
        limit: &RemovalBound,
    ) -> Result<RemovalObservation, StorageLedgerError>;
}
```

`ApprovedRoot`, `SingleComponent` and handles have broker-private constructors.
The Host authenticated client cannot construct, receive, or serialize them.
`RootTransactionGuard` retains the opened root identity and actual exclusive
OS lock for one reload/recheck/transition/persist transaction. Release it
after commit so another authenticated Host client can transact against the
new epoch; never keep one Host's cached state authoritative after releasing it.
`NamespaceMutationFence` excludes admissions
and namespace writers for the exact owned subtree until final observation/
commit. Neither type is serde, a boolean, a deadline, or a caller token.

On Windows, the broker acquires a machine-wide named mutex keyed from the
opened local volume/file identity before creating lock/ledger files; validate ownership,
abandonment and collisions. Retain no-follow directory handles and compare
volume/file IDs; reject reparse points. Child deletion uses verified handles
and the platform disposition API, not `remove_dir_all` on a reconstructed
string. Sharing/ACL rules and the process fence must exclude rename/replacement
by writers during the operation. The broker guard must serialize every Host
client using that root, not merely one core Rust mutex.

On non-Windows platforms the protected broker is unsupported and writer/apply
operations remain unavailable. Do not reintroduce a core-local Unix writer or
claim a portable secure delete from a trait stub. Remote/shared filesystems
without the broker's locking guarantee are unsupported.

Keep broker lock order explicit: OS root transaction guard, then the broker
transaction mutex, then the specific namespace fence. Reload after taking the
OS guard, not before. The Host awaits actual writer shutdown **before** its
authenticated request; the broker validates the sealed Host-owned terminal
evidence nonblockingly inside the transaction, as required by
`HostStorageTerminationEvidence`. Do not hold broker OS/accounting locks while
waiting for child exit or call back into the firewall from a proof provider.
Busy/unavailable/abandoned/unknown results fail closed and never trigger a
Host fallback or takeover by TTL.

## P2-base implementation tasks

### Task 1: Wire bootstrap and control transactions through the broker

**Files:** broker `root_fs.rs`, `ledger.rs`, `ledger_codec.rs`, `service.rs`, and
`protocol.rs`; Host client `storage_broker.rs`, `storage_service.rs`, `mod.rs`,
and the bounded queue-provenance seam in `registry.rs`.

**Precondition:** Storage broker Tasks 1-6 are delivered and compiled. This
task does not recreate their root writer; it binds verified Plan 2 control
ownership/lifecycle to the broker's three path-free transactions. Every root
open/write below is broker-side. The Host remains an authenticated client.

- [ ] In the broker, open/validate the configured root without writing, obtain
  its OS transaction guard, reload any current root snapshot, verify the unchanged
  trusted base policy and measure available capacity. An existing valid
  ledger is joined transactionally, never overwritten with an empty state.
  No new user fields are needed or allowed: all policy values remain pending
  the user's root-plus-eight choice; absent policy is not zero/unlimited.
- [ ] Calculate an internal bootstrap reservation before the first metadata
  write. Its bound includes the encoded empty snapshot/header and all files
  simultaneously alive during creation/replacement: old snapshot, stage,
  bounded pending journal/receipt, and any on-disk lock representation.
  Charge directories/files according to the same deterministic counting rule
  used by observations; do not hide stage/lock overhead.
- [ ] Use a bounded counting serializer and existing per-record protocol
  limits to prove the encoded maximum. Account for maximum numeric widths,
  current collection sizes and the proposed records. Stop encoding/scanning
  at the remaining admitted bound. Reject overflow or an unprovable bound;
  do not invent a record count, unlimited log or free bootstrap exception.
- [ ] Before a ledger exists, the broker's live root transaction guard owns this startup
  reservation in memory and excludes concurrent root transactions. Write the first durable
  snapshot containing that same control reservation, then release the guard
  and publish the service. For an existing ledger reserve only this operation's
  required delta against the reloaded shared state.
  This transfers ownership of one reservation; it does not charge twice.
  A crash leaving only stage/unknown files enters recovery, never fresh-empty
  initialization over those files.
- [ ] Before every growth/replacement, calculate peak physical coexistence and
  reserve the positive delta from existing global bytes/files limits while
  checking the free-space floor. Only then create/write. Shrink/release a
  control footprint only after durable commit and verified old/stage removal.
  Already-accounted file extents are not summed again as parent and child.
- [ ] The bootstrap allocation belongs to the root lifetime, not the first
  Host process. Record its last mutator for audit, but do not release it on
  that Host's exit or reserve it again when another authenticated Host client
  joins the same broker-owned ledger.
- [ ] Wire the broker-owned `reserve_control_once`, `replace_control_payload`
  and `settle_control` operations through the same service transaction, taking
  verified Host queue provenance and bounded encoded payloads, not caller paths/owners.
  Queue control lives under the private `approved_root/control` namespace,
  outside `generated/<target>/members` and outside the common Cargo cache.
- [ ] A queue allocation survives initial member release and all intermediate
  waves. It settles only after the verified queue is exhausted/cancelled,
  no queue writer remains and its final payload/postcheck is durable.
  It never holds a Cargo family lease. Initial and same-key successor groups
  can therefore settle/reacquire their family without destroying live queue
  metadata. Queue suballocations transfer reserved extents; they are not a
  second global charge on top of a parent footprint.
- [ ] Keep retained control metadata and replay tombstones bounded. If a next
  snapshot cannot fit, reject the mutation before write. Do not discard
  tombstones or live queue records to make it fit. Terminal metadata retirement
  needs its own proven lifecycle/epoch rule; no age-only trimming.

- [ ] Report this control-transaction slice ready only after the broker and
  Host client compile together with verified queue provenance still sealed.
  Only then may Plan 1 Task 6 durable refill/lifecycle wiring proceed. This
  does not complete remaining P2-base recovery or authorize cleanup/activation.

### Remaining P2-base after Plan 1 Task 6

### Task 2: Make kernel transitions one durable transaction

**Files:** broker `ledger.rs`, `ledger_codec.rs`, `service.rs`, and `protocol.rs`;
Host client/evaluator `storage_broker.rs`, `storage_firewall.rs`, and
`storage_service.rs`; later attachment points in `registry.rs`,
`codex_adapter.rs`, and `coordinator.rs`.

**Precondition:** Plan 1 Task 6 durable refill/lifecycle wiring follows the
broker-backed Task 1 control boundary above. This remaining P2-base slice must
not be pulled ahead of that lifecycle wiring.

- [ ] Replace the private kernel state mutex with the reloaded ledger transaction state.
  Preserve `create_group`, `reserve_member_once`, `seal_group`,
  `consume_member_once`, `revoke_unused_member` and
  `release_member_after_postcheck` semantics. Internal transition evaluators
  take transaction state rather than reacquiring a separate lock.
- [ ] Under a newly acquired broker root guard/transaction lock: reload the committed
  snapshot, verify expected epoch, current and other recorded owners/policy,
  exact authority and namespace identities; compute the
  next family/group/member/control snapshot and peak metadata reservation.
  Run all fallible validation/checked arithmetic before granting an action.
- [ ] Persist one bounded next snapshot with a unique owned stage handle,
  sync file contents, atomically replace through the held parent handles, and
  complete the platform durability barrier. Reuse existing
  `registry.rs::persist_json_file` replacement semantics as a reference, not
  as proof that its path-based helper already supplies the required fence.
  On Unix also sync the parent; on Windows use the existing durable replacement
  behavior plus identity-safe handles.
- [ ] Return a grant and refresh any read-only cache only after commit.
  A serialization/write failure before replacement keeps the old state and
  releases only proven-unused operation space. An uncertain replacement/
  durability outcome freezes the service in recovery; do not report the old
  or next epoch as certainly committed. Leftover stage files remain accounted
  and protected until their recorded identity can be verified.
- [ ] Persist Reserved admission before returning its decision and persist
  Consumed before **any** writer preparation/materialization is permitted.
  Successful transport is not another reservation. `consume_batch` and the
  native selected-wave path use this same service and original frozen batch
  hashes; no synthetic bridge exchange is needed.
- [ ] Only an unconsumed Reserved grant may use `revoke_unused_member`.
  Consumed prepare failure, cancellation, timeout and terminal ACK require
  actual never-launched or all-writing-descendants-stopped evidence plus
  postcheck before settlement. Do not unconditionally release on prepare error.
  Preserve ownership if an adapter runtime object is removed during recovery.
- [ ] Heartbeats/observations are bounded state updates, not authority renewal.
  They cannot reactivate Released members, expand budgets, or erase shared
  observed usage. A root snapshot contains the exact effect once.

### Task 3: Recover epochs/owners without inventing live authority

**Files:** broker `ledger.rs`, `ledger_codec.rs`, `root_fs.rs`, and `service.rs`;
Host client `storage_broker.rs`/`storage_service.rs` and queue recovery seams in
`registry.rs`; use Plan 1 process-lifecycle evidence.

- [ ] Have the broker acquire the real root transaction guard and reload before
  advancing state.
  Validate exact snapshot-v2 structure, checksum/predecessor epoch and pending
  operation evidence under bounded reads. Unknown/malformed/regressing state
  is protected; a self-consistent hash alone is not trusted owner authority.
- [ ] Register the starting Host's real instance/PID/creation identity and
  owner epoch through a durable transaction before its first grant. A restart
  generation applies only to the corresponding prior Host instance, never
  the entire root. Preserve other live Host owners and all their member/
  control reservations in the shared global totals. Starting this Host does
  not migrate, reclaim or relabel another active Host's members.
- [ ] Reconcile a prior owner only using actual process/Job handles and full
  ownership/path evidence. PID equality, PID absence, heartbeat expiry and
  OS mutex abandonment do not prove old descendants stopped. Uncertain old
  owners become RecoveryPending while their reservation/allowance/observation
  remains charged; live other owners remain live. Do not resume execution
  solely from stored hashes or reconstruct Sent/native authority from a string.
- [ ] Await the real owned-process fence outside the transaction; inside it
  verify the bound terminal evidence and remeasure under the namespace fence.
  Only a committed recovery settlement can release capacity. Unknown owner,
  missing evidence, replaced root, failed stat or unreconciled journal keeps
  admission/apply blocked. Quarantine is a logical protected state, not an
  automatic move/delete operation. An abandoned guard or unmatched stage
  triggers bounded pending-operation reconciliation, not automatic deletion
  or a guess about which Host owned the stage.
- [ ] Treat legacy flat lease-v1 as read-only recovery input. It cannot
  reconstruct exact sealed member sets, shared allowance or control ownership
  unambiguously, so do not auto-migrate it into active snapshot-v2 or duplicate
  one family lease per task. Unknown versions fail closed. Existing data and
  raw legacy evidence remain untouched pending an explicit audited recovery.
  A missing ledger never authorizes adoption/deletion of unknown artifacts.
- [ ] Report remaining P2-base ready only after Plan 1 Task 6 and the
  broker-backed owner recovery/restart gate are wired and compiled. It does
  not enable cleanup apply or live activation.

## P2-apply: classification, preview and locked deletion

### Task 4: Classify only proven disposable, unowned generated objects

**Files:** broker-owned cleanup/classification module plus `root_fs.rs`,
`ledger.rs`, `ledger_codec.rs`, `service.rs`, and the path-free protocol;
Host retains only its authenticated client/projection seams.

**Precondition:** Broker Tasks 1-6, Plan 2 P2-base Task 1, Plan 1 Task 6, and
the remaining P2-base owner recovery are complete in that order. None of those
prerequisites authorizes preview/apply or live activation.

- [ ] Build candidates from registered family/member generated roots only.
  Eligibility requires verified producer/classification evidence, released
  ownership, terminal/postcheck proof, no live group/member/control reference,
  stable no-follow identity and a bounded content manifest. Unreleased family
  data, unknown files, dirty output, source/worktree data, sessions, metadata
  and live queues are protected. Size/age/name may order already-eligible
  candidates but never make an object eligible.
- [ ] Shared Cargo cleanup targets the family cache once, never one task's
  duplicate lease reference. Member scratch/output is a separate bounded
  object and is not disposable merely because its phase is Released.
  Non-Cargo output can contain valuable results; require explicit classification
  and requested cleanup scope. Do not infer disposability from the four
  artifact-kind names alone.
- [ ] Preview is read-only and path-free. Under a consistent ledger view,
  bind root/policy/epoch, exact candidate set, last ownership/evidence,
  content/identity manifest and finite limits into the candidate hash.
  Counts and hashes are observations, not deletion capabilities; caller
  recomputation does not mint Host classification or a handle fence.
- [ ] Retain the planned bounded public apply payload:
  `{candidate_hash, policy_hash, ledger_epoch, batch_limit}`, with batch_limit
  1 through 16 and no path/owner fields, inside the authenticated operation
  envelope. Explicit user authorization for that destructive scope is still
  required. Status/preview are read-only; no automatic pressure cleanup.

### Task 5: Acquire fences first, then recheck and journal apply

**Files:** the broker-owned cleanup module, `root_fs.rs`, `ledger.rs`,
`ledger_codec.rs`, `service.rs`, and protocol; only afterward the Host
authenticated-client, mapped rmcp-client, and DevKit projection files.

- [ ] Resolve the broker-issued preview reference, have the broker acquire the
  root transaction lock and real namespace-mutation fence, then freshly reopen/remeasure the
  exact candidate set through retained no-follow handles. Recheck owner/
  member/control references, policy, expected epoch, classification, identity,
  content manifest and finite bounds **inside** that fenced interval.
  Never compute a new preview first and acquire the writer fence afterward.
- [ ] Any discrepancy returns `STORAGE_CANDIDATE_STALE` or the precise
  protected code before deletion. Unknown/active/dirty/recovery/control data
  remains protected. No path string, TTL, `owner_state = none` label or a
  successful hash comparison substitutes for this fenced recheck.
- [ ] Reserve the bounded apply journal/receipt peak before writing it, using
  the same control accounting. Persist an Applying operation for the exact
  selected identities/expected epoch before the first delete. The root lock
  prevents another admission during this transition; retain the namespace
  fence through deletion, post-stat and final ledger commit.
- [ ] Delete only through the broker's `OwnedFilesystem::remove_verified_tree` with finite
  bytes/files/depth/time limits, no links/mount traversal, stable opened parent
  identities and the supported platform's actual mutation exclusion.
  Reject multiply linked or unowned entries when ownership cannot be proved.
  No generic `remove_dir_all` fallback or wildcard deletion is permitted.
- [ ] Postcheck the exact result and update retained family/control observation
  once before committing the bounded receipt. Deletion does not release an
  active lease; only already-settled eligible data enters apply. On partial
  removal or an uncertain final commit, persist/protect the pending operation,
  return `STORAGE_APPLY_INCOMPLETE`, stop later items and require recovery.
  Never roll back by claiming removed bytes/files still exist or attempt an
  unrelated cleanup. Crash recovery does not auto-continue deletion.
- [ ] Expose only stable codes, hashes, counts and receipt identity through
  `storage_status`/`storage_preview`/`storage_apply`. Keep absolute paths,
  owner PID/creation identity and handles private. Stable failures include
  `STORAGE_CANDIDATE_STALE`, `STORAGE_PROTECTED_UNKNOWN`,
  `STORAGE_PROTECTED_ACTIVE`, `STORAGE_PROTECTED_DIRTY`,
  `STORAGE_APPLY_INCOMPLETE` and existing admission/recovery codes.
  Internal snapshot-v2 is not a change to public admission-v1.

## Compile-first verification and commits

Keep only two new core boundary cases; preserve existing kernel vectors.
Use temporary, owned fixture roots and injected filesystem/process backends,
never the configured live generated root.

1. `shared_family_control_transaction_is_single_charge`: two distinct members
   share one family lease; repeating one intent is not a new reservation.
   Persist/reopen a protected snapshot with allowance and retained observation
   intact. Alternate transactions through two runtime service fixtures and
   verify each reloads the latest epoch and retains the other live owner's
   reservations. Initial settlement keeps a live independent queue; a same-key
   successor can reserve without inheriting its predecessor's active lease.
   Inject commit failure and verify no uncommitted grant is returned and
   retained control/stage capacity is not silently freed.
2. `apply_rechecks_identity_under_fence_and_retains_failed_shutdown`: a
   Consumed member lacking real terminal proof cannot release or become a
   candidate. Change candidate identity/epoch between preview and fence;
   apply rejects before the delete primitive. The fixture backend verifies
   that the successful recheck/deletion interval actually holds both guards.

These are acceptance cases to implement, not claims that the test functions
already exist. Keep one small Python assertion rejecting paths/unknown fields
and unbounded apply batches if the bridge projection changes. Do not recreate
old missing-module RED failures or introduce a broad test matrix.

After a changed slice compiles, run the relevant case once, from Host
`codex-rs`; protected-root transaction/apply cases belong to the broker crate:

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-storage-broker-windows --bins -p codex-rmcp-client -p codex-core --lib --locked -j1
cargo test -p codex-storage-broker-windows shared_family_control_transaction_is_single_charge --locked -j1
cargo test -p codex-storage-broker-windows apply_rechecks_identity_under_fence_and_retains_failed_shutdown --locked -j1
```

Stop before probes if compilation fails; no repeated full-suite runs. Compile
any changed runtime caller separately with the same target/settings rather
than treating the two-crate gate as coverage of an edited app-server.
For changed Python modules, run only their `py_compile` and the selected
exact request assertion after the Rust gate. No package build or live cleanup
is needed for these document/base slices.

- [ ] Commit P2-base state/control work separately from later cleanup/wire
  changes. Use explicit named-file staging after `git diff --check`; do not
  stage other workers' changes or commit an uncompiled layer as accepted.
- [ ] Record which OS handle/descendant fence has real implementation and
  verification. An unsupported backend stays unavailable; a mock guard or
  compile success is not production deletion approval.

## Plan 2 acceptance gates and handoff

- [ ] One authenticated Host client per runtime submits path-free operations to
  the broker's cross-process root transaction: OS fence, reload/recheck
  epoch/owner state, transition, durable commit. Other live Host owners remain
  active and globally counted; no core-local writer or per-Host authoritative
  counter cache exists. Same-key members share the lease, and no path performs
  firewall admission followed by independent ledger reservation.
- [ ] Root-plus-eight trusted configuration remains unchanged and numerically
  pending the user. Bootstrap/queue/snapshot/stage/journal bytes/files are
  bounded and charged before writes from the existing global limits;
  unprovable capacity/bounds fail closed without creating metadata.
- [ ] After broker Tasks 1-6, P2-base Task 1 alone enables Plan 1's durable
  refill/lifecycle dependency; only afterward may remaining P2-base owner
  recovery proceed. Live queues survive initial member settlement without
  occupying its Cargo family lease or releasable member paths.
- [ ] Real cross-process root exclusion, epoch/creation identity, recovery
  protection and no-follow handle fences exist. TTL/strings/PID equality
  cannot authorize takeover, release or deletion.
- [ ] Released tombstones and retained family observations survive restart;
  legacy/missing/ambiguous state never grants fresh ownership over old data.
- [ ] Consumed prepare failure and terminal settlement require actual process
  proof and postcheck. Uncertain commit/shutdown/stat leaves ownership intact.
- [ ] Cleanup requires explicit scope authorization and fresh locked/fenced
  eligibility/identity checks, with bounded journal/delete/postcheck.
  Record a fixture stale-candidate/no-delete result before any separately
  authorized live apply; this plan edit itself authorizes none.
- [ ] Broker completion, package presence, compile/probe success, or later
  provisioning does not authorize cleanup or live activation. Record those as
  separate gates; this revision claims no Task 7 probes, elevated acceptance,
  or Bazel completion.
- [ ] No generated/source/session/queue data was deleted merely to complete a
  test, satisfy pressure, or repair a snapshot. Plan 3 receives protected
  classifications and the real fence, not permission for broader deletion.
