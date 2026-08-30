# Windows Protected Storage Broker 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-only, explicitly provisioned machine service that is the sole writer for the 1.1.3 protected storage root, while the Host becomes an authenticated client that fails closed whenever the broker is disabled, absent, untrusted, mismatched, or recovering.

**Architecture:** Follow `RECOMMEND_C` and treat `NEW_BROKER_REQUIRED` as resolved only by a separate `codex-storage-broker-windows` crate. A LocalSystem SCM service with an unrestricted Service SID owns the protected root and all ledger/control mutations; the Host verifies the connected service process and root identity and never falls back to its current in-process writer. Provisioning is a separate, explicit elevated action, never a side effect of build, package install, Host startup, configuration load, or this plan.

**Tech Stack:** Rust 2024, Cargo workspace, `windows-sys` 0.52 Win32 APIs, length-prefixed binary IPC over a local named pipe, SHA-256, existing handle/no-reparse storage primitives, Bazel `codex_rust_crate`, PowerShell acceptance probes, GitHub Windows release packaging.

---

## Status, repositories, and non-goals

- Host repository: `G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2`.
- DevKit repository: `G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery`.
- Host baseline commits `d41c566`, `11e9330`, `9ca17fd`, and `f80ab0b` have already passed their separate specification and quality reviews. Preserve their fail-closed bootstrap behavior while moving write authority out of `codex-core`.
- Existing unrelated Host edits in `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs` and `coordinator.rs` are outside this plan until the later Plan 1 lifecycle slice explicitly owns them. Never stage them accidentally.
- `FastLaneStorageConfigToml` remains the exact approved root plus eight non-zero policy values. Missing configuration is the default-disabled state. Present configuration with no valid provisioning is `STORAGE_BROKER_UNPROVISIONED`, not permission to create a local ledger.
- This plan does not run provisioning, create a service, edit SCM, change an ACL, create the production root, activate live configuration, clean generated data, or delete a legacy/staged object.
- This plan does not implement generated cleanup. Cleanup stays in Plan 2 `P2-apply` after the broker, Plan 1 lifecycle proof, preview, and deletion fences exist.

## Existing code to reuse deliberately

| Existing path and symbol | Reuse boundary |
| --- | --- |
| `codex-rs/core/src/fast_lane_host_dispatch/storage_fs.rs::{ApprovedRoot, PlatformRootOwnershipProvider, write_stage, publish_stage, StageDestination}` | Move the handle-based root writer into the broker crate. `ReplaceVerified` must become a real broker-only operation; it must remain fail closed until destination identity, parent identity, stage identity, and durable replacement are all verified. |
| `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs::{RootStorageLedger, reserve_control_once, replace_control_payload, settle_control}` | Move the reviewed bootstrap transaction and the three control API shapes into broker ownership. Do not leave a second compilable writer in `codex-core`. |
| `codex-rs/core/src/fast_lane_host_dispatch/storage_service.rs::{ValidatedStorageConfig, HostStorageService}` | Keep trusted config provenance and the one-service-per-runtime object; replace local ledger construction with one cached, authenticated `BrokerClient`. |
| `codex-rs/rmcp-client/src/inherited_host_bridge.rs::create_platform_bridge` | Copy the local-only pipe, protected DACL, `FILE_FLAG_FIRST_PIPE_INSTANCE`, PID, and process-creation binding patterns; do not reuse its same-user bearer authority. |
| `codex-rs/rmcp-client/src/stdio_server_launcher.rs` | Reuse the `GetNamedPipeServerProcessId` plus `OpenProcess`/`GetProcessTimes` anti-PID-reuse sequence. |
| `codex-rs/windows-sandbox-rs/src/elevated/runner_pipe.rs::{create_named_pipe, connect_pipe}` | Reuse `PIPE_REJECT_REMOTE_CLIENTS`, client PID discovery, and explicit SDDL construction patterns. Broker authentication is stricter than the runner helper. |
| `codex-rs/windows-sandbox-rs/src/bin/setup_main/win/no_reparse_dir.rs::open_or_create_no_reparse` | Port the `OBJ_DONT_REPARSE` directory-open pattern into broker provisioning/root code; do not add a dependency from the broker to the large sandbox crate. |
| `codex-rs/windows-sandbox-rs/src/acl.rs` | Follow its `SetNamedSecurityInfoW`, protected DACL, SID conversion, and post-write ACL verification patterns. |
| `.github/workflows/rust-release-windows.yml`, `.github/scripts/build-codex-package-archive.sh`, `scripts/codex_package/{targets,cargo,cli,layout}.py` | Add, sign, stage, archive, and validate the service and provisioner as Windows resources. Packaging them never provisions them. |

