"""Contract tests for deterministic Code Atlas matching and preparation."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.canonical import canonical_hash, canonical_json
from code_atlas.matching import (
    MatchClass,
    select_recipe,
    structural_repository_signature,
)
from code_atlas.models import (
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
)
from code_atlas.recipes import BundledRecipeLoader
from code_atlas.security import (
    MAX_CHANGED_FILES,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
)
from code_atlas.service import CodeAtlasService
from code_atlas.store import AtlasStore
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
ASSETS = ROOT / "skills" / "code-atlas" / "assets"


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
    operations: tuple[TemplateOperation, ...] = (),
    superseded_ids: tuple[str, ...] = (),
    quarantine_state: str | None = None,
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
        "operations": [item.to_dict() for item in operations],
        "superseded_ids": list(superseded_ids),
        "quarantine_state": quarantine_state,
    }
    return RecipeManifest(
        recipe_id=recipe_id,
        recipe_key=intent_id,
        version=version,
        intent_id=intent_id,
        language_name=language,
        language_extractor_version="1",
        repository_signature=repository_signature,
        layer=layer,
        manifest_hash=canonical_hash(manifest_data),
        framework_name=framework,
        framework_specifier=framework_specifier,
        slots=slots,
        constraints=constraints,
        operations=operations,
        provenance_kind="observed",
        provenance_source="fixture",
        superseded_ids=superseded_ids,
        quarantine_state=quarantine_state,
    )


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
        extractor_id="fixture",
        extractor_version="1",
        provenance="observed",
        source_hashes=(manifest.manifest_hash,),
        quarantine_state=manifest.quarantine_state,
    )
    registered = replace(manifest, recipe_id=recipe_node.node_id)
    children = tuple(
        AtlasNode.create(kind, payload, provenance="observed")
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
    service: CodeAtlasService


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
    service = CodeAtlasService(store, BundledRecipeLoader(ASSETS), index)
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
    assert first.packet.next_action == "code_atlas_render"
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
        facts, language="python", framework="pytest"
    )
    local = _put_local_recipe(
        atlas_environment.store,
        _manifest(
            recipe_id="",
            intent_id="python.pytest-regression",
            version=2,
            framework="pytest",
            framework_specifier=">=7,<9",
            repository_signature=signature,
        ),
    )
    preferred = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert preferred.status is AtlasStatus.READY
    assert preferred.packet is not None
    assert preferred.packet.recipe_id == local.recipe_id

    second = _put_local_recipe(
        atlas_environment.store,
        _manifest(
            recipe_id="",
            intent_id="python.pytest-regression",
            framework="pytest",
            framework_specifier=">=7,<9",
            repository_signature=signature,
        ),
    )
    tied = _prepare_pytest(atlas_environment, snapshot_id=snapshot.snapshot_id)
    assert tied.status is AtlasStatus.AMBIGUOUS_MATCH
    assert tied.packet is None
    assert tied.candidate_recipe_ids == tuple(
        sorted((local.recipe_id, second.recipe_id))
    )


def test_local_payload_and_template_body_are_fail_closed(
    atlas_environment: AtlasEnvironment,
) -> None:
    snapshot = atlas_environment.index.sync(atlas_environment.root)
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
        _manifest(
            recipe_id="",
            intent_id="python.unsafe-template",
            slots=(SlotSpec("test_path", "relative_python_path"),),
            operations=(
                TemplateOperation("append_python_nodes", "test_path", template_hash),
            ),
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
        _manifest(recipe_id="", intent_id="python.unsafe-evidence"),
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
        (ROOT / "mcp-tools" / "code_atlas" / "service.py")
        .read_text(encoding="utf-8")
        .casefold()
    )
    for forbidden in ("codegraph", "openai", "embedding", "vector"):
        assert forbidden not in service_source
