# 2718lab Code Atlas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic typed code-recipe graph that returns verified implementation packets, automatically records accepted coding tasks, and promotes only round-trip-verifiable Python edits into reusable recipes.

**Architecture:** Keep the current repository graph in `project-index.sqlite3`, workflow acceptance and its outbox in `orchestrator.sqlite3`, and local recipe knowledge in a separate `code-atlas.sqlite3` plus a bounded content-addressed store. `CodeAtlasService` joins bundled and local recipes with a fresh project-index snapshot; it returns data or patch candidates only, while native Codex/Claude tools remain responsible for edits and commands.

**Tech Stack:** Python 3.11+ standard library, SQLite/WAL, official MCP Python SDK v1, Python `ast`, `difflib`, JSON manifests, pytest/unittest-compatible tests, Codex/Claude plugin hooks.

---

## Scope and locked contracts

This stays one implementation plan because graph storage, deterministic
extraction, acceptance/outbox, and host receipts share one two-cycle acceptance
test. Splitting them into independently shipped plans would leave automatic
ingestion or safe rendering unverifiable between releases.

The implementation must preserve these decisions:

- Never import, call, query, discover, or fall back to an external CodeGraph.
- Never use an LLM, embedding model, vector database, learned ranker, or model
  summary inside graph construction, matching, rendering, or ingestion.
- Never let an Atlas MCP tool execute a command, apply a patch, or write into a
  target workspace.
- Keep bundled recipes under plugin source and local recipes under the resolved
  durable data root. Never write into an installed plugin cache.
- Every accepted code task creates one `TaskEpisode`; recipe creation is gated
  separately and may result in an episode-only receipt.
- Codex routing is Sol coordinator/final acceptor, Luna with `max` reasoning for
  code/test/debug execution, and Terra for bounded medium-complexity work.
- Claude routing is Opus coordinator/final acceptor, Sonnet default executor,
  Haiku lightweight worker, and Fable explicit expensive escalation with an
  automatic budget of zero.
- Requested model unavailability returns `MODEL_UNAVAILABLE`; no role may be
  silently impersonated by another model.

### Canonical identity rules

Use UTF-8 JSON with sorted keys, compact separators, `ensure_ascii=False`, and
`allow_nan=False`. A node id is the SHA-256 of:

```json
{
  "kind": "Recipe",
  "schema_version": "1",
  "extractor_id": "python-recipe",
  "extractor_version": "1",
  "provenance": "observed",
  "payload": {},
  "source_hashes": []
}
```

An edge id uses the same rule over `relation`, `source_id`, `target_id`,
`schema_version`, `provenance`, and immutable payload. `created_at`,
`superseded_at`, quarantine state, retry counts, and storage sequence numbers
must not participate in content ids.

### Version-one Python edit shapes

`PythonRecipeExtractor` recognizes only:

1. `create_python_file`: an added UTF-8 `.py` file that parses successfully and
   is within the task write scope.
2. `append_python_nodes`: an existing UTF-8 `.py` file whose accepted bytes
   begin with the exact checkpoint bytes and whose suffix parses as complete
   top-level `FunctionDef`, `AsyncFunctionDef`, or `ClassDef` nodes.

One accepted task may contain at most eight touched files and 256 KiB of
candidate source. Deletions, in-place replacements, rename-only edits,
documentation-only edits, imports appended outside a complete node, generated
files, vendored files, binary files, secret-bearing fragments, unsupported
languages, and parser gaps remain episode-only with stable reasons.

The extractor parameterizes only:

- target relative paths;
- names of newly added top-level functions/classes;
- names of newly added pytest functions.

All other code remains exact template text. Rendering with the original
bindings against checkpoint bytes must reproduce the accepted output hashes.
A second extraction pass must produce byte-identical canonical manifests.

### Deterministic match classes

Candidates are deduplicated by recipe id and ordered for display by
`(match_class_rank, recipe_id)`. Selection uses:

1. `EXACT_REPOSITORY`: exact intent/language plus a compatible local repository
   structural signature.
2. `EXACT_FRAMEWORK`: exact intent/language/framework and all constraints.
3. `LANGUAGE_GENERIC`: exact intent/language and no framework requirement.

A unique best active candidate returns `READY`. More than one non-superseded
candidate in the best class returns `AMBIGUOUS_MATCH`; recipe id order is only
for display and never breaks a tie. Matching only quarantined candidates returns
`RECIPE_QUARANTINED`; no candidate returns `NO_VERIFIED_RECIPE`.

### Frozen MCP signatures

The four new functions use the existing `_safe_call` envelope:

```text
code_atlas_graph_query(
    root_node_ids: list[str] | None = None,
    node_kinds: list[str] | None = None,
    relations: list[str] | None = None,
    intent_id: str = "",
    max_nodes: int = 50,
    max_edges: int = 100,
    max_depth: int = 1,
    byte_budget: int = 65_536,
) -> dict[str, Any]


code_atlas_prepare(
    workspace: str,
    snapshot_id: str,
    intent_id: str,
    language: str = "python",
    framework: str = "",
    target_paths: list[str] | None = None,
    target_symbols: list[str] | None = None,
    max_candidates: int = 20,
    byte_budget: int = 131_072,
) -> dict[str, Any]


code_atlas_render(
    workspace: str,
    snapshot_id: str,
    packet_id: str,
    bindings: dict[str, str],
) -> dict[str, Any]


workflow_accept_code_task(
    workflow_id: str,
    code_task_id: str,
    expected_code_task_version: int,
    expected_output_snapshot_id: str,
    coordinator_task_id: str,
    coordinator_owner: str,
    coordinator_lease_epoch: int,
    execution_receipt_ids: list[str],
) -> dict[str, Any]
```

The first three tools are `_READ_ONLY`; acceptance is
`_IDEMPOTENT_MUTATION`.

### Acceptance and outbox boundary

A task eligible for `workflow_accept_code_task` must:

- have `task_kind="code"`, a normalized explicit `intent_id`, a language, and a
  non-empty bounded write scope;
- be `DONE` at `expected_code_task_version`;
- have a strict input snapshot, task-owned checkpoint, output snapshot, indexed
  diff, output query receipt, and verification artifact;
- have only successful hook receipts bound to the output workspace/snapshot;
- be accepted by a running coordinator task in the same workflow whose active
  lease owner/epoch match and whose role is `sol` or `opus`.

The orchestrator transaction inserts one immutable acceptance and one
content-addressed outbox row. Projection into `code-atlas.sqlite3` occurs only
after that transaction commits. Projection failure leaves the acceptance valid
and the outbox `pending`; startup and later acceptance calls retry it.

### Resource and security bounds

Use these constants from `code_atlas/security.py`:

```python
MAX_CHANGED_FILES = 8
MAX_TEMPLATE_BYTES = 65_536
MAX_RECIPE_BYTES = 262_144
MAX_PACKET_BYTES = 524_288
MAX_SLOT_COUNT = 32
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_GRAPH_DEPTH = 4
MAX_COMMAND_SPEC_BYTES = 4_096
GENERATED_COMPONENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "__pycache__",
    }
)
```

Reject absolute paths, `..`, path escapes, symlinks/reparse points, binary
content, filenames matching `*.generated.*` or `*_pb2.py`, and fragments that
still match a credential detector after redaction.

## File map

Create:

- `mcp-tools/code_atlas/__init__.py`: stable public exports.
- `mcp-tools/code_atlas/models.py`: enums and immutable domain records.
- `mcp-tools/code_atlas/canonical.py`: canonical JSON, ids, intent normalization.
- `mcp-tools/code_atlas/security.py`: path/content/slot limits and quarantine.
- `mcp-tools/code_atlas/store.py`: local graph SQLite, CAS, receipts, packets.
- `mcp-tools/code_atlas/recipes.py`: bundled manifest loading and Pattern Cards.
- `mcp-tools/code_atlas/extractors.py`: language-neutral protocol and Python v1.
- `mcp-tools/code_atlas/matching.py`: discrete compatibility and tie handling.
- `mcp-tools/code_atlas/rendering.py`: slot validation and patch generation.
- `mcp-tools/code_atlas/receipts.py`: bounded raw hook receipt format/import.
- `mcp-tools/code_atlas/service.py`: graph query, prepare, render, projection.
- `mcp-tools/code_atlas/routing.py`: deterministic host-role capability checks.
- `hooks/execution_receipt.py`: fail-open bounded PostToolUse capture.
- `skills/code-atlas/SKILL.md`: thin host workflow entry.
- `skills/code-atlas/references/atlas-workflow.md`: host orchestration contract.
- `skills/code-atlas/references/host-routing.md`: Codex/Claude role policy.
- `skills/code-atlas/references/status-contract.md`: stable statuses and actions.
- `skills/code-atlas/assets/host-profiles.json`: machine-readable routing policy.
- `skills/code-atlas/assets/recipes/*.json`: three bundled recipe manifests.
- `skills/code-atlas/assets/templates/sha256/*`: three verified template blobs.
- `skills/code-atlas/scripts/validate_recipes.py`: deterministic asset validator.
- `skills/code-atlas/scripts/export_recipe.py`: local promotion-bundle exporter.
- `agents/bugkiller-luna-code-worker.md`: scoped Luna Max code/test/debug worker.
- `mcp-tools/tests/test_code_atlas_models.py`
- `mcp-tools/tests/test_code_atlas_store.py`
- `mcp-tools/tests/test_code_atlas_recipes.py`
- `mcp-tools/tests/test_code_atlas_extractor.py`
- `mcp-tools/tests/test_code_atlas_service.py`
- `mcp-tools/tests/test_code_atlas_receipts.py`
- `mcp-tools/tests/test_code_atlas_acceptance.py`
- `mcp-tools/tests/test_code_atlas_routing.py`
- `mcp-tools/tests/test_code_atlas_e2e.py`

Modify:

- `mcp-tools/project_index/models.py`, `service.py`, and `checkpoints.py` for
  internal snapshot/checkpoint reads with existing safety checks.
- `mcp-tools/orchestrator/models.py`, `store.py`, and `service.py` for typed code
  tasks, acceptance, outbox, retry projection state, and status.
- `mcp-tools/server.py` for runtimes, four MCP tools, and startup retry.
- `hooks/hooks.json` to add receipt capture without removing metadata checks.
- Bugkiller/work-methodology agent, skill, validator, test, README, and plugin
  metadata files listed in Task 11.

Do not create another repository-index package, MCP namespace, model service, or
workspace-writing Atlas command.

