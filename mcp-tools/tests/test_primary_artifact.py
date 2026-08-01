"""Fail-closed and reproducible primary artifact contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / ".codex-plugin" / "build_main_artifact.py"
ALLOWLIST = ROOT / ".codex-plugin" / "main-artifact-allowlist.json"
SOURCE_MTIME = 347_155_200  # 1981-01-01T00:00:00Z, representable by ZIP.
EXPECTED_FILES = (
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "LICENSE",
    "mcp-tools/pyproject.toml",
    "mcp-tools/server.py",
    "mcp-tools/uv.lock",
)
EXPECTED_TREES = (
    "mcp-tools/bugkiller",
    "mcp-tools/devkit_atlas",
    "mcp-tools/devkit_relay",
    "mcp-tools/orchestrator",
    "mcp-tools/project_index",
)


def _load_allowlist(path: Path = ALLOWLIST) -> dict[str, Any]:
    assert path.is_file(), f"missing primary artifact allowlist: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_fixture(destination: Path) -> Path:
    plugin_root = destination / "plugin"
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    for relative in (".codex-plugin/plugin.json", ".mcp.json", "LICENSE"):
        source = ROOT / relative
        target = plugin_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    assert BUILDER.is_file(), f"missing primary artifact builder: {BUILDER}"
    shutil.copy2(BUILDER, plugin_root / ".codex-plugin" / BUILDER.name)
    shutil.copy2(ALLOWLIST, plugin_root / ".codex-plugin" / ALLOWLIST.name)

    for relative in EXPECTED_FILES[3:]:
        source = ROOT / relative
        target = plugin_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in EXPECTED_TREES:
        shutil.copytree(ROOT / relative, plugin_root / relative)

    excluded = {
        "mcp-tools/tests/not-runtime.py": "raise AssertionError('not packaged')\n",
        "skills/not-primary.txt": "legacy skill\n",
        "agents/not-primary.md": "legacy agent\n",
        "commands/not-primary.md": "legacy command\n",
        "hooks/not-primary.py": "legacy hook\n",
        "extensions/not-primary.txt": "legacy extension\n",
        ".claude-plugin/plugin.json": "{}\n",
        "mcp-tools/bugkiller/__pycache__/ignored.pyc": "cache\n",
    }
    for relative, content in excluded.items():
        target = plugin_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    for path in plugin_root.rglob("*"):
        if path.is_file():
            os.utime(path, (SOURCE_MTIME, SOURCE_MTIME))
    return plugin_root


def _run_builder(
    plugin_root: Path,
    output: Path,
    *,
    allowlist: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert BUILDER.is_file(), f"missing primary artifact builder: {BUILDER}"
    command = [
        sys.executable,
        str(BUILDER),
        "--plugin-root",
        str(plugin_root),
        "--allowlist",
        str(allowlist or plugin_root / ".codex-plugin" / ALLOWLIST.name),
        "--output",
        str(output),
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _expected_names(plugin_root: Path) -> list[str]:
    names: list[str] = list(EXPECTED_FILES)
    for relative in EXPECTED_TREES:
        tree = plugin_root / relative
        names.extend(
            path.relative_to(plugin_root).as_posix()
            for path in tree.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(names)


def test_primary_allowlist_is_explicit_and_runtime_only() -> None:
    allowlist = _load_allowlist()

    assert set(allowlist) == {"schema", "files", "trees"}
    assert allowlist["schema"] == "2718lab-devkit/main-artifact-allowlist-v1"
    assert tuple(allowlist["files"]) == EXPECTED_FILES
    assert tuple(allowlist["trees"]) == EXPECTED_TREES
    serialized = json.dumps(allowlist, ensure_ascii=False).casefold()
    for excluded in (
        "tests",
        "__pycache__",
        ".claude-plugin",
        "agents",
        "commands",
        "hooks",
        "skills",
        "extensions",
    ):
        assert excluded not in serialized


def test_python_project_and_lock_use_pep440_rc1_metadata() -> None:
    project_path = ROOT / "mcp-tools" / "pyproject.toml"
    lock_path = ROOT / "mcp-tools" / "uv.lock"
    assert project_path.is_file(), "missing independently runnable MCP project"
    assert lock_path.is_file(), "missing MCP runtime lock"

    with project_path.open("rb") as project_file:
        project = tomllib.load(project_file)
    assert project["project"]["version"] == "1.0.0rc1"
    assert project["project"]["dependencies"] == ["mcp[cli]>=1,<2"]
    assert "devkit_atlas" in project["tool"]["pyright"]["include"]
    assert "code_atlas" not in project["tool"]["pyright"]["include"]
    lock_text = lock_path.read_text(encoding="utf-8")
    assert 'name = "2718lab-devkit-mcp"' in lock_text
    assert 'version = "1.0.0rc1"' in lock_text


def test_two_builds_are_byte_identical_with_normalized_zip_metadata(
    tmp_path: Path,
) -> None:
    plugin_root = _copy_fixture(tmp_path)
    first = tmp_path / "primary-a.zip"
    second = tmp_path / "primary-b.zip"

    first_result = _run_builder(plugin_root, first)
    second_result = _run_builder(plugin_root, second)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first.read_bytes() == second.read_bytes()
    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        assert names == sorted(names)
        assert names == _expected_names(plugin_root)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all((info.external_attr >> 16) & 0o777 == 0o644 for info in infos)
        assert all("tests/" not in name for name in names)
        assert all("__pycache__/" not in name for name in names)


def test_builder_rejects_output_inside_plugin_root(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    output = plugin_root / "primary.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("files", "../outside.txt"),
        ("files", "LICENSE"),
        ("trees", "mcp-tools/bugkiller"),
    ],
)
def test_builder_rejects_traversal_and_duplicate_archive_inputs(
    tmp_path: Path,
    field: str,
    unsafe_value: str,
) -> None:
    plugin_root = _copy_fixture(tmp_path)
    allowlist = _load_allowlist(plugin_root / ".codex-plugin" / ALLOWLIST.name)
    values = list(allowlist[field])
    if unsafe_value in values:
        values.append(unsafe_value)
    else:
        values[0] = unsafe_value
    allowlist[field] = values
    malicious = plugin_root / ".codex-plugin" / "malicious-allowlist.json"
    malicious.write_text(json.dumps(allowlist), encoding="utf-8")
    output = tmp_path / "malicious.zip"

    result = _run_builder(plugin_root, output, allowlist=malicious)

    assert result.returncode != 0
    assert not output.exists()


def test_builder_rejects_junction_or_symlink_escape(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not ship\n", encoding="utf-8")
    link = plugin_root / "mcp-tools" / "bugkiller" / "escape"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        link.symlink_to(outside, target_is_directory=True)
    output = tmp_path / "escaped.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert not output.exists()
