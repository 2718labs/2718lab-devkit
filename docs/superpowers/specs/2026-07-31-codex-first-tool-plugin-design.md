# 2718lab DevKit Codex-first Tool Plugin Design

**Status:** approved design, pending written-spec review

**Date:** 2026-07-31

**Scope:** the primary Codex plugin, its local Python stdio MCP backend, and
the durable local state needed by Atlas and Relay.

## 1. Decision

`2718lab DevKit` is a Codex-first, tool-first plugin. Its product surface is a
small local MCP backend, not a prompt catalog, a skill bundle, or a generic
agent platform.

- **Atlas** is the deterministic code-knowledge, implementation-context, and
  durable acceptance-evidence service.
- **Relay** compiles bounded work into a durable dispatch state and tells the
  Codex host which agents to start or refill.
- Python persists and validates state; the main Sol conversation consumes
  structured `host_actions` and alone calls Codex `spawn_agent`.
- The Python process must never import, invoke, emulate, or proxy a Codex
  agent-spawn API.

The primary plugin contains no MCP prompts, `defaultPrompt`, static prompt
agents, `Runner`, `OpenAIRunner`, or equivalent model-runtime abstraction.
It also does not present itself as a skill aggregation product. Existing
skills, commands, and Claude-facing material move to optional development
documentation or extension packages and are not primary-plugin capabilities.
AstrBot remains an optional template/validator extension and is never a
runtime, startup, lifecycle, or data-storage dependency.

## 2. Architecture

```text
Codex main Sol
  -> local stdio MCP server
     -> Atlas: code graph, packets, durable acceptance projection
     -> Relay: deterministic plan, workflow, task, lease, dispatch state
     -> project index and Bugkiller: bounded supporting services
  <- stable JSON envelopes and structured host_actions
  -> Codex spawn_agent for each approved action
  <- workers use lease-scoped lifecycle tools and persist evidence
```

The server continues to use stdio only. It has no listener, remote control
plane, background model process, UI, or cloud service. Worker lifecycle
primitives may remain internal MCP protocol support, but Relay is the only
public dispatch lifecycle API. They must not grow into an arbitrary agent
framework.

## 3. Names And Ownership

| Concern | Canonical name | Required implementation name |
| --- | --- | --- |
| Product | 2718lab DevKit | plugin display name |
| Knowledge service | Atlas | `devkit_atlas` |
| Atlas tools | `atlas_*` | `atlas_query`, `atlas_prepare`, `atlas_render`, `atlas_accept` |
| Atlas database | `atlas.sqlite3` | under the durable data root |
| Dispatch service | Relay | `devkit_relay` |
| Relay tools | `relay_*` | `relay_compile`, `relay_start`, `relay_status` |
| Relay database | `relay.sqlite3` | under the durable data root |

`AtlasStore` and `ATLAS_*` identifiers may remain when their meaning is
unchanged. All public names, manifest text, package imports, docs, test names,
and persisted schema labels must use the canonical names above after cutover.

## 4. Stable Tool Envelope

Every new Atlas and Relay tool returns exactly one versioned envelope:

```json
{"schema":"2718lab-devkit/tool-result-v1","ok":true,"data":{}}
```

or:

```json
{"schema":"2718lab-devkit/tool-result-v1","ok":false,"error":{"code":"RELAY_PLAN_INVALID","message":"request rejected"}}
```

Objects have exact schemas; unknown fields, malformed identifiers, duplicate
items, oversized collections, non-finite numbers, secret-bearing values, and
unbounded text fail closed. Arrays are deterministically ordered and all
hashes use canonical JSON with UTF-8, sorted keys, and SHA-256. The envelope
never returns raw command output, source text, absolute worker paths, or stack
traces.

## 5. Atlas Contract

Atlas is the sole public code-knowledge surface. It returns facts, bounded
patch candidates, and inert verification specifications; it never executes a
command, edits a repository, calls a model, or applies a patch.

| Tool | Mutation class | Canonical input | Result |
| --- | --- | --- | --- |
| `atlas_query` | read-only | intent or graph roots plus bounded filters | deterministic bounded graph traversal |
| `atlas_prepare` | idempotent local packet write | registered `workspace_id`, current `snapshot_id`, intent, language, optional framework, relative targets | matching decision and immutable packet id |
| `atlas_render` | read-only | registered workspace/snapshot, packet id, typed bindings | patch candidate and inert test specifications |
| `atlas_accept` | idempotent durable projection | workflow id, code-task id, acceptance id, ingestion key | episode/recipe ingestion projection and receipt |

