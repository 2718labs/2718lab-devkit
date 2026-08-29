# Storage Lease Ledger and Owned Cleanup 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every admitted generated-storage lease across process restarts and make generated-cache cleanup a bounded preview/recheck/apply transaction owned by the Codex Host.

**Architecture:** Plan 1 supplies the validated `StorageAdmissionReceipt` and deterministic target root. This plan adds a host-owned JSON ledger with atomic replacement, owner/process fencing, restart recovery, byte/file/free-space accounting, and a fair pressure state; the DevKit only forwards typed status/preview/apply requests over the authenticated bridge. A cleanup candidate is immutable evidence, not permission: only a fresh candidate hash, policy hash, ledger epoch, writer fence, and post-stat match can authorize a bounded generated-cache deletion.

**Tech Stack:** Rust 2021 (`serde`, `serde_json`, `sha2`, `tokio`, `std::fs`, `std::time`), Python 3.11 (`dataclasses`, `hashlib`, `json`, `pathlib`), FastMCP/Pydantic, and the existing atomic JSON replacement and authenticated inherited-handle transport.

---

## Scope and file map

Line ranges refer to the Plan 1 baseline (`37029a9` for DevKit and
`552fe8035d` for Host). Re-read the named symbol before editing because Plan 1
will add the storage admission types.

DevKit:

- Create `mcp-tools/devkit_runtime/storage_ledger.py`: typed status, preview,
  and apply request/receipt projections; it must never inspect or delete a host
  path.
- Modify `mcp-tools/devkit_runtime/host_bridge.py:218-264,916-1045` to carry
  `storage_status`, `storage_preview`, and `storage_apply` request/receipt
  frames through the authenticated session.
- Modify `mcp-tools/devkit_runtime/host_session.py:159-335,636-735` with
  `storage_status()`, `storage_preview()`, and `storage_apply()` methods that
  return only stable codes, hashes, counts, and opaque receipt identities.
- Modify `mcp-tools/server.py:179-289,1008-1314` and
  `mcp-tools/devkit_runtime/tool_metadata.py:1-28` to add the read-only
  `storage_status`/`storage_preview` tools and the explicitly destructive
  `storage_apply` tool; the apply model requires candidate/policy/epoch hashes
  and a finite batch limit.
- Create `mcp-tools/tests/test_storage_ledger.py` and modify
  `mcp-tools/tests/test_mcp_contract.py:240-360` for tool annotations and
  exact request validation.

Codex Host:

- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`: the
  `LeaseRecord`, ledger snapshot, atomic journal, owner probe, recovery state,
  quota accounting, candidate manifest, and generated apply transaction.
- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49` to register and
  export the ledger types to the coordinator.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs` at
  its admission/release methods to call the ledger rather than maintaining
  process-local counters.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-2060`
  and `coordinator.rs:393-580,1218-1260` to recover/open the ledger at host
  startup, reserve/heartbeat/release records, and block admission in pressure
  or recovery state.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`
  and `.../envelope.rs:25-220` for exact ledger/preview/apply wire schemas.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs:159-240,247-350,412-570`
  to expose the typed operation queue without creating a second receiver.

No source/session deletion, GitHub reachability, or CAS deduplication belongs
to this plan; those are Plan 3. No unknown directory can enter this ledger.

## Shared lease schema and public operation contract

This plan consumes Plan 1's `StorageAdmissionReceipt` and uses this exact
record shape for `schema == "2718lab.storage.lease.v1"`:

```rust
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LeaseRecord {
    pub(crate) ledger_epoch: u64,
    pub(crate) schema_version: String,
    pub(crate) lease_id: String,
    pub(crate) task_id: String,
    pub(crate) assignment_id: String,
    pub(crate) plan_binding: String,
    pub(crate) project_identity: String,
    pub(crate) repository_identity: String,
    pub(crate) worktree_identity: String,
    pub(crate) artifact_kind: String,
    pub(crate) target_key: String,
    pub(crate) path_identity: String,
    pub(crate) owner_epoch: u64,
    pub(crate) owner_kind: String,
    pub(crate) process_id: u32,
    pub(crate) process_start_time: u64,
    pub(crate) host_instance_id: String,
    pub(crate) state: LeaseState,
    pub(crate) created_at: u64,
    pub(crate) last_heartbeat: u64,
    pub(crate) expires_at: u64,
    pub(crate) restart_generation: u64,
    pub(crate) reserved_bytes: u64,
    pub(crate) reserved_files: u64,
    pub(crate) observed_bytes: u64,
    pub(crate) observed_files: u64,
    pub(crate) free_space_before: u64,
    pub(crate) free_space_after_reserve: u64,
    pub(crate) free_space_floor: u64,
    pub(crate) candidate_hash: Option<String>,
    pub(crate) receipt_hash: Option<String>,
    pub(crate) release_reason: Option<String>,
    pub(crate) cleanup_policy_hash: Option<String>,
}
```

