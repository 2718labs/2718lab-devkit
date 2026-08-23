## Summary

<!-- What problem does this change solve, and what user-visible behavior changes? -->

## Changes

<!-- List the important files, contracts, or workflows changed. -->

## Integration boundary

<!-- State the exclusive write scope, task/lease identity, G: task root, and
the independent-review evidence required before the coordinator integrates. -->

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
- [ ] The coordinator integrated this change only after PR-style independent review.
- [ ] I have reviewed the repository's [contributing guidance](../CONTRIBUTING.md) when present.