`workspace_id` names a previously registered project-index workspace; it is
not an arbitrary filesystem path. Target paths are normalized relative paths
validated against that workspace and current snapshot. `atlas_prepare` may
persist only a content-addressed packet or reuse an identical one; it has no
workspace side effect.

`atlas_accept` has an intentionally narrow trust boundary. It accepts only
opaque durable identifiers. It must not accept source bodies, arbitrary paths,
commands, command output, patch bodies, caller-provided receipts, or caller-
provided acceptance evidence.

The server constructs an `OrchestratorAcceptanceEvidenceReader` (or an
equivalent production adapter) from the durable workflow and project-index
stores. Before projection it reads and cross-checks the accepted code-task
record, current lease/coordinator authorization, input and output snapshots,
checkpoint binding, indexed diff, query trace, registered verification receipt
hashes, and immutable evidence-binding hash. A missing, stale, forged,
cross-workflow, or inconsistent record fails with an Atlas evidence error.
The caller cannot manufacture a successful acceptance by choosing identifiers.

## 6. Relay Contract

Relay consumes an explicit bounded work package. It validates a DAG, task
kinds, dependencies, write scopes, current snapshots, evidence requirements,
explicit route classes, and requested slot capacity. It permits multiple
concurrent implementation writers whenever their write scopes are disjoint and
their dependency relationship is safe. It does not infer architecture from
prose, create tasks by LLM, or route through a prompt template.

| Tool | Mutation class | Required behavior |
| --- | --- | --- |
| `relay_compile` | read-only | deterministically validate and compile an explicit package into a canonical plan and `plan_hash` |
| `relay_start` | idempotent mutation | create or refill a run, durable workflow/tasks/leases/ready wave, then return `host_actions` |
| `relay_status` | read-only | return run/task/lease state, outstanding action ids, and refill directives |

`relay_compile` reads only named, current project-index and Atlas references.
It writes neither database rows nor files. Equivalent input produces bytewise
equivalent `plan`, `plan_hash`, task order, dependency order, and route data.
The compiled plan includes a fixed `workflow_id`, input snapshot binding,
write-scope hashes, task cards, dependencies, route class, and bounded
evidence requirements. It also has a deterministic prepared/prewarm queue,
ready queue, running-slot capacity, and review/integration queue. A task is
blocked only by an overlapping write scope, an unsafe dependency, capacity, or
missing bounded evidence; it is never blocked merely because another safe
writer is running. The plan contains no absolute worktree path, shell command,
secret, or prompt field.

`relay_start` has two exact request modes. `create` accepts a complete
compiled plan and an idempotency key. `refill` accepts a workflow id, one
status-issued refill directive id, expected state version, and idempotency
key. In either mode it validates all hashes and compare-and-swap versions,
creates only the eligible ready wave, writes unique leases, records immutable
dispatch actions, reserves a unique execution context for each writer, and
returns those actions. The reservation identifies one task-owned Git worktree,
one branch, one base commit, and one D-drive temporary root; no active writer
may share any of them. The Codex host, not Python, materializes the reserved
worktree and branch before spawning the writer. Public actions carry an opaque
context-reservation id and bounded relative suffixes, never an absolute path;
the host resolves them under its configured trusted workspace and D-drive
temporary roots. Repeating the same request returns the same run and action
identifiers. Reusing a key with different content, replaying a consumed
directive, or racing a newer state fails closed.

`relay_status` never allocates a lease or starts an agent. Each refill
directive identifies a ready task, expected state version, route, and required
`relay_start` request. The host realizes it through `relay_start`; a status
response alone is not authorization to spawn.

Relay persists five ordered queues: `prepared_prewarms` for read-only context
preparation, `ready` for dependency-safe dispatch candidates, `running_slots`
for live leased workers, `review_integration` for finished candidates awaiting
Sol, and `terminal` for accepted, rejected, blocked, or cancelled tasks. A
terminal worker event frees its slot immediately. The next `relay_status` must
emit a refill directive for the best safe ready task without waiting for a
wave, a sibling worker, or review completion. Prewarm work never owns a write
scope or a writer lease; it can be discarded when its input snapshot changes.
The host consumes terminal events immediately, calls `relay_status`, and then
calls `relay_start(refill)` for the returned directive. Every available slot
must remain occupied by the highest-value safe writer, reviewer, verifier, or
prewarm action while useful eligible work exists. An idle slot is valid only
when the host capacity is lower or Relay can name a stable dependency,
ownership, evidence, or safety reason why no useful action is eligible; Relay
never invents filler work merely to report full utilization.

