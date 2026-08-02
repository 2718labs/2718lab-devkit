# Ultra Fast Lane Design

**Status:** approved concept, implementation pending specification review
**Date:** 2026-07-30
**Scope:** `2718lab-devkit` work-methodology orchestration only

## 1. Problem

The existing `team_efficiency.py` helper can validate work packages and compile
safe dependency waves, but it does not express the operating model needed by
an Ultra main conversation:

- the main Sol conversation should remain an active, highest-intelligence work
  lane instead of becoming a passive dispatcher;
- three additional subagent slots should be kept on useful work whenever safe
  work exists;
- a completed or blocked slot should be refilled immediately from prepared
  queues;
- routine worker verification, integration regression, and workflow API calls
  should not be repeated at every orchestration step;
- architecture must remain owned by the main Sol conversation, with bounded
  Sol Ultra design probes as the only parallel design route.

Prompt-only guidance is too easy to apply inconsistently. Extending the exact
`team-efficiency/decomposition-plan-v1` result would break consumers that
validate its fields exactly. The solution is a separate, backward-compatible
fast-lane compiler layered over the existing work-package compiler.

## 2. Goals

The fast lane must:

1. automatically activate when the host declares the main reasoning effort as
   `ultra`;
2. remain explicitly opt-in at all lower reasoning efforts;
3. treat the main Sol conversation as lane 0 and three subagents as additional
   lanes;
4. keep those three subagent lanes occupied with safe execution, review, or
   read-only prewarming whenever useful independent work exists;
5. refill a free slot at the next dispatch boundary without polling or waiting
   for a whole wave;
6. preserve exclusive write ownership and dependency ordering;
7. reduce verification duplication to targeted worker gates plus one
   integration regression pass and one blocker review;
8. keep workflow calls at work-item start and completion boundaries;
9. emit deterministic, canonical, bounded JSON suitable for exact-schema
   consumers;
10. reuse the current `decompose()` safety and Code Atlas evidence checks
    rather than invent semantic tasks.

## 3. Non-goals

The fast lane does not:

- introspect the active model or reasoning effort;
- spawn agents, call models, invoke MCP tools, edit files, run tests, or poll
  agent state itself;
- replace the main conversation's architectural judgment;
- turn blocked or dependency-ineligible tasks into writers;
- create filler work solely to report 100 percent utilization;
- add a new MCP tool surface;
- change any existing subcommand's input, output, behavior, routing value, or
  canonical serialization;
- guarantee that three safe independent tasks always exist.

The compiler produces a deterministic DAG dispatch contract plus a mandatory
terminal policy. The host and work-methodology skill execute them.

## 4. Decision

Add a standalone `fast-lane` command and a pure
`compile_fast_lane(...)` function to
`skills/work-methodology/scripts/team_efficiency.py`.

```text
python scripts/team_efficiency.py fast-lane \
  --input <fast-lane-request.json> \
  --reasoning-effort ultra
```

For non-Ultra use, the host must add `--enable`. Without `--enable`, a lower
effort returns a canonical inactive result and creates no assignments.

```text
python scripts/team_efficiency.py fast-lane \
  --input <fast-lane-request.json> \
  --reasoning-effort max \
  --enable
```

The command consumes a new exact request schema, calls the existing
`decompose()` function on its embedded unmodified work package, validates a
bounded scheduler snapshot, and emits
`team-efficiency/fast-lane-plan-v1`.

`--reasoning-effort` is required and accepts only `low`, `medium`, `high`,
`xhigh`, `max`, or `ultra`. `--enable` is a boolean CLI control, not a field
inside the JSON request.

The existing `decompose` and `plan-waves` commands continue to emit byte-for-
byte compatible `team-efficiency/decomposition-plan-v1` results.

## 5. Activation boundary

The script cannot discover the current model or reasoning effort. Automatic
Ultra behavior therefore lives at the host/skill boundary:

1. the Codex host knows or explicitly declares the main conversation's
   reasoning effort;
2. the work-methodology skill invokes `fast-lane` with
   `--reasoning-effort ultra` at the start of a substantial task;
3. the compiler records `activation.reason = "ultra_auto"`;
4. lower efforts require the host's explicit `--enable`, recorded as
   `activation.reason = "explicit_opt_in"`;
5. a mismatch or missing effort fails closed; caller-provided JSON alone
   cannot claim automatic Ultra activation.

The effort flag accepts only the host-supported closed set. The request JSON
does not contain a second, potentially contradictory effort field.

## 6. Lane architecture

### 6.1 Lane 0: main Sol

Lane 0 is outside the three-slot subagent capacity. It remains active while
workers run and owns:

- architecture and design decisions;
- decomposition and dispatch;
- cross-boundary or exceptionally difficult defects;
- write-scope conflict resolution;
- integration decisions;
- blocker adjudication;
- final verification and acceptance.

Lane 0 may implement work directly when it is the highest-leverage path. It
must not write concurrently inside a subagent's owned scope. The compiler
therefore emits the active exclusive write scopes and a lane-0 exclusion set.
Ownership must be explicitly transferred before lane 0 enters one of those
scopes.

### 6.2 Three additional subagent slots

The normal allocation is:

- two execution slots;
- one support slot for read-only prewarming or candidate review.

This allocation is elastic. The support slot may become a third execution slot
when a third dependency-ready, write-disjoint, useful unit exists and no
higher-value review or prewarm action is available. Conversely, all three
slots may perform read-only review/prewarming when no safe writer is ready.

The work package's existing `capacity` remains the upper bound for concurrent
execution writers. Fast lane never raises it: writer capacity is at most
`min(work_package.capacity, 3)`. The remaining physical slots may still do
read-only review or prewarming.

The scheduler never invents filler. If no safe and useful action exists, the
slot appears in `idle_slots` with a stable dependency, ownership, context, or
terminal-phase reason. This is the only allowed unsaturated state.

### 6.3 Model routing

Fast-lane routing is a policy overlay; it does not mutate the existing
`recommended_route` in decomposition results.

| Work class | Explicit route |
| --- | --- |
| Main architecture, exceptional work, integration, final acceptance | main `gpt-5.6-sol` lane 0 at the host-declared effort |
| Bounded parallel architecture alternative or adversarial design review | `model=gpt-5.6-sol`, `reasoning_effort=ultra` |
| Read-only prewarm | `model=gpt-5.6-terra`, `reasoning_effort=medium` |
| Candidate review | `model=gpt-5.6-terra`, `reasoning_effort=high` |
| Routine implementation, evidence, or verification | `model=gpt-5.6-terra`, `reasoning_effort=high` |
| Moderate or harder implementation/debugging/verification execution | `model=gpt-5.6-terra`, `reasoning_effort=max` |

Terra never owns architecture. A Sol Ultra design subagent returns candidate
reasoning; lane 0 retains the decision and acceptance. The legacy
`recommended_route = "Sol High"` value remains unchanged for compatibility,
but fast lane does not auto-dispatch such an exceptional unit. It returns
`LANE0_REQUIRED` so lane 0 can own or explicitly replan it.

## 7. Input contract

The exact top-level request is:

```json
{
  "schema": "team-efficiency/fast-lane-request-v1",
  "work_package": {
    "schema": "team-efficiency/work-package-v1",
    "task_id": "FAST-1",
    "goal": "Deliver a bounded result",
    "capacity": 3,
    "decomposition": "artifact_boundaries",
    "source_kind": "explicit_artifact_boundaries",
    "artifacts": [
      {
        "task_id": "FAST-1-A",
        "goal": "Implement one bounded artifact",
        "output_boundary": "fast-lane compiler",
        "write_scope": [
          "skills/work-methodology/scripts/team_efficiency.py"
        ],
        "depends_on": [],
        "required_evidence": ["focused-fast-lane-tests"],
        "complexity": "moderate",
        "execution_contracts": ["contracts/fast-lane-v1"]
      }
    ]
  },
  "target_gates": [
    {
      "task_id": "FAST-1-A",
      "driver_gate_id": "focused",
      "gates": [
        {
          "gate_id": "focused",
          "argv": [
            "python",
            "-m",
            "pytest",
            "skills/work-methodology/tests/test_team_efficiency.py",
            "-q"
          ],
          "red_expected_exit_codes": [1],
          "green_expected_exit_code": 0,
          "timeout_seconds": 300,
          "red_failure_ids": [
            "skills/work-methodology/tests/test_team_efficiency.py::FastLaneTests::test_ultra_auto"
          ],
          "red_failure_fingerprint": "sha256:...",
          "acceptance_constraint_hashes": []
        }
      ]
    }
  ],
  "execution_contexts": [],
  "read_contexts": [],
  "remediation_request": null,
  "scheduler_state": {
    "source_plan_hash": null,
    "phase": "execution",
    "integration_state": {
      "commit": "0123456789abcdef0123456789abcdef01234567",
      "tree": "89abcdef0123456789abcdef0123456789abcdef",
      "integration_workspace_snapshot_id": null
    },
    "lane0_state": {
      "active_task_id": null,
      "owned_write_scopes": []
    },
    "completed_tasks": [],
    "review_ready_candidates": [],
    "reviewed_candidates": [],
    "prewarmed_evidence": [],
    "design_evidence": [],
    "running_assignments": [],
    "dispatch_contexts": [],
    "blocked_task_ids": [],
    "pending_design_probe_task_ids": [],
    "slot_epochs": {
      "slot-1": 0,
      "slot-2": 0,
      "slot-3": 0
    },
    "global_remediation": {
      "round": 0,
      "state": "not_requested",
      "task_id": null,
      "affected_task_ids": [],
      "blocker_review_hash": null,
      "finding_hash": null,
      "dispatch_receipt": null,
      "completion_receipt_hash": null
    }
  }
}
```

`work_package` is the unchanged existing manifest. It receives the same byte
budget, exact-field validation, Code Atlas verification, path rules, conflict
checks, and fail-closed behavior as `decompose()`.

### 7.1 Target gates

`target_gates` is an exact, bounded list keyed by known unit ID. Each record
contains an ordered, bounded `gates` list whose `gate_id` values are unique
inside the unit. Each gate contains only `gate_id`, inert `argv`,
`red_expected_exit_codes`, one `green_expected_exit_code`, bounded positive
`timeout_seconds`, `red_failure_ids`, nullable `red_failure_fingerprint`, and
`acceptance_constraint_hashes`. `argv` is never a shell string; it is subject
to command length, item count, secret, control-character, traversal, and
absolute-path rejection. RED and GREEN for one gate use the same `argv`.
`timeout_seconds` is an integer in the closed range
`1..MAX_GATE_TIMEOUT_SECONDS`; v1 fixes
`MAX_GATE_TIMEOUT_SECONDS = 3600`.

