# 2718lab DevKit review guide

Review the repository as a bounded, evidence-first MCP system.

- Treat host attestations, workspace identity, leases, receipts, quota data,
  and terminal lifecycle evidence as security boundaries. Do not recommend a
  fallback that weakens fail closed behavior.
- Flag a change that starts work, refills a lane, recovers a task, or accepts a
  result without a validated predecessor/context/token binding.
- Prefer narrow, deterministic changes. Require focused test coverage for
  changed contracts and flag a missing RED-to-GREEN proof for behavior changes.
- Flag accidental cross-project state, private host paths, credentials, raw
  quota samples, mutable caches, or runtime evidence committed to source.
- Review package allowlists, version/changelog coupling, and release workflow
  changes as release-critical. Workflows still require human review because
  the GitHub integration does not comment on workflow files.
- Treat bot output as advisory evidence: it never replaces an accountable
  maintainer, CI, or the repository's release gate.
