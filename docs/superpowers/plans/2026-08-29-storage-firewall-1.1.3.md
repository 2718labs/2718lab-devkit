# Storage Firewall 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Cargo, Python, MCP-package, and Fast Lane write begin with one host-approved deterministic generated root and a fail-closed byte/file/free-space reservation.

**Architecture:** DevKit emits a bounded, path-free `StorageIntent` whose hash is bound to the Fast Lane task, source plan, execution context, and project identity. The Codex Host resolves an already-issued profile, validates the intent, and reserves a deterministic target family for a Host-owned wave with per-task member grants. Receipts contain identities only; private worker facts carry paths. Existing route/lease batch hashes remain unchanged. Lease persistence, cleanup, GitHub source authorization, and session CAS are separate follow-up plans.

**Tech Stack:** Python 3.11 standard library (`dataclasses`, `hashlib`, `json`, `pathlib`), MCP FastMCP/Pydantic, Rust 2021, `serde`/`serde_json`, `sha2`, Tokio, platform filesystem-capacity APIs, and the existing authenticated inherited-handle bridge.

**Revision status (2026-08-30):** This is an unpublished protocol revision made
after the existing Task 4a slice passed its compile checks. That result does
not mean admission, group reservations, or successor production wiring below
is implemented. The unpublished admission-v1 request changes from exact4 to
exact5; intent/target/profile-v1 stay unchanged. If exact4 has been deployed
outside these worktrees, use admission-v2 instead and reject downgrade.

**Compile-first execution:** Reuse the Host's existing
`G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target`,
set `CARGO_INCREMENTAL=0`, and use `--locked -j1`. Check the changed crates
before running a small relevant probe. Do not recreate RED failures from
completed slices, repeatedly run full suites, or create a storage-specific
Cargo target. Unchecked tasks below are implementation/acceptance work, not
claims that the revised contract is complete.

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
- Modify `mcp-tools/devkit_runtime/__init__.py` only for the intent exports.
- Modify `mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py:14-415`
  and `mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py:1-318`:
  retain explicit budgets through initial and successor waves; construct
  initial intents only from verified Host profiles, not caller facts.
- Modify `mcp-tools/devkit_runtime/fastlane_host_adapter.py`: forward initial
  intent/profile references without rewriting attested route/lease facts.
- Modify `mcp-tools/devkit_runtime/fastlane_host_intent.py:333-595`:
  structurally parse the new intent and reject an intent whose binding does not
  equal the assignment's task/context/plan hashes.
- Modify `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677` and
  `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`: add the private
  `storage_admit` request/receipt exchange to the already authenticated bridge.
- Modify `mcp-tools/server.py:1008-1314` only at the authenticated Fast Lane
  dispatch seam so the production path forwards intent/profile references
  and returns receipt identities; paths never cross into the DevKit response.
- Create `mcp-tools/tests/test_storage_firewall.py` and modify
  `mcp-tools/tests/test_fastlane_host_adapter.py:894-1014` for the smallest
  projection-to-host regression.

Codex Host files:

- Create `codex-rs/rmcp-client/src/storage_intent.rs` for the single strict
  Rust intent/target codec and export it from `codex-rs/rmcp-client/src/lib.rs`.
  Reuse rmcp-client's existing `sha2`/`serde_json`; do not add a protocol-crate
  dependency or make rmcp-client depend on core.
- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs`: the
  canonical target key, approved-root fence, capacity provider, quota policy,
  admission receipt, and stable errors. Re-export the rmcp-client intent type
  here for the existing RED imports; do not create a second Rust codec.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49` to register the
  module and keep its types `pub(crate)`.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/contract.rs:1-842` to define
  the separate Host-owned `StorageAssignmentBinding`; do not append storage
  fields to, or rehash, an already-attested route/lease assignment or batch.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:1-420`
  and `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:1-310`
  to validate the exact wire payload and its size before it enters core.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs`
  and `pump.rs` for session-local completed-profile lookup and admission I/O;
  modify `codex-rs/core/src/fast_lane_host_dispatch/receiver.rs` to record
  successful profile transmission before accepting admission.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/storage_profile.rs` only
  to share Host-derived profile construction with selected-wave authority;
  successor materialization must not fabricate a bridge request.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-1668` to
  reserve the target-family key before writer preparation and
  `coordinator.rs:393-580,1218-1260` to release the admission on terminal or
  recovery paths.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:1-1444`
  at worker environment construction so the host-issued root is the sole
  `CARGO_TARGET_DIR`/task-temp value.