## Locked security and wire contracts

The implementation must use these names and bounds consistently across tasks:

```rust
pub const SERVICE_NAME: &str = "2718labStorageBroker";
pub const SERVICE_ACCOUNT: &str = "LocalSystem";
pub const PIPE_NAME: &str = r"\\.\pipe\2718lab-storage-broker-v1";
pub const PROTOCOL_MAGIC: [u8; 8] = *b"2718SB01";
pub const PROTOCOL_VERSION: u16 = 1;
pub const MAX_CONTROL_PAYLOAD_BYTES: usize = 1024 * 1024;
pub const MAX_FRAME_BODY_BYTES: usize = MAX_CONTROL_PAYLOAD_BYTES + 4096;
pub const MAX_FRAME_BYTES: usize = 16 + MAX_FRAME_BODY_BYTES;
pub const MAX_REQUESTS_PER_CONNECTION: u32 = 64;
pub const MAX_CONCURRENT_CONNECTIONS: usize = 8;
pub const IO_DEADLINE: Duration = Duration::from_secs(5);
pub const BROKER_RELEASE_SEQUENCE: u64 = 1_001_003;
```

The 16-byte frame header is exactly magic `[u8; 8]`, protocol `u16` little-endian, opcode `u16` little-endian, and body length `u32` little-endian. Decoding rejects a wrong magic/version, unknown opcode, a body over the cap, truncation, trailing bytes, duplicate fields, a zero identifier, and any non-canonical enum value before allocating the declared body.

```rust
pub type Digest32 = [u8; 32];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub creation_time_100ns: u64,
    pub image_sha256: Digest32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RootIdentity {
    pub volume_serial_number: u64,
    pub file_id: [u8; 16],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControlAuthority {
    pub host_owner_id: Digest32,
    pub queue_id: Digest32,
    pub queue_epoch: NonZeroU64,
    pub active_lease_set_hash: Digest32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ControlMutation {
    ReserveOnce {
        control_id: Digest32,
        expected_ledger_epoch: u64,
        authority: ControlAuthority,
        payload: BoundedControlPayload,
    },
    ReplaceVerified {
        control_id: Digest32,
        expected_ledger_epoch: u64,
        expected_payload_sha256: Digest32,
        authority: ControlAuthority,
        payload: BoundedControlPayload,
    },
    Settle {
        control_id: Digest32,
        expected_ledger_epoch: u64,
        expected_payload_sha256: Digest32,
        authority: ControlAuthority,
    },
}

pub struct BrokerRequest {
    pub request_id: Digest32,
    pub connection_nonce: Digest32,
    pub root_identity: RootIdentity,
    pub mutation: ControlMutation,
}

pub struct BrokerReceipt {
    pub request_id: Digest32,
    pub previous_ledger_epoch: u64,
    pub committed_ledger_epoch: u64,
    pub snapshot_sha256: Digest32,
    pub payload_sha256: Digest32,
    pub root_identity: RootIdentity,
    pub service_process: ProcessIdentity,
}
```

No broker request contains a path, path component, environment-derived directory, owner SID string, service name, byte/file limit, release number, or deletion instruction. The service takes its single root, policy, accepted Host image hashes, release floor, and service binary hash only from its machine-protected active record.

The Host/client handshake is ordered and mutually bound:

1. Host loads `%ProgramData%\2718lab\StorageBroker\active-v1.json` through a no-reparse handle and verifies owner `SYSTEM` plus a protected DACL with no non-administrator write ACE.
2. Host opens only `PIPE_NAME` with `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`; UNC/remote pipe names are not representable by the API.
3. Host obtains the pipe server PID, matches it to `QueryServiceStatusEx(SERVICE_NAME)`, opens the process, records creation time, validates service SID membership, validates the protected version-directory image path and exact manifest SHA-256, then rechecks PID/creation after the handshake.
4. `ServerHello` repeats service PID/creation, broker release sequence, service image hash, root identity, provisioning generation, and both nonces. Every field must match the protected record or live handles.
5. Service obtains the client PID with `GetNamedPipeClientProcessId`, binds its creation time, image path, exact authorized Host SHA-256, and Authenticode publisher, then rechecks PID/creation before each mutation. A merely same-user process is rejected.

