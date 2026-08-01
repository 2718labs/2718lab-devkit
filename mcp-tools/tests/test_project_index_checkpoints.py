from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

import project_index.checkpoints as checkpoints_module  # noqa: E402
from devkit_runtime.workspace_authority import WorkspaceRootAuthority  # noqa: E402
from project_index import IndexError, ProjectIndexService  # noqa: E402
from project_index.checkpoints import (  # noqa: E402
    CheckpointService,
    WorkspaceOwnership,
)


@dataclass(frozen=True)
class _Snapshot:
    snapshot_id: str


class _FilesystemRegistry:
    """Registry-shaped authority input for the checkpoint service double."""

    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots = roots

    def resolve(self, workspace_id: str) -> Path:
        root = self._roots.get(workspace_id)
        if root is None:
            raise IndexError("WORKSPACE_UNREGISTERED", "request rejected")
        return root


class _FilesystemIndex:
    """Small deterministic index double used to isolate checkpoint behavior."""

    def __init__(self) -> None:
        self._workspace_ids: dict[Path, str] = {}
        self._roots: dict[str, Path] = {}
        self._snapshots: set[tuple[str, str]] = set()
        self.workspace_authority = WorkspaceRootAuthority(
            _FilesystemRegistry(self._roots)  # type: ignore[arg-type]
        )

    def project_index_register(self, workspace_root: str | Path) -> str:
        root = Path(workspace_root).resolve(strict=True)
        workspace_id = self._workspace_ids.get(root)
        if workspace_id is None:
            workspace_id = (
                "workspace:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()
            )
            self._workspace_ids[root] = workspace_id
            self._roots[workspace_id] = root
        return workspace_id

    def _snapshot(self, workspace_id: str) -> _Snapshot:
        root = self.workspace_authority.resolve(workspace_id).root
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
        payload = json.dumps((workspace_id, entries), separators=(",", ":")).encode(
            "utf-8"
        )
        snapshot = _Snapshot(f"sha256:{hashlib.sha256(payload).hexdigest()}")
        self._snapshots.add((workspace_id, snapshot.snapshot_id))
        return snapshot

    def sync(
        self, workspace_id: str, include_paths: tuple[str, ...] | None = None
    ) -> _Snapshot:
        del include_paths
        return self._snapshot(workspace_id)

    def assert_current(
        self,
        workspace_id: str,
        snapshot_id: str,
        required_paths: tuple[str, ...] | None = None,
    ) -> _Snapshot:
        del required_paths
        current = self._snapshot(workspace_id)
        if current.snapshot_id != snapshot_id:
            raise IndexError("INDEX_STALE", "request rejected")
        return current

    def snapshot_facts(self, workspace_id: str, snapshot_id: str) -> None:
        self.workspace_authority.resolve(workspace_id)
        if (workspace_id, snapshot_id) not in self._snapshots:
            raise IndexError("NOT_FOUND", "request rejected")


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
            encoding="mbcs",
            errors="replace",
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
        tmp_path / "index.sqlite3",
        tmp_path / "checkpoint-cas",
        index_service,
        workspace_authority=index_service.workspace_authority,
    )
    yield instance
    instance.close()


def _workspace_id(
    index_service: _FilesystemIndex | ProjectIndexService, root: Path
) -> str:
    return index_service.project_index_register(root)


def _snapshot(
    index_service: _FilesystemIndex | ProjectIndexService, root: Path
) -> _Snapshot:
    return index_service.sync(_workspace_id(index_service, root))