## Test environment

Run every command in PowerShell with:

```powershell
$taskRoot = 'D:\bun\tmp\codex\2718-devkit\code-atlas'
New-Item -ItemType Directory -Force -Path $taskRoot | Out-Null
$env:TEMP = $taskRoot
$env:TMP = $taskRoot
$env:TMPDIR = $taskRoot
$env:CODEX_TASK_TEMP = $taskRoot
$env:BUGKILLER_HOME = Join-Path $taskRoot 'runtime-data'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'mcp-tools'
```

No test, fixture, cache, screenshot, SQLite file, or evidence artifact may use a
C-drive temporary directory.

### Task 1: Portable D-drive test foundation

**Files:**

- Create: `mcp-tools/tests/temp_support.py`
- Modify: `mcp-tools/tests/test_orchestrator_messaging.py:7-27`
- Modify: `mcp-tools/tests/test_orchestrator_service.py:5-32`
- Modify: `mcp-tools/tests/test_orchestrator_store_service_api.py:5-35`
- Modify: `mcp-tools/tests/test_project_index_workflow.py:5-58`
- Test: `mcp-tools/tests/test_temp_policy.py`

- [ ] **Step 1: Write the failing policy test**

```python
from pathlib import Path

from temp_support import task_scratch


def test_task_scratch_stays_under_configured_root(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "codex-task"
    monkeypatch.setenv("CODEX_TASK_TEMP", str(configured))
    actual = task_scratch("atlas")
    assert actual == (configured / "atlas").resolve()
    assert actual.is_dir()


def test_task_scratch_rejects_escape(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_TASK_TEMP", str(tmp_path))
    try:
        task_scratch("../escape")
    except ValueError as error:
        assert str(error) == "scratch name must be one safe path component"
    else:
        raise AssertionError("escape was accepted")
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_temp_policy.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named
'temp_support'`.

- [ ] **Step 3: Implement the portable helper**

Create `mcp-tools/tests/temp_support.py` with:

```python
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def task_scratch(name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("scratch name must be one safe path component")
    base = Path(os.environ.get("CODEX_TASK_TEMP", tempfile.gettempdir())).resolve()
    target = (base / name).resolve()
    if target.parent != base:
        raise ValueError("scratch name must be one safe path component")
    target.mkdir(parents=True, exist_ok=True)
    return target
```

Replace the four hard-coded legacy Bugkiller scratch roots with:

- `task_scratch("orchestrator-messaging")`;
- `task_scratch("orchestrator-service")`;
- `task_scratch("orchestrator-store")`;
- `task_scratch("strict-project-index")`.

Preserve each test's `TemporaryDirectory` cleanup.

- [ ] **Step 4: Run the affected legacy tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_temp_policy.py `
  mcp-tools/tests/test_orchestrator_messaging.py `
  mcp-tools/tests/test_orchestrator_service.py `
  mcp-tools/tests/test_orchestrator_store_service_api.py `
  mcp-tools/tests/test_project_index_workflow.py -q
```

Expected: all tests pass and every created path is below `$env:CODEX_TASK_TEMP`.

- [ ] **Step 5: Commit**

```powershell
git add mcp-tools/tests
git commit -m "test: isolate devkit scratch storage"
```

### Task 2: Canonical graph records and security policy

**Files:**

- Create: `mcp-tools/code_atlas/__init__.py`
- Create: `mcp-tools/code_atlas/canonical.py`
- Create: `mcp-tools/code_atlas/models.py`
- Create: `mcp-tools/code_atlas/security.py`
- Test: `mcp-tools/tests/test_code_atlas_models.py`

- [ ] **Step 1: Write failing tests for ids, types, and bounds**

The test module must contain these exact behaviors:

```python
from dataclasses import replace

import pytest

from code_atlas.canonical import canonical_id, normalize_intent_id
from code_atlas.models import AtlasEdge, AtlasNode, EdgeRelation, NodeKind
from code_atlas.security import validate_candidate_path


def test_node_id_ignores_mutable_storage_metadata() -> None:
    first = AtlasNode.create(
        kind=NodeKind.RECIPE,
        payload={"intent_id": "python.validation-guard"},
        extractor_id="python-recipe",
        extractor_version="1",
        provenance="observed",
        source_hashes=("sha256:source",),
        created_at="2026-07-28T00:00:00+00:00",
    )
    second = replace(
        first,
        created_at="2027-01-01T00:00:00+00:00",
        quarantine_state="quarantined",
    )
    assert first.node_id == second.node_id


def test_edge_rejects_invalid_endpoint_kinds() -> None:
    recipe = AtlasNode.create(NodeKind.RECIPE, {"name": "r"})
    language = AtlasNode.create(NodeKind.LANGUAGE, {"name": "python"})
    with pytest.raises(ValueError, match="edge endpoints"):
        AtlasEdge.create(EdgeRelation.CHANGES, recipe, language)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Python.Validation Guard ", "python.validation-guard"),
        ("python__pytest regression", "python-pytest-regression"),
    ],
)
def test_intent_normalization_is_deterministic(raw: str, expected: str) -> None:
    assert normalize_intent_id(raw) == expected


@pytest.mark.parametrize(
    "path",
    ["../secret.py", "/absolute.py", "vendor/x.py", "src/generated_pb2.py"],
)
def test_candidate_paths_reject_unsafe_or_generated_files(path: str) -> None:
    with pytest.raises(ValueError):
        validate_candidate_path(path)


def test_canonical_id_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_id("Recipe", {"weight": float("nan")})
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_models.py -q
```

Expected: collection fails because `code_atlas` does not exist.

- [ ] **Step 3: Implement immutable public contracts**

`models.py` must define:

```python
class NodeKind(str, Enum):
    TASK_EPISODE = "TaskEpisode"
    INTENT = "Intent"
    RECIPE = "Recipe"
    CODE_TEMPLATE = "CodeTemplate"
    ADAPTATION_SLOT = "AdaptationSlot"
    CONSTRAINT = "Constraint"
    DEPENDENCY = "Dependency"
    TEST_SPEC = "TestSpec"
    EXECUTION_RECEIPT = "ExecutionReceipt"
    SOURCE_EVIDENCE = "SourceEvidence"
    LANGUAGE = "Language"
    FRAMEWORK = "Framework"


class EdgeRelation(str, Enum):
    SOLVES = "SOLVES"
    DERIVED_FROM = "DERIVED_FROM"
    HAS_IMPLEMENTATION = "HAS_IMPLEMENTATION"
    HAS_SLOT = "HAS_SLOT"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    REQUIRES = "REQUIRES"
    VERIFIED_BY = "VERIFIED_BY"
    CHANGES = "CHANGES"
    TESTS = "TESTS"
    SUPERSEDES = "SUPERSEDES"
    BUNDLED_AS = "BUNDLED_AS"


class AtlasStatus(str, Enum):
    READY = "READY"
    NO_VERIFIED_RECIPE = "NO_VERIFIED_RECIPE"
    INDEX_STALE = "INDEX_STALE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    RENDER_INVALID = "RENDER_INVALID"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    RECIPE_QUARANTINED = "RECIPE_QUARANTINED"
    INGEST_PENDING = "INGEST_PENDING"
    ATLAS_UNAVAILABLE = "ATLAS_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
```

Also define `AtlasError` with a stable `code`, plus frozen `AtlasNode`,
`AtlasEdge`, `SlotSpec`, `ConstraintSpec`, `DependencySpec`, `TestSpec`,
`TemplateOperation`, `RecipeManifest`, `GraphQueryResult`,
`ImplementationPacket`, `PreparationResult`, `RenderResult`, `ExtractionGap`,
`ExtractionResult`, `IngestionReceipt`, and `AcceptanceProjection`. Store
JSON-like values as canonical dictionaries/lists at serialization boundaries
and immutable tuples inside records.

The edge constructor must validate the endpoint matrix from the design, not
only the relation name. `canonical.py` must expose `canonical_json`,
`canonical_hash`, `canonical_id`, and `normalize_intent_id`; `security.py` must
expose the locked constants plus `validate_candidate_path`,
`validate_fragment`, and `validate_slot_value`.

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_models.py -q
```

Expected: all tests pass with stable ids across two process invocations.

- [ ] **Step 5: Commit**

```powershell
git add mcp-tools/code_atlas mcp-tools/tests/test_code_atlas_models.py
git commit -m "feat: define deterministic Code Atlas contracts"
```

### Task 3: Local graph store, migrations, CAS, and bounded graph query

**Files:**

- Create: `mcp-tools/code_atlas/store.py`
- Test: `mcp-tools/tests/test_code_atlas_store.py`

- [ ] **Step 1: Write the failing store tests**

Cover schema version 1, WAL, foreign keys, idempotent node/edge insertion,
payload conflict rejection, CAS hash verification, packet receipts, ingestion
receipt idempotency, and depth/node/edge/byte budgets. Include:

```python
from dataclasses import replace
from pathlib import Path

import pytest

from code_atlas.models import AtlasEdge, AtlasNode, EdgeRelation, NodeKind
from code_atlas.store import AtlasStore, StoreConflictError


def test_store_is_idempotent_and_rejects_same_id_with_other_payload(
    tmp_path: Path,
) -> None:
    store = AtlasStore(tmp_path / "code-atlas.sqlite3", tmp_path / "cas")
    node = AtlasNode.create(NodeKind.INTENT, {"intent_id": "python.guard"})
    assert store.put_nodes((node,)) == (node,)
    assert store.put_nodes((node,)) == (node,)
    conflicting = replace(node, payload=(("intent_id", "other"),))
    with pytest.raises(StoreConflictError):
        store.put_nodes((conflicting,))
    store.close()


def test_cas_rejects_hash_mismatch(tmp_path: Path) -> None:
    store = AtlasStore(tmp_path / "code-atlas.sqlite3", tmp_path / "cas")
    with pytest.raises(StoreConflictError, match="blob hash"):
        store.put_blob("sha256:" + "0" * 64, b"verified template")
    store.close()


def test_graph_query_is_budgeted_and_stably_ordered(tmp_path: Path) -> None:
    store = AtlasStore(tmp_path / "code-atlas.sqlite3", tmp_path / "cas")
    intent = AtlasNode.create(NodeKind.INTENT, {"intent_id": "python.guard"})
    recipes = tuple(
        AtlasNode.create(NodeKind.RECIPE, {"name": name})
        for name in ("b", "a", "c")
    )
    store.put_nodes((intent, *recipes))
    store.put_edges(
        tuple(AtlasEdge.create(EdgeRelation.SOLVES, recipe, intent) for recipe in recipes)
    )
    result = store.graph_query(
        root_node_ids=(intent.node_id,),
        max_nodes=2,
        max_edges=1,
        max_depth=1,
        byte_budget=65_536,
    )
    assert tuple(node.node_id for node in result.nodes) == tuple(
        sorted(node.node_id for node in result.nodes)
    )
    assert len(result.nodes) == 2
    assert len(result.edges) == 1
    assert result.truncated is True
    store.close()
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_store.py -q
```