## State machines and fail-closed outcomes

Root transaction states are:

```text
Locked -> Reloaded -> PeakReserved -> StageSynced -> ReplaceVerified -> Committed
   |          |             |               |               |
   +----------+-------------+---------------+---------------+-> RecoveryRequired
```

- The broker is the only process allowed a write-capable root handle.
- `ReplaceVerified` checks the still-open destination, parent, and stage identities immediately before replacement and verifies the new identity/hash immediately afterward.
- Failure before replacement leaves the old committed snapshot authoritative and retains the deterministic stage as protected recovery input.
- An uncertain replacement or durability barrier enters `RecoveryRequired`; it never guesses old/new state, removes the stage, recreates an empty ledger, or serves another mutation.
- Startup with a stage/journal reconciles only an exact recorded predecessor/next hash and identity. Any ambiguity remains recovery-required for explicit administrator repair.

Provisioning states are:

```text
Absent/ActiveOld -> CandidateVerified -> CandidateProtected -> ScmSwitched
      -> HealthChecked -> ActiveRecordPublished
```

- The active record is published last. Before that publication, Host clients continue trusting only the old record.
- A candidate release lower than the protected release floor is rejected. The same sequence is idempotent only when every binary/root/Host hash is identical.
- Failure before `ScmSwitched` leaves the old service/root/record untouched. Failure after it restores the old SCM image path and restarts the old service; the old version directory and root are retained.
- Candidate/stage artifacts are never age-cleaned. A later explicit `status` or `repair` command reports/reconciles them.

## Task 1: Scaffold the independent protocol/client crate

**Files:**
- Create: `codex-rs/storage-broker-windows/Cargo.toml`
- Create: `codex-rs/storage-broker-windows/BUILD.bazel`
- Create: `codex-rs/storage-broker-windows/src/lib.rs`
- Create: `codex-rs/storage-broker-windows/src/protocol.rs`
- Create: `codex-rs/storage-broker-windows/src/error.rs`
- Create: `codex-rs/storage-broker-windows/src/provision_record.rs`
- Modify: `codex-rs/Cargo.toml`
- Modify: `codex-rs/Cargo.lock`
- Modify: `MODULE.bazel.lock`

- [ ] Add workspace member `storage-broker-windows` and workspace dependency `codex-storage-broker-windows = { path = "storage-broker-windows" }`. The crate exposes protocol/client types on every OS; service/provisioning modules are `cfg(windows)`, and non-Windows `BrokerClient::connect` returns `STORAGE_BROKER_UNSUPPORTED`.
- [ ] Implement the locked header, `BoundedControlPayload::try_from(Vec<u8>)`, exact encode/decode functions, and a closed `BrokerErrorCode` enum. Use checked arithmetic before allocation and return only stable codes plus bounded diagnostic text.
- [ ] Implement strict `ActiveProvisionRecordV1` decoding with `serde(deny_unknown_fields)`, a maximum 16 KiB file, one active and at most one rollback Host hash, exact `BROKER_RELEASE_SEQUENCE`, fixed service/pipe names, fixed protected base directories, and `RootIdentity`. Reject environment-supplied Program Files/ProgramData paths; Windows code resolves known folders.
- [ ] Keep public API minimal and path-free:

```rust
pub fn encode_request(request: &BrokerRequest) -> Result<Vec<u8>, BrokerError>;
pub fn decode_request(frame: &[u8]) -> Result<BrokerRequest, BrokerError>;
pub fn encode_response(response: &BrokerResponse) -> Result<Vec<u8>, BrokerError>;
pub fn decode_response(frame: &[u8]) -> Result<BrokerResponse, BrokerError>;
```

