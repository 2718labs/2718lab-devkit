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
from .service import ProjectIndexService

__all__ = [
    "CoverageGap",
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
]
