# Storage Firewall 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Cargo, Python, MCP-package, and Fast Lane write begin with one host-approved deterministic generated root and a fail-closed byte/file/free-space reservation.

**Architecture:** DevKit emits a bounded, path-free `StorageIntent` whose hash is bound to the Fast Lane task, source plan, execution context, and project identity. The Codex Host resolves an already-issued profile, validates the intent, and reserves a deterministic target family for a Host-owned wave with per-task member grants. The Host remains an authenticated client of `codex-storage-broker-windows`; it never owns protected-root writer handles or falls back to a local ledger. Receipts contain identities only; private worker facts carry paths. Existing route/lease batch hashes remain unchanged. Lease persistence, cleanup, GitHub source authorization, and session CAS are separate follow-up plans.

**Tech Stack:** Python 3.11 standard library (`dataclasses`, `hashlib`, `json`, `pathlib`), MCP FastMCP/Pydantic, Rust 2021, `serde`/`serde_json`, `sha2`, Tokio, platform filesystem-capacity APIs, and the existing authenticated inherited-handle bridge.

**Revision status (2026-08-30):** This is an unpublished protocol revision.
Preserve the implemented Task 4 profile/Sent/admission exchange and Task 5
shared codec/kernel contracts and their recorded compile evidence; the Host
aggregate two-crate check at `99e5` was reported exit 0. That is not Task 6
production acceptance. Task 6 below records the remaining configuration,
runtime sharing, filesystem ordering, process-proof, and postcheck work; no
checkbox is completed by this documentation update. The unpublished
admission-v1 request is exact5, replacing exact4; intent/target/profile-v1 stay
unchanged. If exact4 has been deployed outside these worktrees, use admission-v2
instead and reject downgrade.

**Protected-broker handoff (2026-08-31):** The dependency text below treats
storage broker Tasks 1-6 as prerequisites, not completed work. Broker delivery
does not authorize cleanup or live activation. This revision claims no Task 7
probe result, elevated provisioning/acceptance, or Bazel completion.

**Compile-first execution:** Reuse the Host's existing
`G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target`,
set `CARGO_INCREMENTAL=0`, and use `--locked -j1`. Check the changed crates
before running a small relevant probe. Do not recreate RED failures from
completed slices, repeatedly run full suites, or create a storage-specific
Cargo target. Unchecked tasks below are implementation/acceptance work, not
claims that the revised contract is complete.

---

## Scope and file map

