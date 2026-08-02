# Local GitHub-Style Parallel Integration

This is a local Git collaboration protocol. It does not authorize a remote
push, remote pull request, network call, or release publication; a remote push
remains an explicit later action.

## Required flow

`task card + base revision -> isolated task branch/worktree -> scoped commit + evidence -> Sol review -> ordered integration/rebase -> CI gate -> release gate`

Each active task has one owner and an exact write scope. Parallel execution is
allowed only when active scopes are disjoint. A same-path or parent/child-path
conflict queues behind the current owner. A worker must not overwrite a changed
path outside its card, must not merge another task, must not rebase another
task, or claim an unreviewed candidate branch is accepted.

The integration record names the candidate/source commit, base revision,
accepted evidence hash, integration order, and Sol review. The work-package
validator exposes `validate_parallel_integration_record` for these shape and
scope checks.

## Durable MCP handoff

Task context, evidence, handoff, and acceptance are durable artifacts rather
than direct-chat payloads. Use this exact sequence:

`workflow_artifact_register -> workflow_message_send -> workflow_inbox -> workflow_artifact_resolve -> workflow_message_ack`

The registered artifact is immutable, task-owned, and redacted as needed. The
message carries only bounded metadata, delivery ids, correlation ids, and an
artifact hash. `collaboration.send_message` may wake a bound worker after
delivery, but direct chat is not the source of truth and does not grant scope,
lease, acceptance, or repository access.

## Interface-first contract handoff

As soon as a public task interface freezes, the producer registers a small
redacted contract artifact and delivers only its hash plus minimal kind metadata
through the existing MCP mailbox. The receiver resolves and acknowledges that
artifact in the durable order above; chat is only a wake-up hint. Downstream
work may begin against the contract before the producer's complete branch is
integrated. The contract record is validated as `artifact_kind=contract`, one
artifact hash, `metadata.kind=contract`, and the ordered mailbox sequence.

## Crash-resume packet

Persist a bounded, redacted crash-resume packet for each active task. It carries
workflow/task identity, lease epoch, current endpoint, base and candidate commit
ids, branch/worktree identifier, write-scope hash, latest RED/GREEN command and
result summary, registered contract/evidence hashes, and one explicit next
action. It must not contain raw stdout/stderr, credentials, source bodies,
environment values, or unbounded chat history.

After a host/model crash, the coordinator rebinds the endpoint and the
replacement worker follows this exact recovery order:

`workflow_endpoint_bind -> workflow_inbox -> workflow_artifact_resolve -> workflow_message_ack -> recorded next action`

The replacement resumes from the recorded next action instead of restarting.
The packet validator rejects unknown or unsafe fields, unbounded summaries and
hash lists, missing redaction, or an out-of-order recovery sequence.

## Verification lanes and release gate

Every candidate records three named verification lanes:

- `core`: scoped contract/security tests, lint/format, compile, diff/scope,
  and secret/privacy checks. A core failure blocks acceptance.
- `extended`: broader regressions and compatibility fixtures. Under an explicit
  timebox, a non-critical failure may be deferred only with exact evidence,
  owner, and release gate.
- `platform`: OS/runtime-specific races and hooks. Local skips are honest
  skips, never represented as passes; claimed platform support requires its
  lane before release.

The release gate also requires English-first bilingual documentation. A local
integration record may not turn a failed or skipped lane into a pass.
