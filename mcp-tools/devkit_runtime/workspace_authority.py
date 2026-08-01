"""Typed workspace-root authority for runtime-owned operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from project_index.registry import WorkspaceRegistry

_ACCESS_CONSTRUCTION_TOKEN: Final = object()


@dataclass(frozen=True, init=False)
class VerifiedWorkspaceAccess:
    """One freshly revalidated workspace binding returned by the authority."""

    workspace_id: str
    root: Path

    def __init__(self, token: object, workspace_id: str, root: Path) -> None:
        if token is not _ACCESS_CONSTRUCTION_TOKEN:
            raise TypeError("VerifiedWorkspaceAccess is authority-constructed")
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "root", root)


class WorkspaceRootAuthority:
    """Resolve opaque workspace IDs through the one durable registry."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    def resolve(self, workspace_id: str) -> VerifiedWorkspaceAccess:
        root = self._registry.resolve(workspace_id)
        return VerifiedWorkspaceAccess(_ACCESS_CONSTRUCTION_TOKEN, workspace_id, root)
