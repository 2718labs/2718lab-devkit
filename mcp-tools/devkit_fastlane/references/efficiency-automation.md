# Team efficiency automation

`scripts/team_efficiency.py` is a local, deterministic helper for bounded team
coordination data. It validates JSON and emits plans, packets, checks, cache
metadata, and Markdown. Inputs are data only: the helper never evaluates an
input as code and has no model or remote-service interface.

## Scheduler topology V1

`2718lab-devkit/scheduler-topology-v1` records auditable opaque identity
bindings for the plan, lease, and G-drive worktree. A/B/C means the main
conversation (which reviews and integrates), scheduler, and writer. One
scheduler has at most a `1:3` writer set. Design and prewarm are read-only,
not writers, but are limited by actual host slots, host capability, lease, and
safety gates. Only a `declared-child` split that strictly reduces conflict may
cross scope; otherwise emit `UNSPLITTABLE_SCOPE_CONFLICT`. It does not restore
account-usage quota, D-drive temporary roots, or a parent model ceiling, and
does not weaken host capability, lease, worktree, or safety gates.

## Bootstrap

`bootstrap` requires a task id, full base commit, safe branch, bounded relative
write scope, existing repository, project identifier, worktree target, and temp
target. Its host-configured base root is `CODEX_FASTLANE_TASK_ROOT`, or the
local default `G:\2718lab\_codex\.codex-task-temp` when unset. A configured root
must be an existing local absolute G: non-volume-root directory without reparse points;
project components and root-bound target components cannot be reparse points;
the lexical path is checked before canonicalization. Both bootstrap targets and every
read-context worktree/temp target must be strictly below the declared derived
`<root>/<project>` task root. A read context names its bounded `project`, carries
the current canonical `task_root_hash`, and, when execution contexts exist, must
match one of their projects. Win32-normalized aliases (including trailing
dot/space and reserved device names) are rejected for root-bound targets. The
root never comes from request JSON or a bootstrap plan: default plans remain
bootstrap-v1, while a non-default root is bound by a canonical hash in
bootstrap-v2. A changed configuration fails diagnostic revalidation; this
repository does not launch Git for bootstrap execution.

The default output is a canonical dry-run plan. Its canonical worktree vector
is:

```text
git -C <repo> worktree add -b <branch> <worktree> <base-commit>
```

`bootstrap --apply` is deliberately disabled in the public CLI, and the
import-callable `apply_bootstrap_plan` helper is equally fail-closed. Both
return `NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE` before building a
caller-supplied plan or invoking the worktree helper, so neither can run
`git worktree add`. Plan-only `bootstrap` remains a non-mutating diagnostic
command; its emitted project/root/worktree values cannot establish authority.

There is no currently executable host-authorized worktree path in this
repository. It contains no private capability registry, runner, Git probe, or
internal adapter that can turn a plan into a worktree. The displayed vector is
diagnostic data only, and attempted mutation always returns
`NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE` before a caller-provided runner,
path, environment, or JSON can have an effect. A Desktop host registry and a
genuinely external private execution bridge are prerequisites for any future
apply behavior; neither is implemented or accepted as an input here.

An older bootstrap or routing schema is rejected as
`FASTLANE_SCHEMA_UPGRADE_REQUIRED`; it is never normalized into an executable
plan. An empty project may produce bootstrap-only index diagnostics, but cannot
produce an assignment until trusted host index context is available.

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
      "write_scope": ["mcp-tools/devkit_fastlane/scripts/team_efficiency.py"],
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

### Project-bound execution envelope

`work-package-v1` remains diagnostic-only input for `decompose` and
`plan-waves`; it never authorizes assignment, worktree creation, claim, resume,
or durable recovery. The exact `team-efficiency/work-package-v2` envelope is
still canonical diagnostic input and the required future external-host contract:

```json
{
  "schema": "team-efficiency/work-package-v2",
  "package": { "schema": "team-efficiency/work-package-v1" },
  "package_payload_hash": "sha256:<canonical-v1-payload>",
  "project_fence": {
    "schema": "team-efficiency/project-fence-v1",
    "project_id": "opaque-project-id",
    "binding_digest": "sha256:<host-binding>",
    "binding_version": 1
  },
  "workspace_id": "sha256:<workspace>",
  "input_snapshot_id": "sha256:<input-snapshot>"
}
```