- Modify the existing `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`;
  preserve its sibling-module registration in `mod.rs`.
- Modify `codex-rs/core/Cargo.toml:80-145` by adding
  `[target.'cfg(target_os = "windows")'.dependencies] windows-sys = {
  version = "0.52", features = ["Win32_Storage_FileSystem"] }`, matching the
  existing CLI dependency version; then update `codex-rs/Cargo.lock` and
  `MODULE.bazel.lock` at the Host repository root in the same Host commit
  when dependency changes require them.

The worker must not edit any file outside this map. The ledger, preview/apply,
source authorization, and session CAS changes belong to Plans 2 and 3.

## Shared wire contract

The following exact JSON shape is the storage intent, nested in the Task 4
admission request. The
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
ten `target_descriptor` fields. It never includes task/wave/member identifiers.
The exact successful `StorageAdmissionReceipt` decision has these fields:

`schema`, `admission_id`, `profile_attestation_hash`, `storage_intent_hash`,
`storage_binding_hash`, `target_key`, `assigned_root_identity`,
`target_family_lease_id`, `reserved_bytes`, `reserved_files`,
`free_space_before`, `free_space_after_reserve`, `free_space_floor`,
`expires_at`, `receipt_hash`.

Its schema is `2718lab.storage.admission-receipt.v1`; `receipt_hash` hashes
all decision fields except itself. The successful private response envelope
is exact `{schema, correlation_id, request_hash, receipt}` with schema
`2718lab.storage.admission-response.v1`. A failure uses the existing bounded
transport error path with a stable code, never a fabricated zero reservation.
The internal transport-completion receipt separately records response hash,
session binding, bridge generation, expiry, and completion time. It is proof
of transmission, not a second capacity reservation or a public path carrier.

## Implementation tasks

### Task 1: Freeze the intent contract with RED tests

**Files:**
- Create: `mcp-tools/tests/test_storage_firewall.py`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`
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
    let approved_root = tempfile::tempdir().unwrap();
    let firewall = StorageFirewall::new(
        approved_root.path().to_path_buf(),
        CapacitySnapshot { free_bytes: 8 * GIB, free_files: 1_000_000 },
        None,
    );
    let error = firewall.admit(intent()).expect_err("missing policy must fail closed");
    assert_eq!(error.code(), "STORAGE_POLICY_MISSING");
    assert!(!approved_root.path().join("generated").exists());
}
```

- [ ] **Step 3: Keep the existing RED evidence; run a new narrow probe only after compilation.**

Run from `G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery\mcp-tools`:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Run from `G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs`:

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-core --lib --locked -j1
cargo test -p codex-core missing_policy_is_stable_and_does_not_create_a_root --locked -j1
```

Historical missing-module RED is not a reason to rerun a failed build. Stop
if the compile gate fails; after implementation, run only the selected
boundary probes. Neither probe may create a production generated root.

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

- [ ] **Step 3: Compile changed modules, then run the Python boundary probe once.**

```powershell
python -m py_compile devkit_runtime/storage_intent.py devkit_runtime/__init__.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Expected: successful compile with no output, then `1 passed`.

- [ ] **Step 4: Commit the Python contract.**

```powershell
git add mcp-tools/devkit_runtime/storage_intent.py mcp-tools/devkit_runtime/__init__.py
git commit -m "feat: add canonical storage intent"
```

### Task 3: Bind storage alongside immutable Fast Lane assignments

