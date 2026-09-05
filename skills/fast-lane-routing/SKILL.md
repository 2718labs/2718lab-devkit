---
name: fast-lane-routing
description: Reference manual for reading a compiled 2718lab Fast Lane plan, explicit routing fields, context fences, and terminal refill rules. Use only for scoped DevKit guidance; it never dispatches agents or creates sessions.
---

# Fast Lane Routing Manual

For new work, prepare `fast-lane-request-v2` with
`prepare_model_neutral_fast_lane_request`; it pairs only with
`work-package-v3`. Read its plan-v3 assignments as requirements, not a model
registry:

- Use `route_requirements` to judge complexity, capability, effort, and cost.
- Choose from the model IDs and supported efforts listed by the current
  `collaboration.spawn_agent` tool metadata. Routine work should stay
  cost-conscious; design, research, and exceptional cross-module work should
  use the strongest suitable current model at its highest worker-safe effort.
- `selection.state="unselected"` is a normal planned state. Call
  `record_model_selection` with the exact chosen model ID, effort, and reason,
  then pass that exact route to the dispatch tool. The record does not claim
  availability or dispatch.
- An `explicit_intent` with `state="required"` is non-substitutable. If the
  current tool rejects it, report the route unavailable; never silently fall
  back to another model.
- Model IDs are bounded strings, not a product whitelist. Do not add a model
  name to DevKit routing tables merely because Codex introduces it.

Legacy request-v1/plan-v2 records remain exact replay contracts: their model,
effort, route, worktree, lease, context, and predecessor bindings are not
defaults to reinterpret.

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