Expected: collection fails because `code_atlas.store` does not exist.

- [ ] **Step 3: Implement schema and transactions**

Create these tables in one `BEGIN IMMEDIATE` migration:

```sql
CREATE TABLE atlas_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE atlas_nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    extractor_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    provenance TEXT NOT NULL,
    source_hashes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    quarantine_state TEXT NOT NULL
);
CREATE TABLE atlas_edges (
    edge_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES atlas_nodes(node_id),
    target_id TEXT NOT NULL REFERENCES atlas_nodes(node_id),
    relation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX atlas_edges_source ON atlas_edges(source_id, relation);
CREATE INDEX atlas_edges_target ON atlas_edges(target_id, relation);
CREATE TABLE atlas_recipes (
    recipe_id TEXT PRIMARY KEY REFERENCES atlas_nodes(node_id),
    intent_id TEXT NOT NULL,
    language TEXT NOT NULL,
    framework TEXT NOT NULL,
    layer TEXT NOT NULL CHECK (layer IN ('bundled', 'local')),
    version INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    repository_signature TEXT NOT NULL,
    state TEXT NOT NULL,
    supersedes_recipe_id TEXT
);
CREATE INDEX atlas_recipe_match
    ON atlas_recipes(intent_id, language, framework, state);
CREATE TABLE atlas_recipe_nodes (
    recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id),
    node_id TEXT NOT NULL REFERENCES atlas_nodes(node_id),
    PRIMARY KEY (recipe_id, node_id)
);
CREATE TABLE atlas_recipe_edges (
    recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id),
    edge_id TEXT NOT NULL REFERENCES atlas_edges(edge_id),
    PRIMARY KEY (recipe_id, edge_id)
);
CREATE TABLE atlas_blobs (
    blob_hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL
);
CREATE TABLE atlas_packet_receipts (
    packet_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    packet_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE atlas_ingestion_receipts (
    ingestion_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

`AtlasStore` must expose `put_nodes`, `put_edges`, `put_recipe`, `put_blob`,
`read_blob`, `put_packet`, `get_packet`, `put_ingestion_receipt`,
`get_ingestion_receipt`, `recipes_for_intent`, `graph_query`, `schema_version`,
`journal_mode`, `foreign_keys_enabled`, and `close`.

CAS paths are computed as
`cas_root / "sha256" / digest[:2] / digest[2:]`. Write to a sibling temporary
file, flush, `os.fsync`, atomically replace, then re-read and verify the digest.
Never resolve a caller-provided CAS path.

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_models.py `
  mcp-tools/tests/test_code_atlas_store.py -q
```

Expected: all tests pass; reopening the database preserves graph and receipt
ids.

- [ ] **Step 5: Commit**

```powershell
git add mcp-tools/code_atlas/store.py mcp-tools/tests/test_code_atlas_store.py
git commit -m "feat: persist local Code Atlas graph"
```

### Task 4: Bundled manifests, three seed recipes, and Pattern Card projection

**Files:**

- Create: `mcp-tools/code_atlas/recipes.py`
- Create: `skills/code-atlas/assets/recipes/python-fastmcp-read-tool.json`
- Create: `skills/code-atlas/assets/recipes/python-validation-guard.json`
- Create: `skills/code-atlas/assets/recipes/python-pytest-regression.json`
- Create: `skills/code-atlas/assets/templates/sha256/c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950`
- Create: `skills/code-atlas/assets/templates/sha256/47820213e5b67e968dff12d9509b8990d7aa1a6465a85b631599628589f8d8de`
- Create: `skills/code-atlas/assets/templates/sha256/d07c7a977b330d26fe6486ab5e07f02855117f092ad19b0b08b2aafb136aef6c`
- Create: `skills/code-atlas/scripts/validate_recipes.py`
- Test: `mcp-tools/tests/test_code_atlas_recipes.py`

- [ ] **Step 1: Write failing manifest and hash tests**

```python
from pathlib import Path

from code_atlas.recipes import BundledRecipeLoader, render_pattern_card


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "skills" / "code-atlas" / "assets"


def test_three_seed_recipes_load_with_verified_blobs() -> None:
    recipes = BundledRecipeLoader(ASSETS).load()
    assert tuple(recipe.intent_id for recipe in recipes) == (
        "python.fastmcp.read-only-tool",
        "python.pytest-regression",
        "python.validation-guard",
    )
    assert all(recipe.layer == "bundled" for recipe in recipes)
    assert len({recipe.recipe_id for recipe in recipes}) == 3


def test_pattern_card_is_a_deterministic_view() -> None:
    recipe = BundledRecipeLoader(ASSETS).load()[0]
    first = render_pattern_card(recipe)
    second = render_pattern_card(recipe)
    assert first == second
    assert recipe.recipe_id in first
    assert recipe.intent_id in first
    assert "## Slots" in first
    assert "## Verification" in first


def test_asset_validator_succeeds_without_writing_assets() -> None:
    before = {
        path.relative_to(ASSETS).as_posix(): path.read_bytes()
        for path in ASSETS.rglob("*")
        if path.is_file()
    }
    assert BundledRecipeLoader(ASSETS).validate() == ()
    after = {
        path.relative_to(ASSETS).as_posix(): path.read_bytes()
        for path in ASSETS.rglob("*")
        if path.is_file()
    }
    assert after == before
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_recipes.py -q
```

Expected: collection fails because `code_atlas.recipes` and the assets are
absent.

- [ ] **Step 3: Add exact content-addressed template blobs**

The MCP wrapper blob is exactly:

```python
@mcp.tool(annotations=_READ_ONLY)
def ${tool_name}(${parameter_name}: str) -> dict[str, Any]:
    """${docstring}"""

    def operation() -> Any:
        return ${service_expression}

    return _safe_call(operation)
```

It ends in one LF and hashes to
`sha256:c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950`.

The validation-guard blob is exactly:

```python
if not (${predicate_expression}):
    raise ${exception_expression}
```

It ends in one LF and hashes to
`sha256:47820213e5b67e968dff12d9509b8990d7aa1a6465a85b631599628589f8d8de`.

The pytest blob is exactly:

```python
def ${test_name}() -> None:
    ${test_body}
```

It ends in one LF and hashes to
`sha256:d07c7a977b330d26fe6486ab5e07f02855117f092ad19b0b08b2aafb136aef6c`.

- [ ] **Step 4: Add canonical manifests and loader**

All manifests use this complete top-level shape:

```json
{
  "schema_version": "1",
  "recipe_key": "python.fastmcp.read-only-tool",
  "version": 1,
  "intent_id": "python.fastmcp.read-only-tool",
  "language": {"name": "python", "extractor_version": "1"},
  "framework": {"name": "mcp-python-sdk", "specifier": ">=1,<2"},
  "repository_signature": "",
  "slots": [
    {"name": "server_path", "type": "relative_python_path", "required": true},
    {"name": "tool_name", "type": "python_identifier", "required": true},
    {"name": "parameter_name", "type": "python_identifier", "required": true},
    {"name": "docstring", "type": "single_line_text", "required": true},
    {"name": "service_expression", "type": "python_expression", "required": true},
    {"name": "test_path", "type": "relative_python_path", "required": true}
  ],
  "constraints": [
    {"kind": "path_suffix", "subject": "server_path", "value": ".py"},
    {"kind": "required_symbol", "subject": "server_path", "value": "_READ_ONLY"},
    {"kind": "required_symbol", "subject": "server_path", "value": "_safe_call"}
  ],
  "dependencies": [
    {"name": "mcp", "kind": "python-package", "specifier": ">=1,<2"}
  ],
  "tests": [
    {
      "argv": ["python", "-m", "pytest", "${test_path}", "-q"],
      "expected_exit_code": 0
    }
  ],
  "operations": [
    {
      "kind": "append_python_nodes",
      "path_slot": "server_path",
      "template_hash": "sha256:c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950",
      "separator": "\n\n"
    }
  ],
  "provenance": {"kind": "bundled", "source": "2718lab-devkit"}
}
```

The validation-guard manifest uses intent
`python.validation-guard`, no framework requirement, slots
`source_path:relative_python_path`,
`target_symbol:python_qualified_name`,
`predicate_expression:python_expression`,
`exception_expression:python_expression`, and
`test_path:relative_python_path`. Its single operation is
`prepend_function_body` on `target_symbol`, with the validation-guard hash.

The pytest manifest uses intent `python.pytest-regression`, framework `pytest`
with specifier `>=7,<9`, slots `test_path:relative_python_path`,
`test_name:python_identifier`, and `test_body:python_statement_block`. Its
single operation is `append_python_nodes`, with the pytest hash and separator
`\n\n`.

`BundledRecipeLoader` must reject unknown keys, missing slots, duplicate slot
names, undeclared template placeholders, hash mismatch, unsupported operation
kinds, non-canonical intent ids, and absolute/path-escaping asset references.
It returns recipes sorted by `(intent_id, recipe_id)`.

`render_pattern_card` is a pure Markdown projection. It must not be parsed back
to build graph facts. The loader materializes every manifest as typed recipe,
intent, language, optional framework, template, slot, constraint, dependency,
test, and bundled `SourceEvidence` nodes plus the exact design relations,
including `BUNDLED_AS`; bundled and local recipes therefore enter one query and
packet protocol.

- [ ] **Step 5: Implement and run the standalone validator**

`validate_recipes.py` imports the loader from `PLUGIN_ROOT / "mcp-tools"`, prints
only `Code Atlas recipes valid: 3`, and exits nonzero on any validation error.

Run:

```powershell
python skills/code-atlas/scripts/validate_recipes.py
python -m pytest mcp-tools/tests/test_code_atlas_recipes.py -q
```

Expected: the validator reports exactly three valid recipes and all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add mcp-tools/code_atlas/recipes.py `
  mcp-tools/tests/test_code_atlas_recipes.py `
  skills/code-atlas/assets `
  skills/code-atlas/scripts/validate_recipes.py
