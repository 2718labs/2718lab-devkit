# Changelog

All notable changes to 2718lab DevKit are documented here.

The project follows Keep a Changelog conventions. A maintainer dispatches the
repository release workflow from current `main`; it creates a new annotated tag
only after the CI and artifact checks pass.

## [Unreleased]

## [1.1.5] - 2026-09-05

### Added

- Added the model-neutral `fast-lane-request-v2` / `work-package-v3` /
  `fast-lane-plan-v3` path. New assignments describe complexity, capability,
  effort, and cost requirements while the coordinator chooses from the model
  IDs and efforts exposed by the current Codex dispatch tool.
- Added public request preparation plus model-selection record helpers. A
  selection record binds the exact model ID, effort, reason,
  `requirement_hash`, and `plan_item_id` without claiming dispatch or model
  availability.

### Fixed

- Future bounded model IDs, including an explicitly requested `gpt-6-astra`,
  no longer require a DevKit model-name whitelist. Explicit model/effort intent
  is hash-bound and cannot be silently replaced by a fallback route.
- Reject mixed request/work-package versions in both directions; request-v1 is
  paired with work-package-v2 and request-v2 with work-package-v3.

### Security

- Model-neutral plans remain `plan_only`, `not_dispatched`, and unauthorized;
  the actual dispatch tool is the only availability gate and worker `ultra`
  remains forbidden. Legacy v3/v4/v5 routing policy assets and legacy
  request-v1/plan-v2 replay behavior are unchanged.

## [1.1.4] - 2026-09-04

### Added

- Added a hostless, compile-only MCP Fast Lane planning path. Caller-supplied
  workspace and snapshot identifiers remain selectors: a read-only local
  RuntimeRoot resolves the registered workspace, requires the exact current
  `INDEX_READY` snapshot and Git binding, and emits a bounded, exact-key
  `team-efficiency/fast-lane-plan-v2` descriptor.

### Fixed

- Normalize the no-null public plan before calculating assignment and plan
  identities, so `plan_item_id` and `plan_hash` can be recomputed exactly from
  inactive plans and read-only as well as writer assignments.
- Fail closed with the snapshot's precise stable error whenever local planning
  observes any Project Index state other than `INDEX_READY`.

### Security

- Plan-v2 is planning data only: it fixes `plan_only=true`,
  `dispatch_state="not_dispatched"`, and `execution_authorized=false`. It does
  not spawn an agent, claim a lease, create a worktree, mutate Git, or dispatch
  work; those actions remain outside the compiler contract.

## [1.1.3] - 2026-09-01

### Added

- Added a canonical, exact-key, path-free storage-intent contract for Cargo
  targets, Python caches, MCP packages, and Fast Lane task storage. Intent
  hashes bind task, plan, context, byte/file budgets, and the complete target
  descriptor without allowing DevKit to choose a filesystem path or claim a
  storage lease.
- Added DevKit-side storage profile/admission validation and compiler-proof
  plumbing for compatible Hosts. Budgets participate in wave/profile evidence
  and intent proofs can be privately bound, while the legacy eight-field
  pre-Host skeleton and public dispatch batch remain free of storage intents;
  production worker admission is not activated by this repository.

### Fixed

- Hardened verified legacy runtime-store migration so physical and semantic
  schema shape, metadata, content addresses, acceptance identities, and exact
  Atlas outbox/finalization bindings are checked before current DDL can run.
  Schema drift, null/orphan rows, half-upgraded state, or content-address
  mismatch fail closed.
- Bounded storage-admission frames, deadlines, cancellation, and active bridge
  I/O teardown; zero free-space floors and mismatched or unknown receipt fields
  are rejected before a storage decision can be consumed.

### Security

- On an ordinary Host, the missing compatible private profile/authority
  exchange keeps budgeted Fast Lane fail-closed with
  `FASTLANE_HOST_AUTHORITY_UNAVAILABLE`/`NO_SAFE_WORK`. DevKit provides the
  path-free intent and private protocol, but does not authenticate or provision
  the Windows protected broker, create its root, activate cleanup, or provide a
  local writer fallback.
