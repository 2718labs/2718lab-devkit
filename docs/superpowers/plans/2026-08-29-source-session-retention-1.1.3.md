# Source and Session Retention 1.1.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require GitHub reachability plus an unexpired exact-path authorization before any local source removal, repair the session index activity timestamp after durable rollout writes, and deduplicate only immutable archived CAS objects with no live reference.

**Architecture:** Plans 1 and 2 provide deterministic generated roots, the persistent storage ledger, owner probes, and the preview/recheck/apply writer fence. This plan adds a separate Host-only retention authority: source cleanup is a two-phase candidate transaction guarded by remote identity, clean/unpushed/current-branch/active-lease checks and an opaque path authorization; session retention updates the append-only activity index after durable writes and limits CAS dedupe to the dedicated archived CAS object root. DevKit validates and forwards exact requests but never proves Git state, mints authorization, scans sessions, or removes a path.

**Tech Stack:** Rust 2021 (`serde`, `serde_json`, `sha2`, Tokio, `std::process::Command`), Git CLI, Python 3.11 (`dataclasses`, `hashlib`, `json`), FastMCP/Pydantic, the existing authenticated inherited-handle bridge, `codex-rollout`, and the Plan 2 storage ledger/journal.

---

## Scope and file map

Line references start at DevKit commit `37029a9b1677aade4a2874d7fb3e30cb61f01092`
and Host commit `552fe8035d8bb928467fff428773fc4d5d34c168`. Plans 1 and 2
will move some Host line numbers, so re-read every named symbol before editing.

DevKit files:

- Create `mcp-tools/devkit_runtime/storage_retention.py`: exact source-preview,
  source-apply, session-CAS-preview, and session-CAS-apply request projections.
  It accepts an opaque Host authorization token but never creates one.
- Modify `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677` and
  `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`: carry the four
  retention operations over the existing authenticated single-reader/session
  queue.
- Modify `mcp-tools/server.py:179-289,1008-1314` and
  `mcp-tools/devkit_runtime/tool_metadata.py:5-23`: expose read-only preview
  tools and destructive apply tools with exact bounded inputs.
- Create `mcp-tools/tests/test_storage_retention.py` and modify
  `mcp-tools/tests/test_mcp_contract.py:240-360`: reject public path guessing,
  missing authorization, unbounded batches, and destructive annotation drift.

Codex Host files:

- Create `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs`:
  exact path authorization verification, GitHub remote reachability, source
  candidate manifests, source apply receipts, and the adapter to archived CAS.
