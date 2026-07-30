# Team efficiency automation

`scripts/team_efficiency.py` is a local, deterministic helper for bounded team
coordination data. It validates JSON and emits plans, packets, checks, cache
metadata, and Markdown. Inputs are data only: the helper never evaluates an
input as code and has no model or remote-service interface.

## Bootstrap

`bootstrap` requires a task id, full base commit, safe branch, bounded relative
write scope, existing repository, project identifier, worktree target, and temp
target. Both targets must be strictly below `D:\bun\tmp\codex\<project>`.

The default output is a canonical dry-run plan. Its only eligible apply vector
is:

```text
git -C <repo> worktree add -b <branch> <worktree> <base-commit>
```

`--apply` revalidates that exact plan and requires the worktree target to be
absent. Before the apply vector, fixed read-only Git probes verify the local
repository and full base commit. The helper creates or verifies only the
validated temp target and passes it as `TEMP`, `TMP`, `TMPDIR`, and
`CODEX_TASK_TEMP` to those child processes.

## Resume, status, contracts, and cache metadata

`resume-packet` accepts only the documented bounded fields: workflow/task,
lease and endpoint, base/candidate commits, safe worktree id, write-scope hash,
RED/GREEN summaries, contract/evidence hashes, next action, and `redacted`.
It rejects unknown fields, unbounded payloads, sensitive material, raw summary
fields, and absolute paths.

`status` renders a deterministic Markdown Todo table from workflow, lease, and
resume data. `pending_init` is visible but never contributes to active parallel
execution; only `running` does. The table contains branches and compact result
summaries/evidence hashes, not command output. Table cells conservatively
escape line breaks, separators, and link/control punctuation before rendering.

`contract-check` succeeds only when schema and artifact hash match exactly.
`cache-key` records an exact candidate commit/tree, write scope, argv,
toolchain/platform fingerprint, dependency-lock hashes, and test lane. It
never invokes a verification lane; incomplete fingerprints cannot match.

## Deterministic work-package planning

The `decompose` and `plan-waves` commands never invent semantic architecture.
They accept either explicit artifact boundaries or a separately shaped
`atlas_evidence` manifest. In both cases the scheduler emits the largest safe
ready wave up to declared capacity, using a stable task-id tie-break.

```json
{
  "schema": "team-efficiency/work-package-v1",
  "task_id": "ATLAS-12B",
  "goal": "Deliver one bounded result",
  "capacity": 2,
  "decomposition": "artifact_boundaries",
  "source_kind": "explicit_artifact_boundaries",
  "artifacts": [
    {
      "task_id": "ATLAS-12B-A",
      "goal": "Implement the bounded artifact",
      "output_boundary": "helper module",
      "write_scope": ["skills/work-methodology/scripts/team_efficiency.py"],
      "depends_on": [],
      "required_evidence": ["focused-helper-tests"],
      "complexity": "routine",
      "execution_contracts": ["contracts/helper-api"]
    }
  ]
}
```

Source kind is optional only for the legacy mode; its default is
explicit_artifact_boundaries. A legacy artifacts list must not be labelled as
Code Atlas evidence. Every artifact must have a unique task id and output
boundary, a non-empty relative write scope, declared dependencies, required
evidence, complexity, and explicit execution contracts. Equal paths and
ancestor/descendant paths conflict. Unsafe, wildcard-like, absolute,
traversal, unknown-dependency, and cyclic manifests are rejected.

### Verified Code Atlas evidence

Code Atlas planning requires decomposition atlas_evidence and one of two real
model serializations forwarded by a trusted host:

- code_atlas_packet accepts an exact ImplementationPacket.to_dict() object plus
  path_bindings. Bindings are render inputs, not a replacement schema: their
  keys must exactly cover actual TemplateOperation.path_slot values, and every
  used slot must be an actual SlotSpec of type relative_python_path.
- `task_episode_graph` requires a trusted host to forward the real extractor's
  explicit `eligible` boolean and exact `GraphQueryResult.to_dict()` object.
  Its scopes come only from real `TaskEpisode --CHANGES--> SourceEvidence`
  edges and concrete `SourceEvidence` payload paths; this mode has no
  path-binding field.

The helper validates every public dataclass field from the packet, graph, node,
edge, operation, slot, constraint, dependency, and test records. It checks
Code Atlas packet/node/edge canonical identifiers, permits only actual NodeKind
and EdgeRelation values, and rejects look-alike graph fields. It does not
import or call Code Atlas, a remote service, an LLM, or a vector store; it
consumes inert JSON produced by to_dict().

