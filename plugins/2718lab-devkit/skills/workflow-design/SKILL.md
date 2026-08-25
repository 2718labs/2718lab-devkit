---
name: workflow-design
description: "Reference manual for designing a bounded 2718lab DevKit workflow: scope, dependencies, one-writer boundaries, verification, and completion conditions. Fast Lane is the default policy; this manual does not dispatch sessions or run commands."
---

# Workflow Design Manual

Use Fast Lane as the default planning policy for scoped DevKit work. Record the
scope, ambiguity, risk, independent write count, verification cost, blocker,
and available capacity before choosing a lane.

- Keep writers disjoint; prewarm and audit roles are read-only.
- Treat compiler-selected model, effort, worktree, lease, and context as
  host-owned facts rather than prose suggestions.
- Refill only after validated terminal evidence; a disconnect or stale context
  is not completion.
- Keep temporary files and worktrees inside the approved task root.

This is a planning manual. The bounded compiler and MCP lifecycle contracts are
documented in \`mcp-tools/devkit_fastlane/\`.