Original line references are against commit `37029a9` in the DevKit worktree
and commit `552fe8035d` in the Codex Host worktree. Task 6's symbol map was
refreshed from the Host `f26f6ae` working tree and concurrent Task 4/5 repairs.
Re-read each named symbol before editing; later commits shift line numbers.

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
  reserve/seal before filesystem materialization and consume before writer
  preparation; modify `coordinator.rs:393-580,1218-1260` to settle only after
  confirmed process-tree shutdown and successful family postcheck.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:1-1444`
  at worker environment construction so the host-issued root is the sole
  `CARGO_TARGET_DIR`/task-temp value.
- Modify the existing `codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs`;
  preserve its sibling-module registration in `mod.rs`.
- Modify `codex-rs/config/src/config_toml.rs` (`ConfigToml`),
  `codex-rs/core/src/config/mod.rs` (`Config::load_config_with_layer_stack`),
  and generated `codex-rs/core/config.schema.json` for the explicit Host
  `fast_lane_storage` block. Read existing `config/src/config_layer_source.rs`
  for trust provenance and `config/src/schema.rs::write_config_schema` for generation.
- Modify `codex-rs/app-server/src/message_processor.rs`,
  `codex-rs/core/src/thread_manager.rs` (`ThreadManagerState`),
  `codex-rs/core/src/session/mod.rs` (`SessionSpawnArgs`),
  `codex-rs/core/src/session/session.rs`, and
  `codex-rs/core/src/state/service.rs` (`SessionServices`) to inject one
  Host-runtime service Arc into all registries. Reuse/modify
  `codex-rs/core/src/fast_lane_host_dispatch/storage_service.rs` for that
  authenticated client/policy facade, registering it in the existing dispatch
  `mod.rs`; reuse the broker plan's `storage_broker.rs` and do not recreate a
  root writer or local fallback in core.
- Modify `codex-rs/core/src/mcp_tool_call.rs` and
  `codex-rs/core/src/fast_lane_host_dispatch/worktree.rs` for planned versus
  materialized roots; no pre-admission `create_dir_all` remains.
- Modify `codex-rs/core/src/unified_exec/mod.rs`, `process.rs`, and
  `process_manager.rs`; `codex-rs/utils/pty/src/process.rs` and `win/job.rs`;
  and `codex-rs/core/src/session/handlers.rs` and `agent/control/legacy.rs`
  for actual owned-process termination evidence. Read/reuse
  `codex-rs/exec-server/src/process.rs` lifecycle events; modify that boundary
  only if its existing exit events cannot carry the required confirmation.
- Modify `codex-rs/protocol/src/shell_environment.rs` at final environment
  assembly, alongside core permissions/worker configuration, so reserved
  environment values survive filtering and cannot be replaced by per-call
  overrides in the storage-managed execution path.
- Create `codex-rs/core/src/fast_lane_host_dispatch/storage_postcheck.rs` and
  register it in `mod.rs`: real no-follow family/member observations after
  the process fence, not caller-supplied counters.

The Task 5 capacity provider itself still reuses existing platform FFI. Task 6
consumes the broker crate dependency established by storage broker Tasks 1-6;
do not add another writer dependency or alter manifests/locks in this
documentation-only revision. `MODULE.bazel.lock` remains a broker-plan gate at
the Host repository root, not evidence that Bazel completion already occurred.

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
all decision fields except itself. Both Host-minted IDs, `admission_id` and
`target_family_lease_id` (the internal family lease ID), are strictly
`sha256:` followed by 64 lowercase hexadecimal characters, not UUIDs, paths,
or arbitrary opaque strings. Keep that exact wire field name; do not add a
second `family_lease_id` alias to the exact receipt. The successful private
response envelope is exact `{schema, correlation_id, request_hash, receipt}` with schema
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
child/reparse checks. Every admitted artifact kind has the common
`<family>/cargo-target` child, including the existing `fastlane-task` intent;
`cargo_target_root()` supplies the worker's Cargo cache independently of its
primary artifact/output root. Scratch and non-Cargo outputs are private
`<family>/members/<owner identity>/<task identity>/{scratch,output}` children.
For `cargo-target`, the assigned primary root may be the common cache; never
send another artifact's output into that cache. Do not put task/member
suffixes in `target_key`, rewrite `fastlane-task` on the wire, or trust the
caller to select a Host-authorized job kind. Shared cache writes require the
supported Cargo locking contract; other outputs remain in disjoint member
grants. Family ownership survives until the
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

On Unix call the existing `libc::statvfs` binding; on Windows reuse the kernel's
existing `GetDiskFreeSpaceExW` FFI without adding a new crate dependency;
on unsupported platforms return
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
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rmcp-client/src/storage_intent.rs codex-rs/rmcp-client/src/lib.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall.rs codex-rs/core/src/fast_lane_host_dispatch/storage_firewall_tests.rs codex-rs/core/src/fast_lane_host_dispatch/mod.rs; git commit -m 'feat: enforce deterministic storage admission'; Pop-Location
```

### Task 6: Connect admission to preparation, worker environment, and terminal release

Use this exact implementation dependency order:

```text
storage broker Tasks 1-6
  -> Plan 2 P2-base Task 1 control transactions
  -> Plan 1 Task 6 durable refill/lifecycle wiring
  -> Plan 2 remaining P2-base owner recovery
  -> Plan 2 P2-apply preview and generated cleanup
```

Broker completion unblocks only Plan 2 P2-base Task 1. It does not authorize
cleanup or live activation, and it does not by itself complete this Task 6.

**Independent file groups:** All paths below are within the scope map above.