Canonical identifiers, content hashes, and provenance fields establish
internal consistency only; they do not authenticate the source of caller-
supplied JSON. The host is responsible for forwarding real Code Atlas output
across a trusted boundary. Caller-crafted data is not authenticated merely
because it reproduces valid hashes or labels.

For a complete packet, operations resolving to one target path merge into one
code unit. Distinct safe paths can share the first wave; a verification unit
depends on every code unit and therefore cannot run early. DependencySpec
describes environment requirements, not invented task edges. The direct
contract hash is canonically derived from the verified packet_id and recipe_id;
no extra packet field or ConstraintSpec convention is accepted.

For a `TaskEpisode` graph, each code unit is a real code `TaskEpisode`.
Participating `TaskEpisode`, `SourceEvidence`, `TestSpec`, and
`ExecutionReceipt` nodes must be observed, unsuperseded, unquarantined, and
source-hash bound. Participating `CHANGES`, `VERIFIED_BY`, `TESTS`, and
TaskEpisode `SOLVES` edges must also be observed. A graph must contain the real
redacted extractor payloads:

- one `bound_receipt_summary` whose count matches the individual receipts;
- at least one successful, zero-exit `command` receipt and one successful,
  zero-exit `write` receipt, each with complete command/input/output hashes;
- one zero-exit `bound_verification`; and
- `command_receipt` verification nodes whose command hashes exactly match the
  successful command receipts and whose observed `TESTS` edges cover the
  changed evidence.

For an accepted, receipt-verified request, `PythonRecipeExtractor` explicitly
emits `observed` provenance only for the direct TaskEpisode evidence graph's
`SOLVES`, `CHANGES`, `VERIFIED_BY`, and `TESTS` edges.

Here `TESTS` is an acceptance-evidence binding: it binds bound verification or
command-receipt test evidence to changed source evidence. It is not a test
coverage measurement or an inference that coverage is complete. The extractor
does not auto-generate or promote `SUPERSEDES`, `BUNDLED_AS`, recipe-facing
links, matching/alias, coverage, ordering, or other inferred edges. In
particular, recipe lineage is not TaskEpisode dependency evidence; recipe-link
promotion or migration is ATLAS-12D work.

The fresh-store end-to-end contract writes the unchanged extractor result to
`AtlasStore`, obtains an untruncated graph by querying from the `episode_id`,
and forwards only that `GraphQueryResult.to_dict()` value to the planner. A
single externally rebuilt `declared` edge must still fail closed with
`ATLAS_EDGE_UNVERIFIED`.

Provenance is part of canonical edge identity, so promoting a legacy
`declared` edge to `observed` changes its `edge_id`. This work guarantees the
fresh-store chain only. Migration of existing durable graphs, repair of recipe
links, and invalidation or migration of packet links are ATLAS-12D work; this
contract does not claim those legacy migrations are complete.

The graph execution-contract hash uses only a canonical sorted set of
participating node ids and edge ids plus the `TaskEpisode` id. Creation times
are deliberately excluded, so time-only metadata changes do not invalidate
the contract. A quarantine, supersession, provenance downgrade, incomplete
hash, failed receipt, or bad exit code instead returns `needs_design`. Recipe
`SUPERSEDES` is version evidence, not TaskEpisode task-order evidence, and
never creates a task dependency; normalized write-scope conflicts still
serialize overlapping work.

The accepted work-package budget is 128 KiB, which covers Code Atlas'
65,536-byte default graph-query envelope. Oversized, truncated, ineligible, or
malformed graph evidence returns `needs_design` with a stable
`ATLAS_*` reason and no units, waves, or raw exception text. Packet gaps and
missing render slots also return `needs_design`.

### Workflow lifecycle plan

Every result includes a versioned
`team-efficiency/workflow-lifecycle-plan-v1` lifecycle plan under the stable
`registration_plan` result key. Each operation is a
`{tool, arguments, host_bound_fields}` descriptor whose argument names match
the real MCP function signature.

The host executes the plan in two separated phases:

1. Register all tasks by following `registration_order` and `register_steps`.
   This is a stable topological order: every dependency is registered before
   its dependent. The required parameters for `workflow_register_task` are
   `workflow_id`, `task_id`, `title`, `owner_role`, and `card`.
