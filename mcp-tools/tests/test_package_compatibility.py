"""The dual-package preflight is diagnostic and cannot grant host capabilities."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "package_compatibility", ROOT / ".codex-plugin/check_compatibility.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "package"
    for rel in (
        "plugin.json",
        "mcp.json",
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "mcp-tools/pyproject.toml",
        "mcp-tools/uv.lock",
        "mcp-tools/server.py",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    for name in ("code-atlas", "fast-lane-routing", "workflow-design"):
        target = root / "skills" / name
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test manual\n---\n", encoding="utf-8"
        )
    return root


def test_checkout_preflight_is_read_only_and_never_claims_host_support():
    result = CHECK.inspect_package(ROOT)
    assert result["ok"]
    assert result["execution_authorized"] is False
    assert "agent_dispatch" in result["not_verified"]
    assert "model_or_effort_availability" in result["not_verified"]


def test_artifact_subset_is_valid(package):
    assert CHECK.inspect_package(package)["ok"]


@pytest.mark.parametrize(
    "file,key,value",
    [
        ("plugin.json", "version", "0.0.0"),
        ("plugin.json", "name", "other"),
        ("plugin.json", "skills", "../outside"),
        ("plugin.json", "extensions", {"com.openai": {"hooks": "./unreviewed.json"}}),
        ("mcp.json", "$schema", "https://invalid.example/schema"),
        ("mcp.json", "env_vars", ["SECRET"]),
    ],
)
def test_manifest_drift_is_rejected(package, file, key, value):
    path = package / file
    data = json.loads(path.read_text())
    data[key] = value
    path.write_text(json.dumps(data))
    assert CHECK.inspect_package(package)["ok"] is False


@pytest.mark.parametrize(
    "update",
    [
        {"cwd": "../escape"},
        {"command": "sh"},
        {"env_vars": ["PRIVATE"]},
        {"type": "streamable-http"},
        {"args": ["run", "--unlocked"]},
    ],
)
def test_portable_launch_cannot_drift(package, update):
    path = package / "mcp.json"
    data = json.loads(path.read_text())
    data["mcpServers"]["2718lab-devkit"].update(update)
    path.write_text(json.dumps(data))
    assert not CHECK.inspect_package(package)["ok"]


def test_duplicate_keys_and_oversized_inputs_are_bounded(package):
    path = package / "plugin.json"
    path.write_text('{"name":"first","name":"second"}')
    report = CHECK.inspect_package(package)
    assert report["checks"][0]["reason"] == "duplicate_json_key"
    path.write_text("x" * (CHECK.MAX_BYTES + 1))
    assert (
        CHECK.inspect_package(package)["checks"][0]["reason"]
        == "file_type_or_size_invalid"
    )


def test_missing_private_data_is_not_leaked(package):
    (package / "mcp.json").unlink()
    text = json.dumps(CHECK.inspect_package(package))
    assert str(package) not in text
    assert "invalid_or_missing_package_data" in text


def test_symlinked_component_is_rejected(package, tmp_path):
    path = package / "plugin.json"
    outside = tmp_path / "outside.json"
    path.replace(outside)
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert (
        CHECK.inspect_package(package)["checks"][0]["reason"] == "symlink_not_allowed"
    )


def test_cli_only_prints_a_diagnostic(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".codex-plugin/check_compatibility.py"),
            "--plugin-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["execution_authorized"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "allowlist", ["main-artifact-allowlist.json", "marketplace-artifact-allowlist.json"]
)
def test_extracted_artifact_passes_self_contained_check(tmp_path, allowlist):
    import zipfile

    archive = tmp_path / "plugin.zip"
    built = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".codex-plugin/build_main_artifact.py"),
            "--plugin-root",
            str(ROOT),
            "--allowlist",
            str(ROOT / ".codex-plugin" / allowlist),
            "--output",
            str(archive),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extracted)
    report = subprocess.run(
        [
            sys.executable,
            str(extracted / ".codex-plugin/check_compatibility.py"),
            "--plugin-root",
            str(extracted),
        ],
        capture_output=True,
        text=True,
    )
    assert report.returncode == 0, report.stdout + report.stderr
    assert json.loads(report.stdout)["execution_authorized"] is False