Every unit eligible for `execution` or `verification` has exactly one
`target_gates` record. An execution record names exactly one
`driver_gate_id`; that gate requires at least one bounded non-zero RED exit
code and a non-null canonical fingerprint of the expected bounded failure
identity. A RED result must match both, so an unrelated import, collection, or
environment failure is not accepted. Other gates may have an empty RED set
and empty failure IDs/null fingerprint, but every gate must return its exact GREEN code before
the result is eligible for lane-0 adjudication. Raw failure output stays in
host-side evidence and is never serialized. A verification record has
`driver_gate_id = null`, every RED set is empty, every failure fingerprint is
null, every failure-ID list is empty, and every gate must return GREEN because
it validates already integrated work rather than driving a new implementation.
Prewarm, review, and design-probe roles carry no gates.

Failure IDs are a non-empty, unique, lexicographically normalized bounded list
of stable test/assertion identifiers supplied by the verified gate contract;
they reject control characters, secrets, absolute paths, and traversal. The
fingerprint is the canonical SHA-256 of exact
`{"schema":"team-efficiency/red-failure-identity-v1",
"gate_id":<gate_id>,"failure_ids":<normalized list>}`. The host's test adapter
must attest the same normalized IDs in the RED evidence artifact. If the
adapter cannot identify the configured failure, RED fails closed; raw output
is never used as canonical identity.

Before hashing and output, gates are normalized by ascending `gate_id`.
`target_gates_hash` is the canonical SHA-256 of exact
`{"driver_gate_id": <value>, "target_gates": <complete normalized list>}`. It
is non-null for execution and verification and null for roles whose
`driver_gate_id` is null and `target_gates` list is empty.

The compiler does not infer commands from `goal`, `required_evidence`, a resume
summary, or arbitrary prose. A worker result is eligible for lane-0
adjudication only when its red and green evidence hashes are bound to this
exact gate.

For `code_atlas_packet`, each verified acceptance constraint is the canonical
hash of exact `{argv, expected_exit_code}` data and must be covered by one gate
with the same canonical command and expected GREEN code. For
`task_episode_graph`, each gate must match a verified command receipt and its
matching TestSpec in the original graph; the gate records the corresponding
canonical constraint hash while the normalized unit retains the TestSpec node
IDs. Across a unit, the union of `acceptance_constraint_hashes` must equal the
complete verified constraint set exactly. Unknown, duplicate, ambiguous, or
missing coverage fails with `ATLAS_GATE_UNVERIFIED`. A unit with multiple
constraints therefore has multiple conjunctive gates; selecting only one is
not valid acceptance.

For a manual artifact with no Atlas constraints, exactly one focused gate is
required and its `acceptance_constraint_hashes` is empty. Gates are additional
evidence and never replace, rewrite, or weaken Atlas constraints.

An Atlas-derived `unit_kind = "verification"` remains a schedulable part of
the source DAG but is never an implementation writer. Once all of its
dependencies are durably completed, the compiler schedules it in
`integration_regression` with `role = "verification"`, its complete gate list,
and a validated read context. It needs no writer bootstrap plan and cannot be
silently reinterpreted as artifact execution, prewarm, review, or design.

### 7.2 Isolated execution contexts

`execution_contexts` contains exact records of `{task_id, bootstrap_plan,
workspace_input_snapshot_id}`. Each nested plan must pass the existing
`_validated_bootstrap_plan()` check, match the unit's write scope and task ID,
and use an independent branch, worker worktree, and task-specific D-drive
temporary root. `workspace_input_snapshot_id` is either `null` for a
non-strict task or the Project Index snapshot obtained by synchronizing that
exact worker worktree before compilation. A strict-index task requires a
non-null snapshot.
`execution_context_hash` is the compiler-recomputed canonical SHA-256 of the
complete validated `{task_id, bootstrap_plan, workspace_input_snapshot_id}`
record; a caller-supplied context hash is never trusted.
The fast-lane result emits only the execution-context hash, bootstrap-plan
hash, base commit, branch, write-scope hash, and snapshot ID; it never emits
the absolute repo, worktree, or temporary path.

A task has at most one execution context. Any `action = "start"` execution
assignment or execution item in `ready_queue` requires exactly one. A unit
without one can only be read-only-prewarmed or reported with
`EXECUTION_CONTEXT_MISSING`; it cannot become a writer. Across contexts,
branch, worktree, and temp target are each unique, and no worker worktree may
equal the integration worktree. Every nested `bootstrap_plan.repo` is the
canonical shared integration-worktree anchor and all such anchors must agree;
therefore the compiler can compare each planned worker worktree with that
anchor without adding a request field. The host re-resolves and attests the
trusted integration worktree, requires its canonical path to equal the request
anchor, and attests the same non-equality before applying a bootstrap plan.
After apply it also verifies the planned target and Git common-directory
identity. A failed pre-apply attestation must not run `git worktree add`. A new
execution dispatch requires
`bootstrap_plan.base_commit == integration_state.commit`; a retained worker
may carry an older immutable base only through its already validated token.
Lease recovery is not a fresh execution dispatch. It may reuse the exact
ledgered execution/dispatch context and older integration basis after the
current scope/conflict and workspace checks pass, while issuing a new
assignment epoch, dispatch receipt, and token whose
`recovery_of_assignment_token` points to the immediately superseded token.
It does not rebase or mutate the old context.
The minimal JSON example intentionally leaves contexts empty and therefore
does not make `FAST-1-A` writer-eligible.

Multiple writers never operate in the shared integration worktree. A
candidate result must contain a Git commit plus RED/GREEN evidence. Lane 0
integrates it after conflict review. Writers started by one compilation use
the same declared integration revision. A later refill may use a newer
integration revision only after recompilation; retained workers keep their
immutable earlier base and require explicit revalidation when integrated.

For a strict-index writer, the registered workflow task, input query,
pre-write checkpoint, writes, output sync/query, and artifact all use the
worker worktree from this execution context. Only its base commit is required
to equal `integration_state.commit`; the shared integration worktree is never
substituted as the strict task's workspace.

### 7.3 Isolated read contexts

`read_contexts` is an exact host-side list. Each record contains exactly:

```json
{
  "task_id": "TASK-1-VERIFY",
  "role": "verification",
  "repo": "validated absolute repository path",
  "worktree": "validated absolute detached worktree path",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "tree": "89abcdef0123456789abcdef0123456789abcdef",
  "workspace_input_snapshot_id": "sha256:...",
  "read_scope": ["relative/path"],
  "temp_target": "validated absolute D-drive task temporary path"
}
```

`role` is one of `verification`, `prewarm`, `review`, or `design_probe`.
`(task_id, role)` is unique. Git object IDs are complete 40- or 64-character
lowercase hexadecimal IDs. Repository and worktree must resolve to the same
repository. Every `repo` is the same canonical shared
integration-worktree anchor used by execution contexts; a differing anchor
fails closed. `read_scope` is normalized, relative, bounded, and read-only.
Worktree and temp paths must be unique across all contexts, must not equal the
shared integration worktree, and the temp path must remain inside the
task-specific `D:\bun\tmp\codex\<project-or-thread>` root. Test caches,
bytecode, logs, and other incidental writes are redirected to `temp_target`;
the source worktree remains unchanged. The host re-resolves repository identity
and canonical paths before it executes a read-only descriptor, requires the
request anchor to equal its trusted shared integration worktree, and records
the non-equality as host evidence; it never substitutes the integration
worktree. Compiler validation proves request consistency, not trusted-anchor
identity.

A new verification dispatch requires exactly one read context whose commit and
tree match the current integration state and whose non-null input snapshot was
obtained by synchronizing that exact read worktree. Project Index snapshot
identity is workspace-specific: it must not be required to equal the optional
snapshot of the shared integration worktree. Other read-only roles also
require one read context, but review may bind the candidate commit/tree instead
of the current integration tree. Retained read-only assignments keep their
original immutable context. Output exposes only `read_context_hash`, base
commit/tree, and the workspace-specific input snapshot; it never exposes the
absolute paths.
`read_context_hash` is the compiler-recomputed canonical SHA-256 of the
complete validated read-context record, including its host-side absolute
paths. Those paths affect identity but never appear in output; a
caller-supplied context hash is rejected.

### 7.4 Immutable dispatch contexts

`scheduler_state.dispatch_contexts` is the immutable ledger used to validate
`start`, `retain`, and terminal results. Every record contains all of the
following fields; role-inapplicable scalar fields are `null` and
role-inapplicable lists are empty:

```json
{
  "context_hash": "sha256:...",
  "task_id": "TASK-1-A",
  "role": "execution",
  "source_plan_hash": "sha256:...",
  "integration_commit": "0123456789abcdef0123456789abcdef01234567",
  "integration_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "workspace_input_snapshot_id": "sha256:...",
  "direct_dependency_result_hashes": [],
  "direct_contract_hashes": [],
  "required_evidence": ["focused-tests"],
  "task_node_ids": [],
  "contract_node_ids": [],
  "acceptance_constraints": [],
  "execution_context_hash": "sha256:...",
  "bootstrap_plan_hash": "sha256:...",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "branch": "codex/task-1-a",
  "write_scope_hash": "sha256:...",
  "read_context_hash": null,
  "target_gates_hash": "sha256:...",
  "candidate_commit": null,
  "red_evidence_hashes": [],
  "green_evidence_hashes": [],
  "basis_hash": null,
  "prewarm_evidence_hash": null,
  "prewarm_revalidation_evidence_hash": null
}
```

`context_hash` is the canonical hash of the record before that field is added.
There is exactly one record for each running assignment and none may share a
`context_hash`. Every `running_assignments.context_hash` must reference the
exact record for the same task and role. Once its assignment token is issued,
the record is immutable: a changed integration basis, dependency result,
contract, gate, execution/read context, candidate, or evidence reference
requires a new context, incremented slot epoch, and new token.

An `execution` context has non-null execution/bootstrap/base/branch/scope
fields and no read or candidate fields. A `verification` context has one
`read_context_hash`, complete target gates, and no execution or candidate
fields. A `review` context has one read context plus non-null candidate, RED,
and GREEN evidence hashes. `prewarm` and `design_probe` have one read context
and a non-null `basis_hash`. Only execution may additionally carry a
successfully revalidated prewarm evidence/hash pair. The context for a newly
started execution or verification assignment must match the current
integration state and dependency results. A retained assignment may preserve
an older integration basis, but the compiler copies its existing record and
token verbatim rather than recomputing either. A recovery assignment may
preserve that same older context while replacing only the
epoch/receipt/token; lane 0 later validates the recovered scoped diff against
the then-current integration tree through the integration proof.

Context records remain in the ledger for as long as any running, terminal,
review, annotation, or completion record references their dispatch receipt.
During an active run they are not removed merely because a running assignment
became a candidate or completed task. This lets a restarted pure compiler
recompute every referenced context, receipt, token, and terminal hash. They may
be archived only with the final accepted run artifact after no further compile
can consume that scheduler state. A late result whose context or token is no
longer current is ignored.

### 7.5 One durable remediation request