**Files:**
- Modify: `mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py:124-333`
- Modify: `mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py:186-284`
- Modify: `mcp-tools/devkit_runtime/fastlane_host_intent.py:333-595`
- Modify: `mcp-tools/devkit_runtime/fastlane_host_adapter.py`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/contract.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs`
- Test: `mcp-tools/tests/test_storage_firewall.py`

- [ ] **Step 1: Keep one focused binding regression.**

The initial case constructs intents from real Host profile fixtures and
explicit budgets, rejects changed task/plan/context/profile facts, and asserts
that original assignment and batch hashes are unchanged. The successor case
belongs at Rust `consume_refill_queue`: Python does not materialize successor
assignments. Until that path is wired, budgeted remaining work must fail closed
before dispatch; do not move `all_units` into the initial capacity/lease wave.

- [ ] **Step 2: Construct initial intents from verified profiles and retain explicit successor budgets.**

Use the existing `_storage_intents_for_profiles` path in
`fastlane_host_adapter.py`; the public caller supplies requested budgets, not
repository/toolchain/context facts. Missing positive budgets fail with
`STORAGE_POLICY_MISSING`, never a default. Retain remaining budgets keyed by
task in the exact refill request/ledger and include them in its canonical
binding; Host checks exact remaining-task coverage and verified index refs.
Registration does not reserve storage for unselected successors.

`claim_evidence` already registers runtime, `by_batch`, `batch_intents`, and
scope leases using the original route/lease batch hash before profile exchange.
Keep those hashes and their preimages unchanged. Define a separate Host-owned
`StorageAssignmentBinding` whose canonical proof binds original batch hash,
task ID, verified profile provenance, storage intent hash, admission ID, and
family lease ID. For initial work the provenance references the completed
profile attestation; for successors it references native selected-wave
authority. Store the proof and private member grant in Host runtime facts;
the digest alone is not authority. Do not append storage and recompute the
original `dispatch_binding_hash` or `batch_hash`.

- [ ] **Step 3: Parse and compare the binding at the host-intent boundary.**

```python
parsed = parse_storage_intent(storage_candidate)
if parsed.task_id != verified_profile["task_id"]:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
if parsed.plan_binding != source_plan_hash:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
if parsed.context_hash != verified_profile["execution_context_hash"]:
    raise ValueError("STORAGE_TARGET_KEY_INVALID")
```

- [ ] **Step 4: Compile changed Python modules, then run the focused binding probe once.**

```powershell
python -m py_compile devkit_fastlane/scripts/authenticated_v5_planner.py devkit_fastlane/scripts/authenticated_v5_projection.py devkit_runtime/fastlane_host_intent.py devkit_runtime/fastlane_host_adapter.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py -k storage_binding -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Expected: successful compile and the selected binding regression passes.
An empty test selection is not a pass. Do not run the broad Fast Lane matrix.

- [ ] **Step 5: Commit the binding change.**

```powershell
git add mcp-tools/devkit_fastlane/scripts/authenticated_v5_planner.py mcp-tools/devkit_fastlane/scripts/authenticated_v5_projection.py mcp-tools/devkit_runtime/fastlane_host_intent.py mcp-tools/devkit_runtime/fastlane_host_adapter.py mcp-tools/tests/test_storage_firewall.py
git commit -m "feat: bind storage intents to fast lane waves"
```

Commit Host binding/refill changes with the Task 6 Host slice; do not treat
the Python commit as complete successor production wiring.

### Task 4: Add the authenticated bridge exchange

**Files:**
- Modify: `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677`
- Modify: `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:25-220`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/pump.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/receiver.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs`
- Test: `mcp-tools/tests/test_storage_firewall.py`
- Test: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol_tests.rs`

- [ ] **Step 1: Add the wire RED test for exact operation fields and replay identity.**

```python
def test_storage_admission_request_is_session_bound_and_replay_stable():
    request = build_storage_admission_request(INTENT, profile_attestation_hash=PROFILE_ATTESTATION_HASH)
    assert request["schema"] == "2718lab.storage.admission-request.v1"
    assert set(request) == {"schema", "correlation_id", "profile_attestation_hash", "storage_intent", "request_hash"}
    assert request["request_hash"] == canonical_hash({key: request[key] for key in request if key != "request_hash"})
```

- [ ] **Step 2: Implement exact validation on both sides.**

The exact request is `{schema, correlation_id, profile_attestation_hash,
storage_intent, request_hash}` with schema
`2718lab.storage.admission-request.v1`. `request_hash` hashes all fields
except itself. Frame `action_id` must equal `correlation_id`; correlation is
only a reply/replay key and must never be parsed or guessed into an owner.

After authenticated frame verification, resolve `profile_attestation_hash`
only in this `SessionCore`'s completed-profile table. Extend the existing
`StorageProfileCompletion` beyond preparation/task/expiry to retain the full
verified profile binding and response identity. A digest supplied by the
caller, or a successful recomputation of an attestation hash, is not evidence
that Host issued or sent that profile. Cross-session references fail closed.

