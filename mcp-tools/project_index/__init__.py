"""Deterministic, parser-backed project indexing API."""

from .models import (
    CoverageGap,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    QueryReceipt,
    QueryResult,
    SnapshotDiff,
    SourceWindow,
)
from .checkpoints import Checkpoint, CheckpointService, WorkspaceOwnership
from .registry import WorkspaceRegistry
from .service import ProjectIndexService

__all__ = [
    "CoverageGap",
    "Checkpoint",
    "CheckpointService",
    "IndexEdge",
    "IndexError",
    "IndexNode",
    "IndexSnapshot",
    "IndexState",
    "IndexStatus",
    "ProjectIndexService",
    "QueryReceipt",
    "QueryResult",
    "SnapshotDiff",
    "SourceWindow",
    "WorkspaceOwnership",
    "WorkspaceRegistry",
]