- [ ] Refresh Bazel lock state because a new workspace crate changes Cargo metadata, even when all external versions already exist.
- [ ] Compile before any probe:

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check --locked -p codex-storage-broker-windows --lib -j1
```

Expected: exit `0`; no service, directory, pipe, ACL, or root is created.

- [ ] Run `git diff --check`, stage only the Task 1 files, and commit:

```powershell
git commit -m "feat: define bounded storage broker protocol"
```

## Task 2: Implement SCM service identity and authenticated local pipe

**Files:**
- Create: `codex-rs/storage-broker-windows/src/identity.rs`
- Create: `codex-rs/storage-broker-windows/src/pipe.rs`
- Create: `codex-rs/storage-broker-windows/src/service.rs`
- Create: `codex-rs/storage-broker-windows/src/bin/storage_broker_service.rs`
- Modify: `codex-rs/storage-broker-windows/Cargo.toml`
- Modify: `codex-rs/storage-broker-windows/BUILD.bazel`

- [ ] Add binary `codex-storage-broker-service`. Its only accepted entry is SCM `StartServiceCtrlDispatcherW`; console launch returns `STORAGE_BROKER_SCM_REQUIRED`. Register STOP/SHUTDOWN controls, report exact service states, and drain bounded in-flight requests before stopping.
- [ ] Require LocalSystem plus the unrestricted `NT SERVICE\2718labStorageBroker` SID in the live token. At startup, compare SCM-configured image path, process image handle/hash, protected version directory identity/DACL, release sequence, and active record. Any mismatch reports `SERVICE_STOPPED` with a deterministic service-specific exit code.
- [ ] Create one local byte-mode pipe with `FILE_FLAG_FIRST_PIPE_INSTANCE`, `PIPE_REJECT_REMOTE_CLIENTS`, the fixed name, a protected DACL, eight maximum instances, and five-second overlapped I/O cancellation. Never accept a caller-selected pipe name.
- [ ] Implement both endpoint verifiers around live handles:

```rust
fn verify_service_endpoint(
    pipe: &OwnedHandle,
    record: &ActiveProvisionRecordV1,
) -> Result<VerifiedServiceEndpoint, BrokerError>;

fn verify_host_client(
    pipe: &OwnedHandle,
    record: &ActiveProvisionRecordV1,
) -> Result<VerifiedHostClient, BrokerError>;
```

The first uses `GetNamedPipeServerProcessId`, `QueryServiceStatusEx`, `OpenProcess`, `GetProcessTimes`, `OpenProcessToken`, `CheckTokenMembership`, `QueryFullProcessImageNameW`, and SHA-256. The second uses `GetNamedPipeClientProcessId`, the same PID/creation/image sequence, exact Host hash allowlisting, and `WinVerifyTrust`. Recheck process creation after hashing and after handshake.
- [ ] Bind requests to two 32-byte OS-random nonces and the verified process/root record. Close the connection on a repeated request ID, request-count overflow, timeout, short write/read, unknown response, or identity drift.
- [ ] Compile the library and service binary only:

```powershell
cargo check --locked -p codex-storage-broker-windows --lib --bin codex-storage-broker-service -j1
```

Expected: exit `0`; running the binary directly is not part of this task.

- [ ] Run `git diff --check`, stage only Task 2 files, and commit:

```powershell
git commit -m "feat: authenticate storage broker pipe endpoints"
```

## Task 3: Move the root writer and three control transactions into the broker

**Files:**
- Create: `codex-rs/storage-broker-windows/src/root_fs.rs`
- Create: `codex-rs/storage-broker-windows/src/ledger.rs`
- Create: `codex-rs/storage-broker-windows/src/ledger_codec.rs`
- Modify: `codex-rs/storage-broker-windows/src/service.rs`
- Modify: `codex-rs/storage-broker-windows/src/lib.rs`
- Source to migrate: `codex-rs/core/src/fast_lane_host_dispatch/storage_fs.rs`
- Source to migrate: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`

- [ ] Port the reviewed no-follow handles, root identity, OS root exclusion, bounded read, stage sync, and snapshot codec into the independent crate. Keep modules below 500 lines by separating filesystem handles, snapshot codec, and transaction logic.
- [ ] Make `BrokerState` own exactly one `RootWriter` for the protected record:

```rust
pub struct RootWriter {
    root: ApprovedRoot,
    policy: StoragePolicy,
    transaction: Mutex<()>,
    recovery: AtomicBool,
}

impl RootWriter {
    pub fn apply(
        &self,
        client: &VerifiedHostClient,
        request: BrokerRequest,
    ) -> Result<BrokerReceipt, BrokerError>;
}
```