Core `claim_storage_profile` must retain an `IssuedStorageProfile` record:
call intent, preparation, task, source plan, index attestation, full profile,
profile/request/attestation hashes, original runtime batch, generation,
expiry, and `Issued`/`Sent` state. After the existing receiver obtains the
actual profile transport receipt, `mark_storage_profile_sent` validates it
against that record and marks `Sent`. Only then may `claim_storage_admission`
accept the bridge-resolved reference. Do not reinterpret the existing
request-specific `session_binding_hash` as a universal session identifier.

Admission rechecks core's `Sent` record, the current preparation and verified
index binding, and exact intent task/plan/context/descriptor equality. The
caller can request bytes/files, but cannot mint profile facts, paths, a group
owner, generation, or expiry. The Host-derived deadline is the minimum of
transport, issued profile, preparation, and index authority deadlines; the
pending record, response, and completion receipt use that same deadline.
Use the shared rmcp-client intent codec and then construct the private-field
`HostStorageAdmissionRequest` from the resolved binding, not from JSON alone.

The Python and Rust validators must reject different sessions, duplicate
correlation IDs, frame payloads above the existing `MAX_OPERATION_BYTES`,
unknown fields, and a receipt whose `storage_intent_hash` or `target_key` does
not match the request. No public MCP response may include the absolute root.

- [ ] **Step 3: Add the bound HostSession call and separated decision/transport receipts.**

```python
def request_storage_admission(self, intent: StorageIntent, *, profile_attestation_hash: str) -> StorageAdmissionReceipt | str:
    request = build_storage_admission_request(intent, profile_attestation_hash=profile_attestation_hash)
    response = self._bridge.request_storage_admission(request)
    if not isinstance(response, StorageAdmissionReceipt):
        return "STORAGE_STAT_UNAVAILABLE"
    return response
```

The Rust session routes this message through the existing single writer queue;
it must not add a second receiver for the same bridge direction. Return the
decision and response envelope defined in the shared wire contract; retain
transport completion separately. A new correlation for the same verified
member/intent may retrieve the same decision, never reserve again. Exact
correlation replay remains rejected. A changed intent for an existing member
is a conflict, not a budget update.

- [ ] **Step 4: Run compile gates first, then the bridge-focused probes once.**

```powershell
python -m py_compile devkit_runtime/host_bridge.py devkit_runtime/host_session.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_storage_admission_request_is_session_bound_and_replay_stable -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1
cargo test -p codex-rmcp-client storage_admission --locked -j1
Pop-Location
```

Run the selected tests only after their compile gate succeeds. Expected:
Python `1 passed`; selected Rust protocol tests pass using the existing Host
target. Do not turn this slice into a full-suite run.

- [ ] **Step 5: Commit the protocol slice.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/tests/test_storage_firewall.py; git commit -m 'feat: carry storage admission over host bridge'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/pump.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol_tests.rs codex-rs/core/src/fast_lane_host_dispatch/receiver.rs codex-rs/core/src/fast_lane_host_dispatch/registry.rs; git commit -m 'feat: carry storage admission over host bridge'; Pop-Location
```

### Task 5: Implement host target-key and capacity admission

**Files:**
- Create: `codex-rs/rmcp-client/src/storage_intent.rs`
- Modify: `codex-rs/rmcp-client/src/lib.rs`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49`
- Modify: `codex-rs/core/Cargo.toml:80-145`
- Modify if dependencies change: `codex-rs/Cargo.lock` and Host-root `MODULE.bazel.lock`

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

- [ ] **Step 2: Implement the shared codec, exact policy, and Host-only group/member admission.**