The diagnostic decomposition and inert public blocked-plan hash incorporate
this full binding into `source_plan_hash`. The manifest provides structural fence data, never
authority; no environment project ID, root path, worktree path, task-root
component, caller-supplied identifier, Python module attribute, or closure can
replace a live host record. This repository has no Desktop-host durable
registry or external private bridge, so its public `compile_fast_lane` and
`fast-lane` CLI do not compare against an in-process provider and cannot
activate V2: a structurally valid V2 request always yields
`NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE` with zero assignments, local
queues, and external-session assignments. An invalid V2 envelope/hash yields
`PROJECT_BINDING_INVALID`; so does an inner canonical-v1 `package` that fails
pure diagnostic parsing, or an invalid fast-lane request schema/key/byte shell.
Those checks occur before any host, account-usage, or index input is read. V1 yields
`NO_SAFE_WORK/LEGACY_PROJECT_UNBOUND`.
An MCP request that explicitly carries host-private fields such as `host_status`,
account-usage, or index evidence is not a diagnostic plan shell: the public adapter must
reject it as `FASTLANE_REQUEST_INVALID` before compilation.
Public `bootstrap --apply` and import-callable `apply_bootstrap_plan` are both
blocked before any caller plan, path, provider override, closure, or JSON can
reach a worktree helper, because no such helper exists in this repository.

Host-status, account-usage, and index payloads remain bounded future-bridge contract
data. The current public CLI parses its stable command surface but does not use
those caller inputs to create authority, schedule work, read a live account source
provider, or bypass the inert result. Only an externally implemented and
accepted Desktop bridge could perform live authority comparison in the future.

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
`team-efficiency/fast-lane-plan-v1` result. It is a pure compiler: all target gates,
contexts, receipts, tokens, and workflow operations are inert dispatch descriptors. The
helper performs no model call, agent spawn, remote service contact, gate run,
Git mutation, workflow call, lease claim, endpoint bind, or workflow
completion.

The host invokes `fast-lane` with an explicit reasoning effort. `ultra` is the
automatic activation path (`ultra_auto`); lower efforts require explicit
`--enable` (`explicit_opt_in`) and otherwise return an inactive plan.

### Fast Lane routing-core v3 capability contract

The separate `fastlane_routing.py` core is a pure, zero-model-call policy
decision function. It does not dispatch work, claim a lease, bind an endpoint,
or integrate with the scheduler. Its policy registry is an eligible envelope,
not an assertion that every registered tuple is usable on the current host.

A normal resolved route must be an **exact host-attested model/effort tuple**.
The core derives the immutable safety floor first, then intersects policy
candidates with the current host's exact attestations. If the preferred tuple
is not attested, it deterministically chooses the lowest-cost exact attested
candidate that satisfies the same floor and all lane, budget, and safety gates;
the result records `capability_fallback`. If no such candidate exists, the
result is unavailable with `capability_unavailable`. It never chooses an
unattested effort. Thus a policy may retain Luna `low`, `medium`, `high`, and
`xhigh` while a medium-only host safely uses its attested `medium` fallback and
a multi-effort host continues to select dynamically by score/floor.

Todo source v2 accepts only bounded numeric metrics, exact SHA-256 task
fingerprints, and closed route/floor reason-code enums. Route metadata and
metrics are validated then excluded from the token-facing projection
fingerprint, so their changes are `noop`. Only compact recovery transitions
(`transport_degraded`, `recovery_probe`, `resumed`, and
`fenced_replacement`) are persisted and emitted as replayable transitions.

### Scheduler adapter boundary