- [ ] Implement `ReserveOnce`, `ReplaceVerified`, and `Settle` as one-shot, epoch-checked transactions. Reload under the OS root guard, verify root/policy/owner/control/payload identities, calculate peak bytes/files with checked arithmetic, check configured limits/floor, persist a bounded next snapshot, and return a receipt only after durable commit.
- [ ] Implement `StageDestination::ReplaceVerified` only here. Open the old destination and stage without following reparse points; compare expected old payload hash and file identity; use handle-relative replacement; sync; reopen; compare new identity/hash; retain stage/journal and freeze on uncertainty.
- [ ] Keep the protocol path-free. Map `control_id` deterministically to one private broker-owned component; reject collisions and unexpected entries. The broker never exposes generic create/write/rename/delete RPCs.
- [ ] Startup joins only an exact snapshot-v2 and exact protected root identity. Missing ledger bootstraps with accounted metadata; known stage/journal is reconciled; malformed/legacy/unknown entries produce `STORAGE_LEDGER_RECOVERY_REQUIRED`. No startup branch removes data.
- [ ] Compile before moving Host call sites:

```powershell
cargo check --locked -p codex-storage-broker-windows --lib --bin codex-storage-broker-service -j1
```

Expected: exit `0`; root mutation is reachable only after verified SCM startup and provisioning.

- [ ] Run `git diff --check`, stage only Task 3 files, and commit:

```powershell
git commit -m "feat: make broker the protected root writer"
```

## Task 4: Convert Host storage service to a client with no local fallback

**Files:**
- Modify: `codex-rs/core/Cargo.toml`
- Modify: `codex-rs/core/BUILD.bazel`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_broker.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_service.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs`
- Delete after migration: `codex-rs/core/src/fast_lane_host_dispatch/storage_fs.rs`
- Delete after migration: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Modify: `codex-rs/Cargo.lock`
- Modify: `MODULE.bazel.lock`

- [ ] Add the broker crate dependency. `HostStorageService` keeps validated root-plus-eight configuration and the in-memory policy evaluator, but replaces `RootStorageLedger` with a cached `Result<Arc<BrokerClient>, BrokerError>` frozen per runtime.
- [ ] Preserve the existing `Arc<HostStorageService>` sharing from `ThreadManager` through ordinary and delegated sessions. A session cannot construct a broker, select a pipe/root, replace the cached client, or obtain a session-local writer; all Host processes still converge on the service's single root transaction.
- [ ] `HostStorageService::new` performs no writes and does not start/install the service. Connection remains lazy. Missing config returns `STORAGE_POLICY_MISSING`; configured but missing active record/service returns `STORAGE_BROKER_UNPROVISIONED`; identity/protocol/recovery failures retain their exact stable code.
- [ ] Expose the three path-free client calls using the sealed Host provenance type:

```rust
pub(super) fn reserve_control_once(
    &self,
    config: &Config,
    provenance: &VerifiedControlProvenance,
    payload: &Value,
) -> io::Result<BrokerReceipt>;

pub(super) fn replace_control_payload(
    &self,
    config: &Config,
    provenance: &VerifiedControlProvenance,
    expected_payload_sha256: Digest32,
    payload: &Value,
) -> io::Result<BrokerReceipt>;