def _ownership(
    index_service: _FilesystemIndex | ProjectIndexService,
    root: Path,
    write_scope: tuple[str, ...] = ("scope",),
) -> WorkspaceOwnership:
    return WorkspaceOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="sol-ultra-checkpoint",
        lease_epoch=7,
        workspace_id=_workspace_id(index_service, root),
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
    snapshot = _snapshot(index_service, repository.worktree)
    database = tmp_path / "index.sqlite3"
    cas_root = tmp_path / "checkpoint-cas"
    ownership = _ownership(index_service, repository.worktree)

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
        snapshot = _snapshot(index, repository.worktree)

        checkpoint = service.create(
            _ownership(index, repository.worktree), snapshot.snapshot_id
        )

        assert checkpoint.snapshot_id == snapshot.snapshot_id
        assert checkpoint.workspace_id == _workspace_id(index, repository.worktree)
        assert checkpoint.workspace_root == ""
        assert checkpoint.write_scope == ("scope",)
        (scope / "tracked.txt").write_bytes(b"changed\n")
        current = _snapshot(index, repository.worktree)

        restored = service.restore(
            _ownership(index, repository.worktree),
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
    ownership = _ownership(index_service, repository.worktree)

    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    changed.write_bytes(b"after\n")
    deleted.unlink()
    (scope / "added.txt").write_bytes(b"remove me\n")
    (scope / "new-empty-dir").mkdir()
    current_snapshot = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree)
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"expected current\n")
    expected_current = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree)
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"changed\n")
    outside.write_bytes(b"outside-after\n")
    expected_current = _snapshot(index_service, repository.worktree)

    restored = service.restore(
        ownership, target.checkpoint_id, expected_current.snapshot_id
    )

    assert tracked.read_bytes() == b"checkpoint\n"
    assert outside.read_bytes() == b"outside-after\n"
    assert (
        restored.restored_snapshot_id
        == _snapshot(index_service, repository.worktree).snapshot_id
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
    ownership = _ownership(index_service, repository.worktree, ("parent/scoped.txt",))
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    scoped_file.unlink()
    parent.rmdir()
    expected_current = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree, ("missing/scoped.txt",))
    snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, snapshot.snapshot_id)

    restored = service.restore(ownership, target.checkpoint_id, snapshot.snapshot_id)

    assert restored.restored_snapshot_id == snapshot.snapshot_id
    assert not (repository.worktree / "missing").exists()