`remediation_request` is `null` unless lane 0 has accepted the independent
blocker review and authorized the single global remediation round. When
present it has this exact shape:

```json
{
  "schema": "team-efficiency/fast-lane-remediation-request-v1",
  "round": 1,
  "task_id": "FLR1-96795cff299da0de3e7bfca7",
  "source_plan_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "blocker_review_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "finding_hash": "sha256:...",
  "severity": "important",
  "affected_task_ids": ["FAST-1-A"],
  "dependencies": ["FAST-1-A", "FAST-1-VERIFY"],
  "base_integration_commit": "0123456789abcdef0123456789abcdef01234567",
  "base_integration_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "goal": "Apply one bounded approved remediation",
  "output_boundary": "bounded remediation candidate",
  "write_scope": ["path/owned/by/worker.py"],
  "direct_contract_hashes": ["sha256:..."],
  "required_evidence": ["focused-remediation-test"],
  "task_node_ids": ["sha256:..."],
  "contract_node_ids": ["sha256:..."],
  "acceptance_constraints": [],
  "driver_gate_id": "remediation-focused",
  "target_gates": [
    {
      "gate_id": "remediation-focused",
      "argv": ["python", "-m", "pytest", "tests/test_target.py", "-q"],
      "red_expected_exit_codes": [1],
      "green_expected_exit_code": 0,
      "timeout_seconds": 300,
      "red_failure_ids": ["tests/test_target.py::test_remediation"],
      "red_failure_fingerprint": "sha256:...",
      "acceptance_constraint_hashes": []
    }
  ]
}
```

The compiler computes
`seed = sha256(canonical({"schema":"fast-lane-remediation-id-v1",
"source_plan_hash":source_plan_hash,"blocker_review_hash":blocker_review_hash,
"round":1}))` and requires `task_id = "FLR1-" + seed.hex[:24]`. The ID is
different from every source-plan task and must be globally unused when
`workflow_register_task` is called; a collision stops automation rather than
changing the seed or retrying another ID. Severity is `critical` or
`important`. Every affected task is an already-`DONE` original writer unit,
never a verification or remediation unit, remains immutable, and appears in
`dependencies`; any applicable declared verification units also appear there.
Every dependency is an exact same-workflow task ID and must already be complete
before `workflow_ready` may promote remediation.

The base commit/tree equals the current integration state. Write scope is one
lane-0-approved bounded subset of the affected original scope; shared scope,
multiple original unit boundaries, or invalidation of multiple regression
shards yields `AUTOMATION_STOPPED`. The request carries a complete exact gate
bundle and non-empty strict-index task-node evidence. A matching
`execution_contexts` entry is required, but the compiler recomputes its hash
instead of accepting one in this request.

This is the only compiler-created unit not present in the source
decomposition. It is created from the exact authorized record, never from
finding prose, uses `unit_kind = "remediation"` with ordinary
`role = "execution"`, and follows the full strict-writer receipt and workflow
lifecycle. It does not reuse or mutate any original task lease, snapshot,
artifact, result, version, or completion receipt. Its completion receipt binds
the remediated final integration commit/tree; final acceptance references
both the immutable original results and this new result.

### 7.6 Scheduler state

`scheduler_state` is exact and bounded:

- `source_plan_hash` is `null` only for a pristine first compile. If any
  completed, candidate, evidence, running, blocked, design, epoch, or
  remediation state exists, it must equal the freshly compiled decomposition
  hash. Every later request must echo the exact prior hash. A changed work
  package cannot inherit state merely because task IDs match.
- `phase` is one of `execution`, `integration_regression`,
  `blocker_review`, `remediation`, `acceptance`, or `stopped`. Original
  implementation units are scheduled only in `execution`; declared
  verification units are scheduled in `integration_regression`; blocker
  review, a possible remediation unit, and acceptance follow Section 10.
- `integration_state` binds the current integration commit, Git tree, and an
  optional `integration_workspace_snapshot_id` for lane 0's integration
  worktree. New dispatch contexts copy
  the commit/tree, but their `workspace_input_snapshot_id` comes from their
  own validated execution or read worktree and is not compared for equality
  with the integration-worktree snapshot.
- `lane0_state` records lane 0's optional active unit and all currently owned
  relative write scopes. Those scopes enter the same equal-path and
  ancestor/descendant conflict graph as worker scopes.
- `completed_tasks` contains exact records of `task_id`, `completion_kind`,
  `integration_commit`, `integration_tree`, `result_hash`,
  `terminal_result_hash`, embedded `terminal_result`,
  `completion_receipt_hash`, and embedded `completion_receipt`.
  `completion_kind` is `integrated_candidate` for a writer or
  `verification_evidence` for a declared read-only verification unit. A writer
  enters this set only after lane 0 integrates its candidate, accepts its
  evidence, registers the completion receipt as the result artifact, and
  successfully calls `workflow_complete`. A verification unit follows its
  separate no-candidate path in Section 10.
- `review_ready_candidates` contains exact records of `task_id`,
  `candidate_commit`, `candidate_tree`, ordered `red_evidence_hashes`,
  ordered `green_evidence_hashes`, `terminal_result_hash`, and embedded
  `terminal_result`. The embedded envelope preserves the dispatch receipt and
  strict query/checkpoint/output references across restart.
- `reviewed_candidates` contains the same candidate identity plus
  `review_hash`, `outcome = "pass"`, `review_terminal_result_hash`, and
  embedded `review_terminal_result`. Both terminal envelopes preserve their
  own dispatch receipts. Review is advisory: the candidate is still incomplete
  until lane 0 integrates and accepts it.
- `prewarmed_evidence` and `design_evidence` contain `task_id`,
  `observation_basis_hash`, `evidence_hash`, `terminal_result_hash`, and
  embedded `terminal_result`. Prewarm records additionally contain nullable
  `revalidation_basis_hash`, `dependency_delta_hash`, and
  `revalidation_evidence_hash`; their rules appear in Section 9.
- `running_assignments` contains at most three exact records with `slot_id`,
  `task_id`, `role`, `assignment_epoch`, `assignment_token`, `context_hash`,
  `model`, `reasoning_effort`, and `dispatch_receipt`. All duplicated values
  must agree exactly.
- `blocked_task_ids` are known units not eligible for redispatch until lane 0
  changes the state.
- `pending_design_probe_task_ids` are known, lane-0-approved bounded design
  units. At most one may run. A design probe is read-only, uses Sol Ultra, and
  occupies one of the same three physical slots.
- `slot_epochs` always contains exactly `slot-1`, `slot-2`, and `slot-3`.
  A new dispatch increments that slot's epoch; a retained assignment preserves
  it.
- `global_remediation.round` is `0` or `1`; `state` is `not_requested`,
  `approved`, `running`, `completed`, or `stopped`. At round zero all other
  fields are null/empty. At round one the task ID is a new deterministic
  workflow-wide ID, the affected IDs are completed source units, and the
  blocker/finding hashes are non-null. A running unit carries its own dispatch
  receipt; a completed unit carries its own completion receipt hash. This is
  one budget for the entire run, not one budget per source task, and it never
  reopens or rewrites a completed source unit.
  `global_remediation.task_id` must equal `remediation_request.task_id`; this
  exact remediation ID is the only scheduler task ID permitted outside the
  source-plan unit set.

Allowed roles are `execution`, `verification`, `prewarm`, `review`, and
`design_probe`. Verification, prewarm, review, and design probes are read-only
and confer no write ownership. Verification is permitted only for
`unit_kind = "verification"` in `integration_regression`. A design probe
always uses `model=gpt-5.6-sol` and `reasoning_effort=ultra`; ordinary
evidence, implementation, and verification use only the explicit Terra routes
in Section 6.3. Prewarm is the intentional lower-cost exception and always
uses `model=gpt-5.6-terra`, `reasoning_effort=medium`.

All scheduler lists reject unknown fields and are bounded by the work-package
unit limit plus the single remediation exception. Their exact keys and
canonical order are:

| List | Exact required keys | Nullable keys | Unique/sort rule |
| --- | --- | --- | --- |
| `completed_tasks` | `task_id`, `completion_kind`, `integration_commit`, `integration_tree`, `result_hash`, `terminal_result_hash`, `terminal_result`, `completion_receipt_hash`, `completion_receipt` | none | unique/sort by `task_id` |
| `review_ready_candidates` | `task_id`, `candidate_commit`, `candidate_tree`, `red_evidence_hashes`, `green_evidence_hashes`, `terminal_result_hash`, `terminal_result` | none | unique/sort by `task_id` |
| `reviewed_candidates` | all review-ready keys plus `review_hash`, `outcome`, `review_terminal_result_hash`, `review_terminal_result` | none; `outcome` is exactly `pass` | unique/sort by `task_id` |
| `prewarmed_evidence` | `task_id`, `observation_basis_hash`, `evidence_hash`, `terminal_result_hash`, `terminal_result`, `revalidation_basis_hash`, `dependency_delta_hash`, `revalidation_evidence_hash` | the three revalidation fields are jointly null or jointly non-null | unique/sort by `task_id` |
| `design_evidence` | `task_id`, `observation_basis_hash`, `evidence_hash`, `terminal_result_hash`, `terminal_result` | none | unique/sort by `task_id` |
| `running_assignments` | `slot_id`, `task_id`, `role`, `assignment_epoch`, `assignment_token`, `context_hash`, `model`, `reasoning_effort`, `dispatch_receipt` | none | unique slot/task/token; fixed slot order |

`blocked_task_ids` is unique and sorted by task ID.
`review_ready_candidates` and `reviewed_candidates` are disjoint; a reviewed
record replaces its byte-identical candidate identity. Completed, candidate,
running, and blocked task sets are pairwise disjoint. Fresh prewarm/design
records are annotations, but a task cannot appear in both annotation lists or
run the same annotation role concurrently. `assignment_epoch` is positive and
no greater than its slot's current epoch. All embedded receipt hashes are
recomputed, not trusted.

A task cannot appear in more than one exclusive lifecycle set (`completed`,
candidate, running, or blocked). Prewarm/design evidence is an annotation and
may remain when a unit later executes, but the same task cannot simultaneously
run the corresponding evidence role. Lane 0's active unit cannot also be a
subagent assignment.

Every assignment has an exact immutable dispatch receipt:

```json
{
  "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
  "source_plan_hash": "sha256:...",
  "task_id": "TASK-1-A",
  "role": "execution",
  "slot_id": "slot-1",
  "assignment_epoch": 1,
  "model": "gpt-5.6-terra",
  "reasoning_effort": "high",
  "dispatch_context_hash": "sha256:...",
  "target_gates_hash": "sha256:...",
  "execution_context_hash": "sha256:...",
  "read_context_hash": null,
  "recovery_of_assignment_token": null
}
```

