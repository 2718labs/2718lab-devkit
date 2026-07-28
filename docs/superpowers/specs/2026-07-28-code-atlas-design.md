# 2718lab Code Atlas Design

## Goal

Add a deterministic, graph-backed code-reuse layer to `2718lab-devkit` so a
coding worker can retrieve a verified implementation packet instead of
reconstructing common code patterns from scratch on every task.

The same Atlas core serves Codex and Claude hosts. Host-specific model routing
and native coding tools remain outside the graph contract.

## Product Decisions

- Reuse the plugin-owned `project_index` as the only repository graph.
- Do not read, call, depend on, fall back to, or integrate any external
  CodeGraph server or namespace.
- Keep one MCP server namespace: `2718lab-tools`.
- Build a two-layer knowledge base:
  - bundled, version-controlled official recipes shipped with the plugin;
  - locally verified recipes and successful task episodes under `PLUGIN_DATA`.
- Store knowledge as typed nodes and edges. Pattern Cards are import, export,
  and review views, not the authoritative database.
- Index Python deeply in version one while keeping the graph and extractor
  protocols language-neutral.
- Do not use an LLM, embeddings, or semantic-vector retrieval to construct,
  score, render, or ingest graph facts.
- Prefer Code/exec for batched host-tool orchestration when it is available.
  Fall back to direct host tool calls when it is not.
- MCP tools may return patch candidates and command specifications, but never
  execute a shell command or apply a patch themselves.
- A successfully accepted coding task automatically enters the knowledge
  pipeline. It does not require a second recipe-review action.
- Every accepted coding task becomes a `TaskEpisode`. Only deterministic,
  round-trip-verifiable changes become reusable `Recipe` subgraphs.

## Non-Goals

- A second repository-index MCP server.
- External CodeGraph compatibility or migration.
- Embedding models, vector databases, learned rerankers, or LLM summaries.
- Automatic repository writes from an Atlas MCP tool.
- Treating an unverified candidate or ambiguous match as a usable recipe.
- Full multi-language parsing in the first release.
- Silently replacing an unavailable requested model with another model.

## System Architecture

The existing project index remains the authoritative read model for the current
workspace. Code Atlas adds a durable knowledge graph and joins it with a fresh
project-index snapshot at query time.

```text
User intent
  -> coordinator acceptance contract
  -> current project-index snapshot
  -> deterministic Atlas match
  -> implementation packet
  -> coding worker uses native host tools
  -> verification receipts and output snapshot
  -> coordinator accepts code task
  -> TaskEpisode node
  -> deterministic reuse gate
     -> episode only, or
     -> verified Recipe subgraph
```

Storage boundaries:

- `project-index.sqlite3`: current-workspace snapshots, source nodes, edges,
  hashes, query receipts, and coverage gaps.
- `orchestrator.sqlite3`: workflows, typed tasks, leases, acceptance records,
  evidence metadata, and the Atlas ingestion outbox.
- `code-atlas.sqlite3`: local knowledge nodes, edges, recipe versions, packet
  receipts, and ingestion receipts.
- `PLUGIN_DATA/code-atlas-cas/`: content-addressed recipe template blobs. It
  stores bounded verified fragments, not complete repository files.
- plugin assets: bundled official recipe manifests and template blobs.

The knowledge projection is rebuildable from bundled recipes, accepted task
records, checkpoint references, output snapshots, and immutable receipts.

## Host Profiles And Model Routing

Model routing is host-specific and must be declared separately from the shared
Atlas graph.

### Codex

- Sol owns the main conversation, architecture, task decomposition, scheduling,
  review, and final acceptance.
- Luna with `max` reasoning owns coding, tests, debugging, and other heavy
  execution.
- Terra owns bounded medium-complexity analysis, documentation, and auxiliary
  validation.
- Coding dispatch always requests Luna with `max` reasoning first. If that
  spawn fails specifically because the model is unavailable, the requested
  reasoning effort is unavailable, or the multi-agent protocol is
  incompatible, dispatch automatically retries once with Terra at `medium`
  reasoning. No other fallback is permitted.
- Every fallback result/receipt must disclose `requested_model`,
  `requested_reasoning_effort`, `effective_model`,
  `effective_reasoning_effort`, and one of the recorded reasons
  `MODEL_UNAVAILABLE`, `REASONING_UNAVAILABLE`, or
  `MULTI_AGENT_PROTOCOL_INCOMPATIBLE`. Terra must never be presented as Luna.
