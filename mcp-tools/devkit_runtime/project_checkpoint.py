"""Invocation-owned Project Index and checkpoint runtime bundles."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from project_index.checkpoints import (
    Checkpoint,
    CheckpointService,
    RestoreResult,
    WorkspaceOwnership,
)
from project_index.models import IndexError, QueryResult
from project_index.service import ProjectIndexService
from project_index.store import ProjectIndexStore, StoreError

from .sqlite_snapshot import (
    SqliteSnapshotError,
    VerifiedSqliteSnapshot,
    open_verified_sqlite_snapshot,
)
from .workspace_authority import WorkspaceRootAuthority


@dataclass
class ProjectCheckpointRuntime:
    """One closeable runtime bundle with a single authority lifetime.

    New callers use the workspace-bound ``create``, ``status``, and ``restore``
    methods. The legacy unscoped ``CheckpointService.status`` remains confined
    to legacy compatibility through R8; R8A removes that path entirely.
    """

    project_index: ProjectIndexService
    _checkpoints: CheckpointService
    workspace_authority: WorkspaceRootAuthority
    snapshot: VerifiedSqliteSnapshot | None
    _connection: sqlite3.Connection | None = None
    _closed: bool = False

    def status(self, workspace_id: str, checkpoint_id: str) -> Checkpoint:
        """Return a checkpoint only through its compound workspace boundary."""

        return self._checkpoints.status_for_workspace(workspace_id, checkpoint_id)

    @property
    def checkpoint_service(self) -> CheckpointService:
        """Expose the call-owned checkpoint service to verified runtime adapters."""

        if self._closed:
            raise IndexError("INDEX_UNAVAILABLE", "project checkpoint runtime is closed")
        return self._checkpoints

    def create(
        self,
        workspace_id: str,
        ownership: WorkspaceOwnership,
        snapshot_id: str,
    ) -> Checkpoint:
        if self.snapshot is not None:
            raise IndexError(
                "INDEX_UNAVAILABLE", "checkpoint create requires readwrite access"
            )
        return self._checkpoints.create_for_workspace(
            workspace_id, ownership, snapshot_id
        )

    def restore(
        self,
        workspace_id: str,
        ownership: WorkspaceOwnership,
        checkpoint_id: str,
        expected_current_snapshot_id: str,
    ) -> RestoreResult:
        if self.snapshot is not None:
            raise IndexError(
                "INDEX_UNAVAILABLE", "checkpoint restore requires readwrite access"
            )
        return self._checkpoints.restore_for_workspace(
            workspace_id,
            ownership,
            checkpoint_id,
            expected_current_snapshot_id,
        )

    def query(
        self,
        workspace_id: str,
        snapshot_id: str,
        query: str,
        mode: str = "lexical",
        node_kinds: Sequence[str] = (),
        relations: Sequence[str] = (),
        max_nodes: int = 50,
        max_depth: int = 1,
        source_lines: int = 12,
        byte_budget: int = 32768,
        allow_miss_escape: bool = False,
    ) -> QueryResult:
        """Persist a deterministic receipt through this runtime's RW service."""

        if self.snapshot is not None:
            raise IndexError(
                "INDEX_UNAVAILABLE", "project index query requires readwrite access"
            )
        return self.project_index.query(
            workspace_id,
            snapshot_id,
            query,
            mode,
            node_kinds,
            relations,
            max_nodes,
            max_depth,
            source_lines,
            byte_budget,
            allow_miss_escape,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.project_index.close()
            self._checkpoints.close()
        finally:
            if self.snapshot is not None:
                self.snapshot.close()
            elif self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> ProjectCheckpointRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def open_project_checkpoint_ro(
    database_path: str | Path,
    cas_root: str | Path,
    *,
    scratch_root: str | Path | None = None,
) -> ProjectCheckpointRuntime:
    """Open Project Index and Checkpoint readers from one verified R0 snapshot."""

    snapshot: VerifiedSqliteSnapshot | None = None
    try:
        snapshot = open_verified_sqlite_snapshot(
            database_path,
            scratch_root=scratch_root,
            protected_roots=(cas_root,),
        )
        project_connection = snapshot.connect()
        checkpoint_connection = snapshot.connect()
        ProjectIndexStore.validate_prepared_connection(project_connection)
        CheckpointService.validate_prepared_connection(checkpoint_connection)
        project_store = ProjectIndexStore.from_prepared_connection(
            database_path, project_connection
        )
        project_index = ProjectIndexService.from_prepared_store(project_store)
        checkpoints = CheckpointService.from_prepared_connection(
            database_path,
            cas_root,
            project_index,
            project_index.workspace_authority,
            checkpoint_connection,
        )
        return ProjectCheckpointRuntime(
            project_index=project_index,
            _checkpoints=checkpoints,
            workspace_authority=project_index.workspace_authority,
            snapshot=snapshot,
        )
    except (IndexError, SqliteSnapshotError, StoreError) as exc:
        if snapshot is not None:
            snapshot.close()
        if isinstance(exc, IndexError):
            raise
        raise IndexError(
            "INDEX_UNAVAILABLE", "project checkpoint storage is unavailable"
        ) from exc
    except Exception:
        if snapshot is not None:
            snapshot.close()
        raise


def open_project_checkpoint_rw(
    database_path: str | Path,
    cas_root: str | Path,
    *,
    scratch_root: str | Path | None = None,
) -> ProjectCheckpointRuntime:
    """Open one prepared Project/Checkpoint connection without bootstrapping."""

    connection: sqlite3.Connection | None = None
    try:
        _validate_prepared_database(database_path, cas_root, scratch_root=scratch_root)
        source = Path(database_path).absolute()
        connection = sqlite3.connect(
            source.as_uri() + "?mode=rw",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        _configure_prepared_rw_connection(connection)
        project_store = ProjectIndexStore.from_prepared_connection(
            database_path, connection
        )
        project_index = ProjectIndexService.from_prepared_store(project_store)
        checkpoints = CheckpointService.from_prepared_connection(
            database_path,
            cas_root,
            project_index,
            project_index.workspace_authority,
            connection,
        )
        return ProjectCheckpointRuntime(
            project_index=project_index,
            _checkpoints=checkpoints,
            workspace_authority=project_index.workspace_authority,
            snapshot=None,
            _connection=connection,
        )
    except (IndexError, SqliteSnapshotError, StoreError, sqlite3.DatabaseError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, IndexError):
            raise
        raise IndexError(
            "INDEX_UNAVAILABLE", "project checkpoint storage is unavailable"
        ) from exc
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _validate_prepared_database(
    database_path: str | Path,
    cas_root: str | Path,
    *,
    scratch_root: str | Path | None,
) -> None:
    """Validate both schemas from an R0 copy before the RW connection is opened."""

    with open_verified_sqlite_snapshot(
        database_path,
        scratch_root=scratch_root,
        protected_roots=(cas_root,),
    ) as snapshot:
        connection = snapshot.connect()
        try:
            ProjectIndexStore.validate_prepared_connection(connection)
            CheckpointService.validate_prepared_connection(connection)
        finally:
            connection.close()


def _configure_prepared_rw_connection(connection: sqlite3.Connection) -> None:
    """Apply non-durable connection safeguards to an already prepared WAL DB."""

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    if foreign_keys != 1 or busy_timeout != 5000 or journal_mode.casefold() != "wal":
        raise IndexError(
            "INDEX_UNAVAILABLE",
            "prepared project checkpoint database must retain WAL safeguards",
        )
