"""Fail-closed and reproducible primary artifact contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / ".codex-plugin" / "build_main_artifact.py"
SECURE_IO = ROOT / ".codex-plugin" / "artifact_secure_io.py"
ALLOWLIST = ROOT / ".codex-plugin" / "main-artifact-allowlist.json"
MCP_CONFIG = ROOT / ".mcp.json"
SOURCE_MTIME = 347_155_200  # 1981-01-01T00:00:00Z, representable by ZIP.
EXPECTED_FILES = (
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "LICENSE",
    ".codex-plugin/fastlane_todo_projection.py",
    "mcp-tools/pyproject.toml",
    "mcp-tools/server.py",
    "mcp-tools/uv.lock",
    "skills/work-methodology/SKILL.md",
    "skills/work-methodology/assets/fastlane-quota-balance-policy-v1.json",
    "skills/work-methodology/assets/fastlane-routing-policy-v3.json",
    "skills/work-methodology/references/efficiency-automation.md",
    "skills/work-methodology/references/grounding-discipline.md",
    "skills/work-methodology/references/orchestration-runtime.md",
    "skills/work-methodology/references/team-patterns.md",
    "skills/work-methodology/references/verification-checklist.md",
    "skills/work-methodology/references/work-packages.md",
    "skills/work-methodology/scripts/codex_account_quota.py",
    "skills/work-methodology/scripts/fastlane_quota_balance.py",
    "skills/work-methodology/scripts/fastlane_routing.py",
    "skills/work-methodology/scripts/team_efficiency.py",
)
EXPECTED_TREES = (
    "mcp-tools/bugkiller",
    "mcp-tools/devkit_atlas",
    "mcp-tools/devkit_relay",
    "mcp-tools/devkit_runtime",
    "mcp-tools/orchestrator",
    "mcp-tools/project_index",
)
EXPECTED_BRIDGE_SELECTORS = (
    "CODEX_DEVKIT_HOST_BRIDGE_FD",
    "CODEX_DEVKIT_HOST_BRIDGE_HANDLE",
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
    if SECURE_IO.is_file():
        shutil.copy2(SECURE_IO, plugin_root / ".codex-plugin" / SECURE_IO.name)
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


def _load_builder_module() -> Any:
    spec = importlib.util.spec_from_file_location("_artifact_builder", BUILDER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        ".github",
        ".claude",
        "agents",
        "commands",
        "hooks",
        "extensions",
        "code_atlas",
        "cache",
        "evidence",
    ):
        assert excluded not in serialized


def test_primary_mcp_config_exports_only_locked_bridge_selectors() -> None:
    configuration = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))

    assert set(configuration) == {"mcpServers"}
    servers = configuration["mcpServers"]
    assert set(servers) == {"2718lab-devkit"}
    server = servers["2718lab-devkit"]
    assert set(server) == {"command", "args", "cwd", "env_vars"}
    assert tuple(server["env_vars"]) == EXPECTED_BRIDGE_SELECTORS
    serialized = json.dumps(configuration, ensure_ascii=False).casefold()
    for forbidden in (
        "bugkiller_home",
        "plugin_data",
        "codex_home",
        "bearer",
        "proof",
        "secret",
        "token",
    ):
        assert forbidden not in serialized


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
    assert "devkit_runtime" in project["tool"]["pyright"]["include"]
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


def _run_barrier_action(
    barrier: threading.Barrier,
    action: Any,
    errors: list[BaseException],
) -> None:
    try:
        barrier.wait(timeout=10)
        action()
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        return
    except BaseException as error:
        errors.append(error)
        barrier.abort()


def test_builder_never_archives_external_hardlink_swapped_before_source_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder_module()
    plugin_root = _copy_fixture(tmp_path)
    selected = plugin_root / ".mcp.json"
    expected = selected.read_bytes()
    external = tmp_path / "external-input-sentinel.txt"
    external_bytes = b"LUNA-EXTERNAL-INPUT-SENTINEL\n"
    external.write_bytes(external_bytes)
    output = tmp_path / "input-race.zip"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    original_read_bytes = Path.read_bytes
    swap_blocked = threading.Event()
    real_backend = builder.get_secure_backend()

    def swap_selected_source() -> None:
        try:
            selected.unlink()
            os.link(external, selected)
        except OSError:
            swap_blocked.set()

    attacker = threading.Thread(
        target=_run_barrier_action,
        args=(barrier, swap_selected_source, errors),
        daemon=True,
    )

    class BarrierRoot:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __enter__(self) -> Any:
            self.inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            self.inner.__exit__(*args)

        def read_control(self, relative_parts: Any) -> bytes:
            return self.inner.read_control(relative_parts)

        def freeze_file(
            self,
            relative_parts: Any,
            archive_name: str,
            spool: Any,
        ) -> Any:
            if tuple(relative_parts) == (".mcp.json",):
                barrier.wait(timeout=10)
                barrier.wait(timeout=10)
            return self.inner.freeze_file(relative_parts, archive_name, spool)

        def freeze_tree(
            self,
            relative_parts: Any,
            selected_names: set[str],
            spool: Any,
        ) -> Any:
            return self.inner.freeze_tree(relative_parts, selected_names, spool)

    class BarrierBackend:
        def open_root(self, path: Path) -> BarrierRoot:
            return BarrierRoot(real_backend.open_root(path))

        def open_output_parent(
            self,
            path: Path,
            *,
            source_root: BarrierRoot,
        ) -> Any:
            return real_backend.open_output_parent(
                path,
                source_root=source_root.inner,
            )

    monkeypatch.setattr(builder, "get_secure_backend", BarrierBackend)
    attacker.start()
    rejected = False
    try:
        builder.build_main_artifact(plugin_root, output)
    except builder.ArtifactBuildError:
        rejected = True
    finally:
        if attacker.is_alive():
            barrier.abort()
        attacker.join(timeout=10)

    assert not attacker.is_alive()
    assert not errors
    assert original_read_bytes(external) == external_bytes
    assert rejected or swap_blocked.is_set()
    if not rejected:
        with zipfile.ZipFile(output) as archive:
            archived = archive.read(".mcp.json")
        assert archived == expected, (
            hashlib.sha256(expected).hexdigest(),
            hashlib.sha256(archived).hexdigest(),
            hashlib.sha256(external_bytes).hexdigest(),
        )


def test_builder_never_mutates_output_hardlink_inserted_before_zip_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder_module()
    plugin_root = _copy_fixture(tmp_path)
    output = tmp_path / "output-race.zip"
    external = tmp_path / "external-output-sentinel.bin"
    external_bytes = b"LUNA-EXTERNAL-OUTPUT-SENTINEL\n"
    external.write_bytes(external_bytes)
    before = external.stat()
    before_hash = hashlib.sha256(external_bytes).hexdigest()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    real_backend = builder.get_secure_backend()

    def insert_output_hardlink() -> None:
        os.link(external, output)

    attacker = threading.Thread(
        target=_run_barrier_action,
        args=(barrier, insert_output_hardlink, errors),
        daemon=True,
    )

    class BarrierPublisher:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __enter__(self) -> Any:
            self.inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            self.inner.__exit__(*args)

        def create_private_spool(self) -> Any:
            return self.inner.create_private_spool()

        def create_zip_temp(self) -> Any:
            return self.inner.create_zip_temp()

        def publish(self, destination_name: str) -> None:
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
            self.inner.publish(destination_name)

    class BarrierBackend:
        def open_root(self, path: Path) -> Any:
            return real_backend.open_root(path)

        def open_output_parent(
            self,
            path: Path,
            *,
            source_root: Any,
        ) -> BarrierPublisher:
            return BarrierPublisher(
                real_backend.open_output_parent(path, source_root=source_root)
            )

    monkeypatch.setattr(builder, "get_secure_backend", BarrierBackend)
    attacker.start()
    rejected = False
    try:
        builder.build_main_artifact(plugin_root, output)
    except builder.ArtifactBuildError:
        rejected = True
    finally:
        if attacker.is_alive():
            barrier.abort()
        attacker.join(timeout=10)

    assert not attacker.is_alive()
    assert not errors
    after = external.stat()
    after_hash = hashlib.sha256(external.read_bytes()).hexdigest()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after_hash == before_hash, (before_hash, after_hash)
    if not rejected:
        assert output.is_file()


def test_builder_rejects_static_selected_hardlink(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    selected = plugin_root / ".mcp.json"
    external_alias = tmp_path / "external-selected-alias.json"
    os.link(selected, external_alias)
    output = tmp_path / "static-source-hardlink.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert "ARTIFACT_SOURCE_HARDLINK" in result.stderr
    assert not output.exists()


def test_builder_rejects_two_selected_names_for_one_inode(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    selected = plugin_root / ".mcp.json"
    inside_alias = plugin_root / "mcp-tools" / "bugkiller" / "inside-alias.json"
    os.link(selected, inside_alias)
    output = tmp_path / "inside-source-hardlink.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert "ARTIFACT_SOURCE_HARDLINK" in result.stderr
    assert not output.exists()


def test_builder_rejects_hardlinked_allowlist(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    allowlist = plugin_root / ".codex-plugin" / ALLOWLIST.name
    external_alias = tmp_path / "external-allowlist-alias.json"
    os.link(allowlist, external_alias)
    output = tmp_path / "hardlinked-allowlist.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert "ARTIFACT_SOURCE_HARDLINK" in result.stderr
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode contract")
def test_builder_rejects_source_with_preheld_writable_handle(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    selected = plugin_root / ".mcp.json"
    output = tmp_path / "preheld-writer.zip"

    with selected.open("r+b"):
        result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert "ARTIFACT_SOURCE_UNSAFE" in result.stderr
    assert not output.exists()


def test_builder_replaces_output_hardlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    plugin_root = _copy_fixture(tmp_path)
    output = tmp_path / "preexisting-output-hardlink.zip"
    external = tmp_path / "external-output-target.bin"
    external_bytes = b"EXTERNAL-OUTPUT-TARGET\n"
    external.write_bytes(external_bytes)
    before = external.stat()
    os.link(external, output)

    result = _run_builder(plugin_root, output)

    assert result.returncode == 0, result.stderr
    after = external.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert external.read_bytes() == external_bytes
    assert output.read_bytes() != external_bytes
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == _expected_names(plugin_root)


def test_builder_replaces_output_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    plugin_root = _copy_fixture(tmp_path)
    output = tmp_path / "preexisting-output-symlink.zip"
    external = tmp_path / "external-output-target.bin"
    external_bytes = b"EXTERNAL-SYMLINK-TARGET\n"
    external.write_bytes(external_bytes)
    try:
        output.symlink_to(external)
    except OSError as error:
        pytest.skip(f"file symlink creation unavailable: {error}")

    result = _run_builder(plugin_root, output)

    assert result.returncode == 0, result.stderr
    assert external.read_bytes() == external_bytes
    assert output.is_file()
    assert not output.is_symlink()
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == _expected_names(plugin_root)


def test_builder_requires_existing_output_parent(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    output = tmp_path / "missing" / "parent" / "primary.zip"

    result = _run_builder(plugin_root, output)

    assert result.returncode != 0
    assert "ARTIFACT_OUTPUT_UNSAFE" in result.stderr
    assert not output.parent.exists()


def test_builder_preserves_existing_output_on_private_zip_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder_module()
    plugin_root = _copy_fixture(tmp_path)
    output = tmp_path / "preserved-on-failure.zip"
    original = b"PREVIOUS-COMPLETE-ARTIFACT\n"
    output.write_bytes(original)
    before = output.stat()

    def fail_private_zip(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected private ZIP failure")

    monkeypatch.setattr(builder, "_write_deterministic_zip", fail_private_zip)
    with pytest.raises(builder.ArtifactBuildError, match="ARTIFACT_IO_FAILED"):
        builder.build_main_artifact(plugin_root, output)

    after = output.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert output.read_bytes() == original


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


def test_builder_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    plugin_root = _copy_fixture(tmp_path)
    allowlist = _load_allowlist(plugin_root / ".codex-plugin" / ALLOWLIST.name)
    payload = (
        f"{{"
        f'"schema":"{allowlist["schema"]}",'
        f'"files":{json.dumps(allowlist["files"], ensure_ascii=False)},'
        f'"trees":{json.dumps(allowlist["trees"], ensure_ascii=False)},'
        f'"schema":"{allowlist["schema"]}"'
        f"}}"
    )
    malicious = (
        plugin_root / ".codex-plugin" / "malicious-allowlist-duplicate-keys.json"
    )
    malicious.write_text(payload, encoding="utf-8")
    output = tmp_path / "duplicate-json-keys.zip"

    result = _run_builder(plugin_root, output, allowlist=malicious)

    assert result.returncode != 0
    assert "duplicate" in result.stderr.casefold()
    assert not output.exists()


def test_builder_rejects_nfc_casefold_archive_aliases(tmp_path: Path) -> None:
    builder = _load_builder_module()
    root = tmp_path
    first = root / "nfc-alias-a\u0300.txt"
    second = root / "nfc-alias-à.txt"
    first.write_text("decomposed\n", encoding="utf-8")
    second.write_text("composed\n", encoding="utf-8")
    assert first.name != second.name
    assert (
        unicodedata.normalize("NFC", first.name).casefold()
        == unicodedata.normalize(
            "NFC",
            second.name,
        ).casefold()
    )
    assert first.name.casefold() != second.name.casefold()

    selected: dict[str, Path] = {}
    aliases: dict[str, str] = {}
    builder._add_file(selected, aliases, root, first)
    with pytest.raises(builder.ArtifactBuildError, match="duplicate"):
        builder._add_file(selected, aliases, root, second)