| Slice | Files and existing attachment points |
| --- | --- |
| 6a configuration/service | `config/src/config_toml.rs::ConfigToml`, `core/src/config/mod.rs::Config::load_config_with_layer_stack`, generated `core/config.schema.json`; core `storage_service.rs`, broker-plan `storage_broker.rs`, and `mod.rs` as the authenticated Host client only; `app-server/src/message_processor.rs`, `core/src/thread_manager.rs::ThreadManagerState`, `core/src/session/mod.rs::SessionSpawnArgs`, `core/src/session/session.rs`, `core/src/state/service.rs::SessionServices` |
| 6b authority/write ordering | `core/src/fast_lane_host_dispatch/{registry,contract,storage_profile,worktree}.rs`, `core/src/mcp_tool_call.rs`; existing rmcp-client refill protocol/session/pump; DevKit `fastlane_host_adapter.py`, `host_bridge.py`, `host_session.py`, `server.py` |
| 6c worker isolation | `core/src/fast_lane_host_dispatch/codex_adapter.rs::{HostWriterContext,CodexHostDispatchFacts,prepare_batch}`, `core/src/config/mod.rs::Permissions`, `protocol/src/shell_environment.rs`, `core/src/unified_exec/process_manager.rs` |
| 6d real termination | `core/src/unified_exec/{mod,process,process_manager}.rs`, `utils/pty/src/{process,win/job}.rs`, `core/src/session/handlers.rs`, `core/src/agent/control/legacy.rs`; reuse `exec-server/src/process.rs` event boundary |
| 6e postcheck/settlement | new `core/src/fast_lane_host_dispatch/storage_postcheck.rs`, `mod.rs`, existing `codex_adapter.rs` terminal/recovery methods and `coordinator.rs` observers; existing storage firewall tests |

Host paths in this table are relative to `codex-rs`; DevKit runtime filenames
are under `mcp-tools/devkit_runtime`, with `server.py` under `mcp-tools`.
6a can compile independently with storage disabled. 6d can be implemented
independently of admission. Durable 6b depends on storage broker Tasks 1-6,
Plan 2 P2-base Task 1, 6a, and the preserved Task 4/5 contracts; 6c depends on
6b. Successful release in 6e requires both phases of 6d, not just worker wiring
or a green compile. Coordinate shared files rather than concurrently editing
registry/adapter/process-manager from two slices.

- [ ] **Step 6a: Load explicit trusted configuration and inject one runtime-owned service.**

Preserve the implemented optional `fast_lane_storage` block in `ConfigToml`,
containing exactly nine required fields: `approved_root`, `task_byte_limit`, `task_file_limit`,
`target_family_byte_limit`, `target_family_file_limit`,
`global_reserved_byte_limit`, `global_reserved_file_limit`,
`free_space_floor_bytes`, and `emergency_floor_bytes`. The root must be an
explicit absolute Host-approved directory; all eight numeric fields use
`NonZeroU64` and must also pass the kernel's checked policy validation,
including emergency <= floor. Keep the root and all eight values pending the
operator's explicit choice; do not populate them in this plan revision.
Do not supply numerical defaults, derive authority from free disk space, or
create an approved root while loading configuration. Missing block disables
storage admission with `STORAGE_POLICY_MISSING`; malformed blocks are rejected.

Use the existing `ConfigLayerSource` provenance, not merged values alone.
System/enterprise-managed/operator user configuration may supply the block;
workspace `Project` configuration cannot set or override it. Treat session
flags as authority only when the Host startup path explicitly validates an
operator override, never when copied from worker/caller facts. Reject an
untrusted storage override instead of silently merging individual fields.
Local configuration rejection uses `STORAGE_CONFIG_SOURCE_NOT_TRUSTED` and
`STORAGE_CONFIG_RESTART_REQUIRED`, alongside `STORAGE_ROOT_NOT_APPROVED` and
`STORAGE_POLICY_MISSING` where applicable. These are local configuration
diagnostics, not new public admission-wire fields or a change to the exact
request/receipt shape; do not expose private configuration paths in responses.

