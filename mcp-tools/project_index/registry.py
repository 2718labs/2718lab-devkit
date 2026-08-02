"""Durable, opaque workspace registration for the project index."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .models import IndexError
from .workspace import (
    canonical_workspace_root,
    is_workspace_id,
    workspace_id_for_root,
    workspace_identity,
    workspace_paths_match,
)

if TYPE_CHECKING:
    from .store import ProjectIndexStore


class WorkspaceRegistry:
    """Bind one validated filesystem root to a durable opaque identifier."""

    def __init__(self, store: ProjectIndexStore) -> None:
        self._store = store

    def project_index_register(self, workspace_root: str | os.PathLike[str]) -> str:
        """Register workspace_root once and return only its opaque identifier."""
        root = canonical_workspace_root(workspace_root)
        workspace_id = workspace_id_for_root(root)
        identity = workspace_identity(root)
        stored = self._store.register_workspace(workspace_id, str(root), identity)
        if (
            not workspace_paths_match(stored.root_path, root)
            or stored.identity != identity
        ):
            raise IndexError(
                "WORKSPACE_REBIND", "workspace registration is no longer valid"
            )
        return workspace_id

    def resolve(self, workspace_id: str) -> Path:
        """Resolve a registered opaque id after rechecking its root identity."""
        if not is_workspace_id(workspace_id):
            raise IndexError("WORKSPACE_UNREGISTERED", "workspace is not registered")
        stored = self._store.get_workspace_registration(workspace_id)
        if stored is None:
            raise IndexError("WORKSPACE_UNREGISTERED", "workspace is not registered")
        try:
            root = canonical_workspace_root(stored.root_path)
            identity = workspace_identity(root)
        except IndexError as exc:
            raise IndexError(
                "WORKSPACE_REBIND", "workspace registration is no longer valid"
            ) from exc
        if (
            workspace_id_for_root(root) != workspace_id
            or not workspace_paths_match(stored.root_path, root)
            or stored.identity != identity
        ):
            raise IndexError(
                "WORKSPACE_REBIND", "workspace registration is no longer valid"
            )
        return root
