"""Contract tests for deterministic Atlas matching and preparation."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas import ASSET_ROOT
from devkit_atlas.canonical import canonical_hash, canonical_json
from devkit_atlas.matching import (
    MatchClass,
    select_recipe,
    structural_repository_signature,
)
from devkit_atlas.models import (
    AtlasEdge,
    AtlasError,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    EdgeRelation,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec as AtlasTestSpec,
)
from devkit_atlas.recipes import BundledRecipeLoader
from devkit_atlas.rendering import render_patch, validate_bindings
from devkit_atlas.security import (
    MAX_CHANGED_FILES,
    MAX_COMMAND_SPEC_BYTES,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
)
from devkit_atlas.service import (
    AtlasService,
    _placeholder_names as _local_placeholder_names,
)
from devkit_atlas.store import AtlasStore
from project_index.models import (
    CoverageGap,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    SnapshotFacts,
)
from project_index.service import ProjectIndexService


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ASSET_ROOT


def _node(
    kind: str,
    name: str,
    *,
    path: str = "src/module.py",
    qualified_name: str | None = None,
) -> IndexNode:
    return IndexNode(
        node_id=canonical_hash({"kind": kind, "name": name, "path": path}),
        kind=kind,
        path=path,
        name=name,
        qualified_name=qualified_name if qualified_name is not None else name,
        start_line=1,
        end_line=1,
        content_hash=canonical_hash({"path": path}),
        extractor_id="python",
        extractor_version="1",
    )


def _facts(
    *,
    file_hashes: tuple[tuple[str, str], ...] = (("src/module.py", "sha256:1"),),
    nodes: tuple[IndexNode, ...] = (),
    gaps: tuple[CoverageGap, ...] = (),
) -> SnapshotFacts:
    snapshot = IndexSnapshot(
        snapshot_id="sha256:" + "0" * 64,
        workspace="C:/absolute/fixture-workspace",
        state=IndexState.INDEX_READY,
        file_count=len(file_hashes),
        blob_count=len(file_hashes),
        reused_blob_count=0,
        node_count=len(nodes),
        edge_count=0,
        gap_count=len(gaps),
    )
    return SnapshotFacts(snapshot, tuple(sorted(file_hashes)), nodes, (), gaps)


def _manifest(
    *,
    recipe_id: str,
    intent_id: str = "python.fixture",
    version: int = 1,
    language: str = "python",
    framework: str | None = None,
    framework_specifier: str | None = None,
    repository_signature: str = "",
    layer: str = "local",
    constraints: tuple[ConstraintSpec, ...] = (),
    slots: tuple[SlotSpec, ...] = (),
    tests: tuple[AtlasTestSpec, ...] = (),
    operations: tuple[TemplateOperation, ...] = (),
    superseded_ids: tuple[str, ...] = (),
    quarantine_state: str | None = None,
    language_extractor_version: str = "1",
    provenance_kind: str = "observed",
    provenance_source: str = "fixture",
) -> RecipeManifest:
    manifest_data = {
        "intent_id": intent_id,
        "version": version,
        "language": language,
        "framework": framework,
        "framework_specifier": framework_specifier,
        "repository_signature": repository_signature,
        "layer": layer,
        "constraints": [item.to_dict() for item in constraints],
        "slots": [item.to_dict() for item in slots],
        "tests": [item.to_dict() for item in tests],
        "operations": [item.to_dict() for item in operations],
        "superseded_ids": list(superseded_ids),
        "quarantine_state": quarantine_state,
        "language_extractor_version": language_extractor_version,
        "provenance_kind": provenance_kind,
        "provenance_source": provenance_source,
    }
    return RecipeManifest(
        recipe_id=recipe_id,
        recipe_key=intent_id,
        version=version,
        intent_id=intent_id,
        language_name=language,
        language_extractor_version=language_extractor_version,
        repository_signature=repository_signature,
        layer=layer,
        manifest_hash=canonical_hash(manifest_data),
        framework_name=framework,
        framework_specifier=framework_specifier,
        slots=slots,
        constraints=constraints,
        tests=tests,
        operations=operations,
        provenance_kind=provenance_kind,
        provenance_source=provenance_source,
        superseded_ids=superseded_ids,
        quarantine_state=quarantine_state,
    )


def _observed_schema_payload(manifest: RecipeManifest) -> dict[str, object]:
    return {
        "schema_version": "1",
        "recipe_key": manifest.recipe_key,
        "version": manifest.version,
        "intent_id": manifest.intent_id,
        "language": {
            "name": manifest.language_name,
            "extractor_version": manifest.language_extractor_version,
        },
        "framework": None,
        "repository_signature": manifest.repository_signature,
        "layer": manifest.layer,
        "slots": [slot.to_dict() for slot in manifest.slots],
        "constraints": [constraint.to_dict() for constraint in manifest.constraints],
        "dependencies": [dependency.to_dict() for dependency in manifest.dependencies],
        "tests": [test.to_dict() for test in manifest.tests],
        "operations": [operation.to_dict() for operation in manifest.operations],
        "provenance": {
            "kind": manifest.provenance_kind,
            "source": manifest.provenance_source,
        },
    }


def _with_observed_manifest_hash(manifest: RecipeManifest) -> RecipeManifest:
    return replace(
        manifest, manifest_hash=canonical_hash(_observed_schema_payload(manifest))
    )


def _observed_manifest(
    *,
    recipe_id: str,
    intent_id: str,
    repository_signature: str,
    superseded_ids: tuple[str, ...] = (),
    quarantine_state: str | None = None,
) -> RecipeManifest:
    slots = (SlotSpec("path_000", "relative_python_path"),)
    manifest = _manifest(
        recipe_id=recipe_id,
        intent_id=intent_id,
        version=1,
        language="python",
        framework=None,
        framework_specifier=None,
        repository_signature=repository_signature,
        layer="local",
        slots=slots,
        constraints=(ConstraintSpec("path_suffix", "path_000", ".py"),),
        tests=(),
        operations=(
            TemplateOperation(
                "create_python_file",
                "path_000",
                canonical_hash({"template": intent_id}),
            ),
        ),
        superseded_ids=superseded_ids,
        quarantine_state=quarantine_state,
        provenance_kind="observed",
        provenance_source="accepted_task",
    )
    return _with_observed_manifest_hash(manifest)


def _recipe_payload(manifest: RecipeManifest) -> dict[str, object]:
    payload = manifest.to_dict()
    del payload["recipe_id"]
    return payload


def _put_local_recipe(
    store: AtlasStore,
    manifest: RecipeManifest,
    graph: tuple[tuple[NodeKind, dict[str, object], EdgeRelation], ...] = (),
) -> RecipeManifest:
    recipe_node = AtlasNode.create(
        NodeKind.RECIPE,
        _recipe_payload(manifest),
        extractor_id="python-ast",
        extractor_version="1",
        provenance="observed",
        source_hashes=(manifest.manifest_hash,),
        quarantine_state=manifest.quarantine_state,
    )
    registered = replace(manifest, recipe_id=recipe_node.node_id)
    children = tuple(
        AtlasNode.create(
            kind,
            payload,
            extractor_id="python-ast",
            extractor_version="1",
            provenance="observed",
            source_hashes=(manifest.manifest_hash,),
        )
        for kind, payload, _relation in graph
    )
    edges = tuple(
        AtlasEdge.create(relation, recipe_node, child, provenance="observed")
        for child, (_kind, _payload, relation) in zip(children, graph, strict=True)
    )
    store.put_nodes((recipe_node, *children))
    store.put_edges(edges)
    store.put_recipe(
        registered,
        node_ids=(recipe_node.node_id, *(item.node_id for item in children)),
        edge_ids=tuple(item.edge_id for item in edges),
    )
    return registered


def test_select_recipe_has_unique_none_and_equal_class_results() -> None:
    facts = _facts()
    one = _manifest(recipe_id="one")

    ready = select_recipe(
        (one,),
        intent_id="python.fixture",
        language="PYTHON",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert ready.status is AtlasStatus.READY
    assert ready.winner is not None
    assert ready.winner.manifest.recipe_id == "one"
    assert ready.winner.match_class is MatchClass.LANGUAGE_GENERIC

    missing = select_recipe(
        (),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert missing.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert missing.winner is None

    ambiguous = select_recipe(
        (replace(one, recipe_id="z"), replace(one, recipe_id="a")),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert ambiguous.status is AtlasStatus.AMBIGUOUS_MATCH
    assert ambiguous.winner is None
    assert tuple(item.manifest.recipe_id for item in ambiguous.candidates) == ("a", "z")


def test_matching_priority_deduplication_layers_and_candidate_boundaries() -> None:
    facts = _facts()
    signature = structural_repository_signature(
        facts, language="python", framework="pytest"
    )
    local = _manifest(
        recipe_id="local",
        framework="pytest",
        framework_specifier=">=7,<9",
        repository_signature=signature,
    )
    bundled = _manifest(
        recipe_id="bundled",
        framework="pytest",
        framework_specifier=">=7,<9",
        layer="bundled",
    )
    result = select_recipe(
        (bundled, local),
        intent_id="python.fixture",
        language="python",
        framework="pytest",
        repository_signature=signature,
        snapshot_facts=facts,
    )
    assert result.status is AtlasStatus.READY
    assert result.winner is not None
    assert result.winner.manifest.recipe_id == "local"
    assert result.winner.match_class is MatchClass.EXACT_REPOSITORY

    duplicate = select_recipe(
        (local, local),
        intent_id="python.fixture",
        language="python",
        framework="pytest",
        repository_signature=signature,
        snapshot_facts=facts,
    )
    assert duplicate.status is AtlasStatus.READY
    assert tuple(item.manifest.recipe_id for item in duplicate.candidates) == ("local",)

    same_class = select_recipe(
        (
            replace(bundled, recipe_id="bundled", layer="bundled"),
            replace(bundled, recipe_id="local", layer="local"),
        ),
        intent_id="python.fixture",
        language="python",
        framework="pytest",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert same_class.status is AtlasStatus.AMBIGUOUS_MATCH

    bounded = select_recipe(
        (
            replace(bundled, recipe_id="first"),
            replace(bundled, recipe_id="second"),
            replace(bundled, recipe_id="third"),
        ),
        intent_id="python.fixture",
        language="python",
        framework="pytest",
        repository_signature="",
        snapshot_facts=facts,
        max_candidates=1,
    )
    assert bounded.status is AtlasStatus.AMBIGUOUS_MATCH
    assert tuple(item.manifest.recipe_id for item in bounded.candidates) == ("first",)


def test_matching_supersession_is_explicit_and_quarantine_is_not_ready() -> None:
    facts = _facts()
    first = _manifest(recipe_id="first")
    second = _manifest(recipe_id="second", superseded_ids=("first",))
    selected = select_recipe(
        (first, second),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert selected.status is AtlasStatus.READY
    assert selected.winner is not None
    assert selected.winner.manifest.recipe_id == "second"

    third = _manifest(recipe_id="third", superseded_ids=("second",))
    unique = select_recipe(
        (first, second, third),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert unique.status is AtlasStatus.READY
    assert unique.winner is not None
    assert unique.winner.manifest.recipe_id == "third"

    absent_intermediary = _manifest(
        recipe_id="third",
        superseded_ids=("not-present",),
    )
    non_transitive = select_recipe(
        (first, absent_intermediary),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert non_transitive.status is AtlasStatus.AMBIGUOUS_MATCH
    assert tuple(item.manifest.recipe_id for item in non_transitive.candidates) == (
        "first",
        "third",
    )

    quarantined = _manifest(
        recipe_id="quarantined",
        superseded_ids=("first", "second"),
        quarantine_state="quarantined",
    )
    suppressed = select_recipe(
        (first, second, quarantined),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature="",
        snapshot_facts=facts,
    )
    assert suppressed.status is AtlasStatus.RECIPE_QUARANTINED
    assert suppressed.winner is None


def test_matching_uses_best_active_class_after_supersession() -> None:
    facts = _facts()
    repository_signature = "sha256:" + "a" * 64
    quarantined_exact = _manifest(
        recipe_id="quarantined-exact",
        repository_signature=repository_signature,
        quarantine_state="quarantined",
    )
    active_generic = _manifest(recipe_id="active-generic")

    selected = select_recipe(
        (quarantined_exact, active_generic),
        intent_id="python.fixture",
        language="python",
        framework="",
        repository_signature=repository_signature,
        snapshot_facts=facts,
    )

    assert selected.status is AtlasStatus.READY
    assert selected.winner is not None
    assert selected.winner.manifest.recipe_id == "active-generic"
    assert selected.winner.match_class is MatchClass.LANGUAGE_GENERIC


def test_matching_framework_and_constraint_matrix_is_strict() -> None:
    facts = _facts(
        file_hashes=(("src/guard.py", "sha256:1"),),
        nodes=(
            _node("function", "guard", qualified_name="pkg.guard"),
            _node("import", "json"),
        ),
    )
    constrained = _manifest(
        recipe_id="constrained",
        framework="pytest",
        framework_specifier=">=7,<9,!=8.1",
        constraints=(
            ConstraintSpec("required_node_kind", "", "function"),
            ConstraintSpec("required_symbol", "", "pkg.guard"),
            ConstraintSpec("required_import", "", "json"),
            ConstraintSpec("path_suffix", "", ".py"),
            ConstraintSpec("forbidden_gap", "", "UNSUPPORTED_PARSER"),
        ),
    )
    for framework in ("pytest", "pytest@7", "PYTEST@7.9.1"):
        result = select_recipe(
            (constrained,),
            intent_id="python.fixture",
            language="python",
            framework=framework,
            repository_signature="",
            snapshot_facts=facts,
        )
        assert result.status is AtlasStatus.READY

    for framework in ("pytest@6", "pytest@8.1", "other@7"):
        result = select_recipe(
            (constrained,),
            intent_id="python.fixture",
            language="python",
            framework=framework,
            repository_signature="",
            snapshot_facts=facts,
        )
        assert result.status is AtlasStatus.NO_VERIFIED_RECIPE

    invalid_specifier = replace(constrained, framework_specifier=">=7,what")
    assert (
        select_recipe(
            (invalid_specifier,),
            intent_id="python.fixture",
            language="python",
            framework="pytest",
            repository_signature="",
            snapshot_facts=facts,
        ).status
        is AtlasStatus.NO_VERIFIED_RECIPE
    )

    with pytest.raises(AtlasError):
        select_recipe(
            (constrained,),
            intent_id="python.fixture",
            language="python",
            framework="pytest@not-a-version",
            repository_signature="",
            snapshot_facts=facts,
        )

    unsupported = replace(
        constrained,
        constraints=(ConstraintSpec("unknown_constraint", "", "value"),),
    )
    assert (
        select_recipe(
            (unsupported,),
            intent_id="python.fixture",
            language="python",
            framework="pytest",
            repository_signature="",
            snapshot_facts=facts,
        ).status
        is AtlasStatus.NO_VERIFIED_RECIPE
    )

    blocked = _facts(
        file_hashes=facts.file_hashes,
        nodes=facts.nodes,
        gaps=(CoverageGap("src/guard.py", "UNSUPPORTED_PARSER", "not persisted"),),
    )
    assert (
        select_recipe(
            (constrained,),
            intent_id="python.fixture",
            language="python",
            framework="pytest",
            repository_signature="",
            snapshot_facts=blocked,
        ).status
        is AtlasStatus.NO_VERIFIED_RECIPE
    )


def test_matching_framework_versions_are_bounded_and_fail_closed() -> None:
    facts = _facts()
    candidate = _manifest(
        recipe_id="candidate",
        framework="pytest",
        framework_specifier=">=7,<9",
    )
    oversized_component = "9" * 5_000
    with pytest.raises(AtlasError) as request_error:
        select_recipe(
            (candidate,),
            intent_id="python.fixture",
            language="python",
            framework=f"pytest@{oversized_component}",
            repository_signature="",
            snapshot_facts=facts,
        )
    assert request_error.value.code == "invalid_framework"

    with pytest.raises(AtlasError) as component_error:
        select_recipe(
            (candidate,),
            intent_id="python.fixture",
            language="python",
            framework="pytest@1.2.3.4.5.6.7.8.9",
            repository_signature="",
            snapshot_facts=facts,
        )
    assert component_error.value.code == "invalid_framework"

    excessive_clauses = replace(
        candidate,
        framework_specifier=",".join(">=7" for _ in range(17)),
    )
    assert (
        select_recipe(
            (excessive_clauses,),
            intent_id="python.fixture",
            language="python",
            framework="pytest@7",
            repository_signature="",
            snapshot_facts=facts,
        ).status
        is AtlasStatus.NO_VERIFIED_RECIPE
    )

    malicious_candidate = replace(
        candidate,
        framework_specifier=f">={oversized_component}",
    )
    assert (
        select_recipe(
            (malicious_candidate,),
            intent_id="python.fixture",
            language="python",
            framework="pytest@7",
            repository_signature="",
            snapshot_facts=facts,
        ).status
        is AtlasStatus.NO_VERIFIED_RECIPE
    )


def test_structural_signature_is_path_neutral_and_fact_only() -> None:
    facts = _facts(
        file_hashes=(
            ("src/guard.py", "sha256:source-body-must-not-affect-signature"),
            ("tests/test_guard.py", "sha256:another-content-hash"),
        ),
        nodes=(
            _node("import", "json", qualified_name="json"),
            _node("call", "guard", qualified_name="pkg.guard"),
        ),
    )
    first = structural_repository_signature(
        facts, language=" PYTHON ", framework="pytest@7.2"
    )
    changed_snapshot = replace(
        facts,
        snapshot=replace(facts.snapshot, snapshot_id="sha256:" + "1" * 64),
    )
    second = structural_repository_signature(
        changed_snapshot, language="python", framework="pytest"
    )
    assert first == second
    assert first.startswith("sha256:")
    assert "fixture-workspace" not in first
    assert "source-body" not in first


@dataclass
class AtlasEnvironment:
    root: Path
    database: Path
    store: AtlasStore
    index: ProjectIndexService
    service: AtlasService


@pytest.fixture
def atlas_environment(tmp_path: Path):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests" / "test_feature.py").write_text(
        "import json\n\n\ndef test_feature() -> None:\n    assert json.loads('1') == 1\n",
        encoding="utf-8",
    )
    (root / "src" / "guards.py").write_text(
        "def guarded(value: int) -> int:\n    return value\n",
        encoding="utf-8",
    )
    database = tmp_path / "atlas.sqlite3"
    store = AtlasStore(database, tmp_path / "atlas-cas")
    index = ProjectIndexService(tmp_path / "index.sqlite3")
    service = AtlasService(store, BundledRecipeLoader(ASSETS), index)
    environment = AtlasEnvironment(root, database, store, index, service)
    try:
        yield environment
    finally:
        index.close()
        store.close()


def _prepare_pytest(
    environment: AtlasEnvironment,
    *,
    snapshot_id: str,
    byte_budget: int = 131_072,
):
    return environment.service.prepare(
        workspace=str(environment.root),
        snapshot_id=snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_feature.py",),
        target_symbols=(),
        max_candidates=20,
        byte_budget=byte_budget,
    )


def _packet_receipt_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(
            connection.execute("SELECT count(*) FROM atlas_packet_receipts").fetchone()[
                0
            ]
        )
    finally:
        connection.close()


def test_prepare_normalizes_equivalent_numeric_framework_versions(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    common = {
        "workspace": str(atlas_environment.root),
        "snapshot_id": snapshot.snapshot_id,
        "intent_id": "python.pytest-regression",
        "language": "python",
        "target_paths": ("tests/test_feature.py",),
    }
    first = atlas_environment.service.prepare(framework="pytest@7", **common)
    second = atlas_environment.service.prepare(framework="pytest@7.0", **common)

    assert first.status is AtlasStatus.READY
    assert second.status is AtlasStatus.READY
    assert first.packet is not None
    assert second.packet is not None
    assert first.packet.trace_id == second.packet.trace_id
    assert first.packet.packet_id == second.packet.packet_id


def test_prepare_builds_idempotent_safe_packet_and_reopens(
    atlas_environment: AtlasEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    before_assets = {
        path.relative_to(ASSETS).as_posix(): path.read_bytes()
        for path in ASSETS.rglob("*")
        if path.is_file()
    }
    query_traces: list[str] = []
    original_query = atlas_environment.index.query

    def recording_query(*args, **kwargs):
        result = original_query(*args, **kwargs)
        query_traces.append(result.trace_id)
        return result

    monkeypatch.setattr(atlas_environment.index, "query", recording_query)
    before_workspace = {
        path.relative_to(atlas_environment.root).as_posix(): path.read_bytes()
        for path in atlas_environment.root.rglob("*")
        if path.is_file()
    }
    first = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    second = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)

    assert first.status is AtlasStatus.READY
    assert first.packet is not None
    assert first == second
    assert first.packet.packet_id == second.packet.packet_id
    assert atlas_environment.store.get_packet(first.packet.packet_id) == first.packet
    assert first.packet.next_action == "atlas_render"
    assert first.packet.evidence_windows
    assert all("text" not in dict(item) for item in first.packet.evidence_windows)
    assert first.packet.receipt_hashes
    receipt = atlas_environment.index.query_receipt(query_traces[-1])
    assert canonical_hash(asdict(receipt)) in first.packet.receipt_hashes
    template_body = (
        ASSETS / "templates" / "sha256" / first.packet.template_hashes[0][7:]
    ).read_text(encoding="utf-8")
    assert template_body not in canonical_json(first.packet.to_dict())
    assert before_workspace == {
        path.relative_to(atlas_environment.root).as_posix(): path.read_bytes()
        for path in atlas_environment.root.rglob("*")
        if path.is_file()
    }
    assert before_assets == {
        path.relative_to(ASSETS).as_posix(): path.read_bytes()
        for path in ASSETS.rglob("*")
        if path.is_file()
    }

    atlas_environment.store.close()
    reopened = AtlasStore(
        atlas_environment.database, atlas_environment.database.parent / "atlas-cas"
    )
    try:
        assert reopened.get_packet(first.packet.packet_id) == first.packet
    finally:
        reopened.close()


def test_prepare_statuses_validate_inputs_evidence_and_staleness(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    unsupported = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="javascript",
        target_paths=("tests/test_feature.py",),
    )
    assert unsupported.status is AtlasStatus.UNSUPPORTED_LANGUAGE
    assert unsupported.packet is None

    with pytest.raises(AtlasError) as invalid_framework:
        atlas_environment.service.prepare(
            workspace=str(atlas_environment.root),
            snapshot_id=snapshot.snapshot_id,
            intent_id="python.pytest-regression",
            language="python",
            framework="pytest@" + "9" * 5_000,
            target_paths=("tests/test_feature.py",),
        )
    assert invalid_framework.value.code == "invalid_framework"

    for paths in (
        ("../escape.py",),
        ("tests/test_feature.py", "tests\\test_feature.py"),
        tuple(f"tests/test_{index}.py" for index in range(MAX_CHANGED_FILES + 1)),
    ):
        with pytest.raises(AtlasError):
            atlas_environment.service.prepare(
                workspace=str(atlas_environment.root),
                snapshot_id=snapshot.snapshot_id,
                intent_id="python.pytest-regression",
                language="python",
                framework="pytest",
                target_paths=paths,
            )

    missing = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("missing.py",),
    )
    assert missing.status is AtlasStatus.EVIDENCE_INCOMPLETE
    symbol = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_feature.py",),
        target_symbols=("not_present",),
    )
    assert symbol.status is AtlasStatus.EVIDENCE_INCOMPLETE
    with pytest.raises(AtlasError):
        atlas_environment.service.prepare(
            workspace=str(atlas_environment.root),
            snapshot_id=snapshot.snapshot_id,
            intent_id="python.pytest-regression",
            language="python",
            framework="pytest",
            target_paths=("tests/test_feature.py",),
            target_symbols=("not a symbol",),
        )

    before = _packet_receipt_count(atlas_environment.database)
    (atlas_environment.root / "tests" / "test_feature.py").write_text(
        "changed = True\n", encoding="utf-8"
    )
    stale = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert stale.status is AtlasStatus.INDEX_STALE
    assert stale.packet is None
    assert _packet_receipt_count(atlas_environment.database) == before


def test_prepare_foreign_and_second_freshness_failure_never_persist(
    atlas_environment: AtlasEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    foreign = atlas_environment.root.parent / "foreign"
    foreign.mkdir()
    (foreign / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    foreign_snapshot = atlas_environment.index.sync(foreign)
    before = _packet_receipt_count(atlas_environment.database)
    foreign_result = _prepare_pytest(
        atlas_environment, snapshot_id=foreign_snapshot.snapshot_id
    )
    assert foreign_result.status is AtlasStatus.INDEX_STALE
    assert foreign_result.packet is None
    assert _packet_receipt_count(atlas_environment.database) == before

    original_assert_current = atlas_environment.index.assert_current
    calls = 0

    def race_assert_current(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IndexError("INDEX_STALE", "fixture race")
        return original_assert_current(*args, **kwargs)

    monkeypatch.setattr(atlas_environment.index, "assert_current", race_assert_current)
    raced = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert raced.status is AtlasStatus.INDEX_STALE
    assert raced.packet is None
    assert _packet_receipt_count(atlas_environment.database) == before


def test_prepare_uses_local_repository_priority_and_preserves_ties(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    local = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id="python.validation-guard",
            repository_signature=signature,
        ),
    )
    request = {
        "workspace": str(atlas_environment.root),
        "snapshot_id": snapshot.snapshot_id,
        "intent_id": "python.validation-guard",
        "language": "python",
        "target_paths": ("tests/test_feature.py",),
    }
    preferred = atlas_environment.service.prepare(**request)
    assert preferred.status is AtlasStatus.READY
    assert preferred.packet is not None
    assert preferred.packet.recipe_id == local.recipe_id

    second = _put_local_recipe(
        atlas_environment.store,
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.validation-guard",
                    repository_signature=signature,
                ),
                operations=(
                    TemplateOperation(
                        "append_python_nodes",
                        "path_000",
                        canonical_hash({"template": "second-local-recipe"}),
                    ),
                ),
            )
        ),
    )
    tied = atlas_environment.service.prepare(**request)
    assert tied.status is AtlasStatus.AMBIGUOUS_MATCH
    assert tied.packet is None
    assert tied.candidate_recipe_ids == tuple(
        sorted((local.recipe_id, second.recipe_id))
    )


def test_observed_local_compatibility_metadata_selects_explicit_superseder(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    intent_id = "python.observed-supersession"
    older = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id=intent_id,
            repository_signature=repository_signature,
        ),
    )
    absent_superseded_id = "sha256:" + "0" * 64
    newer = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id=intent_id,
            repository_signature=repository_signature,
            superseded_ids=tuple(sorted((absent_superseded_id, older.recipe_id))),
        ),
    )

    assert newer.manifest_hash == older.manifest_hash
    result = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id=intent_id,
        language="python",
        target_paths=("tests/test_feature.py",),
    )

    assert result.status is AtlasStatus.READY
    assert result.packet is not None
    assert result.packet.recipe_id == newer.recipe_id
    assert result.candidate_recipe_ids == (newer.recipe_id,)


def test_quarantined_observed_superseder_suppresses_older_local_recipe(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    intent_id = "python.quarantined-supersession"
    older = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id=intent_id,
            repository_signature=repository_signature,
        ),
    )
    quarantined = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id=intent_id,
            repository_signature=repository_signature,
            superseded_ids=(older.recipe_id,),
            quarantine_state="quarantined",
        ),
    )

    result = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id=intent_id,
        language="python",
        target_paths=("tests/test_feature.py",),
    )

    assert result.status is AtlasStatus.RECIPE_QUARANTINED
    assert result.packet is None
    assert result.candidate_recipe_ids == (quarantined.recipe_id,)


def test_observed_local_compatibility_metadata_rejects_invalid_values(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    first_id = "sha256:" + "1" * 64
    duplicate_id = "sha256:" + "e" * 64
    invalid_manifests = (
        _observed_manifest(
            recipe_id="",
            intent_id="python.invalid-superseded-hash",
            repository_signature=repository_signature,
            superseded_ids=("not-a-hash",),
        ),
        _observed_manifest(
            recipe_id="",
            intent_id="python.duplicate-superseded-id",
            repository_signature=repository_signature,
            superseded_ids=(duplicate_id, duplicate_id),
        ),
        _observed_manifest(
            recipe_id="",
            intent_id="python.noncanonical-superseded-order",
            repository_signature=repository_signature,
            superseded_ids=(duplicate_id, first_id),
        ),
        _observed_manifest(
            recipe_id="",
            intent_id="python.oversized-superseded-ids",
            repository_signature=repository_signature,
            superseded_ids=tuple(
                f"sha256:{index:064x}" for index in range(MAX_GRAPH_NODES + 1)
            ),
        ),
        _observed_manifest(
            recipe_id="",
            intent_id="python.invalid-local-quarantine-state",
            repository_signature=repository_signature,
            quarantine_state="ready",
        ),
    )
    for manifest in invalid_manifests:
        _put_local_recipe(atlas_environment.store, manifest)
        result = atlas_environment.service.prepare(
            workspace=str(atlas_environment.root),
            snapshot_id=snapshot.snapshot_id,
            intent_id=manifest.intent_id,
            language="python",
            target_paths=("tests/test_feature.py",),
        )
        assert result.status is AtlasStatus.NO_VERIFIED_RECIPE
        assert result.packet is None
        assert "malformed_local_recipe" in result.reasons


def test_observed_local_quarantine_payload_must_match_root_metadata(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    mismatches = ((None, "quarantined"), ("quarantined", None))
    for index, (payload_state, root_state) in enumerate(mismatches):
        intent_id = f"python.quarantine-mismatch-{index}"
        manifest = _observed_manifest(
            recipe_id="",
            intent_id=intent_id,
            repository_signature=repository_signature,
            quarantine_state=payload_state,
        )
        root = AtlasNode.create(
            NodeKind.RECIPE,
            _recipe_payload(manifest),
            extractor_id="python-ast",
            extractor_version="1",
            provenance="observed",
            source_hashes=(manifest.manifest_hash,),
            quarantine_state=root_state,
        )
        atlas_environment.store.put_nodes((root,))
        atlas_environment.store.put_recipe(replace(manifest, recipe_id=root.node_id))
        result = atlas_environment.service.prepare(
            workspace=str(atlas_environment.root),
            snapshot_id=snapshot.snapshot_id,
            intent_id=intent_id,
            language="python",
            target_paths=("tests/test_feature.py",),
        )
        assert result.status is AtlasStatus.NO_VERIFIED_RECIPE
        assert result.packet is None
        assert "malformed_local_recipe" in result.reasons


def test_observed_local_root_superseded_timestamp_is_rejected(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    manifest = _observed_manifest(
        recipe_id="",
        intent_id="python.root-superseded-timestamp",
        repository_signature=repository_signature,
    )
    root = AtlasNode.create(
        NodeKind.RECIPE,
        _recipe_payload(manifest),
        extractor_id="python-ast",
        extractor_version="1",
        provenance="observed",
        source_hashes=(manifest.manifest_hash,),
        superseded_at="2026-07-29T00:00:00Z",
    )
    atlas_environment.store.put_nodes((root,))
    atlas_environment.store.put_recipe(replace(manifest, recipe_id=root.node_id))

    result = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id=manifest.intent_id,
        language="python",
        target_paths=("tests/test_feature.py",),
    )

    assert result.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert result.packet is None
    assert "malformed_local_recipe" in result.reasons


def test_local_hydration_rejects_loader_invalid_semantics(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    template_hash = canonical_hash({"template": "fixture"})
    invalid_manifests = (
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-operation",
                    repository_signature=repository_signature,
                ),
                operations=(
                    TemplateOperation("execute_command", "path_000", template_hash),
                ),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-slot",
                    repository_signature=repository_signature,
                ),
                slots=(SlotSpec("path_000", "execute_command"),),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-slot-order",
                    repository_signature=repository_signature,
                ),
                slots=(
                    SlotSpec("symbol_000", "python_identifier"),
                    SlotSpec("path_000", "relative_python_path"),
                ),
                constraints=(
                    ConstraintSpec("required_symbol", "symbol_000", "observed_symbol"),
                    ConstraintSpec("path_suffix", "path_000", ".py"),
                ),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-constraint",
                    repository_signature=repository_signature,
                ),
                constraints=(ConstraintSpec("path_suffix", "missing_slot", ".py"),),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-test-placeholder",
                    repository_signature=repository_signature,
                ),
                tests=(AtlasTestSpec(("pytest", "${missing_slot}")),),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-test-placeholder-closer",
                    repository_signature=repository_signature,
                ),
                tests=(AtlasTestSpec(("pytest", "${path_000}}")),),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.oversized-test-command",
                    repository_signature=repository_signature,
                ),
                tests=(AtlasTestSpec(("pytest", "x" * MAX_COMMAND_SPEC_BYTES)),),
            )
        ),
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.invalid-append-separator",
                    repository_signature=repository_signature,
                ),
                operations=(
                    TemplateOperation(
                        "append_python_nodes", "path_000", template_hash, "\n\n"
                    ),
                ),
            )
        ),
    )
    for manifest in invalid_manifests:
        _put_local_recipe(atlas_environment.store, manifest)
        result = atlas_environment.service.prepare(
            workspace=str(atlas_environment.root),
            snapshot_id=snapshot.snapshot_id,
            intent_id=manifest.intent_id,
            language="python",
            target_paths=("tests/test_feature.py",),
        )
        assert result.status is AtlasStatus.NO_VERIFIED_RECIPE
        assert result.packet is None
        assert "malformed_local_recipe" in result.reasons


def test_local_observed_manifest_requires_extractor_shape_and_source_hash(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    valid = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id="python.observed-local",
            repository_signature=repository_signature,
        ),
    )
    ready = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.observed-local",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert ready.status is AtlasStatus.READY
    assert ready.packet is not None
    assert ready.packet.recipe_id == valid.recipe_id

    tampered = replace(
        _observed_manifest(
            recipe_id="",
            intent_id="python.tampered-local-hash",
            repository_signature=repository_signature,
        ),
        manifest_hash="sha256:" + "f" * 64,
    )
    _put_local_recipe(atlas_environment.store, tampered)
    rejected = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.tampered-local-hash",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert rejected.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert rejected.packet is None
    assert "malformed_local_recipe" in rejected.reasons

    metadata_tampered = _observed_manifest(
        recipe_id="",
        intent_id="python.tampered-local-metadata",
        repository_signature=repository_signature,
    )
    metadata_root = AtlasNode.create(
        NodeKind.RECIPE,
        _recipe_payload(metadata_tampered),
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(metadata_tampered.manifest_hash,),
    )
    atlas_environment.store.put_nodes((metadata_root,))
    atlas_environment.store.put_recipe(
        replace(metadata_tampered, recipe_id=metadata_root.node_id)
    )
    metadata_rejected = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.tampered-local-metadata",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert metadata_rejected.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert metadata_rejected.packet is None
    assert "malformed_local_recipe" in metadata_rejected.reasons

    created_at_tampered = _observed_manifest(
        recipe_id="",
        intent_id="python.tampered-local-created-at",
        repository_signature=repository_signature,
    )
    created_at_root = AtlasNode.create(
        NodeKind.RECIPE,
        _recipe_payload(created_at_tampered),
        extractor_id="python-ast",
        extractor_version="1",
        provenance="observed",
        source_hashes=(created_at_tampered.manifest_hash,),
        created_at="2026-07-29T00:00:00Z",
    )
    atlas_environment.store.put_nodes((created_at_root,))
    atlas_environment.store.put_recipe(
        replace(created_at_tampered, recipe_id=created_at_root.node_id)
    )
    created_at_rejected = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.tampered-local-created-at",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert created_at_rejected.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert created_at_rejected.packet is None
    assert "malformed_local_recipe" in created_at_rejected.reasons


def test_graph_query_rejects_sentinel_local_discovery_before_traversal(
    atlas_environment: AtlasEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers = tuple(f"sha256:{index:064x}" for index in range(MAX_GRAPH_NODES + 1))
    limits: list[int] = []

    def sentinel_recipes(_intent_id: str, *, limit: int) -> tuple[str, ...]:
        limits.append(limit)
        return identifiers

    def fail_if_traversed(*_args, **_kwargs):
        raise AssertionError("sentinel roots must fail before graph traversal")

    monkeypatch.setattr(atlas_environment.store, "recipes_for_intent", sentinel_recipes)
    monkeypatch.setattr(atlas_environment.store, "graph_query", fail_if_traversed)

    with pytest.raises(AtlasError) as error:
        atlas_environment.service.graph_query(intent_id="python.discovery-sentinel")

    assert error.value.code == "too_many_roots"
    assert limits == [MAX_GRAPH_NODES + 1]


def test_prepare_stops_before_hydrating_unbounded_local_discovery(
    atlas_environment: AtlasEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    identifiers = tuple(f"sha256:{index:064x}" for index in range(MAX_GRAPH_NODES + 1))
    limits: list[int] = []

    def sentinel_recipes(_intent_id: str, *, limit: int) -> tuple[str, ...]:
        limits.append(limit)
        return identifiers

    monkeypatch.setattr(atlas_environment.store, "recipes_for_intent", sentinel_recipes)

    def fail_if_hydrated(*_args, **_kwargs):
        raise AssertionError("local recipes must not be hydrated beyond the bound")

    monkeypatch.setattr(atlas_environment.store, "graph_query", fail_if_hydrated)
    result = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.discovery-limit",
        language="python",
        target_paths=("tests/test_feature.py",),
    )

    assert result.status is AtlasStatus.AMBIGUOUS_MATCH
    assert result.packet is None
    assert result.candidate_recipe_ids == ()
    assert result.reasons == ("candidate_limit_exceeded",)
    assert limits == [MAX_GRAPH_NODES + 1]


def test_local_payload_and_template_body_are_fail_closed(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    facts = atlas_environment.index.snapshot_facts(
        atlas_environment.root, snapshot.snapshot_id
    )
    repository_signature = structural_repository_signature(
        facts, language="python", framework=None
    )
    malformed = _manifest(recipe_id="", intent_id="python.malformed")
    payload = _recipe_payload(malformed)
    payload["slots"] = "not-a-list"
    malicious = AtlasNode.create(
        NodeKind.RECIPE,
        payload,
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(malformed.manifest_hash,),
    )
    atlas_environment.store.put_nodes((malicious,))
    atlas_environment.store.put_recipe(replace(malformed, recipe_id=malicious.node_id))
    unknown_payload = _recipe_payload(malformed)
    unknown_payload["unexpected"] = "not-allowed"
    unknown = AtlasNode.create(
        NodeKind.RECIPE,
        unknown_payload,
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(malformed.manifest_hash,),
    )
    atlas_environment.store.put_nodes((unknown,))
    atlas_environment.store.put_recipe(replace(malformed, recipe_id=unknown.node_id))
    malformed_result = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.malformed",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert malformed_result.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert malformed_result.packet is None

    secret_payload = _recipe_payload(malformed)
    secret_payload["provenance_source"] = "sk-abcdefghijk"
    secret_recipe = AtlasNode.create(
        NodeKind.RECIPE,
        secret_payload,
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(malformed.manifest_hash,),
    )
    atlas_environment.store.put_nodes((secret_recipe,))
    atlas_environment.store.put_recipe(
        replace(malformed, recipe_id=secret_recipe.node_id)
    )
    schema_payload = _recipe_payload(malformed)
    schema_payload["schema_version"] = "2"
    schema_recipe = AtlasNode.create(
        NodeKind.RECIPE,
        schema_payload,
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(malformed.manifest_hash,),
    )
    atlas_environment.store.put_nodes((schema_recipe,))
    atlas_environment.store.put_recipe(
        replace(malformed, recipe_id=schema_recipe.node_id)
    )
    hardened_malformed = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.malformed",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert hardened_malformed.status is AtlasStatus.NO_VERIFIED_RECIPE
    assert hardened_malformed.packet is None

    marker = "ATLAS07_TEMPLATE_BODY_sk-abcdefghijk"
    template_hash = canonical_hash({"template": "safe-reference"})
    local = _put_local_recipe(
        atlas_environment.store,
        _with_observed_manifest_hash(
            replace(
                _observed_manifest(
                    recipe_id="",
                    intent_id="python.unsafe-template",
                    repository_signature=repository_signature,
                ),
                operations=(
                    TemplateOperation("append_python_nodes", "path_000", template_hash),
                ),
            )
        ),
        graph=(
            (
                NodeKind.CODE_TEMPLATE,
                {
                    "template_hash": template_hash,
                    "kind": "append_python_nodes",
                    "body": marker,
                },
                EdgeRelation.HAS_IMPLEMENTATION,
            ),
        ),
    )
    graph = atlas_environment.service.graph_query(
        roots=(local.recipe_id,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert graph.truncated is True
    assert marker not in canonical_json(graph.to_dict())
    rejected = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.unsafe-template",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert rejected.status is AtlasStatus.EVIDENCE_INCOMPLETE
    assert rejected.packet is None

    evidence_marker = "ATLAS07_SOURCE_EVIDENCE_BODY"
    unsafe_evidence = _put_local_recipe(
        atlas_environment.store,
        _observed_manifest(
            recipe_id="",
            intent_id="python.unsafe-evidence",
            repository_signature=repository_signature,
        ),
        graph=(
            (
                NodeKind.SOURCE_EVIDENCE,
                {"body": evidence_marker},
                EdgeRelation.DERIVED_FROM,
            ),
        ),
    )
    evidence_graph = atlas_environment.service.graph_query(
        roots=(unsafe_evidence.recipe_id,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert evidence_graph.truncated is True
    assert evidence_marker not in canonical_json(evidence_graph.to_dict())
    evidence_packet = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.unsafe-evidence",
        language="python",
        target_paths=("tests/test_feature.py",),
    )
    assert evidence_packet.status is AtlasStatus.EVIDENCE_INCOMPLETE
    assert evidence_packet.packet is None


def test_graph_query_merges_filters_and_propagates_local_truncation(
    atlas_environment: AtlasEnvironment,
) -> None:
    constrained = _put_local_recipe(
        atlas_environment.store,
        _manifest(recipe_id="", intent_id="python.graph"),
        graph=(
            (
                NodeKind.CONSTRAINT,
                ConstraintSpec("path_suffix", "", ".py").to_dict(),
                EdgeRelation.CONSTRAINED_BY,
            ),
            (
                NodeKind.SOURCE_EVIDENCE,
                {"source": "fixture"},
                EdgeRelation.DERIVED_FROM,
            ),
        ),
    )
    filtered = atlas_environment.service.graph_query(
        roots=(constrained.recipe_id,),
        node_kinds=(NodeKind.CONSTRAINT,),
        relations=(EdgeRelation.CONSTRAINED_BY,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert {node.kind for node in filtered.nodes} == {
        NodeKind.RECIPE,
        NodeKind.CONSTRAINT,
    }
    assert {edge.relation for edge in filtered.edges} == {EdgeRelation.CONSTRAINED_BY}
    aliased = atlas_environment.service.graph_query(
        roots=(constrained.recipe_id,),
        kinds=(NodeKind.CONSTRAINT,),
        relations=(EdgeRelation.CONSTRAINED_BY,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert aliased == filtered
    assert atlas_environment.service.graph_query(roots=()).nodes == ()
    assert atlas_environment.service.graph_query(intent_id="").nodes == ()
    with pytest.raises(AtlasError):
        atlas_environment.service.graph_query(roots=(), node_kinds=("not-a-kind",))
    with pytest.raises(AtlasError):
        atlas_environment.service.graph_query(roots=("bad-id",))
    with pytest.raises(AtlasError):
        atlas_environment.service.graph_query(
            roots=(constrained.recipe_id,), node_kinds=("not-a-kind",)
        )
    with pytest.raises(AtlasError):
        atlas_environment.service.graph_query(
            roots=tuple(f"sha256:{index:064x}" for index in range(MAX_GRAPH_NODES + 1))
        )

    crowded = _put_local_recipe(
        atlas_environment.store,
        _manifest(recipe_id="", intent_id="python.crowded"),
        graph=tuple(
            (
                NodeKind.SOURCE_EVIDENCE,
                {"ordinal": index},
                EdgeRelation.DERIVED_FROM,
            )
            for index in range(MAX_GRAPH_NODES)
        ),
    )
    first = atlas_environment.service.graph_query(
        roots=(crowded.recipe_id,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    repeated = atlas_environment.service.graph_query(
        roots=(crowded.recipe_id,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert first.truncated is True
    assert first == repeated
    assert len(first.nodes) <= MAX_GRAPH_NODES
    assert len(first.edges) <= MAX_GRAPH_EDGES


def test_graph_query_enforces_global_edge_depth_byte_and_layer_bounds(
    atlas_environment: AtlasEnvironment,
) -> None:
    local = _put_local_recipe(
        atlas_environment.store,
        _manifest(recipe_id="", intent_id="python.graph-bounds"),
        graph=(
            (
                NodeKind.TEST_SPEC,
                {"kind": "fixture", "expected_exit_code": 0},
                EdgeRelation.VERIFIED_BY,
            ),
            (
                NodeKind.SOURCE_EVIDENCE,
                {"kind": "fixture", "ordinal": 1},
                EdgeRelation.DERIVED_FROM,
            ),
        ),
    )
    baseline = atlas_environment.service.graph_query(
        roots=(local.recipe_id,),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    recipe_node = next(
        node for node in baseline.nodes if node.node_id == local.recipe_id
    )
    test_node = next(node for node in baseline.nodes if node.kind is NodeKind.TEST_SPEC)
    leaf = AtlasNode.create(NodeKind.SOURCE_EVIDENCE, {"kind": "depth-leaf"})
    atlas_environment.store.put_nodes((leaf,))
    atlas_environment.store.put_edges(
        (AtlasEdge.create(EdgeRelation.TESTS, test_node, leaf),)
    )

    edge_limited = atlas_environment.service.graph_query(
        roots=(local.recipe_id,),
        max_nodes=10,
        max_edges=1,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    depth_limited = atlas_environment.service.graph_query(
        roots=(local.recipe_id,),
        max_nodes=10,
        max_edges=10,
        max_depth=1,
        byte_budget=MAX_PACKET_BYTES,
    )
    root_only_budget = len(
        canonical_json(
            {"nodes": [recipe_node.to_dict()], "edges": [], "truncated": True}
        ).encode("utf-8")
    )
    byte_limited = atlas_environment.service.graph_query(
        roots=(local.recipe_id,),
        max_nodes=10,
        max_edges=10,
        max_depth=4,
        byte_budget=root_only_budget,
    )
    bundled = BundledRecipeLoader(ASSETS).load()[0]
    merged = atlas_environment.service.graph_query(
        roots=(local.recipe_id, bundled.recipe_id),
        max_nodes=MAX_GRAPH_NODES,
        max_edges=MAX_GRAPH_EDGES,
        max_depth=4,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert edge_limited.truncated is True
    assert depth_limited.truncated is True
    assert byte_limited.truncated is True
    assert tuple(node.node_id for node in byte_limited.nodes) == (local.recipe_id,)
    assert {local.recipe_id, bundled.recipe_id} <= {
        node.node_id for node in merged.nodes
    }
    assert (
        atlas_environment.service.graph_query(
            roots=(local.recipe_id, bundled.recipe_id), max_nodes=1
        ).truncated
        is True
    )


def test_prepare_packet_budget_query_truncation_and_secret_redaction(
    atlas_environment: AtlasEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = atlas_environment.root / "tests" / "test_secret_packet.py"
    source_marker = "ATLAS07_SOURCE_BODY_MARKER"
    secret_path.write_text(
        "def test_secret_packet() -> None:\n"
        f"    marker = '{source_marker}'\n"
        "    token = 'sk-abcdefghijk'\n"
        "    bearer = 'Authorization: Bearer fixture-value'\n"
        "    private = '-----BEGIN PRIVATE KEY-----'\n"
        "    sensitive = 'SECRET_ACCESS_KEY=fixture-value'\n"
        "    assert marker\n",
        encoding="utf-8",
    )
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    prepared = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_secret_packet.py",),
        byte_budget=MAX_PACKET_BYTES,
    )
    assert prepared.status is AtlasStatus.READY
    assert prepared.packet is not None
    packet_json = canonical_json(prepared.packet.to_dict())
    for forbidden in (
        source_marker,
        "sk-abcdefghijk",
        "Bearer fixture-value",
        "BEGIN PRIVATE KEY",
        "SECRET_ACCESS_KEY=fixture-value",
    ):
        assert forbidden not in packet_json

    connection = sqlite3.connect(atlas_environment.database)
    try:
        stored_packet = connection.execute(
            "SELECT packet_json FROM atlas_packet_receipts WHERE packet_id=?",
            (prepared.packet.packet_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored_packet == packet_json

    exact_budget = len(packet_json.encode("utf-8"))
    exact = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_secret_packet.py",),
        byte_budget=exact_budget,
    )
    assert exact.status is AtlasStatus.READY
    assert exact.packet is not None
    too_small = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.pytest-regression",
        language="python",
        framework="pytest",
        target_paths=("tests/test_secret_packet.py",),
        byte_budget=exact_budget - 1,
    )
    assert too_small.status is AtlasStatus.EVIDENCE_INCOMPLETE
    assert too_small.packet is None

    original_query = atlas_environment.index.query

    def gapped_query(*args, **kwargs):
        result = original_query(*args, **kwargs)
        return replace(
            result,
            gaps=(
                CoverageGap(
                    "tests/test_secret_packet.py", "SAFE_GAP", "ATLAS07_GAP_MESSAGE"
                ),
            ),
        )

    monkeypatch.setattr(atlas_environment.index, "query", gapped_query)
    gapped = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert gapped.status is AtlasStatus.READY
    assert gapped.packet is not None
    gapped_json = canonical_json(gapped.packet.to_dict())
    assert "SAFE_GAP" in gapped.packet.gaps
    assert "ATLAS07_GAP_MESSAGE" not in gapped_json

    def truncated_query(*args, **kwargs):
        return replace(original_query(*args, **kwargs), truncated=True)

    monkeypatch.setattr(atlas_environment.index, "query", truncated_query)
    truncated = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert truncated.status is AtlasStatus.EVIDENCE_INCOMPLETE
    assert truncated.packet is None

    service_source = (
        (ROOT / "mcp-tools" / "devkit_atlas" / "service.py")
        .read_text(encoding="utf-8")
        .casefold()
    )
    for forbidden in ("codegraph", "openai", "embedding", "vector"):
        assert forbidden not in service_source


def test_render_is_deterministic_and_never_writes_the_workspace(
    atlas_environment: AtlasEnvironment,
) -> None:
    (atlas_environment.root / "tests" / "test_feature.py").write_bytes(
        b"import json\n\n\ndef test_feature() -> None:\n    assert json.loads('1') == 1\n"
    )
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    prepared = _prepare_pytest(
        atlas_environment,
        snapshot_id=snapshot.snapshot_id,
    )
    assert prepared.status is AtlasStatus.READY
    assert prepared.packet is not None
    before = {
        path.relative_to(atlas_environment.root).as_posix(): path.read_bytes()
        for path in atlas_environment.root.rglob("*")
        if path.is_file()
    }
    bindings = {
        "test_path": "tests/test_feature.py",
        "test_name": "test_rendered_feature",
        "test_body": "assert True",
    }

    first = atlas_environment.service.render(
        str(atlas_environment.root),
        snapshot.snapshot_id,
        prepared.packet.packet_id,
        bindings,
    )
    second = atlas_environment.service.render(
        str(atlas_environment.root),
        snapshot.snapshot_id,
        prepared.packet.packet_id,
        bindings,
    )

    assert first.status is AtlasStatus.READY
    assert first == second
    assert first.patch_candidate.startswith("--- a/tests/test_feature.py\n")
    assert first.patch_hash.startswith("sha256:")
    assert before == {
        path.relative_to(atlas_environment.root).as_posix(): path.read_bytes()
        for path in atlas_environment.root.rglob("*")
        if path.is_file()
    }


def test_render_rejects_two_path_bindings_that_collide() -> None:
    manifest = RecipeManifest(
        recipe_id="sha256:" + "a" * 64,
        recipe_key="python.binding-collision",
        version=1,
        intent_id="python.binding-collision",
        language_name="python",
        language_extractor_version="1",
        repository_signature="",
        layer="local",
        manifest_hash="sha256:" + "b" * 64,
        slots=(
            SlotSpec("path_000", "relative_python_path"),
            SlotSpec("path_001", "relative_python_path"),
        ),
        provenance_kind="observed",
        provenance_source="accepted_task",
    )

    with pytest.raises(AtlasError) as captured:
        validate_bindings(
            manifest,
            {"path_000": "src/new.py", "path_001": "src/new.py"},
        )

    assert captured.value.code == "path_case_collision"


def test_render_invalid_request_never_echoes_a_secret_like_packet_id(
    atlas_environment: AtlasEnvironment,
) -> None:
    result = atlas_environment.service.render(
        str(atlas_environment.root),
        "not-a-snapshot",
        "sk-atlas-secret-token",
        {},
    )

    assert result.status is AtlasStatus.RENDER_INVALID
    assert result.packet_id == ""
    assert "sk-" not in canonical_json(result.to_dict())


def test_prepend_places_rendered_body_immediately_after_a_docstring() -> None:
    manifest = RecipeManifest(
        recipe_id="sha256:" + "a" * 64,
        recipe_key="python.docstring-prepend",
        version=1,
        intent_id="python.docstring-prepend",
        language_name="python",
        language_extractor_version="1",
        repository_signature="",
        layer="bundled",
        manifest_hash="sha256:" + "b" * 64,
        slots=(
            SlotSpec("source_path", "relative_python_path"),
            SlotSpec("target_symbol", "python_qualified_name"),
            SlotSpec("predicate", "python_expression"),
            SlotSpec("exception", "python_expression"),
        ),
        operations=(
            TemplateOperation(
                "prepend_function_body",
                "source_path",
                "sha256:" + "c" * 64,
                target_symbol_slot="target_symbol",
            ),
        ),
        provenance_kind="bundled",
        provenance_source="fixture",
    )
    source = 'def guard(value: int) -> int:\n    """A guard."""\n\n    return value\n'

    rendered = render_patch(
        manifest,
        {
            "source_path": "src/guard.py",
            "target_symbol": "guard",
            "predicate": "value > 0",
            "exception": "ValueError('bad')",
        },
        source_files={"src/guard.py": source.encode("utf-8")},
        snapshot_paths=("src/guard.py",),
        template_reader=lambda _hash: (
            b"if not (${predicate}):\n    raise ${exception}\n"
        ),
    )

    assert "@@ -2,0 +3,2 @@" in rendered.patch_candidate


def test_render_supports_a_bundled_prepend_with_docstring_target(
    atlas_environment: AtlasEnvironment,
) -> None:
    (atlas_environment.root / "src" / "guards.py").write_bytes(
        b"def guarded(value: int) -> int:\n"
        b'    """Return an accepted value."""\n'
        b"    return value\n"
    )
    (atlas_environment.root / "tests" / "test_feature.py").write_bytes(
        b"def test_feature() -> None:\n    assert True\n"
    )
    snapshot = atlas_environment.index.sync(atlas_environment.root)
    prepared = atlas_environment.service.prepare(
        workspace=str(atlas_environment.root),
        snapshot_id=snapshot.snapshot_id,
        intent_id="python.validation-guard",
        language="python",
        target_paths=("src/guards.py", "tests/test_feature.py"),
        target_symbols=("guarded",),
    )
    assert prepared.status is AtlasStatus.READY
    assert prepared.packet is not None

    result = atlas_environment.service.render(
        str(atlas_environment.root),
        snapshot.snapshot_id,
        prepared.packet.packet_id,
        {
            "source_path": "src/guards.py",
            "target_symbol": "guarded",
            "predicate_expression": "value > 0",
            "exception_expression": "ValueError('bad')",
            "test_path": "tests/test_feature.py",
        },
    )

    assert result.status is AtlasStatus.READY
    assert result.patch_candidate.startswith("--- a/src/guards.py\n")
    assert "@@ -2,0 +3,2 @@" in result.patch_candidate


def test_render_substitutes_each_placeholder_exactly_once() -> None:
    manifest = RecipeManifest(
        recipe_id="sha256:" + "a" * 64,
        recipe_key="python.single-pass-substitution",
        version=1,
        intent_id="python.single-pass-substitution",
        language_name="python",
        language_extractor_version="1",
        repository_signature="",
        layer="local",
        manifest_hash="sha256:" + "b" * 64,
        slots=(
            SlotSpec("path", "relative_python_path"),
            SlotSpec("first", "single_line_text"),
            SlotSpec("second", "single_line_text"),
        ),
        operations=(
            TemplateOperation("create_python_file", "path", "sha256:" + "c" * 64),
        ),
        provenance_kind="observed",
        provenance_source="fixture",
    )

    rendered = render_patch(
        manifest,
        {"path": "generated.py", "first": "${second}", "second": "injected"},
        source_files={},
        snapshot_paths=(),
        template_reader=lambda _hash: b'first = "${first}"\nsecond = "${second}"\n',
    )

    assert '+first = "${second}"' in rendered.patch_candidate
    assert '+second = "injected"' in rendered.patch_candidate


def test_render_rejects_text_slot_interpolation_inside_test_argv() -> None:
    manifest = RecipeManifest(
        recipe_id="sha256:" + "a" * 64,
        recipe_key="python.argv-injection",
        version=1,
        intent_id="python.argv-injection",
        language_name="python",
        language_extractor_version="1",
        repository_signature="",
        layer="local",
        manifest_hash="sha256:" + "b" * 64,
        slots=(
            SlotSpec("path", "relative_python_path"),
            SlotSpec("text", "single_line_text"),
        ),
        tests=(AtlasTestSpec(("python", "-c", "${text}")),),
        operations=(
            TemplateOperation("create_python_file", "path", "sha256:" + "c" * 64),
        ),
        provenance_kind="observed",
        provenance_source="fixture",
    )

    with pytest.raises(AtlasError) as captured:
        render_patch(
            manifest,
            {"path": "generated.py", "text": "import os; os.system('bad')"},
            source_files={},
            snapshot_paths=(),
            template_reader=lambda _hash: b'payload = "${text}"\n',
        )

    assert captured.value.code == "test_spec_invalid"


def test_render_rejects_an_unmatched_placeholder_closer() -> None:
    manifest = RecipeManifest(
        recipe_id="sha256:" + "a" * 64,
        recipe_key="python.placeholder-closer",
        version=1,
        intent_id="python.placeholder-closer",
        language_name="python",
        language_extractor_version="1",
        repository_signature="",
        layer="local",
        manifest_hash="sha256:" + "b" * 64,
        slots=(
            SlotSpec("path", "relative_python_path"),
            SlotSpec("name", "single_line_text"),
        ),
        operations=(
            TemplateOperation("create_python_file", "path", "sha256:" + "c" * 64),
        ),
        provenance_kind="observed",
        provenance_source="fixture",
    )

    with pytest.raises(AtlasError) as captured:
        render_patch(
            manifest,
            {"path": "generated.py", "name": "safe"},
            source_files={},
            snapshot_paths=(),
            template_reader=lambda _hash: b'value = "${name}}"\n',
        )

    assert captured.value.code == "template_placeholder_invalid"


def test_local_recipe_placeholder_validator_rejects_an_unmatched_closer() -> None:
    with pytest.raises(AtlasError) as captured:
        _local_placeholder_names("${path_000}}")

    assert captured.value.code == "malformed_local_recipe"
