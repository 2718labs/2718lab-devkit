"""Contract tests for the local Code Atlas SQLite/CAS store."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
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
from code_atlas.canonical import canonical_json
from code_atlas.store import AtlasStore, StoreConflictError
from code_atlas.security import MAX_PACKET_BYTES


def store_at(tmp_path: Path) -> AtlasStore:
    return AtlasStore(tmp_path / "atlas.sqlite", tmp_path / "cas")


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
