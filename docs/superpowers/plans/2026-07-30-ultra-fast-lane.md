# Ultra Fast Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic `fast-lane` compiler that turns a bounded work package plus host-side state into lane 0 and three safe, immediately refillable subagent dispatch descriptors, with Ultra automatic activation and Terra Medium prewarm.

**Architecture:** Keep `decompose()` as the only semantic work-package compiler and wrap its unchanged result with a new, exact `team-efficiency/fast-lane-plan-v1` orchestration layer. The helper validates and hashes inert gates, contexts, receipts, and scheduler state but never starts agents, runs gates, mutates Git, calls a model, or performs workflow operations. Main Sol remains lane 0 and owns design, integration, risk decisions, and acceptance; one Terra Max worker serially owns the shared Python source/test surface, a Terra High worker owns the disjoint documentation surface, and one Terra Medium support assignment may perform a selected useful read-only prewarm observation. Prewarm never runs a target gate or regression check, claims a workflow task, or creates completion evidence; declared verification owns GREEN gates in `integration_regression`.

**Tech Stack:** Python 3.12, `unittest`, deterministic canonical JSON, existing `team_efficiency.py` validators and bootstrap planner, PowerShell, Git

---

## Locked contract and execution topology

The approved design is
`docs/superpowers/specs/2026-07-30-ultra-fast-lane-design.md`. It is normative
for every exact field, phase transition, receipt, token, gate, context, queue,
reason code, and security bound. This plan records implementation order and
verification commands; it does not weaken the design.

The implementation must preserve these decisions:

- The public API is exactly
  `compile_fast_lane(request, *, reasoning_effort, enable=False)`.
- The CLI is exactly
  `fast-lane --input <json> --reasoning-effort <effort> [--enable]`.
- The request schema remains
  `team-efficiency/fast-lane-request-v1`; the result schema remains
  `team-efficiency/fast-lane-plan-v1`.
- `ultra` activates automatically with `activation.reason="ultra_auto"`.
  Lower efforts require `--enable` and record
  `activation.reason="explicit_opt_in"`; without it the compiler returns the
  exact inactive plan.
- Lane 0 is main `gpt-5.6-sol` and does not consume any of
  `slot-1`, `slot-2`, or `slot-3`.
- `prewarm` is always `gpt-5.6-terra` with
  `reasoning_effort="medium"`, read-only, gate-free, lease-free, and
  claim-free.
- Routine implementation and verification use Terra High; moderate or harder
  implementation and verification use Terra Max; review is exactly Terra High;
  only a bounded design probe may use Sol Ultra.
- Legacy `recommended_route` remains compatibility metadata. `_ROUTES`,
  `decompose()`, `plan_waves()`, resume packets, status rendering, bootstrap,
  contract checks, and cache metadata must retain their old bytes and schemas.
- A request's canonical `repo` values are one integration-worktree anchor.
  The compiler proves only internal anchor consistency and path inequality.
  The host must match that anchor to its trusted integration worktree before
  applying a bootstrap plan or executing a read descriptor.
- Target gate `argv` values are inert data. The compiler never executes them.
- Output never contains absolute repo/worktree/temp paths, prompts, raw command
  output, arbitrary exception text, secret material, or raw external receipts.
- A candidate or review pass never unlocks a dependency. Completion requires
  lane-0 integration, artifact registration, and durable workflow completion.
- Terminal handling permits one integration regression, one blocker review,
  and at most one narrow global remediation.

Every task selects its lane in the current PowerShell session before running a
relative command. Repeat this block in a new shell:

```powershell
$ErrorActionPreference='Stop'
$Lane='integration'
if ($Lane -notin @('integration', 'code', 'docs', 'prewarm')) {
    throw 'invalid fast-lane lane'
}
$worktreeName = @{
    integration = 'ultra-fast-lane'
    code = 'ultra-fast-lane-code'
    docs = 'ultra-fast-lane-docs'
    prewarm = 'ultra-fast-lane-read'
}[$Lane]
$env:CODEX_TASK_TEMP="D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\$Lane"
$env:TEMP=$env:CODEX_TASK_TEMP
$env:TMP=$env:CODEX_TASK_TEMP
$env:TMPDIR=$env:CODEX_TASK_TEMP
$env:PYTHONPYCACHEPREFIX="$env:CODEX_TASK_TEMP\pycache"
New-Item -ItemType Directory -Force -Path $env:CODEX_TASK_TEMP | Out-Null
$expectedRoot="D:\bun\tmp\codex\2718-devkit\worktrees\$worktreeName"
Set-Location "$expectedRoot\skills\work-methodology"
$actualRoot=(& git rev-parse --show-toplevel)
if (
    $LASTEXITCODE -ne 0 -or
    $actualRoot.Trim().Replace('/', '\') -ne $expectedRoot
) {
    throw "lane worktree verification failed: expected $expectedRoot, got $actualRoot"
}
& git branch --show-current
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect selected lane branch' }
Get-Location
$env:CODEX_TASK_TEMP
```

Concurrent write ownership is fixed:

| Lane | Route | Worktree and temporary root | Write ownership |
| --- | --- | --- | --- |
| lane 0 | main Sol | `D:\bun\tmp\codex\2718-devkit\worktrees\ultra-fast-lane`; `D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\integration` | plan, dispatch, review, integration, final acceptance |
| coding worker | Terra Max | `D:\bun\tmp\codex\2718-devkit\worktrees\ultra-fast-lane-code`; `D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\code` | `scripts/team_efficiency.py`, then `tests/test_team_efficiency.py`, serially |
| docs worker | Terra High | `D:\bun\tmp\codex\2718-devkit\worktrees\ultra-fast-lane-docs`; `D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\docs` | `SKILL.md`, `references/efficiency-automation.md` |
| prewarm worker | Terra Medium | `D:\bun\tmp\codex\2718-devkit\worktrees\ultra-fast-lane-read`; `D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\prewarm` | read-only baselines, fixtures, deterministic goldens, and prewarm observations; no target gates |

No two workers edit the Python source or test file concurrently. The coding
and docs workers branch from the same accepted plan commit. The docs worker may
start as soon as Task 1 records its RED documentation assertions; it proceeds
in parallel with coding Tasks 2–5.

## Lane bootstrap

Lane 0 performs this once after the plan commit and before dispatch. All four
targets must be absent, and the source worktree must be clean. The plan/spec
commit is the immutable execution base; do not run this block while either
document is uncommitted:

```powershell
$ErrorActionPreference='Stop'
function Invoke-GitChecked {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git failed ($LASTEXITCODE): $($GitArgs -join ' ')"
    }
}
$source='D:\bun\tmp\codex\2718-devkit\worktrees\code-atlas-v1'
$worktreeRoot='D:\bun\tmp\codex\2718-devkit\worktrees'
$dirty=& git -C $source status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect source status' }
if ($dirty) { throw 'source worktree must be clean' }
$targets=@(
    "$worktreeRoot\ultra-fast-lane",
    "$worktreeRoot\ultra-fast-lane-code",
    "$worktreeRoot\ultra-fast-lane-docs",
    "$worktreeRoot\ultra-fast-lane-read"
)
foreach ($target in $targets) {
    if (Test-Path -LiteralPath $target) {
        throw "worktree target already exists: $target"
    }
}
$base=(& git -C $source rev-parse feature/code-atlas-v1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'cannot resolve plan base' }
$sourceHead=(& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceHead -ne $base) {
    throw 'source HEAD does not equal the immutable execution base'
}
& git -C $source cat-file -e "${base}:docs/superpowers/plans/2026-07-30-ultra-fast-lane.md"
if ($LASTEXITCODE -ne 0) { throw 'plan is not committed in execution base' }
& git -C $source cat-file -e "${base}:docs/superpowers/specs/2026-07-30-ultra-fast-lane-design.md"
if ($LASTEXITCODE -ne 0) { throw 'approved spec is not committed in execution base' }
Invoke-GitChecked -C $source worktree add -b codex/ultra-fast-lane "$worktreeRoot\ultra-fast-lane" $base
Invoke-GitChecked -C $source worktree add -b codex/ultra-fast-lane-code "$worktreeRoot\ultra-fast-lane-code" $base
Invoke-GitChecked -C $source worktree add -b codex/ultra-fast-lane-docs "$worktreeRoot\ultra-fast-lane-docs" $base
Invoke-GitChecked -C $source worktree add --detach "$worktreeRoot\ultra-fast-lane-read" $base
Invoke-GitChecked -C $source worktree list --porcelain
```

Lane 0 or the Terra Max coding worker runs the clean baseline from the code
worktree before the first edit:

```powershell
Set-Location 'D:\bun\tmp\codex\2718-devkit\worktrees\ultra-fast-lane-code\skills\work-methodology'
$env:CODEX_TASK_TEMP='D:\bun\tmp\codex\2718-devkit\ultra-fast-lane\code'
$env:TEMP=$env:CODEX_TASK_TEMP
$env:TMP=$env:CODEX_TASK_TEMP
$env:TMPDIR=$env:CODEX_TASK_TEMP
$env:PYTHONPYCACHEPREFIX="$env:CODEX_TASK_TEMP\pycache"
New-Item -ItemType Directory -Force -Path $env:CODEX_TASK_TEMP | Out-Null
python -m unittest discover -s tests -p 'test_*.py'
```

Expected baseline: the existing 55 work-methodology tests pass. Every branch
must resolve to `$base` before its first edit.

The Terra Medium prewarm lane may inspect files, derive fixture data, calculate
deterministic hashes, and prepare read-only evidence. It must not invoke
`unittest`, pytest, a target gate, or any regression command.

After each coding task commit, lane 0 performs two ordered gates before the
coding worker advances: first line-by-line spec compliance, then code quality.
Any issue returns to the same coding worker with one focused failing regression
test; the relevant reviewer re-checks the fix. When a Terra High/Max review
slot is free, use it for the independent review while lane 0 retains the
acceptance decision. The Terra Medium prewarm lane never substitutes for the
required Terra High review.

## Task 1: Freeze legacy bytes and add activation RED tests

**Files:**

- Modify: `skills/work-methodology/tests/test_team_efficiency.py`
- Test: `skills/work-methodology/tests/test_team_efficiency.py`

- [ ] Add a canonical-byte helper beside `load_efficiency()`:

```python
def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
```

- [ ] Add `fast_lane_request()` to `TeamEfficiencyTests`. Build its
  `work_package` from a deep copy of `decomposition_manifest()`. Generate every
  nested writer plan with `helper.build_bootstrap_plan()` so the fixture cannot
  drift from `team-efficiency/bootstrap-v1`. Use these exact top-level keys:

```python
{
    "schema": "team-efficiency/fast-lane-request-v1",
    "work_package": work_package,
    "target_gates": target_gates,
    "execution_contexts": execution_contexts,
    "read_contexts": read_contexts,
    "remediation_request": None,
    "scheduler_state": scheduler_state,
}
```

- [ ] Keep the first activation fixture intentionally small:
  `execution_contexts=[]`, `read_contexts=[]`, `remediation_request=None`, and
  the exact initial scheduler state from design Section 7. Even without
  contexts, construct one complete legal target-gate record for every source
  execution/verification unit. This fixture proves that activation does not
  invent a writer context and renders `blocked / NO_SAFE_WORK`.

- [ ] Add these focused tests:

```python
def test_fast_lane_ultra_auto_activation(self) -> None:
    helper = load_efficiency()
    activation = helper._fast_lane_activation("ultra", False)
    self.assertEqual(
        {"reasoning_effort": "ultra", "reason": "ultra_auto"},
        activation,
    )

def test_fast_lane_lower_effort_requires_explicit_enable(self) -> None:
    helper = load_efficiency()
    for effort in ("low", "medium", "high", "xhigh", "max"):
        with self.subTest(effort=effort):
            self.assertEqual(
                {"reasoning_effort": effort, "reason": "explicit_opt_in"},
                helper._fast_lane_activation(effort, True),
            )

def test_fast_lane_ultra_context_ineligible_is_blocked(self) -> None:
    helper = load_efficiency()
    result = helper.compile_fast_lane(
        self.fast_lane_request(helper, include_contexts=False),
        reasoning_effort="ultra",
    )
    self.assertEqual("blocked", result["status"])
    self.assertEqual("NO_SAFE_WORK", result["decision_code"])
    self.assertEqual("ultra_auto", result["activation"]["reason"])
    self.assertEqual([], result["assignments"])

def test_fast_lane_lower_effort_without_enable_is_exactly_inactive(self) -> None:
    helper = load_efficiency()
    result = helper.compile_fast_lane(
        self.fast_lane_request(helper, include_contexts=False),
        reasoning_effort="max",
        enable=False,
    )
    self.assertEqual("inactive", result["status"])
    self.assertEqual("EXPLICIT_OPT_IN_REQUIRED", result["decision_code"])
    self.assertEqual([], result["assignments"])
    self.assertTrue(
        all(
            item["reason_code"] == "OPT_IN_REQUIRED"
            for item in result["idle_slots"]
        )
    )
```

