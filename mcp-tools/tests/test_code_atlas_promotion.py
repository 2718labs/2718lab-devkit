"""Public CLI contract tests for deterministic Code Atlas promotion export."""

from __future__ import annotations

import importlib.util
import errno
import hashlib
import os
import sqlite3
from pathlib import Path

from dataclasses import replace

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.canonical import canonical_hash
from code_atlas.models import (
    AtlasEdge,
    AtlasNode,
    ConstraintSpec,
    EdgeRelation,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
)
from code_atlas.store import AtlasStore


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "code-atlas" / "scripts" / "export_recipe.py"


def test_export_recipe_script_exposes_a_public_main_entrypoint() -> None:
    specification = importlib.util.spec_from_file_location("export_recipe", SCRIPT)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    assert callable(module.main)


def _export_module():
    specification = importlib.util.spec_from_file_location("export_recipe", SCRIPT)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _observed_payload(manifest: RecipeManifest) -> dict[str, object]:
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


def _stored_local_recipe(data_root: Path) -> str:
    store = AtlasStore(data_root / "code-atlas.sqlite3", data_root / "code-atlas-cas")
    template = b"def ${symbol_000}() -> int:\n    return 1\n"
    template_hash = "sha256:" + hashlib.sha256(template).hexdigest()
    manifest = RecipeManifest(
        recipe_id="",
        recipe_key="python.promotion-fixture",
        version=1,
        intent_id="python.promotion-fixture",
        language_name="python",
        language_extractor_version="1",
        repository_signature="sha256:" + "1" * 64,
        layer="local",
        manifest_hash="",
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
    manifest = replace(
        manifest, manifest_hash=canonical_hash(_observed_payload(manifest))
    )
    root_payload = manifest.to_dict()
    del root_payload["recipe_id"]
    recipe_root = AtlasNode.create(
        NodeKind.RECIPE,
        root_payload,
        extractor_id="python-ast",
        extractor_version="1",
        provenance="observed",
        source_hashes=(manifest.manifest_hash,),
    )
    registered = replace(manifest, recipe_id=recipe_root.node_id)
    template_node = AtlasNode.create(
        NodeKind.CODE_TEMPLATE,
        {"kind": "create_python_file", "template_hash": template_hash},
        extractor_id="python-ast",
        extractor_version="1",
        provenance="observed",
        source_hashes=(manifest.manifest_hash,),
    )
    edge = AtlasEdge.create(
        EdgeRelation.HAS_IMPLEMENTATION,
        recipe_root,
        template_node,
        provenance="observed",
    )
    store.put_nodes((recipe_root, template_node))
    store.put_edges((edge,))
    store.put_recipe(
        registered,
        node_ids=(recipe_root.node_id, template_node.node_id),
        edge_ids=(edge.edge_id,),
    )
    store.put_blob(template_hash, template, media_type="text/x-python")
    store.close()
    return registered.recipe_id


def test_export_promotes_one_verified_local_recipe_deterministically(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "durable"
    recipe_id = _stored_local_recipe(data_root)
    first = tmp_path / "first"
    second = tmp_path / "second"
    exporter = _export_module()
    before = {
        path.relative_to(data_root).as_posix(): path.read_bytes()
        for path in data_root.rglob("*")
        if path.is_file()
    }

    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(first),
            ]
        )
        == 0
    )
    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(second),
            ]
        )
        == 0
    )

    first_files = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    assert set(first_files) == {
        "manifest.json",
        "pattern-card.md",
        "promotion-receipt.json",
        "templates/sha256/"
        + next(
            path.name
            for path in (first / "templates" / "sha256").iterdir()
            if path.is_file()
        ),
    }
    assert str(data_root).encode("utf-8") not in first_files["promotion-receipt.json"]
    after = {
        path.relative_to(data_root).as_posix(): path.read_bytes()
        for path in data_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_bundle_publish_does_not_call_check_then_os_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _export_module()
    parent = tmp_path / "output-parent"
    parent.mkdir()
    output = parent / "bundle"

    def forbidden_rename(*_args, **_kwargs) -> None:
        raise AssertionError("final promotion must use atomic no-replace")

    monkeypatch.setattr(exporter.os, "rename", forbidden_rename)

    exporter._write_bundle(parent, output, {"receipt.txt": b"verified\n"})

    assert (output / "receipt.txt").read_bytes() == b"verified\n"


def test_native_atomic_noreplace_never_overwrites_an_existing_directory(
    tmp_path: Path,
) -> None:
    exporter = _export_module()
    stage = tmp_path / "stage"
    destination = tmp_path / "destination"
    stage.mkdir()
    destination.mkdir()
    sentinel = destination / "sentinel.txt"
    sentinel.write_bytes(b"keep")

    try:
        exporter._atomic_noreplace_directory(stage, destination)
    except FileExistsError:
        pass
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            pytest.skip("atomic no-replace directory promotion is unavailable")
        raise
    else:
        pytest.fail("atomic no-replace promotion overwrote an existing directory")

    assert stage.exists()
    assert sentinel.read_bytes() == b"keep"


def test_bundle_publish_rejects_a_destination_created_at_atomic_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _export_module()
    parent = tmp_path / "output-parent"
    parent.mkdir()
    output = parent / "bundle"

    def contender(_stage: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "attacker.txt").write_bytes(b"keep")
        raise FileExistsError(destination)

    monkeypatch.setattr(exporter, "_atomic_noreplace_directory", contender)

    with pytest.raises(exporter.PromotionError, match="promotion_output_raced"):
        exporter._write_bundle(parent, output, {"receipt.txt": b"verified\n"})

    assert (output / "attacker.txt").read_bytes() == b"keep"


def test_bundle_publish_fails_closed_when_the_output_parent_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _export_module()
    parent = tmp_path / "output-parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    output = parent / "bundle"
    original_write = exporter._write_file
    replaced = False

    def replace_parent_after_first_write(path: Path, body: bytes) -> None:
        nonlocal replaced
        original_write(path, body)
        if not replaced:
            replaced = True
            os.replace(parent, outside)
            parent.mkdir()

    monkeypatch.setattr(exporter, "_write_file", replace_parent_after_first_write)

    with pytest.raises(exporter.PromotionError, match="promotion_output_raced"):
        exporter._write_bundle(
            parent,
            output,
            {"a.txt": b"first\n", "b.txt": b"second\n"},
        )

    assert not output.exists()
    assert not tuple(outside.rglob("b.txt"))


def test_export_refuses_parent_replacement_between_validation_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _export_module()
    data_root = tmp_path / "durable"
    recipe_id = _stored_local_recipe(data_root)
    parent = tmp_path / "output-parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    output = parent / "bundle"
    original_write_bundle = exporter._write_bundle

    def replace_parent_then_write(*args, **kwargs) -> None:
        os.replace(parent, outside)
        parent.mkdir()
        return original_write_bundle(*args, **kwargs)

    monkeypatch.setattr(exporter, "_write_bundle", replace_parent_then_write)

    with pytest.raises(exporter.PromotionError, match="promotion_output_raced"):
        exporter.export_recipe(data_root, recipe_id, output)

    assert not output.exists()
    assert not (outside / "bundle").exists()


def test_export_rejects_tampered_recipe_metadata_and_cas_without_writing_output(
    tmp_path: Path,
) -> None:
    exporter = _export_module()
    data_root = tmp_path / "durable"
    recipe_id = _stored_local_recipe(data_root)
    template = b"def ${symbol_000}() -> int:\n    return 1\n"
    digest = hashlib.sha256(template).hexdigest()
    blob = data_root / "code-atlas-cas" / "sha256" / digest[:2] / digest[2:]
    blob.write_bytes(b"def ${symbol_000}() -> int:\n    return 2\n")
    cas_output = tmp_path / "cas-output"

    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(cas_output),
            ]
        )
        == 1
    )
    assert not cas_output.exists()
    blob.write_bytes(template)

    connection = sqlite3.connect(data_root / "code-atlas.sqlite3")
    try:
        connection.execute(
            "UPDATE atlas_recipes SET manifest_hash=? WHERE recipe_id=?",
            ("sha256:" + "f" * 64, recipe_id),
        )
        connection.commit()
    finally:
        connection.close()
    metadata_output = tmp_path / "metadata-output"

    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(metadata_output),
            ]
        )
        == 1
    )
    assert not metadata_output.exists()


def test_export_rejects_existing_overlap_and_linked_output_paths(
    tmp_path: Path,
) -> None:
    exporter = _export_module()
    data_root = tmp_path / "durable"
    recipe_id = _stored_local_recipe(data_root)
    existing = tmp_path / "existing-output"
    existing.mkdir()
    sentinel = existing / "sentinel.txt"
    sentinel.write_bytes(b"do-not-overwrite")

    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(existing),
            ]
        )
        == 1
    )
    assert sentinel.read_bytes() == b"do-not-overwrite"

    overlap = data_root / "must-not-write"
    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(overlap),
            ]
        )
        == 1
    )
    assert not overlap.exists()

    real_parent = tmp_path / "real-parent"
    linked_parent = tmp_path / "linked-parent"
    real_parent.mkdir()
    try:
        os.symlink(real_parent, linked_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable for this test account")
    linked_output = linked_parent / "bundle"

    assert (
        exporter.main(
            [
                "--data-root",
                str(data_root),
                "--recipe-id",
                recipe_id,
                "--output",
                str(linked_output),
            ]
        )
        == 1
    )
    assert not (real_parent / "bundle").exists()
