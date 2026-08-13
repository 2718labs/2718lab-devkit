"""Canonical Atlas package cutover contracts."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_atlas_package_exposes_canonical_public_types_and_embedded_assets(
    tmp_path: Path,
) -> None:
    import devkit_atlas
    from devkit_atlas.receipts import (
        EVIDENCE_KEY_FILENAME,
        ReceiptRepository,
    )
    from devkit_atlas.routing import load_host_profiles
    from devkit_atlas.service import (
        AcceptedAtlasProjectionEvidence,
        AcceptedAtlasProjectionRequest,
        AtlasService,
    )

    package_root = Path(devkit_atlas.__file__).resolve().parent

    assert AtlasService.__name__ == "AtlasService"
    assert AcceptedAtlasProjectionRequest.__name__ == "AcceptedAtlasProjectionRequest"
    assert AcceptedAtlasProjectionEvidence.__name__ == "AcceptedAtlasProjectionEvidence"
    assert devkit_atlas.ASSET_ROOT == package_root / "assets"
    assert (package_root / "assets" / "host-profiles.json").is_file()
    assert (package_root / "assets" / "recipes").is_dir()
    assert "hosts" in load_host_profiles()
    assert EVIDENCE_KEY_FILENAME == "atlas-evidence.key"
    assert (
        ReceiptRepository(tmp_path).receipt_root
        == tmp_path / "atlas-receipts" / "sha256"
    )
    assert importlib.util.find_spec("code_atlas") is None


def test_atlas_runtime_is_self_contained_without_a_skills_tree(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    for package in (
        "devkit_atlas",
        "devkit_continuity",
        "devkit_relay",
        "devkit_runtime",
        "orchestrator",
        "project_index",
    ):
        shutil.copytree(source_root / package, runtime_root / package)

    assert not (runtime_root / "skills").exists()
    expected_assets = {
        "host-profiles.json",
        "recipes/python-fastmcp-read-tool.json",
        "recipes/python-pytest-regression.json",
        "recipes/python-validation-guard.json",
        "templates/sha256/47820213e5b67e968dff12d9509b8990d7aa1a6465a85b631599628589f8d8de",
        "templates/sha256/c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950",
        "templates/sha256/d07c7a977b330d26fe6486ab5e07f02855117f092ad19b0b08b2aafb136aef6c",
    }
    assets = runtime_root / "devkit_atlas" / "assets"
    actual_assets = {
        path.relative_to(assets).as_posix()
        for path in assets.rglob("*")
        if path.is_file()
    }
    assert actual_assets == expected_assets
    for relative in expected_assets:
        asset = assets / relative
        assert not asset.is_symlink()
        assert not (getattr(asset.lstat(), "st_file_attributes", 0) & 0x400)

    script = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(runtime_root)!r})
import devkit_atlas
from devkit_atlas.recipes import BundledRecipeLoader
from devkit_atlas.routing import HOST_PROFILES
from devkit_atlas.service import AtlasService
from devkit_atlas.store import AtlasStore
from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.project_checkpoint import open_project_checkpoint_rw

loader = BundledRecipeLoader(devkit_atlas.ASSET_ROOT)
recipes = loader.load()
assert len(recipes) == 3
assert len(loader.materialize().nodes) > 3
assert set(HOST_PROFILES) == {{"schema_version", "hosts"}}
state = Path({str(runtime_root)!r}) / "state"
state.mkdir()
scratch = Path({str(runtime_root)!r}) / "scratch"
scratch.mkdir()
config = RuntimeConfig.load(
    environ={{"PLUGIN_DATA": str(state), "CODEX_TASK_TEMP": str(scratch)}}
)
RuntimeBootstrap.run(config)
store = AtlasStore(config.atlas_database)
runtime = open_project_checkpoint_rw(
    config.project_index_database,
    config.checkpoint_cas_root,
    scratch_root=config.scratch_root,
)
try:
    AtlasService(store, loader, runtime.project_index)
finally:
    runtime.close()
    store.close()
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=runtime_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