pub(super) fn settle_control(
    &self,
    config: &Config,
    provenance: &VerifiedControlProvenance,
    expected_payload_sha256: Digest32,
) -> io::Result<BrokerReceipt>;
```

- [ ] Encode JSON into `BoundedControlPayload` before connecting. Preserve the current unconstructable provenance barrier until Plan 2 `P2-base Task 1` wires verified queue ownership; do not add a public constructor or synthesize owner/queue hashes.
- [ ] Remove both local writer modules from `mod.rs` and the tree after the broker migration is compiled. A search with PowerShell `Select-String` over tracked Rust files must show no remaining Host call to `ApprovedRoot::open`, `RootStorageLedger::open`, `write_stage`, or `publish_stage`.
- [ ] Compile the broker and Host, once:

```powershell
cargo check --locked -p codex-storage-broker-windows --bins -p codex-core --lib -j1
```

Expected: exit `0`; existing unrelated dirty adapter/coordinator files remain unstaged.

- [ ] Run `git diff --check`, explicitly stage Task 4 paths, verify `git diff --cached --name-only`, and commit:

```powershell
git commit -m "refactor: route storage writes through broker"
```

## Task 5: Add explicit administrator and enterprise provisioning source

**Files:**
- Create: `codex-rs/storage-broker-windows/src/provisioning.rs`
- Create: `codex-rs/storage-broker-windows/src/bin/storage_broker_provision.rs`
- Create: `codex-rs/storage-broker-windows/build.rs`
- Create: `codex-rs/storage-broker-windows/codex-storage-broker-provision.manifest`
- Modify: `codex-rs/storage-broker-windows/Cargo.toml`
- Modify: `codex-rs/storage-broker-windows/BUILD.bazel`

- [ ] Add `codex-storage-broker-provision` with only `status`, `provision`, and `repair-status`. Embed `requireAdministrator`; still verify an elevated administrator token at runtime. Do not add auto-provision, uninstall, cleanup, root-reset, force-downgrade, arbitrary service-binary, or arbitrary pipe options.
- [ ] Require one explicit mode:

```text
provision --mode administrator --root <absolute-path> --host-binary <absolute-codex.exe> --confirm-root-id <hex>
provision --mode enterprise-managed
status
repair-status
```

Administrator mode validates the exact displayed root identity confirmation. Enterprise mode reads only `HKLM\SOFTWARE\Policies\2718lab\StorageBroker` values `RootPath`, `AuthorizedHostPath`, and `ConfirmedRootId`; it accepts no path flags.
- [ ] Locate the service binary only as the fixed sibling `codex-storage-broker-service.exe`. Verify its Authenticode publisher matches the provisioner, its embedded release sequence equals `BROKER_RELEASE_SEQUENCE`, and its SHA-256 is stable across copy. Reject an unsigned, user-replaced, reparse-backed, alternate-name, or lower-sequence binary.
- [ ] Resolve Program Files and ProgramData through Known Folder APIs. Install the service binary under `%ProgramFiles%\2718lab\StorageBroker\versions\00000000001001003\`; store `active-v1.json`, `release-floor-v1.json`, and deterministic candidate status under `%ProgramData%\2718lab\StorageBroker\`.
- [ ] Apply and then read back protected DACLs. Version/state locations are owned by SYSTEM; SYSTEM and Administrators have full control, the service SID has required read/execute, ordinary users have read-only access only to the active record/service image needed for Host verification, and no ordinary-user write ACE exists. The root grants full control only to SYSTEM, Administrators, and the service SID and is opened with no-reparse semantics.
- [ ] Create/update SCM with `CreateServiceW`/`ChangeServiceConfigW`, LocalSystem, fixed service name, fixed protected image path, automatic start, and `ChangeServiceConfig2W(SERVICE_CONFIG_SERVICE_SID_INFO, SERVICE_SID_TYPE_UNRESTRICTED)`. Read back every property before switching.
- [ ] Perform the locked provisioning state machine. Health check the candidate service as an elevated maintenance client, publish the active record last with atomic replace, and update the release floor monotonically. On failure restore the old SCM image path/start state and retain the old root/version/record; report candidate evidence without deleting it.
- [ ] `status` is read-only and reports active/candidate versions, hashes, SCM PID/state, service SID type, root identity, DACL verdict, and recovery state. `repair-status` only reconciles/verifies status metadata; it never repairs ledger contents or deletes stages.
- [ ] Compile only; do not run the provisioner:

```powershell
cargo check --locked -p codex-storage-broker-windows --bins -j1
```

Expected: exit `0`; `Get-Service 2718labStorageBroker -ErrorAction SilentlyContinue` is unchanged before and after compilation.

- [ ] Run `git diff --check`, stage only Task 5 files, and commit:

```powershell
git commit -m "feat: add explicit storage broker provisioning"
```

## Task 6: Wire Cargo, Bazel, signing, and package manifests

**Files:**
- Modify: `codex-rs/storage-broker-windows/BUILD.bazel`
- Modify: `codex-rs/Cargo.toml`
- Modify: `codex-rs/Cargo.lock`
- Modify: `MODULE.bazel.lock`
- Modify: `.github/workflows/rust-release-windows.yml`
- Modify: `.github/scripts/build-codex-package-archive.sh`
- Modify: `scripts/codex_package/targets.py`
- Modify: `scripts/codex_package/cargo.py`
- Modify: `scripts/codex_package/cli.py`
- Modify: `scripts/codex_package/layout.py`
- Modify: `scripts/codex_package/test_cargo.py`
- Modify: `scripts/codex_package/test_layout.py`

- [ ] Make `codex_rust_crate` expose library, service, and provisioner targets compatible only with Windows binaries. Mirror the existing sandbox setup manifest resource handling so Cargo and both Bazel Windows ABIs embed the elevation manifest correctly.
- [ ] Add both broker executables to `WINDOWS_BINARIES`, helper build/stage/PDB/sign/verify/symbol lists, and Windows package resource inputs. Do not add them to non-Windows target builds.
- [ ] Extend package types with explicit fields `codex_storage_broker_service_bin` and `codex_storage_broker_provision_bin`; add matching CLI/archive-script flags; source-build them for Windows; copy them to `codex-resources/`; require both during Windows package validation.
- [ ] Preserve the package/install boundary: archive creation only copies signed files. It does not invoke the provisioner, create Program Files/ProgramData directories, call SCM, edit registry, create the root, or change ACLs.
- [ ] Update only the two existing package tests to assert the exact Windows resource list and non-Windows rejection. Do not add a broad packaging matrix.
- [ ] Refresh lock state and compile before focused package assertions:

```powershell
just bazel-lock-update
cargo check --locked -p codex-storage-broker-windows --bins -p codex-core --lib -j1
python -m unittest scripts.codex_package.test_cargo scripts.codex_package.test_layout
bazel build --platforms=//:local_windows_msvc //codex-rs/storage-broker-windows:codex-storage-broker-service //codex-rs/storage-broker-windows:codex-storage-broker-provision
```

Expected: all four commands exit `0`; Python assertions confirm package inclusion only, not provisioning.

- [ ] Run `git diff --check`, explicitly stage Task 6 files, inspect the staged list, and commit:

```powershell
git commit -m "build: package signed storage broker binaries"
```

## Task 7: Add exactly two boundary probes and close the dependency handoff

**Files:**
- Create: `codex-rs/storage-broker-windows/src/protocol_identity_tests.rs`
- Create: `codex-rs/storage-broker-windows/src/root_writer_tests.rs`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_broker_tests.rs`
- Modify: `codex-rs/storage-broker-windows/src/lib.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs`
- Modify: `docs/superpowers/plans/2026-08-29-owned-cleanup-1.1.3.md` in the DevKit repository
- Modify: `docs/superpowers/plans/2026-08-29-storage-firewall-1.1.3.md` in the DevKit repository