- [ ] Add a legacy golden test that hashes the current manual fixture's
  canonical `decompose()` result and asserts the already measured baseline:

```python
payload = canonical_bytes(helper.decompose(self.decomposition_manifest()))
waves_payload = canonical_bytes(helper.plan_waves(self.decomposition_manifest()))
self.assertEqual(payload, waves_payload)
self.assertEqual(11730, len(payload))
self.assertEqual(
    "f736b99d55bc562252d1fd6a98fb0f2d12813b0b50ba518f88d4f12c298a7775",
    hashlib.sha256(payload).hexdigest(),
)
```

- [ ] Add the two documentation RED assertions specified in Task 6 now. They
  intentionally remain red on the coding branch until the disjoint docs commit
  is integrated.

- [ ] Run the RED activation tests:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_auto_activation `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_lower_effort_requires_explicit_enable `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_context_ineligible_is_blocked `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_lower_effort_without_enable_is_exactly_inactive
```

Expected: all four fail because `_fast_lane_activation` and
`compile_fast_lane` do not exist. The legacy golden must already pass before
production code changes.

- [ ] Run the legacy golden independently and verify that it passes before any
  production implementation:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_legacy_decompose_golden_bytes_are_unchanged
```

Expected: one test passes and proves both `decompose()` and `plan_waves()`
produce the captured bytes.

- [ ] Commit the RED tests:

```powershell
git add tests/test_team_efficiency.py
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "test: define fast lane activation contract"
```

## Task 2: Implement activation, exact request shell, and explicit CLI dispatch

**Files:**

- Modify: `skills/work-methodology/scripts/team_efficiency.py`
- Modify: `skills/work-methodology/tests/test_team_efficiency.py`
- Test: `skills/work-methodology/tests/test_team_efficiency.py`

- [ ] Add fast-lane-only constants without changing any legacy constant or
  field set:

```python
MAX_GATE_TIMEOUT_SECONDS = 3600
FAST_LANE_SLOT_IDS = ("slot-1", "slot-2", "slot-3")
FAST_LANE_REASONING_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
_FAST_LANE_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "work_package",
        "target_gates",
        "execution_contexts",
        "read_contexts",
        "remediation_request",
        "scheduler_state",
    }
)
```

- [ ] Implement a stable effort validator. It accepts the API's scalar effort
  and the CLI's `action="append"` list through separate callers:

```python
def _fast_lane_effort(value: object) -> str:
    effort = _text(value, "reasoning_effort", maximum=16)
    if effort not in FAST_LANE_REASONING_EFFORTS:
        raise ValueError("reasoning_effort is invalid")
    return effort


def _one_fast_lane_effort(values: object) -> str:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes, bytearray))
        or len(values) != 1
    ):
        raise ValueError("fast-lane requires exactly one reasoning effort")
    return _fast_lane_effort(values[0])
```

- [ ] Implement `_fast_lane_activation()` and the exact inactive response
  shape. Validate `type(enable) is bool`. Ultra always records `ultra_auto`;
  lower effort plus `enable=True` records `explicit_opt_in`; lower effort
  without enable uses the design's inactive status, empty queues, and three
  `OPT_IN_REQUIRED` idle slots. Calculate `plan_hash` only after all other
  top-level fields exist.

- [ ] Implement the final exact top-level renderer skeleton in this task; do
  not create a temporary output shape that later tasks must migrate. Add
  `_render_fast_lane_plan()`, `_fast_lane_needs_design_plan()`, and
  `_fast_lane_stopped_plan()`. Every status has exactly:

```python
{
    "schema",
    "status",
    "decision_code",
    "activation",
    "source_plan_hash",
    "phase",
    "main_lane",
    "subagent_capacity",
    "assignments",
    "ready_queue",
    "review_queue",
    "prewarm_queue",
    "design_queue",
    "invalidated_evidence_task_ids",
    "idle_slots",
    "refill_plan",
    "terminal_protocol",
    "workflow_policy",
    "plan_hash",
}
```

Later tasks populate validated assignment, receipt, and terminal details inside
this fixed envelope; they must not add or remove top-level fields.

- [ ] Add `_validated_fast_lane_request()` as an exact top-level shell. At this
  stage it must:

  1. reject non-mappings, wrong schema, unknown/missing fields, NaN, and input
     larger than `MAX_MANIFEST_INPUT_BYTES`;
  2. call unchanged `decompose(request["work_package"])`;
  3. compute `source_plan_hash = _sha256_json(source_plan)`;
  4. retain the other five sections for the validators added in later tasks;
  5. return `needs_design` immediately when `decompose()` does.

- [ ] Add a thin public compiler now and keep it thin as later layers arrive:

```python
def compile_fast_lane(
    request: Mapping[str, Any],
    *,
    reasoning_effort: str,
    enable: bool = False,
) -> dict[str, Any]:
    activation = _fast_lane_activation(reasoning_effort, enable)
    validated = _validated_fast_lane_request(request)
    return _render_fast_lane_plan(validated, activation)
```

For an Ultra request with no eligible context, render the exact `blocked /
NO_SAFE_WORK` plan rather than falsely reporting `FAST_LANE_ACTIVE`. Task 4
adds the first full public active-plan assertion once the scheduler can safely
emit useful work.

- [ ] Register the CLI with a non-required `action="append"` effort so missing,
  unknown, and duplicate efforts all flow through the existing stable
  `ValueError`/return-code-2 boundary:

```python
fast_lane = commands.add_parser("fast-lane")
fast_lane.add_argument("--input", required=True)
fast_lane.add_argument("--reasoning-effort", action="append", default=[])
fast_lane.add_argument("--enable", action="store_true")
```

- [ ] Add an explicit `main()` branch before the current decompose fallback:

```python
elif args.command == "fast-lane":
    request = _read_json(args.input, maximum=MAX_MANIFEST_INPUT_BYTES)
    result = compile_fast_lane(
        _mapping(request, "fast-lane request"),
        reasoning_effort=_one_fast_lane_effort(args.reasoning_effort),
        enable=args.enable,
    )