The scheduler does not carry a fixed role-to-model table. The host supplies a
bounded (at most 3 MiB), exact-key `--host-status` object with `workflow_id`,
`current_leases`, `host_bindings`, and `routing_context`. Each
`routing_context.routes` entry uniquely names `(task_id, scheduler_role)` and
carries one complete (at most 32 KiB) `2718lab-devkit/fastlane-routing-request-v3`, its bounded
trusted-evidence inputs, and any compatibility floor. The adapter cross-binds
the entry key to the source unit and core task role/access, then calls only the
pure routing core. It must not infer a score, safety floor, capability fallback,
or worker route from `recommended_route`, a legacy profile, or raw capability
data.
The envelope permits up to 85 entries: the 16-unit source plan plus one approved
global-remediation unit for each of the five scheduler roles.

For every start descriptor, the dispatch receipt and assignment token bind the
core-derived model/effort plus `routing_context_hash`, `routing_result_hash`,
`task_fingerprint`, `routing_reason_codes`, and
`routing_safety_floor_rank`, together with the dispatch context, slot epoch,
and historical bounded routing input. Lifecycle validation replays that archived
input through the pure core so a later event, lease change, or capability report
cannot rewrite the route that was actually dispatched. Missing, duplicate,
unknown, task/role-mismatched, invalid, or core-unavailable routes—including a
missing exact capability attestation—have no guessed fallback: the scheduler
invalidates the entire dispatch matrix: it returns `NO_SAFE_WORK` with no
assignments or queues, even if another route is otherwise valid.

`ultra` activates Fast Lane and lane 0 only; it never becomes a worker route.
Every worker route is selected per task from the host-attested core result and
must use a non-`ultra` effort. The coordinator lane owns design, integration,
risk decisions, and final acceptance; a Sol lane is selected only when the
exact host-attested route warrants architecture, difficult diagnosis, or
independent terminal review. Prewarm remains a separate read-only evidence role;
it is never execution or acceptance evidence.

The request supplies bounded work-package, target-gate, execution-context,
read-context, remediation, and scheduler-state data. The plan binds a source
plan hash, partitions exactly three **local child slots** into start/retain
assignments and honest idle slots, and carries canonical dispatch
receipts/tokens. The host spawns only `action="start"`; it never respawns a
retained assignment and refills a free slot only after a terminal event. Neither
the compiler nor the host polls commentary updates or refills from commentary
(`no commentary polling`).

Every rendered assignment includes `host_dispatch`. The host MUST call
`collaboration.spawn_agent` with the exact `model` and `reasoning_effort` from
that object, with `inherit_current_session_model=false`; omitting either value
or allowing the current session model to fill it is a rejected dispatch. This
keeps the visible route (for example Terra, Sol, or Luna) identical to the
attested route in the receipt.

Every rendered assignment also includes one bounded `index_context` packet.
The host owns its `project_index` preparation: one input query at the dispatch
boundary and, for writers, one output query at the terminal boundary. The
worker receives the packet and consumes its anchors/scope; it does not invent
queries, call `project_index_register/sync/status/query`, or poll status while
working. A missing or hash-invalid packet stops that assignment. The compact
packet is the normal path; the long list of index lifecycle operations is no
longer an LLM task list.

### Cross-session capacity projection

Cross-session selection is compiler-owned, not an LLM preference. When the
projection carries `dispatch_policy.action=dispatch_all` and
`target=independent_codex_session`, the host dispatches every listed assignment
mechanically. `dispatch_none` creates no session and `stop` fails closed;
`selection_authority=compiler` and `llm_choice=false` must be preserved.
This is still an inert projection: only a trusted host integration creates an
independent session/worktree after its worktree, lease, context, and predecessor
fences validate. The compiler and skills do not call host dispatch APIs.

`3` is the per-Codex-session child-agent limit. It is not the global main-pool
limit, and it does not mean three sessions. Global active/free values come only
from the verified cross-session ledger; they are agent counts, never a session
count or an inferred number of sessions. The compiler never derives capacity
from caller-supplied or account-usage data.

When all three local child slots are fully occupied or admitted and verified
host capacity remains, the plan may contain an inert
`external_session_required` projection. It is an assignment/lease **plan**, not
an external host action: it creates no session, worker, target, process, or
workflow transition. Each deterministic external assignment carries the exact
task route/context plus a predecessor fence binding the source plan, route
decision hashes, ledger epoch, active-lease-set hash, and its
assignment identity. Every external assignment also carries
`worktree_required=true`: the independent-session owner must create and bind
an isolated Git worktree under the approved task root, never the coordinator's
dirty integration checkout, then revalidate that fence and acquire its own
atomic workflow/ledger claim before any execution. A missing, foreign, or
unverified worktree fails closed.