Generate only `core/config.schema.json` with the lightweight config example;
do not rebuild a core binary solely to emit schema. The implementation command
below is documentation, not an instruction to run it during plan-only edits:

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo run --locked -p codex-config --example write_config_schema -j1 -- 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\core\config.schema.json'
Pop-Location
```

Construct the sole service Arc from the manager's base `Config` in
`ThreadManager` initialization, not from per-session or per-turn config.
The Host runtime caller in `app-server/src/message_processor.rs` supplies
that base configuration. Store the same Arc in `ThreadManagerState`, forward
through `SessionSpawnArgs`/`SessionServices`, and give every
`FastLaneHostFactsRegistry` a reference to it. The new
`storage_service.rs` facade owns the single Host-side policy evaluator and
authenticated broker client for the immutable resolved policy/root, not an
independent reservation ledger or protected-root handle.
Adapt every constructor/call site; test-only construction may be explicitly
disabled or use an injected fixture, never a permissive production default.
Current `session/session.rs` creates registries per Session: keep that facts
scope but do not create a firewall there. Per-thread config reload must not
reset global reservations or replace policy/root; reject a differing storage
configuration with the local `STORAGE_CONFIG_RESTART_REQUIRED` diagnostic.

This is cross-session sharing of one authenticated client inside one Host
runtime. Cross-process serialization, persistence, and root ownership belong
only to `codex-storage-broker-windows`; an independent Host process is another
authenticated client and cannot claim the root. Remaining owner recovery stays
in Plan 2 after this Task 6. An in-memory Arc is never root authority.

- [ ] **Step 6b: Admit/seal before any task-root write, then consume once.**

Initial `claim_storage_admission` resolves the Task 4 `Sent` profile and calls
`reserve_member_once` for the original Host runtime batch. Each task retains
its own profile/attestation hash; group authority comes from verified original
batch/preparation/call/generation, not equality of per-task profile hashes.
Keep each decision, `StorageAssignmentBinding`, and grant in private Host
state. Seal against the original exact task set before filesystem writes.
`consume_batch` revalidates that sealed set and consumes/transfers the grants
before setting `BatchConsumed`, removing `by_batch`, or activating scope
leases. On failure preserve a recoverable consistent state. It neither
rewrites original route/lease hashes nor reserves a second time.

`mcp_tool_call.rs` currently creates a hardcoded per-session/call task root
before constructing `GitWorktreeBroker`; remove this pre-admission mkdir.
Split `GitWorktreeBroker::new`'s current existing-root canonicalization into
planned-root validation against the approved existing parent and later
materialization with identity/reparse revalidation. Materialize only after
successful admission/seal and before the first durable queue write or
`reserve_batch`; adapter preparation revalidates the materialized binding.

Python currently registers the refill queue before dispatch, while Host
`refill_authority_root_binding` canonicalizes an already-existing task root.
Move initial admission/seal before queue registration; delaying mkdir only
until `prepare_batch` is insufficient. Put queue metadata, atomic-replacement
temporaries, and worktree roots under explicit accounted ownership, but do
not place durable queue records in a member's releasable scratch/output.
Their lifecycle spans waves: releasing/cleaning the initial member must not
erase an active queue, and retaining its Cargo family lease for the queue
would permanently block a different-owner same-key successor.

Durable refill registration therefore depends first on storage broker Tasks
1-6 and then on Plan 2 P2-base Task 1 binding an independently bounded
broker-owned control allocation to a verified queue-lifetime owner, explicit
positive byte/file allowance, safe private root, and terminal queue settlement.
The Host accesses those control transactions only as an authenticated client.
Plan 2 must provide that control-allocation contract before this part of 6b is
enabled; implement no ledger or new artifact/wire field in this Plan 1 update.
Until it exists, reject durable registration before any write. Do not invent
a control task/lease, take an unrequested budget, or exempt metadata from
accounting. Its owner must not hold the shared Cargo family lease; verify
that initial member release preserves the queue while same-key successors
can acquire their own group lease. This prerequisite changes the execution
order, not the frozen admission-v1 or target-v1 contract.

Successors remain native to `consume_refill_queue`. Extend the exact refill
codec and durable snapshot with task-keyed remaining budgets covered by the
queue hash; verify exact remaining-task coverage and existing index refs at
registration. Allocate only the selected wave after dependency/scope gates.
Use its verified native authority to build profile provenance, intent, and
binding and invoke the same service; no synthetic bridge request/correlation
or fabricated transport receipt. Seal/attach before prepare, and commit queue
cursor/budget consumption consistently with the attempt outcome. Admission
failure does not advance the queue. Old snapshots without required budget
provenance fail closed, not silent upgrade or all-backlog initial reservation.

- [ ] **Step 6c: Attach grants to workers and enforce reserved paths at final environment/permission assembly.**

`CodexHostDispatchAdapter::prepare_batch` checks sealed grants before
`worktree_broker.reserve_batch`; it never invokes admission again. Carry
private grant/binding state in `HostWriterContext`/`CodexHostDispatchFacts`.
Use `cargo_target_root()` for `CARGO_TARGET_DIR` for every artifact kind,
including `fastlane-task`, and the private scratch getter for
`CODEX_TASK_TEMP`; non-Cargo outputs use the member output root. No path is
put into the decision, public bounded context, or DevKit result.

Setting `config.permissions.shell_environment_policy.r#set` alone is not
sufficient: `include_only` is applied afterward and per-call overrides can
replace values. Preserve/reject overrides of reserved values at final
storage-managed environment assembly. Update actual `Permissions` using its
workspace-root/effective-profile APIs as well as `Config.workspace_roots`,
granting only the member paths and shared cache while respecting managed
denies. Do not make all of approved_root writable. Environment variables alone
do not confine arbitrary shell writes; the sandbox and descendant fence must
cover those writes, or the managed-storage execution path remains unavailable.

