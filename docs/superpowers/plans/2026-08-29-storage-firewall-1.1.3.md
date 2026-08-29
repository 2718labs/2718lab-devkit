# Storage Firewall 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Cargo, Python, MCP-package, and Fast Lane write begin with one host-approved deterministic generated root and a fail-closed byte/file/free-space reservation.

**Architecture:** DevKit emits a bounded, path-free `StorageIntent` whose hash is bound to the Fast Lane task, source plan, execution context, and project identity. The Codex Host validates the intent, computes the canonical target key, checks the approved root and disk policy, and returns an opaque admission receipt containing the assigned root; the worker never selects `CARGO_TARGET_DIR` or a temporary directory. Lease persistence, cleanup, GitHub source authorization, and session CAS are separate follow-up plans and are not hidden in this admission path.

**Tech Stack:** Python 3.11 standard library (`dataclasses`, `hashlib`, `json`, `pathlib`), MCP FastMCP/Pydantic, Rust 2021, `serde`/`serde_json`, `sha2`, Tokio, platform filesystem-capacity APIs, and the existing authenticated inherited-handle bridge.

---

## Scope and file map

All line references are against commit `37029a9` in the DevKit worktree and
commit `552fe8035d` in the Codex Host worktree. A worker must re-read the
symbol at the listed line before editing because earlier tasks can shift line
numbers.

DevKit files:

- Create `mcp-tools/devkit_runtime/storage_intent.py`: immutable intent and
  canonical target-descriptor validation; it contains no filesystem write and
  no owner claim.
- Modify `mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py:14-415`
  and `mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py:1-318`:
  bind one intent to every compiler assignment and retain it through initial
  and successor waves.
- Modify `mcp-tools/devkit_runtime/fastlane_host_intent.py:333-595`:
  structurally parse the new intent and reject an intent whose binding does not
  equal the assignment's task/context/plan hashes.
- Modify `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677` and
  `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`: add the private
  `storage_admit` request/receipt exchange to the already authenticated bridge.
- Modify `mcp-tools/server.py:1008-1314` only at the authenticated Fast Lane
  dispatch seam so the production path forwards the intent and exposes the
  returned root as a private execution binding; no public path argument is
  added.
- Create `mcp-tools/tests/test_storage_firewall.py` and modify
  `mcp-tools/tests/test_fastlane_host_adapter.py:894-1014` for the smallest
  projection-to-host regression.

Codex Host files:

- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs`: the
  canonical target key, approved-root fence, capacity provider, quota policy,
  admission receipt, and stable errors.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49` to register the
  module and keep its types `pub(crate)`.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/contract.rs:1-842` only in
  the assignment/skeleton projection to carry and hash `storage_intent`.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:1-420`
  and `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:1-310`
  to validate the exact wire payload and its size before it enters core.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-1668` to
  reserve the target-family key before writer preparation and
  `coordinator.rs:393-580,1218-1260` to release the admission on terminal or
  recovery paths.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:1-1444`
  at worker environment construction so the host-issued root is the sole
  `CARGO_TARGET_DIR`/task-temp value.
- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`
  and register it from the new module with `#[path = ...]`.
- Modify `codex-rs/core/Cargo.toml:80-145` by adding
  `[target.'cfg(target_os = "windows")'.dependencies] windows-sys = {
  version = "0.52", features = ["Win32_Storage_FileSystem"] }`, matching the
  existing CLI dependency version; then update `codex-rs/Cargo.lock` and
  `codex-rs/MODULE.bazel.lock` in the same Host commit.

The worker must not edit any file outside this map. The ledger, preview/apply,
source authorization, and session CAS changes belong to Plans 2 and 3.

## Shared wire contract

The following exact JSON shape is the only storage admission payload. The
`target_descriptor` is the sole input to `target_key`; request sizes and root
bindings are policy inputs and are not smuggled into the target-key identity.

```json
{
  "schema": "2718lab.storage.intent.v1",
  "task_id": "task-01",
  "plan_binding": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "context_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "storage_intent_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "requested_bytes": 104857600,
  "requested_files": 4096,
  "target_descriptor": {
    "schema": "2718lab.storage.target.v1",
    "artifact_kind": "cargo-target",
    "repository_identity": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "workspace_manifest_hash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "cargo_lock_hash": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "toolchain_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "target_triple": "x86_64-pc-windows-msvc",
    "profile": "dev",
    "features_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    "build_env_class": "windows-msvc"
  }
}
```

