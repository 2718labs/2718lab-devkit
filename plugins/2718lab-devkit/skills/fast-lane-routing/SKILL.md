---
name: fast-lane-routing
description: Reference manual for reading a compiled 2718lab Fast Lane plan, explicit routing fields, context fences, and terminal refill rules. Use only for scoped DevKit guidance; it never dispatches agents or creates sessions.
---

# Fast Lane Routing Manual

Read a plan as a bounded host contract: model, effort, route, worktree, lease,
context, and predecessor bindings are exact values, not defaults to infer.

- A missing, altered, or unattested route is \`NO_SAFE_WORK\`.
- A missing first index snapshot in a newly opened project is a cold-start
  precondition, not a route verdict. Follow `workflow-design`'s one-time
  `project_index_register` -> `project_index_sync` sequence before compiling or
  declaring a degraded mode.
- Cross-session projection is host-owned; the manual never opens a session.
- \`index_context\` is bounded evidence, not an invitation to poll or rescan.
- Refill follows a validated terminal event only; retained work stays retained.

See \`mcp-tools/devkit_fastlane/FASTLANE_CONTRACT.md\` and its references for the
complete schema and executable compiler boundary.