If a local child slot is unfilled, the projection is `not_required`; it cannot
invent an external assignment. `external_agent_count` counts additional agent
assignments, not sessions: several assignments may be placed in one validated
external worktree/session. Unknown, stale, untrusted, receipt-invalid,
foreign-host, mismatched, or exhausted evidence blocks the projection with no
assignments. A host-defined cap limits additional assignments after three local
child admissions; it never denotes the number of sessions. A larger queue is
blocked rather than truncated. External plans cannot route `ultra` or Spark work.

Where the declared lifecycle orders `host_spawn_exact_route` before
`workflow_claim_with_host_target`, that spawn is a `parked endpoint bootstrap`,
not prewarm: it may create an inert route solely to obtain `host_target`. Until
the claim succeeds, and `workflow_endpoint_bind` succeeds when its declared
condition applies, the host MUST NOT deliver a task payload or permit that
worker to read a worktree, run a target gate, write, checkpoint, sync or query
project state, or emit a receipt, candidate, or terminal result. The first
execution or verification authorization must bind the claimed task, owner,
lease epoch, and exact parked `host_target`. A failed, stale, or rejected claim
leaves the parked worker inert and creates no durable task transition. This is
a host invariant; it adds no compiler operation. Prewarm remains its separate
read-only role and is not a name for this bootstrap.

Prewarm is read-only evidence, not acceptance evidence: a later writer may
reuse it only after current-basis delta revalidation passes.

### Capacity boundary

Fast Lane has no account-usage coordinator integration. A future host
may attest bounded capacity as part of a separately versioned route contract;
this repository neither reads that data nor treats it as a caller-supplied input.

The terminal protocol is bounded to one regression (the integration regression
pass), one blocker review, and at most one global remediation (targeted to the
finding). Candidate or review results never unlock a dependency; lane 0 integration, artifact
registration, and durable workflow completion remain required.

The compiler proves only request consistency. All execution and read `repo`
anchors must agree on a canonical repo anchor, and worker/read worktrees must
differ from that anchor. If a future external host bridge executes a descriptor,
it must match the request anchor to its trusted shared integration worktree and
reject a mismatch before `git worktree add`. After an external apply, it must
re-resolve the created target, verify that it equals the planned worker worktree
and shares the trusted anchor's Git common directory, and record that
post-apply attestation as host evidence. This repository implements none of
those mutation steps and never substitutes the integration worktree for a
worker or read worktree.

Rendered plans contain redacted bounded metadata only: no absolute
repo/worktree/temp paths, prompts, raw command output, secrets, or raw
external receipt bodies.

The adapter never archives work. The host may archive only after coordinator-lane acceptance and final evidence binding have completed. Fast Lane scratch files,
worktrees, ordinary caches, test evidence, and read worktrees must remain below
the declared project root derived from trusted `CODEX_FASTLANE_TASK_ROOT` (or
the local `G:\2718lab\_codex\.codex-task-temp` default). This remains the
bootstrap/read-context boundary. C-drive and non-G-drive local temporary roots
are forbidden. After X unsuccessful rollback rounds, the host may record only
candidate cleanup eligibility; it does not delete any path automatically.

## CLI

```text
python scripts/team_efficiency.py resume-packet --input <packet.json>
python scripts/team_efficiency.py status --input <snapshot.json>
python scripts/team_efficiency.py contract-check --producer <producer.json> --consumer <consumer.json>
python scripts/team_efficiency.py cache-key --input <cache-inputs.json>
python scripts/team_efficiency.py decompose --input <work-package.json>
python scripts/team_efficiency.py plan-waves --input <work-package.json>
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --host-status <fast-lane-host-status.json> --reasoning-effort ultra
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --host-status <fast-lane-host-status.json> --reasoning-effort max --enable
```