`LeaseState` is exactly `reserved | active | released | recovery_pending |
quarantined | cleanup_eligible`. The only legal automatic transitions are
`reserved -> active -> released`; restart evidence can move an active or
reserved record to `recovery_pending`, and failed verification can move it to
`quarantined`. `cleanup_eligible` never deletes anything by itself.
`StorageLedgerError::LeaseConflict` maps exactly to
`STORAGE_LEASE_CONFLICT`; it is returned for a stale owner proof, duplicate
activation, heartbeat after release, and release by a different owner.

The public status/preview/apply shapes are:

```json
{
  "schema": "2718lab.storage.preview.v1",
  "ledger_epoch": 12,
  "policy_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "candidate_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "candidates": [
    {
      "path_identity": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "artifact_kind": "cargo-target",
      "bytes": 1024,
      "files": 3,
      "content_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "owner_state": "none",
      "classification": "generated-disposable",
      "lease_id": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    }
  ]
}
```

`storage_apply` accepts exactly `candidate_hash`, `policy_hash`,
`ledger_epoch`, and `batch_limit` (1 through 16). It returns
`STORAGE_CANDIDATE_STALE`, `STORAGE_PROTECTED_UNKNOWN`,
`STORAGE_PROTECTED_ACTIVE`, `STORAGE_PROTECTED_DIRTY`, or
`STORAGE_APPLY_INCOMPLETE` without deleting when any recheck differs.

## Implementation tasks

### Task 1: Define ledger and operation RED tests

**Files:**
- Create: `mcp-tools/tests/test_storage_ledger.py`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`
- Modify: `mcp-tools/tests/test_mcp_contract.py:240-360`

- [ ] **Step 1: Add the Python RED test for exact apply fields and bounded batch.**

```python
def test_storage_apply_rejects_path_and_unbounded_batch():
    from devkit_runtime.storage_ledger import StorageApplyRequest, StorageLedgerError

    try:
        StorageApplyRequest.from_mapping({
            "candidate_hash": "sha256:" + "a" * 64,
            "policy_hash": "sha256:" + "b" * 64,
            "ledger_epoch": 1,
            "batch_limit": 17,
            "path": "G:/source"
        })
    except StorageLedgerError as error:
        assert error.code == "STORAGE_CANDIDATE_STALE"
    else:
        raise AssertionError("invalid apply request was accepted")
```

- [ ] **Step 2: Add the Rust RED test for an invalid transition.**

```rust
#[test]
fn released_lease_cannot_receive_a_heartbeat() {
    let mut ledger = test_ledger();
    let lease = ledger.reserve(test_admission()).unwrap();
    ledger.activate(&lease.lease_id, owner()).unwrap();
    ledger.release(&lease.lease_id, "terminal").unwrap();
    assert_eq!(
        ledger.heartbeat(
            &lease.lease_id,
            owner(),
            ObservedStorage { bytes: 100, files: 1 },
        ),
        Err(StorageLedgerError::LeaseConflict),
    );
}
```

- [ ] **Step 3: Run only the new RED tests.**

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_ledger.py::test_storage_apply_rejects_path_and_unbounded_batch -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-pytest
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-rust-target'; cargo test -p codex-core released_lease_cannot_receive_a_heartbeat --locked -j1; Pop-Location
```

Expected: both commands fail because the ledger types do not exist. The
failure must occur before any production path or deletion call.

- [ ] **Step 4: Commit only the RED contract.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/tests/test_storage_ledger.py mcp-tools/tests/test_mcp_contract.py; git commit -m 'test: define owned storage ledger contract'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs; git commit -m 'test: define owned storage ledger contract'; Pop-Location
```

### Task 2: Implement atomic ledger snapshots and schema migration

**Files:**
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`

- [ ] **Step 1: Define the store and exact snapshot envelope.**

```rust
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LedgerSnapshot {
    pub(crate) schema: String,
    pub(crate) ledger_epoch: u64,
    pub(crate) restart_generation: u64,
    pub(crate) host_instance_id: String,
    pub(crate) policy_hash: String,
    pub(crate) leases: Vec<LeaseRecord>,
    pub(crate) journal: Option<LedgerJournal>,
}

pub(crate) struct StorageLedger {
    path: PathBuf,
    snapshot: LedgerSnapshot,
    owner_probe: Box<dyn OwnerProbe>,
    capacity: Box<dyn CapacityProvider>,
}
```