- [ ] Implement Probe 1, `unprovisioned_or_spoofed_broker_never_writes`, as one Windows-focused boundary case. It covers: no active record/service; a same-user fake pipe server; wrong service PID/creation; missing Service SID; wrong service binary hash/root identity; oversized/unknown/path-bearing frames. Assert the exact fail-closed code and an unchanged owned fixture root inventory.
- [ ] Implement Probe 2, `uncertain_replace_retains_old_snapshot_and_stage`, with an injected broker filesystem backend. Fail once after stage sync and once at replacement verification; change destination identity before `ReplaceVerified`; assert the old snapshot remains authoritative when known, the stage stays present/accounted, reopening enters recovery when outcome is uncertain, and no delete primitive is called.
- [ ] Keep test seams private and capability-shaped. Do not expose a public fake client, fake identity constructor, path mutation API, or production bypass. Compare whole receipts/snapshots rather than field-by-field assertions.
- [ ] Amend the two existing DevKit plans with this exact dependency order:

```text
storage broker Tasks 1-6
  -> Plan 2 P2-base Task 1 control transactions
  -> Plan 1 Task 6 durable refill/lifecycle wiring
  -> Plan 2 remaining P2-base owner recovery
  -> Plan 2 P2-apply preview and generated cleanup
```

State explicitly that broker completion does not authorize cleanup or live activation. Move Plan 2's root-writer file ownership from `codex-core` to `codex-storage-broker-windows`; keep Host as authenticated client.
- [ ] Compile once, then run only the two named probes:

```powershell
cargo check --locked -p codex-storage-broker-windows --bins -p codex-core --lib -j1
cargo test --locked -p codex-core unprovisioned_or_spoofed_broker_never_writes -j1
cargo test --locked -p codex-storage-broker-windows uncertain_replace_retains_old_snapshot_and_stage -j1
```

Expected: compile exit `0`; each command reports one selected passing test. Do not run a workspace/full suite in this slice.

