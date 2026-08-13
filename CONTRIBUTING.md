# Contributing to 2718lab DevKit

Thanks for improving the MCP runtime, its contracts, and the compact Skill
manuals. This repository is Codex-first: contributions must not add a second
executable host runtime, prompt-agent surface, credentials, or unchecked local
state. Skills remain concise documentation manuals rather than a second runtime.

## Before opening a pull request

1. Start from current `main` and keep one owner per mutable write scope.
2. Describe the smallest observable contract change and its failure boundary.
3. Add a focused failing test first, implement the smallest change, then show
   the relevant GREEN checks. Do not claim a broad suite that did not run.
4. Keep caches, task data, worktrees, host receipts, and quota samples outside
   the repository. Remove private paths and secrets from fixtures and logs.
5. Update the README, contract, or changelog when public behavior changes.

## Pull-request review

Use the PR template. CI and CodeQL are required evidence; automated reviewers
are advisory. Changes to lifecycle fencing, host bridges, storage ownership,
allowlists, or release automation need an explicit maintainer review.

## Release process

Only a maintainer dispatches **Release** from current `main`. The workflow
validates the exact main commit and release metadata, re-runs the MCP, Fast
Lane, and primary-artifact gates, creates an annotated tag only after those
gates pass, and then creates the GitHub Release. Do not push release tags to
publish a release.

See [repository automation](docs/governance/repository-automation.md) for the
configuration boundary and external app setup.