```

- [ ] Add tests for missing, unknown, duplicate, and JSON-embedded effort. Each
  CLI failure must return `2`, emit no plan JSON on stdout, and use the existing
  bounded error path. Add a test proving `fast-lane` cannot fall into
  `decompose()`'s final `else`, plus an exact top-level key assertion for the
  inactive, blocked, and needs-design envelopes.

- [ ] Run focused GREEN:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_auto_activation `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_lower_effort_requires_explicit_enable `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_context_ineligible_is_blocked `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_lower_effort_without_enable_is_exactly_inactive `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_cli_effort_errors_are_stable `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_cli_uses_explicit_dispatch_not_decompose_fallback `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_top_level_fields_are_exact
```

Expected: all pass.

- [ ] Run the legacy guard:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_bootstrap_defaults_to_a_dry_run_with_only_safe_git_argv `
  tests.test_team_efficiency.TeamEfficiencyTests.test_resume_packet_is_canonical_bounded_and_secret_safe `
  tests.test_team_efficiency.TeamEfficiencyTests.test_status_markdown_keeps_pending_initialization_out_of_active_count `
  tests.test_team_efficiency.TeamEfficiencyTests.test_decompose_emits_maximal_deterministic_capacity_bounded_waves `
  tests.test_team_efficiency.TeamEfficiencyTests.test_legacy_decompose_golden_bytes_are_unchanged
```

Expected: all pass; the measured hash and byte length remain unchanged.

- [ ] Commit:

```powershell
git add scripts/team_efficiency.py tests/test_team_efficiency.py
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "feat: add fast lane activation boundary"
```

## Task 3: Validate gates and isolated host contexts

**Files:**

- Modify: `skills/work-methodology/tests/test_team_efficiency.py`
- Modify: `skills/work-methodology/scripts/team_efficiency.py`
- Test: `skills/work-methodology/tests/test_team_efficiency.py`

- [ ] Add test factories for one exact target gate, one dry-run execution
  context, and one read context. Recompute RED identity with:

```python
red_failure_fingerprint = helper._sha256_json(
    {
        "schema": "team-efficiency/red-failure-identity-v1",
        "gate_id": gate_id,
        "failure_ids": sorted(red_failure_ids),
    }
)
```

The planned writer worktree must not exist. The read context is host-declared
data under the D-drive task root. Compiler tests must not call
`apply_bootstrap_plan`, `git worktree add`, a model, a workflow API, or a gate.

- [ ] Add RED tests covering:

  - exact target-gate fields and one driver gate per executable unit;
  - driver RED identity, exact RED exit-code set, GREEN exit code, time bounds,
    and verification's empty RED fields;
  - inert `argv` rejection for shell wrappers, absolute/traversal paths,
    sensitive markers, empty components, oversize values, and unknown fields;
  - Atlas acceptance-constraint coverage by the union of declared gate hashes;
  - `_validated_bootstrap_plan()` reuse, exact task/write-scope/base binding,
    and unique branch/worktree/temp targets;
  - one canonical repo anchor shared by execution and read contexts;
  - canonical `worktree != repo` and unique read worktree/temp targets;
  - uniqueness over the union of execution/read worktrees and temp targets,
    including rejection when an execution target equals a read target;
  - exact verification commit/tree/snapshot binding;
  - exact running-assignment scheduler fields, immutable dispatch contexts,
    dispatch receipts, epochs, and derived assignment tokens;
  - output includes normalized configured `target_gates` exactly where a role
    requires them, but redacts raw command/failure output, traceback or
    exception text, prompts, secrets, and absolute host paths.

- [ ] Run RED:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_validates_exact_target_gates_and_driver_identity `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_unsafe_gates_and_unbound_contexts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_context_anchor_uniqueness_and_path_redaction `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_atlas_gate_coverage_fails_closed `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_running_ledger_and_assignment_token_are_exact
```

Expected: failures identify missing gate/context validators.

- [ ] Implement these private layers after `plan_waves()` and before `_parser()`:

```text
_validated_fast_lane_gate
_validated_fast_lane_target_gates
_fast_lane_red_failure_fingerprint
_validated_fast_lane_execution_context
_validated_fast_lane_execution_contexts
_validated_fast_lane_read_context
_validated_fast_lane_read_contexts
_validated_fast_lane_remediation_request
_validated_fast_lane_scheduler_state
_validated_fast_lane_dispatch_context
_validated_fast_lane_dispatch_receipt
_fast_lane_assignment_token
_fast_lane_context_index
_fast_lane_validate_cross_references
```

Use `_mapping`, `_exact_keys`, `_text`, `_task_id`, `_git_id`, `_hash`,
`_relative_scope`, `_normalised_list`, `_normalised_scopes`,
`_bounded_records`, `_absolute_path`, `_strictly_below`,
`_validated_bootstrap_plan`, `_canonical_json`, and `_sha256_json`. Add
fast-lane-specific exact field sets; do not widen a legacy field set.

- [ ] Canonicalize every host path with existing `_absolute_path()`. Require
all execution/read `repo` anchors to be equal and every selected worktree to be
different from the anchor. Treat this as request consistency only. Do not emit
`isolation_verified` or any equivalent claim.

- [ ] Validate the running ledger before any retain/refill scheduling. A
  dispatch context and receipt must bind the exact source-plan hash, task, role,
  slot, epoch, explicit route, context hashes, target-gate hash, and recovery
  predecessor. Recompute the assignment token and reject old, duplicate, or
  cross-role records.

- [ ] Hash the complete validated context, including host paths, but render
only these role-appropriate fields:

```python
{
    "execution_context_hash": execution_context_hash,
    "bootstrap_plan_hash": bootstrap_plan_hash,
    "base_commit": base_commit,
    "branch": branch,
    "write_scope_hash": write_scope_hash,
    "workspace_input_snapshot_id": workspace_input_snapshot_id,
}
```

or:

```python
{
    "read_context_hash": read_context_hash,
    "read_base_commit": base_commit,
    "read_tree": tree,
    "workspace_input_snapshot_id": workspace_input_snapshot_id,
}
```

- [ ] Assert compiler purity by patching `subprocess.run`,
  `apply_bootstrap_plan`, and any workflow/model seam to raise if invoked during
  `compile_fast_lane()`. Add an AST assertion scoped to `compile_fast_lane` and
  `_fast_lane*` function bodies that rejects calls to `subprocess.run`,
  `apply_bootstrap_plan`, `Path.mkdir`, Git mutation helpers, workflow
  adapters, and network adapters. Existing bootstrap-apply code outside that
  call graph remains legal.

- [ ] Run focused GREEN:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_validates_exact_target_gates_and_driver_identity `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_unsafe_gates_and_unbound_contexts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_context_anchor_uniqueness_and_path_redaction `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_atlas_gate_coverage_fails_closed `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_running_ledger_and_assignment_token_are_exact `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_has_no_external_side_effect
```