- Create
  `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`
  and modify `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49`.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs` from
  Plan 2 to expose read-only active-lease/path checks and the existing writer
  fence/journal to retention; no second ledger is created.
- Modify `codex-rs/core/src/fast_lane_host_dispatch/worktree.rs:65-119` and
  `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:802-979`: stop
  automatic integrated-worktree removal, register a protected source cleanup
  candidate, and route later removal through the retention authority.
- Modify `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`,
  `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:25-220`,
  and
  `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs:412-570`
  with exact retention wire schemas.
- Modify `codex-rs/rollout/src/session_index.rs:20-294` and
  `codex-rs/rollout/src/session_index_tests.rs:1-430`: append an activity row
  after a durable rollout write while retaining the last explicit thread name.
- Modify `codex-rs/rollout/src/recorder.rs:829-1024,1624-1867` and
  `codex-rs/rollout/src/recorder_tests.rs:90-190`: carry `codex_home/thread_id`
  into the writer, preserve deferred creation, create a missing YYYY/MM/DD
  directory on first persistence, and touch the activity index only after the
  rollout flush succeeds.
- Create `codex-rs/rollout/src/archived_session_cas.rs` and
  `codex-rs/rollout/src/archived_session_cas_tests.rs`; modify
  `codex-rs/rollout/src/lib.rs:60-140` and
  `codex-rs/rollout/src/rollout_reference_index.rs:19-90`: transactionally
  deduplicate only registered `.blob` objects below
  `archived_sessions/cas/objects`, using rollout reference evidence.

The worker must not edit ordinary rollout bodies, compression policy,
`delete_thread.rs`, arbitrary project directories, or any file outside this
map. A source directory that is not a registered linked worktree is protected
in 1.1.3; support for deleting standalone clones requires a later design.

## Exact contracts and invariants

`source_cleanup_apply` accepts this exact shape. `exact_path` is required on
the destructive call so the user-visible authorization and the Host recheck
bind the same literal target; public receipts expose only `path_identity`.

```json
{
  "schema": "2718lab.storage.source-cleanup-apply.v1",
  "exact_path": "G:\\2718lab\\_codex\\.codex-task-temp\\writer-01",
  "repository_identity": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "tree": "89abcdef0123456789abcdef0123456789abcdef",
  "remote_name": "origin",
  "candidate_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "ledger_epoch": 14,
  "authorization_token": "pathauth-v1.opaque-host-signed-value",
  "batch_limit": 1
}
```

The Host-decoded `PathAuthorization` has exact fields `schema`,
`authorization_id`, `exact_path`, `path_identity`, `repository_identity`,
`commit`, `tree`, `authorizer`, `issued_at`, `expires_at`, `nonce`, and
`authorization_hash`. It is signed or MAC-verified by a Host-injected
`PathAuthorizationVerifier`; DevKit cannot mint it. The authorization expires
after at most 15 minutes and authorizes exactly one literal canonical path,
repository identity, commit, and tree.

The source operation is fail-closed unless all of these fresh facts agree:

1. The target is one registered linked worktree below the approved task root,
   not the repository root, current Host cwd, current worktree, or a protected
   branch.
2. `git status --porcelain=v1 -z --untracked-files=all` is empty and `HEAD` plus
   `HEAD^{tree}` exactly match the authorization.
3. The configured remote URL canonicalizes to the same
   `github.com/<owner>/<repository>` identity as the ledger record.
4. A Host-owned, quota-admitted bare verifier fetch proves the commit is an
   ancestor of at least one currently advertised GitHub head. A failed network
   or stat call is `GITHUB_REACHABILITY_UNAVAILABLE`; no matching head is
   `GITHUB_COMMIT_NOT_REACHABLE` and therefore also covers unpushed commits.
5. Plan 2 reports no active/recovery/quarantined storage lease, Fast Lane scope
   lease, running owner, pending integration, or incomplete cleanup receipt for
   the exact path identity.
6. Candidate hash, ledger epoch, authorization, Git facts, and path identity
   still match after acquiring the Plan 2 writer fence.

Archived-session CAS uses a separate exact root and never treats a rollout
body as an object. Eligible paths have the form
`archived_sessions/cas/objects/<two lowercase hex>/<62 lowercase hex>.blob`.
Every object must be immutable, hash/length verified, referenced only by
archived records, absent from `RolloutReferenceIndex` live/current lineage,
and absent from active writer/lease/checkpoint/reference probes. Ordinary
`.jsonl`, `.jsonl.zst`, `sessions/**`, the current rollout, and any unknown file
are always protected. A dedupe transaction atomically repoints archived CAS
references to the lexically first verified object, persists the CAS index,
removes only redundant `.blob` objects, then writes a receipt.

## Implementation tasks

### Task 1: Freeze retention requests and protection failures with RED tests

**Files:**
- Create: `mcp-tools/tests/test_storage_retention.py`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`
- Create: `codex-rs/rollout/src/archived_session_cas_tests.rs`
- Modify: `mcp-tools/tests/test_mcp_contract.py:240-360`

- [ ] **Step 1: Add the Python RED test for mandatory exact path authorization.**

```python
def test_source_cleanup_apply_requires_exact_path_and_host_authorization():
    from devkit_runtime.storage_retention import RetentionContractError, SourceCleanupApply

    value = {
        "schema": "2718lab.storage.source-cleanup-apply.v1",
        "repository_identity": "sha256:" + "a" * 64,
        "commit": "0" * 40,
        "tree": "1" * 40,
        "remote_name": "origin",
        "candidate_hash": "sha256:" + "b" * 64,
        "ledger_epoch": 1,
        "authorization_token": "",
        "batch_limit": 1,
    }
    try:
        SourceCleanupApply.from_mapping(value)
    except RetentionContractError as error:
        assert error.code == "PATH_AUTHORIZATION_REQUIRED"
    else:
        raise AssertionError("source cleanup accepted no exact authorization")
```

- [ ] **Step 2: Add Host RED tests for dirty, unpushed, active, and current worktrees.**

```rust
#[test]
fn source_cleanup_protects_dirty_unpushed_active_and_current_worktrees() {
    for (mutation, code) in [
        (SourceMutation::Dirty, "STORAGE_PROTECTED_DIRTY"),
        (SourceMutation::Unpushed, "GITHUB_COMMIT_NOT_REACHABLE"),
        (SourceMutation::ActiveLease, "STORAGE_PROTECTED_ACTIVE"),
        (SourceMutation::CurrentWorktree, "STORAGE_PROTECTED_SOURCE"),
    ] {
        let fixture = source_fixture(mutation);
        let error = fixture.authority.preview(fixture.request).unwrap_err();
        assert_eq!(error.code(), code);
        assert!(fixture.worktree.exists());
    }
}
```

- [ ] **Step 3: Add the archived CAS RED test proving rollout bodies are outside the deletion domain.**

```rust
#[test]
fn cas_dedupe_never_selects_current_active_or_rollout_body() {
    let fixture = archived_cas_fixture_with_duplicate_and_rollout_body();
    let preview = fixture.store.preview(&fixture.protection).unwrap();
    assert_eq!(preview.groups.len(), 1);
    assert!(preview.groups[0].objects.iter().all(|item| item.path.extension().unwrap() == "blob"));
    assert!(fixture.current_rollout.exists());
    assert!(fixture.archived_rollout_body.exists());
}
```

- [ ] **Step 4: Run only the new RED probes.**

From the DevKit `mcp-tools` directory:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_retention.py::test_source_cleanup_apply_requires_exact_path_and_host_authorization -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-retention-pytest
```

From the Host `codex-rs` directory:

```powershell
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-core source_cleanup_protects_dirty_unpushed_active_and_current_worktrees --locked -j1
$env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-rollout cas_dedupe_never_selects_current_active_or_rollout_body --locked -j1
```

Expected: all three commands fail because the retention modules do not yet
exist. No source, rollout, or CAS object is removed.

- [ ] **Step 5: Commit RED tests separately in their owning repositories.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/tests/test_storage_retention.py mcp-tools/tests/test_mcp_contract.py; git commit -m 'test: define source and session retention contract'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs codex-rs/rollout/src/archived_session_cas_tests.rs; git commit -m 'test: define source and session retention contract'; Pop-Location
```

### Task 2: Implement exact DevKit retention contracts and Host stable types

**Files:**
- Create: `mcp-tools/devkit_runtime/storage_retention.py`
- Create: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/mod.rs:1-49`
- Test: `mcp-tools/tests/test_storage_retention.py`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`

- [ ] **Step 1: Implement the exact Python apply parser.**

```python
from dataclasses import dataclass
import re
from typing import Mapping

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REMOTE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_APPLY_FIELDS = frozenset({
    "schema", "exact_path", "repository_identity", "commit", "tree",
    "remote_name", "candidate_hash", "ledger_epoch",
    "authorization_token", "batch_limit",
})


class RetentionContractError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceCleanupApply:
    exact_path: str
    repository_identity: str
    commit: str
    tree: str
    remote_name: str
    candidate_hash: str
    ledger_epoch: int
    authorization_token: str
    batch_limit: int

    @classmethod
    def from_mapping(cls, value: object) -> "SourceCleanupApply":
        if type(value) is not dict or set(value) != _APPLY_FIELDS:
            raise RetentionContractError("PATH_AUTHORIZATION_REQUIRED")
        if value.get("schema") != "2718lab.storage.source-cleanup-apply.v1":
            raise RetentionContractError("PATH_AUTHORIZATION_REQUIRED")
        path = value.get("exact_path")
        token = value.get("authorization_token")
        epoch = value.get("ledger_epoch")
        limit = value.get("batch_limit")
        if (
            type(path) is not str or not path or not _is_absolute_literal(path)
            or type(token) is not str or not token.startswith("pathauth-v1.")
            or len(token) > 4096
            or type(epoch) is not int or isinstance(epoch, bool) or epoch < 1
            or type(limit) is not int or isinstance(limit, bool) or limit != 1
            or type(value.get("repository_identity")) is not str
            or _DIGEST.fullmatch(value["repository_identity"]) is None
            or type(value.get("candidate_hash")) is not str
            or _DIGEST.fullmatch(value["candidate_hash"]) is None
            or type(value.get("commit")) is not str
            or _OBJECT_ID.fullmatch(value["commit"]) is None
            or type(value.get("tree")) is not str
            or _OBJECT_ID.fullmatch(value["tree"]) is None
            or type(value.get("remote_name")) is not str
            or _REMOTE.fullmatch(value["remote_name"]) is None
        ):
            raise RetentionContractError("PATH_AUTHORIZATION_REQUIRED")
        return cls(path, value["repository_identity"], value["commit"], value["tree"], value["remote_name"], value["candidate_hash"], epoch, token, limit)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": "2718lab.storage.source-cleanup-apply.v1",
            "exact_path": self.exact_path,
            "repository_identity": self.repository_identity,
            "commit": self.commit,
            "tree": self.tree,
            "remote_name": self.remote_name,
            "candidate_hash": self.candidate_hash,
            "ledger_epoch": self.ledger_epoch,
            "authorization_token": self.authorization_token,
            "batch_limit": self.batch_limit,
        }


def _is_absolute_literal(value: str) -> bool:
    return (
        len(value) >= 4
        and value[1:3] == ":\\"
        and value[0].isalpha()
        and "/" not in value
        and "\\.\\" not in value
        and "\\..\\" not in value
        and not value.endswith(("\\.", "\\.."))
    )
```

Add concrete `from_mapping` and `to_wire` methods for
`SourceCleanupPreview(exact_path, repository_identity, commit, tree,
remote_name)`, `SessionCasPreview()` and
`SessionCasApply(candidate_hash, ledger_epoch, batch_limit)`. Their exact
field sets are respectively `{schema, exact_path, repository_identity,
commit, tree, remote_name}`, `{schema}`, and `{schema, candidate_hash,
ledger_epoch, batch_limit}`; the schemas are the four constants in Task 7.
Reuse `_is_absolute_literal`, `_DIGEST`, `_OBJECT_ID`, and `_REMOTE` above,
require `ledger_epoch >= 1` and `1 <= batch_limit <= 16`, and return a dict
containing exactly those fields from each `to_wire`. No parser accepts a
wildcard, relative path, free-form Git URL, CAS path, or delete flag.

- [ ] **Step 2: Define Host types and exact stable codes.**

```rust
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct PathAuthorization {
    pub(crate) schema: String,
    pub(crate) authorization_id: String,
    pub(crate) exact_path: PathBuf,
    pub(crate) path_identity: String,
    pub(crate) repository_identity: String,
    pub(crate) commit: String,
    pub(crate) tree: String,
    pub(crate) authorizer: String,
    pub(crate) issued_at: u64,
    pub(crate) expires_at: u64,
    pub(crate) nonce: String,
    pub(crate) authorization_hash: String,
}

pub(crate) trait PathAuthorizationVerifier: Send + Sync {
    fn verify(&self, opaque: &str, now: u64) -> Result<PathAuthorization, RetentionError>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub(crate) enum RetentionError {
    #[error("GitHub reachability unavailable")]
    GithubReachabilityUnavailable,
    #[error("commit is not reachable from a configured GitHub head")]
    GithubCommitNotReachable,
    #[error("exact path authorization is required")]
    PathAuthorizationRequired,
    #[error("candidate is stale")]
    CandidateStale,
    #[error("active owner protects the target")]
    ProtectedActive,
    #[error("dirty source protects the target")]
    ProtectedDirty,
    #[error("source target is protected")]
    ProtectedSource,
    #[error("session target is protected")]
    ProtectedSession,
    #[error("CAS evidence does not match")]
    CasMismatch,
    #[error("CAS reference is active")]
    CasReferenceActive,
    #[error("retention apply is incomplete")]
    ApplyIncomplete,
    #[error("retention postcheck failed")]
    PostcheckFailed,
}

impl RetentionError {
    pub(crate) fn code(self) -> &'static str {
        match self {
            Self::GithubReachabilityUnavailable => "GITHUB_REACHABILITY_UNAVAILABLE",
            Self::GithubCommitNotReachable => "GITHUB_COMMIT_NOT_REACHABLE",
            Self::PathAuthorizationRequired => "PATH_AUTHORIZATION_REQUIRED",
            Self::CandidateStale => "STORAGE_CANDIDATE_STALE",
            Self::ProtectedActive => "STORAGE_PROTECTED_ACTIVE",
            Self::ProtectedDirty => "STORAGE_PROTECTED_DIRTY",
            Self::ProtectedSource => "STORAGE_PROTECTED_SOURCE",
            Self::ProtectedSession => "STORAGE_PROTECTED_SESSION",
            Self::CasMismatch => "STORAGE_CAS_MISMATCH",
            Self::CasReferenceActive => "STORAGE_CAS_REFERENCE_ACTIVE",
            Self::ApplyIncomplete => "STORAGE_APPLY_INCOMPLETE",
            Self::PostcheckFailed => "STORAGE_POSTCHECK_FAILED",
        }
    }
}
```

- [ ] **Step 3: Turn the contract probes green and compile the new Python module.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery\mcp-tools'; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_retention.py::test_source_cleanup_apply_requires_exact_path_and_host_authorization -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-retention-pytest; python -m py_compile devkit_runtime/storage_retention.py; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-core source_retention_contract --locked -j1; Pop-Location
```

Expected: the Python test passes, `py_compile` is silent, and the focused Host
contract tests pass.

- [ ] **Step 4: Commit the contract implementation separately.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/devkit_runtime/storage_retention.py mcp-tools/tests/test_storage_retention.py; git commit -m 'feat: validate exact retention requests'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs codex-rs/core/src/fast_lane_host_dispatch/mod.rs; git commit -m 'feat: define host retention authority'; Pop-Location
```

### Task 3: Repair durable session activity indexing and day-directory creation

**Files:**
- Modify: `codex-rs/rollout/src/session_index.rs:20-294`
- Modify: `codex-rs/rollout/src/session_index_tests.rs:1-430`
- Modify: `codex-rs/rollout/src/recorder.rs:829-1024,1624-1867`
- Modify: `codex-rs/rollout/src/recorder_tests.rs:90-190`

- [ ] **Step 1: Add a RED test that a durable write refreshes the index and creates the missing day directory.**

```rust
#[tokio::test]
async fn persist_creates_day_directory_and_refreshes_session_index_activity() -> std::io::Result<()> {
    let home = tempfile::tempdir()?;
    let config = test_config(home.path());
    let thread_id = ThreadId::new();
    append_session_index_entry(home.path(), &SessionIndexEntry {
        id: thread_id,
        thread_name: "saved-thread".into(),
        updated_at: "2024-01-01T00:00:00Z".into(),
    }).await?;
    let recorder = create_test_recorder(&config, thread_id).await?;
    let parent = recorder.rollout_path().parent().unwrap().to_path_buf();
    assert!(!parent.exists());
    recorder.record_canonical_items(&[test_event("saved")]).await?;
    recorder.persist().await?;
    assert!(parent.is_dir());
    let entry = latest_session_index_entry(home.path(), thread_id).await?.unwrap();
    assert_eq!(entry.thread_name, "saved-thread");
    assert_ne!(entry.updated_at, "2024-01-01T00:00:00Z");
    Ok(())
}
```

- [ ] **Step 2: Add a lock-safe activity touch that preserves the latest explicit name.**

```rust
pub async fn touch_thread_updated_at(
    codex_home: &Path,
    thread_id: ThreadId,
) -> std::io::Result<SessionIndexEntry> {
    let _guard = SESSION_INDEX_LOCK
        .lock()
        .map_err(|err| std::io::Error::other(err.to_string()))?;
    let path = session_index_path(codex_home);
    let thread_name = if path.exists() {
        scan_index_from_end_by_id(&path, &thread_id)?
            .map(|entry| entry.thread_name)
            .unwrap_or_default()
    } else {
        String::new()
    };
    let entry = SessionIndexEntry {
        id: thread_id,
        thread_name,
        updated_at: now_rfc3339()?,
    };
    append_session_index_entry_locked(codex_home, &entry)?;
    Ok(entry)
}
```

Refactor `append_session_index_entry` to acquire `SESSION_INDEX_LOCK` and call
the same synchronous `append_session_index_entry_locked`. Make
`find_thread_name_by_id` ignore an empty name. This keeps old rows readable,
prevents a touch/rename race, and allows unnamed persisted sessions to receive
an activity timestamp without inventing a title.

- [ ] **Step 3: Carry the Host-owned identity into the writer and touch only after flush succeeds.**

```rust
struct RolloutWriterState {
    writer: Option<JsonlWriter>,
    deferred_creation: bool,
    pending_items: Vec<RolloutItem>,
    meta: Option<SessionMeta>,
    cwd: PathBuf,
    codex_home: PathBuf,
    thread_id: ThreadId,
    rollout_path: PathBuf,
    ordinal_state: RolloutOrdinalState,
    last_logged_error: Option<String>,
}

async fn write_pending_once(&mut self) -> std::io::Result<()> {
    self.ensure_writer_open().await?;
    self.write_session_meta_if_needed().await?;
    self.write_pending_items_once().await?;
    if let Some(writer) = self.writer.as_mut() {
        writer.file.flush().await?;
    }
    super::session_index::touch_thread_updated_at(&self.codex_home, self.thread_id).await?;
    Ok(())
}
```

For `Create`, set `thread_id=conversation_id`; for `Resume`, derive the thread
ID with the existing `parse_timestamp_uuid_from_filename` result and fail the
recorder open if it does not match the rollout metadata. Keep
`precompute_new_rollout_path` side-effect free. The existing
`open_log_file -> fs::create_dir_all(parent)` remains the only first-persist
day-directory creation seam, so constructing an unused conversation still
creates neither a rollout nor a date directory.

- [ ] **Step 4: Run the two focused rollout probes and compile the crate.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-rollout persist_creates_day_directory_and_refreshes_session_index_activity --locked -j1; cargo test -p codex-rollout touch_thread_updated_at_preserves_latest_name --locked -j1; cargo check -p codex-rollout --locked -j1; Pop-Location
```

Expected: both focused tests pass and the crate check finishes with no new
warning. The first test must read the saved rollout before asserting the index
timestamp, proving the body was durable before the activity row.

- [ ] **Step 5: Commit the session durability repair.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rollout/src/session_index.rs codex-rs/rollout/src/session_index_tests.rs codex-rs/rollout/src/recorder.rs codex-rs/rollout/src/recorder_tests.rs; git commit -m 'fix: refresh session activity after durable writes'; Pop-Location
```

### Task 4: Prove GitHub reachability and build source cleanup previews

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`

- [ ] **Step 1: Add deterministic RED fixtures for remote mismatch and exact ancestor reachability.**

```rust
#[test]
fn preview_requires_matching_github_identity_and_remote_ancestor() {
    let reachable = source_fixture(SourceMutation::None);
    let preview = reachable.authority.preview(reachable.request).unwrap();
    assert_eq!(preview.repository_identity, reachable.repository_identity);
    let mismatch = source_fixture(SourceMutation::RemoteMismatch);
    assert_eq!(mismatch.authority.preview(mismatch.request).unwrap_err().code(), "GITHUB_REACHABILITY_UNAVAILABLE");
}
```

- [ ] **Step 2: Implement the injected GitHub reachability verifier.**

```rust
pub(crate) trait GithubReachability: Send + Sync {
    fn prove(
        &self,
        repository_root: &Path,
        remote_name: &str,
        expected_identity: &str,
        commit: &str,
    ) -> Result<GithubReachabilityReceipt, RetentionError>;
}

fn prove_with_bare_verifier(&self, request: &GithubRequest) -> Result<GithubReachabilityReceipt, RetentionError> {
    let remote_url = git_stdout(&request.repository_root, ["config", "--get", &format!("remote.{}.url", request.remote_name)])
        .map_err(|_| RetentionError::GithubReachabilityUnavailable)?;
    let identity = canonical_github_identity(remote_url.trim())
        .ok_or(RetentionError::GithubReachabilityUnavailable)?;
    if identity != request.expected_identity {
        return Err(RetentionError::GithubReachabilityUnavailable);
    }
    let verifier = self.admitted_verifier_root(&request.request_hash)?;
    run_git(&verifier, ["init", "--bare", "--quiet"])
        .map_err(|_| RetentionError::GithubReachabilityUnavailable)?;
    run_git(&verifier, ["fetch", "--quiet", "--no-tags", "--filter=blob:none", remote_url.trim(), "+refs/heads/*:refs/remotes/verified/*"])
        .map_err(|_| RetentionError::GithubReachabilityUnavailable)?;
    let heads = git_lines(&verifier, ["for-each-ref", "--format=%(refname)", "refs/remotes/verified"])
        .map_err(|_| RetentionError::GithubReachabilityUnavailable)?;
    if !heads.iter().any(|head| git_success(&verifier, ["merge-base", "--is-ancestor", request.commit.as_str(), head.as_str()])) {
        return Err(RetentionError::GithubCommitNotReachable);
    }
    Ok(GithubReachabilityReceipt::new(identity, request.commit.clone(), heads))
}
```

The verifier root is obtained from Plan 1 admission with its own byte/file
budget and released through Plan 2; it is never created below the source
worktree. `canonical_github_identity` accepts only exact GitHub HTTPS or SSH
remote forms and hashes the lowercase `github.com/owner/repository` identity.
Do not pass credentials or remote URL into any public receipt.

- [ ] **Step 3: Build the canonical source candidate only after every read-only gate passes.**

```rust
pub(crate) fn preview(&self, request: SourcePreviewRequest) -> Result<SourceCleanupPreview, RetentionError> {
    let source = self.resolve_registered_worktree(&request.exact_path)?;
    self.reject_repository_current_or_protected(&source)?;
    self.ledger.require_no_path_owner(&source.path_identity)?;
    require_empty_porcelain(&source.path)?;
    require_exact_git_object(&source.path, "HEAD", &request.commit)?;
    require_exact_git_object(&source.path, "HEAD^{tree}", &request.tree)?;
    let remote = self.github.prove(&source.repository_root, &request.remote_name, &request.repository_identity, &request.commit)?;
    let manifest = SourceCandidateManifest::new(self.ledger.epoch(), source, request, remote);
    Ok(SourceCleanupPreview::from_manifest(manifest, canonical_hash(&manifest)?))
}
```

`StorageLedger::require_no_path_owner` returns `STORAGE_PROTECTED_ACTIVE` for
active/reserved/recovery records and `STORAGE_PROTECTED_SOURCE` for
quarantined or incomplete-receipt records. The manifest contains the exact
commit/tree, registered-worktree identity, clean-status hash, remote heads
hash, ledger epoch, policy hash, and path identity.

- [ ] **Step 4: Run the focused preview tests and the core compile gate.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-core preview_requires_matching_github_identity_and_remote_ancestor --locked -j1; cargo test -p codex-core source_cleanup_protects_dirty_unpushed_active_and_current_worktrees --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: both tests pass and core compiles with no new warning. Tests use a
local injected `GithubReachability` fixture; they make no network call.

- [ ] **Step 5: Commit reachability and preview.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs; git commit -m 'feat: prove source cleanup reachability'; Pop-Location
```

### Task 5: Require authorization at apply and remove automatic worktree deletion

**Files:**
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/worktree.rs:65-119`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs:802-979`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`

- [ ] **Step 1: Add a RED test that remote reachability without exact authorization still preserves the worktree.**

```rust
#[test]
fn reachable_clean_source_without_exact_authorization_is_not_removed() {
    let fixture = source_fixture(SourceMutation::None);
    let preview = fixture.authority.preview(fixture.request.clone()).unwrap();
    let request = SourceApplyRequest::without_authorization(&preview);
    assert_eq!(fixture.authority.apply(request).unwrap_err().code(), "PATH_AUTHORIZATION_REQUIRED");
    assert!(fixture.worktree.exists());
}
```

- [ ] **Step 2: Verify authorization, re-preview under the writer fence, journal, remove, and postcheck.**

```rust
pub(crate) fn apply(&mut self, request: SourceApplyRequest) -> Result<SourceApplyReceipt, RetentionError> {
    let authorization = self.authorization.verify(&request.authorization_token, self.clock.now()?)?;
    require_authorization_match(&authorization, &request)?;
    let fence = self.ledger.writer_fence()?;
    let preview = self.preview(request.preview_request())?;
    if preview.candidate_hash != request.candidate_hash || preview.ledger_epoch != request.ledger_epoch {
        return Err(RetentionError::CandidateStale);
    }
    require_authorization_match_preview(&authorization, &preview)?;
    self.ledger.write_source_cleanup_started(&preview, &authorization, &fence)?;
    self.worktrees.remove_authorized_worktree(&preview.exact_path, &preview.commit)?;
    if preview.exact_path.exists() || self.worktrees.is_registered(&preview.exact_path)? {
        return Err(RetentionError::PostcheckFailed);
    }
    self.ledger.commit_source_cleanup_receipt(&preview, &authorization, &fence)
}
```

`require_authorization_match` verifies exact canonical path equality, path
identity, repository identity, commit, tree, `issued_at <= now < expires_at`,
maximum 15-minute lifetime, nonce uniqueness, and authorization hash. The
receipt schema is `2718lab.storage.source-cleanup-receipt.v1` and includes
only `code`, `path_identity`, `repository_identity`, `commit`, `tree`,
`candidate_hash`, `ledger_epoch`, `authorization_id`, `remote_receipt_hash`,
`removed`, and `receipt_hash`.

- [ ] **Step 3: Replace the production auto-remove seam with candidate registration.**

```rust
// codex_adapter.rs, after successful integration and writer shutdown
for worktree in &successful_worktrees.created {
    self.retention.register_integrated_source_candidate(
        worktree,
        &integration_receipt,
        self.registry.active_lease_set_hash(),
    )?;
}
// Do not call remove_integrated_batch here. Source cleanup is a later,
// explicitly authorized operation.
```

Rename `GitWorktreeBroker::remove_integrated_batch` to
`remove_authorized_worktree(path, expected_head)` and make it `pub(crate)` only
to `SourceSessionRetention`. It rechecks direct-child scope, registration,
clean porcelain, and exact HEAD immediately before `git worktree remove`; no
`--force` is allowed. Failed integration, dirty writers, and missing receipts
remain quarantined.

- [ ] **Step 4: Run the authorization and production-seam tests, then compile core.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-core reachable_clean_source_without_exact_authorization_is_not_removed --locked -j1; cargo test -p codex-core integrated_worktree_is_retained_until_authorized_cleanup --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: both tests pass, the integrated worktree still exists before apply,
the authorized one-item fixture is removed only after all rechecks, and core
compiles with no new warning.

- [ ] **Step 5: Commit source apply and production wiring.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs codex-rs/core/src/fast_lane_host_dispatch/worktree.rs codex-rs/core/src/fast_lane_host_dispatch/codex_adapter.rs codex-rs/core/src/fast_lane_host_dispatch/storage_ledger.rs; git commit -m 'feat: require authorization for source cleanup'; Pop-Location
```

### Task 6: Implement archived no-live-reference CAS dedupe

**Files:**
- Create: `codex-rs/rollout/src/archived_session_cas.rs`
- Create: `codex-rs/rollout/src/archived_session_cas_tests.rs`
- Modify: `codex-rs/rollout/src/lib.rs:60-140`
- Modify: `codex-rs/rollout/src/rollout_reference_index.rs:19-90`
- Modify: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs`
- Test: `codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs`

- [ ] **Step 1: Define the exact archived CAS index and protection snapshot.**

```rust
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchivedCasObject {
    pub object_id: String,
    pub content_hash: String,
    pub byte_length: u64,
    pub relative_path: PathBuf,
    pub immutable: bool,
    pub archived_thread_ids: Vec<ThreadId>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SessionProtectionSnapshot {
    pub active_thread_ids: HashSet<ThreadId>,
    pub current_rollout_ids: HashSet<RolloutId>,
    pub checkpoint_hashes: HashSet<String>,
    pub active_lease_hashes: HashSet<String>,
}

pub struct ArchivedSessionCas {
    root: PathBuf,
    index_path: PathBuf,
    objects: Vec<ArchivedCasObject>,
}
```

`ArchivedSessionCas::open` canonicalizes
`archived_sessions/cas/objects`, rejects any reparse point, validates exact
lowercase hash fan-out paths, rejects duplicate object IDs, and reads the
index with the same atomic replacement pattern used by Plan 2. It never scans
`sessions/**` or ordinary files in `archived_sessions/**`.

- [ ] **Step 2: Implement preview and transactional apply.**

```rust
pub fn preview(&self, protection: &SessionProtectionSnapshot, references: &RolloutReferenceIndex) -> Result<CasDedupePreview, ArchivedCasError> {
    let mut groups = group_verified_objects_by_hash_and_length(&self.objects, &self.root)?;
    groups.retain(|group| group.objects.len() > 1);
    for group in &groups {
        require_archived_inactive_unreferenced(group, protection, references)?;
    }
    groups.sort_by(|left, right| left.content_hash.cmp(&right.content_hash));
    CasDedupePreview::new(groups)
}

pub fn apply(&mut self, request: &CasDedupeApply, protection: &SessionProtectionSnapshot, references: &RolloutReferenceIndex) -> Result<CasDedupeReceipt, ArchivedCasError> {
    let preview = self.preview(protection, references)?;
    if request.candidate_hash != preview.candidate_hash {
        return Err(ArchivedCasError::Mismatch);
    }
    let selected = preview.groups.into_iter().take(request.batch_limit).collect::<Vec<_>>();
    let next = repoint_archived_references_to_lexical_canonical(&self.objects, &selected)?;
    self.persist_index_atomically(&next)?;
    for redundant in redundant_blob_paths(&selected) {
        remove_verified_blob_only(&self.root, &redundant)?;
    }
    verify_canonical_blobs_and_rollout_bodies_unchanged(&self.root, &selected)?;
    self.objects = next;
    CasDedupeReceipt::from_applied(selected)
}
```

`require_archived_inactive_unreferenced` returns
`STORAGE_CAS_REFERENCE_ACTIVE` if any thread is active/current, any rollout
lineage references the object, or any checkpoint/lease reference exists. Hash,
length, index, lock, path, or immutable-state mismatch returns
`STORAGE_CAS_MISMATCH` and keeps every object. Apply does not create a CAS
object from rollout JSONL and does not delete a final canonical object.

- [ ] **Step 3: Bind CAS apply to the Plan 2 fence and receipt journal.**

```rust
let fence = self.ledger.writer_fence()?;
let references = RolloutReferenceIndex::scan(&self.codex_home)
    .await
    .map_err(|_| RetentionError::CasReferenceActive)?;
let protection = self.session_owners.snapshot()?;
let receipt = self.archived_cas.apply(&request, &protection, &references)
    .map_err(RetentionError::from)?;
self.ledger.commit_session_cas_receipt(&receipt, &fence)?;
```

The Host response strips all filesystem paths and thread IDs. It exposes
`candidate_hash`, `ledger_epoch`, `canonical_object_count`,
`redundant_object_count`, `reclaimed_bytes`, `code`, and `receipt_hash`.

- [ ] **Step 4: Run the CAS protection/commit tests and compile both affected crates.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-rollout cas_dedupe_never_selects_current_active_or_rollout_body --locked -j1; cargo test -p codex-rollout archived_equal_hash_cas_dedupe_commits_reference_transaction --locked -j1; cargo check -p codex-rollout --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: focused tests pass and both compile gates finish with no new warning.
The post-test fixture must prove both rollout bodies still exist byte-for-byte.

- [ ] **Step 5: Commit archived CAS dedupe.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rollout/src/archived_session_cas.rs codex-rs/rollout/src/archived_session_cas_tests.rs codex-rs/rollout/src/lib.rs codex-rs/rollout/src/rollout_reference_index.rs codex-rs/core/src/fast_lane_host_dispatch/source_session_retention.rs codex-rs/core/src/fast_lane_host_dispatch/source_session_retention_tests.rs; git commit -m 'feat: deduplicate protected archived session cas'; Pop-Location
```

### Task 7: Wire authenticated retention tools and final compile gates

**Files:**
- Modify: `mcp-tools/devkit_runtime/host_bridge.py:218-264,2298-2677`
- Modify: `mcp-tools/devkit_runtime/host_session.py:159-335,636-735`
- Modify: `mcp-tools/server.py:179-289,1008-1314`
- Modify: `mcp-tools/devkit_runtime/tool_metadata.py:5-23`
- Modify: `mcp-tools/tests/test_storage_retention.py`
- Modify: `mcp-tools/tests/test_mcp_contract.py:240-360`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs:35-240`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs:25-220`
- Modify: `codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs:412-570`

- [ ] **Step 1: Add RED tests for exact wire fields and destructive annotations.**

```python
def test_retention_tools_have_exact_safety_annotations():
    from devkit_runtime.tool_metadata import TOOL_ANNOTATIONS

    assert TOOL_ANNOTATIONS["source_cleanup_preview"] == (True, False, True, True)
    assert TOOL_ANNOTATIONS["source_cleanup_apply"] == (False, True, False, True)
    assert TOOL_ANNOTATIONS["session_cas_preview"] == (True, False, True, False)
    assert TOOL_ANNOTATIONS["session_cas_apply"] == (False, True, False, False)
```

- [ ] **Step 2: Add exact authenticated bridge schemas on Python and Rust sides.**

```rust
pub(crate) const SOURCE_CLEANUP_PREVIEW_SCHEMA: &str =
    "2718lab.storage.source-cleanup-preview.v1";
pub(crate) const SOURCE_CLEANUP_APPLY_SCHEMA: &str =
    "2718lab.storage.source-cleanup-apply.v1";
pub(crate) const SESSION_CAS_PREVIEW_SCHEMA: &str =
    "2718lab.storage.session-cas-preview.v1";
pub(crate) const SESSION_CAS_APPLY_SCHEMA: &str =
    "2718lab.storage.session-cas-apply.v1";
```

Each envelope uses the existing session ID, monotonic correlation ID, request
hash, size bound, replay cache, and single receiver. Require exact fields from
Task 2; reject unknown fields, duplicate correlation IDs, expired sessions,
and responses whose candidate/ledger/authorization binding differs from the
request. Absolute paths appear only inside the encrypted/authenticated private
request and never in public results.

- [ ] **Step 3: Register the four tools and project path-free receipts.**

```python
@mcp.tool(annotations=_tool_annotations("source_cleanup_apply"))
def source_cleanup_apply(request: dict[str, object]) -> dict[str, object]:
    from devkit_runtime.storage_retention import RetentionContractError, SourceCleanupApply
    try:
        exact = SourceCleanupApply.from_mapping(request)
    except RetentionContractError as error:
        return _failure(error.code)
    return _project_retention_receipt(_host_session().source_cleanup_apply(exact.to_wire()))


def _project_retention_receipt(value: object) -> dict[str, object]:
    allowed = {
        "code", "path_identity", "repository_identity", "commit", "tree",
        "candidate_hash", "ledger_epoch", "authorization_id",
        "remote_receipt_hash", "removed", "canonical_object_count",
        "redundant_object_count", "reclaimed_bytes", "receipt_hash",
    }
    if type(value) is not dict or not set(value) <= allowed:
        return _failure("INTERNAL_ERROR")
    return {key: value[key] for key in sorted(value)}
```

Register `source_cleanup_preview`, `session_cas_preview`, and
`session_cas_apply` by parsing their corresponding Task 2 type, passing
`parsed.to_wire()` to the same-named `HostSession` method, and returning
`_project_retention_receipt(response)`. Each catches
`RetentionContractError` and returns `_failure(error.code)`. No tool accepts a
recursive flag, wildcard, root expansion, force flag, or ordinary session ID.

- [ ] **Step 4: Run focused contract tests and compile-first gates.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery\mcp-tools'; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_storage_retention.py tests/test_mcp_contract.py -q -o cache_dir=G:\2718lab\_codex\.codex-task-temp\storage-113-retention-pytest; python -m py_compile devkit_runtime/storage_retention.py devkit_runtime/host_bridge.py devkit_runtime/host_session.py server.py; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2\codex-rs'; $env:CARGO_TARGET_DIR='G:\2718lab\_codex\.codex-task-temp\storage-113-retention-rust-target'; cargo test -p codex-rmcp-client source_cleanup_protocol --locked -j1; cargo test -p codex-rmcp-client session_cas_protocol --locked -j1; cargo check -p codex-rmcp-client -p codex-mcp --lib --locked -j1; cargo check -p codex-core --lib --locked -j1; Pop-Location
```

Expected: focused Python/Rust probes pass, Python compile is silent, and both
Rust checks finish with no new warning. Do not run the full workspace suite.

- [ ] **Step 5: Commit the bridge and public tool surface separately.**

```powershell
Push-Location 'G:\2718lab\_codex\.codex-task-temp\devkit-1.1.2-recovery'; git add mcp-tools/devkit_runtime/storage_retention.py mcp-tools/devkit_runtime/host_bridge.py mcp-tools/devkit_runtime/host_session.py mcp-tools/devkit_runtime/tool_metadata.py mcp-tools/server.py mcp-tools/tests/test_storage_retention.py mcp-tools/tests/test_mcp_contract.py; git commit -m 'feat: expose governed retention operations'; Pop-Location
Push-Location 'G:\2718lab\_codex\.codex-task-temp\codex-host-mcp-fix-recovery2'; git add codex-rs/rmcp-client/src/inherited_host_bridge_protocol.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/envelope.rs codex-rs/rmcp-client/src/inherited_host_bridge_protocol/session.rs; git commit -m 'feat: authenticate retention bridge operations'; Pop-Location
```

## Plan 3 acceptance gate and 1.1.3 completion

- [ ] Re-read the design sections “GitHub 可达性与精确路径授权”, “归档会话的 CAS 去重”, “稳定错误”, “迁移与回滚”, and “1.1.3 版本边界”; map every requirement and stable code to a task above.
- [ ] Run `git diff --check` in both worktrees and verify only the Plan 3 file map changed since the Plan 2 commits.
- [ ] Run the named DevKit `py_compile`, Host `cargo check -p codex-rollout --locked -j1`, `cargo check -p codex-rmcp-client -p codex-mcp --lib --locked -j1`, and `cargo check -p codex-core --lib --locked -j1` commands using the single retention target root.
- [ ] Record a source preview receipt, an unreachable/unpushed refusal, a missing-authorization refusal, an active-lease refusal, one explicitly authorized worktree removal receipt, a session-index activity receipt, a protected-current-session CAS refusal, and one archived duplicate-CAS receipt. Every apply receipt must bind candidate hash and ledger epoch.
- [ ] Verify source cleanup refuses the repository root, current worktree, protected branch, dirty tree, unpushed commit, mismatched GitHub identity, expired authorization, changed HEAD/tree, active/recovery/quarantined lease, and stale candidate without removing a path.
- [ ] Verify a saved rollout refreshes `session_index.jsonl` only after durable body flush, first persistence creates its missing YYYY/MM/DD directory, and an unused deferred session creates neither file nor directory.
- [ ] Verify CAS dedupe never scans or removes `sessions/**`, current/active rollouts, ordinary archived `.jsonl`/`.jsonl.zst` bodies, final canonical blobs, unknown files, or objects with active checkpoint/lease/rollout references.
- [ ] Run one controlled Host production receipt flow for each apply operation. A network, stat, lock, ledger, authorization, or postcheck failure is a protected refusal; it never expands the candidate set or invokes force deletion.

Plan 1, Plan 2, and Plan 3 together complete 1.1.3. They do not authorize a
local deletion merely because a commit was pushed, do not delete ordinary
Codex conversations, and do not add remote session synchronization,
compression, full-disk scanning, cross-machine target sharing, or any Fast
Lane route/lease/context/capability fallback.