`rmcp-client/src/storage_intent.rs` owns the one strict Rust `StorageIntent`
and target codec, using the crate's existing `sha2`/`serde_json`. Export from
`lib.rs`; core `storage_firewall.rs` re-exports the intent type to preserve
the existing RED struct literals/imports. Admission protocol and firewall
must use this same codec; no duplicate validator or reversed core dependency.

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
```

Define `StorageAdmissionAuthority`, `StorageGroupOwner`, and member grants as
Host-only records, never deserializable caller claims. The initial owner is
the verified pending runtime batch; a successor owner is the Host-created
selected-wave attempt. `reserve_member_once` accepts validated authority and
intent; `seal_group(expected_tasks)` requires the exact Host task set, with no
missing/extra/duplicate member, before any writer may prepare.

For each new member, check positive policy/budget values and task limits, then
family observed + reserved + requested bytes/files, global reserved + requested
bytes/files, and free bytes minus global reserved/requested against the floor.
Use checked arithmetic throughout; any overflow or unavailable measurement
fails closed. Below the emergency floor, do not admit new writers. Serialize
reservation updates under one state lock and count each member budget once.

The same owner and target key share one family lease. An identical member and
intent returns its existing grant without charging again; a changed intent
conflicts. A different owner conflicts while the lease remains held. Sum
member budgets rather than reserving an entire family again per assignment.
This permits scope-disjoint writers in the same wave instead of making the
second same-key assignment permanently `NO_SAFE_WORK`.

Map the family to `approved_root/generated/<target_key digest>` using strict
child/reparse checks. Member scratch directories are separate Host-assigned
children; the Cargo cache remains one shared canonical target root. Do not
put task/member suffixes in `target_key` or duplicate the Cargo cache. Shared
cache writes require the supported Cargo locking contract; other outputs
must remain in disjoint member grants. Family ownership survives until the
last member's confirmed shutdown and successful postcheck; cloning a grant
or replaying a request neither reserves nor releases capacity. Admission
expiry is a deadline for starting/attaching a grant, not permission to release
a running member without confirmed shutdown and postcheck.

`StorageError::code()` must include the Plan 1 admission codes
`STORAGE_ROOT_NOT_APPROVED`, `STORAGE_TARGET_KEY_INVALID`,
`STORAGE_POLICY_MISSING`, `STORAGE_QUOTA_EXCEEDED`,
`STORAGE_FILE_LIMIT_EXCEEDED`, `STORAGE_FREE_SPACE_FLOOR`, and
`STORAGE_STAT_UNAVAILABLE`, plus `STORAGE_LEASE_CONFLICT` for a different owner
or changed member intent and `STORAGE_POSTCHECK_FAILED` for retained ownership
after failed postcheck. A missing/overflowed policy field is never treated as
zero or unlimited. Preserve the existing missing-policy `admit(intent())`
test seam, but production admission requires verified Host authority.

- [ ] **Step 3: Implement platform capacity providers using the existing CLI doctor implementation as the reference.**

On Unix call `libc::statvfs`; on Windows call
`GetDiskFreeSpaceExW`; on unsupported platforms return
`STORAGE_STAT_UNAVAILABLE`. The provider is injected as a trait in tests so
tests do not query the real G drive. `StorageFirewall::new` must not create the
approved root or target directory during construction or failed admission.

- [ ] **Step 4: Compile both changed crates, then run only selected boundary probes once.**

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1
cargo test -p codex-core target_key_reuses_only_identical_build_semantics --locked -j1
cargo test -p codex-core missing_policy_is_stable_and_does_not_create_a_root --locked -j1
```

Stop on compile failure. Expected afterward: both selected tests pass. Keep
one small same-owner sharing/cross-owner conflict/seal regression with the
kernel, not a broad matrix. If space or statistics are unavailable, stop;
do not create a second Cargo target or repeatedly rerun the suite.

- [ ] **Step 5: Commit the host firewall.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rmcp-client/src/storage_intent.rs codex-rs/rmcp-client/src/lib.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs codex-rs/core/src/fast_lane_host_dispatch/mod.rs codex-rs/core/Cargo.toml codex-rs/Cargo.lock MODULE.bazel.lock; git commit -m 'feat: enforce deterministic storage admission'; Pop-Location
```

### Task 6: Connect admission to preparation, worker environment, and terminal release

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/registry.rs:567-1668`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs:393-580,1218-1260`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:1-1444`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/contract.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_profile.rs`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/pump.rs`
- Modify: `mcp-tools/devkit_runtime/fastlane_host_adapter.py`
- Modify: `mcp-tools/devkit_runtime/host_bridge.py`
- Modify: `mcp-tools/devkit_runtime/host_session.py`
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

- [ ] **Step 2: Connect both authority sources to one reservation service and consume sealed grants exactly once.**