git commit -m "feat: bundle verified Code Atlas seeds"
```

### Task 5: Snapshot facts and task-owned checkpoint readers

**Files:**

- Modify: `mcp-tools/project_index/models.py:72-157`
- Modify: `mcp-tools/project_index/service.py:371-431`
- Modify: `mcp-tools/project_index/checkpoints.py:64-162`
- Modify: `mcp-tools/tests/test_project_index_core.py`
- Modify: `mcp-tools/tests/test_project_index_checkpoints.py`

- [ ] **Step 1: Add failing read-contract tests**

Add tests proving that snapshot reads are current/hash-verified and checkpoint
reads are task-bound:

```python
def test_snapshot_facts_and_files_are_hash_verified(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = workspace / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    service = ProjectIndexService(tmp_path / "index.sqlite3")
    snapshot = service.sync(workspace)
    facts = service.snapshot_facts(workspace, snapshot.snapshot_id)
    files = service.read_snapshot_files(
        workspace,
        snapshot.snapshot_id,
        ("module.py",),
        byte_budget=1024,
    )
    assert facts.snapshot == snapshot
    assert facts.file_hashes == (("module.py", files[0].content_hash),)
    assert files[0].body == b"VALUE = 1\n"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(IndexError) as captured:
        service.read_snapshot_files(
            workspace, snapshot.snapshot_id, ("module.py",), byte_budget=1024
        )
    assert captured.value.code == "INDEX_STALE"
    service.close()
```

Checkpoint coverage must create a real isolated Git worktree using the existing
fixture helper, then assert:

```python
files = checkpoints.read_files_for_task(
    checkpoint.checkpoint_id,
    workflow_id=checkpoint.workflow_id,
    task_id=checkpoint.task_id,
    paths=("src/module.py",),
    byte_budget=4096,
)
assert files[0].body == b"VALUE = 1\n"
with pytest.raises(IndexError) as captured:
    checkpoints.read_files_for_task(
        checkpoint.checkpoint_id,
        workflow_id=checkpoint.workflow_id,
        task_id="other-task",
        paths=("src/module.py",),
        byte_budget=4096,
    )
assert captured.value.code == "WORKTREE_UNOWNED"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_project_index_core.py `
  mcp-tools/tests/test_project_index_checkpoints.py -q
```

Expected: the new calls fail with missing
`snapshot_facts`, `read_snapshot_files`, and `read_files_for_task`.

- [ ] **Step 3: Add immutable internal read records**

Add to `project_index/models.py`:

```python
@dataclass(frozen=True)
class SnapshotFacts:
    snapshot: IndexSnapshot
    file_hashes: tuple[tuple[str, str], ...]
    nodes: tuple[IndexNode, ...]
    edges: tuple[IndexEdge, ...]
    gaps: tuple[CoverageGap, ...]


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content_hash: str
    body: bytes
```

Add to `checkpoints.py`:

```python
@dataclass(frozen=True)
class CheckpointFile:
    path: str
    content_hash: str
    body: bytes
```

`ProjectIndexService.snapshot_facts` verifies workspace ownership of the
snapshot and returns sorted tuples from the existing store. It does not expose
the store object.

`ProjectIndexService.read_snapshot_files` calls `assert_current`, normalizes
every requested path through existing safe-path logic, enforces the byte
budget, rejects links/reparse points, reads a regular file once, and compares
its SHA-256 to the snapshot manifest before returning bytes.

`CheckpointService.read_files_for_task` loads and verifies the checkpoint and
CAS, checks workflow/task equality, limits paths to the checkpoint write scope,
enforces the byte budget, and returns only regular file entries. It does not
restore or write any file and does not require an expired worker lease.

- [ ] **Step 4: Run index/checkpoint regression tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_project_index_core.py `
  mcp-tools/tests/test_project_index_checkpoints.py `
  mcp-tools/tests/test_project_index_workflow.py -q
```

Expected: all tests pass; no source body is added to
`project-index.sqlite3`.

- [ ] **Step 5: Commit**

```powershell
git add mcp-tools/project_index mcp-tools/tests/test_project_index_core.py `
  mcp-tools/tests/test_project_index_checkpoints.py
git commit -m "feat: expose verified snapshot inputs to Code Atlas"
```

### Task 6: Language-neutral extractor and deterministic Python reuse gate

**Files:**

- Create: `mcp-tools/code_atlas/extractors.py`
- Test: `mcp-tools/tests/test_code_atlas_extractor.py`

- [ ] **Step 1: Write the failing extraction matrix**

Build fixtures from explicit `CheckpointFile`, `SnapshotFile`, index nodes/gaps,
and successful execution receipts. The primary test must assert:

```python
def test_python_append_shape_round_trips_and_is_repeatable() -> None:
    request = extraction_request(
        intent_id="python.validation-helper",
        before={
            "src/guards.py": b"VALUE = 1\n",
            "tests/test_guards.py": b"from src.guards import VALUE\n",
        },
        after={
            "src/guards.py": (
                b"VALUE = 1\n\n"
                b"def is_valid(value: int) -> bool:\n"
                b"    return value > 0\n"
            ),
            "tests/test_guards.py": (
                b"from src.guards import VALUE\n\n"
                b"def test_is_valid() -> None:\n"
                b"    assert VALUE == 1\n"
            ),
        },
    )
    extractor = PythonRecipeExtractor()
    first = extractor.extract(request)
    second = extractor.extract(request)
    assert first == second
    assert first.eligible is True
    assert first.gaps == ()
    assert tuple(operation.kind for operation in first.manifest.operations) == (
        "append_python_nodes",
        "append_python_nodes",
    )
    rendered = render_operations(
        first.manifest,
        first.original_bindings,
        request.before_files,
    )
    assert rendered == request.after_files
```

Parametrize episode-only reasons:

```python
@pytest.mark.parametrize(
    ("fixture_name", "reason"),
    [
        ("rename-only", "UNSUPPORTED_EDIT_SHAPE"),
        ("in-place-replacement", "UNSUPPORTED_EDIT_SHAPE"),
        ("documentation-only", "UNSUPPORTED_LANGUAGE"),
        ("generated-file", "GENERATED_PATH"),
        ("vendor-file", "VENDORED_PATH"),
        ("binary-file", "BINARY_CONTENT"),
        ("secret-fragment", "SECRET_BEARING"),
        ("oversize-fragment", "SIZE_LIMIT"),
        ("parser-gap", "PARSER_GAP"),
        ("failed-test-receipt", "VERIFICATION_FAILED"),
        ("snapshot-mismatch", "SNAPSHOT_MISMATCH"),
    ],
)
def test_unsupported_inputs_are_episode_only(
    fixture_name: str, reason: str
) -> None:
    result = PythonRecipeExtractor().extract(load_fixture(fixture_name))
    assert result.eligible is False
    assert reason in tuple(gap.code for gap in result.gaps)
    assert result.manifest is None
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_extractor.py -q
```

Expected: collection fails because `code_atlas.extractors` does not exist.

- [ ] **Step 3: Implement the extractor protocol**

Define:

```python
class RecipeExtractor(Protocol):
    extractor_id: str
    extractor_version: str
    languages: frozenset[str]

    @abstractmethod
    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        raise NotImplementedError
```

Import `abstractmethod` from `abc`. The concrete
`PythonRecipeExtractor.extract` owns all behavior. `ExtractionRequest` contains
workflow/task/acceptance ids, normalized intent, workspace hash, input/output
snapshot ids, write scope, before/after files, changed index nodes, coverage
gaps, and bound execution receipts.

For each path, the Python extractor must:

1. Validate scope/path/type/size/secret rules.
2. Parse accepted output with `ast.parse`.
3. For an added file, emit `create_python_file`.
4. For a changed file, require `after.startswith(before)`, parse only the
   suffix, and require every suffix statement to be a top-level function,
   async function, or class before emitting `append_python_nodes`.
5. Derive path and new-symbol slots in stable path/span/name order.
6. Build the typed episode graph unconditionally.
7. Build a recipe graph only when every gate passes.
8. Render with original bindings and compare every accepted output hash.
9. Repeat extraction and compare the canonical manifest hash before returning
   an eligible result.

Do not call `ast.unparse` for stored templates; keep exact accepted bytes so the
round trip is byte-for-byte.

- [ ] **Step 4: Run extractor and security tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_models.py `
  mcp-tools/tests/test_code_atlas_extractor.py -q
```

Expected: all extraction shapes and stable episode-only reasons pass.

- [ ] **Step 5: Commit**

```powershell
git add mcp-tools/code_atlas/extractors.py `
  mcp-tools/tests/test_code_atlas_extractor.py
git commit -m "feat: extract round-trip Python recipes"
```

### Task 7: Deterministic matcher, graph query, and implementation packets

**Files:**

- Create: `mcp-tools/code_atlas/matching.py`
- Create: `mcp-tools/code_atlas/service.py`
- Test: `mcp-tools/tests/test_code_atlas_service.py`

- [ ] **Step 1: Write failing matching and packet tests**

Use a real `ProjectIndexService`, temporary `AtlasStore`, and bundled loader.
Cover no match, one match, equal-best ambiguity, supersession, quarantine,
framework mismatch, repository-local priority, stale index, and repeated packet
identity. Include:

```python
def test_prepare_returns_same_packet_for_same_snapshot_and_intent(
    atlas_service, fixture_workspace
) -> None:
    snapshot = fixture_workspace.sync()
    first = atlas_service.prepare(
        workspace=str(fixture_workspace.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_feature.py",),
        target_symbols=(),
        max_candidates=20,
        byte_budget=131_072,
    )
    second = atlas_service.prepare(
        workspace=str(fixture_workspace.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_feature.py",),
        target_symbols=(),
        max_candidates=20,
        byte_budget=131_072,
    )
    assert first.status.value == "READY"
    assert first == second
    assert first.packet.packet_id == second.packet.packet_id
    assert first.packet.snapshot_id == snapshot.snapshot_id


def test_equal_best_candidates_are_ambiguous(atlas_service, fixture_workspace) -> None:
    snapshot = fixture_workspace.sync()
    add_two_active_local_recipes(
        atlas_service.store,
        intent_id="python.custom-guard",
        repository_signature=fixture_workspace.signature,
    )
    result = atlas_service.prepare(
        workspace=str(fixture_workspace.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.custom-guard",
        language="python",
        target_paths=("src/guards.py",),
    )
    assert result.status.value == "AMBIGUOUS_MATCH"
    assert result.packet is None
    assert tuple(candidate.recipe_id for candidate in result.candidates) == tuple(
        sorted(candidate.recipe_id for candidate in result.candidates)
    )


def test_stale_snapshot_never_produces_a_packet(
    atlas_service, fixture_workspace
) -> None:
    snapshot = fixture_workspace.sync()
    fixture_workspace.write("tests/test_feature.py", "changed = True\n")
    result = atlas_service.prepare(
        workspace=str(fixture_workspace.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert result.status.value == "INDEX_STALE"
    assert result.packet is None
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_service.py -q
```

Expected: collection fails because matching/service modules do not exist.

- [ ] **Step 3: Implement discrete matching**

`matching.py` defines `MatchClass(IntEnum)` with
`EXACT_REPOSITORY=0`, `EXACT_FRAMEWORK=1`, and `LANGUAGE_GENERIC=2`.
It also defines frozen `MatchCandidate` and `MatchResult` records and exposes:

```python
def select_recipe(
    candidates: Sequence[RecipeManifest],
    *,
    intent_id: str,
    language: str,
    framework: str,
    repository_signature: str,
    snapshot_facts: SnapshotFacts,
) -> MatchResult:
    """Return a unique class winner or a stable non-ready result."""
```

Filtering is exact and discrete:

- `intent_id` and language must match exactly after normalization.
- A declared framework must match exactly; the v1 specifier parser supports
  only comma-separated `>`, `>=`, `<`, `<=`, `==`, and `!=` numeric clauses.
- Every required node kind/symbol/import/path suffix must exist in current
  snapshot facts.
- Any forbidden gap, quarantine, or unsatisfied constraint excludes readiness.
- A `SUPERSEDES` chain removes only the explicitly superseded recipe.
- Layer alone never breaks an equal-class tie.

- [ ] **Step 4: Implement `CodeAtlasService.graph_query` and `prepare`**

The service constructor receives an `AtlasStore`, `BundledRecipeLoader`, and
`ProjectIndexService`. It materializes bundled manifests into in-memory typed
nodes/edges, then combines those immutable records with local store queries.

`graph_query` validates the locked graph budgets, follows only requested
relations from requested roots, includes no template body unless the byte budget
admits it, and returns ids in stable order.

`prepare` must:

1. Normalize inputs and call `ProjectIndexService.assert_current` for target
   paths.
2. Load `SnapshotFacts`; derive the repository signature from immutable
   language/framework/node/import facts, not an absolute workspace path.
3. Select a unique recipe with `select_recipe`.
4. Obtain evidence windows through the existing project-index query path.
5. Build an `ImplementationPacket` containing the recipe subgraph, current
   evidence windows/hashes, template operations, slot specs, constraints,
   dependencies, test specs, gaps, and `next_action`.
6. Compute `trace_id` and `packet_id` from canonical immutable inputs.
7. Store the packet receipt and return the same receipt on repetition.

Map project-index `INDEX_STALE` to an Atlas result with status `INDEX_STALE`;
do not hide it in a generic exception. An unsupported extractor language maps
to `UNSUPPORTED_LANGUAGE`. No match and ambiguity return no packet.

- [ ] **Step 5: Run service tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_store.py `
  mcp-tools/tests/test_code_atlas_recipes.py `
  mcp-tools/tests/test_code_atlas_service.py -q
```

Expected: all tests pass and repeated calls return identical trace, packet,
node, edge, and hash values.

- [ ] **Step 6: Commit**

```powershell
git add mcp-tools/code_atlas/matching.py `
  mcp-tools/code_atlas/service.py `
  mcp-tools/tests/test_code_atlas_service.py
git commit -m "feat: prepare deterministic implementation packets"
```

### Task 8: Slot validation, deterministic patch rendering, and promotion export

**Files:**

- Create: `mcp-tools/code_atlas/rendering.py`
- Create: `skills/code-atlas/scripts/export_recipe.py`
- Modify: `mcp-tools/code_atlas/service.py`
- Modify: `mcp-tools/tests/test_code_atlas_service.py`
- Test: `mcp-tools/tests/test_code_atlas_promotion.py`

- [ ] **Step 1: Add failing render tests**

```python
def test_render_returns_same_patch_without_writing_workspace(
    atlas_service, fixture_workspace
) -> None:
    snapshot = fixture_workspace.sync()
    prepared = atlas_service.prepare(
        workspace=str(fixture_workspace.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_feature.py",),
    )
    before = fixture_workspace.read_bytes("tests/test_feature.py")
    bindings = {
        "test_path": "tests/test_feature.py",
        "test_name": "test_feature",
        "test_body": "assert True",
    }
    first = atlas_service.render(
        str(fixture_workspace.root),
        snapshot.snapshot_id,
        prepared.packet.packet_id,
        bindings,
    )
    second = atlas_service.render(
        str(fixture_workspace.root),
        snapshot.snapshot_id,
        prepared.packet.packet_id,
        bindings,
    )
    assert first.status.value == "READY"
    assert first == second
    assert first.patch_candidate.startswith("--- a/tests/test_feature.py\n")
    assert fixture_workspace.read_bytes("tests/test_feature.py") == before


@pytest.mark.parametrize(
    ("bindings", "reason"),
    [
        ({"test_path": "../escape.py", "test_name": "test_ok", "test_body": "pass"}, "path"),
        ({"test_path": "tests/test_x.py", "test_name": "not valid", "test_body": "pass"}, "identifier"),
        ({"test_path": "tests/test_x.py", "test_name": "test_ok", "test_body": "return"}, "statement"),
    ],
)
def test_render_rejects_invalid_bindings(
    atlas_service, ready_packet, bindings, reason
) -> None:
    result = atlas_service.render(
        ready_packet.workspace,
        ready_packet.snapshot_id,
        ready_packet.packet_id,
        bindings,
    )
    assert result.status.value == "RENDER_INVALID"
    assert reason in result.reasons[0].casefold()
    assert result.patch_candidate == ""
```

Also test a changed source hash, stale packet, undeclared/absent slot, unmet
constraint, path escape, link/reparse point, template hash mismatch, and
multi-file patch ordering.

- [ ] **Step 2: Run the tests and verify failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_service.py -q
```

Expected: render tests fail because the renderer is absent.

- [ ] **Step 3: Implement the renderer**

`rendering.py` supports only `create_python_file`, `append_python_nodes`, and
`prepend_function_body`. It must:

- validate every required slot exactly once and reject unknown bindings;
- parse `python_identifier`, `python_qualified_name`, `python_expression`, and
  `python_statement_block` with `ast`;
- validate relative paths with `security.py`;
- replace only the exact `${slot_name}` token grammar;
- reparse every resulting Python file;
- apply edits to in-memory bytes only;
- generate LF-only `difflib.unified_diff` output with `a/{relative_path}` and
  `b/{relative_path}`
  headers and no timestamps;
- sort file patches by normalized path;
- return exact test command specs after safe slot substitution;
- hash the patch candidate and bindings canonically.

For `prepend_function_body`, locate one exact qualified function/method in the
packet-bound source. Insert after its docstring when one exists, preserve the
body indentation detected from source spans, and reject zero or multiple
matches.

Before rendering, `CodeAtlasService.render` reloads the stored packet,
re-validates its recipe/template hashes, calls project-index freshness checks,
and verifies every packet-bound source hash. It returns `RENDER_INVALID` with
stable reasons on any mismatch.

- [ ] **Step 4: Add deterministic promotion export**

`export_recipe.py` accepts:

```text
--data-root DURABLE_ROOT
--recipe-id SHA256_RECIPE_ID
--output NEW_OUTPUT_DIRECTORY
```

It opens `code-atlas.sqlite3` read-only, validates the selected local recipe and
its CAS blobs, rejects output inside the durable data root, plugin source, or a
path containing `plugins/cache`, and writes:

```text
NEW_OUTPUT_DIRECTORY/
  manifest.json
  pattern-card.md
  templates/sha256/{digest}
  promotion-receipt.json
```

The promotion receipt hashes the other files and contains no absolute workspace
path. Running twice into two empty output directories must produce identical
file bytes.

- [ ] **Step 5: Run render and promotion tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_service.py `
  mcp-tools/tests/test_code_atlas_promotion.py -q
```

Expected: all tests pass; fixture workspaces and plugin assets remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add mcp-tools/code_atlas/rendering.py `
  mcp-tools/code_atlas/service.py `
  mcp-tools/tests/test_code_atlas_service.py `
  mcp-tools/tests/test_code_atlas_promotion.py `
  skills/code-atlas/scripts/export_recipe.py
git commit -m "feat: render verified Code Atlas patches"
```

### Task 9: Bounded direct/Code-exec execution receipts

**Files:**

- Create: `mcp-tools/code_atlas/receipts.py`
- Create: `hooks/execution_receipt.py`
- Modify: `hooks/hooks.json`
- Test: `mcp-tools/tests/test_code_atlas_receipts.py`
- Modify: `mcp-tools/tests/test_bugkiller_metadata.py`

- [ ] **Step 1: Write failing host-payload tests**

Use sanitized fixtures representing:

- Claude `Bash`, `Edit`, and `Write`;
- Codex `shell_command` and `apply_patch`;
- a Code/exec wrapper containing nested shell and patch results;
- successful and failed exits;
- a command and output containing fake credentials;
- absent plugin trust/context and malformed payload.

The core assertions are:

```python
def test_direct_and_nested_payloads_normalize_to_equivalent_receipts(
    tmp_path: Path,
) -> None:
    direct = normalize_post_tool_use(load_fixture("codex-direct.json"))
    nested = normalize_post_tool_use(load_fixture("codex-code-nested.json"))
    assert tuple(item.semantic_projection() for item in direct) == tuple(
        item.semantic_projection() for item in nested
    )


def test_receipt_repository_stores_hashes_not_raw_output(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    receipt = repository.capture(load_fixture("claude-bash-secret.json"))[0]
    stored = repository.read(receipt.receipt_id)
    body = canonical_json(stored)
    assert "sk-fixture-secret" not in body
    assert "complete command output" not in body
    assert stored.output_hash.startswith("sha256:")
    assert stored.command_spec_hash.startswith("sha256:")


def test_malformed_hook_payload_fails_open_without_receipt(tmp_path: Path) -> None:
    repository = ReceiptRepository(tmp_path)
    assert repository.capture({"unexpected": object()}) == ()
    assert tuple(tmp_path.rglob("*.json")) == ()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_receipts.py -q
```

Expected: collection fails because receipt code and hook fixtures are absent.

- [ ] **Step 3: Implement bounded receipt normalization**

`RawExecutionReceipt` stores:

```python
@dataclass(frozen=True)
class RawExecutionReceipt:
    receipt_id: str
    schema_version: str
    host: str
    session_id_hash: str
    turn_id_hash: str
    tool_use_id: str
    parent_tool_use_id: str
    canonical_tool: str
    command_spec: tuple[str, ...]
    command_spec_hash: str
    input_hash: str
    exit_code: int
    success: bool
    output_hash: str
    workspace_hash: str
    observed_at: str
```

Hash session/turn/workspace values before storage. Preserve only a redacted,
bounded structured command spec; never store arbitrary environment values or
full stdout/stderr. For patch tools, store an empty command spec and the patch
input hash. Receipt ids exclude `observed_at` so equivalent replays deduplicate.

`ReceiptRepository` stores immutable canonical JSON at
`DATA_ROOT / "code-atlas-receipts" / "sha256" / f"{digest}.json"`, verifies it on every
read, and rejects a same-id payload conflict.

- [ ] **Step 4: Implement the fail-open hook**

`hooks/execution_receipt.py`:

1. reads one JSON payload from stdin;
2. resolves the durable root with the same
   `BUGKILLER_HOME -> PLUGIN_DATA -> CODEX_HOME/bugkiller` priority;
3. normalizes direct and nested supported tool results;
4. writes only bounded immutable receipt files;
5. emits no stdout on success or failure;
6. always exits zero.

Keep the existing metadata hook entry and add a second `PostToolUse` entry:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/metadata_guard.py\"",
            "timeout": 10
          }
        ],
        "matcher": "Edit|Write"
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/execution_receipt.py\"",
            "timeout": 10
          }
        ],
        "matcher": "Bash|Edit|Write|shell_command|apply_patch|Code|exec"
      }
    ]
  }
}
```

Hook absence or an untrusted hook produces no receipt. It never synthesizes
success and therefore causes later acceptance to return
`EVIDENCE_INCOMPLETE`.

- [ ] **Step 5: Run receipt and legacy hook tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_receipts.py `
  mcp-tools/tests/test_bugkiller_metadata.py -q