Expected: all pass.

- [ ] Commit:

```powershell
git add scripts/team_efficiency.py tests/test_team_efficiency.py
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "feat: bind fast lane gates and contexts"
```

## Task 4: Implement three-slot scheduling, routing, and event refill

**Files:**

- Modify: `skills/work-methodology/tests/test_team_efficiency.py`
- Modify: `skills/work-methodology/scripts/team_efficiency.py`
- Test: `skills/work-methodology/tests/test_team_efficiency.py`

- [ ] Extend the fixture to contain disjoint routine and moderate writer units,
  a future prewarm unit, a verification unit, and exact matching contexts.
  Keep all write scopes disjoint except in tests that intentionally exercise
  ancestor/descendant conflict.

- [ ] Add RED tests for:

  - lane 0 plus exactly three physical subagent slots;
  - public `compile_fast_lane(..., reasoning_effort="ultra")` returning
    `active / FAST_LANE_ACTIVE` with `activation.reason="ultra_auto"` when the
    fully bound fixture has safe useful work;
  - public `compile_fast_lane(..., enable=True)` activating Medium, High, and
    Max requests with `activation.reason="explicit_opt_in"`;
  - two writers plus one useful support action under ordinary scheduling;
  - a third disjoint writer only after review/design/prewarm support is absent;
  - routine Terra High, moderate/complex Terra Max, review Terra High,
    prewarm Terra Medium, design probe Sol Ultra;
  - exceptional legacy `Sol High` producing no dispatch; its otherwise free
    slot uses `idle_slots[].reason_code="LANE0_REQUIRED"` while the top-level
    status/decision remains one of the closed output-contract combinations;
  - lane-0 scope conflicts and worker ancestor/descendant conflicts;
  - dependency readiness based on lane-0-accepted completion only;
  - a one-slot terminal event retaining other assignments byte-for-byte and
    issuing only one new epoch/token;
  - honest idle slots that exactly partition the unused physical slots;
  - deterministic prewarm critical-path distance and tie breakers.

- [ ] Run RED:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_emits_lane_zero_and_three_useful_slots `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_public_explicit_opt_in_activates_lower_efforts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_routes_are_explicit_and_legacy_route_is_unchanged `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_blocks_lane_zero_and_scope_conflicts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_refills_only_terminal_slot_and_retains_live_assignments `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_prewarm_critical_path_is_deterministic
```

Expected: failures identify missing scheduling and route logic.

- [ ] Implement the scheduler as small pure functions:

```text
_fast_lane_unit_index
_fast_lane_topology_index
_fast_lane_validate_retained_assignments
_fast_lane_completed_ids
_fast_lane_dependency_ready
_fast_lane_conflict_graph
_fast_lane_ready_items
_fast_lane_preferred_prewarms
_fast_lane_select_actions
_fast_lane_assignment
_fast_lane_idle_slots
_fast_lane_build_schedule
```

- [ ] Implement the route table as executable authority distinct from inert
  legacy labels:

```python
def _fast_lane_route(
    unit: Mapping[str, Any], role: str
) -> tuple[str, str] | None:
    if role == "design_probe":
        return ("gpt-5.6-sol", "ultra")
    if role == "prewarm":
        return ("gpt-5.6-terra", "medium")
    if role == "review":
        return ("gpt-5.6-terra", "high")
    if role == "verification":
        return _fast_lane_route_from_recommended_label(
            unit["recommended_route"]
        )
    return _fast_lane_route_from_recommended_label(unit["recommended_route"])


def _fast_lane_route_from_recommended_label(
    route: object,
) -> tuple[str, str] | None:
    if route == "Terra High":
        return ("gpt-5.6-terra", "high")
    if route == "Terra Max":
        return ("gpt-5.6-terra", "max")
    if route == "Sol High":
        return None
    raise ValueError("unit route is invalid")
```

- [ ] Implement readiness critical-path distance exactly:

```python
def _fast_lane_critical_path_distance(
    task_id: str,
    units: Mapping[str, Mapping[str, Any]],
    completed: frozenset[str],
    memo: dict[str, int],
) -> int:
    if task_id in memo:
        return memo[task_id]
    unfinished = [
        dependency
        for dependency in units[task_id]["depends_on"]
        if dependency not in completed
    ]
    distance = (
        0
        if not unfinished
        else 1
        + max(
            _fast_lane_critical_path_distance(
                dependency, units, completed, memo
            )
            for dependency in unfinished
        )
    )
    memo[task_id] = distance
    return distance
```

Eligible prewarm units must be unfinished, unblocked, execution-capable
artifact or Code Atlas `unit_kind="code"` units with no running assignment,
candidate, or review state and an exact `(task_id, "prewarm")` read context.
Verification units are not eligible. Build
`topology_index` with a Kahn traversal whose ready set is ordered by task ID,
and build `legacy_wave_index` from `source_plan["waves"]`. Sort prewarm
candidates by:

```python
(
    distance,
    topology_index[task_id],
    legacy_wave_index[task_id],
    task_id,
)
```

Cover a multi-parent DAG with equal distances and one Code Atlas code unit.

- [ ] Enforce the selection order from design Section 9:

  1. retain valid live assignments;
  2. restore at most two safe dependency-ready writers;
  3. prefer candidate review;
  4. prefer a lane-0-approved design probe;
  5. prefer one eligible prewarm;
  6. only then use support capacity for a third disjoint writer;
  7. otherwise emit an exact idle reason.

- [ ] Compute assignment epochs from `slot_epochs`; bind model, effort, task,
  role, slot, context hash, source-plan hash, and receipt hash into the
  assignment token. A retained assignment preserves its prior token, receipt,
  and context.

- [ ] Run focused GREEN:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_ultra_emits_lane_zero_and_three_useful_slots `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_public_explicit_opt_in_activates_lower_efforts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_routes_are_explicit_and_legacy_route_is_unchanged `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_blocks_lane_zero_and_scope_conflicts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_refills_only_terminal_slot_and_retains_live_assignments `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_prewarm_critical_path_is_deterministic
```

Expected: all pass and reordered request dictionaries render the same plan.

- [ ] Commit:

```powershell
git add scripts/team_efficiency.py tests/test_team_efficiency.py
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "feat: schedule and refill fast lane slots"
```

## Task 5: Validate lifecycle receipts, verification, recovery, and remediation

**Files:**

- Modify: `skills/work-methodology/tests/test_team_efficiency.py`
- Modify: `skills/work-methodology/scripts/team_efficiency.py`
- Test: `skills/work-methodology/tests/test_team_efficiency.py`

- [ ] Add RED state-transition tests for:

  - exact scheduler-state fields, normalized bounded lists, and closed phases;
  - initial `source_plan_hash=null` versus later exact source-plan binding;
  - completed/candidate/running/blocked lifecycle-set disjointness;
  - immutable dispatch contexts and exact dispatch receipts;
  - forged, old, duplicate, or cross-role terminal results and tokens;
  - candidate and review states that cannot unlock dependencies;
  - completion receipts bound to integration commit/tree, artifact, workflow
    task version, and terminal result;
  - lost-worker recovery with a new epoch/token and
    `recovery_of_assignment_token`;
  - no-output recovery only while the worktree matches its bound input,
    stopping on stale input with unbound writes;
  - output-bound recovery only with a host-persisted attested output snapshot,
    preserving output state and forbidding a checkpoint against the old input;
  - prewarm observation plus current-basis delta revalidation;
  - verification only in `integration_regression`, with complete gates, no RED,
    no writer bootstrap, and a read context;
  - Packet and TaskEpisode verification using declared source-DAG units only;
  - one narrow global remediation and fail-closed second/broad remediation;
  - terminal protocol with one regression and one blocker review;
  - exact status/decision/idle combinations for inactive, needs-design,
    active, terminal, blocked, and stopped phases;
  - exact top-level fields plus role-specific assignment/queue/idle keys and
    nullability from design Section 8;
  - exact ordered `workflow_policy.boundary_operations` and conditional
    operation.

- [ ] Run RED:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_invalid_or_overlapping_scheduler_state `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_candidate_never_unlocks_before_lane_zero_completion `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_forged_stale_duplicate_or_cross_role_tokens `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_packet_and_episode_verification_is_declared `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_allows_one_narrow_remediation_then_stops `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_terminal_protocol_is_bounded `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_recovery_branches_are_phase_aware `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_workflow_policy_is_exact_and_ordered `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_phase_and_role_shapes_are_exact
```

Expected: failures identify missing state, receipt, and terminal validators.

- [ ] Implement exact validators and renderers:

```text
_validated_fast_lane_scheduler_state
_validated_fast_lane_terminal_result
_validated_fast_lane_completion_receipt
_validated_fast_lane_candidate
_validated_fast_lane_prewarm_evidence
_validated_fast_lane_remediation_request
_fast_lane_terminal_policy
_render_fast_lane_plan
```

Extend the Task 2 renderer and Task 3 scheduler/dispatch validators; do not
introduce a second output envelope, ledger format, or token implementation.

- [ ] Recompute every caller-supplied derivable hash and reject mismatches.
  Bind terminal results to their current dispatch context, receipt, task, role,
  epoch, and token. Do not accept free-form blockers or arbitrary result
  metadata.

- [ ] Keep verification distinct from artifact execution. It is a real
  source-DAG task with workflow boundary descriptors, but it remains read-only,
  has no writer checkpoint/diff/candidate path, and uses its complete declared
  gate bundle.

- [ ] Derive remediation IDs exactly:

```python
seed = _sha256_json(
    {
        "schema": "fast-lane-remediation-id-v1",
        "source_plan_hash": source_plan_hash,
        "blocker_review_hash": blocker_review_hash,
        "round": 1,
    }
)
task_id = "FLR1-" + seed.removeprefix("sha256:")[:24]
```

Reject collisions rather than changing the seed. Preserve all original DONE
records; bind the new bounded scope, dependencies, current integration
commit/tree, driver RED identity, and final completion to the remediation
record. Round two or wide/shared scope renders `stopped / AUTOMATION_STOPPED`
and creates no durable task descriptor.

- [ ] Implement both recovery branches exactly. No-output recovery performs one
  status read, verifies the bound input is current, claims a new lease epoch,
  reuses the predecessor dispatch context, issues a linked receipt/token, and
  recreates current-lease evidence; stale input stops. Output-bound recovery
  additionally requires a host-persisted attested output snapshot, verifies the
  worktree still matches it, preserves output state, never creates a new old-
  input checkpoint, and re-registers only current-lease output query and
  verification evidence. `workflow_status` alone never proves bound output.

- [ ] Render this exact ordered workflow policy as inert descriptors only:

```python
_FAST_LANE_BOUNDARY_OPERATIONS = (
    (
        "strict_writer_start",
        ("execution",),
        (
            "project_index_sync_input_worker_worktree",
            "workflow_create_if_absent",
            "workflow_register_task_strict_index",
            "workflow_ready",
            "host_spawn_exact_route",
            "workflow_claim_with_host_target",
        ),
    ),
    (
        "strict_writer_execution_and_completion_preparation",
        ("execution",),
        (
            "project_index_query_input",
            "worktree_checkpoint_create_before_first_write",
            "native_scoped_write_and_target_gates",
            "project_index_sync_output_worker_worktree",
            "project_index_query_output",
        ),
    ),
    (
        "strict_writer_completion",
        ("execution",),
        (
            "host_attest_and_lane0_integrate",
            "workflow_artifact_register_completion_receipt_at_output_snapshot",
            "workflow_complete_with_completion_receipt_hash",
        ),
    ),
    (
        "read_only_verification_lifecycle",
        ("verification",),
        (
            "project_index_sync_input_read_worktree",
            "workflow_create_if_absent",
            "workflow_register_task_strict_index",
            "workflow_ready",
            "host_spawn_exact_route",
            "workflow_claim_with_host_target",
            "project_index_query_input",
            "run_all_green_target_gates",
            "workflow_artifact_register_completion_receipt_at_input_snapshot",
            "workflow_complete_with_completion_receipt_hash",
        ),
    ),
    (
        "lease_recovery_without_bound_output",
        ("execution", "verification"),
        (
            "workflow_status_once_for_recovery",
            "verify_bound_input_snapshot_current_or_stop",
            "workflow_claim_new_lease_epoch",
            "reuse_predecessor_dispatch_context",
            "issue_new_dispatch_receipt_and_token",
            "reject_old_epoch_receipt_and_token",
            "reestablish_current_input_query_and_required_write_evidence",
        ),
    ),
    (
        "lease_recovery_with_valid_bound_output",
        ("execution",),
        (
            "require_host_persisted_attested_output_snapshot",
            "workflow_status_once_for_recovery",
            "workflow_claim_new_lease_epoch",
            "reuse_predecessor_dispatch_context",
            "issue_new_dispatch_receipt_and_token",
            "reject_old_epoch_receipt_and_token",
            "verify_workspace_matches_bound_output_snapshot",
            "reregister_new_lease_output_query_and_verification_evidence",
            "continue_host_attested_completion_without_new_input_checkpoint",
        ),
    ),
)
```

Also emit the one conditional operation
`claim_host_target_unavailable_or_rebind_required -> workflow_endpoint_bind`,
`operation_set_is_closed_capability_list=false`,
`mid_item_status_polling=false`,
`recovery_status_reads="start_or_recovery_boundary_only"`, and
`release_tool_available=false`. Do not call an operation or invent a new MCP
method.

- [ ] Add `plan_hash` last:

```python
result_without_hash = {
    key: value
    for key, value in rendered.items()
    if key != "plan_hash"
}
rendered["plan_hash"] = _sha256_json(result_without_hash)
```

Then assert `len(_json_bytes(rendered)) <= MAX_MANIFEST_BYTES`.

- [ ] Run focused GREEN:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_invalid_or_overlapping_scheduler_state `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_candidate_never_unlocks_before_lane_zero_completion `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_forged_stale_duplicate_or_cross_role_tokens `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_packet_and_episode_verification_is_declared `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_allows_one_narrow_remediation_then_stops `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_terminal_protocol_is_bounded `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_recovery_branches_are_phase_aware `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_workflow_policy_is_exact_and_ordered `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_phase_and_role_shapes_are_exact
```

