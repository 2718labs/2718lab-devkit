from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest


MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from project_index import IndexError, ProjectIndexService  # noqa: E402
import project_index.checkpoints as checkpoints_module  # noqa: E402
from project_index.checkpoints import (  # noqa: E402
    CheckpointService,
    WorktreeOwnership,
)


@dataclass(frozen=True)
class _Snapshot:
    snapshot_id: str


class _FilesystemIndex:
    """Small deterministic index double used to isolate checkpoint behavior."""

    def _snapshot(self, workspace: str | Path) -> _Snapshot:
        root = Path(workspace).resolve(strict=True)
        entries: list[tuple[str, str]] = []
        for current_root, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name != ".git" and not (current / name).is_symlink()
            )
            for name in sorted(file_names):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if relative == ".git":
                    continue
                if path.is_symlink():
                    entries.append((relative, f"link:{os.readlink(path)}"))
                else:
                    entries.append(
                        (relative, hashlib.sha256(path.read_bytes()).hexdigest())
                    )
        payload = json.dumps(entries, separators=(",", ":")).encode("utf-8")
        return _Snapshot(f"sha256:{hashlib.sha256(payload).hexdigest()}")

    def sync(
        self, workspace: str | Path, include_paths: tuple[str, ...] | None = None
    ) -> _Snapshot:
        del include_paths
        return self._snapshot(workspace)

    def assert_current(
        self,
        workspace: str | Path,
        snapshot_id: str,
        required_paths: tuple[str, ...] | None = None,
    ) -> _Snapshot:
        del required_paths
        current = self._snapshot(workspace)
        if current.snapshot_id != snapshot_id:
            raise IndexError("INDEX_STALE", "request rejected")
        return current


@dataclass(frozen=True)
class _Repository:
    original: Path
    worktree: Path


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def test_git_worktree_paths_uses_devnull_for_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "worktree"
    git_dir = tmp_path / "git-dir"
    common_dir = tmp_path / "common-dir"
    for path in (root, git_dir, common_dir):
        path.mkdir()
    observed_kwargs: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="\n".join(str(path) for path in (root, git_dir, common_dir)),
            stderr="",
        )

    monkeypatch.setattr(checkpoints_module.subprocess, "run", fake_run)

    assert checkpoints_module._git_worktree_paths(root) == (
        root.resolve(),
        git_dir.resolve(),
        common_dir.resolve(),
    )
    assert observed_kwargs.get("stdin") is subprocess.DEVNULL


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def repository(tmp_path: Path) -> _Repository:
    original = tmp_path / "original"
    worktree = tmp_path / "owned-worktree"
    original.mkdir()
    _git("init", "-b", "main", cwd=original)
    (original / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=original)
    _git(
        "-c",
        "user.name=Checkpoint Test",
        "-c",
        "user.email=checkpoint@example.invalid",
        "commit",
        "-m",
        "seed",
        cwd=original,
    )
    _git("worktree", "add", "-b", "checkpoint-task", str(worktree), cwd=original)
    return _Repository(original=original, worktree=worktree)


@pytest.fixture
def index_service() -> _FilesystemIndex:
    return _FilesystemIndex()


@pytest.fixture
def service(tmp_path: Path, index_service: _FilesystemIndex) -> CheckpointService:
    instance = CheckpointService(
        tmp_path / "index.sqlite3", tmp_path / "checkpoint-cas", index_service
    )
    yield instance
    instance.close()


def _ownership(
    root: Path, write_scope: tuple[str, ...] = ("scope",)
) -> WorktreeOwnership:
    return WorktreeOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="sol-ultra-checkpoint",
        lease_epoch=7,
        workspace_root=str(root.resolve()),
        write_scope=write_scope,
    )


def _workspace_state(root: Path) -> dict[str, bytes]:
    state: dict[str, bytes] = {}
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name != ".git" and not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if relative != ".git" and not path.is_symlink():
                state[relative] = path.read_bytes()
    return state


