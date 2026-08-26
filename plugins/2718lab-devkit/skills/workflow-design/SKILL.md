---
name: workflow-design
description: "Reference manual for designing a bounded 2718lab DevKit workflow: scope, dependencies, one-writer boundaries, verification, and completion conditions. Fast Lane is the default policy; this manual does not dispatch sessions or run commands."
---

# Workflow Design Manual

Use Fast Lane as the default planning policy for scoped DevKit work. Record the
scope, ambiguity, risk, independent write count, verification cost, blocker,
and available capacity before choosing a lane.

For a newly opened project that has no known workspace id or first index
snapshot, initialize the local index before deciding that the plugin is
degraded: call `project_index_register` once with the current canonical project
root, then call `project_index_sync` once with the returned opaque workspace id.
A successful first snapshot is normal cold start, even when the project already
contains source, README, or configuration files. Do not call
`project_index_status` before the first sync and do not report
`DEGRADED_SKILL_ONLY` merely because a new project did not already have an
index. Degrade only when the MCP tool is unavailable or register/sync fails;
preserve that failure as evidence and do not guess an index identity.

- Keep writers disjoint; prewarm and audit roles are read-only.
- Treat compiler-selected model, effort, worktree, lease, and context as
  host-owned facts rather than prose suggestions.
- Refill only after validated terminal evidence; a disconnect or stale context
  is not completion.
- Keep temporary files and worktrees inside the approved task root.

This is a planning manual. The bounded compiler and MCP lifecycle contracts are
documented in \`mcp-tools/devkit_fastlane/\`.
