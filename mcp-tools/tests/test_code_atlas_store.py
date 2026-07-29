"""Contract tests for the local Code Atlas SQLite/CAS store."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.models import (
    AtlasEdge,
    AtlasNode,
    AtlasStatus,
    EdgeRelation,
    ImplementationPacket,
    IngestionReceipt,
    NodeKind,
    RecipeManifest,
    TemplateOperation,
    SlotSpec,
    ConstraintSpec,
    DependencySpec,
    TestSpec as AtlasTestSpec,
)
from code_atlas.canonical import canonical_hash, canonical_json
from code_atlas import store as store_module
from code_atlas.store import AtlasStore, StoreConflictError
from code_atlas.security import MAX_GRAPH_NODES, MAX_PACKET_BYTES, MAX_TEMPLATE_BYTES


def store_at(tmp_path: Path) -> AtlasStore:
    return AtlasStore(tmp_path / "atlas.sqlite", tmp_path / "cas")


def _store_discovery_recipe(
    store: AtlasStore,
    *,
    index: int,
    intent_id: str,
    language: str = "python",
    framework: str | None = None,
    state: str | None = None,
) -> str:
    recipe = AtlasNode.create(
        NodeKind.RECIPE,
        {
            "discovery_index": index,
            "framework": framework or "",
            "intent_id": intent_id,
            "language": language,
            "state": state or "ready",
        },
    )
    store.put_nodes((recipe,))
    return store.put_recipe(
        RecipeManifest(
            recipe.node_id,
            f"discovery-{index}",
            1,
            intent_id,
            language,
            "1",
            "repository",
            "local",
            f"manifest-{index}",
            framework,
            quarantine_state=state,
        )
    )


def test_recipe_discovery_is_ordered_and_limited(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    intent_id = "python.discovery-order"
    identifiers = tuple(
        _store_discovery_recipe(store, index=index, intent_id=intent_id)
        for index in (5, 1, 4, 2, 3)
    )

    assert (
        store.recipes_for_intent(intent_id, limit=3) == tuple(sorted(identifiers))[:3]
    )
    store.close()


def test_recipe_discovery_exact_boundary_and_default_sentinel(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    exact_intent = "python.discovery-exact-boundary"
    overflow_intent = "python.discovery-overflow"
    exact_identifiers = tuple(
        _store_discovery_recipe(store, index=index, intent_id=exact_intent)
        for index in range(MAX_GRAPH_NODES)
    )
    overflow_identifiers = tuple(
        _store_discovery_recipe(
            store,
            index=index,
            intent_id=overflow_intent,
        )
        for index in range(MAX_GRAPH_NODES + 2)
    )

    assert store.recipes_for_intent(exact_intent) == tuple(sorted(exact_identifiers))
    assert (
        store.recipes_for_intent(overflow_intent)
        == tuple(sorted(overflow_identifiers))[: MAX_GRAPH_NODES + 1]
    )
    store.close()


@pytest.mark.parametrize(
    "limit",
    (True, False, 0, -1, 1.5, "1", MAX_GRAPH_NODES + 2),
)
def test_recipe_discovery_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    store = store_at(tmp_path)

    with pytest.raises(ValueError, match="invalid recipe discovery limit"):
        store.recipes_for_intent("python.discovery-invalid-limit", limit=limit)
    store.close()


def test_recipe_discovery_combines_filters_with_limit_and_reopens(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    intent_id = "python.discovery-filtered"
    matching = tuple(
        _store_discovery_recipe(
            store,
            index=index,
            intent_id=intent_id,
            language="python",
            framework="pytest",
            state="ready",
        )
        for index in (2, 1, 3)
    )
    _store_discovery_recipe(
        store,
        index=4,
        intent_id=intent_id,
        language="python",
        framework="pytest",
        state="quarantined",
    )
    _store_discovery_recipe(
        store,
        index=5,
        intent_id=intent_id,
        language="python",
        framework="other",
        state="ready",
    )
    _store_discovery_recipe(
        store,
        index=6,
        intent_id=intent_id,
        language="ruby",
        framework="pytest",
        state="ready",
    )
    store.close()

    reopened = store_at(tmp_path)
    assert (
        reopened.recipes_for_intent(
            intent_id,
            language="python",
            framework="pytest",
            state="ready",
            limit=2,
        )
        == tuple(sorted(matching))[:2]
    )
    reopened.close()


def test_schema_wal_foreign_keys_and_reopen(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.schema_version() == 1
    assert store.journal_mode() == "wal"
    assert store.foreign_keys_enabled() is True
    store.close()
    reopened = store_at(tmp_path)
    assert reopened.schema_version() == 1
    connection = sqlite3.connect(tmp_path / "atlas.sqlite")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables == {
        "atlas_metadata",
        "atlas_nodes",
        "atlas_edges",
        "atlas_recipes",
        "atlas_recipe_nodes",
        "atlas_recipe_edges",
        "atlas_blobs",
        "atlas_packet_receipts",
        "atlas_ingestion_receipts",
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
        )
    }
    assert indexes == {"atlas_edges_source", "atlas_edges_target", "atlas_recipe_match"}
    reopened.close()


def test_nodes_recompute_identity_reject_tamper_and_allow_mutable_metadata(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    node = AtlasNode.create(NodeKind.INTENT, {"intent_id": "a"}, created_at="one")
    assert store.put_nodes((node,)) == (node,)
    assert store.put_nodes((node,)) == (node,)
    mutable = replace(
        node,
        created_at="two",
        superseded_at="three",
        quarantine_state="accepted",
    )
    assert store.put_nodes((mutable,)) == (mutable,)
    with pytest.raises(StoreConflictError, match="node identity"):
        store.put_nodes((replace(node, payload=(("intent_id", "other"),)),))
    store.close()


def test_all_relations_edges_reopen_and_foreign_endpoints_rejected(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    pairs = [
        (EdgeRelation.SOLVES, NodeKind.RECIPE, NodeKind.INTENT),
        (EdgeRelation.DERIVED_FROM, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.HAS_IMPLEMENTATION, NodeKind.RECIPE, NodeKind.CODE_TEMPLATE),
        (EdgeRelation.HAS_SLOT, NodeKind.RECIPE, NodeKind.ADAPTATION_SLOT),
        (EdgeRelation.CONSTRAINED_BY, NodeKind.RECIPE, NodeKind.CONSTRAINT),
        (EdgeRelation.REQUIRES, NodeKind.RECIPE, NodeKind.DEPENDENCY),
        (EdgeRelation.VERIFIED_BY, NodeKind.RECIPE, NodeKind.TEST_SPEC),
        (EdgeRelation.CHANGES, NodeKind.TASK_EPISODE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.TESTS, NodeKind.TEST_SPEC, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.SUPERSEDES, NodeKind.RECIPE, NodeKind.RECIPE),
        (EdgeRelation.BUNDLED_AS, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
    ]
    edges = []
    for relation, source_kind, target_kind in pairs:
        source = AtlasNode.create(source_kind, {"source": relation.value})
        target = AtlasNode.create(target_kind, {"target": relation.value})
        store.put_nodes((source, target))
        edges.append(AtlasEdge.create(relation, source, target))
    assert store.put_edges(tuple(edges)) == tuple(edges)
    assert store.put_edges((replace(edges[0], created_at="later"),)) == (
        replace(edges[0], created_at="later"),
    )
    with pytest.raises(StoreConflictError, match="(identity|endpoint)"):
        store.put_edges((replace(edges[0], source_id="sha256:" + "a" * 64),))
    store.close()
    reopened = store_at(tmp_path)
    roots = tuple(edge.source_id for edge in edges)
    assert (
        len(
            reopened.graph_query(
                root_node_ids=roots,
                max_nodes=200,
                max_edges=400,
                max_depth=1,
                byte_budget=MAX_PACKET_BYTES,
            ).edges
        )
        == 11
    )
    reopened.close()


def test_verified_cas_and_receipts_are_idempotent_or_conflicting(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    digest = "sha256:" + "0" * 64
    with pytest.raises(StoreConflictError, match="blob hash"):
        store.put_blob(digest, b"verified template")
    valid = "sha256:" + hashlib.sha256(b"verified template").hexdigest()
    assert store.put_blob(valid, b"verified template") == valid
    assert store.read_blob(valid) == b"verified template"
    blob_path = tmp_path / "cas" / "sha256" / valid[7:9] / valid[9:]
    blob_path.unlink()
    with pytest.raises(StoreConflictError):
        store.read_blob(valid)
    packet = ImplementationPacket(
        "packet",
        "trace",
        "workspace",
        "snap",
        "recipe",
        ("node",),
        ("edge",),
        ({"path": "x"},),
        ("eh",),
        (TemplateOperation("replace", "path", "th"),),
        (SlotSpec("name", "single_line_text"),),
        (ConstraintSpec("kind", "subject", {"a": 1}),),
        (DependencySpec("pytest", "python", ">=8"),),
        (AtlasTestSpec(("python", "-m", "pytest")),),
        ("gap",),
        ("source",),
        ("template",),
        ("receipt",),
        "next",
    )
    store.put_packet(packet)
    store.put_packet(packet)
    with pytest.raises(StoreConflictError):
        store.put_packet(replace(packet, snapshot_id="other"))
    receipt = IngestionReceipt(
        "key",
        "sha256:" + "b" * 64,
        AtlasStatus.READY,
        "episode",
        "recipe",
        ("ok",),
        "now",
    )
    store.put_ingestion_receipt(receipt)
    store.put_ingestion_receipt(receipt)
    with pytest.raises(StoreConflictError):
        store.put_ingestion_receipt(replace(receipt, episode_id="other"))
    store.close()
    store = store_at(tmp_path)
    assert store.get_packet("packet") == packet
    assert store.get_ingestion_receipt("key") == receipt
    store.close()


def test_recipe_links_and_bounded_bidirectional_query(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    intent = AtlasNode.create(NodeKind.INTENT, {"id": "intent"})
    recipes = tuple(
        AtlasNode.create(NodeKind.RECIPE, {"id": str(index)}) for index in range(3)
    )
    store.put_nodes((intent, *recipes))
    edges = tuple(
        AtlasEdge.create(EdgeRelation.SOLVES, recipe, intent) for recipe in recipes
    )
    store.put_edges(edges)
    manifest = RecipeManifest(
        recipes[0].node_id,
        "key",
        1,
        intent.node_id,
        "python",
        "1",
        "repo",
        "local",
        "hash",
        "",
        "ready",
    )
    store.put_recipe(
        manifest,
        node_ids=(intent.node_id, recipes[0].node_id),
        edge_ids=(edges[0].edge_id,),
    )
    assert store.recipes_for_intent(intent.node_id) == (recipes[0].node_id,)
    result = store.graph_query(
        root_node_ids=(intent.node_id,),
        max_nodes=2,
        max_edges=1,
        max_depth=1,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert tuple(node.node_id for node in result.nodes) == tuple(
        sorted(node.node_id for node in result.nodes)
    )
    assert (
        len(result.nodes) == 2 and len(result.edges) == 1 and result.truncated is True
    )
    assert {edge.source_id for edge in result.edges} | {
        edge.target_id for edge in result.edges
    } <= {node.node_id for node in result.nodes}
    with pytest.raises(ValueError):
        store.graph_query(
            root_node_ids=(intent.node_id,),
            max_nodes=0,
            max_edges=1,
            max_depth=1,
            byte_budget=1,
        )
    with pytest.raises(ValueError):
        store.graph_query(
            root_node_ids=(intent.node_id,),
            max_nodes=1,
            max_edges=1,
            max_depth=0,
            byte_budget=1,
        )


def test_recipe_rejects_wrong_kind_and_missing_links(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    wrong = AtlasNode.create(NodeKind.INTENT, {"id": "wrong"})
    store.put_nodes((wrong,))
    manifest = RecipeManifest(
        wrong.node_id, "key", 1, wrong.node_id, "python", "1", "repo", "local", "hash"
    )
    with pytest.raises(StoreConflictError):
        store.put_recipe(manifest)
    recipe = AtlasNode.create(NodeKind.RECIPE, {"id": "recipe"})
    intent = AtlasNode.create(NodeKind.INTENT, {"id": "intent"})
    extra = AtlasNode.create(NodeKind.SOURCE_EVIDENCE, {"id": "extra"})
    store.put_nodes((recipe, intent, extra))
    valid_manifest = RecipeManifest(
        recipe.node_id, "key", 1, wrong.node_id, "python", "1", "repo", "local", "hash"
    )
    with pytest.raises(StoreConflictError):
        store.put_recipe(valid_manifest, node_ids=("missing",))
    assert store._conn.execute("SELECT count(*) FROM atlas_recipes").fetchone()[0] == 0
    with pytest.raises(StoreConflictError):
        store.put_recipe(
            valid_manifest, node_ids=(recipe.node_id,), edge_ids=("missing",)
        )
    assert store._conn.execute("SELECT count(*) FROM atlas_recipes").fetchone()[0] == 0
    edge = AtlasEdge.create(EdgeRelation.SOLVES, recipe, intent)
    store.put_edges((edge,))
    with pytest.raises(StoreConflictError):
        store.put_recipe(
            valid_manifest,
            node_ids=(recipe.node_id, extra.node_id),
            edge_ids=(edge.edge_id,),
        )
    assert store._conn.execute("SELECT count(*) FROM atlas_recipes").fetchone()[0] == 0
    assert (
        store._conn.execute("SELECT count(*) FROM atlas_recipe_nodes").fetchone()[0]
        == 0
    )
    assert (
        store._conn.execute("SELECT count(*) FROM atlas_recipe_edges").fetchone()[0]
        == 0
    )
    store.close()


def test_depth_boundary_marks_omitted_edge_between_chosen_nodes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    recipe = AtlasNode.create(NodeKind.RECIPE, {"id": "r"})
    test = AtlasNode.create(NodeKind.TEST_SPEC, {"id": "t"})
    evidence = AtlasNode.create(NodeKind.SOURCE_EVIDENCE, {"id": "e"})
    store.put_nodes((recipe, test, evidence))
    store.put_edges(
        (
            AtlasEdge.create(EdgeRelation.VERIFIED_BY, recipe, test),
            AtlasEdge.create(EdgeRelation.DERIVED_FROM, recipe, evidence),
            AtlasEdge.create(EdgeRelation.TESTS, test, evidence),
        )
    )
    result = store.graph_query(
        root_node_ids=(recipe.node_id,),
        max_nodes=3,
        max_edges=3,
        max_depth=1,
        byte_budget=MAX_PACKET_BYTES,
    )
    assert len(result.edges) == 2
    assert result.truncated is True


def test_byte_trim_keeps_root_before_non_root(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    root = AtlasNode.create(NodeKind.RECIPE, {"id": "root"})
    neighbor = AtlasNode.create(NodeKind.INTENT, {"id": "neighbor"})
    store.put_nodes((root, neighbor))
    store.put_edges((AtlasEdge.create(EdgeRelation.SOLVES, root, neighbor),))
    full = store.graph_query(
        root_node_ids=(root.node_id,),
        max_nodes=2,
        max_edges=1,
        max_depth=1,
        byte_budget=MAX_PACKET_BYTES,
    )
    root_only = len(
        canonical_json(
            {"nodes": [root.to_dict()], "edges": [], "truncated": True}
        ).encode()
    )
    assert root_only < len(canonical_json(full.to_dict()).encode())
    result = store.graph_query(
        root_node_ids=(root.node_id,),
        max_nodes=2,
        max_edges=1,
        max_depth=1,
        byte_budget=root_only,
    )
    assert tuple(node.node_id for node in result.nodes) == (root.node_id,)
    assert result.edges == () and result.truncated is True


def test_cas_directory_replacement_is_a_safe_conflict(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    content = b"registered"
    blob_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    store.put_blob(blob_hash, content)
    path = tmp_path / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    path.unlink()
    path.mkdir()
    with pytest.raises(StoreConflictError, match="blob"):
        store.put_blob(blob_hash, content)
    with pytest.raises(StoreConflictError, match="blob"):
        store.read_blob(blob_hash)


def test_cas_rejects_file_only_db_only_and_same_size_tamper(tmp_path: Path) -> None:
    file_only_store = store_at(tmp_path / "file-only")
    content = b"content-a"
    blob_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    path = tmp_path / "file-only" / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(StoreConflictError):
        file_only_store.put_blob(blob_hash, content)
    with pytest.raises(StoreConflictError):
        file_only_store.read_blob(blob_hash)

    db_only_store = store_at(tmp_path / "db-only")
    db_only_store.put_blob(blob_hash, content)
    db_path = tmp_path / "db-only" / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    db_path.unlink()
    with pytest.raises(StoreConflictError):
        db_only_store.put_blob(blob_hash, content)
    with pytest.raises(StoreConflictError):
        db_only_store.read_blob(blob_hash)

    tamper_store = store_at(tmp_path / "tamper")
    tamper_store.put_blob(blob_hash, content)
    tamper_path = (
        tmp_path / "tamper" / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    )
    tamper_path.write_bytes(b"content-b")
    with pytest.raises(StoreConflictError):
        tamper_store.put_blob(blob_hash, content)
    with pytest.raises(StoreConflictError):
        tamper_store.read_blob(blob_hash)


@pytest.mark.parametrize("stress_run", range(10))
def test_concurrent_first_blob_put_is_idempotent_across_connections(
    tmp_path: Path,
    stress_run: int,
) -> None:
    content = b"concurrent verified blob"
    blob_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    database_path = tmp_path / f"atlas-{stress_run}.sqlite"
    cas_root = tmp_path / f"cas-{stress_run}"
    seed = AtlasStore(database_path, cas_root)
    seed.close()
    barrier = threading.Barrier(2)

    def put_from_independent_connection() -> str:
        store = AtlasStore(database_path, cas_root)
        try:
            barrier.wait(timeout=10)
            return store.put_blob(blob_hash, content)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(lambda _: put_from_independent_connection(), range(2))
        )

    assert results == (blob_hash, blob_hash)
    reopened = AtlasStore(database_path, cas_root)
    assert reopened.read_blob(blob_hash) == content
    assert reopened._conn.execute(
        "SELECT count(*) FROM atlas_blobs WHERE blob_hash=?", (blob_hash,)
    ).fetchone() == (1,)
    reopened.close()


def _verified_packet() -> ImplementationPacket:
    """Build a packet whose public content id is independently reproducible."""
    digest = "sha256:" + "a" * 64
    provisional = ImplementationPacket(
        packet_id="",
        trace_id=digest,
        workspace="workspace",
        snapshot_id=digest,
        recipe_id=digest,
        node_ids=(digest,),
        edge_ids=(digest,),
        evidence_windows=({"content_hash": digest, "path": "src/example.py"},),
        evidence_hashes=(digest,),
        operations=(TemplateOperation("append_python_nodes", "path_000", digest),),
        slots=(SlotSpec("path_000", "relative_python_path"),),
        constraints=(),
        dependencies=(),
        tests=(AtlasTestSpec(("python", "-m", "pytest")),),
        gaps=(),
        source_hashes=(digest,),
        template_hashes=(digest,),
        receipt_hashes=(digest,),
        next_action="code_atlas_render",
    )
    payload = provisional.to_dict()
    del payload["packet_id"]
    return replace(provisional, packet_id=canonical_hash(payload))


def test_verified_packet_and_bounded_cas_reads_use_public_boundaries(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    blob = b"def rendered() -> None:\n    pass\n"
    blob_hash = "sha256:" + hashlib.sha256(blob).hexdigest()
    store.put_blob(blob_hash, blob, media_type="text/x-python")
    packet = _verified_packet()
    store.put_packet(packet)

    assert store.read_blob_verified(blob_hash, max_bytes=MAX_TEMPLATE_BYTES) == blob
    assert store.get_packet_verified(packet.packet_id) == packet
    store.close()


@pytest.mark.parametrize("replace_component", ("cas", "sha256"))
def test_verified_blob_read_rejects_an_equivalent_replaced_cas_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_component: str,
) -> None:
    store = store_at(tmp_path)
    content = b"def verified() -> None:\n    pass\n"
    blob_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    store.put_blob(blob_hash, content, media_type="text/x-python")
    cas_root = tmp_path / "cas"
    digest = blob_hash.removeprefix("sha256:")
    replacement = tmp_path / f"replacement-{replace_component}"
    replacement_blob = (
        replacement / "sha256" / digest[:2] / digest[2:]
        if replace_component == "cas"
        else replacement / digest[:2] / digest[2:]
    )
    replacement_blob.parent.mkdir(parents=True)
    replacement_blob.write_bytes(content)
    original_assert = store_module._assert_safe_existing_path
    swapped = False

    def swap_after_cas_validation(path: Path, *, require_regular: bool = False) -> None:
        nonlocal swapped
        original_assert(path, require_regular=require_regular)
        if swapped or Path(path).absolute() != cas_root.absolute():
            return
        swapped = True
        target = cas_root if replace_component == "cas" else cas_root / "sha256"
        parked = tmp_path / f"parked-{replace_component}"
        os.replace(target, parked)
        os.replace(replacement, target)

    monkeypatch.setattr(
        store_module, "_assert_safe_existing_path", swap_after_cas_validation
    )

    try:
        with pytest.raises(StoreConflictError):
            store.read_blob_verified(blob_hash, max_bytes=MAX_TEMPLATE_BYTES)
    finally:
        store.close()


def _durable_file_state(
    root: Path,
) -> dict[str, tuple[bytes, tuple[int, int, int, int, int]]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            (
                path.lstat().st_dev,
                path.lstat().st_ino,
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            ),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_open_readonly_store_uses_a_scratch_snapshot_without_touching_durable_files(
    tmp_path: Path,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    writable.close()
    before = _durable_file_state(durable)

    readonly = AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    try:
        assert readonly.schema_version() == 1
        assert _durable_file_state(durable) == before
    finally:
        readonly.close()

    after = _durable_file_state(durable)
    assert after == before
    assert not tuple(scratch.iterdir())


@pytest.mark.skipif(os.name == "posix", reason="POSIX uses its real quarantine path")
def test_readonly_scratch_lease_dispatches_cleanup_to_the_posix_quarantine_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the POSIX cleanup dispatch covered while this suite runs on Windows."""

    calls: list[str] = []

    class ProbeLease:
        _stage_descriptor = 1
        _owns_scratch = False

        def _cleanup_stage_posix_quarantined(self) -> bool:
            calls.append("stage")
            return True

    monkeypatch.setattr(store_module.os, "name", "posix")

    assert store_module._ReadonlyScratchLease.cleanup(ProbeLease()) is None
    assert calls == ["stage"]


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX atomic quarantine is platform-specific"
)
def test_open_readonly_posix_cleanup_never_deletes_a_snapshot_replaced_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot-file swap is retained in quarantine instead of being deleted."""

    durable = tmp_path / "durable"
    scratch = tmp_path / "readonly-scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    expected_snapshot = database.read_bytes()
    parked = tmp_path / "parked-snapshot.sqlite3"
    attacker = b"attacker-owned\n"
    original_rename = store_module._posix_rename_noreplace
    raced = False

    def replace_before_quarantine(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        nonlocal raced
        if not raced and old_name.startswith(".code-atlas-readonly-"):
            raced = True
            snapshot = next(scratch.glob(".code-atlas-readonly-*")) / (
                "code-atlas.sqlite3"
            )
            os.replace(snapshot, parked)
            snapshot.write_bytes(attacker)
        original_rename(old_directory_fd, old_name, new_directory_fd, new_name)

    monkeypatch.setattr(
        store_module, "_posix_rename_noreplace", replace_before_quarantine
    )

    readonly = AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    readonly.close()

    assert raced is True
    assert parked.read_bytes() == expected_snapshot
    quarantined = tuple(scratch.rglob("code-atlas.sqlite3"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == attacker


def test_open_readonly_supports_the_two_path_public_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    task_temp = tmp_path / "task-temp"
    task_temp.mkdir()
    monkeypatch.setenv("CODEX_TASK_TEMP", str(task_temp))
    writable = AtlasStore(database, cas_root)
    writable.close()

    readonly = AtlasStore.open_readonly(database, cas_root)
    try:
        assert readonly.schema_version() == 1
    finally:
        readonly.close()
    assert not tuple(task_temp.iterdir())


@pytest.mark.parametrize("unsafe_temp", ("durable", "cas", "missing"))
def test_open_readonly_two_path_api_rejects_unsafe_or_missing_default_scratch_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_temp: str,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    before_files = _durable_file_state(durable)
    before_entries = tuple(
        sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
    )
    configured_temp = {
        "durable": durable,
        "cas": cas_root,
        "missing": tmp_path / "missing-task-temp",
    }[unsafe_temp]
    monkeypatch.setenv("CODEX_TASK_TEMP", str(configured_temp))

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root)

    assert _durable_file_state(durable) == before_files
    assert (
        tuple(
            sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
        )
        == before_entries
    )
    assert not tuple(durable.rglob(".code-atlas-readonly-root-*"))
    assert unsafe_temp != "missing" or not configured_temp.exists()


@pytest.mark.parametrize("use_default_scratch", (True, False))
def test_open_readonly_never_creates_under_a_parent_replaced_before_scratch_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_default_scratch: bool,
) -> None:
    """A scratch parent swap must not redirect even the first private write."""

    durable = tmp_path / "durable"
    scratch_parent = tmp_path / "scratch-parent"
    parked = tmp_path / "parked-scratch-parent"
    durable.mkdir()
    scratch_parent.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    before_files = _durable_file_state(durable)
    before_entries = tuple(
        sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
    )
    state = {
        "attempted": False,
        "blocked": False,
        "capability_create_called": False,
        "swapped": False,
        "unsafe_mkdtemp": False,
    }

    def swap_parent_once() -> None:
        if state["attempted"]:
            return
        state["attempted"] = True
        moved_parent = False
        try:
            os.replace(scratch_parent, parked)
            moved_parent = True
            os.replace(durable, scratch_parent)
            state["swapped"] = True
        except OSError:
            state["blocked"] = True
            if moved_parent and parked.exists() and not scratch_parent.exists():
                os.replace(parked, scratch_parent)

    original_mkdtemp = tempfile.mkdtemp

    def swap_then_mkdtemp(*args: object, **kwargs: object) -> str:
        directory = kwargs.get("dir")
        if directory is not None and Path(os.fspath(directory)) == scratch_parent:
            swap_parent_once()
            if state["swapped"]:
                state["unsafe_mkdtemp"] = True
        return original_mkdtemp(*args, **kwargs)

    original_create = getattr(store_module, "_create_readonly_directory", None)

    def swap_then_capability_create(*args: object, **kwargs: object) -> Path:
        state["capability_create_called"] = True
        swap_parent_once()
        assert original_create is not None
        return original_create(*args, **kwargs)

    monkeypatch.setattr(store_module.tempfile, "mkdtemp", swap_then_mkdtemp)
    monkeypatch.setattr(
        store_module,
        "_create_readonly_directory",
        swap_then_capability_create,
        raising=False,
    )
    if use_default_scratch:
        monkeypatch.setenv("CODEX_TASK_TEMP", str(scratch_parent))

    readonly: AtlasStore | None = None
    try:
        try:
            readonly = AtlasStore.open_readonly(
                database,
                cas_root,
                **({} if use_default_scratch else {"scratch_root": scratch_parent}),
            )
        except StoreConflictError:
            pass
        finally:
            if readonly is not None:
                readonly.close()
    finally:
        if state["swapped"]:
            if scratch_parent.exists() and not durable.exists():
                os.replace(scratch_parent, durable)
            if parked.exists() and not scratch_parent.exists():
                os.replace(parked, scratch_parent)

    assert state["attempted"] is True
    assert state["unsafe_mkdtemp"] is False
    assert state["capability_create_called"] is True
    assert _durable_file_state(durable) == before_files
    assert (
        tuple(
            sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
        )
        == before_entries
    )
    assert not tuple(durable.rglob(".code-atlas-readonly-*"))


def test_open_readonly_store_sees_committed_wal_state(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    node = AtlasNode.create(NodeKind.INTENT, {"intent_id": "python.wal"})
    writable.put_nodes((node,))
    before = _durable_file_state(durable)

    readonly = AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    try:
        graph = readonly.graph_query(
            (node.node_id,),
            max_nodes=1,
            max_edges=1,
            max_depth=1,
            byte_budget=MAX_PACKET_BYTES,
        )
        assert graph.nodes == (node,)
        assert _durable_file_state(durable) == before
    finally:
        readonly.close()
        writable.close()


def test_open_readonly_fails_closed_when_the_source_database_is_replaced_mid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    writable.close()
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    original_copy = store_module._copy_snapshot_file

    def replace_after_copy(
        source: Path, destination: Path
    ) -> tuple[int, int, int, int, int]:
        copied = original_copy(source, destination)
        if source == database:
            os.replace(replacement, database)
        return copied

    monkeypatch.setattr(store_module, "_copy_snapshot_file", replace_after_copy)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)


def test_open_readonly_fails_closed_when_the_live_wal_is_replaced_mid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    writable.put_nodes((AtlasNode.create(NodeKind.INTENT, {"intent_id": "wal"}),))
    wal = Path(str(database) + "-wal")
    replacement = tmp_path / "replacement-wal"
    replacement.write_bytes(wal.read_bytes())
    original_copy = store_module._copy_snapshot_file

    def replace_after_copy(
        source: Path, destination: Path
    ) -> tuple[int, int, int, int, int]:
        copied = original_copy(source, destination)
        if source == wal:
            os.replace(replacement, wal)
        return copied

    monkeypatch.setattr(store_module, "_copy_snapshot_file", replace_after_copy)
    try:
        with pytest.raises(StoreConflictError):
            AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    finally:
        writable.close()


def test_open_readonly_rejects_a_scratch_root_that_overlaps_durable_data(
    tmp_path: Path,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "code-atlas.sqlite3"
    cas_root = durable / "code-atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    before = _durable_file_state(durable)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root, scratch_root=durable)

    assert _durable_file_state(durable) == before