Each `host_actions` item has a stable `action_id`, `kind: "codex.spawn_agent"`,
workflow/task ids, opaque lease tuple, explicit model and reasoning effort,
and a structured task contract. Writer actions also carry a host-only
worktree-bootstrap contract with the allocated branch, base commit, and
validated D-drive context. It has no `prompt` or static-agent identity. Sol
performs the worktree bootstrap, translates the contract into the transient
task message required by the Codex API, deduplicates `action_id`, calls
`spawn_agent`, and retains design, review, integration, security, and final
acceptance ownership. A worker receives only its own lease-scoped context.
Python never observes or fabricates a successful spawn; endpoint binding and
task completion are the durable evidence of worker progress.

Each implementation worker produces a durable PR/integration candidate, not a
direct merge: candidate id, task id, branch, immutable base commit, head
commit, scoped diff hash, required evidence hashes, and optional host-created
PR reference. It moves from `candidate_ready` to `review_integration` only
after its focused checks pass. Sol reviews the candidate and alone accepts,
rejects, supersedes, rebases, resolves conflicts, or integrates it into the
shared integration worktree. A remote PR is optional; the candidate record is
required even for a local-only integration flow. The PR-style candidate and
review gate are mandatory protocol behavior; only publication to a remote Git
hosting service is optional.

Integration compares the candidate base with the current integration head. A
matching base is eligible for Sol review. A changed base with no overlapping
changed scope may be rebased into a new candidate, which must receive a new
head hash and repeat its required checks and review. An overlapping change,
rebase conflict, or failed post-rebase evidence moves the candidate to
`stale_base` or `conflicted`; it is never auto-merged and its writer may be
refilled only through a new safe task/candidate decision by Sol.

## 7. Durable State And Data Flow

The resolved data root is selected from the documented plugin-data variables
or the Codex data directory. It must be outside plugin source and plugin cache
directories. Startup rejects an invalid root and creates no fallback state in
the repository.

```text
project-index snapshot -> atlas_prepare -> immutable packet -> worker context
worker evidence -> durable workflow acceptance -> atlas_accept -> Atlas receipt
explicit work package -> relay_compile -> canonical plan
canonical plan/refill directive -> relay_start -> context reservations + leases + host_actions
host bootstrap -> isolated worktrees and branches -> Codex spawn_agent
workers -> PR/integration candidates -> Sol review/integration/acceptance
terminal event -> relay_status -> immediate next safe refill directive
```

`atlas.sqlite3` owns nodes, edges, packets, ingestion receipts, and the Atlas
content-addressed blob store. `relay.sqlite3` owns runs, compiled-plan hashes,
workflow/task records, dependency state, lease epochs, immutable dispatch
actions, idempotency records, queue state, writer-context reservations,
candidate records, and refill directives. Project-index state and Bugkiller
approval journals remain separate durable stores. All mutations use
transactions, foreign keys, version compare-and-swap, and content/hash checks.

## 8. Errors And Recovery

The public error code set is closed and documented. General failures use
`DATA_ROOT_INVALID`, `DATA_ROOT_UNAVAILABLE`, `STORAGE_ERROR`, or
`INTERNAL_ERROR`. Atlas uses `ATLAS_REQUEST_INVALID`, `ATLAS_SNAPSHOT_STALE`,
`ATLAS_PACKET_NOT_FOUND`, `ATLAS_PACKET_STALE`, `ATLAS_EVIDENCE_UNAVAILABLE`,
`ATLAS_EVIDENCE_CONFLICT`, and `ATLAS_MIGRATION_CONFLICT`. Relay uses
`RELAY_REQUEST_INVALID`, `RELAY_PLAN_INVALID`, `RELAY_PLAN_STALE`,
`RELAY_STATE_STALE`, `RELAY_IDEMPOTENCY_CONFLICT`, `RELAY_LEASE_CONFLICT`,
`RELAY_CANDIDATE_STALE_BASE`, `RELAY_CANDIDATE_CONFLICT`, and
`RELAY_MIGRATION_CONFLICT`.

No tool silently retries a model, substitutes a model, claims another task's
lease, repairs a stale snapshot, or mutates state after a validation error.
An action may be returned again only with the same durable `action_id`; Sol
must deduplicate it. A worker failure, expired lease, candidate rejection, or
successful candidate handoff becomes observable terminal state and can yield a
new safe refill directive immediately. Stale-base and conflict candidates
preserve evidence for review but cannot unblock dependencies or merge.