def _assert_error(code: str, operation) -> None:
    with pytest.raises(IndexError) as captured:
        operation()
    assert captured.value.code == code


def test_create_is_content_addressed_idempotent_and_reopens(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "alpha.bin").write_bytes(b"alpha\x00\r\n")
    (scope / "empty").mkdir()
    snapshot = index_service.sync(repository.worktree)
    database = tmp_path / "index.sqlite3"
    cas_root = tmp_path / "checkpoint-cas"
    ownership = _ownership(repository.worktree)

    first_service = CheckpointService(database, cas_root, index_service)
    first = first_service.create(ownership, snapshot.snapshot_id)
    repeated = first_service.create(ownership, snapshot.snapshot_id)
    first_status = first_service.status(first.checkpoint_id)
    first_service.close()

    reopened = CheckpointService(database, cas_root, index_service)
    try:
        assert repeated == first
        assert first_status == first
        assert reopened.status(first.checkpoint_id) == first
        assert reopened.status(first.checkpoint_id) == reopened.status(
            first.checkpoint_id
        )
        assert first.snapshot_id == snapshot.snapshot_id
        assert first.entry_count == 3
        assert first.manifest_hash.startswith("sha256:")
        assert first.cas_root_hash.startswith("sha256:")
        blobs = [path for path in cas_root.rglob("*") if path.is_file()]
        assert len(blobs) == 1
        assert blobs[0].read_bytes() == b"alpha\x00\r\n"
    finally:
        reopened.close()


def test_create_accepts_directory_scope_with_real_project_index(
    repository: _Repository, tmp_path: Path
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "tracked.txt").write_bytes(b"tracked\n")
    database = tmp_path / "real-index.sqlite3"
    index = ProjectIndexService(database)
    service = CheckpointService(database, tmp_path / "real-cas", index)
    try:
        snapshot = index.sync(repository.worktree)

        checkpoint = service.create(
            _ownership(repository.worktree), snapshot.snapshot_id
        )

        assert checkpoint.snapshot_id == snapshot.snapshot_id
        assert checkpoint.write_scope == ("scope",)
        (scope / "tracked.txt").write_bytes(b"changed\n")
        current = index.sync(repository.worktree)

        restored = service.restore(
            _ownership(repository.worktree),
            checkpoint.checkpoint_id,
            current.snapshot_id,
        )

        assert restored.restored_snapshot_id == snapshot.snapshot_id
        assert (scope / "tracked.txt").read_bytes() == b"tracked\n"
    finally:
        service.close()
        index.close()


