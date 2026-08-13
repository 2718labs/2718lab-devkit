# Repository automation and review

This repository uses a small, auditable automation surface. Workflow files are
part of the release boundary and require human review even when a bot is
enabled.

## Checked-in controls

| Control | Repository file | Effect |
| --- | --- | --- |
| CI | `.github/workflows/ci.yml` | validates MCP runtime, Fast Lane contracts, and the allowlisted package on `main` and pull requests |
| CodeQL | `.github/workflows/codeql.yml` | scans Python on pull requests, `main`, and a weekly schedule |
| Dependency review | `.github/workflows/dependency-review.yml` | rejects pull-request dependency changes with high-severity advisories after GitHub Dependency Graph is enabled |
| Ownership | `.github/CODEOWNERS` | maps every active path to the accountable maintainer |
| Gemini review policy | `.gemini/config.yaml`, `.gemini/styleguide.md` | configures severity, automatic review, and DevKit-specific review criteria without credentials |
| Manual release | `.github/workflows/release.yml` | only creates a tag and GitHub Release after a maintainer dispatches it from current `main` and all release gates pass |

The release workflow never publishes merely because a tag was pushed. It
validates the selected commit against remote `main` before and after its gates,
then creates an annotated tag only after validation succeeds. It re-fetches and
revalidates the tag's annotated object and target commit immediately before
creating or resuming the draft release. It rejects an unrelated or published
existing tag, while an exact annotated tag with no release or a draft release
can be resumed safely after a transient failure. Checkout credentials are not
persisted through artifact construction; write credentials are supplied only to
the tag and GitHub Release steps.

## Required repository settings

GitHub rulesets cannot be enforced from a committed workflow. A repository
administrator must protect `main` with pull-request review, required status
checks (`CI` and `CodeQL`), CODEOWNERS review where appropriate, and blocked
force pushes. Protect the `prerelease` and `production` environments used by
the Release workflow before dispatching releases. In **Settings → Advanced
Security**, enable **Dependency Graph** before requiring the `Dependency review`
check; GitHub otherwise reports that the action is unsupported for the
repository.

## Dosu size labels

Dosu is configured through its GitHub App and Dosu Agent settings, not through
a repository secret or workflow. Install it with access limited to this
repository, then enable **Auto-Labeling → Add Size Labels**. It owns and
maintains `size:XS`, `size:S`, `size:M`, `size:L`, `size:XL`, and `size:XXL`
from meaningful pull-request line changes. Do not add a second local size-label
workflow: two writers would race and produce misleading labels.

## Gemini Code Assist review

The checked-in `.gemini` files are intentionally credential-free. A Google
Cloud/GitHub administrator must still connect this repository through Developer
Connect and install the Gemini Code Assist GitHub App. Once linked, the bot can
review pull requests automatically and can be requested with `/gemini review`.
Its comments are advisory; CI and a CODEOWNER remain the merge authority.

Gemini does not provide review comments for `.github/workflows/**`; use human
review and CodeQL/CI results for workflow changes.

## Safe release checklist

1. Merge the version, manifests, changelog, and implementation to `main`.
2. Confirm the `main` CI and CodeQL runs are green.
3. Dispatch **Release** from current `main`, select the exact new tag and the
   matching channel.
4. Wait for all validation jobs and the protected environment approval.
5. Verify the immutable annotated tag, GitHub Release, primary ZIP, and SHA256
   asset before announcing the release.