## 9. Security Boundaries

- stdio is the only transport; stdout remains protocol-only and diagnostics go
  to stderr.
- Inputs are bounded structured data, not shell fragments. Neither Atlas nor
  Relay executes caller-provided commands.
- Leases are task-, epoch-, and owner-bound. Cross-task reads/writes,
  stale epochs, and forged endpoint bindings are rejected.
- Atlas packet and acceptance records bind workspace identity, snapshots,
  evidence identifiers, and canonical hashes before use.
- Secrets, source blobs, raw receipts, absolute temporary paths, and external
  host handles stay out of tool outputs and persisted public projections.
- Existing AstrBot templates/validators may be used by a developer explicitly,
  but their imports and runtime hooks are absent from the primary server path.

## 10. One-Time Naming Migration

This release is a hard public rename. The historical labels `Code Atlas`,
`Fast Lane`, and `Ultra Fast Lane` are accepted only as migration inputs,
compatibility-detection markers, and historical documentation. No public tool,
package import, manifest capability, alias, hidden alias, or fallback route
uses those labels after cutover. This specification replaces the prior rule
that the latter dispatch capability had no MCP surface.

At first startup, a migration coordinator operates only inside the resolved
durable data root:

1. It acquires an exclusive migration lock and records a schema/version ledger.
2. If the canonical Atlas store is absent, it imports the historical local
   database and content-addressed blobs into `atlas.sqlite3` only after schema,
   foreign-key, blob-hash, and receipt-hash verification.
3. It imports durable workflow records into Relay only as historical records;
   no imported task has an active lease, outstanding host action, or automatic
   refill. Compatible completed evidence remains readable by the trusted
   acceptance reader.
4. It writes canonical-name metadata and an import fingerprint transactionally.
   A repeated matching run is a no-op; a divergent target or checksum conflict
   returns the appropriate migration conflict without overwriting data.
5. It preserves the source migration input until a later explicit maintenance
   operation; it never reads plugin source/cache as a migration source.

Fresh installations create only canonical stores. The migration is complete
only when the manifest, imports, server registration, docs, tests, and durable
schema labels expose the canonical names.

## 11. Implementation And Tests

Implementation registers the seven canonical tools in the existing stdio
server, moves Atlas code to `devkit_atlas`, introduces `devkit_relay`, wires
the production acceptance-evidence reader, and removes primary-plugin prompt,
static-agent, and skill-aggregation metadata. Existing project-index and
Bugkiller behavior remains bounded supporting functionality.

Acceptance requires all of the following:

1. The Atlas and Relay inventory is exactly the four and three tools listed
   above, with no old public aliases and no MCP prompt surface.
2. A fresh stdio MCP process starts independently, lists the canonical tools,
   and keeps stdout protocol-clean.
3. `relay_compile` is deterministic and performs no durable mutation;
   `relay_start` is idempotent; `relay_status` cannot mutate or spawn.
4. A create and a refill flow create correct workflow/task/lease records and
   return only structured, deduplicable `host_actions` for Codex spawning.
5. Two disjoint ready implementation tasks receive distinct worktrees,
   branches, temporary roots, leases, and concurrent writer actions; an
   overlapping scope or unsafe dependency is withheld without serializing the
   unrelated writer.
6. A terminal worker event refills one free slot immediately while another
   safe worker remains running; prewarm, ready, running, and review/integration
   queues remain deterministic across a status/restart round trip.
7. A candidate cannot merge itself. Matching-base, stale-base, clean-rebase,
   rebase-conflict, reject, and post-rebase verification paths preserve the
   required candidate lifecycle and leave Sol as the sole integrator.
8. Atlas query/prepare/render operate against a current registered snapshot;
   rendering is inert and acceptance rejects caller-supplied evidence forms.
9. The production reader accepts only a fully durable, same-workflow,
   hash-consistent evidence chain and rejects forged, stale, incomplete, and
   cross-workflow identifiers.
10. Data-root, migration, conflict, stale-version, stale-lease, stale-base,
   invalid worktree context, and invalid
   input tests fail closed without repository or plugin-cache writes.
11. Existing project-index, workflow evidence, Bugkiller, and relevant MCP
   regression tests pass after the rename and public-contract update.

## 12. Out Of Scope

This version adds no web UI, remote service, telemetry pipeline, release work,
general-purpose agent framework, model runner, prompt marketplace, embedding
retrieval, autonomous shell execution, or AstrBot runtime integration.