`open` must reject a symlink/reparse-point ledger file, malformed JSON, a
non-monotonic epoch, unknown state, duplicate `lease_id`, or a path whose
canonical parent is outside the approved generated root. An absent file is
opened as a zero-lease `storage-ledger-v1` snapshot only after the parent root
has been proved approved; it is not an authorization to write arbitrary roots.

- [ ] **Step 2: Implement atomic replacement with a journal.**

```rust
fn persist(&mut self, next: LedgerSnapshot) -> Result<(), StorageLedgerError> {
    validate_snapshot(&next)?;
    let temporary = self.path.with_extension("json.stage");
    let bytes = serde_json::to_vec(&next).map_err(|_| StorageLedgerError::StatUnavailable)?;
    let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary)
        .map_err(|_| StorageLedgerError::StatUnavailable)?;
    file.write_all(&bytes).map_err(|_| StorageLedgerError::StatUnavailable)?;
    file.sync_all().map_err(|_| StorageLedgerError::StatUnavailable)?;
    replace_file_durably(&temporary, &self.path)?;
    self.snapshot = next;
    Ok(())
}
```

The Windows replace helper must use the same write-through replacement
semantics already used by `registry.rs:4281-4380`; Unix uses `rename` after
`sync_all`. A failed replacement leaves the prior snapshot and the stage file
is removed only when its identity still matches the stage created by this
operation.

- [ ] **Step 3: Add migration and rollback tests, then turn the RED schema tests green.**

```rust
#[test]
fn old_or_missing_ledger_becomes_recovery_pending_without_deletion() {
    let mut ledger = open_fixture_with_legacy_snapshot();
    let result = ledger.recover_after_restart(current_owner_set_empty());
    assert!(result.is_ok());
    assert!(ledger.records().iter().all(|record| record.state == LeaseState::RecoveryPending));
    assert!(fixture_generated_file().exists());
}
```

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-rust-target'; cargo test -p codex-core storage_ledger --locked -j1; Pop-Location
```

Expected: the focused ledger tests pass; migration failure returns to the
previous snapshot and keeps every generated file.

- [ ] **Step 4: Commit the durable ledger core.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs codex-rs/core/src/fast_lane_host_dispatch/mod.rs; git commit -m 'feat: persist storage lease ledger atomically'; Pop-Location
```

### Task 3: Enforce reserve, heartbeat, release, and pressure gates

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-1668`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs:393-580,1218-1260`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`

- [ ] **Step 1: Add RED accounting tests for all four admission equations.**

```rust
#[test]
fn byte_file_global_and_floor_limits_fail_closed() {
    let cases = [
        (AdmissionMutation::TaskBytes, "STORAGE_QUOTA_EXCEEDED"),
        (AdmissionMutation::TaskFiles, "STORAGE_FILE_LIMIT_EXCEEDED"),
        (AdmissionMutation::GlobalReserved, "STORAGE_QUOTA_EXCEEDED"),
        (AdmissionMutation::FreeFloor, "STORAGE_FREE_SPACE_FLOOR"),
    ];
    for (mutation, code) in cases {
        let firewall = fixture_firewall(mutation);
        assert_eq!(firewall.admit(test_intent()).unwrap_err().code(), code);
        assert!(fixture_target_root().read_dir().unwrap().next().is_none());
    }
}
```

- [ ] **Step 2: Implement lease state methods with owner fencing.**

```rust
pub(crate) fn reserve(&mut self, admission: StorageAdmissionReceipt, now: u64) -> Result<LeaseRecord, StorageLedgerError>;
pub(crate) fn activate(&mut self, lease_id: &str, owner: OwnerProof) -> Result<(), StorageLedgerError>;
pub(crate) fn heartbeat(&mut self, lease_id: &str, owner: OwnerProof, observed: ObservedStorage) -> Result<LeaseRecord, StorageLedgerError>;
pub(crate) fn release(&mut self, lease_id: &str, owner: OwnerProof, reason: &str) -> Result<ReleaseReceipt, StorageLedgerError>;
```

`heartbeat` remeasures bytes/files and applies the task, family, global, and
free-space equations before persisting. If an observation exceeds a limit,
new reservations return the stable pressure/quota code; the active lease is
not killed and its directory is not deleted. At or below
`emergency_floor_bytes`, the ledger enters `pressure=true` and allows only
release, recovery, and read-only preview operations.

- [ ] **Step 3: Connect `registry.rs` and the coordinator to the ledger.**

```rust
let admission = storage_firewall.admit(intent)?;
let lease = storage_ledger.reserve(admission, clock.now()?)?;
let prepared = adapter.prepare_batch_with_storage(batch, lease.clone()).await?;
storage_ledger.activate(&lease.lease_id, owner_probe.current()?)?;
```

Every failed preparation calls `release` with `"prepare_failed"`; every
terminal/recovery path calls it with its exact reason. Releasing the Fast Lane
scope lease and releasing storage are separate journal entries bound by the
same `assignment_id`, `plan_binding`, and receipt hash.

- [ ] **Step 4: Run the focused accounting and core compile gates.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-rust-target'; cargo test -p codex-core byte_file_global_and_floor_limits_fail_closed --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: the four cases pass and `cargo check` finishes with zero warnings.
If disk statistics fail, the result is `STORAGE_STAT_UNAVAILABLE` and no new
target root is created.

- [ ] **Step 5: Commit the quota and lifecycle wiring.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs codex-rs/core/src/fast_lane_host_dispatch/registry.rs codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs; git commit -m 'feat: bind storage leases to quota lifecycle'; Pop-Location
```

