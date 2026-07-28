"""Contract tests for the local Code Atlas SQLite/CAS store."""

from __future__ import annotations

import sqlite3
import sys
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.models import (
    AtlasEdge, AtlasNode, AtlasStatus, EdgeRelation, ImplementationPacket,
    IngestionReceipt, NodeKind, RecipeManifest,
)
from code_atlas.store import AtlasStore, StoreConflictError


def store_at(tmp_path: Path) -> AtlasStore:
    return AtlasStore(tmp_path / "atlas.sqlite", tmp_path / "cas")


def test_schema_wal_foreign_keys_and_reopen(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.schema_version() == "1"
    assert store.journal_mode() == "wal"
    assert store.foreign_keys_enabled() is True
    store.close()
    reopened = store_at(tmp_path)
    assert reopened.schema_version() == "1"
    tables = {row[0] for row in sqlite3.connect(tmp_path / "atlas.sqlite").execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"atlas_metadata", "atlas_nodes", "atlas_edges", "atlas_recipes", "atlas_recipe_nodes", "atlas_recipe_edges", "atlas_blobs", "atlas_packet_receipts", "atlas_ingestion_receipts"} <= tables
    reopened.close()


def test_nodes_recompute_identity_reject_tamper_and_allow_mutable_metadata(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    node = AtlasNode.create(NodeKind.INTENT, {"intent_id": "a"}, created_at="one")
    store.put_nodes((node, replace(node, created_at="two", quarantine_state="accepted")))
    with pytest.raises(StoreConflictError, match="node identity"):
        store.put_nodes((replace(node, payload=(("intent_id", "other"),)),))
    store.close()


def test_all_relations_edges_reopen_and_foreign_endpoints_rejected(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    pairs = [
        (EdgeRelation.SOLVES, NodeKind.RECIPE, NodeKind.INTENT), (EdgeRelation.DERIVED_FROM, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.HAS_IMPLEMENTATION, NodeKind.RECIPE, NodeKind.CODE_TEMPLATE), (EdgeRelation.HAS_SLOT, NodeKind.RECIPE, NodeKind.ADAPTATION_SLOT),
        (EdgeRelation.CONSTRAINED_BY, NodeKind.RECIPE, NodeKind.CONSTRAINT), (EdgeRelation.REQUIRES, NodeKind.RECIPE, NodeKind.DEPENDENCY),
        (EdgeRelation.VERIFIED_BY, NodeKind.RECIPE, NodeKind.TEST_SPEC), (EdgeRelation.CHANGES, NodeKind.TASK_EPISODE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.TESTS, NodeKind.TEST_SPEC, NodeKind.SOURCE_EVIDENCE), (EdgeRelation.SUPERSEDES, NodeKind.RECIPE, NodeKind.RECIPE),
        (EdgeRelation.BUNDLED_AS, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
    ]
    edges = []
    for relation, source_kind, target_kind in pairs:
        source = AtlasNode.create(source_kind, {"source": relation.value})
        target = AtlasNode.create(target_kind, {"target": relation.value})
        store.put_nodes((source, target))
        edges.append(AtlasEdge.create(relation, source, target))
    store.put_edges(tuple(edges))
    with pytest.raises(StoreConflictError, match="(identity|endpoint)"):
        store.put_edges((replace(edges[0], source_id="sha256:" + "a" * 64),))
    store.close()
    reopened = store_at(tmp_path)
    assert len(reopened.graph_query((), max_nodes=200, max_edges=400, max_depth=1, byte_budget=1000000).edges) == 0
    assert sqlite3.connect(tmp_path / "atlas.sqlite").execute("SELECT count(*) FROM atlas_edges").fetchone()[0] == 11
    reopened.close()


def test_verified_cas_and_receipts_are_idempotent_or_conflicting(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    digest = "sha256:" + "0" * 64
    with pytest.raises(StoreConflictError, match="blob hash"):
        store.put_blob(digest, b"verified template")
    valid = "sha256:" + hashlib.sha256(b"verified template").hexdigest()
    assert store.put_blob(valid, b"verified template") == valid
    assert store.read_blob(valid) == b"verified template"
    packet = ImplementationPacket("packet", "trace", "workspace", "snap", "recipe")
    store.put_packet(packet)
    store.put_packet(packet)
    with pytest.raises(StoreConflictError):
        store.put_packet(replace(packet, snapshot_id="other"))
    receipt = IngestionReceipt("key", "sha256:" + "b" * 64, AtlasStatus.READY, "episode", "recipe", ("ok",), "now")
    store.put_ingestion_receipt(receipt)
    store.put_ingestion_receipt(receipt)
    with pytest.raises(StoreConflictError):
        store.put_ingestion_receipt(replace(receipt, episode_id="other"))
    assert store.get_packet("packet") == packet
    assert store.get_ingestion_receipt("key") == receipt
    store.close()


def test_recipe_links_and_bounded_bidirectional_query(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    intent = AtlasNode.create(NodeKind.INTENT, {"id": "intent"})
    recipes = tuple(AtlasNode.create(NodeKind.RECIPE, {"id": str(index)}) for index in range(3))
    store.put_nodes((intent, *recipes))
    edges = tuple(AtlasEdge.create(EdgeRelation.SOLVES, recipe, intent) for recipe in recipes)
    store.put_edges(edges)
    manifest = RecipeManifest(recipes[0].node_id, "key", 1, intent.node_id, "python", "1", "repo", "local", "hash", "", "ready")
    store.put_recipe(manifest, node_ids=(intent.node_id, recipes[0].node_id), edge_ids=(edges[0].edge_id,))
    assert store.recipes_for_intent(intent.node_id) == (recipes[0].node_id,)
    result = store.graph_query((intent.node_id,), max_nodes=2, max_edges=1, max_depth=1, byte_budget=1000000)
    assert tuple(node.node_id for node in result.nodes) == tuple(sorted(node.node_id for node in result.nodes))
    assert len(result.nodes) == 2 and len(result.edges) == 1 and result.truncated is True
    assert {edge.source_id for edge in result.edges} | {edge.target_id for edge in result.edges} <= {node.node_id for node in result.nodes}
    with pytest.raises(ValueError):
        store.graph_query((intent.node_id,), max_nodes=0, max_edges=1, max_depth=1, byte_budget=1)
    store.close()
