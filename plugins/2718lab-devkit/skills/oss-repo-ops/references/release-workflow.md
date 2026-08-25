# Versioning and release workflow

This is a repository-operations reference, not authority to publish. Remote
tags, releases, or package uploads always need an explicit maintainer action.

## Version boundaries

- **PATCH** fixes a compatible public behavior defect.
- **MINOR** adds a compatible capability or optional configuration.
- **MAJOR** removes or changes a public tool, schema, lifecycle guarantee, or
  supported host boundary in a way existing users cannot keep using unchanged.
- Prerelease versions use a hyphenated suffix such as `1.2.0-rc1`; the Python
  package may use its PEP 440 equivalent `1.2.0rc1`.

Before release, the plugin manifest, Python project, CHANGELOG entry, and
release channel must agree. Do not rewrite an existing tag to repair drift:
create a corrective commit and a new tag instead. The only retry exception is
an exact annotated tag created by this workflow whose GitHub Release is absent
or still a draft after a transient publication failure.

## Maintainer-dispatched release

This repository's `.github/workflows/release.yml` is dispatch-only. A
maintainer selects current `main`, a new `vMAJOR.MINOR.PATCH[-suffix]` tag, and
the matching `prerelease` or `production` channel. The workflow then:

1. verifies that the selected SHA is the current remote `main` SHA;
2. validates version/changelog coupling and rejects unrelated or published
   existing tags;
3. re-runs MCP runtime, Fast Lane, and primary-artifact gates;
4. creates an annotated immutable tag only after those gates are green; and
5. creates a draft release, uploads the allowlisted ZIP and checksum, then
   publishes it; an exact draft can be resumed safely if upload is interrupted.

A pushed tag alone is intentionally inert. This prevents an unreviewed branch
tag or a stale commit from publishing a release.

## Evidence to record

Record the merge commit, release-workflow URL, tag, release URL, artifact
SHA256, and the exact tests that passed. Treat a cancelled or unavailable CI
run as no evidence, not a successful release.