- [ ] Perform installed-but-unprovisioned acceptance on a disposable package directory, without elevation:

```powershell
$packageRoot='G:\2718lab\_codex\.codex-task-temp\storage-broker-unprovisioned-package'
python scripts/build_codex_package.py --target x86_64-pc-windows-msvc --variant codex --cargo-profile dev --package-dir $packageRoot --force
Test-Path -LiteralPath (Join-Path $packageRoot 'codex-resources\codex-storage-broker-service.exe')
Test-Path -LiteralPath (Join-Path $packageRoot 'codex-resources\codex-storage-broker-provision.exe')
Get-Service 2718labStorageBroker -ErrorAction SilentlyContinue
```

Expected: both `Test-Path` calls are `True`; packaging exits `0`; no new service exists, no active machine record/root is created, and the focused Host probe returns `STORAGE_BROKER_UNPROVISIONED` without writing its fixture root.

- [ ] Run `just fmt` once after all Rust edits. Do not rerun probes after formatting per repository guidance. Run `git diff --check`, stage only Task 7-owned paths, inspect the staged list, and commit Host test work and DevKit plan amendments in their respective repositories:

```powershell
git commit -m "test: prove storage broker fail-closed boundaries"
git commit -m "docs: gate storage lifecycle on protected broker"
```

## Final acceptance gates

- [ ] `codex-storage-broker-windows` is an independent crate and the only compiled owner of write-capable protected-root handles.
- [ ] The SCM service runs as LocalSystem with the exact unrestricted Service SID and fixed local-only pipe; Host and service bind PID plus creation time and exact signed image hashes in both directions.
- [ ] Host verifies protected active record, SCM PID, service token SID, service image/hash, handshake nonces, provisioning generation, and root identity before accepting a receipt.
- [ ] No same-user process, user-writable broker binary, remote pipe, caller path, alternate root, or arbitrary opcode can reach the writer.
- [ ] Multiple ordinary/delegated sessions share one Host client, and multiple Host processes serialize through the broker's one OS-fenced ledger; session identity never becomes root authority.
- [ ] Protocol allocation, request count, payload, concurrency, and I/O time are hard-capped before use.
- [ ] The broker owns `ReserveOnce`, `ReplaceVerified`, and `Settle`; receipt publication follows durable commit; uncertain state freezes and protects stage/recovery data.
- [ ] Missing configuration is disabled. Installed but unprovisioned, unsupported OS, service absent, signature mismatch, downgrade, root mismatch, or recovery state fails closed with no local fallback.
- [ ] Provisioning occurs only through an explicitly elevated administrator command or the fixed enterprise policy registry source. Build, package, install, Host startup, and config load never invoke it.
- [ ] Upgrades reject rollback/downgrade and publish active state last. Every failure retains the old service version, old active record, and old root; no candidate or stage is automatically deleted.
- [ ] Windows release builds sign and package both binaries, while package assembly proves no SCM/ACL/root mutation.
- [ ] Only the two named boundary probes are added/run. Compile-first evidence is recorded separately from live provisioning acceptance.
- [ ] Broker delivery unblocks only Plan 2 `P2-base Task 1`; control lifecycle, Plan 1 Task 6, owner recovery, preview, and generated cleanup remain ordered downstream.

## Plan self-review checklist

- [ ] Every created/modified/deleted path is named above and exists in the mapped Cargo/Bazel/package structure.
- [ ] `BrokerRequest`, `ControlMutation`, `BrokerReceipt`, `ProcessIdentity`, `RootIdentity`, and all constants have one spelling and one definition across tasks.
- [ ] PowerShell `Select-String` over this plan finds none of the forbidden placeholder phrases from the planning skill.
- [ ] No step asks an implementation worker to run provisioning, edit production ACLs, create a live root, delete generated data, or stage unrelated dirty files.
- [ ] `git diff --check` passes in both repositories, and each commit stages explicit named files only.

## Execution handoff

Use subagent-driven execution: one fresh implementation worker per task, then a specification review and a quality review before the next task. Tasks 1-6 are Host-repository commits; Task 7 closes with one Host probe commit and one DevKit dependency-document commit. Stop immediately on compile failure, identity ambiguity, disk-pressure threshold, or any attempted live provisioning/cleanup, and report the exact failing gate without substituting a local writer.
