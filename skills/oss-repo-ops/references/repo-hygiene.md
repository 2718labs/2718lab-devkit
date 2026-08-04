# Repository hygiene

This is engineering guidance, not legal advice. Ask qualified counsel for a
license or compliance question that turns on a specific jurisdiction or
dependency relationship.

## Public repository baseline

- Keep `README.md` as the short product map and link details rather than
  duplicating every contract.
- Keep `CHANGELOG.md` factual: document user-visible changes and preserve old
  entries as history instead of retroactively rewriting releases.
- Keep `LICENSE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and `SECURITY.md`
  current and mutually consistent.
- Keep `.github/` workflows, CODEOWNERS, issue forms, and review configuration
  explicit. Repository rulesets and third-party GitHub Apps are configured
  separately; do not pretend a markdown file can enforce remote policy.
- Never commit credentials, private host handles, durable runtime state,
  workspace receipts, raw quota samples, personal filesystem paths, or caches.

## Pull-request hygiene

Every pull request should state the behavior change, write scope, validation,
and any breaking impact. Keep generated artifacts out unless the project
explicitly versions them. Review workflow changes like code: they can alter
release authority and require stronger evidence than a prose-only change.

## Automation hygiene

Use one authority for a mutable automation output. For example, if Dosu owns
size labels, do not add a second label workflow that races it. Treat Gemini or
other automated review comments as advisory, never as a substitute for CI,
CODEOWNERS, or a responsible maintainer.