The canonical `target_key` is the SHA-256 of the UTF-8 canonical JSON of the
ten `target_descriptor` fields. The host returns a `StorageAdmissionReceipt`
with `storage_intent_hash`, `target_key`, `assigned_root_identity`,
`target_family_lease_id`, `reserved_bytes`, `reserved_files`,
`free_space_before`, `free_space_after_reserve`, and `free_space_floor`.

## Implementation tasks

### Task 1: Freeze the intent contract with RED tests

**Files:**
- Create: `mcp-tools/tests/test_storage_firewall.py`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`
- Modify: `mcp-tools/tests/test_fastlane_host_adapter.py:894-1014`

- [ ] **Step 1: Add the Python RED fixture.**

```python
def test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key():
    from devkit_runtime.storage_intent import StorageIntentError, parse_storage_intent

    value = {
        "schema": "2718lab.storage.intent.v1",
        "task_id": "task-01",
        "plan_binding": "sha256:" + "a" * 64,
        "context_hash": "sha256:" + "b" * 64,
        "requested_bytes": 1,
        "requested_files": 1,
        "target_descriptor": {
            "schema": "2718lab.storage.target.v1",
            "artifact_kind": "cargo-target",
            "repository_identity": "sha256:" + "c" * 64,
            "workspace_manifest_hash": "sha256:" + "d" * 64,
            "cargo_lock_hash": "sha256:" + "e" * 64,
            "toolchain_digest": "sha256:" + "f" * 64,
            "target_triple": "x86_64-pc-windows-msvc",
            "profile": "dev",
            "features_hash": "sha256:" + "1" * 64,
            "build_env_class": "windows-msvc",
            "path": "G:/unapproved"
        }
    }
    try:
        parse_storage_intent(value)
    except StorageIntentError as error:
        assert error.code == "STORAGE_TARGET_KEY_INVALID"
    else:
        raise AssertionError("invalid descriptor was accepted")
```

- [ ] **Step 2: Add the Rust RED assertions for policy failure.**

```rust
#[test]
fn missing_policy_is_stable_and_does_not_create_a_root() {
    let firewall = StorageFirewall::new(
        PathBuf::from(r"G:\2718lab\_codex\.codex-task-temp"),
        CapacitySnapshot { free_bytes: 8 * GIB, free_files: 1_000_000 },
        None,
    );
    let error = firewall.admit(intent()).expect_err("missing policy must fail closed");
    assert_eq!(error.code(), "STORAGE_POLICY_MISSING");
    assert!(!Path::new(r"G:\2718lab\_codex\.codex-task-temp\targets").exists());
}
```

- [ ] **Step 3: Run only the new RED probes.**

Run from `G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery\mcp-tools`:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Run from `G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs`:

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-rust-target'; cargo test -p codex-core missing_policy_is_stable_and_does_not_create_a_root --locked -j1
```

Expected: both commands fail because the new module/types do not exist;
neither command may create a production target root.

- [ ] **Step 4: Commit the RED tests only.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/tests/test_storage_firewall.py mcp-tools/tests/test_fastlane_host_adapter.py; git commit -m 'test: define storage firewall admission boundary'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs; git commit -m 'test: define storage firewall admission boundary'; Pop-Location
```

### Task 2: Implement canonical Python intents

**Files:**
- Create: `mcp-tools/devkit_runtime/storage_intent.py`
- Modify: `mcp-tools/devkit_runtime/__init__.py:1-18`

- [ ] **Step 1: Define the bounded immutable records and stable error.**

```python
@dataclass(frozen=True, slots=True)
class StorageIntent:
    task_id: str
    plan_binding: str
    context_hash: str
    storage_intent_hash: str
    requested_bytes: int
    requested_files: int
    target_descriptor: Mapping[str, str]