- [ ] **Step 6d.1: Retain process handles and await real exit, without minting terminal proof yet.**

`ProcessStore` currently has no wait operation. Its
`terminate_all_processes` drains entries then issues non-confirming terminate
calls; an empty map is not shutdown proof. `UnifiedExecProcess::terminate_confirmed`
also synthesizes `signal_exit` after termination without waiting for a real
local exit. Replace this use with an asynchronous confirmed boundary that
closes new process admission, retains ownership/handles, propagates kill
errors, and waits for the existing local `SpawnedPty.exit_rx`/wait task or
ExecServer `Exited` event. Distinguish actual exit from closed channels,
unknown exit, timeout, and synthetic status. Preserve retained grants and
recovery handles on failure; never turn an error into success by draining.

`utils/pty::ProcessHandle::terminate` currently ignores killer errors and
drops its wait handle; extend that boundary so the caller can await actual
termination. This phase establishes managed root-process exit only, not proof
that all descendants have stopped writing.

- [ ] **Step 6d.2: Fence owned descendants and shutdown-time writers before issuing TerminalProof.**

`shutdown_agent_tree` waits for agent threads, not an OS process tree.
For storage-managed processes enforce retained, non-escaping process-family
ownership; on Windows the JobObject path must not use `preserve_descendants`
and must confirm no active owned processes remain after termination. Use an
equivalent owned process-family fence on supported platforms; unsupported or
unconfirmable cases retain ownership/fail closed. Do not scan/kill unrelated
processes or equate root PID exit with descendant exit.

Cover code-mode, MCP, prewarm, and hooks that can write into grants.
`shutdown_session_runtime` currently runs session-end hooks after terminating
unified exec: run all permitted shutdown work before the final write fence,
or explicitly deny those paths once fenced. Prevent post-proof process or
writer creation. Bind the Host-created terminal evidence to the actual
member/group/generation and owned process set. Neither assistant `Completed`,
terminal ACK, nor `ShutdownComplete` establishes this evidence.

A never-started cancellation is allowed only when Host lifecycle state proves
no worker/process was ever created. A prepared endpoint with no assistant
input can already have prewarm/hooks; it requires the same termination fence.
Failed prepare/recovery must leave storage ownership in the shared service
even if adapter runtime objects are removed. Until both 6d phases hold, do not
wire a synthetic TerminalProof just to enable successful release.

- [ ] **Step 6e: Scan real family contents after the fence and settle exactly once.**

`WriterIntegrationCandidate::collect_terminal` proves Git/worktree state, not
disk usage. Implement the new `storage_postcheck.rs` collector over private
grant paths: bounded no-follow/reparse-safe traversal, checked byte/file
sums, and path identity revalidation. Inaccessible, unstable, or unbounded
observations return `STORAGE_POSTCHECK_FAILED` and retain ownership. Never
accept caller counters or delete data to make a postcheck pass.

Attach member terminal handling at adapter
`shutdown_and_validate_terminal_success` and its recovery paths, with
coordinator observers retaining the group barrier. A fenced member's private
scratch/output can be checked independently; shared cache/family counts
require every active member in that family to be quiet. Count shared files
once, retain family observed usage across released tombstones, and release
the family lease only after the last required postcheck succeeds. Scope-lease
release and terminal ACK are not substitutes for storage settlement.

