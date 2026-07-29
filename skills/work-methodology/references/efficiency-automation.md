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
`CODEX_TASK_TEMP` to those child processes. The plan also emits the minimal fields for the host's existing
`workflow_register_task`, `workflow_claim`, and `workflow_endpoint_bind`
requests.

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

`decompose` and `plan-waves` compile an explicit, bounded artifact manifest
into deterministic execution waves. They do not create semantic architecture
or task boundaries.

```json
{
  "schema": "team-efficiency/work-package-v1",
  "task_id": "ATLAS-12B",
  "goal": "Deliver one bounded result",
  "capacity": 2,
  "decomposition": "artifact_boundaries",
  "source_kind": "code_atlas_packet",
  "artifacts": [
    {
      "task_id": "ATLAS-12B-A",
      "goal": "Implement the bounded artifact",
      "output_boundary": "helper module",
      "write_scope": ["skills/work-methodology/scripts/team_efficiency.py"],
      "depends_on": [],
      "required_evidence": ["focused-helper-tests"],
      "complexity": "routine",
      "handoff_contracts": ["contracts/helper-api"]
    }
  ]
}
```

`source_kind` is optional; the default is `explicit_artifact_boundaries`.
`code_atlas_packet` and `task_episode_graph` preserve the provenance of a
known, accepted Code Atlas implementation packet or TaskEpisode graph. The
helper does not import or call Code Atlas: it validates and compiles only the
graph-derived boundaries provided in the manifest.

Every artifact must have a unique task id and output boundary, a non-empty
relative write scope, declared dependencies, required evidence, complexity,
and explicit handoff contracts. Scopes are normalized and form a conflict graph:
equal paths and ancestor/descendant paths conflict. Unsafe, wildcard-like,
absolute, traversal, unknown-dependency, and cyclic manifests are rejected.

The resulting units retain task id, goal, output boundary, normalized scope,
dependencies, required evidence, recommended route, and handoff contracts.
The planner emits the largest safe ready wave up to declared capacity, then
uses a stable task-id tie-break. Routes are `Terra High` for `routine`, `Terra
Max` for `moderate` or `complex`, and `Sol High` only for `exceptional` work.

When no explicit artifact boundaries or graph-derived template exists, submit a
manifest with `"decomposition": "semantic"`. The result is
`"status": "needs_design"` with no invented units. Sol owns semantic
architecture, dispatch decisions, review, integration, and final acceptance;
this helper only compiles, validates, and schedules declared boundaries.

## CLI

```text
python scripts/team_efficiency.py resume-packet --input <packet.json>
python scripts/team_efficiency.py status --input <snapshot.json>
python scripts/team_efficiency.py contract-check --producer <producer.json> --consumer <consumer.json>
python scripts/team_efficiency.py cache-key --input <cache-inputs.json>
python scripts/team_efficiency.py decompose --input <work-package.json>
python scripts/team_efficiency.py plan-waves --input <work-package.json>
```
