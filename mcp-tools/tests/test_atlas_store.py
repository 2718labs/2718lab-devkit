"""Contract tests for the local Atlas SQLite/CAS store."""

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
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas import store as store_module
from devkit_atlas.canonical import canonical_hash, canonical_json
from devkit_atlas.models import (
    AtlasEdge,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    EdgeRelation,
    ImplementationPacket,
    IngestionReceipt,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
)
from devkit_atlas.models import (
    TestSpec as AtlasTestSpec,
)
from devkit_atlas.security import (
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
    MAX_RECIPE_BYTES,
    MAX_TEMPLATE_BYTES,
)
from devkit_atlas.store import AtlasStore, StoreConflictError
from devkit_runtime import sqlite_snapshot as snapshot_module


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
    packet = _verified_packet()
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
    assert store.get_packet(packet.packet_id) == packet
    assert store.get_ingestion_receipt("key") == receipt
    store.close()


def _ingestion_bundle_fixture() -> tuple[
    tuple[AtlasNode, ...],
    tuple[AtlasEdge, ...],
    RecipeManifest,
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, bytes, str], ...],
    IngestionReceipt,
]:
    """Create one complete observed recipe bundle with deterministic identities."""

    template = b"def ${symbol_000}() -> int:\n    return 1\n"
    template_hash = "sha256:" + hashlib.sha256(template).hexdigest()
    manifest = RecipeManifest(
        recipe_id="",
        recipe_key="python.atomic-bundle",
        version=1,
        intent_id="python.atomic-bundle",
        language_name="python",
        language_extractor_version="1",
        repository_signature="sha256:" + "1" * 64,
        layer="local",
        manifest_hash="sha256:" + "2" * 64,
        slots=(
            SlotSpec("path_000", "relative_python_path"),
            SlotSpec("symbol_000", "python_identifier"),
        ),
        constraints=(
            ConstraintSpec("path_suffix", "path_000", ".py"),
            ConstraintSpec("required_symbol", "symbol_000", "created"),
        ),
        operations=(
            TemplateOperation("create_python_file", "path_000", template_hash),
        ),
        provenance_kind="observed",
        provenance_source="accepted_task",
    )
    intent = AtlasNode.create(NodeKind.INTENT, {"intent_id": manifest.intent_id})
    episode = AtlasNode.create(
        NodeKind.TASK_EPISODE,
        {"acceptance_id_hash": "sha256:" + "3" * 64, "task_kind": "code"},
    )
    recipe_payload = manifest.to_dict()
    del recipe_payload["recipe_id"]
    recipe = AtlasNode.create(NodeKind.RECIPE, recipe_payload)
    manifest = replace(manifest, recipe_id=recipe.node_id)
    template_node = AtlasNode.create(
        NodeKind.CODE_TEMPLATE,
        {"template_hash": template_hash, "kind": "create_python_file"},
    )
    nodes = (intent, episode, recipe, template_node)
    edges = (
        AtlasEdge.create(EdgeRelation.SOLVES, episode, intent),
        AtlasEdge.create(EdgeRelation.SOLVES, recipe, intent),
        AtlasEdge.create(EdgeRelation.DERIVED_FROM, recipe, episode),
        AtlasEdge.create(EdgeRelation.HAS_IMPLEMENTATION, recipe, template_node),
    )
    receipt = IngestionReceipt(
        ingestion_key="sha256:" + "4" * 64,
        payload_hash="sha256:" + "5" * 64,
        status=AtlasStatus.READY,
        episode_id=episode.node_id,
        recipe_id=recipe.node_id,
        reasons=(),
    )
    return (
        nodes,
        edges,
        manifest,
        tuple(node.node_id for node in nodes),
        tuple(edge.edge_id for edge in edges),
        ((template_hash, template, "text/x-python"),),
        receipt,
    )


def _put_ingestion_bundle(
    store: AtlasStore,
    fixture: tuple[
        tuple[AtlasNode, ...],
        tuple[AtlasEdge, ...],
        RecipeManifest,
        tuple[str, ...],
        tuple[str, ...],
        tuple[tuple[str, bytes, str], ...],
        IngestionReceipt,
    ],
) -> IngestionReceipt:
    nodes, edges, manifest, node_ids, edge_ids, blobs, receipt = fixture
    return store.put_ingestion_bundle(
        nodes=nodes,
        edges=edges,
        manifest=manifest,
        recipe_node_ids=node_ids,
        recipe_edge_ids=edge_ids,
        blobs=blobs,
        receipt=receipt,
    )


