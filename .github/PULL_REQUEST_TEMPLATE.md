## Summary

<!-- What problem does this change solve, and what user-visible behavior changes? -->

## Changes

<!-- List the important files, contracts, or workflows changed. -->

## Integration boundary

<!-- State the exclusive write scope, task/lease identity, G: task root, and
the independent-review evidence required before the coordinator integrates. -->

<!-- Scheduler changes record `2718lab-devkit/scheduler-topology-v1` and
opaque plan/lease/worktree identities. -->

## Commit and evidence binding

- Candidate commit:
- Base commit:
- Integration commit:
- Review evidence (receipt/link and reviewer):
- Verification evidence (commands, result, and receipt/link):

## Retention, rollback, and cleanup evidence

- Retention strategy and required `x` consecutive post-merge accepted integration rounds:
- Evidence that every required round was explicitly accepted by the coordinator:
- No-rollback evidence for those rounds:
- Fresh host recheck and its receipt/link:
- Cleanup eligibility/candidate evidence (if any):
- [ ] I understand that an unverified or merely observed result is not an accepted round.
- [ ] I understand that a cleanup candidate is eligibility only, not deletion authorization; no automatic deletion is permitted.
- [ ] If no retention strategy is declared, the candidate/material remains permanently retained.

## Validation

- [ ] `uv lock --check` passes for every changed Python project.
- [ ] The configured Ruff checks and format checks pass without modifying the worktree.
- [ ] The configured type checks and affected pytest suite pass.
- [ ] MCP contract/stdio behavior was checked when the runtime or server changed.
- [ ] The allowlisted artifact was built and inspected when packaging changed.
- [ ] Logs, screenshots, and examples contain no credentials or private paths.

## Checklist

- [ ] This change is not breaking, or the breaking contract is documented.
- [ ] CHANGELOG/README/docs are updated when public behavior changes.
- [ ] No runtime state, caches, credentials, or unrelated files are committed.
- [ ] Parallel A1/A2/A3 work used disjoint write scopes and independent G: task roots.
- [ ] Scheduler topology V1 used A/B/C, a maximum 1:3 scheduler-to-writer ratio, and host-slot-gated read-only design/prewarm work.
- [ ] Any cross-scope change used a declared-child split that strictly reduced conflict, or was recorded as UNSPLITTABLE.
- [ ] The coordinator integrated this change only after PR-style independent review.
- [ ] I have reviewed the repository's [contributing guidance](../CONTRIBUTING.md) when present.