def parse_storage_intent(value: object) -> StorageIntent:
    mapping = _exact_mapping(value, _INTENT_FIELDS)
    descriptor = _exact_mapping(mapping["target_descriptor"], _TARGET_FIELDS)
    task_id = _bounded_identifier(mapping["task_id"], "task_id")
    plan_binding = _digest(mapping["plan_binding"], "plan_binding")
    context_hash = _digest(mapping["context_hash"], "context_hash")
    requested_bytes = _bounded_positive(mapping["requested_bytes"], "requested_bytes")
    requested_files = _bounded_positive(mapping["requested_files"], "requested_files")
    target = _canonical_target(descriptor)
    expected = _sha256_json({"target_descriptor": target, "task_id": task_id, "plan_binding": plan_binding, "context_hash": context_hash, "requested_bytes": requested_bytes, "requested_files": requested_files})
    storage_intent_hash = _digest(mapping["storage_intent_hash"], "storage_intent_hash")
    if expected != storage_intent_hash:
        raise StorageIntentError("STORAGE_TARGET_KEY_INVALID")
    return StorageIntent(task_id, plan_binding, context_hash, storage_intent_hash, requested_bytes, requested_files, target)
```

The implementation must reject absolute paths, reparse-point hints, unknown
keys, booleans used as integers, zero/overflow values, non-lowercase digests,
and artifact kinds outside `cargo-target`, `python-cache`, `mcp-package`, and
`fastlane-task`. It must use `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` before hashing.

- [ ] **Step 2: Export only the parser and record.**

```python
from .storage_intent import StorageIntent, StorageIntentError, parse_storage_intent

__all__ = ["StorageIntent", "StorageIntentError", "parse_storage_intent"]
```

- [ ] **Step 3: Turn the Python RED green and compile the changed modules.**

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
python -m py_compile devkit_runtime/storage_intent.py devkit_runtime/__init__.py
```

Expected: `1 passed`, then a successful compile with no output.

- [ ] **Step 4: Commit the Python contract.**

```powershell
git add mcp-tools/devkit_runtime/storage_intent.py mcp-tools/devkit_runtime/__init__.py
git commit -m "feat: add canonical storage intent"
```

### Task 3: Bind intents to every Fast Lane assignment

**Files:**
- Modify: `mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py:124-333`
- Modify: `mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py:186-284`
- Modify: `mcp-tools/devkit_runtime/fastlane_host_intent.py:333-595`
- Test: `mcp-tools/tests/test_storage_firewall.py`

- [ ] **Step 1: Add a RED assertion that initial and successor assignments carry the same exact intent binding.**

```python
def test_every_fastlane_wave_carries_plan_context_bound_storage_intent():
    from devkit_fastlane.scripts.authenticated_v5_planner import compile_skeletons

    first, successor = compile_skeletons(SOURCE_UNITS, source_plan_hash=PLAN_HASH, context=CONTEXT)
    assert first[0]["storage_intent"]["task_id"] == first[0]["task_id"]
    assert successor[0]["storage_intent"]["plan_binding"] == PLAN_HASH
    assert successor[0]["storage_intent"]["context_hash"] == CONTEXT["execution_context_hash"]
```

- [ ] **Step 2: Add `storage_intent` to the exact planner/projection field sets and construct it from normalized assignment data.**

```python
intent = make_storage_intent(
    task_id=unit["task_id"],
    plan_binding=source_hash,
    context_hash=context["execution_context_hash"],
    artifact_kind="fastlane-task",
    repository_identity=context["repository_identity"],
    workspace_manifest_hash=context["workspace_manifest_hash"],
    cargo_lock_hash=context["cargo_lock_hash"],
    toolchain_digest=context["toolchain_digest"],
    target_triple=context["target_triple"],
    profile=context["profile"],
    features_hash=context["features_hash"],
    build_env_class=context["build_env_class"],
    requested_bytes=unit["storage_budget"]["bytes"],
    requested_files=unit["storage_budget"]["files"],
)
assignment["storage_intent"] = intent
```

The compiler may not synthesize a default budget. A source unit without the
two positive budget values fails with `STORAGE_POLICY_MISSING` during compile;
it never gets a guessed path or a guessed owner. The projection hash must
include the complete `storage_intent` object before `dispatch_binding_hash` is
computed.

- [ ] **Step 3: Parse and compare the binding at the host-intent boundary.**