def _bundle_counts(store: AtlasStore) -> tuple[int, int, int, int, int]:
    return tuple(
        int(store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in (
            "atlas_nodes",
            "atlas_edges",
            "atlas_recipes",
            "atlas_blobs",
            "atlas_ingestion_receipts",
        )
    )


def test_ingestion_bundle_persists_one_atomic_recipe_projection(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    nodes, _edges, manifest, _node_ids, _edge_ids, blobs, receipt = fixture

    assert _put_ingestion_bundle(store, fixture) == receipt
    assert store.get_ingestion_receipt(receipt.ingestion_key) == receipt
    assert _bundle_counts(store) == (len(nodes), 4, 1, 1, 1)
    assert store.read_blob(blobs[0][0]) == blobs[0][1]
    assert store.recipe_metadata(manifest.recipe_id) is not None
    store.close()


def test_ingestion_bundle_persists_an_episode_only_projection(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    intent = AtlasNode.create(NodeKind.INTENT, {"intent_id": "python.episode-only"})
    episode = AtlasNode.create(NodeKind.TASK_EPISODE, {"task_kind": "code"})
    edge = AtlasEdge.create(EdgeRelation.SOLVES, episode, intent)
    receipt = IngestionReceipt(
        ingestion_key="sha256:" + "6" * 64,
        payload_hash="sha256:" + "7" * 64,
        status=AtlasStatus.EVIDENCE_INCOMPLETE,
        episode_id=episode.node_id,
        reasons=("UNSUPPORTED_LANGUAGE",),
    )

    assert (
        store.put_ingestion_bundle(
            nodes=(intent, episode),
            edges=(edge,),
            manifest=None,
            recipe_node_ids=(),
            recipe_edge_ids=(),
            blobs=(),
            receipt=receipt,
        )
        == receipt
    )
    assert _bundle_counts(store) == (2, 1, 0, 0, 1)
    store.close()


def test_ingestion_bundle_replays_exactly_and_conflicts_before_mutation(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    _put_ingestion_bundle(store, fixture)
    before = _bundle_counts(store)

    assert _put_ingestion_bundle(store, fixture) == fixture[-1]
    assert _bundle_counts(store) == before
    conflicting = (
        *fixture[:-1],
        replace(fixture[-1], payload_hash="sha256:" + "8" * 64),
    )
    with pytest.raises(StoreConflictError, match="ingestion receipt"):
        _put_ingestion_bundle(store, conflicting)
    assert _bundle_counts(store) == before
    store.close()


def test_ingestion_bundle_requires_complete_and_exact_recipe_templates(
    tmp_path: Path,
) -> None:
    fixture = _ingestion_bundle_fixture()
    blob_hash, content, media_type = fixture[5][0]
    extra = b"def unrelated() -> None:\n    pass\n"
    extra_hash = "sha256:" + hashlib.sha256(extra).hexdigest()
    invalid_cases = (
        (*fixture[:5], (), fixture[6]),
        (*fixture[:5], (fixture[5][0], (extra_hash, extra, media_type)), fixture[6]),
    )
    for index, candidate in enumerate(invalid_cases):
        store = store_at(tmp_path / f"template-invalid-{index}")
        with pytest.raises(StoreConflictError, match="bundle blob"):
            _put_ingestion_bundle(store, candidate)
        assert _bundle_counts(store) == (0, 0, 0, 0, 0)
        store.close()

    adopted = store_at(tmp_path / "template-adopted")
    adopted.put_blob(blob_hash, content, media_type)
    existing_case = (*fixture[:5], (), fixture[6])
    assert _put_ingestion_bundle(adopted, existing_case) == fixture[6]
    assert adopted.read_blob(blob_hash) == content
    adopted.close()


def test_ingestion_bundle_rejects_unstable_receipts_and_recipe_status_mismatch(
    tmp_path: Path,
) -> None:
    fixture = _ingestion_bundle_fixture()
    recipe_receipt = fixture[6]
    episode_intent = AtlasNode.create(NodeKind.INTENT, {"intent_id": "python.gap"})
    episode = AtlasNode.create(NodeKind.TASK_EPISODE, {"task_kind": "code"})
    episode_edge = AtlasEdge.create(EdgeRelation.SOLVES, episode, episode_intent)
    episode_receipt = IngestionReceipt(
        ingestion_key="sha256:" + "d" * 64,
        payload_hash="sha256:" + "e" * 64,
        status=AtlasStatus.EVIDENCE_INCOMPLETE,
        episode_id=episode.node_id,
        reasons=("PARSER_GAP",),
    )
    recipe_cases = (
        replace(recipe_receipt, status=AtlasStatus.EVIDENCE_INCOMPLETE),
        replace(recipe_receipt, reasons=("UNEXPECTED",)),
        replace(recipe_receipt, created_at="2026-07-29T00:00:00Z"),
        replace(recipe_receipt, reasons=("not-stable",)),
    )
    for index, receipt in enumerate(recipe_cases):
        store = store_at(tmp_path / f"recipe-receipt-{index}")
        candidate = (*fixture[:-1], receipt)
        with pytest.raises(StoreConflictError, match="bundle"):
            _put_ingestion_bundle(store, candidate)
        assert _bundle_counts(store) == (0, 0, 0, 0, 0)
        store.close()

    for index, receipt in enumerate(
        (
            replace(episode_receipt, status=AtlasStatus.READY, reasons=()),
            replace(episode_receipt, reasons=()),
            replace(episode_receipt, reasons=("PARSER_GAP", "PARSER_GAP")),
            replace(episode_receipt, reasons=(1,)),
        )
    ):
        store = store_at(tmp_path / f"episode-receipt-{index}")
        with pytest.raises(StoreConflictError, match="bundle"):
            store.put_ingestion_bundle(
                nodes=(episode_intent, episode),
                edges=(episode_edge,),
                manifest=None,
                recipe_node_ids=(),
                recipe_edge_ids=(),
                blobs=(),
                receipt=receipt,
            )
        assert _bundle_counts(store) == (0, 0, 0, 0, 0)
        store.close()


def test_ingestion_bundle_prevalidates_every_recipe_link_endpoint(
    tmp_path: Path,
) -> None:
    fixture = _ingestion_bundle_fixture()
    missing_edge = "sha256:" + "f" * 64
    cases = (
        (*fixture[:4], (missing_edge,), fixture[5], fixture[6]),
        (
            *fixture[:3],
            tuple(
                node_id for node_id in fixture[3] if node_id != fixture[0][1].node_id
            ),
            fixture[4],
            fixture[5],
            fixture[6],
        ),
    )
    for index, candidate in enumerate(cases):
        store = store_at(tmp_path / f"link-invalid-{index}")
        with pytest.raises(StoreConflictError, match="recipe edge link"):
            _put_ingestion_bundle(store, candidate)
        assert _bundle_counts(store) == (0, 0, 0, 0, 0)
        store.close()


def test_ingestion_bundle_requires_recipe_node_to_match_manifest(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    mismatched_manifest = replace(fixture[2], version=2)
    candidate = (
        fixture[0],
        fixture[1],
        mismatched_manifest,
        fixture[3],
        fixture[4],
        fixture[5],
        fixture[6],
    )

    with pytest.raises(StoreConflictError, match="bundle recipe"):
        _put_ingestion_bundle(store, candidate)
    assert _bundle_counts(store) == (0, 0, 0, 0, 0)
    store.close()


@pytest.mark.parametrize(
    "stage",
    (
        "_bundle_publish_blob",
        "_bundle_insert_blobs",
        "_bundle_insert_nodes",
        "_bundle_insert_edges",
        "_bundle_insert_recipe",
        "_bundle_insert_receipt",
        "_bundle_commit",
    ),
)
def test_ingestion_bundle_rolls_back_every_logical_write_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise StoreConflictError("forced bundle failure")

    monkeypatch.setattr(store, stage, fail)
    with pytest.raises(StoreConflictError, match="forced bundle failure"):
        _put_ingestion_bundle(store, fixture)
    assert _bundle_counts(store) == (0, 0, 0, 0, 0)
    blob_hash = fixture[5][0][0]
    blob_path = tmp_path / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    assert not blob_path.exists()
    store.close()


def test_ingestion_bundle_preserves_existing_cas_and_adopts_verified_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    blob_hash, content, media_type = fixture[5][0]
    store.put_blob(blob_hash, content, media_type)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise StoreConflictError("forced receipt failure")

    monkeypatch.setattr(store, "_bundle_insert_receipt", fail)
    with pytest.raises(StoreConflictError, match="forced receipt failure"):
        _put_ingestion_bundle(store, fixture)
    assert store.read_blob(blob_hash) == content
    assert _bundle_counts(store) == (0, 0, 0, 1, 0)
    store.close()

    orphan_store = store_at(tmp_path / "orphan")
    orphan_path = (
        tmp_path / "orphan" / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    )
    orphan_path.parent.mkdir(parents=True)
    orphan_path.write_bytes(content)

    def forbid_plain_path_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("bundle orphan verification must use the no-follow reader")

    monkeypatch.setattr(Path, "read_bytes", forbid_plain_path_read)
    assert _put_ingestion_bundle(orphan_store, fixture) == fixture[-1]
    assert orphan_store.read_blob(blob_hash) == content
    orphan_store.close()


def test_ingestion_bundle_rejects_a_mismatching_orphan_and_reopens(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    blob_hash, _content, _media_type = fixture[5][0]
    path = tmp_path / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mismatching orphan")

    with pytest.raises(StoreConflictError, match="blob"):
        _put_ingestion_bundle(store, fixture)
    assert _bundle_counts(store) == (0, 0, 0, 0, 0)
    assert path.read_bytes() == b"mismatching orphan"
    store.close()

    clean = store_at(tmp_path / "restart")
    assert _put_ingestion_bundle(clean, fixture) == fixture[-1]
    clean.close()
    reopened = store_at(tmp_path / "restart")
    assert reopened.get_ingestion_receipt(fixture[-1].ingestion_key) == fixture[-1]
    assert reopened.read_blob(fixture[5][0][0]) == fixture[5][0][1]
    reopened.close()


@pytest.mark.parametrize("kind", ("directory", "symlink"))
def test_ingestion_bundle_rejects_unsafe_orphan_file_types(
    tmp_path: Path,
    kind: str,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    blob_hash = fixture[5][0][0]
    path = tmp_path / "cas" / "sha256" / blob_hash[7:9] / blob_hash[9:]
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "untrusted-template"
        target.write_bytes(fixture[5][0][1])
        try:
            os.symlink(target, path)
        except OSError:
            pytest.skip("symlinks are unavailable for this test account")

    with pytest.raises(StoreConflictError, match="blob"):
        _put_ingestion_bundle(store, fixture)
    assert _bundle_counts(store) == (0, 0, 0, 0, 0)
    store.close()


def test_ingestion_bundle_rechecks_blob_parent_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_at(tmp_path)
    fixture = _ingestion_bundle_fixture()
    blob_hash = fixture[5][0][0]
    parent = tmp_path / "cas" / "sha256" / blob_hash[7:9]
    outside = tmp_path / "outside"
    outside.mkdir()
    original_prepare = store._bundle_prepare_blob_parent

    def swap_parent(candidate: Path) -> None:
        original_prepare(candidate)
        try:
            os.rmdir(parent)
            os.symlink(outside, parent, target_is_directory=True)
        except OSError:
            pytest.skip("parent symlink swaps are unavailable for this test account")

    monkeypatch.setattr(store, "_bundle_prepare_blob_parent", swap_parent)
    with pytest.raises(StoreConflictError, match="blob"):
        _put_ingestion_bundle(store, fixture)
    assert not tuple(outside.iterdir())
    assert _bundle_counts(store) == (0, 0, 0, 0, 0)
    store.close()


def test_ingestion_bundle_rejects_blob_bounds_metadata_and_duplicates_before_write(
    tmp_path: Path,
) -> None:
    fixture = _ingestion_bundle_fixture()
    blob_hash, content, media_type = fixture[5][0]

    invalid_cases = (
        (*fixture[:5], (fixture[5][0], fixture[5][0]), fixture[6]),
        (*fixture[:5], ((blob_hash, content, "not-a-media-type"),), fixture[6]),
        (
            *fixture[:5],
            tuple(
                (
                    "sha256:"
                    + hashlib.sha256(
                        b"x" * (MAX_TEMPLATE_BYTES - 1) + str(index).encode("ascii")
                    ).hexdigest(),
                    b"x" * (MAX_TEMPLATE_BYTES - 1) + str(index).encode("ascii"),
                    media_type,
                )
                for index in range(5)
            ),
            fixture[6],
        ),
        (
            *fixture[:5],
            tuple(
                (
                    "sha256:" + hashlib.sha256(f"blob-{index}".encode()).hexdigest(),
                    f"blob-{index}".encode(),
                    media_type,
                )
                for index in range(MAX_GRAPH_NODES + 1)
            ),
            fixture[6],
        ),
    )
    assert 5 * MAX_TEMPLATE_BYTES > MAX_RECIPE_BYTES

    for index, candidate in enumerate(invalid_cases):
        store = store_at(tmp_path / f"invalid-{index}")
        with pytest.raises(StoreConflictError, match="bundle blob"):
            _put_ingestion_bundle(store, candidate)
        assert _bundle_counts(store) == (0, 0, 0, 0, 0)
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
        workspace_id="sha256:" + "b" * 64,
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
        next_action="atlas_render",
        request_hash="sha256:" + "c" * 64,
        matcher_version="atlas-matcher/v1",
        target_paths=("src/example.py",),
        target_symbols=(),
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


def test_open_readonly_delegates_to_the_shared_verified_snapshot_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_runtime.sqlite_snapshot import open_verified_sqlite_snapshot

    durable = tmp_path / "durable"
    scratch = tmp_path / "readonly-scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    calls: list[tuple[Path, Path, tuple[Path, ...]]] = []

    def observed_factory(
        database_path: str | Path,
        *,
        scratch_root: str | Path | None = None,
        protected_roots: tuple[str | Path, ...] = (),
    ) -> object:
        assert scratch_root is not None
        calls.append(
            (
                Path(database_path),
                Path(scratch_root),
                tuple(Path(root) for root in protected_roots),
            )
        )
        return open_verified_sqlite_snapshot(
            database_path,
            scratch_root=scratch_root,
            protected_roots=protected_roots,
        )

    monkeypatch.setattr(
        store_module,
        "open_verified_sqlite_snapshot",
        observed_factory,
        raising=False,
    )

    readonly = AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    try:
        assert readonly.schema_version() == 1
    finally:
        readonly.close()

    assert calls == [(database, scratch, (cas_root,))]


def test_open_readonly_preserves_schema_validation_and_snapshot_error_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_runtime.sqlite_snapshot import SqliteSnapshotError

    durable = tmp_path / "durable"
    scratch = tmp_path / "readonly-scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE atlas_metadata SET value='2' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()
    before = _durable_file_state(durable)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)

    assert not tuple(scratch.iterdir())
    assert _durable_file_state(durable) == before

    def unavailable_snapshot(*args: object, **kwargs: object) -> object:
        raise SqliteSnapshotError()

    monkeypatch.setattr(
        store_module,
        "open_verified_sqlite_snapshot",
        unavailable_snapshot,
    )
    with pytest.raises(StoreConflictError) as raised:
        AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    assert raised.value.__cause__ is None


def test_open_readonly_preserves_cas_identity_across_snapshot_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devkit_runtime.sqlite_snapshot import open_verified_sqlite_snapshot

    durable = tmp_path / "durable"
    scratch = tmp_path / "readonly-scratch"
    durable.mkdir()
    scratch.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    parked = tmp_path / "parked-cas"
    replacement = tmp_path / "replacement-cas"
    replacement.mkdir()
    swapped = False

    def swap_cas_after_snapshot(
        database_path: str | Path,
        *,
        scratch_root: str | Path | None = None,
        protected_roots: tuple[str | Path, ...] = (),
    ) -> object:
        nonlocal swapped
        snapshot = open_verified_sqlite_snapshot(
            database_path,
            scratch_root=scratch_root,
            protected_roots=protected_roots,
        )
        os.replace(cas_root, parked)
        os.replace(replacement, cas_root)
        swapped = True
        return snapshot

    monkeypatch.setattr(
        store_module,
        "open_verified_sqlite_snapshot",
        swap_cas_after_snapshot,
    )
    try:
        with pytest.raises(StoreConflictError):
            AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)
    finally:
        if swapped:
            os.replace(cas_root, replacement)
            os.replace(parked, cas_root)

    assert swapped is True
    assert not tuple(scratch.iterdir())


def test_open_readonly_store_uses_a_scratch_snapshot_without_touching_durable_files(
    tmp_path: Path,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
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


def test_open_readonly_supports_the_two_path_public_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
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
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
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
    assert not tuple(durable.rglob(".sqlite-snapshot-root-*"))
    assert unsafe_temp != "missing" or not configured_temp.exists()


def test_open_readonly_default_scratch_never_probes_temp_before_overlap_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold tempfile cache must not create a probe in durable storage."""

    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    before_files = _durable_file_state(durable)
    before_entries = tuple(
        sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
    )
    original_probe = tempfile._get_default_tempdir
    probes = 0

    def count_temp_probe() -> str:
        nonlocal probes
        probes += 1
        return original_probe()

    monkeypatch.delenv("CODEX_TASK_TEMP", raising=False)
    for variable in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(variable, str(durable))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tempfile, "_get_default_tempdir", count_temp_probe)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root)

    assert probes == 0
    assert _durable_file_state(durable) == before_files
    assert (
        tuple(
            sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
        )
        == before_entries
    )


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
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
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
        "unsafe_capability_create": False,
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

    def swap_then_mkdtemp(*args: Any, **kwargs: Any) -> str:
        directory = kwargs.get("dir")
        if directory is not None and Path(os.fspath(directory)) == scratch_parent:
            swap_parent_once()
            if state["swapped"]:
                state["unsafe_mkdtemp"] = True
        return original_mkdtemp(*args, **kwargs)

    original_create = snapshot_module._create_readonly_directory

    def swap_then_capability_create(*args: Any, **kwargs: Any) -> Path:
        state["capability_create_called"] = True
        swap_parent_once()
        created = original_create(*args, **kwargs)
        if state["swapped"] and created.exists():
            state["unsafe_capability_create"] = True
        return created

    monkeypatch.setattr(tempfile, "mkdtemp", swap_then_mkdtemp)
    monkeypatch.setattr(
        snapshot_module,
        "_create_readonly_directory",
        swap_then_capability_create,
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
    assert state["unsafe_capability_create"] is False
    assert state["unsafe_mkdtemp"] is False
    assert state["capability_create_called"] is True
    assert _durable_file_state(durable) == before_files
    assert (
        tuple(
            sorted(path.relative_to(durable).as_posix() for path in durable.rglob("*"))
        )
        == before_entries
    )
    assert not tuple(durable.rglob(".sqlite-snapshot-*"))


def test_open_readonly_store_sees_committed_wal_state(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
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
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    writable.close()
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    original_copy = snapshot_module._copy_snapshot_file

    def replace_after_copy(
        source: Path, destination: snapshot_module._SnapshotDestination
    ) -> tuple[int, int, int, int, int]:
        copied = original_copy(source, destination)
        if source == database:
            os.replace(replacement, database)
        return copied

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_file", replace_after_copy)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root, scratch_root=scratch)


def test_open_readonly_fails_closed_when_the_live_wal_is_replaced_mid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    scratch = tmp_path / "readonly-scratch"
    scratch.mkdir()
    writable = AtlasStore(database, cas_root)
    writable.put_nodes((AtlasNode.create(NodeKind.INTENT, {"intent_id": "wal"}),))
    wal = Path(str(database) + "-wal")
    replacement = tmp_path / "replacement-wal"
    replacement.write_bytes(wal.read_bytes())
    original_copy = snapshot_module._copy_snapshot_file

    def replace_after_copy(
        source: Path, destination: snapshot_module._SnapshotDestination
    ) -> tuple[int, int, int, int, int]:
        copied = original_copy(source, destination)
        if source == wal:
            os.replace(replacement, wal)
        return copied

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_file", replace_after_copy)
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
    database = durable / "atlas.sqlite3"
    cas_root = durable / "atlas-cas"
    writable = AtlasStore(database, cas_root)
    writable.close()
    before = _durable_file_state(durable)

    with pytest.raises(StoreConflictError):
        AtlasStore.open_readonly(database, cas_root, scratch_root=durable)

    assert _durable_file_state(durable) == before