`assignment_token` is the canonical hash of this entire receipt and the receipt
is persisted before spawn. A terminal lifecycle record embeds that exact
receipt; therefore a later pure compilation can recompute the token even after
the record replaces `running_assignments`. Review records also preserve both
the candidate's execution receipt and the reviewer's receipt. A token whose
role, slot, epoch, context, gate, model, effort, or context-kind hash differs
is invalid. Late, duplicated, cross-role, or superseded results are ignored
and cannot mutate candidate, completion, or dependency state.

`recovery_of_assignment_token` is null for an initial/refill dispatch and the
immediately superseded token for a lease-recovery dispatch. That predecessor
must resolve to the same task, role, and immutable context; only slot epoch and
receipt/token identity advance. Chains that skip, fork, or point across
contexts fail closed.

The compiler validates canonical records and hash chains, but it cannot prove
that Git, Project Index, workflow, or model events really occurred. The Codex
host and work-methodology skill are the trusted attesters for commit/tree
identity, query and checkpoint receipts, artifact registration, lease state,
and successful completion. Untrusted worker prose never becomes scheduler
state directly.

A worker terminal event is accepted only through this exact envelope:

```json
{
  "schema": "team-efficiency/fast-lane-terminal-result-v1",
  "dispatch_receipt": {
    "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
    "source_plan_hash": "sha256:...",
    "task_id": "TASK-1-A",
    "role": "execution",
    "slot_id": "slot-1",
    "assignment_epoch": 1,
    "model": "gpt-5.6-terra",
    "reasoning_effort": "high",
    "dispatch_context_hash": "sha256:...",
    "target_gates_hash": "sha256:...",
    "execution_context_hash": "sha256:...",
    "read_context_hash": null,
    "recovery_of_assignment_token": null
  },
  "assignment_token": "sha256:...",
  "task_id": "TASK-1-A",
  "role": "execution",
  "outcome": "candidate",
  "candidate_commit": "0123456789abcdef0123456789abcdef01234567",
  "candidate_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "red_evidence_hashes": ["sha256:..."],
  "green_evidence_hashes": ["sha256:..."],
  "evidence_hash": null,
  "review_hash": null,
  "input_query_trace_id": "sha256:...",
  "checkpoint_id": "sha256:...",
  "output_workspace_snapshot_id": "sha256:...",
  "output_query_trace_id": "sha256:..."
}
```

All fields are always present. Role-inapplicable scalar fields are `null` and
evidence lists are empty. `assignment_token` must equal
`sha256(canonical(dispatch_receipt))`, and task/role/token must match the
embedded receipt and current context. `terminal_result_hash` is separately
defined as `sha256(canonical(terminal-result envelope))`; it is never equal to
the assignment token by definition. Execution uses `outcome = "candidate"`, a
commit/tree, every required GREEN gate, at least the designated driver RED,
and all four strict-index trace/checkpoint/output fields for a strict task.
Verification uses `outcome = "verified"`, all GREEN gates, an input query
trace, no candidate/checkpoint/output fields, and one `evidence_hash`.
Prewarm, design-probe, and review use `outcome = "evidence"` or `"pass"` with
only their role-specific evidence field. `blocked`, `failed`, and `obsolete`
outcomes carry no success evidence and never advance task state.

After host validation and, for a writer, lane-0 integration, completion uses an
exact immutable receipt:

```json
{
  "schema": "team-efficiency/fast-lane-completion-receipt-v1",
  "terminal_result_hash": "sha256:...",
  "workflow_id_hash": "sha256:...",
  "task_id": "TASK-1-A",
  "completion_kind": "integrated_candidate",
  "integration_commit": "0123456789abcdef0123456789abcdef01234567",
  "integration_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "candidate_commit": "fedcba9876543210fedcba9876543210fedcba98",
  "candidate_tree": "76543210fedcba9876543210fedcba9876543210",
  "integration_proof_hash": "sha256:...",
  "workspace_input_snapshot_id": "sha256:...",
  "output_workspace_snapshot_id": "sha256:...",
  "verification_evidence_hashes": ["sha256:..."]
}
```

For a writer, `integration_proof_hash` is the canonical SHA-256 of this exact
host-attested preimage:

```json
{
  "schema": "team-efficiency/fast-lane-integration-proof-v1",
  "terminal_result_hash": "sha256:...",
  "base_integration_commit": "0123456789abcdef0123456789abcdef01234567",
  "base_integration_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "candidate_commit": "fedcba9876543210fedcba9876543210fedcba98",
  "candidate_tree": "76543210fedcba9876543210fedcba9876543210",
  "final_integration_commit": "1111111111111111111111111111111111111111",
  "final_integration_tree": "2222222222222222222222222222222222222222",
  "write_scope_hash": "sha256:...",
  "candidate_scoped_diff_hash": "sha256:...",
  "integrated_scoped_diff_hash": "sha256:..."
}
```

The two scoped-diff hashes must be equal. The host derives them from Git using
the validated base and write scope; workers cannot supply the attestation.

For `integrated_candidate`, the host independently proves that the candidate's
scoped diff was applied exactly to the recorded integration commit/tree and
records that canonical comparison as `integration_proof_hash`; candidate and
integration trees need not be equal when other disjoint candidates were
integrated first. For `verification_evidence`, candidate fields,
`integration_proof_hash`, and `output_workspace_snapshot_id` are null, the recorded
integration commit/tree equals the read context, and all declared verification
gate hashes appear in `verification_evidence_hashes`.

The canonical completion-receipt hash is both the verification artifact hash
registered against the role-appropriate snapshot and the `result_hash` passed
to `workflow_complete`. A different artifact hash, snapshot, integration
tree, or terminal-result hash fails closed.

Unknown fields, unknown task IDs, duplicate assignments, contradictory states,
unsafe work packages, invalid gates, invalid bootstrap plans, stale source
state, overlapping writers, oversized input, or a second remediation round
are rejected without a partial plan.

If `decompose()` returns `needs_design`, the fast lane emits no implementation
or prewarm assignment. Lane 0 receives
`main_lane.next_action = "design_required"`. Lane 0 may later submit a new,
approved work package containing bounded evidence probes; the compiler never
manufactures them from a failed decomposition.

## 8. Output contract

The output has exact schema
`team-efficiency/fast-lane-plan-v1`. Its exact top-level fields are shown
below. The active values illustrate a larger valid request than the minimal
input example in Section 7:

```json
{
  "schema": "team-efficiency/fast-lane-plan-v1",
  "status": "active",
  "decision_code": "FAST_LANE_ACTIVE",
  "activation": {
    "reasoning_effort": "ultra",
    "reason": "ultra_auto"
  },
  "source_plan_hash": "sha256:...",
  "phase": "execution",
  "main_lane": {
    "lane_id": "lane-0",
    "model": "gpt-5.6-sol",
    "reasoning_effort": "ultra",
    "next_action": "adjudicate_and_integrate",
    "design_owner": "main-sol",
    "parallel_design": {
      "model": "gpt-5.6-sol",
      "reasoning_effort": "ultra",
      "max_concurrent": 1
    },
    "owned_write_scopes": [],
    "excluded_write_scopes": ["path/owned/by/worker.py"]
  },
  "subagent_capacity": 3,
  "assignments": [
    {
      "slot_id": "slot-1",
      "action": "start",
      "assignment_epoch": 1,
      "assignment_token": "sha256:...",
      "dispatch_receipt": {
        "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
        "source_plan_hash": "sha256:...",
        "task_id": "TASK-1-A",
        "role": "execution",
        "slot_id": "slot-1",
        "assignment_epoch": 1,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "dispatch_context_hash": "sha256:...",
        "target_gates_hash": "sha256:...",
        "execution_context_hash": "sha256:...",
        "read_context_hash": null,
        "recovery_of_assignment_token": null
      },
      "task_id": "TASK-1-A",
      "goal": "Implement one bounded artifact",
      "output_boundary": "artifact A",
      "unit_kind": "artifact",
      "operation_count": 0,
      "recommended_route": "Terra High",
      "role": "execution",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "high",
      "access": "exclusive_write",
      "context_hash": "sha256:...",
      "execution_context_hash": "sha256:...",
      "read_context_hash": null,
      "workspace_input_snapshot_id": "sha256:...",
      "read_base_commit": null,
      "read_tree": null,
      "base_commit": "0123456789abcdef0123456789abcdef01234567",
      "bootstrap_plan_hash": "sha256:...",
      "branch": "codex/task-1-a",
      "write_scope_hash": "sha256:...",
      "write_scope": ["path/owned/by/worker.py"],
      "depends_on": [],
      "unmet_dependencies": [],
      "required_evidence": ["focused-tests"],
      "execution_contracts": ["contracts/task-a"],
      "direct_contract_hashes": [],
      "task_node_ids": [],
      "contract_node_ids": [],
      "acceptance_constraints": [],
      "driver_gate_id": "focused",
      "target_gates": [
        {
          "gate_id": "focused",
          "argv": ["python", "-m", "pytest", "tests/test_target.py", "-q"],
          "red_expected_exit_codes": [1],
          "green_expected_exit_code": 0,
          "timeout_seconds": 300,
          "red_failure_ids": ["tests/test_target.py::test_target"],
          "red_failure_fingerprint": "sha256:...",
          "acceptance_constraint_hashes": []
        }
      ],
      "candidate_commit": null,
      "basis_hash": null
    },
    {
      "slot_id": "slot-2",
      "action": "retain",
      "assignment_epoch": 4,
      "assignment_token": "sha256:...",
      "dispatch_receipt": {
        "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
        "source_plan_hash": "sha256:...",
        "task_id": "TASK-1-B",
        "role": "execution",
        "slot_id": "slot-2",
        "assignment_epoch": 4,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "dispatch_context_hash": "sha256:...",
        "target_gates_hash": "sha256:...",
        "execution_context_hash": "sha256:...",
        "read_context_hash": null,
        "recovery_of_assignment_token": null
      },
      "task_id": "TASK-1-B",
      "goal": "Implement another bounded artifact",
      "output_boundary": "artifact B",
      "unit_kind": "artifact",
      "operation_count": 0,
      "recommended_route": "Terra Max",
      "role": "execution",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "max",
      "access": "exclusive_write",
      "context_hash": "sha256:...",
      "execution_context_hash": "sha256:...",
      "read_context_hash": null,
      "workspace_input_snapshot_id": "sha256:...",
      "read_base_commit": null,
      "read_tree": null,
      "base_commit": "0123456789abcdef0123456789abcdef01234567",
      "bootstrap_plan_hash": "sha256:...",
      "branch": "codex/task-1-b",
      "write_scope_hash": "sha256:...",
      "write_scope": ["another/disjoint/path.py"],
      "depends_on": [],
      "unmet_dependencies": [],
      "required_evidence": ["focused-tests"],
      "execution_contracts": ["contracts/task-b"],
      "direct_contract_hashes": [],
      "task_node_ids": [],
      "contract_node_ids": [],
      "acceptance_constraints": [],
      "driver_gate_id": "focused",
      "target_gates": [
        {
          "gate_id": "focused",
          "argv": ["python", "-m", "pytest", "tests/test_other.py", "-q"],
          "red_expected_exit_codes": [1],
          "green_expected_exit_code": 0,
          "timeout_seconds": 300,
          "red_failure_ids": ["tests/test_other.py::test_other"],
          "red_failure_fingerprint": "sha256:...",
          "acceptance_constraint_hashes": []
        }
      ],
      "candidate_commit": null,
      "basis_hash": null
    },
    {
      "slot_id": "slot-3",
      "action": "start",
      "assignment_epoch": 2,
      "assignment_token": "sha256:...",
      "dispatch_receipt": {
        "schema": "team-efficiency/fast-lane-dispatch-receipt-v1",
        "source_plan_hash": "sha256:...",
        "task_id": "TASK-1-C",
        "role": "prewarm",
        "slot_id": "slot-3",
        "assignment_epoch": 2,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "dispatch_context_hash": "sha256:...",
        "target_gates_hash": null,
        "execution_context_hash": null,
        "read_context_hash": "sha256:...",
        "recovery_of_assignment_token": null
      },
      "task_id": "TASK-1-C",
      "goal": "Prewarm a future bounded artifact",
      "output_boundary": "artifact C",
      "unit_kind": "artifact",
      "operation_count": 0,
      "recommended_route": "Terra High",
      "role": "prewarm",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "medium",
      "access": "read_only",
      "context_hash": "sha256:...",
      "execution_context_hash": null,
      "read_context_hash": "sha256:...",
      "workspace_input_snapshot_id": "sha256:...",
      "read_base_commit": "0123456789abcdef0123456789abcdef01234567",
      "read_tree": "89abcdef0123456789abcdef0123456789abcdef",
      "base_commit": null,
      "bootstrap_plan_hash": null,
      "branch": null,
      "write_scope_hash": "sha256:...",
      "write_scope": ["future/task/path.py"],
      "depends_on": ["TASK-1-A"],
      "unmet_dependencies": ["TASK-1-A"],
      "required_evidence": ["implementation-map"],
      "execution_contracts": ["contracts/task-c"],
      "direct_contract_hashes": [],
      "task_node_ids": [],
      "contract_node_ids": [],
      "acceptance_constraints": [],
      "driver_gate_id": null,
      "target_gates": [],
      "candidate_commit": null,
      "basis_hash": "sha256:..."
    }
  ],
  "ready_queue": [
    {
      "task_id": "TASK-1-D",
      "goal": "Implement a ready bounded artifact",
      "output_boundary": "artifact D",
      "unit_kind": "artifact",
      "operation_count": 0,
      "recommended_route": "Terra High",
      "role": "execution",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "high",
      "access": "exclusive_write",
      "context_hash": "sha256:...",
      "execution_context_hash": "sha256:...",
      "read_context_hash": null,
      "workspace_input_snapshot_id": "sha256:...",
      "read_base_commit": null,
      "read_tree": null,
      "base_commit": "0123456789abcdef0123456789abcdef01234567",
      "bootstrap_plan_hash": "sha256:...",
      "branch": "codex/task-1-d",
      "write_scope_hash": "sha256:...",
      "write_scope": ["ready/disjoint/path.py"],
      "depends_on": [],
      "unmet_dependencies": [],
      "required_evidence": ["focused-tests"],
      "execution_contracts": ["contracts/task-d"],
      "direct_contract_hashes": [],
      "task_node_ids": [],
      "contract_node_ids": [],
      "acceptance_constraints": [],
      "driver_gate_id": "focused",
      "target_gates": [
        {
          "gate_id": "focused",
          "argv": ["python", "-m", "pytest", "tests/test_ready.py", "-q"],
          "red_expected_exit_codes": [1],
          "green_expected_exit_code": 0,
          "timeout_seconds": 300,
          "red_failure_ids": ["tests/test_ready.py::test_ready"],
          "red_failure_fingerprint": "sha256:...",
          "acceptance_constraint_hashes": []
        }
      ],
      "candidate_commit": null,
      "basis_hash": null
    }
  ],
  "review_queue": [],
  "prewarm_queue": [
    {
      "task_id": "TASK-1-E",
      "goal": "Prewarm another future artifact",
      "output_boundary": "artifact E",
      "unit_kind": "artifact",
      "operation_count": 0,
      "recommended_route": "Terra High",
      "role": "prewarm",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "medium",
      "access": "read_only",
      "context_hash": "sha256:...",
      "execution_context_hash": null,
      "read_context_hash": "sha256:...",
      "workspace_input_snapshot_id": "sha256:...",
      "read_base_commit": "0123456789abcdef0123456789abcdef01234567",
      "read_tree": "89abcdef0123456789abcdef0123456789abcdef",
      "base_commit": null,
      "bootstrap_plan_hash": null,
      "branch": null,
      "write_scope_hash": "sha256:...",
      "write_scope": ["later/task/path.py"],
      "depends_on": ["TASK-1-B"],
      "unmet_dependencies": ["TASK-1-B"],
      "required_evidence": ["implementation-map"],
      "execution_contracts": ["contracts/task-e"],
      "direct_contract_hashes": [],
      "task_node_ids": [],
      "contract_node_ids": [],
      "acceptance_constraints": [],
      "driver_gate_id": null,
      "target_gates": [],
      "candidate_commit": null,
      "basis_hash": "sha256:..."
    }
  ],
  "design_queue": [],
  "invalidated_evidence_task_ids": [],
  "idle_slots": [],
  "refill_plan": {
    "trigger": "slot_terminal_event",
    "dispatch_at": "next_host_dispatch_boundary",
    "priority": [
      "restore_two_safe_execution_slots",
      "declared_verification_unit",
      "candidate_review",
      "lane0_approved_design_probe",
      "dependency_prewarmer",
      "third_safe_execution"
    ],
    "polling": false
  },
  "terminal_protocol": {
    "owner": "lane0_and_work_methodology_skill",
    "compiler_schedules_declared_verification_units": true,
    "compiler_schedules_ad_hoc_terminal_slots": false,
    "verification_unit_task_ids": ["TASK-1-VERIFY"],
    "integration_regression_passes": 1,
    "blocker_reviews": 1,
    "global_targeted_remediation_rounds": 1,
    "wide_or_shared_scope_remediation": "stop_for_lane0"
  },
  "workflow_policy": {
    "owner": "work_methodology_skill",
    "boundary_operations": [
      {
        "boundary": "strict_writer_start",
        "roles": ["execution"],
        "operations": [
          "project_index_sync_input_worker_worktree",
          "workflow_create_if_absent",
          "workflow_register_task_strict_index",
          "workflow_ready",
          "host_spawn_exact_route",
          "workflow_claim_with_host_target"
        ]
      },
      {
        "boundary": "strict_writer_execution_and_completion_preparation",
        "roles": ["execution"],
        "operations": [
          "project_index_query_input",
          "worktree_checkpoint_create_before_first_write",
          "native_scoped_write_and_target_gates",
          "project_index_sync_output_worker_worktree",
          "project_index_query_output"
        ]
      },
      {
        "boundary": "strict_writer_completion",
        "roles": ["execution"],
        "operations": [
          "host_attest_and_lane0_integrate",
          "workflow_artifact_register_completion_receipt_at_output_snapshot",
          "workflow_complete_with_completion_receipt_hash"
        ]
      },
      {
        "boundary": "read_only_verification_lifecycle",
        "roles": ["verification"],
        "operations": [
          "project_index_sync_input_read_worktree",
          "workflow_create_if_absent",
          "workflow_register_task_strict_index",
          "workflow_ready",
          "host_spawn_exact_route",
          "workflow_claim_with_host_target",
          "project_index_query_input",
          "run_all_green_target_gates",
          "workflow_artifact_register_completion_receipt_at_input_snapshot",
          "workflow_complete_with_completion_receipt_hash"
        ]
      },
      {
        "boundary": "lease_recovery_without_bound_output",
        "roles": ["execution", "verification"],
        "operations": [
          "workflow_status_once_for_recovery",
          "verify_bound_input_snapshot_current_or_stop",
          "workflow_claim_new_lease_epoch",
          "reuse_predecessor_dispatch_context",
          "issue_new_dispatch_receipt_and_token",
          "reject_old_epoch_receipt_and_token",
          "reestablish_current_input_query_and_required_write_evidence"
        ]
      },
      {
        "boundary": "lease_recovery_with_valid_bound_output",
        "roles": ["execution"],
        "operations": [
          "require_host_persisted_attested_output_snapshot",
          "workflow_status_once_for_recovery",
          "workflow_claim_new_lease_epoch",
          "reuse_predecessor_dispatch_context",
          "issue_new_dispatch_receipt_and_token",
          "reject_old_epoch_receipt_and_token",
          "verify_workspace_matches_bound_output_snapshot",
          "reregister_new_lease_output_query_and_verification_evidence",
          "continue_host_attested_completion_without_new_input_checkpoint"
        ]
      }
    ],
    "conditional_operations": [
      {
        "condition": "claim_host_target_unavailable_or_rebind_required",
        "operation": "workflow_endpoint_bind"
      }
    ],
    "operation_set_is_closed_capability_list": false,
    "mid_item_status_polling": false,
    "recovery_status_reads": "start_or_recovery_boundary_only",
    "release_tool_available": false
  },
  "plan_hash": "sha256:..."
}
```

`terminal_protocol.verification_unit_task_ids` is the deterministic manifest of
declared source-DAG verification units, not their whole dispatch payload.
During `integration_regression`, each ready ID appears as a normal
`verification` assignment/queue envelope carrying its explicit route, complete
gate bundle, workspace snapshot, read-context hash, dispatch context, receipt,
epoch, and token. Ad-hoc regression shards never appear in this list.

Assignments contain the exact fields shown above. `action = "start"` is the
only instruction that may spawn a new subagent; `action = "retain"` represents
an already-running assignment and must never be spawned again. Queue items
contain the same role/context fields but omit slot, action, epoch, and token.
Every item carries one normalized unit envelope: artifact units receive
`unit_kind = "artifact"`, `operation_count = 0`, and
`acceptance_constraints = []`; TaskEpisode units receive
`operation_count = 0`; packet units retain their validated operation count.
Goal, output boundary, scopes, dependencies, evidence, route recommendation,
execution/direct contracts, acceptance constraints, and node IDs all come from
the decomposition result.

Role-specific nullability for every assignment and queue envelope is closed:

| Role | `access` | Execution context/base/bootstrap/branch | Read context/base/tree | Workspace input snapshot | Candidate | Basis | Driver / gates |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `execution` with artifact/code unit | `exclusive_write` | required | null | required for strict-index, otherwise null | null until review role | null, except revalidated prewarm hashes live in dispatch context | non-null driver; non-empty complete gates |
| `execution` with `unit_kind=remediation` | `exclusive_write` | required | null | required | null until review role | null | non-null driver; non-empty complete gates |
| `verification` | `read_only` | null | required | required | null | null | null driver; non-empty complete gates; every RED list empty |
| `prewarm` | `read_only` | null | required | required | null | required observation basis | null driver; empty gates |
| `review` | `read_only` | null | required | required | required candidate commit | null | null driver; empty gates |
| `design_probe` | `read_only` | null | required | required | null | required observation basis | null driver; empty gates |