### Task 4: Implement restart owner recovery and fail-closed quarantine

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:3809-4380`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`

- [ ] **Step 1: Add RED tests for PID reuse, changed path, unknown files, and locked-stat recovery.**

```rust
#[test]
fn restart_requires_instance_pid_start_and_owner_epoch() {
    let mut ledger = open_fixture_with_active_lease(owner_with(41, 900, 7));
    ledger.recover_after_restart(owner_with(41, 901, 7)).unwrap();
    assert_eq!(ledger.records()[0].state, LeaseState::RecoveryPending);
    assert_eq!(ledger.records()[0].restart_generation, 2);
}
```

- [ ] **Step 2: Implement `OwnerProbe` and recovery validation.**

```rust
pub(crate) trait OwnerProbe: Send + Sync {
    fn current(&self) -> Result<OwnerProof, StorageLedgerError>;
    fn matches(&self, owner: &OwnerProof) -> Result<bool, StorageLedgerError>;
}

fn recover_record(record: &mut LeaseRecord, owner_probe: &dyn OwnerProbe, root: &Path) -> Result<(), StorageLedgerError> {
    if record.state != LeaseState::Active && record.state != LeaseState::Reserved {
        return Ok(());
    }
    if !owner_probe.matches(&OwnerProof::from_record(record))?
        || !verify_target_identity(root, record)?
        || !manifest_matches(record)?
    {
        record.state = LeaseState::Quarantined;
        return Ok(());
    }
    record.state = LeaseState::Active;
    Ok(())
}
```

The host increments `restart_generation` under the ledger lock before examining
records. Missing receipt, path change, dirty state, unknown file, or a failed
lock/stat check becomes `quarantined`; an owner that cannot be proved becomes
`recovery_pending` until a later explicit recovery receipt. `apply` is blocked
while any recovery remains unresolved.

- [ ] **Step 3: Add a restart recovery receipt and verify no deletion occurred.**

```rust
assert_eq!(receipt.code(), "STORAGE_RECOVERY_REQUIRED");
assert_eq!(receipt.restart_generation(), 2);
assert!(fixture_generated_file().exists());
```

- [ ] **Step 4: Run the focused recovery probe and compile gate.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-rust-target'; cargo test -p codex-core restart_requires_instance_pid_start_and_owner_epoch --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: `1 passed`, then a zero-warning compile.

- [ ] **Step 5: Commit restart recovery.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs codex-rs/core/src/fast_lane_host_dispatch/registry.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs; git commit -m 'feat: recover storage ownership across restarts'; Pop-Location
```

### Task 5: Add preview hash, recheck fence, and bounded generated apply

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:25-220`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs:412-570`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs`

- [ ] **Step 1: Add RED tests for candidate invalidation and protected classifications.**

```rust
#[test]
fn preview_hash_invalidates_on_epoch_owner_or_content_change() {
    let mut ledger = test_ledger_with_disposable_candidate();
    let preview = ledger.preview().unwrap();
    ledger.bump_epoch_for_test();
    let error = ledger.apply(&ApplyRequest::from_preview(&preview, 1)).unwrap_err();
    assert_eq!(error.code(), "STORAGE_CANDIDATE_STALE");
    assert!(fixture_candidate_path().exists());
}
```

- [ ] **Step 2: Implement canonical candidate manifest and preview hash.**