```

Expected: all tests pass, both hook entries parse, and no raw fake secret or
command output appears in stored receipt JSON.

- [ ] **Step 6: Commit**

```powershell
git add mcp-tools/code_atlas/receipts.py hooks `
  mcp-tools/tests/test_code_atlas_receipts.py `
  mcp-tools/tests/test_bugkiller_metadata.py
git commit -m "feat: capture bounded execution receipts"
```

### Task 10: Typed code tasks, automatic acceptance, outbox, and projection

**Files:**

- Modify: `mcp-tools/orchestrator/models.py:9-58`
- Modify: `mcp-tools/orchestrator/store.py:168-267,1573-1797`
- Modify: `mcp-tools/orchestrator/service.py:28-57,157-234`
- Modify: `mcp-tools/code_atlas/service.py`
- Test: `mcp-tools/tests/test_code_atlas_acceptance.py`
- Modify: `mcp-tools/tests/test_project_index_workflow.py`
- Modify: `mcp-tools/tests/test_orchestrator_store_service_api.py`

- [ ] **Step 1: Write failing schema/authorization/idempotency tests**

Add `TaskKind` coverage without breaking legacy default tasks:

```python
def test_legacy_task_defaults_to_general_kind(store) -> None:
    task = store.register_task(Task("legacy", "wf", "legacy", "worker"))
    reopened = store.get_task(task.id)
    assert reopened.task_kind is TaskKind.GENERAL
    assert reopened.intent_id == ""
    assert reopened.language == ""