“Execution context” means `execution_context_hash`, `base_commit`,
`bootstrap_plan_hash`, `branch`, and `write_scope_hash`; “read context” means
`read_context_hash`, `read_base_commit`, and `read_tree`.
`workspace_input_snapshot_id` is the snapshot of that role's own worktree.
Verification has `write_scope = []` and the canonical hash of an empty scope;
other read-only roles may copy a declared scope for observation but never own
it. Fields not marked required are exactly `null` or `[]`, never omitted.

`review_queue` uses `role = "review"`, `access = "read_only"`, a non-null
`candidate_commit`, `driver_gate_id = null`, and `target_gates = []`.
`design_queue` uses `role = "design_probe"`, the explicit Sol Ultra route,
read-only access, `driver_gate_id = null`, and `target_gates = []`. All
semantic unit fields are copied from the validated
decomposition result. Target gates and bootstrap hashes come only from their
separately validated request records. The compiler does not accept a second
free-form unit copy.

`invalidated_evidence_task_ids` identifies design evidence whose basis changed
and prewarm evidence whose current delta revalidation is missing or stale.
`idle_slots` contains exact
`{slot_id, reason_code}` records, using only
`NO_SAFE_INDEPENDENT_WORK`, `WAITING_FOR_DEPENDENCY`,
`WRITE_SCOPE_CONFLICT`, `LANE0_SCOPE_CONFLICT`, or
`EXECUTION_CONTEXT_MISSING`, `LANE0_REQUIRED`, or
`TERMINAL_PHASE_OWNED_BY_LANE0`, `OPT_IN_REQUIRED`,
`WORK_PACKAGE_NEEDS_DESIGN`, or `AUTOMATION_STOPPED`.

Assignment slot IDs are unique; idle slot IDs are unique; both sets are
disjoint; and their union is exactly `slot-1`, `slot-2`, and `slot-3`. Queues
never reserve a slot.

Every dispatch carries the exact `model` and `reasoning_effort`; the host must
report a tool rejection instead of silently substituting either value. The
output does not include prompts, command output, absolute workspaces, secrets,
raw external receipt bodies, or arbitrary metadata. It may contain only the
redacted exact fast-lane receipts defined here.

`recommended_route` is compatibility metadata, not a second executable route.
For ordinary execution the compiler maps an allowed legacy Terra label to the
explicit model/effort fields; those explicit fields are the only dispatch
authority. `Sol High` maps to `LANE0_REQUIRED`. A lane-0-approved design probe
always uses explicit Sol Ultra regardless of the unit's inert legacy label.

`plan_hash` is the SHA-256 canonical hash of the result before the
`plan_hash` field is added. List order is deterministic:

1. dependency topology;
2. the existing wave index as a stable priority hint, never as a runtime
   completion barrier;
3. task ID as the stable tie-break;
4. fixed slot order.

Status and decision codes are closed:

| Status | Decision code | Assignments and queues | Idle slots |
| --- | --- | --- | --- |
| `inactive` | `EXPLICIT_OPT_IN_REQUIRED` | all empty | all three: `OPT_IN_REQUIRED` |
| `needs_design` | `WORK_PACKAGE_NEEDS_DESIGN` | all empty | all three: `WORK_PACKAGE_NEEDS_DESIGN` |
| `active` | `FAST_LANE_ACTIVE` | zero or more retained/start items and queues | only genuinely unused slots |
| `active` | `TERMINAL_PROTOCOL_OWNED_BY_LANE0` | DAG assignments and queues empty | all three: `TERMINAL_PHASE_OWNED_BY_LANE0` |
| `blocked` | `NO_SAFE_WORK` | no assignment and no eligible queue while incomplete tasks remain | all three with stable reasons |
| `stopped` | `AUTOMATION_STOPPED` | all empty | all three: `AUTOMATION_STOPPED` |

Every ID present in `blocked_task_ids` implicitly has the bounded generic code
`TASK_BLOCKED_BY_HOST`; there is no separate free-form blocker field.
Arbitrary blocker prose is never accepted or emitted.

`source_plan_hash` is the canonical hash of the unchanged
`team-efficiency/decomposition-plan-v1` result. The fast-lane output wraps that
result by hash and validated unit records; it never adds fields to the legacy
result.

Fast-lane request input remains bounded by `MAX_MANIFEST_INPUT_BYTES`; output
must not exceed `MAX_MANIFEST_BYTES`. Canonical serialization and hashing fail
closed on NaN, unsupported values, or overflow.

## 9. Queue and refill state machine

```text
QUEUED
  |-- read-only --> PREWARMING --> PREWARMED(valid | stale)
  |-- read-only --> DESIGN_PROBE --> DESIGN_EVIDENCE(valid | stale)
  |
  `-- dependencies integrated + scope free
       --> STARTING --> EXECUTING --> GREEN_CANDIDATE
            --> REVIEW_READY --> REVIEWED_PENDING_LANE0
            --> LANE0_ADJUDICATION --> INTEGRATED
            --> ARTIFACT_REGISTERED --> WORKFLOW_COMPLETED
            --> COMPLETED