def test_create_rejects_original_or_noncanonical_workspaces(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    original_snapshot = _snapshot(index_service, repository.original)
    _assert_error(
        "WORKTREE_UNOWNED",
        lambda: service.create(
            _ownership(index_service, repository.original),
            original_snapshot.snapshot_id,
        ),
    )

    child = repository.worktree / "child"
    child.mkdir()
    child_snapshot = _snapshot(index_service, child)
    _assert_error(
        "WORKTREE_UNOWNED",
        lambda: service.create(
            _ownership(index_service, child), child_snapshot.snapshot_id
        ),
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
    snapshot = _snapshot(index_service, repository.worktree)
    _assert_error(
        "SCOPE_ESCAPE",
        lambda: service.create(
            _ownership(index_service, repository.worktree, scope), snapshot.snapshot_id
        ),
    )


def test_create_rejects_string_write_scope(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    snapshot = _snapshot(index_service, repository.worktree)
    ownership = WorkspaceOwnership(
        workflow_id="workflow-1",
        task_id="task-1",
        owner="sol-ultra-checkpoint",
        lease_epoch=7,
        workspace_id=_workspace_id(index_service, repository.worktree),
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

    snapshot = _snapshot(index_service, repository.worktree)
    _assert_error(
        "UNSAFE_PATH_TYPE",
        lambda: service.create(
            _ownership(index_service, repository.worktree), snapshot.snapshot_id
        ),
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
    ownership = _ownership(index_service, repository.worktree)
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"expected-current\n")
    expected_current = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree)
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    tracked.write_bytes(b"changed\n")
    git_config.write_bytes(b"git-after\n")
    cached.write_bytes(b"cache-after\n")
    expected_current = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree)
    target_snapshot = _snapshot(index_service, repository.worktree)
    target = service.create(ownership, target_snapshot.snapshot_id)

    scope.chmod(0o755)
    tracked.write_bytes(b"changed\n")
    expected_current = _snapshot(index_service, repository.worktree)
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
    snapshot = _snapshot(index_service, repository.worktree)
    try:
        _assert_error(
            "UNSAFE_PATH_TYPE",
            lambda: service.create(
                _ownership(index_service, repository.worktree), snapshot.snapshot_id
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
    ownership = _ownership(index_service, repository.worktree)
    snapshot = _snapshot(index_service, repository.worktree)
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
    ownership = _ownership(index_service, repository.worktree)
    snapshot = _snapshot(index_service, repository.worktree)
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
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
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
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
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


def test_checkpoint_file_reader_verifies_all_cas_payloads(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "requested.py").write_bytes(b"requested\n")
    (scope / "other.py").write_bytes(b"other\n")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    other = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/other.py"
    )
    assert other.blob_hash is not None
    service._blob_path(other.blob_hash).write_bytes(b"tampered\n")
    _assert_error(
        "INDEX_CORRUPT",
        lambda: service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/requested.py",),
            byte_budget=4096,
        ),
    )


@pytest.mark.parametrize("byte_budget", (0, -1, True, 1.5, 1))
def test_checkpoint_file_reader_rejects_invalid_or_small_budget(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
    byte_budget: int | float,
) -> None:
    source = repository.worktree / "scope" / "module.py"
    source.parent.mkdir()
    source.write_bytes(b"xx")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    with pytest.raises(IndexError):
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/module.py",),
            byte_budget=byte_budget,
        )


def test_checkpoint_file_reader_scope_order_ownership_and_missing_cas(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(b"aa")
    (scope / "b.py").write_bytes(b"bb")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    files = service.read_files_for_task(
        checkpoint.checkpoint_id,
        workflow_id=checkpoint.workflow_id,
        task_id=checkpoint.task_id,
        paths=("scope/a.py", "scope/b.py"),
        byte_budget=4,
    )
    assert tuple(file.path for file in files) == ("scope/a.py", "scope/b.py")
    for workflow_id, task_id, paths in (
        ("other", checkpoint.task_id, ("scope/a.py",)),
        (checkpoint.workflow_id, "other", ("scope/a.py",)),
        (checkpoint.workflow_id, checkpoint.task_id, ("other.py",)),
        (checkpoint.workflow_id, checkpoint.task_id, ("scope/b.py", "scope/a.py")),
    ):
        with pytest.raises(IndexError):
            service.read_files_for_task(
                checkpoint.checkpoint_id,
                workflow_id=workflow_id,
                task_id=task_id,
                paths=paths,
                byte_budget=4,
            )
    entry = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/a.py"
    )
    assert entry.blob_hash is not None
    service._blob_path(entry.blob_hash).unlink()
    _assert_error(
        "INDEX_CORRUPT",
        lambda: service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/a.py",),
            byte_budget=4,
        ),
    )


def test_checkpoint_file_reader_rejects_aggregate_directory_and_missing_entries(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(b"aa")
    (scope / "b.py").write_bytes(b"bb")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    for paths, budget in (
        (("scope/a.py", "scope/b.py"), 3),
        (("scope",), 8),
        (("scope/missing.py",), 8),
    ):
        with pytest.raises(IndexError):
            service.read_files_for_task(
                checkpoint.checkpoint_id,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                paths=paths,
                byte_budget=budget,
            )


def test_checkpoint_file_reader_is_independent_of_expired_lease(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"lease-independent")
    ownership = _ownership(index_service, repository.worktree)
    checkpoint = service.create(
        ownership, _snapshot(index_service, repository.worktree).snapshot_id
    )
    monkeypatch.setattr(
        service,
        "_require_checkpoint_owner",
        lambda *args: pytest.fail("reader must not validate lease ownership"),
    )
    assert (
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=ownership.workflow_id,
            task_id=ownership.task_id,
            paths=("scope/module.py",),
            byte_budget=1024,
        )[0].body
        == b"lease-independent"
    )


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE checkpoint_records SET manifest_hash = 'sha256:' || printf('%064d', 0)",
        "UPDATE checkpoint_records SET cas_root_hash = 'sha256:' || printf('%064d', 0)",
    ),
)
def test_checkpoint_file_reader_rejects_tampered_record_metadata(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
    statement: str,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"record-marker")
    database = tmp_path / "reader-record.sqlite3"
    cas_root = tmp_path / "reader-record-cas"
    service = CheckpointService(database, cas_root, index_service)
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    service.close()
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    reopened = CheckpointService(database, cas_root, index_service)
    try:
        _assert_error(
            "INDEX_CORRUPT",
            lambda: reopened.read_files_for_task(
                checkpoint.checkpoint_id,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                paths=("scope/module.py",),
                byte_budget=1024,
            ),
        )
    finally:
        reopened.close()