```python
parsed = parse_storage_intent(candidate["storage_intent"])
if parsed.task_id != candidate["task_id"]:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
if parsed.plan_binding != source_plan_hash:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
if parsed.context_hash != candidate["execution_context_hash"]:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
```

- [ ] **Step 4: Run the focused RED/GREEN probe and Python compile gate.**

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_every_fastlane_wave_carries_plan_context_bound_storage_intent -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
python -m py_compile devkit_fastlane/scripts/authenticated_v5_planner.py devkit_fastlane/scripts/authenticated_v5_projection.py devkit_runtime/fastlane_host_intent.py
```

Expected: `1 passed` and a successful compile. Do not run the broad Fast Lane
matrix in this plan.

- [ ] **Step 5: Commit the binding change.**

```powershell
git add mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py mcp-tools/devkit_runtime/fastlane_host_intent.py mcp-tools/tests/test_storage_firewall.py
git commit -m "feat: bind storage intents to fast lane waves"
```

### Task 4: Add the authenticated bridge exchange

**Files:**
- Modify: `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677`
- Modify: `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:25-220`
- Test: `mcp-tools/tests/test_storage_firewall.py`
- Test: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol_tests.rs`

- [ ] **Step 1: Add the wire RED test for exact operation fields and replay identity.**

```python
def test_storage_admission_request_is_session_bound_and_replay_stable():
    request = build_storage_admission_request(INTENT)
    assert request["schema"] == "2718lab.storage.admission-request.v1"
    assert set(request) == {"schema", "correlation_id", "storage_intent", "request_hash"}
    assert request["request_hash"] == canonical_hash({key: request[key] for key in request if key != "request_hash"})
```

- [ ] **Step 2: Implement exact validation on both sides.**

```rust
pub(crate) const STORAGE_ADMISSION_REQUEST_SCHEMA: &str =
    "2718lab.storage.admission-request.v1";
pub(crate) const STORAGE_ADMISSION_RECEIPT_SCHEMA: &str =
    "2718lab.storage.admission-receipt.v1";

fn validate_storage_admission(payload: &Map<String, Value>) -> Result<StorageAdmissionRequest, HostBridgeProtocolError> {
    require_exact_fields(payload, &["schema", "correlation_id", "storage_intent", "request_hash"])?;
    if value_str(payload, "schema")? != STORAGE_ADMISSION_REQUEST_SCHEMA {
        return Err(HostBridgeProtocolError::InvalidOperation);
    }
    let intent = validate_storage_intent(value_object(payload, "storage_intent")?)?;
    let request_hash = strict_digest(value_str(payload, "request_hash")?)?;
    if digest(&canonical_bytes(&without_field(payload, "request_hash")?)?) != request_hash {
        return Err(HostBridgeProtocolError::InvalidOperation);
    }
    Ok(StorageAdmissionRequest::new(value_str(payload, "correlation_id")?.into(), intent, request_hash))
}
```

The Python and Rust validators must reject different sessions, duplicate
correlation IDs, frame payloads above the existing `MAX_OPERATION_BYTES`,
unknown fields, and a receipt whose `storage_intent_hash` or `target_key` does
not match the request. No public MCP response may include the absolute root.

- [ ] **Step 3: Add `HostSession.request_storage_admission(intent)` and its typed receipt.**

```python
def request_storage_admission(self, intent: StorageIntent) -> StorageAdmissionReceipt | str:
    request = build_storage_admission_request(intent)
    response = self._bridge.request_storage_admission(request)
    if not isinstance(response, StorageAdmissionReceipt):
        return "STORAGE_STAT_UNAVAILABLE"
    return response
```

The Rust session routes this message through the existing single writer queue;
it must not add a second receiver for the same bridge direction.

- [ ] **Step 4: Run the bridge-focused probes and compile gates.**

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_admission_request_is_session_bound_and_replay_stable -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-rust-target'; cargo test -p codex-rmcp-client storage_admission --locked -j1
```

Expected: Python `1 passed`; Rust storage protocol tests pass with no new
target root outside the named task target.

- [ ] **Step 5: Commit the protocol slice.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/tests/test_storage_firewall.py; git commit -m 'feat: carry storage admission over host bridge'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol_tests.rs; git commit -m 'feat: carry storage admission over host bridge'; Pop-Location
```