Side states: BLOCKED | LOST | OBSOLETE
```

Every slot terminal event is one of:

- execution candidate ready;
- prewarm evidence ready;
- review pass;
- blocked;
- failed;
- obsolete because lane 0 changed the plan.

Every terminal result must match the current assignment token before it enters
state. A review pass advances only to `REVIEWED_PENDING_LANE0`; it never marks
the task complete or unlocks dependents. Lane 0 may reject, request the single
global remediation, or integrate. `COMPLETED` requires both the integration
commit and successful workflow completion.

At that event, the host consumes the already ordered refill plan immediately:

1. restore up to two dependency-ready, conflict-free execution assignments;
2. use the support slot to review a new candidate;
3. otherwise run one lane-0-approved Sol Ultra design probe;
4. otherwise prewarm the nearest future unit by dependency distance;
5. otherwise use the support slot as a third safe writer when the work-package
   capacity permits;
6. otherwise place the slot in `idle_slots` with a stable reason.

For step 4, a future unit is an unfinished, unblocked, execution-capable
artifact or Code Atlas code unit with no running assignment, candidate, or
review state and an exact `(task_id, prewarm)` read context. Verification units
are not eligible. Its readiness distance is `0` when all dependencies are
complete and otherwise `1 + max(distance(dependency))` across unfinished
dependencies. The host chooses the smallest distance, then the stable Kahn
topology index, legacy wave index, and task ID. The Kahn ready set is ordered by
task ID. This critical-path definition makes multi-parent DAG selection
deterministic.

Refill is event-driven. Neither the helper nor the host should busy-poll a
subagent. A prewarmer cannot modify files, claim a writer lease, or be counted
as completion evidence. Its evidence must be reviewed or handed to the later
writer. A design probe follows the same read-only rule and occupies a real
slot.

The scheduler is writer-count aware: it first restores at most two ordinary
execution writers, then prefers useful review/prewarm/design support, and only
then uses the support slot as a third writer. It does not repeatedly apply
“ready execution first” until all three slots are writers.

The host recompiles only when semantic state changes: accepted completion,
candidate arrival, review result, blocker decision, ownership transfer,
dependency change, stale evidence, terminal phase transition, or a replacement
work package. Commentary updates alone do not trigger recompilation. The host
starts only `action = "start"` records and retains `action = "retain"` records.

The legacy `execution_waves` and its whole-wave `DONE` barrier are not the
fast-lane runtime state machine. On every recompile, eligibility is calculated
from individual accepted dependencies, active exclusive scopes, and the three
host-owned slots. A unit deferred to a later legacy wave may fill a newly free
slot as soon as it is dependency-ready and conflict-free; unrelated tasks in
the earlier wave do not have to finish first.

Prewarm evidence is deliberately split into a stable observation and a
dispatch-time revalidation. `observation_basis_hash` binds the source plan,
observed commit/tree, the prewarmer's workspace-specific snapshot and read
scope, declared dependency IDs, then-settled dependency results, and contracts.
It is immutable provenance, not a claim that the future tree is unchanged.

When the task becomes execution-ready, the host derives this exact
revalidation record:

```json
{
  "schema": "team-efficiency/fast-lane-prewarm-revalidation-v1",
  "task_id": "TASK-1-C",
  "observation_basis_hash": "sha256:...",
  "current_source_plan_hash": "sha256:...",
  "current_integration_commit": "0123456789abcdef0123456789abcdef01234567",
  "current_integration_tree": "89abcdef0123456789abcdef0123456789abcdef",
  "current_workspace_input_snapshot_id": "sha256:...",
  "direct_dependency_result_hashes": ["sha256:..."],
  "direct_contract_hashes": ["sha256:..."],
  "changed_paths_hash": "sha256:...",
  "dependency_delta_hash": "sha256:...",
  "read_scope_hash": "sha256:...",
  "outcome": "pass"
}
```

`dependency_delta_hash` is the canonical hash of exact
`{"direct_dependency_result_hashes": <normalized list>,
"changed_paths_hash": <value>}`. `revalidation_basis_hash` is the canonical
hash of all fields through `read_scope_hash`;
`revalidation_evidence_hash` is the canonical hash of the whole record. In an
execution dispatch,
`dispatch_context.prewarm_revalidation_evidence_hash` equals that exact
`revalidation_evidence_hash`; the accompanying `prewarm_evidence_hash` equals
the state record's evidence hash. The host attests the Git delta and returns
`pass` only when the source plan and contracts still match and changes since
observation do not invalidate facts in the prewarm read scope. Otherwise the
outcome is `stale`.
Dependency completion therefore does not automatically pretend evidence is
fresh: it forces this explicit delta check. A passing execution dispatch
includes both prewarm evidence and revalidation hashes in its immutable
dispatch context; a stale record appears in
`invalidated_evidence_task_ids` and is regenerated or omitted.

Prewarm output is never RED/GREEN, acceptance, candidate, or completion
evidence. A later writer must still execute every target gate. Design evidence
has no delta shortcut: any changed source plan, relevant contract, integration
tree, or workspace snapshot marks it stale before reuse.

## 10. Verification economy

The v1 compiler schedules original implementation units in `execution` and
declared source-DAG verification units in `integration_regression`. Lane 0 and
the work-methodology skill execute the emitted assignments and own integration,
any explicitly declared non-DAG regression shards, blocker adjudication,
remediation authorization, and final acceptance. The compiler never runs a
test, spawns a model, or calls a workflow API.

Phase progression is closed:

1. `execution` ends only when every required non-verification source unit is
   durably completed;
2. `integration_regression` schedules dependency-ready declared verification
   units as `role = "verification"` assignments in the same three physical
   slots and runs each explicitly bounded additional regression shard once;
3. only after all declared verification units are durably completed and every
   authorized shard is accepted does phase advance to `blocker_review`;
4. blocker review advances to `acceptance`, the single `remediation` round, or
   `stopped`;
5. a completed remediation advances to `acceptance`; no second automatic
   regression or remediation is implied.

### 10.1 Worker gate

Every implementation worker must produce:

1. the designated driver gate failing with one allowed RED exit code before
   the change;
2. the smallest implementation inside its exclusive write scope;
3. every exact configured gate, including the driver, returning its configured
   GREEN exit code;
4. only the directly relevant lint, format, type, or compile checks.

The complete ordered RED/GREEN evidence lists, candidate commit/tree,
bootstrap plan hash, base commit, dispatch context, and assignment token are
bound by the terminal result. Workers do not independently run the whole
repository suite. A worker GREEN result is a candidate, not task completion.

### 10.2 Integration gate

Lane 0 integrates accepted writer candidates from isolated worktrees into the
integration tree and creates the exact integration proof and completion
receipt. Only artifact-registered, workflow-completed records satisfy
dependencies.

A declared verification unit has no candidate and no integration step. Its
assignment is read-only, has empty write scope, exactly one read context, no
execution/bootstrap context, a Terra route, `driver_gate_id = null`, and the
complete verified gate list. Its durable lifecycle is:

1. synchronize its validated read worktree and bind that workspace-specific
   input snapshot;
2. JIT-register, ready, spawn, and claim the strict read-only task;
3. record the input Project Index query;
4. run every gate GREEN exactly once with all caches and temporary writes
   redirected to its D-drive temp target;
5. return a token-valid `verified` terminal result;
6. lane 0 accepts the evidence and creates a
   `completion_kind = "verification_evidence"` receipt bound to the same
   integration commit/tree and input snapshot;
7. register that receipt hash as the verification artifact at the input
   snapshot and call `workflow_complete` with the same result hash.

No checkpoint, output snapshot/diff, candidate commit, or integration proof is
required for this read-only completion. Packet and TaskEpisode verification
units follow the same path and must cover all of their acceptance constraints.

The rest of the single integration regression may be split into
non-overlapping, explicitly bounded argv shards across subagents, but commands
cannot be inferred from prose. Every shard is bound to the same exact
integration commit/tree, every test lane runs once, and every shard uses a
fresh task-specific D-drive temporary root. These ad-hoc shards are
lane-0/skill-owned terminal work; the compiler schedules only declared
verification units.

### 10.3 Blocker gate

One independent blocker review follows the single regression pass. It reviews
the integrated diff and bound evidence. This is distinct from optional
per-candidate reviews. The blocker reviewer is a Terra verification worker;
lane 0 owns the decision.

If it finds a Critical or Important issue, lane 0 may spend the one global
remediation round by supplying the exact Section 7.5 request. The compiler
turns it into one new JIT durable strict-index task with its own independent
execution context, lease, dispatch/terminal receipts, target gates,
checkpoint, output snapshot/query, completion receipt, artifact, and
`workflow_complete`. Its completed dependencies remain `DONE`; their state,
version, result hashes, and artifacts are never reopened or rewritten.

A second remediation request stops automatic dispatch. If the fix touches a
shared scope, crosses multiple original unit boundaries, or invalidates more
than one regression shard, automatic remediation also stops for lane-0
redesign or user direction.

Any remediation changes the integration commit, so the earlier regression
fingerprint is explicitly stale for that new commit. Fast lane does not claim
otherwise and does not silently start a second full regression. The targeted
recheck is bound to the remediated commit; lane 0 records the narrower evidence
boundary in the remediation completion receipt and final acceptance. The new
remediation task's result/artifact hash therefore represents the final tree;
the old source-task results continue to represent their immutable original
outputs. A new whole-suite pass requires a new, explicitly authorized run
outside the automatic one-round protocol.

No severity downgrade is allowed merely to avoid this stop condition.

## 11. Workflow API economy

The fast-lane compiler does not call workflow APIs and does not add a workflow
MCP method. Its `workflow_policy` is enforced by the work-methodology skill,
using the ordered `boundary_operations` emitted in Section 8. This is a
non-closed orchestration/state sequence, not a capability whitelist for native
shell, read, patch, test, or Git operations.

For a strict writer, the skill first verifies that the validated independent
worker worktree is still at the current integration commit/tree, then calls
`project_index_sync` on that worker worktree. If the root workflow does not
yet exist it is created once. The skill JIT-registers the task with
`strict_index = true`, that same worker `workspace_root`, its workspace-
specific input snapshot, exact dependencies, scopes, node IDs, contracts, and
evidence; calls `workflow_ready`; spawns the exact routed subagent; and calls
`workflow_claim(host_target=<exact spawned target>)`.

Input `project_index_query`, the pre-write `worktree_checkpoint_create`, native
scoped writes, target gates, worker-worktree
`project_index_sync(bind_as = "output")`, and output query are ordered
execution/completion-preparation gates. They are not orchestration-start
polling. After the terminal receipt and lane-0 integration are independently
attested, the skill registers the completion-receipt hash as a verification
artifact at the worker output snapshot and calls `workflow_complete` with the
same hash, current lease epoch, and version compare-and-swap.

A declared read-only verification unit uses its validated read worktree
instead: input sync, JIT register/ready, exact spawn, claim, input query, all
GREEN gates, lane-0 evidence acceptance, artifact registration at that input
snapshot, then `workflow_complete`. It has no checkpoint, output diff, or
integration step. The global remediation unit follows the strict-writer
sequence inside the existing workflow; it does not create a new workflow.

Fast lane does not execute the legacy lifecycle plan's register-all timing.
It may reuse its validated task-card fields, but strict-index tasks are
registered just in time after dependencies are integrated so their workspace
and input snapshot are current. The legacy lifecycle descriptors remain
unchanged for legacy consumers.

There are no workflow status calls for commentary, prewarm progress, local
test progress, or slot polling. A dependency transition that starts a new work
item is a new start boundary, not a mid-item poll. A process restart may perform
one status read as a recovery boundary.

After an expired running lease, recovery performs that one status read and a
new `workflow_claim`. The returned `lease_epoch` reuses the exact predecessor
dispatch context but produces a new receipt, assignment epoch, and token linked
through `recovery_of_assignment_token`. This remains valid when unrelated
parallel integration has advanced the global integration tree; scope conflicts
are rechecked now and the recovered candidate is revalidated against the new
tree at lane-0 integration. The old receipt/token/lease can never become a
candidate, artifact, or completion. Existing lease fencing must reject the old
owner at output sync, artifact registration, and completion.

Recovery is phase-aware. If no valid output snapshot is bound, automatic
reclaim is allowed only while the worker worktree is still current against its
bound input snapshot. The host verifies that fact before claim; the recovered
owner may then re-establish the input query and, for a writer, new-lease
checkpoint/output evidence. If the input is stale because unbound writes
already occurred, current MCP fencing cannot safely bridge the state, so fast
lane stops for lane-0 manual recovery.

If a valid output snapshot is already bound and the worktree still matches it,
recovery must not roll back to the old input or create a new checkpoint against
that old input; it keeps the output state, re-registers only the new-lease
output query and verification evidence required by the current contract, then
continues host-attested completion. A mismatch rejects the output-bound
shortcut and fails closed. There is no public renew or release shortcut.

The output-bound branch is available only when the host already holds a
persisted, host-attested `output_workspace_snapshot_id` from the prior lease,
such as an accepted terminal-result/candidate record. `workflow_status` does
not expose strict index bindings and cannot be used to infer that snapshot. If
the host lacks this validated identity, recovery uses the no-output branch;
when its bound input is stale, it stops for manual recovery even if an
unobservable database binding might exist.

`workflow_ready` may promote every dependency-satisfied durable task and does
not enforce local capacity or write-scope conflicts. The host therefore treats
it only as a start-boundary state transition. The fast-lane scheduler remains
the authority for the three-slot capacity, the two-writer default, and
ancestor/descendant scope exclusion. An empty later `workflow_ready` response
does not invalidate a locally eligible task that was already promoted.

Prewarm, candidate review, and design-probe work is local and read-only. They
do not register or claim durable tasks. Verification is also read-only, but it
is deliberately the exception: it is a real source-DAG task and follows its
JIT claim/completion path. A worker failure is recorded as host-local
blocked state; there is no public per-task `workflow_release` or fail/block
tool. The lease may expire for recovery. Cancelling the whole workflow
requires separate explicit authority and is never an automatic fast-lane
reaction.

`workflow_claim(host_target=...)` performs the normal endpoint binding.
`workflow_endpoint_bind` is used only if the target was unavailable at claim
or a valid current lease needs rebinding; it is not a default extra call.

### 11.1 Host-attested completion boundary

Worker-provided commit, tree, snapshot, gate, and receipt hashes are untrusted
candidate declarations. Before `workflow_artifact_register` and
`workflow_complete`, the host independently verifies the current integration
commit/tree, candidate scoped-diff integration proof, dispatch/terminal hash
chain, strict-index input/output query and checkpoint evidence where
applicable, and equality between artifact hash, completion-receipt hash, and
the requested workflow result hash. Verification completion instead checks
the read-context commit/tree/input snapshot and every GREEN gate.

Host attestation is a local observation, not authorization copied from a
worker, prompt, task card, or request JSON. It does not expand MCP permissions
or replace lease-epoch, strict-index, artifact-ownership, or compare-and-swap
checks. A mismatch keeps the durable task non-`DONE` and invalidates that
terminal result.

The output names ordered boundary operations, not executable MCP descriptors.
The skill resolves the current lease, version, workspace snapshot, artifact
hash, and host target at the boundary. Redacted fast-lane dispatch, terminal,
integration-proof, and completion receipt envelopes defined by this
specification may appear in state/output; raw external receipt bodies, command
output, prompts, secrets, and exception text may not.

## 12. Safety invariants

The following invariants are mandatory:

1. one writer per file or ancestor/descendant scope;
2. lane 0's owned scopes participate in the same conflict graph, and lane 0
   respects worker exclusions until explicit ownership transfer;
3. every writer uses a validated independent worktree, immutable base commit,
   exclusive branch, and task-specific temporary root;
4. verification, prewarm, review, and design-probe lanes are read-only;
5. a writer dependency is complete only after lane-0 integration, evidence
   registration, and durable workflow completion; a verification dependency
   is complete only after all gates, evidence registration, and durable
   workflow completion;
6. scheduler state is bound to the exact source plan and current integration
   context; reused task IDs cannot import old state;
7. every implementation dispatch has one complete bounded target-gates record
   and a validated bootstrap plan; every verification dispatch has complete
   gates and a validated read context;
8. bootstrap absolute paths stay in the validated host-side context and are
   never emitted; no absolute workspace or temporary path comes from the work
   package;
9. all temporary artifacts remain below the task-specific
   `D:\bun\tmp\codex\<project-or-thread>` root;
10. no secrets, raw external receipts, command output, or exception text are
    serialized; only the exact redacted fast-lane receipts are permitted;
11. no task is synthesized from prose; units come from the existing verified
    work-package compiler except the one exact, lane-0-authorized,
    deterministic remediation record;
12. `needs_design` cannot become an implementation or automatic design-probe
    assignment;
13. one global targeted remediation round is the entire automatic budget;
14. model names and reasoning efforts are explicit and never silently
    substituted;
15. every terminal worker result matches a current dispatch context, receipt,
    epoch, role, route, and assignment token;
16. fast-lane states never enter the legacy status or resume-packet schemas;
17. empty capacity is honest: no filler, duplicate review, or unsafe overlap;
18. workspace-specific snapshots are never substituted across integration,
    execution, and read worktrees;
19. host attestation and existing lease/strict-index checks remain completion
    authority;
20. lease recovery always creates a new fenced receipt/token and never
    destroys an already valid output-bound state.

## 13. Failure behavior

- Invalid request: exit code 2 with a stable, bounded stderr message and no
  partial JSON.
- Existing work package returns `needs_design`: valid `needs_design` plan, no
  implementation or automatic design probe.
- Lower effort without opt-in: valid inactive plan.
- All remaining tasks blocked or context-ineligible: `blocked` plan with empty
  assignments and stable slot reasons.
- Running state contradicts dependencies or write scopes: reject the snapshot;
  do not silently repair it.
- A stale source-plan hash rejects the snapshot. Design evidence on a changed
  basis is invalidated. Prewarm evidence reaches a writer only after an exact
  current-basis delta revalidation passes.
- A stale assignment token is ignored and cannot create a candidate,
  completion, or dependency transition.
- A slot fails or disappears: the host records local blocked/lost state,
  allows its lease to expire, and recompiles without waiting for a whole wave;
  recovery uses the phase-aware no-output or output-bound branch.
- A requested model or reasoning effort rejected by the host is reported;
  there is no fallback substitution.
- An exceptional legacy `Sol High` route becomes `LANE0_REQUIRED`, not an
  automatically spawned subagent.
- A terminal-phase Critical/Important issue after the one global remediation,
  or any wide/shared-scope remediation, changes the phase to `stopped`.

## 14. Compatibility

Implementation must prove:

- all existing subcommand parameters, behavior, errors, and outputs remain
  unchanged; top-level help may only gain the new `fast-lane` command;
- existing request validators and exact output shapes are unchanged;
- `decompose()` remains the sole semantic work-package compiler;
- `plan-waves` remains an alias of `decompose`;
- current routing strings remain unchanged in decomposition output;
- legacy `status`, `resume-packet`, and lifecycle descriptor schemas receive no
  fast-lane fields or states;
- new schemas use separate constants and validators;
- parser dispatch handles `fast-lane` explicitly rather than routing it
  through the existing fallback to `decompose`;
- no new package, network dependency, model interface, or MCP server method is
  introduced.

## 15. Test plan

Focused tests in `skills/work-methodology/tests/test_team_efficiency.py` must
cover:

### Activation

- Ultra activates without `--enable`;
- every lower effort remains inactive without `--enable`;
- lower effort activates with `--enable`;
- missing, unknown, duplicated, or contradictory effort input fails closed.

### Scheduling

- lane 0 is present and not counted in capacity three;
- initial useful work fills three subagent slots when safe work exists;
- normal selection is two writers plus one prewarm/review;
- a third disjoint ready writer can use the support slot;
- a terminal event refills one slot without waiting for other running slots;
- retained assignments are marked `retain` and only the free slot is `start`;
- assignment epochs and tokens reject late or duplicate results;
- dependency-nearest prewarm selection uses the specified readiness
  critical-path distance and deterministic tie breakers;
- every prewarm dispatch is Terra Medium, read-only, and carries no gate,
  lease, claim, or write ownership;
- a prewarm observation survives dependency completion only through a passing
  current-basis delta revalidation; stale delta evidence is never dispatched;
- no-safe-work emits exact `idle_slots` records and stable reason codes;
- assignments and idle slots are disjoint, unique, and exactly partition all
  three physical slots;
- lane-0 and worker scopes participate in the same conflict graph;
- dependency and ancestor/descendant write conflicts never run together;
- every writer has a distinct validated worktree/branch/temp root;
- a Sol Ultra design probe occupies a real slot;
- exceptional legacy `Sol High` units return `LANE0_REQUIRED`.

### State validation

- unknown or duplicate task IDs fail;
- initial-null and subsequent exact `source_plan_hash` rules are enforced;
- task IDs cannot be completed, candidate, running, and blocked
  simultaneously;
- only three slot IDs are accepted and each appears once;
- verification/prewarm/review/design is read-only;
- execution requires readiness;
- lane-0 active scopes can block worker dispatch;
- completed records require embedded terminal/completion receipts, matching
  hashes, integration commit/tree, and result hash;
- global remediation round above one fails;
- inactive, needs-design, blocked, terminal, and stopped phases emit their
  exact status/decision/idle-reason combinations;
- `needs_design` emits no writer.

### Gates, contexts, receipts, and candidates

- every executable or verification unit has one exact target-gates record;
- multi-constraint packet and TaskEpisode units are accepted only when the
  gate-coverage union equals every verified constraint and all gates GREEN;
- implementation requires the designated driver RED; verification has no RED
  driver; each gate's RED and GREEN use the same bounded argv;
- unrelated non-zero failures are rejected unless the configured RED failure
  fingerprint matches, and every gate enforces its bounded timeout;
- shell strings, absolute/traversal argv, secrets, oversized vectors, and
  unknown fields fail closed;
- bootstrap plans are exact, task/scope/base bound, independent, and hashed;
- duplicate contexts, reused branches/worktrees/temp roots, integration-tree
  reuse, missing writer contexts, and new-start base mismatches fail closed;
- Packet and TaskEpisode verification units are scheduled in
  `integration_regression` with exact read contexts and complete target gates,
  never as implementation writers;
- worker GREEN creates only a review-ready candidate;
- candidate review pass remains pending lane-0 acceptance;
- only lane-0-integrated and workflow-completed records unlock dependencies;
- `action=retain` preserves the exact immutable context/receipt/epoch/token
  even after the integration tree advances;
- retained review context preserves candidate/tree and RED/GREEN evidence;
- forged, old, duplicate, or cross-role receipt/token transitions fail closed;
- prewarm revalidation binds observation, dependency delta, changed paths,
  current tree/snapshot, and dispatch-context evidence hashes exactly;
- legacy route labels remain byte-compatible but cannot override explicit
  dispatch model/effort; `Sol High` is never auto-dispatched.

### Verification and workflow policy

- output always encodes one integration pass and one blocker review;
- output caps the whole run to one global targeted remediation round;
- compiler schedules declared verification units in the three slots but never
  invents or schedules ad-hoc regression shards;
- Packet and TaskEpisode verification each complete end to end without a
  candidate, checkpoint, output diff, or integration step;
- wide/shared remediation stops automatic cycling;
- ordered boundary operations cover writer start, input query/checkpoint,
  output sync/query, host-attested completion, read-only verification, and
  recovery without becoming a closed capability whitelist;
- strict-index writer registration is just in time against its independent
  worker worktree; verification uses its independent read worktree;
- prewarm/review/design produces no workflow claim, while verification does;
- a round-one remediation creates one new JIT strict-index task, leaves every
  original `DONE` record unchanged, and binds its completion artifact/result
  to the remediated final commit/tree;
- a second or broad remediation creates no durable task and stops;
- no-output recovery proceeds only while the worktree still matches its bound
  input snapshot, then recreates current lease-bound evidence; stale input with
  unbound writes stops for manual recovery; valid output-bound recovery
  preserves output state and does not create a new checkpoint against the old
  input;
- output-bound recovery requires a host-persisted, attested output snapshot;
  `workflow_status` alone never selects that branch, and a missing host record
  falls back to no-output/manual-stop semantics;
- when unrelated integration has advanced, valid input/output-bound recovery
  reuses the predecessor's older immutable context, issues a linked new
  epoch/receipt/token, and defers current-tree scoped-diff proof to lane 0;
- old lease epochs/tokens cannot sync output, register artifacts, or complete;
- no release descriptor or nonexistent per-task failure tool is emitted.

### Security and determinism

- absolute paths in work packages, argv, or unvalidated contexts; traversal;
  secrets; oversized input; NaN; unknown fields; and malformed nested work
  packages fail closed. Validated host-side execution/read paths remain input-
  only and hash-bound;
- canonical output and `plan_hash` are stable across dictionary insertion
  order;
- no absolute workspace, prompt, raw external receipt body, or command output
  appears in output;
- existing decompose golden bytes, `plan-waves` alias behavior, legacy
  status/resume exact schemas, and the full existing test suite remain
  unchanged.

### Skill integration

- `SKILL.md` requires fast-lane compilation for substantial Ultra tasks;
- it keeps non-Ultra behavior opt-in;
- it states that main Sol owns design/integration/final acceptance;
- it authorizes only Sol Ultra for bounded parallel design;
- it tells the host to refill on terminal events and not on commentary;
- it requires isolated bootstrap worktrees and token validation;
- it executes compiler-declared verification assignments plus the one-pass
  regression, one blocker review, and one global remediation protocol.

## 16. Implementation surface

After this specification is approved, implementation is limited to:

- `skills/work-methodology/scripts/team_efficiency.py`;
- `skills/work-methodology/tests/test_team_efficiency.py`;
- `skills/work-methodology/references/efficiency-automation.md`;
- `skills/work-methodology/SKILL.md`.

No MCP server, plugin manifest, or existing schema migration is required.

## 17. Acceptance criteria

The feature is accepted when:

1. all focused fast-lane tests pass;
2. all existing team-efficiency tests pass unchanged;
3. exact legacy command outputs remain compatible;
4. an Ultra fixture deterministically emits lane 0 plus three useful
   subagent assignments;
5. a single-slot terminal event retains live workers and dispatches only the
   next safe item with a new epoch/token;
6. candidate review cannot unlock a dependency before lane-0 integration,
   artifact registration, and workflow completion;
7. plan changes, stale evidence, lane-0 conflicts, shared worktrees, blocked
   dependencies, a second global remediation, and `needs_design` all fail
   closed;
8. every dispatched model and reasoning effort is explicit, with Sol Ultra
   reserved for bounded design probes and Terra Medium reserved for prewarm;
9. documentation clearly states the host-declared effort limitation, compiler
   ownership of declared verification scheduling, and lane-0/skill ownership
   of terminal execution and acceptance;
10. the terminal protocol performs one integration regression pass, one
    independent blocker review, and at most one global targeted remediation;
11. the integrated diff contains no MCP surface expansion or hidden external
    side effect.
