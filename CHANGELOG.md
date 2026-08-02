# Changelog

All notable changes to 2718lab DevKit are documented here.

The project follows Keep a Changelog conventions. Tagged versions are
published by the repository release workflow after the CI and artifact checks
pass.

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