### Task 5: Implement host target-key and capacity admission

**Files:**
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49`
- Modify: `codex-rs/core/Cargo.toml:80-145`
- Modify: `codex-rs/Cargo.lock` and `codex-rs/MODULE.bazel.lock`

- [ ] **Step 1: Add the Rust RED target-key vectors.**

```rust
#[test]
fn target_key_reuses_only_identical_build_semantics() {
    let first = target_key(&descriptor("dev", "sha256:".to_owned() + &"1".repeat(64))).unwrap();
    let same = target_key(&descriptor("dev", "sha256:".to_owned() + &"1".repeat(64))).unwrap();
    let profile = target_key(&descriptor("release", "sha256:".to_owned() + &"1".repeat(64))).unwrap();
    let features = target_key(&descriptor("dev", "sha256:".to_owned() + &"2".repeat(64))).unwrap();
    assert_eq!(first, same);
    assert_ne!(first, profile);
    assert_ne!(first, features);
}
```

- [ ] **Step 2: Implement the exact policy and deterministic root mapping.**

```rust
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct StoragePolicy {
    pub(crate) task_byte_limit: u64,
    pub(crate) task_file_limit: u64,
    pub(crate) target_family_byte_limit: u64,
    pub(crate) target_family_file_limit: u64,
    pub(crate) global_reserved_byte_limit: u64,
    pub(crate) global_reserved_file_limit: u64,
    pub(crate) free_space_floor_bytes: u64,
    pub(crate) emergency_floor_bytes: u64,
}

pub(crate) fn admit(&self, intent: &StorageIntent) -> Result<StorageAdmissionReceipt, StorageError> {
    let policy = self.policy.as_ref().ok_or(StorageError::PolicyMissing)?;
    let free_before = self.capacity.free_bytes().map_err(|_| StorageError::StatUnavailable)?;
    if intent.requested_bytes > policy.task_byte_limit {
        return Err(StorageError::QuotaExceeded);
    }
    if intent.requested_files > policy.task_file_limit {
        return Err(StorageError::FileLimitExceeded);
    }
    if self.family_observed_bytes + self.family_reserved_bytes + intent.requested_bytes
        > policy.target_family_byte_limit
    {
        return Err(StorageError::QuotaExceeded);
    }
    if self.family_observed_files + self.family_reserved_files + intent.requested_files
        > policy.target_family_file_limit
    {
        return Err(StorageError::FileLimitExceeded);
    }
    if self.global_reserved_bytes + intent.requested_bytes > policy.global_reserved_byte_limit
        || self.global_reserved_files + intent.requested_files > policy.global_reserved_file_limit
    {
        return Err(StorageError::QuotaExceeded);
    }
    if free_before < policy.free_space_floor_bytes
        || free_before - (self.global_reserved_bytes + intent.requested_bytes)
            < policy.free_space_floor_bytes
    {
        return Err(StorageError::FreeSpaceFloor);
    }
    let target_key = target_key(&intent.target_descriptor)?;
    let assigned_root = self.approved_root.join("generated").join(&target_key[7..]);
    verify_strict_child(&self.approved_root, &assigned_root)?;
    Ok(self.reserve(target_key, assigned_root, free_before, intent, policy))
}
```

`StorageError::code()` must return exactly one of the Plan 1 codes
`STORAGE_ROOT_NOT_APPROVED`, `STORAGE_TARGET_KEY_INVALID`,
`STORAGE_POLICY_MISSING`, `STORAGE_QUOTA_EXCEEDED`,
`STORAGE_FILE_LIMIT_EXCEEDED`, `STORAGE_FREE_SPACE_FLOOR`, and
`STORAGE_STAT_UNAVAILABLE`. A missing/overflowed policy field is never treated
as zero and never treated as unlimited.

- [ ] **Step 3: Implement platform capacity providers using the existing CLI doctor implementation as the reference.**

On Unix call `libc::statvfs`; on Windows call
`GetDiskFreeSpaceExW`; on unsupported platforms return
`STORAGE_STAT_UNAVAILABLE`. The provider is injected as a trait in tests so
tests do not query the real G drive. `StorageFirewall::new` must not create the
approved root or target directory during construction or failed admission.

- [ ] **Step 4: Run only the target-key and admission probes.**

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-rust-target'; cargo test -p codex-core target_key_reuses_only_identical_build_semantics --locked -j1
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-rust-target'; cargo test -p codex-core missing_policy_is_stable_and_does_not_create_a_root --locked -j1
```