- Compatible, buildable Host source exists only on Ayleovelle's user-fork
  [`codex/host-1.1.3-storage-governance-upstream`](https://github.com/Ayleovelle/codex/tree/codex/host-1.1.3-storage-governance-upstream)
  branch, fixed at immutable commit
  [`c3dde23bec21c45d10740f2eec09d9a1b87cd329`](https://github.com/Ayleovelle/codex/commit/c3dde23bec21c45d10740f2eec09d9a1b87cd329).
  This identifies the separate Host source boundary only: it is not merged into
  OpenAI upstream and is not shipped or activated by this plugin. Stock Codex
  Hosts still fail closed, and final protected-broker compile, probe, and
  runtime receipts remain Host-side release evidence rather than DevKit claims.

## [1.1.2] - 2026-08-27

### Fixed

- Replaced inherited numeric Windows host-bridge handles with a strict local
  named-pipe selector bound to the exact launcher PID and process creation
  FILETIME while preserving the Unix inherited-FD contract. Untagged,
  path-like, remote, malformed, PID-mismatched, or creation-mismatched selectors
  now fail closed before any session key is sent.
- Treat a newly opened project without an index as normal cold start: initialize
  it with one bounded `project_index_register -> project_index_sync` sequence
  before considering degraded mode, including when README, configuration, or
  source files already exist.
- Preserve the host-attested initial entry count through bootstrap and require
  the synchronized manifest and entry count to match exactly before indexed
  recompilation; registration, synchronization, identity, or attestation
  failures remain fail-closed.

## [1.1.1] - 2026-08-25

### Fixed

- Added conservative local project-index retention with protected-snapshot
  revalidation, oldest-first bounded batches, and orphan cache/blob collection
  so reproducible historical index data can be released safely.
- Added explicit index compaction with observed before/after file sizes; legacy
  databases require an opt-in, free-space-checked rewrite before incremental
  vacuuming is enabled.
- Activated fresh local Atlas runtime initialization and repaired the accepted
  recipe matching loop so durable knowledge can be reused after reopening.
- Kept project indexes local-only while documenting the future
  server-authoritative Atlas boundary.
- Added a dedicated marketplace artifact boundary that includes the Codex Skill
  manuals, so remote installs expose documented workflows without widening the
  runtime-only primary release package.

## [1.1.0] - 2026-08-23

### Changed

- Documented G-drive local task roots, disjoint multi-writer scopes, and the
  PR-style independent-review integration boundary.
- Removed the quota-coordinator contract from the shipped Fast Lane guidance.
- Clarified host-attested independent child routing, bootstrap-only empty
  project handling, and candidate-only cleanup eligibility after exhausted
  rollback rounds.

### Changed

- Removed the retired AstrBot extension, its dedicated manual, and its
  packaging/release/CI paths. DevKit is now one Codex-first product with an
  MCP runtime and a documentation-only Skill manual bundle.
- Replaced tag-push publication with a maintainer-dispatched release gate that
  validates current `main`, creates an annotated tag only after its gates, and
  can safely resume an exact tag that has no release or only a draft release.
- Added repository governance files, CODEOWNERS, CodeQL, and checked-in Gemini
  Code Assist review policy. Dosu size labeling is documented as its external
  GitHub App integration so it remains a single label writer.

## [1.0.0] - 2026-08-13

### Added

- Added verified Continuity replay and typed frozen-view recovery, including
  deterministic receipt, pointer, and CAS verification before replayed Atlas
  projection is allowed.
- Added release-grade project-index package pagination and package-bound MCP
  selectors, with the locked 17-tool public surface retained.
- Added canonical project-fence and host-admission handoff contracts for a
  future Desktop-attested vNext data plane.

### Changed

- Promoted the MCP-only primary package from RC4 to the first stable `1.0.0`
  release.
- Added an explicit `CODEX_DEVKIT_DATA_ROOT` override so the installed runtime
  can keep its project-scoped index on a durable non-C drive without reusing a
  legacy `PLUGIN_DATA` database.
- Public Fast Lane compilation and bootstrap are intentionally authority-inert
  until a Desktop host supplies a private, project-attested admission grant;
  public calls return a zero-assignment fail-closed preview rather than
  selecting a project from caller input.

### Fixed

- Hardened Windows-native Continuity CAS traversal, publication, cleanup, and
  cross-store finalization primitives against reparse, stale-fence, journal,
  and recovery races.
- Normalized future strict-admission failure handling to the path-neutral
  `PROJECT_AUTHORITY_UNAVAILABLE` public envelope without exposing internal
  provider details.

### Security

- Tightened immutable replay, receipt, command, and physical SQLite proof
  checks; invalid or unavailable evidence fails closed without falling back to
  live workspace authority.
- Kept Desktop project-registry issuance and vNext project-bound writes
  disabled in this repository until the external host contract exists.

## [1.0.0-rc4] - 2026-08-03

### Fixed

- Normalized the Fast Lane source and regression-test formatting with the
  Ruff 0.16.1 version enforced by the tag-driven release gate. This is a
  formatting-only release correction with no runtime behavior change.

## [1.0.0-rc3] - 2026-08-03

### Added

- Added bounded Spark/Luna/main-pool routing, index-evidence bindings, private
  Host-session envelopes, and durable external-bootstrap descriptors.

### Changed

- Bound opaque compiler evidence to session and quota validity; external
  bootstrap persistence now uses schema v10 content commitments and composite
  bindings.

### Fixed

- Removed reachable compiler marker material and reject forged, expired, or
  unattested Host intent. The primary artifact now ships every versioned Fast
  Lane policy asset required at runtime.

### Security

- Host execution remains a fail-closed preview: without a Desktop Host-private
  verifier, no external session, worktree, or automatic cross-session dispatch
  is authorized.

## [1.0.0-rc2] - 2026-08-03

### Added

- Added a tag-driven release gate that rechecks the primary-artifact contract
  and Windows Fast Lane contracts before publishing.
- Added a Windows MCP runtime gate with the same task-local environment
  contract used by CI; Linux continues to validate AstrBot and the primary
  artifact contract.
- Added retained GitHub Actions artifacts and checksums for both the primary
  MCP ZIP and the independently buildable AstrBot wheel and source
  distribution.

### Changed

- Bumped the MCP-only plugin and AstrBot companion package together to RC2.
- Reduced the optional Codex skill surface to concise module manuals; runtime
  dispatch, policy, and validation remain in the MCP package.
- Pinned release-workflow Actions to audited full commit SHAs and prevented the
  publish checkout from persisting its token.

### Fixed

- Marked release-candidate tags as GitHub prereleases rather than ordinary
  releases.
- Bound release metadata validation to the AstrBot package version as well as
  the primary plugin and MCP runtime versions.

## [1.0.0-rc1] - 2026-08-02

### Added

- Shipped the canonical MCP-only stdio runtime with an exact 17-tool public
  surface covering Project Index, Checkpoints, Atlas, and Relay.
- Added the independently runnable, locked Python runtime and deterministic
  allowlisted primary-artifact builder.
- Added bounded tool-result envelopes, opaque workspace identity, scoped
  checkpoints, host-attested Relay lifecycle actions, and evidence-bound
  recovery.
- Added deterministic Fast Lane difficulty routing, quota admission, terminal
  refill, read-only prewarm, explicit host-dispatch/index packets,
  compiler-owned cross-session projections, and fail-closed model/effort
  attestation.

### Changed

- Removed the legacy mutating Project and Checkpoint constructor paths from the
  canonical runtime and migrated callers to workspace-scoped services.
- Kept the primary MCP ZIP free of prompts, resources, static agents, the
  optional skill bundle, external CodeGraph, network services, and model
  runners.
- Restricted primary host configuration to the two bridge selector names:
  CODEX_DEVKIT_HOST_BRIDGE_FD and CODEX_DEVKIT_HOST_BRIDGE_HANDLE.

### Fixed

- Rejected stale, forged, cross-workflow, duplicate, or unbound lifecycle
  receipts and assignment contexts.
- Made missing host capability fail closed instead of falling back to an
  unrelated local start.
- Preserved deterministic artifact output and protocol-clean stdio behavior.

### Verification

- Release gates run the full integrated regression and fresh-artifact stdio
  checks. Published CI is the source of truth for live test counts.
- Fresh-artifact stdio checks verify exact 17 tools, zero prompts/resources,
  normal and error calls, missing-host capability rejection, and source-checkout
  independence.
