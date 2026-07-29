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

The decompose and plan-waves commands never invent semantic architecture. They
accept either a legacy explicit artifact manifest or a separately shaped
atlas_evidence manifest. In both cases the scheduler emits the largest safe
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
      "handoff_contracts": ["contracts/helper-api"]
    }
  ]
}
```

Source kind is optional only for the legacy mode; its default is
explicit_artifact_boundaries. A legacy artifacts list must not be labelled as
Code Atlas evidence. Every artifact must have a unique task id and output
boundary, a non-empty relative write scope, declared dependencies, required
evidence, complexity, and explicit handoff contracts. Equal paths and
ancestor/descendant paths conflict. Unsafe, wildcard-like, absolute,
traversal, unknown-dependency, and cyclic manifests are rejected.

### Verified Code Atlas evidence

Code Atlas planning requires decomposition atlas_evidence and one of two real
model serializations:

- code_atlas_packet accepts an exact ImplementationPacket.to_dict() object plus
  path_bindings. Bindings are render inputs, not a replacement schema: their
  keys must exactly cover actual TemplateOperation.path_slot values, and every
  used slot must be an actual SlotSpec of type relative_python_path.
- task_episode_graph accepts an exact GraphQueryResult.to_dict() object. Its
  scopes come only from real TaskEpisode --CHANGES--> SourceEvidence edges and
  concrete SourceEvidence payload paths; this mode has no path-binding field.

The helper validates every public dataclass field from the packet, graph, node,
edge, operation, slot, constraint, dependency, and test records. It checks
Code Atlas packet/node/edge canonical identifiers, permits only actual NodeKind
and EdgeRelation values, and rejects look-alike graph fields. It does not
import or call Code Atlas, a remote service, an LLM, or a vector store; it
consumes inert JSON produced by to_dict().

For a complete packet, operations resolving to one target path merge into one
code unit. Distinct safe paths can share the first wave; a verification unit
depends on every code unit and therefore cannot run early. DependencySpec
describes environment requirements, not invented task edges. The direct
contract hash is canonically derived from the verified packet_id and recipe_id;
no extra packet field or ConstraintSpec convention is accepted.

For a TaskEpisode graph, each code unit is a real code TaskEpisode. CHANGES
evidence supplies its write scope and VERIFIED_BY TestSpec or ExecutionReceipt
evidence supplies its acceptance evidence. Its direct contract hash is
canonically derived from the normalized verified graph fingerprint and its
TaskEpisode node id. It is an execution-contract identity, not a graph
handoff-node claim. A Recipe --SUPERSEDES--> Recipe edge serializes the newer
episode behind the older one; overlapping source-evidence paths are also
serialized by the conflict graph. Sharing a graph fingerprint never creates a
dependency edge by itself.

Truncated graphs, packet gaps, missing render slots, missing concrete changed
paths, missing verified evidence, or unknown semantics return status
needs_design with no units or waves. Malformed paths, hashes, identifiers,
fields, or graph endpoints are rejected. Sol owns the resulting design
decision, dispatch, review, integration, and final acceptance.

## CLI

```text
python scripts/team_efficiency.py resume-packet --input <packet.json>
python scripts/team_efficiency.py status --input <snapshot.json>
python scripts/team_efficiency.py contract-check --producer <producer.json> --consumer <consumer.json>
python scripts/team_efficiency.py cache-key --input <cache-inputs.json>
python scripts/team_efficiency.py decompose --input <work-package.json>
python scripts/team_efficiency.py plan-waves --input <work-package.json>
```