- [ ] **Step 6f: Compile changed slices first, then keep only bounded acceptance probes.**

Retain the current Task 4/5 compile evidence rather than rerunning unchanged
slices. For newly changed wiring, run from DevKit `mcp-tools`:

```powershell
python -m py_compile server.py devkit_runtime/host_bridge.py devkit_runtime/host_session.py
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs\target'
$env:CARGO_INCREMENTAL='0'
cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1
Pop-Location
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_firewall.py::test_fastlane_dispatch_forwards_receipt_identity_without_public_path -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-firewall-pytest
```

Also compile changed app-server/PTY/protocol callers in the relevant slice,
using the same existing target, `CARGO_INCREMENTAL=0`, and `--locked -j1`.
The two-crate check alone does not cover an edited app-server constructor.
Stop before probes on compile failure. Preserve one selected public-path
non-disclosure probe, one native-successor/shared-group regression, and one
bounded termination-failure/retained-ownership probe. Do not run the full
workspace suite, regenerate old RED evidence, or repeatedly rebuild unchanged
slices. Python-only initial evidence is not full production acceptance.

- [ ] **Step 6g: Commit each compiled file group with explicit remaining gates.**

Stage only that slice's named files after `git diff --check`; do not use a
broad `git add` or include another worker's dirty adapter/registry changes.
Record the compile scope and unverified process/platform/config conditions
with each handoff. A disabled 6a service or root-process-only 6d.1 can be a
compiled intermediate slice, but cannot be reported as Task 6 storage release
or complete Plan 1 acceptance.

## Plan 1 acceptance gate and handoff

- [ ] Re-read the design sections “确定性 target key 与数据流” and “配额、文件数与剩余空间门槛”; verify every field and every fail-closed code is represented by a task above.
- [ ] Run `git diff --check` in both worktrees and verify only the mapped files changed.
- [ ] Run `python -m py_compile` on changed DevKit Python files and `cargo check -p codex-rmcp-client -p codex-core --lib --locked -j1` with the existing Host `codex-rs\target` and `CARGO_INCREMENTAL=0`; retain current compile evidence instead of rerunning unchanged slices.
- [ ] Record one controlled admission receipt proving same semantics reuse one target key and one changed semantic forks it; record one low-space/policy-failure receipt proving no directory was created.
- [ ] Verify exact5 session lookup/core `Sent` binding, unchanged original batch hashes, same-owner member sharing with exact sealing, cross-owner conflict, and a native selected-successor admission without a second reservation. Do not label initial-only wiring complete.
- [ ] Verify explicit trusted root plus eight policy values, one authenticated
  Host client service shared across Sessions, and no pre-admission
  task/control/queue directory writes. Verify the broker is the sole root writer
  and no Host-local fallback exists across independent Host processes.
- [ ] Before enabling durable refill, complete storage broker Tasks 1-6 and
  obtain Plan 2 P2-base Task 1's broker-backed bounded queue-lifetime control
  allocation: initial member release cannot remove active metadata, and that
  allocation cannot retain the Cargo family lease or block same-key successors.
  Missing allocation rejects registration before writes; no free budget or
  fabricated task is allowed. Remaining P2-base owner recovery follows this
  Task 6; P2-apply follows only after that recovery.
- [ ] Verify actual process and descendant shutdown evidence, denial of post-proof writers, and a real all-members-quiet family scan before release. Failure/timeout retains ownership; `Completed`, `ShutdownComplete`, ACK, or an empty ProcessStore is not sufficient.
- [ ] Obtain the operator's approved absolute root and eight policy values before production enablement; do not invent them or reuse example test capacities as configuration. Record any unsupported process-containment platform as a remaining activation gate.
- [ ] Do not implement ledger persistence, preview/apply, source deletion, session deletion, compression, or remote synchronization in this plan. Plan 2 consumes `StorageAdmissionReceipt`; Plan 3 consumes the released/observed storage records.
- [ ] Treat broker completion, package presence, compile/probe success, and any
  later elevated provisioning acceptance as separate gates. None authorizes
  cleanup or live activation; this plan revision claims none complete.
