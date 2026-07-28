"""Contracts for deterministic bundled Code Atlas recipes."""

from __future__ import annotations

import hashlib
import importlib.util
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
LOCKED_SEEDS = {
    "python-fastmcp-read-tool.json": (
        "python.fastmcp.read-only-tool",
        "sha256:090d5bb247d1fa88d5ae65f39451ca4f0bf176a50716ef6c647752d10a1a029b",
        "sha256:c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950",
    ),
    "python-pytest-regression.json": (
        "python.pytest-regression",
        "sha256:29572bd77a897bbb035fbf6cb79a21ff1a2298144b8c60dcd0d6a12231b45f4c",
        "sha256:d07c7a977b330d26fe6486ab5e07f02855117f092ad19b0b08b2aafb136aef6c",
    ),
    "python-validation-guard.json": (
        "python.validation-guard",
        "sha256:c6f2adacb33c5a5037da8559b1b4b550b1cf25dbcfd834d377aaacb6f51ed59a",
        "sha256:47820213e5b67e968dff12d9509b8990d7aa1a6465a85b631599628589f8d8de",
    ),
}


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
    by_intent = {recipe.intent_id: recipe for recipe in recipes}
    for filename, (intent_id, manifest_hash, template_hash) in LOCKED_SEEDS.items():
        raw = (ASSETS / "recipes" / filename).read_bytes()
        assert "sha256:" + hashlib.sha256(raw[:-1]).hexdigest() == manifest_hash
        recipe = by_intent[intent_id]
        assert recipe.manifest_hash == manifest_hash
        assert tuple(item.template_hash for item in recipe.operations) == (
            template_hash,
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


@pytest.mark.parametrize(
    ("name", "mutate", "code"),
    [
        (
            "append target symbol",
            lambda item: item["operations"][0].__setitem__(
                "target_symbol_slot", "tool_name"
            ),
            "invalid_operation",
        ),
        (
            "prepend separator",
            lambda item: item["operations"][0].__setitem__("separator", "\n\n"),
            "invalid_operation",
        ),
        (
            "argv missing slot",
            lambda item: item["tests"][0].__setitem__("argv", ["python", "${missing}"]),
            "invalid_test_spec",
        ),
        (
            "argv malformed placeholder",
            lambda item: item["tests"][0].__setitem__(
                "argv", ["python", "${bad-name}"]
            ),
            "invalid_test_spec",
        ),
        (
            "invalid slot type",
            lambda item: item["slots"][0].__setitem__("type", "unknown"),
            "invalid_slots",
        ),
        (
            "nonpositive version",
            lambda item: item.__setitem__("version", 0),
            "invalid_recipe",
        ),
    ],
)
def test_loader_rejects_kind_dependent_and_placeholder_contracts(
    tmp_path: Path, name: str, mutate, code: str
) -> None:
    root = copied_assets(tmp_path)
    manifest = (
        "python-validation-guard.json"
        if name == "prepend separator"
        else "python-fastmcp-read-tool.json"
    )
    rewrite_manifest(root, manifest, mutate)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == code


@pytest.mark.parametrize("body", [b"${bad-name}\n", b"${bad\n", b"${outer${inner}}\n"])
def test_loader_rejects_malformed_template_placeholder_syntax(
    tmp_path: Path, body: bytes
) -> None:
    root = copied_assets(tmp_path)
    digest = hashlib.sha256(body).hexdigest()
    (root / "templates" / "sha256" / digest).write_bytes(body)
    rewrite_manifest(
        root,
        "python-validation-guard.json",
        lambda item: item["operations"][0].__setitem__(
            "template_hash", "sha256:" + digest
        ),
    )
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "invalid_template_placeholder"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda item: item["constraints"][0].__setitem__("kind", "unknown"),
            "invalid_constraint",
        ),
        (
            lambda item: item["constraints"][0].__setitem__("value", 7),
            "invalid_constraint",
        ),
    ],
)
def test_loader_rejects_unsupported_bundled_constraints(
    tmp_path: Path, mutate, code: str
) -> None:
    root = copied_assets(tmp_path)
    rewrite_manifest(root, "python-fastmcp-read-tool.json", mutate)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == code


@pytest.mark.parametrize(
    "mutation", ["extra_blob", "missing_blob", "swapped_manifests"]
)
def test_loader_locks_complete_seed_inventory(tmp_path: Path, mutation: str) -> None:
    root = copied_assets(tmp_path)
    if mutation == "extra_blob":
        (root / "templates" / "sha256" / ("f" * 64)).write_bytes(b"extra\n")
    elif mutation == "missing_blob":
        (
            root
            / "templates"
            / "sha256"
            / LOCKED_SEEDS["python-pytest-regression.json"][2][7:]
        ).unlink()
    else:
        first = root / "recipes" / "python-fastmcp-read-tool.json"
        second = root / "recipes" / "python-pytest-regression.json"
        first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
        first.write_bytes(second_bytes)
        second.write_bytes(first_bytes)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "bundled_asset_mismatch"


def test_loader_rejects_self_consistent_replacement_seed(tmp_path: Path) -> None:
    root = copied_assets(tmp_path)
    body = b"def ${test_name}() -> None:\n    ${test_body}\n    # replacement\n"
    digest = hashlib.sha256(body).hexdigest()
    (root / "templates" / "sha256" / digest).write_bytes(body)
    locked = (
        root
        / "templates"
        / "sha256"
        / LOCKED_SEEDS["python-pytest-regression.json"][2][7:]
    )
    locked.unlink()
    rewrite_manifest(
        root,
        "python-pytest-regression.json",
        lambda item: item["operations"][0].__setitem__(
            "template_hash", "sha256:" + digest
        ),
    )
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "bundled_asset_mismatch"


def test_safe_read_rejects_atomic_manifest_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    root = copied_assets(tmp_path)
    target = root / "recipes" / "python-fastmcp-read-tool.json"
    replacement = target.with_suffix(".replacement")
    replacement.write_bytes(target.read_bytes())
    original_open = os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            replacement.replace(target)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "unsafe_asset_reference"


def test_safe_read_rejects_symlink_swap_after_safe_child(
    tmp_path: Path, monkeypatch
) -> None:
    root = copied_assets(tmp_path)
    target = root / "recipes" / "python-fastmcp-read-tool.json"
    backup = target.with_suffix(".backup")
    original_open = os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            target.replace(backup)
            try:
                target.symlink_to(backup)
            except OSError as exc:
                backup.replace(target)
                pytest.skip(f"symlink creation unavailable: {exc}")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(AtlasError) as captured:
        BundledRecipeLoader(root).load()
    assert captured.value.code == "unsafe_asset_reference"


def test_validator_main_returns_one_without_success_noise(monkeypatch, capsys) -> None:
    script = ROOT / "skills" / "code-atlas" / "scripts" / "validate_recipes.py"
    spec = importlib.util.spec_from_file_location(
        "validate_recipes_failure_test", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FailingLoader:
        def __init__(self, asset_root) -> None:
            pass

        def load(self):
            raise AtlasError("bundled_asset_mismatch")

    monkeypatch.setattr(module, "BundledRecipeLoader", FailingLoader)
    assert module.main() == 1
    assert capsys.readouterr() == ("", "")


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
