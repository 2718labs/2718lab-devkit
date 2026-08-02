# Changelog

All notable changes to 2718lab DevKit are documented here.

The project follows Keep a Changelog conventions. Tagged versions are
published by the repository release workflow after the CI and artifact checks
pass.

## [1.0.0-rc1] - 2026-08-02

### Added

- Shipped the canonical MCP-only stdio runtime with an exact 16-tool public
  surface covering Project Index, Checkpoints, Atlas, and Relay.
- Added the independently runnable, locked Python runtime and deterministic
  allowlisted primary-artifact builder.
- Added bounded tool-result envelopes, opaque workspace identity, scoped
  checkpoints, host-attested Relay lifecycle actions, and evidence-bound
  recovery.
- Added deterministic Fast Lane difficulty routing, quota admission, terminal
  refill, read-only prewarm, and fail-closed model/effort attestation.

### Changed

- Removed the legacy mutating Project and Checkpoint constructor paths from the
  canonical runtime and migrated callers to workspace-scoped services.
- Kept the primary MCP package free of prompts, resources, static agents,
  skill aggregation, external CodeGraph, network services, and model runners.
- Restricted primary host configuration to the two bridge selector names:
  CODEX_DEVKIT_HOST_BRIDGE_FD and CODEX_DEVKIT_HOST_BRIDGE_HANDLE.

### Fixed

- Rejected stale, forged, cross-workflow, duplicate, or unbound lifecycle
  receipts and assignment contexts.
- Made missing host capability fail closed instead of falling back to an
  unrelated local start.
- Preserved deterministic artifact output and protocol-clean stdio behavior.

### Verification

- Full integrated regression: 1037 passed, 13 skipped, 40 subtests passed.
- Fresh-artifact stdio checks: exact 16 tools, zero prompts/resources, normal
  and error calls, missing-host capability rejection, and source-checkout
  independence.