- If Terra at `medium` is also unavailable, return `MODEL_UNAVAILABLE` with
  both attempted capabilities and their failures. Non-capability spawn errors
  are returned directly and do not trigger a model fallback.

### Claude

- Opus owns the main conversation, architecture, scheduling, review, and final
  acceptance.
- Sonnet is the default coding, testing, debugging, and medium-complexity
  execution model.
- Haiku handles lightweight investigation, metadata organization, formatting,
  and mechanical checks. Deterministic graph construction itself uses no model.
- Fable is a high-cost, high-capability escalation lane for long-running
  refactors, cross-repository migrations, repeated failure, or explicitly
  requested frontier work.
- Fable has a default automatic budget of zero. It requires an explicit
  escalation condition or user selection.
- Resolve full model identifiers from host capabilities. Do not assume a short
  alias exists, and do not silently fall back.

## Typed Knowledge Graph

### Node kinds

- `TaskEpisode`: one successfully accepted coding task.
- `Intent`: a stable action-oriented problem class.
- `Recipe`: one reusable implementation pattern and version.
- `CodeTemplate`: a content-addressed patch or code template.
- `AdaptationSlot`: a typed placeholder with validation rules.
- `Constraint`: an applicability or prohibition rule.
- `Dependency`: a package, framework, API, configuration, or version
  requirement.
- `TestSpec`: an exact verification command specification and expected result.
- `ExecutionReceipt`: host-observed command or patch evidence.
- `SourceEvidence`: a snapshot-bound reference to source symbols, tests, and
  changed spans.
- `Language`: a language and extractor protocol version.
- `Framework`: an optional framework and supported version range.

### Edge relations

- `SOLVES`: `TaskEpisode|Recipe -> Intent`
- `DERIVED_FROM`: `Recipe -> TaskEpisode|SourceEvidence`
- `HAS_IMPLEMENTATION`: `Recipe -> CodeTemplate`
- `HAS_SLOT`: `Recipe -> AdaptationSlot`
- `CONSTRAINED_BY`: `Recipe -> Constraint`
- `REQUIRES`: `Recipe -> Dependency|Framework|Language`
- `VERIFIED_BY`: `TaskEpisode|Recipe -> TestSpec|ExecutionReceipt`
- `CHANGES`: `TaskEpisode -> SourceEvidence`
- `TESTS`: `TestSpec|SourceEvidence -> SourceEvidence`
- `SUPERSEDES`: `Recipe -> Recipe`
- `BUNDLED_AS`: `Recipe -> SourceEvidence`

Each node and edge includes:

- a deterministic content-derived identifier;
- schema and extractor versions;
- provenance (`observed`, `resolved`, or `declared`);
- source or receipt hashes;
- creation and supersession metadata;
- compatibility and quarantine state.

No edge is created from prose similarity alone.

## Official And Local Recipe Layers

Bundled recipes are machine-readable JSON manifests plus content-addressed
template files under the Code Atlas skill assets. JSON is the canonical format
because the server can validate it with the Python standard library.

Generated Pattern Cards provide a readable Markdown projection containing:

- intent;
- applicability and exclusions;
- adaptation slots;
- dependencies and framework versions;
- canonical template;
- verification commands;
- provenance and content hashes.

Local accepted recipes use the same node and template contract in
`code-atlas.sqlite3` and `code-atlas-cas`. Exporting a local recipe creates a
deterministic promotion bundle for a later plugin-source patch. The installed
plugin cache is never modified.

Version one ships a small verified seed set:

- an official Python MCP SDK read-only tool wrapper;
- a Python validation guard with a regression test;
- a Python pytest regression-test pattern.

Unsupported shapes remain episodes until a deterministic extractor is added.

## Language Extractor Contract

A language extractor receives:

- task intent and typed task metadata;
- the input project-index snapshot;
- the task-owned pre-write checkpoint;
- the accepted output snapshot;
- changed source nodes and exact byte spans;
- host-observed verification receipts.

It may emit:

- normalized edit operations;
- a template and typed slot definitions;
- applicability constraints;
- dependencies and framework bounds;
- linked test specifications;
- provenance gaps that prevent recipe creation.

The first extractor is `PythonRecipeExtractor`. Future TypeScript, Rust, Go, or
other extractors implement the same result contract without changing MCP tool
schemas or graph tables.

## Deterministic Reuse Gate

Every accepted coding task creates a `TaskEpisode`. A recipe is created only
when all of these conditions hold:

