"""Runtime-owned Project Index and checkpoint integration tests."""

from __future__ import annotations

import ast
import inspect
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.project_checkpoint import (
    open_project_checkpoint_ro,
    open_project_checkpoint_rw,
)
from devkit_runtime.sqlite_snapshot import VerifiedSqliteSnapshot
from devkit_runtime.workspace_authority import VerifiedWorkspaceAccess
from project_index.checkpoints import CheckpointService, WorkspaceOwnership
from project_index.models import IndexError
from project_index.service import ProjectIndexService
from project_index.store import ProjectIndexStore, StoreError


def _file_state(path: Path) -> tuple[tuple[str, bytes], ...]:
    """Capture only durable files below one test-controlled root."""

    if not path.exists():
        return ()
    return tuple(
        sorted(
            (
                candidate.relative_to(path).as_posix(),
                candidate.read_bytes(),
            )
            for candidate in path.rglob("*")
            if candidate.is_file()
        )
    )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _prepared_database(
    tmp_path: Path,
) -> tuple[Path, Path, str, str, Path, Path]:
    """Explicitly bootstrap state before exercising zero-write runtime opens."""

    bootstrap_scratch = tmp_path.parent / f"{tmp_path.name}-bootstrap-scratch"
    bootstrap_scratch.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path),
            "CODEX_TASK_TEMP": str(bootstrap_scratch),
        }
    )

    def prepare_proof_registry(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_marker (id INTEGER)"
            )

    RuntimeBootstrap.run(config, proof_registry_bootstrap=prepare_proof_registry)
    database_path = config.project_index_database
    cas_root = config.checkpoint_cas_root
    original = tmp_path / "original"
    workspace = tmp_path / "workspace"
    original.mkdir()
    _git("init", "-b", "main", cwd=original)
    (original / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=original)
    _git(
        "-c",
        "user.name=Runtime Test",
        "-c",
        "user.email=runtime@example.invalid",
        "commit",
        "-m",
        "seed",
        cwd=original,
    )
    _git("worktree", "add", "-b", "runtime-task", str(workspace), cwd=original)
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    runtime = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=bootstrap_scratch,
    )
    try:
        workspace_id = runtime.project_index.project_index_register(workspace)
        snapshot_id = runtime.project_index.sync(workspace_id).snapshot_id
    finally:
        runtime.close()
    return database_path, cas_root, workspace_id, snapshot_id, workspace, original


def _assert_error(code: str, operation) -> None:
    with pytest.raises(IndexError) as captured:
        operation()
    assert captured.value.code == code


def test_legacy_constructors_reject_missing_storage_without_artifacts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing" / "project-index.sqlite3"
    cas_root = tmp_path / "missing" / "checkpoint-cas"
    before = _file_state(tmp_path)

    with pytest.raises(StoreError):
        ProjectIndexStore(database_path)
    assert _file_state(tmp_path) == before

    _assert_error("INDEX_UNAVAILABLE", lambda: ProjectIndexService(database_path))
    assert _file_state(tmp_path) == before

    _assert_error(
        "INDEX_UNAVAILABLE",
        lambda: CheckpointService(database_path, cas_root, object()),
    )
    assert _file_state(tmp_path) == before


def test_checkpoint_service_status_requires_a_workspace_boundary() -> None:
    assert tuple(inspect.signature(CheckpointService.status).parameters) == (
        "self",
        "workspace_id",
        "checkpoint_id",
    )


def test_production_retirement_inventory_keeps_only_bootstrap_constructors() -> None:
    source_root = Path(__file__).resolve().parents[1]
    sources = {
        relative: ast.parse((source_root / relative).read_text(encoding="utf-8"))
        for relative in (
            "project_index/service.py",
            "project_index/store.py",
            "project_index/checkpoints.py",
            "devkit_atlas/service.py",
            "server.py",
            "devkit_runtime/bootstrap.py",
        )
    }
    constructor_callers: set[str] = set()
    for relative, tree in sources.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {
                    "ProjectIndexStore",
                    "ProjectIndexService",
                    "CheckpointService",
                }:
                    constructor_callers.add(relative)

    assert constructor_callers == {"devkit_runtime/bootstrap.py"}
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "_workspace_root"
        for relative, tree in sources.items()
        if relative != "devkit_runtime/bootstrap.py"
        for node in ast.walk(tree)
    )