```

The acceptance suite must use real orchestrator/index/checkpoint/receipt
services and cover:

```python
def test_acceptance_and_outbox_are_atomic_and_idempotent(runtime) -> None:
    accepted = runtime.accept_completed_code_task()
    assert accepted.acceptance.code_task_id == "code-task"
    assert accepted.outbox.state == "pending"
    again = runtime.accept_completed_code_task()
    assert again == accepted
    assert runtime.store.acceptance_count("code-task") == 1
    assert runtime.store.outbox_count(accepted.outbox.ingestion_key) == 1


def test_acceptance_requires_same_workflow_coordinator_lease(runtime) -> None:
    with pytest.raises(ServiceError) as captured:
        runtime.accept_completed_code_task(
            coordinator_task_id="other-workflow-sol",
        )
    assert captured.value.code == "ACCEPTANCE_FORBIDDEN"


def test_missing_or_failed_hook_receipt_blocks_acceptance(runtime) -> None:
    with pytest.raises(ServiceError) as captured:
        runtime.accept_completed_code_task(execution_receipt_ids=())
    assert captured.value.code == "EVIDENCE_INCOMPLETE"


def test_transaction_failure_writes_neither_acceptance_nor_outbox(runtime) -> None:
    with mock.patch.object(
        runtime.store,
        "_insert_atlas_outbox",
        side_effect=sqlite3.OperationalError("injected"),
    ):
        with pytest.raises(sqlite3.OperationalError):
            runtime.accept_completed_code_task()
    assert runtime.store.acceptance_count("code-task") == 0
    assert runtime.store.pending_ingestions(limit=10) == ()
```

Import `mock` from `unittest`. Keep `_insert_atlas_outbox` as the one private
insert helper called within the acceptance transaction; do not add a mutable
production failure flag.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_acceptance.py `
  mcp-tools/tests/test_project_index_workflow.py `
  mcp-tools/tests/test_orchestrator_store_service_api.py -q
```

Expected: tests fail because typed tasks and acceptance tables are absent.

- [ ] **Step 3: Extend task records compatibly**

Add:

```python
class TaskKind(str, Enum):
    GENERAL = "general"
    CODE = "code"
    ANALYSIS = "analysis"
    DOCUMENTATION = "documentation"
    VERIFICATION = "verification"
```

Append defaulted fields to `Task`:

```python
task_kind: TaskKind = TaskKind.GENERAL
intent_id: str = ""
language: str = ""
framework: str = ""
```

Bump orchestrator schema to 4. Migrate `tasks` with four `NOT NULL DEFAULT`
columns, preserving every v3 row. A code task registration requires a
non-empty write scope, strict index binding, canonical intent id, and language.
General legacy registrations remain unchanged.

- [ ] **Step 4: Add atomic acceptance/outbox tables**

Add:

```sql
CREATE TABLE IF NOT EXISTS code_task_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    code_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    code_task_version INTEGER NOT NULL,
    input_snapshot_id TEXT NOT NULL,
    output_snapshot_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    indexed_diff_hash TEXT NOT NULL,
    coordinator_task_id TEXT NOT NULL REFERENCES tasks(id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atlas_ingestion_outbox (
    ingestion_key TEXT PRIMARY KEY,
    acceptance_id TEXT NOT NULL UNIQUE
        REFERENCES code_task_acceptances(acceptance_id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('pending', 'projected', 'quarantined')),
    attempt_count INTEGER NOT NULL,
    last_error_code TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS atlas_outbox_pending
    ON atlas_ingestion_outbox(state, created_at, ingestion_key);
```

Add frozen `CodeTaskAcceptance`, `AtlasOutboxItem`, and
`CodeAcceptanceResult` records. Store methods must include
`accept_code_task`, `acceptance_for_task`, `list_acceptances`,
`pending_ingestions`, `mark_ingestion_projected`,
`mark_ingestion_quarantined`, and `mark_ingestion_retry`.

Within the one acceptance transaction:

1. validate the coordinator's current lease;
2. require coordinator and code task in the same workflow;
3. require coordinator role exactly `sol` or `opus`;
4. compare code-task state/version/kind and output snapshot;
5. re-run the existing strict-completion database gates;
6. return an existing same-output acceptance idempotently;
7. insert acceptance and pending outbox together.

- [ ] **Step 5: Validate evidence and project accepted tasks**

`OrchestratorService.accept_code_task` validates outside the transaction, then
passes one canonical immutable payload into the store:

- project-index output snapshot is current for the write scope;
- checkpoint record matches workflow/task/input snapshot/write scope;
- receipt repository contains every requested id;
- at least one successful write/patch receipt and one successful command receipt
  exist;
- all receipts bind to the same workspace hash;
- generic strict query and verification artifacts are present.

The payload binds raw receipts into final `ExecutionReceipt` graph facts by
adding workflow id, task id, acceptance id, and output snapshot id. A successful
test is evidence only; it does not create coordinator authority.

`CodeAtlasService.project_acceptance`:

1. returns the existing ingestion receipt when ingestion key and payload hash
   match;
2. reads checkpoint preimages and current output files through Task 5 APIs;
3. creates and stores a `TaskEpisode` graph for every acceptance;
4. runs `PythonRecipeExtractor` for supported code;
5. stores a local recipe/CAS blobs only when the complete reuse gate passes;
6. writes an episode-only ingestion receipt with stable reasons otherwise;
7. rejects a same-key/different-payload conflict.

`OrchestratorService.status` adds a bounded `code_acceptances` projection with
`acceptance_id`, `code_task_id`, `output_snapshot_id`,
`atlas_ingest_state`, `episode_id`, `recipe_id`, and reasons. It never returns
the outbox payload, source bodies, or raw receipt JSON.

- [ ] **Step 6: Test projection failure and restart recovery**

Add:

```python
def test_projection_failure_leaves_acceptance_and_restart_projects_once(
    runtime,
) -> None:
    accepted = runtime.accept_completed_code_task(projector_available=False)
    assert accepted.outbox.state == "pending"
    assert runtime.store.acceptance_for_task("code-task") is not None

    restarted = runtime.reopen(projector_available=True)
    drained = restarted.drain_pending_ingestions()
    assert len(drained) == 1
    status = restarted.service.status("wf")
    projection = status["code_acceptances"][0]
    assert projection["atlas_ingest_state"] == "projected"
    assert projection["episode_id"].startswith("sha256:")
    assert restarted.drain_pending_ingestions() == ()
    assert restarted.atlas_store.ingestion_receipt_count() == 1
```

Temporary SQLite/CAS failures call `mark_ingestion_retry` and remain pending.
Canonical payload corruption or same-key conflicts call
`mark_ingestion_quarantined`. Episode-only is a successful `projected` state,
not a retry.

- [ ] **Step 7: Run acceptance and regression tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_acceptance.py `
  mcp-tools/tests/test_project_index_workflow.py `
  mcp-tools/tests/test_orchestrator_store_service_api.py `
  mcp-tools/tests/test_orchestrator_service.py -q
```

