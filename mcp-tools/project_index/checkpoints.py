"""Task-owned content-addressed checkpoints and drift-safe restore."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from devkit_runtime.workspace_authority import WorkspaceRootAuthority

from .models import IndexError

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_READ_CHUNK_SIZE = 64 * 1024
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_INDEX_DATABASE_NAMES = frozenset(
    {
        ".project-index.db",
        ".project-index.sqlite",
        ".project-index.sqlite3",
        "project-index.db",
        "project-index.sqlite",
        "project-index.sqlite3",
        "project_index.db",
        "project_index.sqlite",
        "project_index.sqlite3",
        "orchestrator.sqlite3",
    }
)


@dataclass(frozen=True)
class WorktreeOwnership:
    workflow_id: str
    task_id: str
    owner: str
    lease_epoch: int
    workspace_root: str
    write_scope: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceOwnership:
    """Task ownership bound to a registered opaque workspace identifier."""

    workflow_id: str
    task_id: str
    owner: str
    lease_epoch: int
    workspace_id: str
    write_scope: tuple[str, ...]


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    workflow_id: str
    task_id: str
    owner: str
    lease_epoch: int
    workspace_root: str
    snapshot_id: str
    write_scope: tuple[str, ...]
    write_scope_hash: str
    manifest_hash: str
    cas_root_hash: str
    entry_count: int
    kind: str
    parent_checkpoint_id: str | None = None
    workspace_id: str = ""


@dataclass(frozen=True)
class RestoreResult:
    checkpoint_id: str
    rescue_checkpoint_id: str
    restored_snapshot_id: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class CheckpointFile:
    path: str
    content_hash: str
    body: bytes


@dataclass(frozen=True)
class _ManifestEntry:
    path: str
    kind: str
    blob_hash: str | None
    size: int
    mode: int


class _SnapshotReference(Protocol):
    """The snapshot identifier returned by Project Index synchronization."""

    snapshot_id: str


class _CheckpointIndexService(Protocol):
    """Subset of Project Index used by checkpoint integrity operations."""

    def assert_current(self, workspace_id: str, snapshot_id: str) -> object: ...

    def snapshot_facts(self, workspace_id: str, snapshot_id: str) -> object: ...

    def sync(self, workspace_id: str) -> _SnapshotReference: ...


class CheckpointService:
    """Persist immutable scoped manifests and restore them after a CAS check."""

    def __init__(
        self,
        database_path: str | Path,
        cas_root: str | Path,
        index_service: object,
        *,
        workspace_authority: WorkspaceRootAuthority | None = None,
    ) -> None:
        self.database_path = _prepare_storage_file(database_path)
        self.cas_root = _prepare_storage_directory(cas_root)
        self.index_service: _CheckpointIndexService = cast(
            _CheckpointIndexService, index_service
        )
        self.workspace_authority = self._resolve_workspace_authority(
            index_service, workspace_authority
        )
        self._owns_connection = True
        self._connection = sqlite3.connect(
            str(self.database_path), isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    @classmethod
    def from_prepared_connection(
        cls,
        database_path: str | Path,
        cas_root: str | Path,
        index_service: object,
        workspace_authority: WorkspaceRootAuthority,
        connection: sqlite3.Connection,
    ) -> CheckpointService:
        """Bind one validated runtime connection without preparing storage."""

        service = cls.__new__(cls)
        service.database_path = Path(database_path)
        service.cas_root = Path(cas_root)
        service.index_service = cast(_CheckpointIndexService, index_service)
        service.workspace_authority = service._resolve_workspace_authority(
            index_service, workspace_authority
        )
        service._owns_connection = False
        service._connection = connection
        connection.row_factory = sqlite3.Row
        return service

    @classmethod
    def validate_prepared_connection(cls, connection: sqlite3.Connection) -> None:
        """Reject a database that lacks the checkpoint schema before runtime use."""

        connection.row_factory = sqlite3.Row
        required_tables = {"checkpoint_records", "checkpoint_entries"}
        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(checkpoint_records)")
            }
        except sqlite3.DatabaseError as exc:
            raise _error("INDEX_CORRUPT") from exc
        if not required_tables.issubset(tables) or "workspace_id" not in columns:
            raise _error("INDEX_UNAVAILABLE")

    def close(self) -> None:
        if self._owns_connection and self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def create(self, ownership: WorkspaceOwnership, snapshot_id: str) -> Checkpoint:
        return self._create(
            ownership, snapshot_id, kind="checkpoint", parent_checkpoint_id=None
        )

    def create_for_workspace(
        self,
        workspace_id: str,
        ownership: WorkspaceOwnership,
        snapshot_id: str,
    ) -> Checkpoint:
        """Create only after workspace identity precedes scope and snapshot checks."""

        self._validate_ownership_shape(ownership)
        self._resolve_requested_workspace(workspace_id)
        if ownership.workspace_id != workspace_id:
            raise _error("WORKTREE_UNOWNED")
        try:
            self.index_service.assert_current(workspace_id, snapshot_id)
        except IndexError as exc:
            if exc.code in {"WORKSPACE_UNREGISTERED", "WORKSPACE_REBIND"}:
                raise
            raise _error("INDEX_STALE") from None
        return self.create(ownership, snapshot_id)

    def status(self, checkpoint_id: str) -> Checkpoint:
        checkpoint, _ = self._load_verified_checkpoint(checkpoint_id)
        return checkpoint

    def status_for_workspace(self, workspace_id: str, checkpoint_id: str) -> Checkpoint:
        """Load a checkpoint only through its requested workspace boundary."""

        self._resolve_requested_workspace(workspace_id)
        checkpoint, _ = self._load_verified_checkpoint_for_workspace(
            workspace_id, checkpoint_id
        )
        return checkpoint

    def restore_for_workspace(
        self,
        workspace_id: str,
        ownership: WorkspaceOwnership,
        checkpoint_id: str,
        expected_current_snapshot_id: str,
    ) -> RestoreResult:
        """Restore through the scoped precedence required by the runtime path."""

        self._validate_ownership_shape(ownership)
        self._resolve_requested_workspace(workspace_id)
        if ownership.workspace_id != workspace_id:
            raise _error("WORKTREE_UNOWNED")
        target, target_entries = self._load_verified_checkpoint_for_workspace(
            workspace_id, checkpoint_id
        )
        self._require_checkpoint_lease(target, ownership)
        self._assert_scoped_expected_current(workspace_id, expected_current_snapshot_id)
        root, scope, resolved_workspace_id = self._validate_ownership(ownership)
        self._require_checkpoint_owner(target, ownership, scope, resolved_workspace_id)
        self._validate_stored_entries(root, scope, target_entries)
        target_blobs = self._load_and_verify_blobs(target_entries)
        self._preflight_restore_paths(root, target_entries)

        try:
            rescue = self._create(
                ownership,
                expected_current_snapshot_id,
                kind="rescue",
                parent_checkpoint_id=target.checkpoint_id,
            )
            self._assert_scoped_expected_current(
                workspace_id, expected_current_snapshot_id
            )
        except IndexError as exc:
            if exc.code in {"INDEX_STALE", "NOT_FOUND"}:
                raise _error("ROLLBACK_DRIFT") from None
            raise

        rescue, current_entries = self._load_verified_checkpoint_for_workspace(
            workspace_id, rescue.checkpoint_id
        )
        latest_entries, _ = self._capture_manifest(root, scope)
        if latest_entries != current_entries:
            raise _error("ROLLBACK_DRIFT")
        changed_paths = _changed_paths(current_entries, target_entries)
        self._apply_manifest(root, scope, current_entries, target_entries, target_blobs)

        restored_entries, _ = self._capture_manifest(root, scope)
        if restored_entries != target_entries:
            raise _error("INDEX_STALE")
        restored = self.index_service.sync(workspace_id)
        restored_snapshot_id = str(restored.snapshot_id)
        self.index_service.assert_current(workspace_id, restored_snapshot_id)
        return RestoreResult(
            checkpoint_id=target.checkpoint_id,
            rescue_checkpoint_id=rescue.checkpoint_id,
            restored_snapshot_id=restored_snapshot_id,
            changed_paths=changed_paths,
        )

    @staticmethod
    def _resolve_workspace_authority(
        index_service: object,
        workspace_authority: WorkspaceRootAuthority | None,
    ) -> WorkspaceRootAuthority:
        from devkit_runtime.workspace_authority import WorkspaceRootAuthority

        if workspace_authority is not None:
            if not isinstance(workspace_authority, WorkspaceRootAuthority):
                raise TypeError("workspace_authority must be a WorkspaceRootAuthority")
            return workspace_authority
        from .service import ProjectIndexService

        if isinstance(index_service, ProjectIndexService):
            return index_service.workspace_authority
        candidate = getattr(index_service, "workspace_authority", None)
        if isinstance(candidate, WorkspaceRootAuthority):
            return candidate
        raise TypeError("CheckpointService requires a WorkspaceRootAuthority")

    def read_files_for_task(
        self,
        checkpoint_id: str,
        *,
        workflow_id: str,
        task_id: str,
        paths: Sequence[str],
        byte_budget: int,
    ) -> tuple[CheckpointFile, ...]:
        if type(byte_budget) is not int or byte_budget <= 0:
            raise _error("SCOPE_ESCAPE")
        if not isinstance(workflow_id, str) or not isinstance(task_id, str):
            raise _error("WORKTREE_UNOWNED")
        requested = _normalize_requested_paths(paths)
        checkpoint, entries = self._load_verified_checkpoint(checkpoint_id)
        if checkpoint.workflow_id != workflow_id or checkpoint.task_id != task_id:
            raise _error("WORKTREE_UNOWNED")
        by_path = {entry.path: entry for entry in entries}
        requested_entries: list[_ManifestEntry] = []
        requested_size = 0
        for path in requested:
            if not _covered_by_scope(path, checkpoint.write_scope):
                raise _error("SCOPE_ESCAPE")
            entry = by_path.get(path)
            if entry is None or entry.kind != "file" or entry.blob_hash is None:
                raise _error("SCOPE_ESCAPE")
            requested_entries.append(entry)
            requested_size += entry.size
        requested_hashes = (
            frozenset()
            if requested_size > byte_budget
            else frozenset(
                str(entry.blob_hash) for entry in requested_entries if entry.blob_hash
            )
        )
        verified_blobs = self._load_and_verify_blobs(entries, requested_hashes)
        if requested_size > byte_budget:
            raise _error("SCOPE_ESCAPE")
        output: list[CheckpointFile] = []
        for entry in requested_entries:
            if entry.blob_hash is None:
                raise _error("INDEX_CORRUPT")
            body = verified_blobs.get(entry.blob_hash)
            if body is None:
                raise _error("INDEX_CORRUPT")
            output.append(CheckpointFile(entry.path, entry.blob_hash, body))
        return tuple(output)

    def _load_verified_checkpoint(
        self, checkpoint_id: str
    ) -> tuple[Checkpoint, tuple[_ManifestEntry, ...]]:
        return self._load_verified_checkpoint_for_workspace(None, checkpoint_id)

    def _load_verified_checkpoint_for_workspace(
        self, workspace_id: str | None, checkpoint_id: str
    ) -> tuple[Checkpoint, tuple[_ManifestEntry, ...]]:
        cursor = self._connection.cursor()
        cursor.execute("BEGIN")
        try:
            if workspace_id is None:
                row = cursor.execute(
                    "SELECT * FROM checkpoint_records WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                ).fetchone()
            else:
                row = cursor.execute(
                    """
                    SELECT * FROM checkpoint_records
                    WHERE workspace_id = ? AND checkpoint_id = ?
                    """,
                    (workspace_id, checkpoint_id),
                ).fetchone()
            if row is None:
                raise _error("NOT_FOUND")
            checkpoint = _checkpoint_from_row(row)
            if not checkpoint.workspace_id or checkpoint.workspace_root:
                raise _error("HISTORICAL_UNVERIFIED")
            entries = self._load_entries(checkpoint.checkpoint_id, cursor)
            self._validate_checkpoint_integrity(checkpoint, entries)
            try:
                self.index_service.snapshot_facts(
                    checkpoint.workspace_id, checkpoint.snapshot_id
                )
            except IndexError as exc:
                if exc.code == "NOT_FOUND":
                    raise _error("INDEX_CORRUPT") from None
                raise
        except (
            KeyError,
            TypeError,
            ValueError,
            sqlite3.DatabaseError,
        ):
            self._connection.rollback()
            raise _error("INDEX_CORRUPT") from None
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
            return checkpoint, entries

    def restore(
        self,
        ownership: WorkspaceOwnership,
        checkpoint_id: str,
        expected_current_snapshot_id: str,
    ) -> RestoreResult:
        root, scope, workspace_id = self._validate_ownership(ownership)
        target, target_entries = self._load_verified_checkpoint(checkpoint_id)
        self._require_checkpoint_owner(target, ownership, scope, workspace_id)

        self._assert_expected_current(
            workspace_id,
            expected_current_snapshot_id,
            rollback=True,
        )
        self._validate_stored_entries(root, scope, target_entries)
        target_blobs = self._load_and_verify_blobs(target_entries)
        self._preflight_restore_paths(root, target_entries)

        try:
            rescue = self._create(
                ownership,
                expected_current_snapshot_id,
                kind="rescue",
                parent_checkpoint_id=target.checkpoint_id,
            )
            self._assert_expected_current(
                workspace_id,
                expected_current_snapshot_id,
                rollback=True,
            )
        except IndexError as exc:
            if exc.code == "INDEX_STALE":
                raise _error("ROLLBACK_DRIFT") from None
            raise

        rescue, current_entries = self._load_verified_checkpoint(rescue.checkpoint_id)
        latest_entries, _ = self._capture_manifest(root, scope)
        if latest_entries != current_entries:
            raise _error("ROLLBACK_DRIFT")
        changed_paths = _changed_paths(current_entries, target_entries)
        self._apply_manifest(root, scope, current_entries, target_entries, target_blobs)

        restored_entries, _ = self._capture_manifest(root, scope)
        if restored_entries != target_entries:
            raise _error("INDEX_STALE")
        restored = self.index_service.sync(workspace_id)
        restored_snapshot_id = str(restored.snapshot_id)
        self.index_service.assert_current(workspace_id, restored_snapshot_id)
        return RestoreResult(
            checkpoint_id=target.checkpoint_id,
            rescue_checkpoint_id=rescue.checkpoint_id,
            restored_snapshot_id=restored_snapshot_id,
            changed_paths=changed_paths,
        )

    def _create(
        self,
        ownership: WorkspaceOwnership,
        snapshot_id: str,
        *,
        kind: str,
        parent_checkpoint_id: str | None,
    ) -> Checkpoint:
        root, scope, workspace_id = self._validate_ownership(ownership)
        first_entries, _ = self._capture_manifest(root, scope)
        self._assert_expected_current(workspace_id, snapshot_id, rollback=False)
        entries, blobs = self._capture_manifest(root, scope)
        if first_entries != entries:
            raise _error("INDEX_STALE")
        self._assert_expected_current(workspace_id, snapshot_id, rollback=False)

        for blob_hash, body in sorted(blobs.items()):
            self._store_blob(blob_hash, body)

        write_scope_hash = _hash_json(scope)
        manifest_hash = _manifest_hash(entries)
        cas_root_hash = _hash_json(
            tuple(sorted({entry.blob_hash for entry in entries if entry.blob_hash}))
        )
        identity: dict[str, object] = {
            "workflow_id": ownership.workflow_id,
            "task_id": ownership.task_id,
            "owner": ownership.owner,
            "lease_epoch": ownership.lease_epoch,
            "snapshot_id": snapshot_id,
            "write_scope_hash": write_scope_hash,
            "manifest_hash": manifest_hash,
            "cas_root_hash": cas_root_hash,
            "kind": kind,
            "parent_checkpoint_id": parent_checkpoint_id,
        }
        identity["workspace_id"] = workspace_id
        checkpoint_id = _hash_json(identity)
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            workflow_id=ownership.workflow_id,
            task_id=ownership.task_id,
            owner=ownership.owner,
            lease_epoch=ownership.lease_epoch,
            workspace_root="",
            snapshot_id=snapshot_id,
            write_scope=scope,
            write_scope_hash=write_scope_hash,
            manifest_hash=manifest_hash,
            cas_root_hash=cas_root_hash,
            entry_count=len(entries),
            kind=kind,
            parent_checkpoint_id=parent_checkpoint_id,
            workspace_id=workspace_id,
        )
        self._persist(checkpoint, entries)
        return self.status(checkpoint_id)

    def _validate_ownership(
        self, ownership: WorkspaceOwnership
    ) -> tuple[Path, tuple[str, ...], str]:
        self._validate_ownership_shape(ownership)
        workspace_id = ownership.workspace_id
        root = self._resolve_requested_workspace(workspace_id)
        if _unsafe_path(root):
            raise _error("UNSAFE_PATH_TYPE")

        top_level, git_dir, common_dir = _git_worktree_paths(root)
        if _path_key(top_level) != _path_key(root):
            raise _error("WORKTREE_UNOWNED")
        if _path_key(git_dir) == _path_key(common_dir):
            raise _error("WORKTREE_UNOWNED")

        scope = _normalize_scope(ownership.write_scope)
        self._reject_internal_storage(root)
        for relative in scope:
            self._validate_scope_path(root, relative)
        return root, scope, workspace_id

    @staticmethod
    def _validate_ownership_shape(ownership: WorkspaceOwnership) -> None:
        if (
            not isinstance(ownership, WorkspaceOwnership)
            or not isinstance(ownership.workflow_id, str)
            or not isinstance(ownership.task_id, str)
            or not isinstance(ownership.owner, str)
            or not isinstance(ownership.lease_epoch, int)
            or isinstance(ownership.lease_epoch, bool)
            or not ownership.workflow_id.strip()
            or not ownership.task_id.strip()
            or not ownership.owner.strip()
            or ownership.lease_epoch < 1
        ):
            raise _error("WORKSPACE_UNREGISTERED")
        if not isinstance(ownership.workspace_id, str) or not ownership.workspace_id:
            raise _error("WORKSPACE_UNREGISTERED")

    def _resolve_requested_workspace(self, workspace_id: str) -> Path:
        from devkit_runtime.workspace_authority import VerifiedWorkspaceAccess

        try:
            access = self.workspace_authority.resolve(workspace_id)
        except IndexError:
            raise
        except Exception:
            raise _error("WORKSPACE_UNREGISTERED") from None
        if (
            not isinstance(access, VerifiedWorkspaceAccess)
            or access.workspace_id != workspace_id
        ):
            raise _error("WORKSPACE_UNREGISTERED")
        return access.root

    def _assert_scoped_expected_current(
        self, workspace_id: str, snapshot_id: str
    ) -> None:
        try:
            self.index_service.assert_current(workspace_id, snapshot_id)
        except IndexError as exc:
            if exc.code in {"WORKSPACE_UNREGISTERED", "WORKSPACE_REBIND"}:
                raise
            raise _error("ROLLBACK_DRIFT") from None

    def _reject_internal_storage(self, root: Path) -> None:
        if _within(root, self.database_path) or _within(root, self.cas_root):
            raise _error("SCOPE_ESCAPE")

    def _validate_scope_path(self, root: Path, relative: str) -> Path:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        if not _within(root, candidate):
            raise _error("SCOPE_ESCAPE")
        _reject_unsafe_ancestors(root, candidate)
        return candidate

    def _capture_manifest(
        self, root: Path, scope: tuple[str, ...]
    ) -> tuple[tuple[_ManifestEntry, ...], dict[str, bytes]]:
        entries: dict[str, _ManifestEntry] = {}
        blobs: dict[str, bytes] = {}
        for relative in scope:
            path = self._validate_scope_path(root, relative)
            self._capture_path(root, path, entries, blobs)
        return tuple(entries[path] for path in sorted(entries)), blobs

    def _capture_path(
        self,
        root: Path,
        path: Path,
        entries: dict[str, _ManifestEntry],
        blobs: dict[str, bytes],
        path_stat: os.stat_result | None = None,
    ) -> None:
        relative = path.relative_to(root).as_posix()
        if path_stat is None:
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                entries.setdefault(
                    relative, _ManifestEntry(relative, "missing", None, 0, 0)
                )
                return
        if _unsafe_stat(path_stat):
            raise _error("UNSAFE_PATH_TYPE")
        mode = stat.S_IMODE(path_stat.st_mode)
        if stat.S_ISREG(path_stat.st_mode):
            body = _read_regular_file(path, path_stat)
            blob_hash = _hash_bytes(body)
            blobs[blob_hash] = body
            entries[relative] = _ManifestEntry(
                relative, "file", blob_hash, len(body), mode
            )
            return
        if not stat.S_ISDIR(path_stat.st_mode):
            raise _error("UNSAFE_PATH_TYPE")

        entries[relative] = _ManifestEntry(relative, "directory", None, 0, mode)
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            raise _error("UNSAFE_PATH_TYPE") from None
        for child in children:
            try:
                child_stat = child.lstat()
            except OSError:
                raise _error("INDEX_STALE") from None
            if _unsafe_stat(child_stat):
                raise _error("UNSAFE_PATH_TYPE")
            if _ignored_checkpoint_entry(child, child_stat):
                continue
            self._capture_path(root, child, entries, blobs, child_stat)

    def _assert_expected_current(
        self,
        workspace_id: str,
        snapshot_id: str,
        *,
        rollback: bool,
    ) -> None:
        try:
            # A full snapshot comparison is stronger than checking individual
            # scope entries and also handles directory scopes uniformly.
            self.index_service.assert_current(workspace_id, snapshot_id)
        except IndexError as exc:
            if rollback and exc.code == "INDEX_STALE":
                raise _error("ROLLBACK_DRIFT") from None
            raise

    def _store_blob(self, blob_hash: str, body: bytes) -> None:
        path = self._blob_path(blob_hash)
        _safe_storage_directory(self.cas_root, path.parent, create=True)
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            path_stat = None
        if path_stat is not None:
            if _unsafe_stat(path_stat) or not stat.S_ISREG(path_stat.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            if _hash_bytes(_read_regular_file(path, path_stat)) != blob_hash:
                raise _error("INDEX_CORRUPT")
            return
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.replace(path)
            except FileExistsError:
                existing_stat = path.lstat()
                if _unsafe_stat(existing_stat) or not stat.S_ISREG(
                    existing_stat.st_mode
                ):
                    raise _error("UNSAFE_PATH_TYPE") from None
                if _hash_bytes(_read_regular_file(path, existing_stat)) != blob_hash:
                    raise _error("INDEX_CORRUPT") from None
            final_stat = path.lstat()
            if _unsafe_stat(final_stat) or not stat.S_ISREG(final_stat.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            if _hash_bytes(_read_regular_file(path, final_stat)) != blob_hash:
                raise _error("INDEX_CORRUPT")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _blob_path(self, blob_hash: str) -> Path:
        digest = _hash_digest(blob_hash)
        return self.cas_root / "sha256" / digest[:2] / digest[2:]

    def _persist(
        self, checkpoint: Checkpoint, entries: Sequence[_ManifestEntry]
    ) -> None:
        self._validate_checkpoint_integrity(checkpoint, entries)
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            existing = cursor.execute(
                "SELECT * FROM checkpoint_records WHERE checkpoint_id = ?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO checkpoint_records (
                        checkpoint_id, workflow_id, task_id, owner, lease_epoch,
                        workspace_root, workspace_id, snapshot_id, write_scope, write_scope_hash,
                        manifest_hash, cas_root_hash, entry_count, kind,
                        parent_checkpoint_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.checkpoint_id,
                        checkpoint.workflow_id,
                        checkpoint.task_id,
                        checkpoint.owner,
                        checkpoint.lease_epoch,
                        checkpoint.workspace_root,
                        checkpoint.workspace_id,
                        checkpoint.snapshot_id,
                        _json(checkpoint.write_scope),
                        checkpoint.write_scope_hash,
                        checkpoint.manifest_hash,
                        checkpoint.cas_root_hash,
                        checkpoint.entry_count,
                        checkpoint.kind,
                        checkpoint.parent_checkpoint_id,
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO checkpoint_entries (
                        checkpoint_id, path, kind, blob_hash, size, mode
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            checkpoint.checkpoint_id,
                            entry.path,
                            entry.kind,
                            entry.blob_hash,
                            entry.size,
                            entry.mode,
                        )
                        for entry in entries
                    ),
                )
            else:
                try:
                    stored_checkpoint = _checkpoint_from_row(existing)
                except (KeyError, TypeError, ValueError):
                    raise _error("INDEX_CORRUPT") from None
                if stored_checkpoint != checkpoint:
                    raise _error("INDEX_CORRUPT")
            stored_entries = self._load_entries(checkpoint.checkpoint_id, cursor)
            if stored_entries != tuple(entries):
                raise _error("INDEX_CORRUPT")
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _load_entries(
        self,
        checkpoint_id: str,
        cursor: sqlite3.Cursor | None = None,
    ) -> tuple[_ManifestEntry, ...]:
        executor = self._connection if cursor is None else cursor
        rows = executor.execute(
            """
            SELECT path, kind, blob_hash, size, mode
            FROM checkpoint_entries
            WHERE checkpoint_id = ?
            ORDER BY path
            """,
            (checkpoint_id,),
        ).fetchall()
        return tuple(
            _ManifestEntry(
                path=str(row["path"]),
                kind=str(row["kind"]),
                blob_hash=(None if row["blob_hash"] is None else str(row["blob_hash"])),
                size=int(row["size"]),
                mode=int(row["mode"]),
            )
            for row in rows
        )

    def _validate_stored_entries(
        self,
        root: Path,
        scope: tuple[str, ...],
        entries: Sequence[_ManifestEntry],
    ) -> None:
        _validate_entry_set(scope, entries)
        for entry in entries:
            candidate = root.joinpath(*PurePosixPath(entry.path).parts)
            if not _within(root, candidate):
                raise _error("INDEX_CORRUPT")

    def _validate_checkpoint_integrity(
        self,
        checkpoint: Checkpoint,
        entries: Sequence[_ManifestEntry],
    ) -> None:
        try:
            scope = _normalize_scope(checkpoint.write_scope)
        except IndexError:
            raise _error("INDEX_CORRUPT") from None
        if scope != checkpoint.write_scope:
            raise _error("INDEX_CORRUPT")
        _validate_entry_set(scope, entries)
        expected_cas_root = _hash_json(
            tuple(sorted({entry.blob_hash for entry in entries if entry.blob_hash}))
        )
        if (
            checkpoint.entry_count != len(entries)
            or checkpoint.write_scope_hash != _hash_json(scope)
            or checkpoint.manifest_hash != _manifest_hash(entries)
            or checkpoint.cas_root_hash != expected_cas_root
            or checkpoint.kind not in {"checkpoint", "rescue"}
            or (checkpoint.kind == "checkpoint" and checkpoint.parent_checkpoint_id)
            or (checkpoint.kind == "rescue" and not checkpoint.parent_checkpoint_id)
            or not checkpoint.workspace_id
            or checkpoint.workspace_root
        ):
            raise _error("INDEX_CORRUPT")
        identity: dict[str, object] = {
            "workflow_id": checkpoint.workflow_id,
            "task_id": checkpoint.task_id,
            "owner": checkpoint.owner,
            "lease_epoch": checkpoint.lease_epoch,
            "snapshot_id": checkpoint.snapshot_id,
            "write_scope_hash": checkpoint.write_scope_hash,
            "manifest_hash": checkpoint.manifest_hash,
            "cas_root_hash": checkpoint.cas_root_hash,
            "kind": checkpoint.kind,
            "parent_checkpoint_id": checkpoint.parent_checkpoint_id,
        }
        identity["workspace_id"] = checkpoint.workspace_id
        if checkpoint.checkpoint_id != _hash_json(identity):
            raise _error("INDEX_CORRUPT")

    def _preflight_restore_paths(
        self,
        root: Path,
        target_entries: Sequence[_ManifestEntry],
    ) -> None:
        target_directories = {
            entry.path for entry in target_entries if entry.kind == "directory"
        }
        paths_to_check = {
            entry.path for entry in target_entries if entry.kind != "missing"
        }
        for relative in sorted(paths_to_check):
            parent = PurePosixPath(relative).parent
            if parent == PurePosixPath("."):
                continue
            parent_relative = parent.as_posix()
            if parent_relative in target_directories:
                continue
            parent_path = root.joinpath(*parent.parts)
            _reject_unsafe_ancestors(root, parent_path)
            try:
                parent_stat = parent_path.lstat()
            except FileNotFoundError:
                raise _error("ROLLBACK_DRIFT") from None
            if _unsafe_stat(parent_stat):
                raise _error("UNSAFE_PATH_TYPE")
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise _error("ROLLBACK_DRIFT")

    def _load_and_verify_blobs(
        self,
        entries: Sequence[_ManifestEntry],
        retain_hashes: frozenset[str] | None = None,
    ) -> dict[str, bytes]:
        expected_sizes: dict[str, int] = {}
        for entry in entries:
            if entry.blob_hash is None:
                continue
            blob_hash = entry.blob_hash
            previous_size = expected_sizes.setdefault(blob_hash, entry.size)
            if previous_size != entry.size:
                raise _error("INDEX_CORRUPT")
        blobs: dict[str, bytes] = {}
        for blob_hash, expected_size in sorted(expected_sizes.items()):
            path = self._blob_path(blob_hash)
            _safe_storage_directory(self.cas_root, path.parent, create=False)
            try:
                path_stat = path.lstat()
            except OSError:
                raise _error("INDEX_CORRUPT") from None
            if _unsafe_stat(path_stat) or not stat.S_ISREG(path_stat.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            retain = retain_hashes is None or blob_hash in retain_hashes
            body = _stream_cas_blob(
                self.cas_root, path, path_stat, blob_hash, expected_size, retain
            )
            if retain:
                if body is None:
                    raise _error("INDEX_CORRUPT")
                blobs[blob_hash] = body
        return blobs

    def _apply_manifest(
        self,
        root: Path,
        scope: tuple[str, ...],
        current_entries: Sequence[_ManifestEntry],
        target_entries: Sequence[_ManifestEntry],
        blobs: Mapping[str, bytes],
    ) -> None:
        current = {
            entry.path: entry for entry in current_entries if entry.kind != "missing"
        }
        target = {
            entry.path: entry for entry in target_entries if entry.kind != "missing"
        }

        for entry in sorted(
            (value for value in current.values() if value.kind == "directory"),
            key=lambda value: (value.path.count("/"), value.path),
        ):
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            _reject_unsafe_ancestors(root, path)
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                raise _error("ROLLBACK_DRIFT") from None
            if _unsafe_stat(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
                raise _error("ROLLBACK_DRIFT")
            try:
                os.chmod(path, entry.mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                raise _error("ROLLBACK_DRIFT") from None

        removals = [
            entry
            for path, entry in current.items()
            if path not in target or target[path].kind != entry.kind
        ]
        changed_files = [
            entry
            for path, entry in current.items()
            if entry.kind == "file"
            and path in target
            and target[path].kind == "file"
            and target[path].blob_hash != entry.blob_hash
        ]
        for entry in sorted(
            (*removals, *changed_files),
            key=lambda value: (value.path.count("/"), value.path),
            reverse=True,
        ):
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            self._remove_entry(root, scope, path, entry.kind)

        target_directories = sorted(
            (entry for entry in target.values() if entry.kind == "directory"),
            key=lambda value: (value.path.count("/"), value.path),
        )
        for entry in target_directories:
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            self._require_parent_directory(root, path.parent)
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                try:
                    path.mkdir()
                    path_stat = path.lstat()
                except OSError:
                    raise _error("ROLLBACK_DRIFT") from None
            if _unsafe_stat(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            try:
                os.chmod(path, entry.mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                raise _error("ROLLBACK_DRIFT") from None

        target_files = sorted(
            (entry for entry in target.values() if entry.kind == "file"),
            key=lambda value: value.path,
        )
        for entry in target_files:
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            current_entry = current.get(entry.path)
            if (
                current_entry is not None
                and current_entry.kind == "file"
                and current_entry.blob_hash == entry.blob_hash
            ):
                try:
                    path_stat = path.lstat()
                except FileNotFoundError:
                    path_stat = None
                if path_stat is not None:
                    if _unsafe_stat(path_stat) or not stat.S_ISREG(path_stat.st_mode):
                        raise _error("ROLLBACK_DRIFT")
                    if (
                        _hash_bytes(_read_regular_file(path, path_stat))
                        == entry.blob_hash
                    ):
                        os.chmod(path, entry.mode)
                        continue
                    self._remove_entry(root, scope, path, "file")
            self._require_parent_directory(root, path.parent)
            if path.exists() or path.is_symlink():
                _reject_unsafe_ancestors(root, path)
                if _unsafe_path(path):
                    raise _error("UNSAFE_PATH_TYPE")
                if path.is_dir():
                    raise _error("UNSAFE_PATH_TYPE")
                path.unlink()
            body = blobs.get(str(entry.blob_hash))
            if body is None:
                raise _error("INDEX_CORRUPT")
            try:
                with path.open("xb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise _error("ROLLBACK_DRIFT") from None
            os.chmod(path, entry.mode)

        for entry in reversed(target_directories):
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            _reject_unsafe_ancestors(root, path)
            try:
                os.chmod(path, entry.mode)
            except OSError:
                raise _error("ROLLBACK_DRIFT") from None

    def _remove_entry(
        self,
        root: Path,
        scope: tuple[str, ...],
        path: Path,
        kind: str,
    ) -> None:
        relative = path.relative_to(root).as_posix()
        if not _covered_by_scope(relative, scope):
            raise _error("SCOPE_ESCAPE")
        _reject_unsafe_ancestors(root, path)
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            return
        if _unsafe_stat(path_stat):
            raise _error("UNSAFE_PATH_TYPE")
        if kind == "directory":
            if not stat.S_ISDIR(path_stat.st_mode):
                raise _error("ROLLBACK_DRIFT")
            try:
                _reject_unsafe_ancestors(root, path)
                path.rmdir()
            except OSError:
                raise _error("ROLLBACK_DRIFT") from None
        else:
            if not stat.S_ISREG(path_stat.st_mode):
                raise _error("ROLLBACK_DRIFT")
            try:
                os.chmod(path, stat.S_IMODE(path_stat.st_mode) | stat.S_IWUSR)
                _reject_unsafe_ancestors(root, path)
                path.unlink()
            except OSError:
                raise _error("ROLLBACK_DRIFT") from None

    def _require_parent_directory(self, root: Path, parent: Path) -> None:
        if not _within(root, parent):
            raise _error("SCOPE_ESCAPE")
        _reject_unsafe_ancestors(root, parent)
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError:
            raise _error("ROLLBACK_DRIFT") from None
        if _unsafe_stat(parent_stat):
            raise _error("UNSAFE_PATH_TYPE")
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise _error("ROLLBACK_DRIFT")

    def _require_checkpoint_owner(
        self,
        checkpoint: Checkpoint,
        ownership: WorkspaceOwnership,
        scope: tuple[str, ...],
        workspace_id: str,
    ) -> None:
        if (
            checkpoint.workflow_id != ownership.workflow_id
            or checkpoint.task_id != ownership.task_id
            or checkpoint.owner != ownership.owner
            or checkpoint.lease_epoch != ownership.lease_epoch
            or checkpoint.write_scope != scope
            or checkpoint.workspace_id != workspace_id
        ):
            raise _error("WORKTREE_UNOWNED")

    @staticmethod
    def _require_checkpoint_lease(
        checkpoint: Checkpoint, ownership: WorkspaceOwnership
    ) -> None:
        if (
            checkpoint.workflow_id != ownership.workflow_id
            or checkpoint.task_id != ownership.task_id
            or checkpoint.owner != ownership.owner
            or checkpoint.lease_epoch != ownership.lease_epoch
        ):
            raise _error("WORKTREE_UNOWNED")

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoint_records (
                    checkpoint_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL,
                    workspace_root TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL,
                    write_scope TEXT NOT NULL,
                    write_scope_hash TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    cas_root_hash TEXT NOT NULL,
                    entry_count INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('checkpoint', 'rescue')),
                    parent_checkpoint_id TEXT
                );
                CREATE INDEX IF NOT EXISTS checkpoint_records_task
                    ON checkpoint_records(workflow_id, task_id, lease_epoch);
                CREATE TABLE IF NOT EXISTS checkpoint_entries (
                    checkpoint_id TEXT NOT NULL
                        REFERENCES checkpoint_records(checkpoint_id),
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL
                        CHECK (kind IN ('missing', 'file', 'directory')),
                    blob_hash TEXT,
                    size INTEGER NOT NULL,
                    mode INTEGER NOT NULL,
                    PRIMARY KEY (checkpoint_id, path)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(checkpoint_records)"
                ).fetchall()
            }
            if "workspace_id" not in columns:
                self._connection.execute(
                    """
                    ALTER TABLE checkpoint_records
                    ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''
                    """
                )


def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=str(row["checkpoint_id"]),
        workflow_id=str(row["workflow_id"]),
        task_id=str(row["task_id"]),
        owner=str(row["owner"]),
        lease_epoch=int(row["lease_epoch"]),
        workspace_root=str(row["workspace_root"]),
        snapshot_id=str(row["snapshot_id"]),
        write_scope=tuple(str(value) for value in json.loads(row["write_scope"])),
        write_scope_hash=str(row["write_scope_hash"]),
        manifest_hash=str(row["manifest_hash"]),
        cas_root_hash=str(row["cas_root_hash"]),
        entry_count=int(row["entry_count"]),
        kind=str(row["kind"]),
        parent_checkpoint_id=(
            None
            if row["parent_checkpoint_id"] is None
            else str(row["parent_checkpoint_id"])
        ),
        workspace_id=str(row["workspace_id"]),
    )


def _git_worktree_paths(root: Path) -> tuple[Path, Path, Path]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        raise _error("WORKTREE_UNOWNED") from None
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 3:
        raise _error("WORKTREE_UNOWNED")
    try:
        return tuple(Path(line).resolve(strict=True) for line in lines)  # type: ignore[return-value]
    except OSError:
        raise _error("WORKTREE_UNOWNED") from None


def _normalize_scope(write_scope: Iterable[str]) -> tuple[str, ...]:
    if isinstance(write_scope, (str, bytes)):
        raise _error("SCOPE_ESCAPE")
    normalized: set[str] = set()
    try:
        for value in write_scope:
            relative = _normalize_relative(value)
            parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
            if any(part in _IGNORED_DIRECTORIES for part in parts):
                raise _error("SCOPE_ESCAPE")
            if parts[-1] in _INDEX_DATABASE_NAMES:
                raise _error("SCOPE_ESCAPE")
            normalized.add(relative)
    except TypeError:
        raise _error("SCOPE_ESCAPE") from None
    if not normalized:
        raise _error("SCOPE_ESCAPE")
    return tuple(sorted(normalized))


def _normalize_requested_paths(paths: Sequence[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)):
        raise _error("SCOPE_ESCAPE")
    try:
        supplied = tuple(paths)
    except TypeError:
        raise _error("SCOPE_ESCAPE") from None
    if not supplied or any(not isinstance(path, str) for path in supplied):
        raise _error("SCOPE_ESCAPE")
    normalized = _normalize_scope(supplied)
    if len(normalized) != len(supplied) or normalized != supplied:
        raise _error("SCOPE_ESCAPE")
    if any(":" in part for path in normalized for part in PurePosixPath(path).parts):
        raise _error("SCOPE_ESCAPE")
    return normalized


def _normalize_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _error("SCOPE_ESCAPE")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.drive or windows.root or posix.is_absolute():
        raise _error("SCOPE_ESCAPE")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise _error("SCOPE_ESCAPE")
    if os.name == "nt" and any(
        ":" in part or part.endswith((" ", ".")) for part in posix.parts
    ):
        raise _error("SCOPE_ESCAPE")
    normalized = posix.as_posix()
    if normalized in {"", "."}:
        raise _error("SCOPE_ESCAPE")
    return normalized


def _covered_by_scope(path: str, scope: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in scope)


def _validate_entry_set(
    scope: Sequence[str], entries: Sequence[_ManifestEntry]
) -> None:
    if not entries or tuple(sorted(entries, key=lambda entry: entry.path)) != tuple(
        entries
    ):
        raise _error("INDEX_CORRUPT")
    for entry in entries:
        try:
            normalized = _normalize_relative(entry.path)
        except IndexError:
            raise _error("INDEX_CORRUPT") from None
        if normalized != entry.path or not _covered_by_scope(entry.path, scope):
            raise _error("INDEX_CORRUPT")
        if entry.kind not in {"missing", "file", "directory"}:
            raise _error("INDEX_CORRUPT")
        if entry.size < 0 or not 0 <= entry.mode <= 0o7777:
            raise _error("INDEX_CORRUPT")
        if entry.kind == "file":
            if entry.blob_hash is None:
                raise _error("INDEX_CORRUPT")
            _hash_digest(entry.blob_hash)
        elif entry.blob_hash is not None or entry.size != 0:
            raise _error("INDEX_CORRUPT")


def _ignored_checkpoint_entry(path: Path, path_stat: os.stat_result) -> bool:
    name = path.name.casefold()
    if name == ".git":
        return True
    if stat.S_ISDIR(path_stat.st_mode) and name in _IGNORED_DIRECTORIES:
        return True
    return stat.S_ISREG(path_stat.st_mode) and name in _INDEX_DATABASE_NAMES


def _reject_unsafe_ancestors(root: Path, candidate: Path) -> None:
    relative = candidate.relative_to(root)
    current = root
    for part in (".", *relative.parts):
        if part != ".":
            current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise _error("UNSAFE_PATH_TYPE") from None
        if _unsafe_stat(current_stat):
            raise _error("UNSAFE_PATH_TYPE")
        if not _within(root, current.resolve(strict=True)):
            raise _error("UNSAFE_PATH_TYPE")


def _unsafe_path(path: Path) -> bool:
    try:
        return _unsafe_stat(path.lstat())
    except OSError:
        return True


def _unsafe_stat(path_stat: os.stat_result) -> bool:
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _read_regular_file(path: Path, before: os.stat_result) -> bytes:
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _unsafe_stat(opened) or not stat.S_ISREG(opened.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            body = stream.read()
        after = path.lstat()
    except OSError:
        raise _error("INDEX_STALE") from None
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_opened = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_opened or identity_opened != identity_after:
        raise _error("INDEX_STALE")
    return body


def _stream_cas_blob(
    cas_root: Path,
    path: Path,
    before: os.stat_result,
    expected_hash: str,
    expected_size: int,
    retain: bool,
) -> bytes | None:
    """Hash one immutable CAS object without retaining an unrequested payload."""

    try:
        _safe_storage_directory(cas_root, path.parent, create=False)
        descriptor = os.open(path, _read_only_open_flags())
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            opened = os.fstat(stream.fileno())
            if _unsafe_stat(opened) or not stat.S_ISREG(opened.st_mode):
                raise _error("UNSAFE_PATH_TYPE")
            if _file_identity(before) != _file_identity(opened):
                raise _error("INDEX_CORRUPT")
            if opened.st_size != expected_size:
                raise _error("INDEX_CORRUPT")
            digest = hashlib.sha256()
            chunks: list[bytes] | None = [] if retain else None
            retained = 0
            remaining = opened.st_size
            while remaining:
                chunk = stream.read(min(_READ_CHUNK_SIZE, remaining))
                if not chunk or len(chunk) > remaining:
                    raise _error("INDEX_CORRUPT")
                digest.update(chunk)
                remaining -= len(chunk)
                if chunks is not None:
                    if retained + len(chunk) > expected_size:
                        raise _error("INDEX_CORRUPT")
                    retained += len(chunk)
                    chunks.append(chunk)
            if stream.read(1):
                raise _error("INDEX_CORRUPT")
        _safe_storage_directory(cas_root, path.parent, create=False)
        after = path.lstat()
    except IndexError:
        raise
    except (OSError, ValueError):
        raise _error("INDEX_CORRUPT") from None
    if _unsafe_stat(after) or not stat.S_ISREG(after.st_mode):
        raise _error("UNSAFE_PATH_TYPE")
    if _file_identity(opened) != _file_identity(after):
        raise _error("INDEX_CORRUPT")
    actual_hash = f"sha256:{digest.hexdigest()}"
    if actual_hash != expected_hash:
        raise _error("INDEX_CORRUPT")
    return None if chunks is None else b"".join(chunks)


def _file_identity(path_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
    )


def _read_only_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _prepare_storage_file(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(Path(path)))
    resolved = absolute.resolve(strict=False)
    if _path_key(absolute) != _path_key(resolved):
        raise _error("UNSAFE_PATH_TYPE")
    parent = _prepare_storage_directory(absolute.parent)
    candidate = parent / absolute.name
    try:
        path_stat = candidate.lstat()
    except FileNotFoundError:
        return candidate
    if _unsafe_stat(path_stat) or not stat.S_ISREG(path_stat.st_mode):
        raise _error("UNSAFE_PATH_TYPE")
    return candidate


def _prepare_storage_directory(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(Path(path)))
    resolved = absolute.resolve(strict=False)
    if _path_key(absolute) != _path_key(resolved):
        raise _error("UNSAFE_PATH_TYPE")
    try:
        absolute.mkdir(parents=True, exist_ok=True)
        path_stat = absolute.lstat()
    except OSError:
        raise _error("INDEX_UNAVAILABLE") from None
    if _unsafe_stat(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise _error("UNSAFE_PATH_TYPE")
    if _path_key(absolute) != _path_key(absolute.resolve(strict=True)):
        raise _error("UNSAFE_PATH_TYPE")
    return absolute


def _safe_storage_directory(base: Path, target: Path, *, create: bool) -> None:
    if not _within(base, target):
        raise _error("SCOPE_ESCAPE")
    current = base
    relative = target.relative_to(base)
    for part in (".", *relative.parts):
        if part != ".":
            current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            if not create:
                raise _error("INDEX_CORRUPT") from None
            try:
                current.mkdir()
                current_stat = current.lstat()
            except OSError:
                raise _error("INDEX_UNAVAILABLE") from None
        if _unsafe_stat(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
            raise _error("UNSAFE_PATH_TYPE")
        if not _within(base, current.resolve(strict=True)):
            raise _error("UNSAFE_PATH_TYPE")


def _within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _manifest_hash(entries: Sequence[_ManifestEntry]) -> str:
    return _hash_json(
        tuple(
            (entry.path, entry.kind, entry.blob_hash, entry.size, entry.mode)
            for entry in entries
        )
    )


def _changed_paths(
    current_entries: Sequence[_ManifestEntry],
    target_entries: Sequence[_ManifestEntry],
) -> tuple[str, ...]:
    current = {entry.path: entry for entry in current_entries}
    target = {entry.path: entry for entry in target_entries}
    return tuple(
        path
        for path in sorted(set(current) | set(target))
        if current.get(path) != target.get(path)
    )


def _hash_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _hash_digest(value: str) -> str:
    prefix, separator, digest = value.partition(":")
    if (
        prefix != "sha256"
        or separator != ":"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise _error("INDEX_CORRUPT")
    return digest


def _hash_json(value: object) -> str:
    return _hash_bytes(_json(value).encode("utf-8"))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _error(code: str) -> IndexError:
    return IndexError(code, "request rejected")


__all__ = [
    "Checkpoint",
    "CheckpointFile",
    "CheckpointService",
    "RestoreResult",
    "WorkspaceOwnership",
]