def test_checkpoint_file_reader_does_not_mutate_storage_or_store_marker(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    marker = b"ATLAS05_CHECKPOINT_READER_MARKER_b9c0"
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(marker)
    (scope / "b.py").write_bytes(b"late")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    workspace_before = _workspace_state(repository.worktree)
    database_before = service.database_path.read_bytes()
    cas_before = tuple(
        sorted(
            (path.relative_to(service.cas_root).as_posix(), path.read_bytes())
            for path in service.cas_root.rglob("*")
            if path.is_file()
        )
    )
    files = service.read_files_for_task(
        checkpoint.checkpoint_id,
        workflow_id=checkpoint.workflow_id,
        task_id=checkpoint.task_id,
        paths=("scope/a.py", "scope/b.py"),
        byte_budget=1024,
    )
    assert files[0].body == marker
    with pytest.raises(IndexError):
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/a.py", "scope/b.py"),
            byte_budget=len(marker),
        )
    assert marker not in service.database_path.read_bytes()
    assert service.database_path.read_bytes() == database_before
    assert (
        tuple(
            sorted(
                (path.relative_to(service.cas_root).as_posix(), path.read_bytes())
                for path in service.cas_root.rglob("*")
                if path.is_file()
            )
        )
        == cas_before
    )
    assert _workspace_state(repository.worktree) == workspace_before


def test_checkpoint_file_reader_rejects_cas_reparse_parent(
    repository: _Repository,
    tmp_path: Path,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"safe")
    database = tmp_path / "reader-cas-link.sqlite3"
    cas_root = tmp_path / "reader-cas-link"
    service = CheckpointService(database, cas_root, index_service)
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    sha_root = cas_root / "sha256"
    external = tmp_path / "external"
    external.mkdir()
    backup = cas_root / "sha256-backup"
    sha_root.rename(backup)
    try:
        _make_directory_link(sha_root, external)
    except (OSError, subprocess.CalledProcessError) as exc:
        backup.rename(sha_root)
        service.close()
        pytest.skip(f"CAS reparse capability unavailable: {exc}")
    try:
        with pytest.raises(IndexError) as captured:
            service.read_files_for_task(
                checkpoint.checkpoint_id,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                paths=("scope/module.py",),
                byte_budget=1024,
            )
        assert captured.value.code in {"UNSAFE_PATH_TYPE", "INDEX_CORRUPT"}
        assert list(external.iterdir()) == []
    finally:
        service.close()
        if os.name == "nt":
            sha_root.rmdir()
        else:
            sha_root.unlink()
        backup.rename(sha_root)


class _CASReadTracker:
    """Delegate to a real file object while recording every requested read size."""

    def __init__(
        self, stream: object, path: Path, reads: dict[Path, list[int]]
    ) -> None:
        self._stream = stream
        self._path = path
        self._reads = reads
        self.closed = False

    def __enter__(self) -> _CASReadTracker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        self._stream.close()  # type: ignore[attr-defined]

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined]

    def read(self, size: int = -1) -> bytes:
        self._reads.setdefault(self._path, []).append(size)
        return self._stream.read(size)  # type: ignore[attr-defined]