Initial `claim_storage_admission` resolves the Task 4 `Sent` profile and calls
`reserve_member_once` for the Host's original runtime batch owner. Store each
decision, `StorageAssignmentBinding`, and private grant in registry/runtime
state. `consume_batch` validates the original batch and exact member coverage,
seals the group, and transfers/attaches those grants to `HostWriterContext`.
It does not regenerate route/lease facts, rekey batch indexes, or reserve again.

Successors are materialized natively by `consume_refill_queue`, not by a new
Python prepare call. Extend the exact refill codec/ledger with explicit
per-task budgets and bind them into the queue hash. `register_refill_queue`
validates coverage against its existing verified remaining skeletons and
index refs; it must not allocate capacity for all remaining work. After the
selected wave passes dependency/scope gates, resolve and revalidate its Host
profile provenance, construct intent and `StorageAssignmentBinding`, and call
the same `reserve_member_once` service with native selected-wave authority.
Do not manufacture bridge requests, correlations, nonces, or transport
receipts for this internal path. Attach sealed grants before the selected
wave can prepare. Failed admission does not advance the queue cursor; an old
queue without required budget/provenance is not silently upgraded or admitted.

`CodexHostDispatchAdapter::prepare_batch` must require the already-sealed
grants before `worktree_broker.reserve_batch`, and must never call admission
again. At worker config construction, inject the private common Cargo target
and per-member scratch into `config.permissions.shell_environment_policy.r#set`
as `CARGO_TARGET_DIR` and `CODEX_TASK_TEMP`. These paths stay in private
`HostWriterContext`, not the caller's receipt or bounded public context.

On prepare/dispatch failure, revoke unused member grants; after a writer has
started, terminal/recovery/integration paths must first confirm child shutdown
and perform postcheck. Release each member once and the family lease only
after the last member succeeds. A clone, timeout, or terminal ACK alone is
not release evidence. Postcheck failure returns `STORAGE_POSTCHECK_FAILED`
and retains ownership for later recovery; it never triggers a broad delete.

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
python -m py_compile server.py devkit_runtime/host_bridge.py devkit_runtime/host_session.py
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1
Pop-Location
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_fastlane_dispatch_forwards_receipt_identity_without_public_path -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Expected: Python probe passes, `py_compile` is silent, and `cargo check`
finishes successfully with no new warning. Stop before probes if compilation
fails. Keep one bounded Host successor/shared-group regression; Python-only
initial evidence is not full production acceptance. Do not run the full
workspace suite or repeatedly rebuild unchanged slices.

- [ ] **Step 5: Commit the production wiring.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/server.py mcp-tools/devkit_runtime/fastlane_host_adapter.py mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/tests/test_storage_firewall.py; git commit -m 'feat: bind admitted storage roots to fast lane workers'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/registry.rs codex-rs/core/src/fast_lane_host_dispatch/coordinator.rs codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs codex-rs/core/src/fast_lane_host_dispatch/contract.rs codex-rs/core/src/fast_lane_host_dispatch/storage_profile.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/pump.rs; git commit -m 'feat: bind admitted storage roots to fast lane workers'; Pop-Location
```

## Plan 1 acceptance gate and handoff

- [ ] Re-read the design sections “确定性 target key 与数据流” and “配额、文件数与剩余空间门槛”; verify every field and every fail-closed code is represented by a task above.
- [ ] Run `git diff --check` in both worktrees and verify only the mapped files changed.
- [ ] Run `python -m py_compile` on changed DevKit Python files and `cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1` with the existing Host `codex-rs\target` and `CARGO_INCREMENTAL=0`; retain current compile evidence instead of rerunning unchanged slices.
- [ ] Record one controlled admission receipt proving same semantics reuse one target key and one changed semantic forks it; record one low-space/policy-failure receipt proving no directory was created.
- [ ] Verify exact5 session lookup/core `Sent` binding, unchanged original batch hashes, same-owner member sharing with exact sealing, cross-owner conflict, and a native selected-successor admission without a second reservation. Do not label initial-only wiring complete.
- [ ] Do not implement ledger persistence, preview/apply, source deletion, session deletion, compression, or remote synchronization in this plan. Plan 2 consumes `StorageAdmissionReceipt`; Plan 3 consumes the released/observed storage records.
