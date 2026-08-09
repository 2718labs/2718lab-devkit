"""Deterministic, parser-backed project indexing API."""

from .checkpoints import Checkpoint, CheckpointService, WorkspaceOwnership
from .models import (
    CoverageGap,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    PackageDescriptor,
    QueryReceipt,
    QueryResult,
    SnapshotDiff,
    SourceWindow,
)
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
    "PackageDescriptor",
    "ProjectIndexService",
    "QueryReceipt",
    "QueryResult",
    "SnapshotDiff",
    "SourceWindow",
    "WorkspaceOwnership",
    "WorkspaceRegistry",
]