2. Process `execution_waves` in order. Each wave first invokes
   `workflow_ready`, whose sole required parameter is `workflow_id`, then
   invokes that wave's `workflow_claim` and `workflow_endpoint_bind`
   descriptors. The required parameters for `workflow_claim` are `task_id`,
   `owner`, and `expires_at`; the required parameters for
   `workflow_endpoint_bind` are `workflow_id`, `task_id`, `owner`,
   `lease_epoch`, and `host_target`.

`workflow_ready` promotes dependency-satisfied NEW tasks, but it may return an
empty result when an earlier capacity-limited wave already promoted a later
task to READY. The plan therefore does not require the returned ready-task set
to equal the scheduled wave. Claims use durable task state as their
precondition. A machine-readable completion barrier permits advancing only
after every task in the current wave reaches `DONE`; claim and bind never
immediately follow an individual registration step.

`workflow_register_task.arguments` places `dependencies`, `write_scope`,
`direct_contract_hashes`, `task_node_ids`, `contract_node_ids`,
`required_evidence`, `input_hash`, `input_snapshot_id`, `workspace_root`, and
`strict_index` at their real top-level MCP parameter positions. Its `card` is
a bounded canonical JSON string containing only redacted task-card metadata.
`workflow_id`, owner, expiry, epoch, collaboration target, clock, workspace
root, and input snapshot use explicit objects with `source=host`, a `ref`, and
a description, and their argument names are repeated in
`host_bound_fields`. The host resolves those fields (and may omit optional
ones) before passing `arguments` directly to the named MCP tool. These are
runtime bindings, not fake values. The plan never copies an absolute
workspace, trace id, or snapshot id from Atlas evidence.

## Ultra Fast Lane

`fast-lane` compiles the exact
`team-efficiency/fast-lane-request-v1` request into a deterministic
`team-efficiency/fast-lane-plan-v1` result. It is a pure compiler: all target
gates, contexts, receipts, tokens, and workflow operations are inert data. The
helper does not call a model, spawn an agent, contact a remote service, run a
gate, mutate Git, make a workflow call, claim a lease, bind an endpoint, or
complete a workflow.

The host invokes `fast-lane` with an explicit reasoning effort. `ultra` is the
automatic activation path; lower efforts require explicit `--enable` and
otherwise return an inactive plan. Descriptors always carry explicit model and
effort: prewarm is `gpt-5.6-terra` / `medium`; routine implementation and
ordinary verification are `gpt-5.6-terra` / `high`; moderate-or-harder
implementation and verification are `gpt-5.6-terra` / `max`; review is
`gpt-5.6-terra` / `high`; and a bounded design probe is `gpt-5.6-sol` /
`ultra`. Main Sol lane 0 owns design, integration, risk decisions, and final
acceptance.

The request supplies bounded work-package, target-gate, execution-context,
read-context, remediation, and scheduler-state data. The plan binds a source
plan hash, partitions exactly three subagent slots into start/retain
assignments and honest idle slots, and carries canonical dispatch
receipts/tokens. The host spawns only `action="start"`; it never respawns a
retained assignment and refills a free slot only after a terminal event. Neither
the compiler nor the host polls commentary updates or refills from commentary
(`no commentary polling`).
Prewarm is read-only evidence, not acceptance evidence: a later writer may
reuse it only after current-basis delta revalidation passes.

The terminal protocol is bounded to one integration regression pass, one
blocker review, and at most one global targeted remediation. Candidate or
review results never unlock a dependency; lane 0 integration, artifact
registration, and durable workflow completion remain required.

The compiler proves only request consistency. All execution and read `repo`
anchors must agree, and worker/read worktrees must differ from that anchor.
Before executing a descriptor, the host matches the request anchor to its
trusted shared integration worktree; before `git worktree add`, it rejects a
mismatch without mutation. After apply, the host re-resolves the created
target, verifies that it equals the planned worker worktree and shares the
trusted anchor's Git common directory, and records that post-apply attestation
as host evidence. The host never substitutes the integration worktree for a
worker or read worktree.

Rendered plans contain redacted bounded metadata only: no absolute
repo/worktree/temp paths, prompts, raw command output, secrets, or raw
external receipt bodies.

## CLI

```text
python scripts/team_efficiency.py resume-packet --input <packet.json>
python scripts/team_efficiency.py status --input <snapshot.json>
python scripts/team_efficiency.py contract-check --producer <producer.json> --consumer <consumer.json>
python scripts/team_efficiency.py cache-key --input <cache-inputs.json>
python scripts/team_efficiency.py decompose --input <work-package.json>
python scripts/team_efficiency.py plan-waves --input <work-package.json>
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort max --enable
```