Expected: both tests pass. If free-space statistics fail, the command must
stop with the stable error and must not create a second Cargo target.

- [ ] **Step 5: Commit the host firewall.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs codex-rs/core/src/fast_lane_host_dispatch/mod.rs codex-rs/core/Cargo.toml codex-rs/Cargo.lock codex-rs/MODULE.bazel.lock; git commit -m 'feat: enforce deterministic storage admission'; Pop-Location
```

### Task 6: Connect admission to preparation, worker environment, and terminal release

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-1668`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs:393-580,1218-1260`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:1-1444`
- Modify: `mcp-tools/server.py:1008-1314`
- Test: `mcp-tools/tests/test_storage_firewall.py`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`

- [ ] **Step 1: Add a RED production-path assertion that an admitted root is present only in host-owned worker facts.**

```python
def test_fastlane_dispatch_forwards_receipt_identity_without_public_path():
    result = dispatch_fixture_with_storage_intent()
    assert result["storage_admission"]["assigned_root_identity"].startswith("sha256:")
    assert "assigned_root" not in result
    assert "CARGO_TARGET_DIR" not in result
```

- [ ] **Step 2: Reserve before `prepare_batch`, inject after reservation, and release on every terminal/recovery branch.**

```rust
let admission = self.storage.admit(&assignment.storage_intent)?;
let prepared = self.prepare_worktrees(&batch, &admission)?;
let mut environment = self.worker_environment(&prepared);
environment.insert("CARGO_TARGET_DIR".into(), admission.assigned_root().display().to_string());
environment.insert("CODEX_TASK_TEMP".into(), admission.assigned_temp_root().display().to_string());
```

The `assigned_root` and `assigned_temp_root` values remain in a private
`HostWriterContext`; only their identity hashes enter public receipts. On
`dispatch_all` error, terminal quarantine, successful integration, and
`recover_batch`, call `StorageFirewall::release` exactly once with the lease
ID. A release failure returns `STORAGE_POSTCHECK_FAILED` and retains the
ledger/lease state for Plan 2 recovery; it never triggers a broad delete.

- [ ] **Step 3: Make the DevKit Fast Lane result expose only stable storage code and receipt identity.**

```python
public = {
    "storage_code": receipt.code,
    "storage_receipt_hash": receipt.receipt_hash,
    "target_key": receipt.target_key,
}
```

- [ ] **Step 4: Run the one production-path probe plus compile-first gates.**

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_fastlane_dispatch_forwards_receipt_identity_without_public_path -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
python -m py_compile server.py devkit_runtime/host_bridge.py devkit_runtime/host_session.py
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-rust-target'; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: Python probe passes, `py_compile` is silent, and `cargo check`
finishes successfully with no new warning. This is the compile-first gate for
Plan 1; do not run the full workspace suite.

- [ ] **Step 5: Commit the production wiring.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/server.py mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/tests/test_storage_firewall.py; git commit -m 'feat: bind admitted storage roots to fast lane workers'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/registry.rs codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs; git commit -m 'feat: bind admitted storage roots to fast lane workers'; Pop-Location
```

## Plan 1 acceptance gate and handoff

- [ ] Re-read the design sections “确定性 target key 与数据流” and “配额、文件数与剩余空间门槛”; verify every field and every fail-closed code is represented by a task above.
- [ ] Run `git diff --check` in both worktrees and verify only the mapped files changed.
- [ ] Run `python -m py_compile` on every changed DevKit Python file and `cargo check -p codex-core --lib --locked -j1` with the one named task target.
- [ ] Record one controlled admission receipt proving same semantics reuse one target key and one changed semantic forks it; record one low-space/policy-failure receipt proving no directory was created.
- [ ] Do not implement ledger persistence, preview/apply, source deletion, session deletion, compression, or remote synchronization in this plan. Plan 2 consumes `StorageAdmissionReceipt`; Plan 3 consumes the released/observed storage records.