def test_checkpoint_file_reader_streams_every_cas_blob_and_retains_requested_only(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    requested = scope / "requested.py"
    requested.write_bytes(b"requested\n")
    unrequested = scope / "unrequested.bin"
    unrequested.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entries = service._load_entries(checkpoint.checkpoint_id)
    requested_entry = next(
        entry for entry in entries if entry.path == "scope/requested.py"
    )
    assert requested_entry.blob_hash is not None
    expected_paths = {
        service._blob_path(str(entry.blob_hash))
        for entry in entries
        if entry.blob_hash is not None
    }
    original_open = checkpoints_module.os.open
    original_fdopen = checkpoints_module.os.fdopen
    original_stream = checkpoints_module._stream_cas_blob
    descriptors: dict[int, Path] = {}
    read_sizes: dict[Path, list[int]] = {}
    observed: list[tuple[Path, bool, bytes | None]] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        descriptors[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _CASReadTracker:
        return _CASReadTracker(
            original_fdopen(descriptor, mode, *args, **kwargs),
            descriptors[descriptor],
            read_sizes,
        )

    def recorded_stream(
        cas_root: Path,
        path: Path,
        before: os.stat_result,
        expected_hash: str,
        expected_size: int,
        retain: bool,
    ) -> bytes | None:
        body = original_stream(
            cas_root, path, before, expected_hash, expected_size, retain
        )
        observed.append((path, retain, body))
        return body

    monkeypatch.setattr(checkpoints_module.os, "open", tracked_open)
    monkeypatch.setattr(checkpoints_module.os, "fdopen", tracked_fdopen)
    monkeypatch.setattr(checkpoints_module, "_stream_cas_blob", recorded_stream)
    files = service.read_files_for_task(
        checkpoint.checkpoint_id,
        workflow_id=checkpoint.workflow_id,
        task_id=checkpoint.task_id,
        paths=("scope/requested.py",),
        byte_budget=1024,
    )
    assert files[0].body == b"requested\n"
    assert {path for path, _, _ in observed} == expected_paths
    assert {path for path, retain, _ in observed if retain} == {
        service._blob_path(requested_entry.blob_hash)
    }
    assert all(body is None for _, retain, body in observed if not retain)
    assert set(read_sizes) == expected_paths
    assert all(
        all(0 < size <= checkpoints_module._READ_CHUNK_SIZE for size in sizes)
        for sizes in read_sizes.values()
    )


def test_checkpoint_file_reader_checks_every_blob_when_requested_total_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(b"aaa")
    (scope / "b.py").write_bytes(b"bbb")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entries = service._load_entries(checkpoint.checkpoint_id)
    expected_paths = {
        service._blob_path(str(entry.blob_hash))
        for entry in entries
        if entry.blob_hash is not None
    }
    original_open = checkpoints_module.os.open
    original_fdopen = checkpoints_module.os.fdopen
    original_stream = checkpoints_module._stream_cas_blob
    descriptors: dict[int, Path] = {}
    read_sizes: dict[Path, list[int]] = {}
    observed: list[tuple[Path, bool, bytes | None]] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        descriptors[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _CASReadTracker:
        return _CASReadTracker(
            original_fdopen(descriptor, mode, *args, **kwargs),
            descriptors[descriptor],
            read_sizes,
        )

    def recorded_stream(
        cas_root: Path,
        path: Path,
        before: os.stat_result,
        expected_hash: str,
        expected_size: int,
        retain: bool,
    ) -> bytes | None:
        body = original_stream(
            cas_root, path, before, expected_hash, expected_size, retain
        )
        observed.append((path, retain, body))
        return body

    monkeypatch.setattr(checkpoints_module.os, "open", tracked_open)
    monkeypatch.setattr(checkpoints_module.os, "fdopen", tracked_fdopen)
    monkeypatch.setattr(checkpoints_module, "_stream_cas_blob", recorded_stream)
    with pytest.raises(IndexError) as captured:
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/a.py", "scope/b.py"),
            byte_budget=5,
        )
    assert captured.value.code == "SCOPE_ESCAPE"
    assert {path for path, _, _ in observed} == expected_paths
    assert all(not retain and body is None for _, retain, body in observed)
    assert set(read_sizes) == expected_paths
    assert all(
        all(0 < size <= checkpoints_module._READ_CHUNK_SIZE for size in sizes)
        for sizes in read_sizes.values()
    )


def test_checkpoint_file_reader_rejects_duplicate_paths_before_loading_cas(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"VALUE = 1\n")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )

    def fail_blob_load(*args: object, **kwargs: object) -> dict[str, bytes]:
        pytest.fail("duplicate requested paths must fail before loading CAS blobs")

    monkeypatch.setattr(service, "_load_and_verify_blobs", fail_blob_load)
    with pytest.raises(IndexError) as captured:
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/module.py", "scope/module.py"),
            byte_budget=1024,
        )
    assert captured.value.code == "SCOPE_ESCAPE"