```rust
pub(crate) fn preview(&self) -> Result<StoragePreview, StorageLedgerError> {
    let mut candidates = self.scan_registered_generated_roots()?;
    candidates.sort_by(|left, right| left.path_identity.cmp(&right.path_identity));
    let manifest = serde_json::json!({
        "schema": "2718lab.storage.preview.v1",
        "ledger_epoch": self.snapshot.ledger_epoch,
        "policy_hash": self.snapshot.policy_hash,
        "candidates": candidates,
    });
    Ok(StoragePreview { manifest, candidate_hash: canonical_hash(&manifest)? })
}
```

The scan follows no reparse point, visits only ledger-registered generated
roots, and labels active/unknown/dirty/source/session entries as protected.
It does not select by size, age, or directory name.

- [ ] **Step 3: Implement apply as recheck, journal, delete, postcheck.**

```rust
pub(crate) fn apply(&mut self, request: ApplyRequest) -> Result<ApplyReceipt, StorageLedgerError> {
    let preview = self.preview()?;
    if request.candidate_hash != preview.candidate_hash
        || request.policy_hash != self.snapshot.policy_hash
        || request.ledger_epoch != self.snapshot.ledger_epoch
    {
        return Err(StorageLedgerError::CandidateStale);
    }
    let fence = self.writer_fence()?;
    let selected = preview.generated_disposable(request.batch_limit)?;
    for candidate in selected {
        self.recheck_candidate(&candidate, &fence)?;
        self.write_journal_started(&candidate)?;
        self.remove_verified_generated_path(&candidate)?;
        if candidate.path_exists()? {
            return Err(StorageLedgerError::PostcheckFailed);
        }
        self.mark_released(&candidate.lease_id)?;
    }
    self.write_receipt_and_release_fence()
}
```

A failed item returns `STORAGE_APPLY_INCOMPLETE` and leaves all later items
untouched. A changed candidate releases the writer fence without deletion.
Apply cannot run while the ledger is in pressure recovery or while any
candidate is active, unknown, dirty, source, or session classified.

- [ ] **Step 4: Add the exact bridge operations and DevKit projections.**

```python
def storage_apply(self, request: StorageApplyRequest) -> dict[str, object]:
    if request.batch_limit < 1 or request.batch_limit > 16:
        return {"code": "STORAGE_CANDIDATE_STALE"}
    response = _host_session().storage_apply(request.to_wire())
    return project_storage_receipt(response)
```

The bridge validator requires exact schemas
`2718lab.storage.status.v1`, `2718lab.storage.preview.v1`, and
`2718lab.storage.apply.v1`; `storage_apply` is the only destructive operation.
The DevKit projection strips absolute paths and owner/process values before
returning an MCP result.

- [ ] **Step 5: Run preview/apply focused tests and compile-first gates.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-rust-target'; cargo test -p codex-core preview_hash_invalidates_on_epoch_owner_or_content_change --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery\mcp-tools'; python -m pytest tests/test_storage_ledger.py -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-ledger-pytest; python -m py_compile devkit_runtime/storage_ledger.py devkit_runtime/host_bridge.py devkit_runtime/host_session.py server.py; Pop-Location
```

Expected: focused Rust and Python tests pass, both compile gates are silent,
and only the named task-local target/cache roots are touched.

- [ ] **Step 6: Commit preview/apply and bridge integration.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/devkit_runtime/storage_ledger.py mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/devkit_runtime/tool_metadata.py mcp-tools/server.py mcp-tools/tests/test_storage_ledger.py mcp-tools/tests/test_mcp_contract.py; git commit -m 'feat: add owned storage preview and apply'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger_tests.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs; git commit -m 'feat: add owned storage preview and apply'; Pop-Location
```

## Plan 2 acceptance gate and handoff

- [ ] Re-read the design sections “Task Storage Lease Ledger”, “重启恢复”, “Preview、候选哈希、复核与 Apply”, and “稳定错误”; map every listed field/code to a task above.
- [ ] Run `git diff --check` in both worktrees and verify only the mapped files changed.
- [ ] Run DevKit `py_compile` and Host `cargo check -p codex-core --lib --locked -j1` with the one named task target; zero warnings are required before any package build.
- [ ] Record receipts for reserve, heartbeat, release, restart recovery, candidate stale, protected candidate, successful one-item generated apply, and partial apply. Each receipt must include ledger epoch and receipt hash.
- [ ] Verify a missing/legacy ledger migrates to recovery protection, a failed atomic write leaves the previous snapshot, pressure blocks new reservations, and no operation kills another process or scans outside registered generated roots.
- [ ] Do not implement GitHub source deletion, ordinary/active session deletion, CAS dedupe, compression, or remote synchronization. Plan 3 consumes the ledger's protected classifications and apply fence.