def test_restore_add_change_delete_is_exact_and_creates_rescue_checkpoint(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    changed = scope / "changed.bin"
    deleted = scope / "deleted.txt"
    changed.write_bytes(b"before\x00\xff\r\n")
    deleted.write_bytes(b"restore me\n")
    (scope / "empty-dir").mkdir()
    (repository.worktree / "outside.txt").write_bytes(b"outside\n")
    ownership = _ownership(repository.worktree)

    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    changed.write_bytes(b"after\n")
    deleted.unlink()
    (scope / "added.txt").write_bytes(b"remove me\n")
    (scope / "new-empty-dir").mkdir()
    current_snapshot = index_service.sync(repository.worktree)
    changed_state = _workspace_state(repository.worktree)

    restored = service.restore(
        ownership, target.checkpoint_id, current_snapshot.snapshot_id
    )

    assert restored.checkpoint_id == target.checkpoint_id
    assert restored.restored_snapshot_id == target_snapshot.snapshot_id
    assert restored.rescue_checkpoint_id != target.checkpoint_id
    rescue = service.status(restored.rescue_checkpoint_id)
    assert rescue.kind == "rescue"
    assert rescue.snapshot_id == current_snapshot.snapshot_id
    assert changed.read_bytes() == b"before\x00\xff\r\n"
    assert deleted.read_bytes() == b"restore me\n"
    assert not (scope / "added.txt").exists()
    assert (scope / "empty-dir").is_dir()
    assert not (scope / "new-empty-dir").exists()
    assert (repository.worktree / "outside.txt").read_bytes() == b"outside\n"

    round_trip = service.restore(
        ownership, rescue.checkpoint_id, target_snapshot.snapshot_id
    )
    assert round_trip.restored_snapshot_id == current_snapshot.snapshot_id
    assert _workspace_state(repository.worktree) == changed_state


def test_restore_rejects_current_tree_drift_without_workspace_writes(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    tracked = scope / "tracked.txt"
    tracked.write_bytes(b"checkpoint\n")
    ownership = _ownership(repository.worktree)
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"expected current\n")
    expected_current = index_service.sync(repository.worktree)
    tracked.write_bytes(b"unregistered drift\n")
    before = _workspace_state(repository.worktree)

    _assert_error(
        "ROLLBACK_DRIFT",
        lambda: service.restore(
            ownership, target.checkpoint_id, expected_current.snapshot_id
        ),
    )

    assert _workspace_state(repository.worktree) == before


def test_restore_preserves_acknowledged_out_of_scope_changes(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    tracked = scope / "tracked.txt"
    outside = repository.worktree / "outside.txt"
    tracked.write_bytes(b"checkpoint\n")
    outside.write_bytes(b"outside-before\n")
    ownership = _ownership(repository.worktree)
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"changed\n")
    outside.write_bytes(b"outside-after\n")
    expected_current = index_service.sync(repository.worktree)

    restored = service.restore(
        ownership, target.checkpoint_id, expected_current.snapshot_id
    )

    assert tracked.read_bytes() == b"checkpoint\n"
    assert outside.read_bytes() == b"outside-after\n"
    assert (
        restored.restored_snapshot_id
        == index_service.sync(repository.worktree).snapshot_id
    )
    assert restored.restored_snapshot_id != target_snapshot.snapshot_id


def test_restore_rejects_missing_parent_for_file_scope_without_writes(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    parent = repository.worktree / "parent"
    parent.mkdir()
    scoped_file = parent / "scoped.txt"
    scoped_file.write_bytes(b"checkpoint\n")
    ownership = _ownership(repository.worktree, ("parent/scoped.txt",))
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    scoped_file.unlink()
    parent.rmdir()
    expected_current = index_service.sync(repository.worktree)
    before = _workspace_state(repository.worktree)

    _assert_error(
        "ROLLBACK_DRIFT",
        lambda: service.restore(
            ownership, target.checkpoint_id, expected_current.snapshot_id
        ),
    )

    assert _workspace_state(repository.worktree) == before
    assert not parent.exists()


def test_restore_allows_nested_missing_file_scope_as_noop(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    ownership = _ownership(repository.worktree, ("missing/scoped.txt",))
    snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, snapshot.snapshot_id)

    restored = service.restore(ownership, target.checkpoint_id, snapshot.snapshot_id)

    assert restored.restored_snapshot_id == snapshot.snapshot_id
    assert not (repository.worktree / "missing").exists()


def test_create_rejects_original_or_noncanonical_workspaces(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    original_snapshot = index_service.sync(repository.original)
    _assert_error(
        "WORKTREE_UNOWNED",
        lambda: service.create(
            _ownership(repository.original), original_snapshot.snapshot_id
        ),
    )

    child = repository.worktree / "child"
    child.mkdir()
    child_snapshot = index_service.sync(child)
    _assert_error(
        "WORKTREE_UNOWNED",
        lambda: service.create(_ownership(child), child_snapshot.snapshot_id),
    )


@pytest.mark.parametrize(
    "scope",
    (
        ("../escape.txt",),
        ("C:/escape.txt",),
        (".git",),
        ("node_modules",),
        ("build/output.txt",),
        ("project-index.sqlite3",),
    ),
)
def test_create_rejects_scope_escape(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
    scope: tuple[str, ...],
) -> None:
    snapshot = index_service.sync(repository.worktree)
    _assert_error(
        "SCOPE_ESCAPE",
        lambda: service.create(
            _ownership(repository.worktree, scope), snapshot.snapshot_id
        ),
    )


def test_create_rejects_string_write_scope(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    snapshot = index_service.sync(repository.worktree)
    ownership = WorktreeOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="sol-ultra-checkpoint",
        lease_epoch=7,
        workspace_root=str(repository.worktree.resolve()),
        write_scope="scope",  # type: ignore[arg-type]
    )

    _assert_error(
        "SCOPE_ESCAPE", lambda: service.create(ownership, snapshot.snapshot_id)
    )


def test_create_rejects_symlink_or_reparse_entries(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
    tmp_path: Path,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    link = scope / "external-link"
    try:
        _make_directory_link(link, external)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot create a symlink or reparse point: {exc}")

    snapshot = index_service.sync(repository.worktree)
    _assert_error(
        "UNSAFE_PATH_TYPE",
        lambda: service.create(_ownership(repository.worktree), snapshot.snapshot_id),
    )


def test_restore_rejects_reparse_drift_before_touching_external_files(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
    tmp_path: Path,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    tracked = scope / "tracked.txt"
    tracked.write_bytes(b"checkpoint\n")
    ownership = _ownership(repository.worktree)
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"expected-current\n")
    expected_current = index_service.sync(repository.worktree)
    tracked.unlink()
    scope.rmdir()
    external = tmp_path / "external-restore"
    external.mkdir()
    external_file = external / "tracked.txt"
    external_file.write_bytes(b"external\n")
    try:
        _make_directory_link(scope, external)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot create a symlink or reparse point: {exc}")

    _assert_error(
        "UNSAFE_PATH_TYPE",
        lambda: service.restore(
            ownership, target.checkpoint_id, expected_current.snapshot_id
        ),
    )

    assert external_file.read_bytes() == b"external\n"


def test_nested_git_metadata_is_never_captured_or_restored(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    nested = scope / "vendor"
    metadata = nested / ".git"
    cache = nested / "__pycache__"
    metadata.mkdir(parents=True)
    cache.mkdir()
    tracked = nested / "tracked.txt"
    git_config = metadata / "config"
    cached = cache / "tracked.pyc"
    tracked.write_bytes(b"checkpoint\n")
    git_config.write_bytes(b"git-before\n")
    cached.write_bytes(b"cache-before\n")
    ownership = _ownership(repository.worktree)
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"changed\n")
    git_config.write_bytes(b"git-after\n")
    cached.write_bytes(b"cache-after\n")
    expected_current = index_service.sync(repository.worktree)
    service.restore(ownership, target.checkpoint_id, expected_current.snapshot_id)

    assert tracked.read_bytes() == b"checkpoint\n"
    assert git_config.read_bytes() == b"git-after\n"
    assert cached.read_bytes() == b"cache-after\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_restore_finalizes_read_only_directory_mode_after_children(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    tracked = scope / "tracked.txt"
    tracked.write_bytes(b"checkpoint\n")
    scope.chmod(0o555)
    ownership = _ownership(repository.worktree)
    target_snapshot = index_service.sync(repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    scope.chmod(0o755)
    tracked.write_bytes(b"changed\n")
    expected_current = index_service.sync(repository.worktree)
    service.restore(ownership, target.checkpoint_id, expected_current.snapshot_id)

    assert tracked.read_bytes() == b"checkpoint\n"
    assert stat.S_IMODE(scope.stat().st_mode) == 0o555


def test_create_rejects_cas_reparse_escape(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "tracked.txt").write_bytes(b"checkpoint\n")
    database = tmp_path / "cas-index.sqlite3"
    cas_root = tmp_path / "cas-root"
    service = CheckpointService(database, cas_root, index_service)
    external = tmp_path / "external-cas"
    external.mkdir()
    try:
        _make_directory_link(cas_root / "sha256", external)
    except (OSError, subprocess.CalledProcessError) as exc:
        service.close()
        pytest.skip(f"cannot create a symlink or reparse point: {exc}")
    snapshot = index_service.sync(repository.worktree)
    try:
        _assert_error(
            "UNSAFE_PATH_TYPE",
            lambda: service.create(
                _ownership(repository.worktree), snapshot.snapshot_id
            ),
        )
        assert list(external.iterdir()) == []
    finally:
        service.close()


@pytest.mark.parametrize(
    "statement",
    (
        "DELETE FROM checkpoint_entries",
        "UPDATE checkpoint_records SET manifest_hash = 'sha256:' || printf('%064d', 0)",
        "UPDATE checkpoint_records SET write_scope_hash = 'sha256:' || printf('%064d', 0)",
        "UPDATE checkpoint_records SET cas_root_hash = 'sha256:' || printf('%064d', 0)",
        "UPDATE checkpoint_records SET entry_count = entry_count + 1",
        "UPDATE checkpoint_entries SET path = path || '.tampered'",
        "UPDATE checkpoint_records SET owner = 'different-owner'",
    ),
)
def test_status_rejects_tampered_checkpoint_identity(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
    statement: str,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "tracked.txt").write_bytes(b"checkpoint\n")
    database = tmp_path / "tampered-index.sqlite3"
    cas_root = tmp_path / "tampered-cas"
    ownership = _ownership(repository.worktree)
    snapshot = index_service.sync(repository.worktree)
    service = CheckpointService(database, cas_root, index_service)
    checkpoint = service.create(ownership, snapshot.snapshot_id)
    service.close()
    with sqlite3.connect(database) as connection:
        connection.execute(statement)

    reopened = CheckpointService(database, cas_root, index_service)
    try:
        _assert_error(
            "INDEX_CORRUPT", lambda: reopened.status(checkpoint.checkpoint_id)
        )
    finally:
        reopened.close()


def test_idempotent_create_does_not_repair_tampered_checkpoint(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "tracked.txt").write_bytes(b"checkpoint\n")
    database = tmp_path / "tampered-create-index.sqlite3"
    cas_root = tmp_path / "tampered-create-cas"
    ownership = _ownership(repository.worktree)
    snapshot = index_service.sync(repository.worktree)
    service = CheckpointService(database, cas_root, index_service)
    service.create(ownership, snapshot.snapshot_id)
    service.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM checkpoint_entries")

    reopened = CheckpointService(database, cas_root, index_service)
    try:
        _assert_error(
            "INDEX_CORRUPT",
            lambda: reopened.create(ownership, snapshot.snapshot_id),
        )
    finally:
        reopened.close()


def test_status_unknown_checkpoint_is_not_found(service: CheckpointService) -> None:
    _assert_error("NOT_FOUND", lambda: service.status("sha256:" + "0" * 64))


def test_checkpoint_files_require_task_ownership(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    source = repository.worktree / "scope" / "module.py"
    source.parent.mkdir()
    source.write_bytes(b"VALUE = 1\n")
    checkpoint = service.create(
        _ownership(repository.worktree),
        index_service.sync(repository.worktree).snapshot_id,
    )
    files = service.read_files_for_task(
        checkpoint.checkpoint_id,
        workflow_id=checkpoint.workflow_id,
        task_id=checkpoint.task_id,
        paths=("scope/module.py",),
        byte_budget=4096,
    )
    assert files[0].body == b"VALUE = 1\n"
    with pytest.raises(IndexError) as captured:
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id="other-task",
            paths=("scope/module.py",),
            byte_budget=4096,
        )
    assert captured.value.code == "WORKTREE_UNOWNED"


def test_checkpoint_file_reader_rejects_tampered_cas_body(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    source = repository.worktree / "scope" / "module.py"
    source.parent.mkdir()
    source.write_bytes(b"VALUE = 1\n")
    checkpoint = service.create(
        _ownership(repository.worktree),
        index_service.sync(repository.worktree).snapshot_id,
    )
    entry = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/module.py"
    )
    assert entry.blob_hash is not None
    service._blob_path(entry.blob_hash).write_bytes(b"VALUE = 2\n")
    _assert_error(
        "INDEX_CORRUPT",
        lambda: service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/module.py",),
            byte_budget=4096,
        ),
    )