def test_checkpoint_file_reader_reports_unrequested_corruption_before_oversize(
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(b"aaa")
    (scope / "b.py").write_bytes(b"bbb")
    (scope / "unrequested.py").write_bytes(b"trusted")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    unrequested = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/unrequested.py"
    )
    assert unrequested.blob_hash is not None
    service._blob_path(unrequested.blob_hash).write_bytes(b"tampered")
    with pytest.raises(IndexError) as captured:
        service.read_files_for_task(
            checkpoint.checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=checkpoint.task_id,
            paths=("scope/a.py", "scope/b.py"),
            byte_budget=5,
        )
    assert captured.value.code == "INDEX_CORRUPT"


def test_checkpoint_cas_loader_deduplicates_matching_hashes_and_rejects_size_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "a.py").write_bytes(b"same")
    (scope / "b.py").write_bytes(b"same")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entries = service._load_entries(checkpoint.checkpoint_id)
    shared = next(entry for entry in entries if entry.path == "scope/a.py")
    assert shared.blob_hash is not None
    original_stream = checkpoints_module._stream_cas_blob
    scanned_hashes: list[str] = []

    def recorded_stream(
        cas_root: Path,
        path: Path,
        before: os.stat_result,
        expected_hash: str,
        expected_size: int,
        retain: bool,
    ) -> bytes | None:
        scanned_hashes.append(expected_hash)
        return original_stream(
            cas_root, path, before, expected_hash, expected_size, retain
        )

    monkeypatch.setattr(checkpoints_module, "_stream_cas_blob", recorded_stream)
    blobs = service._load_and_verify_blobs(entries, frozenset({shared.blob_hash}))
    assert blobs == {shared.blob_hash: b"same"}
    assert scanned_hashes == [shared.blob_hash]
    conflicting = tuple(
        replace(entry, size=entry.size + 1) if entry.path == "scope/b.py" else entry
        for entry in entries
    )
    with pytest.raises(IndexError) as captured:
        service._load_and_verify_blobs(conflicting, frozenset())
    assert captured.value.code == "INDEX_CORRUPT"


