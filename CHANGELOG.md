# Changelog

All notable changes to 2718lab DevKit are documented here.

The project follows Keep a Changelog conventions. A maintainer dispatches the
repository release workflow from current `main`; it creates a new annotated tag
only after the CI and artifact checks pass.

## [Unreleased]

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
