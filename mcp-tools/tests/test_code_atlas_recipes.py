"""Contracts for deterministic bundled Code Atlas recipes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.canonical import canonical_json
from code_atlas.models import AtlasError, EdgeRelation, NodeKind
from code_atlas.recipes import BundledRecipeLoader, render_pattern_card
from code_atlas.store import AtlasStore


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
    for recipe in recipes:
        for operation in recipe.operations:
            digest = operation.template_hash.removeprefix("sha256:")
            blob = ASSETS / "templates" / "sha256" / digest
            assert blob.is_file()
            assert (
                "sha256:" + hashlib.sha256(blob.read_bytes()).hexdigest()
                == operation.template_hash
            )


def test_pattern_card_is_a_deterministic_view() -> None:
    recipe = BundledRecipeLoader(ASSETS).load()[0]
    first = render_pattern_card(recipe)
    assert first == render_pattern_card(recipe)
    assert recipe.recipe_id in first
    assert recipe.intent_id in first
    assert "## Applicability" in first
    assert "## Slots" in first
    assert "## Constraints" in first
    assert "## Operations" in first
    assert "## Verification" in first
    assert recipe.manifest_hash in first


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


def test_materialization_has_typed_nodes_and_locked_relations() -> None:
    graph = BundledRecipeLoader(ASSETS).materialize()
    assert graph.nodes == tuple(sorted(graph.nodes, key=lambda node: node.node_id))
    assert graph.edges == tuple(sorted(graph.edges, key=lambda edge: edge.edge_id))
    assert {node.kind for node in graph.nodes} >= {
        NodeKind.RECIPE,
        NodeKind.INTENT,
        NodeKind.LANGUAGE,
        NodeKind.CODE_TEMPLATE,
        NodeKind.ADAPTATION_SLOT,
        NodeKind.CONSTRAINT,
        NodeKind.DEPENDENCY,
        NodeKind.TEST_SPEC,
        NodeKind.SOURCE_EVIDENCE,
    }
    assert any(edge.relation is EdgeRelation.BUNDLED_AS for edge in graph.edges)
    recipe_ids = {node.node_id for node in graph.nodes if node.kind is NodeKind.RECIPE}
    assert {
        recipe.recipe_id for recipe in BundledRecipeLoader(ASSETS).load()
    } == recipe_ids


def test_materialized_recipes_can_be_persisted_with_their_graph_links(
    tmp_path: Path,
) -> None:
    loader = BundledRecipeLoader(ASSETS)
    graph = loader.materialize()
    store = AtlasStore(tmp_path / "atlas.sqlite", tmp_path / "cas")
    try:
        store.put_nodes(graph.nodes)
        store.put_edges(graph.edges)
        node_ids = tuple(node.node_id for node in graph.nodes)
        edge_ids = tuple(edge.edge_id for edge in graph.edges)
        for recipe in loader.load():
            store.put_recipe(recipe, node_ids=node_ids, edge_ids=edge_ids)
    finally:
        store.close()


def copied_assets(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    shutil.copytree(ASSETS, root)
    return root


def rewrite_manifest(root: Path, name: str, mutate) -> None:
    path = root / "recipes" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_bytes(canonical_json(data).encode("utf-8") + b"\n")


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda item: item.__setitem__("unknown", True), "invalid_recipe"),
        (lambda item: item.pop("slots"), "invalid_recipe"),
        (
            lambda item: item.__setitem__("intent_id", "Python Bad"),
            "invalid_intent_id",
        ),
        (lambda item: item["slots"].append(dict(item["slots"][0])), "invalid_slots"),
        (
            lambda item: item["operations"][0].__setitem__(
                "template_hash", "sha256:" + "0" * 64
            ),
            "missing_template_blob",
        ),
        (
            lambda item: item["operations"][0].__setitem__("kind", "execute_command"),
            "invalid_operation",
        ),
        (
            lambda item: item["operations"][0].__setitem__("path_slot", "../escape.py"),
            "invalid_operation",
        ),
        (
            lambda item: item["operations"][0].__setitem__(
                "template_hash", "../escape"
            ),
            "invalid_template_hash",
        ),
        (lambda item: item["language"].__setitem__("extra", "no"), "invalid_recipe"),
        (
            lambda item: item["tests"][0].__setitem__("argv", "not-a-list"),
            "invalid_recipe",
        ),
        (
            lambda item: item["provenance"].__setitem__("kind", "local"),
            "invalid_recipe",
        ),
    ],
)
def test_loader_rejects_every_strict_boundary(
    tmp_path: Path, mutate, code: str
) -> None:
    root = copied_assets(tmp_path)
    rewrite_manifest(root, "python-fastmcp-read-tool.json", mutate)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == code
    assert not (tmp_path / "escape").exists()


def test_loader_rejects_undeclared_template_placeholder(tmp_path: Path) -> None:
    root = copied_assets(tmp_path)
    blobs = root / "templates" / "sha256"
    body = b"value = ${missing}\n"
    digest = hashlib.sha256(body).hexdigest()
    (blobs / digest).write_bytes(body)
    data = json.loads(
        (ASSETS / "recipes" / "python-validation-guard.json").read_text(
            encoding="utf-8"
        )
    )
    data["operations"][0]["template_hash"] = "sha256:" + digest
    (root / "recipes" / "python-validation-guard.json").write_bytes(
        canonical_json(data).encode("utf-8") + b"\n"
    )
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "undeclared_placeholder"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda item: item["slots"][0].__setitem__("name", 1), "invalid_slots"),
        (lambda item: item["slots"][0].__setitem__("type", 1), "invalid_slots"),
        (lambda item: item["slots"][0].__setitem__("required", 1), "invalid_slots"),
        (lambda item: item["framework"].__setitem__("name", 1), "invalid_recipe"),
        (lambda item: item["constraints"][0].__setitem__("kind", 1), "invalid_recipe"),
        (
            lambda item: item["dependencies"][0].__setitem__("specifier", 1),
            "invalid_recipe",
        ),
        (
            lambda item: item["tests"][0].__setitem__("expected_exit_code", True),
            "invalid_recipe",
        ),
        (
            lambda item: item["operations"][0].__setitem__("separator", 1),
            "invalid_operation",
        ),
        (
            lambda item: item["operations"][0].__setitem__("path_slot", "tool_name"),
            "invalid_operation",
        ),
    ],
)
def test_loader_rejects_malformed_nested_types(
    tmp_path: Path, mutate, code: str
) -> None:
    root = copied_assets(tmp_path)
    rewrite_manifest(root, "python-fastmcp-read-tool.json", mutate)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == code


def test_loader_rejects_junction_or_reparse_asset_components(
    tmp_path: Path, monkeypatch
) -> None:
    root = copied_assets(tmp_path)
    original_lstat = Path.lstat

    class Reparse:
        st_file_attributes = 0x400
        st_mode = 0

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: Reparse() if path.name == "recipes" else original_lstat(path),
    )
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "unsafe_asset_reference"

    monkeypatch.setattr(Path, "lstat", original_lstat)
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda path: Path(path).name == "sha256",
        raising=False,
    )
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "unsafe_asset_reference"


def test_standalone_validator_has_exact_success_stdout() -> None:
    result = subprocess.run(
        [sys.executable, "skills/code-atlas/scripts/validate_recipes.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "Code Atlas recipes valid: 3\n"
    assert result.stderr == ""