Expected: all pass.

- [ ] Run all fast-lane tests:

```powershell
python -m unittest discover -s tests -p 'test_team_efficiency.py' -k fast_lane
```

Expected: all compiler/scheduler fast-lane tests pass. The two documentation
tests intentionally omit `fast_lane` from their method names and run only after
the docs branch is integrated in Task 7.

- [ ] Commit:

```powershell
git add scripts/team_efficiency.py tests/test_team_efficiency.py
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "feat: fence fast lane lifecycle transitions"
```

## Task 6: Document host consumption and Ultra auto policy

**Files:**

- Modify: `skills/work-methodology/SKILL.md`
- Modify: `skills/work-methodology/references/efficiency-automation.md`
- Test after integration: `skills/work-methodology/tests/test_team_efficiency.py`

The docs worker owns only the two Markdown files. Task 1 already added these
RED documentation assertions on the coding branch:

  - exact command `fast-lane`;
  - exact request/plan schema strings;
  - Ultra automatic activation and lower-effort explicit opt-in;
  - main Sol lane-0 ownership;
  - Terra Medium prewarm;
  - Terra High for routine implementation/verification, Terra Max for
    moderate-or-harder implementation/verification, and Terra High review;
  - event-driven terminal refill with no commentary polling;
  - canonical repo anchor plus host pre-apply and post-apply Git attestation;
  - inert target gates and dispatch descriptors;
  - no model call, agent spawn, remote service, Git mutation, or workflow call;
  - one regression, one blocker review, and one global remediation.

- [ ] Confirm the assertions are RED against the plan-base documentation by
  running them in the coding worktree:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_skill_documents_ultra_auto_policy `
  tests.test_team_efficiency.TeamEfficiencyTests.test_efficiency_reference_documents_host_contract
```

Expected: documentation assertions fail.

- [ ] Update `SKILL.md` after the existing dispatch rules. State that a
  substantial Ultra task invokes:

```text
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra
```

State that the host consumes only `action="start"` descriptors, never respawns
`action="retain"`, refills on terminal state changes, and leaves an idle slot
honest when no safe useful work exists.

- [ ] Document the route floor exactly:

```text
prewarm: gpt-5.6-terra / medium
routine implementation and ordinary verification: gpt-5.6-terra / high
moderate-or-harder implementation and verification: gpt-5.6-terra / max
review: gpt-5.6-terra / high
bounded design probe: gpt-5.6-sol / ultra
```

- [ ] Update `references/efficiency-automation.md` before its CLI section.
  Explain the exact schemas, activation, context/gate/state inputs, plan hash,
  slot partition, receipts/tokens, prewarm revalidation, terminal protocol,
  host trust boundary, and compatibility behavior. Add:

```text
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort max --enable
```

- [ ] Make the trust boundary explicit: the compiler checks that all request
  anchors agree and worker/read paths differ from the anchor. Before executing
  any descriptor, the host must match the request anchor to its trusted shared
  integration worktree; before `git worktree add`, it must reject a mismatch
  without mutation.

- [ ] After `git worktree add`, document that the host re-resolves the created
  target, verifies it equals the planned worker worktree and shares the trusted
  anchor's Git common directory, and records that post-apply attestation as host
  evidence.

- [ ] The docs worker commits only its disjoint Markdown scope:

```powershell
git add SKILL.md references/efficiency-automation.md
git -c user.name="哀洛芙" -c user.email="273111507+Ayleovelle@users.noreply.github.com" commit -m "docs: explain Ultra fast lane operation"
```

## Task 7: Compatibility, security, and acceptance verification

**Files:**

- Verify: `skills/work-methodology/scripts/team_efficiency.py`
- Verify: `skills/work-methodology/tests/test_team_efficiency.py`
- Verify: `skills/work-methodology/SKILL.md`
- Verify: `skills/work-methodology/references/efficiency-automation.md`

- [ ] In the lane-0 integration worktree, integrate the serial coding commits
  followed by the disjoint docs commit:

```powershell
$ErrorActionPreference='Stop'
function Invoke-GitChecked {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git failed ($LASTEXITCODE): $($GitArgs -join ' ')"
    }
}
$dirty=& git status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect integration status' }
if ($dirty) { throw 'integration worktree must be clean' }
$base=(& git merge-base codex/ultra-fast-lane-code codex/ultra-fast-lane-docs).Trim()
if ($LASTEXITCODE -ne 0) { throw 'cannot resolve immutable worker base' }
$integrationHead=(& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $integrationHead -ne $base) {
    throw 'integration branch is not at the immutable worker base'
}
Invoke-GitChecked merge-base --is-ancestor $base codex/ultra-fast-lane-code
Invoke-GitChecked merge-base --is-ancestor $base codex/ultra-fast-lane-docs
$expectedCode=@(
    'skills/work-methodology/scripts/team_efficiency.py',
    'skills/work-methodology/tests/test_team_efficiency.py'
) | Sort-Object
$actualCode=@(& git diff --name-only "$base..codex/ultra-fast-lane-code") | Sort-Object
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect code branch scope' }
if (Compare-Object $expectedCode $actualCode) {
    throw "code branch scope mismatch: $($actualCode -join ', ')"
}
$expectedDocs=@(
    'skills/work-methodology/SKILL.md',
    'skills/work-methodology/references/efficiency-automation.md'
) | Sort-Object
$actualDocs=@(& git diff --name-only "$base..codex/ultra-fast-lane-docs") | Sort-Object
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect docs branch scope' }
if (Compare-Object $expectedDocs $actualDocs) {
    throw "docs branch scope mismatch: $($actualDocs -join ', ')"
}
Invoke-GitChecked cherry-pick "$base..codex/ultra-fast-lane-code"
Invoke-GitChecked cherry-pick "$base..codex/ultra-fast-lane-docs"
```

Expected: both operations succeed without overlapping-file conflict. Do not
accept a dirty integration worktree or a docs branch that touched the Python
source or tests.

- [ ] Run documentation GREEN now that both worker branches are present in the
  integration worktree:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_skill_documents_ultra_auto_policy `
  tests.test_team_efficiency.TeamEfficiencyTests.test_efficiency_reference_documents_host_contract
```

Expected: both pass.

- [ ] Confirm the implementation diff contains only the four approved feature
  files:

```powershell
$base=(& git merge-base codex/ultra-fast-lane-code codex/ultra-fast-lane-docs).Trim()
if ($LASTEXITCODE -ne 0) { throw 'cannot resolve immutable worker base' }
git diff --check "$base..HEAD"
if ($LASTEXITCODE -ne 0) { throw 'integrated diff has whitespace errors' }
$actualIntegrated=@(& git diff --name-only "$base..HEAD") | Sort-Object
if ($LASTEXITCODE -ne 0) { throw 'cannot enumerate integrated diff' }
$expectedIntegrated=@(
    'skills/work-methodology/SKILL.md',
    'skills/work-methodology/references/efficiency-automation.md',
    'skills/work-methodology/scripts/team_efficiency.py',
    'skills/work-methodology/tests/test_team_efficiency.py'
) | Sort-Object
if (Compare-Object $expectedIntegrated $actualIntegrated) {
    throw "integrated scope mismatch: $($actualIntegrated -join ', ')"
}
$actualIntegrated
```

Expected: no whitespace errors and no MCP, manifest, dependency, or unrelated
source file.

- [ ] Run legacy API/CLI compatibility:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_bootstrap_defaults_to_a_dry_run_with_only_safe_git_argv `
  tests.test_team_efficiency.TeamEfficiencyTests.test_resume_packet_is_canonical_bounded_and_secret_safe `
  tests.test_team_efficiency.TeamEfficiencyTests.test_status_markdown_keeps_pending_initialization_out_of_active_count `
  tests.test_team_efficiency.TeamEfficiencyTests.test_contract_check_fails_closed_on_artifact_or_schema_mismatch `
  tests.test_team_efficiency.TeamEfficiencyTests.test_cache_metadata_requires_an_exact_complete_fingerprint `
  tests.test_team_efficiency.TeamEfficiencyTests.test_decompose_emits_maximal_deterministic_capacity_bounded_waves `
  tests.test_team_efficiency.TeamEfficiencyTests.test_cli_canonicalizes_resume_packet_and_skill_links_the_reference `
  tests.test_team_efficiency.TeamEfficiencyTests.test_cli_decomposes_packet_and_episode_evidence_without_artifacts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_legacy_decompose_golden_bytes_are_unchanged
```

Expected: all pass and the golden remains 11,730 bytes with SHA-256
`f736b99d55bc562252d1fd6a98fb0f2d12813b0b50ba518f88d4f12c298a7775`.

- [ ] Run focused security and determinism checks:

```powershell
python -m unittest `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_has_no_external_side_effect `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_context_anchor_uniqueness_and_path_redaction `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_unsafe_gates_and_unbound_contexts `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_rejects_forged_stale_duplicate_or_cross_role_tokens `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_prewarm_critical_path_is_deterministic `
  tests.test_team_efficiency.TeamEfficiencyTests.test_fast_lane_allows_one_narrow_remediation_then_stops
```

Expected: all pass.

- [ ] Run the full suite:

```powershell
python -m unittest tests.test_team_efficiency
python -m unittest discover -s tests -p 'test_*.py'
```

Expected: the focused module and all work-methodology tests pass.

- [ ] Run a second full suite with randomized hash seed:

```powershell
$oldHashSeed=$env:PYTHONHASHSEED
try {
    $env:PYTHONHASHSEED='421'
    python -m unittest tests.test_team_efficiency
    $testExit=$LASTEXITCODE
}
finally {
    if ($null -eq $oldHashSeed) {
        Remove-Item Env:PYTHONHASHSEED -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONHASHSEED=$oldHashSeed
    }
}
if ($testExit -ne 0) { exit $testExit }
```

Expected: identical pass count and no order-sensitive failure.

- [ ] Verify source containment statically:

```powershell
Select-String -LiteralPath scripts\team_efficiency.py -Pattern `
  'compile_fast_lane|subprocess\.|Popen|requests|urllib|workflow_|git worktree add' `
  -Context 3,3
```

Expected: use this only as a manual context scan; legal existing bootstrap code
will match. The Task 3 runtime monkeypatch and scoped AST test are the
authoritative proof that `compile_fast_lane` and its private call graph contain
no execution, network, workflow, directory creation, or Git mutation call.

- [ ] Review every rendered dictionary and prove that absolute `repo`,
  `worktree`, and `temp_target` values appear only in internal validated
  contexts and hashes, never in plan output.

- [ ] Main Sol reviews the full diff against design Sections 5–17, verifies the
  12 high-risk boundaries, and owns the final acceptance decision.

- [ ] If source review finds a Critical or Important implementation defect,
  repair it through the normal bounded RED/GREEN development loop and re-run
  spec then quality review. The compiled runtime's one-round remediation limit
  does not restrict source-code review repairs.

- [ ] Commit any acceptance-only test correction with exact identity, then
  leave the branch clean:

```powershell
git status --short
```

Expected: no uncommitted changes.