Expected: all tests pass, v3 fixtures migrate to schema 4, and duplicate
acceptance/projection counts stay at one.

- [ ] **Step 8: Commit**

```powershell
git add mcp-tools/orchestrator mcp-tools/code_atlas/service.py `
  mcp-tools/tests/test_code_atlas_acceptance.py `
  mcp-tools/tests/test_project_index_workflow.py `
  mcp-tools/tests/test_orchestrator_store_service_api.py
git commit -m "feat: ingest automatically accepted code tasks"
```

### Task 11: Four MCP tools and startup outbox recovery

**Files:**

- Modify: `mcp-tools/server.py:15-41,118-202,228-418,568-604,929-932`
- Modify: `mcp-tools/tests/test_mcp_contract.py`
- Test: `mcp-tools/tests/test_code_atlas_mcp.py`

- [ ] **Step 1: Extend the failing FastMCP contract**

Update the exact expected tool set with:

```python
{
    "code_atlas_graph_query",
    "code_atlas_prepare",
    "code_atlas_render",
    "workflow_accept_code_task",
}
```

Add assertions:

```python
def test_code_atlas_annotations_and_schemas_are_safe(self) -> None:
    tools = self._tool_map()
    for name in (
        "code_atlas_graph_query",
        "code_atlas_prepare",
        "code_atlas_render",
    ):
        annotations = tools[name].annotations
        self.assertTrue(annotations.readOnlyHint)
        self.assertFalse(annotations.destructiveHint)
        self.assertTrue(annotations.idempotentHint)
    acceptance = tools["workflow_accept_code_task"].annotations
    self.assertFalse(acceptance.readOnlyHint)
    self.assertFalse(acceptance.destructiveHint)
    self.assertTrue(acceptance.idempotentHint)

    rendered_schema = tools["code_atlas_render"].inputSchema
    self.assertEqual(
        set(rendered_schema["required"]),
        {"workspace", "snapshot_id", "packet_id", "bindings"},
    )
```

Scan Python imports and MCP manifest keys structurally. Reject a module import
or MCP namespace whose normalized name equals `codegraph` or
`external_codegraph`; do not reject prose that documents the prohibition.

- [ ] **Step 2: Run contract tests and verify failure**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_mcp_contract.py `
  mcp-tools/tests/test_code_atlas_mcp.py -q
```

Expected: missing tools/runtime errors.

- [ ] **Step 3: Add Atlas runtimes and error mapping**

Add an `_atlas_runtime` context manager that resolves:

```text
DATA_ROOT/code-atlas.sqlite3
DATA_ROOT/code-atlas-cas/
DATA_ROOT/code-atlas-receipts/
PLUGIN_ROOT/skills/code-atlas/assets/
```

It opens `AtlasStore`, bundled loader, project index, checkpoint service, and
`CodeAtlasService`, then closes every SQLite connection. Extend `_safe_call` to
map `AtlasError.code` without exposing paths, payloads, tracebacks, command
specs, or source.

Add wrappers with the frozen signatures and annotations. The wrappers only call
service methods and serialize records; they contain no subprocess, shell,
patch, model, or workspace-write branch.

`workflow_accept_code_task`:

1. opens receipt/index/checkpoint/orchestrator runtimes;
2. validates and commits acceptance/outbox;
3. attempts exactly one synchronous Atlas projection after commit;
4. updates outbox projection state;
5. returns acceptance plus graph ids or `INGEST_PENDING`.

- [ ] **Step 4: Add bounded startup recovery**

Implement:

```python
def _drain_atlas_outbox(limit: int = 32) -> None:
    """Best-effort deterministic projection; never write to stdout."""
```

It opens the same runtimes, processes pending rows in
`(created_at, ingestion_key)` order, and records projected, retry, or
quarantined state. It catches errors at the per-item boundary so one bad item
does not starve later rows. Call it immediately before `mcp.run()` under
`if __name__ == "__main__":`.

Do not run models, host tools, or target-workspace writes during startup.

Add failure-contract assertions: an unreadable Atlas database returns the safe
error code `ATLAS_UNAVAILABLE` from graph/prepare/render; a committed acceptance
whose projection cannot open Atlas returns `INGEST_PENDING` and remains visible
in `workflow_status`. Neither condition may select an external fallback.

- [ ] **Step 5: Run MCP and recovery tests**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_mcp_contract.py `
  mcp-tools/tests/test_code_atlas_mcp.py `
  mcp-tools/tests/test_code_atlas_acceptance.py -q
python skills/mcp-server-dev/scripts/validate_mcp_server.py mcp-tools/server.py
```

Expected: all tests pass and the validator reports no MCP framework errors.

- [ ] **Step 6: Commit**

```powershell
git add mcp-tools/server.py mcp-tools/tests/test_mcp_contract.py `
  mcp-tools/tests/test_code_atlas_mcp.py
git commit -m "feat: expose Code Atlas through 2718lab tools"
```

### Task 12: Codex/Claude routing assets and plugin workflow

**Files:**

- Create: `mcp-tools/code_atlas/routing.py`
- Create: `mcp-tools/tests/test_code_atlas_routing.py`
- Create: `skills/code-atlas/SKILL.md`
- Create: `skills/code-atlas/references/atlas-workflow.md`
- Create: `skills/code-atlas/references/host-routing.md`
- Create: `skills/code-atlas/references/status-contract.md`
- Create: `skills/code-atlas/assets/host-profiles.json`
- Create: `agents/bugkiller-sol-coordinator.md`
- Create: `agents/bugkiller-luna-code-worker.md`
- Delete: `agents/bugkiller-sol-code-writer.md`
- Delete: `agents/bugkiller-luna-triage.md`
- Modify: `agents/bugkiller-terra-investigator.md`
- Modify: `agents/bugkiller-terra-doc-writer.md`
- Modify: `agents/bugkiller-terra-verifier.md`
- Modify: `agents/bugkiller-sol-escalation.md`
- Modify: `agents/2718lab-redteam.md`
- Modify: `agents/openai.yaml`
- Modify: `skills/bugkiller/SKILL.md`
- Modify: `skills/bugkiller/references/roles.md`
- Modify: `skills/bugkiller/references/workflow.md`
- Modify: `skills/bugkiller/scripts/validate_bugkiller.py`
- Modify: `skills/work-methodology/references/team-patterns.md`
- Modify: `skills/work-methodology/references/orchestration-runtime.md`
- Modify: `skills/work-methodology/scripts/validate_work_package.py`
- Modify: `mcp-tools/tests/test_bugkiller_assets.py`
- Modify: `mcp-tools/tests/test_bugkiller_metadata.py`
- Modify: `skills/work-methodology/tests/test_methodology_policy.py`
- Modify: `skills/work-methodology/tests/test_work_package.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing routing-policy tests**

`test_code_atlas_routing.py` must assert:

```python
def test_codex_roles_require_exact_models_and_luna_max(profile) -> None:
    assert profile["codex"]["coordinator"] == {
        "model": "gpt-5.6-sol",
        "responsibilities": ["design", "dispatch", "review", "final_acceptance"],
    }
    assert profile["codex"]["code_worker"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "responsibilities": ["coding", "testing", "debugging", "heavy_execution"],
    }
    assert profile["codex"]["medium_worker"]["model"] == "gpt-5.6-terra"
    assert profile["codex"]["fallback"] == "fail_closed"


def test_claude_fable_is_explicit_and_zero_budget(profile) -> None:
    assert profile["claude"]["coordinator"]["family"] == "opus"
    assert profile["claude"]["code_worker"]["family"] == "sonnet"
    assert profile["claude"]["light_worker"]["family"] == "haiku"
    assert profile["claude"]["escalation"]["family"] == "fable"
    assert profile["claude"]["escalation"]["automatic_budget"] == 0
    assert profile["claude"]["resolve_full_id_from_capabilities"] is True


def test_missing_requested_model_fails_closed(profile) -> None:
    result = resolve_role(
        profile,
        host="codex",
        role="code_worker",
        available_models=("gpt-5.6-sol", "gpt-5.6-terra"),
        requested_reasoning_effort="max",
    )
    assert result.status.value == "MODEL_UNAVAILABLE"
    assert result.selected_model == ""
    assert result.required_model == "gpt-5.6-luna"
```

Update legacy tests first so they reject Sol as code writer and accept only the
new Luna Max code-worker asset.

- [ ] **Step 2: Run policy tests and verify failure**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_routing.py `
  mcp-tools/tests/test_bugkiller_assets.py `
  skills/work-methodology/tests/test_methodology_policy.py `
  skills/work-methodology/tests/test_work_package.py -q
```

Expected: failures identify the current Sol-writer/Luna-read-only policy.

- [ ] **Step 3: Add the machine-readable host profile and resolver**

`host-profiles.json` contains only role requirements and capability family
names. Claude entries do not invent a full model id:

```json
{
  "schema_version": "1",
  "codex": {
    "coordinator": {
      "model": "gpt-5.6-sol",
      "responsibilities": ["design", "dispatch", "review", "final_acceptance"]
    },
    "code_worker": {
      "model": "gpt-5.6-luna",
      "reasoning_effort": "max",
      "responsibilities": ["coding", "testing", "debugging", "heavy_execution"]
    },
    "medium_worker": {
      "model": "gpt-5.6-terra",
      "responsibilities": ["bounded_analysis", "documentation", "aux_validation"]
    },
    "fallback": "fail_closed"
  },
  "claude": {
    "coordinator": {"family": "opus"},
    "code_worker": {"family": "sonnet"},
    "light_worker": {"family": "haiku"},
    "escalation": {
      "family": "fable",
      "automatic_budget": 0,
      "triggers": [
        "user_selected",
        "long_running_refactor",
        "cross_repository_migration",
        "two_failed_execution_rounds"
      ]
    },
    "resolve_full_id_from_capabilities": true,
    "fallback": "fail_closed"
  }
}
```

`routing.resolve_role` is pure and accepts host-reported available model ids or
Claude family/id pairs. It never probes a network, invokes a model, or chooses a
different family. Its error includes the exact required model/family and
available capability names.

- [ ] **Step 4: Replace contradictory role assets**

`bugkiller-sol-coordinator` owns design, task cards, dispatch, review,
acceptance, and the `workflow_accept_code_task` call. It has no routine code
write scope.