1. The task is typed as code and has a non-empty bounded write scope.
2. Input and output snapshots are current and belong to the same workspace.
3. A task-owned pre-write checkpoint reconstructs the input files.
4. Required host-observed verification receipts have successful exit status and
   bind to the accepted output snapshot.
5. The supported language extractor recognizes a bounded edit shape.
6. Touched files are not generated, vendored, binary, secret-bearing, or above
   configured size limits.
7. No blocking parser gap affects the changed symbols.
8. The extracted template and original slot bindings render against the input
   checkpoint to reproduce the accepted output hashes exactly.
9. The recipe manifest, nodes, edges, and blobs hash identically on a second
   extraction pass.

Failure of any reuse condition produces an episode-only ingestion receipt with
stable reasons. It does not weaken the gate or request an LLM judgment.

## Retrieval And Implementation Packets

Matching is a deterministic join across:

- normalized intent identifiers and terms;
- language and framework compatibility;
- target node kinds and structural relations;
- repository symbols, imports, calls, and tests;
- recipe constraints and supersession state;
- current source hashes and coverage gaps.

Stable discrete match classes are used instead of learned scores. An equal best
match returns `AMBIGUOUS_MATCH`; it never selects one by arbitrary order.

An implementation packet contains:

- packet and trace identifiers;
- selected recipe nodes and edges;
- current repository evidence windows;
- the canonical template and typed slots;
- required slot bindings and validation rules;
- dependencies and version constraints;
- applicability and prohibition conditions;
- exact test command specifications;
- source, template, snapshot, and receipt hashes;
- uncovered gaps and an explicit next action.

`code_atlas_render` validates supplied bindings and returns a patch candidate.
It never writes the patch.

## MCP Tool Contract

### `code_atlas_graph_query`

Bounded read-only query over episode, recipe, test, dependency, and evidence
nodes. It follows explicit relations and byte/node/depth budgets.

### `code_atlas_prepare`

Read-only deterministic retrieval using an intent and current project-index
snapshot. It returns an implementation packet or a stable non-ready status.

### `code_atlas_render`

Read-only template rendering. It validates packet freshness, slot types,
constraints, and source hashes, then returns a patch candidate and verification
plan.

### `workflow_accept_code_task`

An idempotent orchestration mutation used by the coordinator acceptance task.
It validates task completion, snapshots, execution receipts, risk gates, and
acceptance authority. In the same orchestrator transaction it records the
acceptance and enqueues one content-addressed Atlas ingestion item.

After committing acceptance, the server attempts the deterministic projection
synchronously. A crash or storage failure leaves an outbox item for automatic
startup retry. Repeated acceptance of the same output returns the original
receipt and graph identifiers. Existing workflow status projections expose the
acceptance receipt and `atlas_ingest_state` so a pending or quarantined
projection is visible without adding another status tool.

None of these tools executes host commands, invokes models, or applies patches.

## Code/exec And Hook Integration

When Code/exec is available, the coding worker may batch:

1. project-index freshness checks;
2. Atlas preparation;
3. packet rendering;
4. direct host patch and verification calls;
5. output index refresh and evidence registration.

When it is unavailable, the same calls run directly in sequence.

Plugin `PostToolUse` hooks observe supported host calls, including Code/exec
nested calls, and create bounded execution receipts. A receipt records:

- canonical tool and command-spec identifiers;
- task, workflow, turn, and tool-use correlation ids;
- command or patch input hash;
- exit status and redacted output hash;
- workspace and output snapshot binding;
- hook schema version and timestamp.

The hook does not store complete stdout, stderr, secrets, or arbitrary source
files. Receipt capture is a guardrail and evidence source, not permission to
execute a command.

## Automatic Ingestion And Recovery

The acceptance record and outbox item are written in one orchestrator
transaction. Atlas projection is idempotent:

- the acceptance receipt hash is the ingestion key;
- node, edge, and blob identifiers are content-derived;
- a repeated projection returns the existing identifiers;
- a changed output hash requires a new acceptance;
- a conflicting payload under the same key is rejected.

The server drains pending outbox items on startup and after successful
acceptance calls. An accepted code task is not rolled back because the
rebuildable knowledge projection is temporarily unavailable.

## Status And Failure Contract