def test_positive_test_callers_use_runtime_openers_not_legacy_constructors() -> None:
    source_root = Path(__file__).resolve().parents[1]
    sources = {
        relative: ast.parse((source_root / relative).read_text(encoding="utf-8"))
        for relative in (
            "devkit_runtime/bootstrap.py",
            "tests/test_atlas_acceptance.py",
            "tests/test_atlas_service.py",
            "tests/test_project_index_checkpoints.py",
            "tests/test_project_index_core.py",
            "tests/test_project_index_registry_v3.py",
        )
    }
    constructor_callers: set[str] = set()
    for relative, tree in sources.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"ProjectIndexService", "CheckpointService"}:
                    constructor_callers.add(relative)

    assert constructor_callers == {"devkit_runtime/bootstrap.py"}


def test_readonly_bundle_uses_one_r0_snapshot_without_durable_side_effects(
    tmp_path: Path,
) -> None:
    database_path, cas_root, workspace_id, _, _, _ = _prepared_database(tmp_path)
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    database_state = _file_state(database_path.parent)
    cas_state = _file_state(cas_root)

    runtime = open_project_checkpoint_ro(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    try:
        assert isinstance(runtime.snapshot, VerifiedSqliteSnapshot)
        assert (
            runtime.project_index._store._connection.execute(
                "PRAGMA query_only"
            ).fetchone()[0]
            == 1
        )
        assert (
            runtime._checkpoints._connection.execute("PRAGMA query_only").fetchone()[0]
            == 1
        )
        assert runtime.project_index.status(workspace_id).snapshot_id is not None
        with pytest.raises(IndexError) as captured:
            runtime.status(workspace_id, "missing-checkpoint")
        assert captured.value.code == "NOT_FOUND"
    finally:
        runtime.close()

    assert _file_state(database_path.parent) == database_state
    assert _file_state(cas_root) == cas_state


def test_readwrite_bundle_shares_one_connection_and_persists_query_receipt(
    tmp_path: Path,
) -> None:
    database_path, cas_root, workspace_id, snapshot_id, _, _ = _prepared_database(
        tmp_path
    )
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()

    runtime = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    connection = runtime.project_index._store._connection
    assert connection is runtime._checkpoints._connection
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    result = runtime.query(workspace_id, snapshot_id, "VALUE")
    assert (
        runtime.project_index.get_query_receipt(result.trace_id).trace_id
        == result.trace_id
    )
    runtime.close()
    runtime.close()

    with pytest.raises(Exception):
        connection.execute("SELECT 1")
    reopened = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    try:
        assert (
            reopened.project_index.get_query_receipt(result.trace_id).trace_id
            == result.trace_id
        )
    finally:
        reopened.close()


def test_readwrite_open_has_no_durable_database_or_cas_side_effects(
    tmp_path: Path,
) -> None:
    database_path, cas_root, _, _, _, _ = _prepared_database(tmp_path)
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    database_state = _file_state(database_path.parent)
    cas_state = _file_state(cas_root)

    runtime = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    runtime.close()

    assert _file_state(database_path.parent) == database_state
    assert _file_state(cas_root) == cas_state


def test_runtime_status_requires_a_workspace_id(
    tmp_path: Path,
) -> None:
    database_path, cas_root, workspace_id, snapshot_id, _, _ = _prepared_database(
        tmp_path
    )
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    runtime = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    try:
        ownership = WorkspaceOwnership(
            workflow_id="workflow",
            task_id="task",
            owner="owner",
            lease_epoch=1,
            workspace_id=workspace_id,
            write_scope=("module.py",),
        )
        checkpoint = runtime.create(workspace_id, ownership, snapshot_id)

        assert runtime.status(workspace_id, checkpoint.checkpoint_id) == checkpoint
        assert not hasattr(runtime, "checkpoints")
        assert not hasattr(runtime, "checkpoint_status")
        with pytest.raises(TypeError):
            runtime.status(checkpoint.checkpoint_id)  # type: ignore[call-arg]
    finally:
        runtime.close()


def test_readonly_open_rejects_missing_prepared_storage_without_creating_it(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    before = _file_state(tmp_path)

    _assert_error(
        "INDEX_UNAVAILABLE",
        lambda: open_project_checkpoint_ro(
            tmp_path / "missing.sqlite3",
            tmp_path / "missing-cas",
            scratch_root=scratch_root,
        ),
    )

    assert _file_state(tmp_path) == before


def test_workspace_authority_rejects_forged_missing_and_rebound_ids(
    tmp_path: Path,
) -> None:
    database_path, cas_root, workspace_id, _, workspace, _ = _prepared_database(
        tmp_path
    )
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    runtime = open_project_checkpoint_ro(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    try:
        access = runtime.workspace_authority.resolve(workspace_id)
        assert access.workspace_id == workspace_id
        assert access.root == workspace
        with pytest.raises(TypeError):
            VerifiedWorkspaceAccess(object(), workspace_id, workspace)
        _assert_error(
            "WORKSPACE_UNREGISTERED",
            lambda: runtime.status("sha256:" + "0" * 64, "missing"),
        )
        _assert_error(
            "NOT_FOUND",
            lambda: runtime.status(workspace_id, "missing"),
        )
        workspace.rename(tmp_path / "rebound-workspace")
        _assert_error(
            "WORKSPACE_REBIND",
            lambda: runtime.status(workspace_id, "missing"),
        )
    finally:
        runtime.close()


def test_scoped_create_status_and_restore_prioritize_workspace_identity(
    tmp_path: Path,
) -> None:
    (
        database_path,
        cas_root,
        workspace_id,
        snapshot_id,
        workspace,
        original,
    ) = _prepared_database(tmp_path)
    second_workspace = tmp_path / "second-workspace"
    _git(
        "worktree",
        "add",
        "-b",
        "runtime-second-task",
        str(second_workspace),
        cwd=original,
    )
    (second_workspace / "second.py").write_text("SECOND = 1\n", encoding="utf-8")
    scratch_root = tmp_path.parent / f"{tmp_path.name}-snapshot-scratch"
    scratch_root.mkdir()
    runtime = open_project_checkpoint_rw(
        database_path,
        cas_root,
        scratch_root=scratch_root,
    )
    try:
        second_workspace_id = runtime.project_index.project_index_register(
            second_workspace
        )
        ownership = WorkspaceOwnership(
            workflow_id="workflow",
            task_id="task",
            owner="owner",
            lease_epoch=1,
            workspace_id=workspace_id,
            write_scope=("module.py",),
        )
        foreign_ownership = replace(ownership, write_scope=("../outside",))
        _assert_error(
            "WORKTREE_UNOWNED",
            lambda: runtime.create(
                second_workspace_id,
                foreign_ownership,
                "not-a-current-snapshot",
            ),
        )
        checkpoint = runtime.create(workspace_id, ownership, snapshot_id)
        _assert_error(
            "NOT_FOUND",
            lambda: runtime.status(second_workspace_id, checkpoint.checkpoint_id),
        )
        _assert_error(
            "WORKTREE_UNOWNED",
            lambda: runtime.restore(
                second_workspace_id,
                foreign_ownership,
                checkpoint.checkpoint_id,
                "not-a-current-snapshot",
            ),
        )
        _assert_error(
            "WORKTREE_UNOWNED",
            lambda: runtime.restore(
                workspace_id,
                replace(ownership, owner="other-owner"),
                checkpoint.checkpoint_id,
                "not-a-current-snapshot",
            ),
        )
        _assert_error(
            "ROLLBACK_DRIFT",
            lambda: runtime.restore(
                workspace_id,
                ownership,
                checkpoint.checkpoint_id,
                "not-a-current-snapshot",
            ),
        )
        (workspace / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        current_snapshot_id = runtime.project_index.sync(workspace_id).snapshot_id
        restored = runtime.restore(
            workspace_id,
            ownership,
            checkpoint.checkpoint_id,
            current_snapshot_id,
        )
        assert restored.checkpoint_id == checkpoint.checkpoint_id
        assert (workspace / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    finally:
        runtime.close()