`bugkiller-luna-code-worker` requires the exact `gpt-5.6-luna` model plus
`reasoning_effort=max`, owns coding/tests/debugging in one assigned write scope,
uses Code/exec batching when available, falls back to equivalent direct host
calls when Code/exec is absent, and returns verification/receipt ids. It cannot
accept its own task, change routing, broaden scope, or request Fable.

Terra assets remain bounded medium-complexity roles. A documentation-only Terra
card may write documentation; Terra does not become the fallback code worker.

Claude docs define Opus/Sonnet/Haiku/Fable responsibilities and require host
capability resolution. Fable remains unavailable automatically until one listed
trigger is explicitly recorded.

The thin `skills/code-atlas/SKILL.md` routes to its three references and states
the exact sequence:

```text
project_index_sync
-> code_atlas_prepare
-> code_atlas_render when READY
-> native patch/Code-exec
-> native verification
-> output project_index_sync/query
-> workflow_complete
-> Sol/Opus workflow_accept_code_task
-> automatic Atlas projection
```

`NO_VERIFIED_RECIPE` proceeds through normal coding once, then the same
acceptance pipeline. External CodeGraph is not a fallback.

`status-contract.md` gives one exact action per status:

| Status | Required host action |
| --- | --- |
| `READY` | Call `code_atlas_render`, then use native patch/test tools. |
| `NO_VERIFIED_RECIPE` | Run normal scoped coding once and keep acceptance ingestion enabled. |
| `INDEX_STALE` | Call `project_index_sync` once and retry preparation; block if it remains stale. |
| `AMBIGUOUS_MATCH` | Return candidate ids/reasons to Sol or Opus; do not render either candidate. |
| `UNSUPPORTED_LANGUAGE` | Run normal coding; accepted work records an episode only. |
| `RENDER_INVALID` | Discard the candidate and re-prepare from a fresh snapshot. |
| `EVIDENCE_INCOMPLETE` | Block acceptance until trusted hook/test evidence exists. |
| `RECIPE_QUARANTINED` | Exclude the recipe and continue through normal coding. |
| `INGEST_PENDING` | Keep acceptance successful and allow deterministic retry. |
| `ATLAS_UNAVAILABLE` | Use normal coding only when the task card explicitly allows degraded Atlas; never use external CodeGraph. |
| `MODEL_UNAVAILABLE` | Block the requested role and report the exact missing capability. |

The plugin cannot inject a host Code/exec tool. The worker inspects the
host-exposed tool list: when Code/exec exists it batches freshness, preparation,
rendering, native patch/test calls, output sync, and evidence registration;
otherwise it performs those same calls directly in the same order.

- [ ] **Step 5: Update validators, tests, and README together**

Remove assertions that Luna/Terra never write code and Sol/ultra is the writer.
Require:

- the two new Sol/Luna assets and absence of deprecated assets;
- Luna's exact model/reasoning/write-scope/acceptance prohibitions;
- Sol's coordinator/final-acceptance responsibility;
- Terra's bounded role;
- Claude families and Fable zero budget;
- Code Atlas status actions and four MCP names;
- Code/exec direct-call equivalence;
- no silent model fallback.

`validate_work_package.py` permits code write scope only when the task card
contains all three exact markers:

```text
Owner: luna-max-code-worker
Model: gpt-5.6-luna
Reasoning effort: max
```

It continues to reject generic Luna, Terra, or Sol code-write claims.

- [ ] **Step 6: Run routing and asset validation**

Run:

```powershell
python -m pytest `
  mcp-tools/tests/test_code_atlas_routing.py `
  mcp-tools/tests/test_bugkiller_assets.py `
  mcp-tools/tests/test_bugkiller_metadata.py `
  skills/work-methodology/tests -q
python skills/bugkiller/scripts/validate_bugkiller.py
python skills/code-atlas/scripts/validate_recipes.py
```

Expected: all tests and both validators pass; no deprecated Sol writer or Luna
triage asset remains.

- [ ] **Step 7: Commit**

```powershell
git add agents skills README.md mcp-tools/code_atlas/routing.py `
  mcp-tools/tests/test_code_atlas_routing.py `
  mcp-tools/tests/test_bugkiller_assets.py `
  mcp-tools/tests/test_bugkiller_metadata.py
git commit -m "feat: route coding through Luna Max and Code Atlas"
```

### Task 13: Two-cycle end-to-end acceptance, full regression, and release metadata

**Files:**

- Create: `mcp-tools/tests/test_code_atlas_e2e.py`
- Modify: `.claude-plugin/plugin.json`
- Modify: `.codex-plugin/plugin.json`
- Modify: `.mcp.json` only if a new already-approved environment variable is
  genuinely required; otherwise leave it byte-identical.
- Modify: `README.md`

- [ ] **Step 1: Write the failing two-cycle E2E test**

The test creates an origin and two isolated Python Git worktrees below
`$env:CODEX_TASK_TEMP`. It runs:

```python
def test_no_match_accept_ingest_then_match_render_accept(
    isolated_two_cycle_runtime,
) -> None:
    first = isolated_two_cycle_runtime.first_cycle()
    assert first.prepare_status == "NO_VERIFIED_RECIPE"
    assert first.test_exit_code == 0
    assert first.acceptance.atlas_ingest_state == "projected"
    assert first.ingestion.episode_id.startswith("sha256:")
    assert first.ingestion.recipe_id.startswith("sha256:")

    second = isolated_two_cycle_runtime.second_cycle(
        intent_id=first.intent_id,
        recipe_id=first.ingestion.recipe_id,
    )
    assert second.prepare_status == "READY"
    assert second.render_status == "READY"
    assert second.test_exit_code == 0
    assert second.acceptance.atlas_ingest_state == "projected"

    repeated = isolated_two_cycle_runtime.repeat_second_prepare_and_render()
    assert repeated.packet_id == second.packet_id
    assert repeated.node_ids == second.node_ids
    assert repeated.edge_ids == second.edge_ids
    assert repeated.patch_hash == second.patch_hash
    assert isolated_two_cycle_runtime.model_call_count == 0
```

The first host simulation writes a source function and pytest regression after
a real task-owned checkpoint, runs real pytest, captures sanitized patch/test
receipts, syncs/query-binds output, completes the task, and invokes automatic
acceptance. The second worktree obtains the local recipe packet, renders it,
applies the returned candidate through the test's host simulator rather than
Atlas, runs real pytest, and accepts.

Also assert:

- installed plugin source/assets are unchanged by both cycles;
- local graph/CAS live only under the D-drive runtime root;
- one accepted unsupported edit produces an episode with no recipe;
- deleting the Atlas database and replaying immutable accepted outbox evidence
  produces the same graph ids and hashes;
- no runtime Python import or MCP namespace refers to external CodeGraph;
- direct and Code/exec fixture paths create equivalent receipt projections.

- [ ] **Step 2: Run E2E and verify the initial failure**

Run:

```powershell
python -m pytest mcp-tools/tests/test_code_atlas_e2e.py -q
```

Expected before final integration: at least one two-cycle contract assertion
fails. Do not weaken the assertion; fix the corresponding integration.

- [ ] **Step 3: Complete E2E integration and update release metadata**

Set the base plugin version to `0.3.0` in both manifests. Update descriptions,
capabilities, and at most three default prompts to mention deterministic Code
Atlas reuse and automatic accepted-task ingestion.

Then refresh only the Codex cachebuster:

```powershell
python C:\Users\pidan\.codex\skills\.system\plugin-creator\scripts\update_plugin_cachebuster.py `
  G:\2718lab\2718lab-devkit
```

The Claude version remains `0.3.0`; the Codex version must match the regular
expression `^0\.3\.0\+codex\.[0-9]+$`.

- [ ] **Step 4: Run the complete regression suite**

Run:

```powershell
python -m pytest mcp-tools/tests skills/work-methodology/tests -q
```

Expected: zero failed tests across Atlas, project-index, checkpoint,
orchestrator, Bugkiller, MCP contracts, hooks, and methodology.

- [ ] **Step 5: Run every validator and metadata parse**

Run:

```powershell
python skills/code-atlas/scripts/validate_recipes.py
python skills/bugkiller/scripts/validate_bugkiller.py
python skills/mcp-server-dev/scripts/validate_mcp_server.py mcp-tools/server.py
python C:\Users\pidan\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py `
  G:\2718lab\2718lab-devkit
python -c "import json, pathlib; [json.loads(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['.mcp.json','.codex-plugin/plugin.json','.claude-plugin/plugin.json','hooks/hooks.json']]; print('JSON valid')"
```

Expected: all validators exit zero and the JSON command prints `JSON valid`.

- [ ] **Step 6: Review security, determinism, and repository state**

Run:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- mcp-tools hooks skills agents README.md `
  .mcp.json .codex-plugin/plugin.json .claude-plugin/plugin.json
```

Sol must inspect:

- all Atlas tools remain data-only;
- no secret/raw output/full repository file is persisted;
- data-root/cache boundaries remain intact;
- acceptance/outbox atomicity and retry evidence passed;
- equal-best matching never selects arbitrarily;
- model routing is exact and fail-closed;
- no unrelated user change is present.

- [ ] **Step 7: Commit the verified delivery**

```powershell
git add mcp-tools/tests/test_code_atlas_e2e.py README.md `
  .claude-plugin/plugin.json .codex-plugin/plugin.json
git commit -m "test: verify Code Atlas two-cycle reuse"
```

Do not configure a remote, push, publish, or modify the installed plugin cache
as part of this plan. Plugin installation/reload is a separate user-authorized
delivery action after the source repository passes all acceptance gates.

## Final acceptance matrix

| Requirement | Evidence |
| --- | --- |
| Typed graph, content ids, edge constraints | Tasks 2-3 tests |
| Separate local DB/CAS and bundled layer | Tasks 3-4 tests |
| Python v1 extractor, reuse gate, round trip | Task 6 tests |
| Deterministic match, ambiguity, packets | Task 7 tests |
| Read-only render and promotion bundle | Task 8 tests |
| Direct/Code-exec receipt equivalence | Task 9 tests |
| Automatic acceptance/outbox/recovery | Tasks 10-11 tests |
| Four safe MCP tools | Task 11 contract tests |
| Sol/Luna Max/Terra routing | Task 12 policy tests |
| Opus/Sonnet/Haiku/Fable routing | Task 12 policy tests |
| First miss, accepted ingestion, second hit | Task 13 E2E |
| Zero LLM calls and no external CodeGraph | Tasks 11 and 13 |
| Legacy regression and validators | Task 13 |