- `READY`: one verified compatible implementation packet is available.
- `NO_VERIFIED_RECIPE`: proceed with normal coding and allow later ingestion.
- `INDEX_STALE`: refresh once; never render from the stale snapshot.
- `AMBIGUOUS_MATCH`: return conflicting nodes and reasons without a patch.
- `UNSUPPORTED_LANGUAGE`: create an episode only.
- `RENDER_INVALID`: reject missing, invalid, or incompatible slot bindings.
- `EVIDENCE_INCOMPLETE`: block code-task acceptance.
- `RECIPE_QUARANTINED`: retain evidence but exclude the recipe from retrieval.
- `INGEST_PENDING`: acceptance succeeded; deterministic projection awaits retry.
- `ATLAS_UNAVAILABLE`: report loss of reuse and ingestion, then follow the
  workflow's explicit degraded-mode policy.
- `MODEL_UNAVAILABLE`: Luna Max and the sole permitted Terra medium fallback
  are unavailable; report requested/effective capability attempts and reasons.

Code/exec absence degrades to direct host calls. External CodeGraph is never a
fallback.

## Security And Privacy

- Reject path escape, unsafe links, reparse points, generated/vendor paths, and
  over-budget fragments.
- Scan bounded candidate fragments and metadata for secrets before blob storage.
- Keep raw command output and complete repository files out of Atlas storage.
- Bind every reusable source fragment to an accepted snapshot and content hash.
- Keep tool annotations honest: graph query, prepare, and render are read-only;
  acceptance is an idempotent journal mutation.
- Treat plugin-bundled hooks as inactive until the host has reviewed and trusted
  their definitions. Missing hook trust means verification evidence is
  incomplete, not implicitly successful.
- Treat templates and command specifications as untrusted until their hashes,
  constraints, and current workspace bindings validate.
- Never infer authorization from a successful test or a recipe match.

## Testing Strategy

### Unit tests

- Graph schema, identifiers, edge constraints, and migrations.
- Python AST edit extraction and unsupported-shape gaps.
- Reuse eligibility and episode-only reasons.
- Stable matching, ambiguity, compatibility, and supersession.
- Slot validation, rendering, and exact round-trip output hashes.
- Secret, generated-file, size, and parser-gap quarantine.
- Codex Luna-Max-first routing, the disclosed Terra-medium-only fallback,
  terminal `MODEL_UNAVAILABLE`, Claude routing, and Fable's zero default
  budget.

### Contract tests

- MCP names, input/output schemas, annotations, and error envelopes.
- No Atlas tool exposes shell execution or patch application.
- No plugin manifest, Atlas module, or runtime route depends on an external
  CodeGraph namespace.
- Bundled recipe manifests and templates parse and hash deterministically.

### Integration tests

- First task has no recipe, is implemented and accepted, creates an episode and
  recipe, and makes the next matching task return `READY`.
- Rename-only, documentation-only, generated-file, unsupported-language, and
  unverified tasks remain episode-only or are rejected as specified.
- A stale snapshot, ambiguous match, invalid slot, or changed source hash never
  returns a patch.
- Simulated failure after acceptance leaves one outbox item; restart recovery
  creates one graph projection.
- Duplicate acceptance and ingestion are idempotent.
- Bundled and local recipes use the same query and packet protocol.
- Codex Code/exec and Claude/native hook payload fixtures produce equivalent
  bounded execution receipts.

### End-to-end acceptance

Use an isolated Python fixture repository under
`D:\bun\tmp\codex\2718-devkit`. Run two full cycles:

1. no-match implementation, native patch, real tests, acceptance, and automatic
   graph ingestion;
2. matching intent, packet retrieval, deterministic render, native patch, real
   tests, and acceptance.

The same intent and snapshot must produce the same node ids, edge ids, packet,
and hashes. Rendering the same packet with the same validated slot bindings must
produce the same patch candidate. Graph construction, matching, rendering, and
ingestion must make zero LLM calls.

Run all affected legacy project-index, checkpoint, orchestrator, Bugkiller, MCP
contract, methodology, and hook tests. The MCP validator must report zero
errors, and plugin manifests plus hook JSON must parse successfully.

## Initial Delivery Boundary

Version one delivers:

- the language-neutral Atlas graph and store;
- the Python extractor framework and the bounded initial shapes;
- official and local recipe loading;
- the four MCP tools above;
- acceptance-driven automatic episode and recipe ingestion;
- host receipt hooks compatible with direct and Code/exec calls;
- Codex and Claude routing assets;
- three bundled verified seed recipes;
- regression and two-cycle end-to-end tests.

It does not deliver external CodeGraph integration, semantic retrieval,
full-language coverage, remote synchronization, public recipe publishing, or
automatic Fable usage.