def test_checkpoint_cas_stream_bounds_post_fstat_growth_probe(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"x")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entry = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/module.py"
    )
    assert entry.blob_hash is not None
    blob_path = service._blob_path(entry.blob_hash)
    before = blob_path.lstat()
    original_open = checkpoints_module.os.open
    original_fdopen = checkpoints_module.os.fdopen
    descriptors: dict[int, Path] = {}
    read_sizes: list[int] = []
    read_bytes = 0
    grew = False

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        descriptors[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _CASReadTracker:
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        path = descriptors.get(descriptor)

        class GrowingCASReader(_CASReadTracker):
            def read(self, size: int = -1) -> bytes:
                nonlocal grew, read_bytes
                if path == blob_path:
                    read_sizes.append(size)
                    if not grew:
                        grew = True
                        with blob_path.open("ab") as growth:
                            growth.write(b"y" * (8 * 1024 * 1024))
                body = self._stream.read(size)  # type: ignore[attr-defined]
                if path == blob_path:
                    read_bytes += len(body)
                return body

        return GrowingCASReader(stream, path or blob_path, {})

    monkeypatch.setattr(checkpoints_module.os, "open", tracked_open)
    monkeypatch.setattr(checkpoints_module.os, "fdopen", tracked_fdopen)
    with pytest.raises(IndexError) as captured:
        checkpoints_module._stream_cas_blob(
            service.cas_root,
            blob_path,
            before,
            entry.blob_hash,
            entry.size,
            False,
        )
    assert captured.value.code == "INDEX_CORRUPT"
    assert read_sizes
    assert all(0 < size <= checkpoints_module._READ_CHUNK_SIZE for size in read_sizes)
    assert read_bytes <= before.st_size + 1


def test_checkpoint_cas_stream_closes_on_success_and_fdopen_failure(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"safe")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entry = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/module.py"
    )
    assert entry.blob_hash is not None
    blob_path = service._blob_path(entry.blob_hash)
    before = blob_path.lstat()
    original_fdopen = checkpoints_module.os.fdopen
    readers: list[_CASReadTracker] = []

    def tracked_fdopen(
        descriptor: int, mode: str, *args: object, **kwargs: object
    ) -> _CASReadTracker:
        reader = _CASReadTracker(
            original_fdopen(descriptor, mode, *args, **kwargs), blob_path, {}
        )
        readers.append(reader)
        return reader

    with monkeypatch.context() as scoped:
        scoped.setattr(checkpoints_module.os, "fdopen", tracked_fdopen)
        assert (
            checkpoints_module._stream_cas_blob(
                service.cas_root,
                blob_path,
                before,
                entry.blob_hash,
                entry.size,
                True,
            )
            == b"safe"
        )
    assert readers and all(reader.closed for reader in readers)

    original_open = checkpoints_module.os.open
    original_close = checkpoints_module.os.close
    opened_descriptors: list[int] = []
    closed_descriptors: list[int] = []

    def tracked_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        descriptor = original_open(path, flags, *args)
        opened_descriptors.append(descriptor)
        return descriptor

    def failing_fdopen(*args: object, **kwargs: object) -> _CASReadTracker:
        raise OSError("injected fdopen failure")

    def tracked_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(checkpoints_module.os, "open", tracked_open)
        scoped.setattr(checkpoints_module.os, "fdopen", failing_fdopen)
        scoped.setattr(checkpoints_module.os, "close", tracked_close)
        with pytest.raises(IndexError) as captured:
            checkpoints_module._stream_cas_blob(
                service.cas_root,
                blob_path,
                before,
                entry.blob_hash,
                entry.size,
                False,
            )
    assert captured.value.code == "INDEX_CORRUPT"
    assert opened_descriptors == closed_descriptors


def test_checkpoint_cas_stream_rejects_same_size_identity_swap(
    monkeypatch: pytest.MonkeyPatch,
    repository: _Repository,
    service: CheckpointService,
    index_service: _FilesystemIndex,
) -> None:
    scope = repository.worktree / "scope"
    scope.mkdir()
    (scope / "module.py").write_bytes(b"safe")
    checkpoint = service.create(
        _ownership(index_service, repository.worktree),
        _snapshot(index_service, repository.worktree).snapshot_id,
    )
    entry = next(
        entry
        for entry in service._load_entries(checkpoint.checkpoint_id)
        if entry.path == "scope/module.py"
    )
    assert entry.blob_hash is not None
    blob_path = service._blob_path(entry.blob_hash)
    before = blob_path.lstat()
    replacement = blob_path.with_name("replacement")
    replacement.write_bytes(b"evil")
    original_open = checkpoints_module.os.open
    replaced = False

    def replace_before_open(
        path: str | os.PathLike[str], flags: int, *args: int
    ) -> int:
        nonlocal replaced
        if Path(path) == blob_path and not replaced:
            replaced = True
            os.replace(replacement, blob_path)
        return original_open(path, flags, *args)

    monkeypatch.setattr(checkpoints_module.os, "open", replace_before_open)
    with pytest.raises(IndexError) as captured:
        checkpoints_module._stream_cas_blob(
            service.cas_root,
            blob_path,
            before,
            entry.blob_hash,
            entry.size,
            False,
        )
    assert captured.value.code == "INDEX_CORRUPT"
