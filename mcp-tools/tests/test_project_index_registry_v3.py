"""Contract coverage for opaque project-index workspace registrations."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest


MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from project_index import IndexError, IndexState, ProjectIndexService  # noqa: E402
from project_index.checkpoints import (  # noqa: E402
    CheckpointService,
    WorktreeOwnership,
    WorkspaceOwnership,
)


def _service(tmp_path: Path) -> ProjectIndexService:
    return ProjectIndexService(tmp_path / "index.sqlite3")


def _workspace(tmp_path: Path, name: str = "workspace") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "module.py").write_text(
        "def value() -> int:\n    return 1\n", encoding="utf-8"
    )
    return root


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *arguments],
        check=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _linked_worktree(tmp_path: Path) -> Path:
    original = tmp_path / "origin"
    worktree = tmp_path / "owned-worktree"
    original.mkdir()
    _git("init", "-b", "main", cwd=original)
    (original / "scope").mkdir()
    (original / "scope" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("add", "scope/module.py", cwd=original)
    _git(
        "-c",
        "user.name=Workspace Registry Test",
        "-c",
        "user.email=registry@example.invalid",
        "commit",
        "-m",
        "seed",
        cwd=original,
    )
    _git("worktree", "add", "-b", "owned-task", str(worktree), cwd=original)
    return worktree


def test_registered_workspace_id_is_opaque_idempotent_and_drives_index_calls(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    service = _service(tmp_path)

    workspace_id = service.project_index_register(root)
    assert workspace_id == service.project_index_register(root)
    assert workspace_id.startswith("sha256:")
    assert len(workspace_id) == len("sha256:") + 64
    assert str(root.resolve()) not in workspace_id

    snapshot = service.sync(workspace_id)
    status = service.status(workspace_id, snapshot.snapshot_id)
    result = service.query(workspace_id, snapshot.snapshot_id, "value")

    assert snapshot.workspace == workspace_id
    assert snapshot.workspace_id == workspace_id
    assert snapshot.binding_state == "active"
    assert status.workspace == workspace_id
    assert status.state is IndexState.INDEX_READY
    assert result.snapshot_id == snapshot.snapshot_id
    assert str(root.resolve()) not in snapshot.snapshot_id
    assert str(root.resolve()) not in repr(snapshot)
    assert str(root.resolve()) not in repr(status)
    assert str(root.resolve()) not in str(asdict(snapshot))
    connection = sqlite3.connect(tmp_path / "index.sqlite3")
    stored_workspace, stored_workspace_id = connection.execute(
        "SELECT workspace, workspace_id FROM project_index_snapshots"
    ).fetchone()
    connection.close()
    assert stored_workspace == ""
    assert stored_workspace_id == ""
    service.close()


def test_v3_snapshot_id_is_bound_to_the_registered_workspace(
    tmp_path: Path,
) -> None:
    first_root = _workspace(tmp_path, "first-workspace")
    second_root = _workspace(tmp_path, "second-workspace")
    service = _service(tmp_path)
    first_workspace_id = service.project_index_register(first_root)
    second_workspace_id = service.project_index_register(second_root)

    first = service.sync(first_workspace_id)
    second = service.sync(second_workspace_id)

    assert first_workspace_id != second_workspace_id
    assert first.snapshot_id != second.snapshot_id
    assert first.workspace_id == first_workspace_id
    assert second.workspace_id == second_workspace_id
    service.close()


def test_only_explicit_registration_accepts_a_workspace_path(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)
    snapshot = service.sync(workspace_id)

    assert not hasattr(service, "register")
    assert not hasattr(service, "register_workspace")
    assert not hasattr(service, "revalidate")
    assert not hasattr(service._registry, "register")

    path_calls = (
        lambda: service.sync(root),
        lambda: service.status(root, snapshot.snapshot_id),
        lambda: service.query(root, snapshot.snapshot_id, "value"),
        lambda: service.assert_current(root, snapshot.snapshot_id),
        lambda: service.snapshot_facts(root, snapshot.snapshot_id),
        lambda: service.read_snapshot_files(
            root,
            snapshot.snapshot_id,
            ("module.py",),
            byte_budget=1024,
        ),
    )
    for call in path_calls:
        with pytest.raises(IndexError) as captured:
            call()
        assert captured.value.code == "WORKSPACE_UNREGISTERED"
        assert str(root.resolve()) not in str(captured.value)
    service.close()


def test_cross_workspace_snapshot_loads_and_diffs_fail_closed(
    tmp_path: Path,
) -> None:
    first_root = _workspace(tmp_path, "first-workspace")
    second_root = _workspace(tmp_path, "second-workspace")
    service = _service(tmp_path)
    first_workspace_id = service.project_index_register(first_root)
    second_workspace_id = service.project_index_register(second_root)
    first = service.sync(first_workspace_id)
    second = service.sync(second_workspace_id)

    with pytest.raises(IndexError) as query:
        service.query(second_workspace_id, first.snapshot_id, "value")
    assert query.value.code == "NOT_FOUND"

    with pytest.raises(IndexError) as comparison:
        service.diff(second_workspace_id, first.snapshot_id, second.snapshot_id)
    assert comparison.value.code == "NOT_FOUND"
    service.close()


def test_unknown_ids_and_root_rebinds_fail_closed_without_path_disclosure(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)

    with pytest.raises(IndexError) as unknown:
        service.sync("sha256:" + "0" * 64)
    assert unknown.value.code == "WORKSPACE_UNREGISTERED"
    assert str(root.resolve()) not in str(unknown.value)

    moved = tmp_path / "moved-workspace"
    root.rename(moved)
    root.mkdir()
    (root / "module.py").write_text(
        "def replacement() -> int:\n    return 2\n", encoding="utf-8"
    )

    with pytest.raises(IndexError) as rebound:
        service.sync(workspace_id)
    assert rebound.value.code == "WORKSPACE_REBIND"
    assert str(root.resolve()) not in str(rebound.value)
    with pytest.raises(IndexError) as duplicate:
        service.project_index_register(root)
    assert duplicate.value.code == "WORKSPACE_REBIND"
    service.close()


def test_registration_rejects_symlink_aliases_before_they_gain_an_id(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")

    service = _service(tmp_path)
    with pytest.raises(IndexError) as captured:
        service.project_index_register(alias)
    assert captured.value.code == "UNSAFE_WORKSPACE"
    assert str(alias) not in str(captured.value)
    service.close()


def test_repository_identity_rebind_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    _git("init", "-b", "main", cwd=root)
    service = _service(tmp_path)
    workspace_id = service.project_index_register(root)
    service.sync(workspace_id)

    shutil.rmtree(root / ".git")
    _git("init", "-b", "replacement", cwd=root)

    with pytest.raises(IndexError) as captured:
        service.sync(workspace_id)
    assert captured.value.code == "WORKSPACE_REBIND"
    connection = sqlite3.connect(tmp_path / "index.sqlite3")
    sync_count = connection.execute(
        "SELECT COUNT(*) FROM project_index_syncs"
    ).fetchone()
    connection.close()
    assert sync_count == (1,)
    service.close()


def test_historical_path_snapshots_need_explicit_revalidation_before_activation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(root)
    snapshot = service.sync(workspace_id)
    service.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE project_index_metadata SET value = '2' WHERE key = 'schema_version'"
    )
    connection.execute(
        "UPDATE project_index_snapshots SET workspace = ?, workspace_id = '', binding_state = 'active'",
        (str(root.resolve()),),
    )
    connection.execute(
        "UPDATE project_index_syncs SET workspace = ?",
        (str(root.resolve()),),
    )
    connection.execute("DELETE FROM project_index_snapshot_bindings")
    connection.commit()
    connection.close()

    migrated = ProjectIndexService(database)
    historical = migrated.snapshot_facts(workspace_id, snapshot.snapshot_id).snapshot
    assert historical.workspace == workspace_id
    assert historical.workspace_id == workspace_id
    assert historical.binding_state == "historical_unverified"
    assert str(root.resolve()) not in repr(historical)
    assert (
        migrated.status(workspace_id, snapshot.snapshot_id).state
        is IndexState.HISTORICAL_UNVERIFIED
    )

    with pytest.raises(IndexError) as blocked:
        migrated.query(workspace_id, snapshot.snapshot_id, "value")
    assert blocked.value.code == "HISTORICAL_UNVERIFIED"

    activated = migrated.revalidate_snapshot(workspace_id, snapshot.snapshot_id)
    assert activated.binding_state == "active"
    assert migrated.query(workspace_id, snapshot.snapshot_id, "value").nodes
    migrated.close()


def test_migration_quarantines_legacy_workspace_ids_without_a_binding(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(root)
    snapshot = service.sync(workspace_id)
    service.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE project_index_metadata SET value = '2' WHERE key = 'schema_version'"
    )
    connection.execute(
        "UPDATE project_index_snapshots "
        "SET workspace = ?, workspace_id = ?, binding_state = 'active'",
        (str(root.resolve()), workspace_id),
    )
    connection.execute("DELETE FROM project_index_snapshot_bindings")
    connection.commit()
    connection.close()

    migrated = ProjectIndexService(database)
    historical = migrated.snapshot_facts(workspace_id, snapshot.snapshot_id).snapshot

    assert historical.binding_state == "historical_unverified"
    assert (
        migrated.status(workspace_id, snapshot.snapshot_id).state
        is IndexState.HISTORICAL_UNVERIFIED
    )
    with pytest.raises(IndexError) as captured:
        migrated.query(workspace_id, snapshot.snapshot_id, "value")
    assert captured.value.code == "HISTORICAL_UNVERIFIED"
    migrated.close()


def test_migration_rekeys_existing_binding_sync_pointers(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(root)
    snapshot = service.sync(workspace_id)
    service.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE project_index_metadata SET value = '3' WHERE key = 'schema_version'"
    )
    connection.execute(
        "UPDATE project_index_syncs SET workspace = ?",
        (str(root.resolve()),),
    )
    connection.commit()
    connection.close()

    migrated = ProjectIndexService(database)

    status = migrated.status(workspace_id)
    assert status.snapshot_id == snapshot.snapshot_id
    assert status.state is IndexState.HISTORICAL_UNVERIFIED
    assert status.binding_state == "historical_unverified"
    migrated.close()


def test_historical_revalidation_rejects_a_same_path_identity_rebind(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    database = tmp_path / "index.sqlite3"
    service = ProjectIndexService(database)
    workspace_id = service.project_index_register(root)
    snapshot = service.sync(workspace_id)
    service.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE project_index_metadata SET value = '2' WHERE key = 'schema_version'"
    )
    connection.execute(
        "UPDATE project_index_snapshots SET workspace = ?, workspace_id = '', binding_state = 'active'",
        (str(root.resolve()),),
    )
    connection.execute(
        "UPDATE project_index_syncs SET workspace = ?",
        (str(root.resolve()),),
    )
    connection.execute("DELETE FROM project_index_snapshot_bindings")
    connection.execute("DELETE FROM project_index_workspaces")
    connection.commit()
    connection.close()

    migrated = ProjectIndexService(database)
    moved = tmp_path / "moved-workspace"
    root.rename(moved)
    root.mkdir()
    (root / "module.py").write_text(
        "def value() -> int:\n    return 1\n", encoding="utf-8"
    )
    rebound_id = migrated.project_index_register(root)
    assert rebound_id == workspace_id

    with pytest.raises(IndexError) as captured:
        migrated.revalidate_snapshot(rebound_id, snapshot.snapshot_id)
    assert captured.value.code == "WORKSPACE_REBIND"
    migrated.close()


def test_checkpoint_capture_uses_workspace_id_without_disclosing_the_root(
    tmp_path: Path,
) -> None:
    root = _linked_worktree(tmp_path)
    index = _service(tmp_path)
    workspace_id = index.project_index_register(root)
    snapshot = index.sync(workspace_id)
    checkpoints = CheckpointService(
        tmp_path / "checkpoints.sqlite3", tmp_path / "checkpoint-cas", index
    )
    ownership = WorkspaceOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="registry-test",
        lease_epoch=1,
        workspace_id=workspace_id,
        write_scope=("scope",),
    )

    checkpoint = checkpoints.create(ownership, snapshot.snapshot_id)

    assert checkpoint.workspace_id == workspace_id
    assert str(root.resolve()) not in repr(checkpoint)
    assert str(root.resolve()) not in str(asdict(checkpoint))
    connection = sqlite3.connect(tmp_path / "checkpoints.sqlite3")
    stored_root, stored_workspace_id = connection.execute(
        "SELECT workspace_root, workspace_id FROM checkpoint_records"
    ).fetchone()
    connection.close()
    assert stored_root == ""
    assert stored_workspace_id == workspace_id
    checkpoints.close()
    index.close()


def test_checkpoint_read_fails_closed_after_workspace_rebind(tmp_path: Path) -> None:
    root = _linked_worktree(tmp_path)
    index = _service(tmp_path)
    workspace_id = index.project_index_register(root)
    snapshot = index.sync(workspace_id)
    checkpoints = CheckpointService(
        tmp_path / "checkpoints.sqlite3", tmp_path / "checkpoint-cas", index
    )
    ownership = WorkspaceOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="registry-test",
        lease_epoch=1,
        workspace_id=workspace_id,
        write_scope=("scope",),
    )
    checkpoint = checkpoints.create(ownership, snapshot.snapshot_id)

    moved = tmp_path / "moved-worktree"
    root.rename(moved)
    root.mkdir()

    with pytest.raises(IndexError) as captured:
        checkpoints.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=ownership.workflow_id,
            task_id=ownership.task_id,
            paths=("scope/module.py",),
            byte_budget=1024,
        )
    assert captured.value.code == "WORKSPACE_REBIND"

    checkpoints.close()
    index.close()


def test_checkpoint_rejects_path_ownership_outside_registration(
    tmp_path: Path,
) -> None:
    root = _linked_worktree(tmp_path)
    index = _service(tmp_path)
    workspace_id = index.project_index_register(root)
    snapshot = index.sync(workspace_id)
    checkpoints = CheckpointService(
        tmp_path / "checkpoints.sqlite3", tmp_path / "checkpoint-cas", index
    )
    ownership = WorktreeOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="registry-test",
        lease_epoch=1,
        workspace_root=str(root),
        write_scope=("scope",),
    )

    with pytest.raises(IndexError) as captured:
        checkpoints.create(ownership, snapshot.snapshot_id)
    assert captured.value.code == "WORKSPACE_UNREGISTERED"
    assert str(root.resolve()) not in str(captured.value)
    checkpoints.close()
    index.close()
