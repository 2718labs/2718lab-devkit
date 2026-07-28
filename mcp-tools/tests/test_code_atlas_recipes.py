"""Contracts for deterministic bundled Code Atlas recipes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.models import AtlasError, EdgeRelation, NodeKind
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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.__setitem__("unknown", True),
        lambda item: item.pop("slots"),
        lambda item: item.__setitem__("intent_id", "Python Bad"),
        lambda item: item["slots"].append(dict(item["slots"][0])),
        lambda item: item["operations"][0].__setitem__(
            "template_hash", "sha256:" + "0" * 64
        ),
        lambda item: item["operations"][0].__setitem__("kind", "execute_command"),
        lambda item: item["operations"][0].__setitem__("path_slot", "../escape.py"),
        lambda item: item["operations"][0].__setitem__("template_hash", "../escape"),
        lambda item: item["language"].__setitem__("extra", "no"),
        lambda item: item["tests"][0].__setitem__("argv", "not-a-list"),
        lambda item: item["provenance"].__setitem__("kind", "local"),
    ],
)
def test_loader_rejects_every_strict_boundary(tmp_path: Path, mutate) -> None:
    root = tmp_path / "assets"
    (root / "recipes").mkdir(parents=True)
    source = ASSETS / "recipes" / "python-fastmcp-read-tool.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    mutate(data)
    (root / "recipes" / source.name).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AtlasError):
        BundledRecipeLoader(root).load()
    assert tuple(root.rglob("*"))
    assert not (tmp_path / "escape").exists()


def test_loader_rejects_undeclared_template_placeholder(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    blobs = root / "templates" / "sha256"
    blobs.mkdir(parents=True)
    body = b"value = ${missing}\n"
    digest = hashlib.sha256(body).hexdigest()
    (blobs / digest).write_bytes(body)
    (root / "recipes").mkdir()
    data = json.loads(
        (ASSETS / "recipes" / "python-validation-guard.json").read_text(
            encoding="utf-8"
        )
    )
    data["operations"][0]["template_hash"] = "sha256:" + digest
    (root / "recipes" / "recipe.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AtlasError):
        BundledRecipeLoader(root).load()


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
