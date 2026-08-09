"""Immutable records exposed by the deterministic project index."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

PROVENANCE_VALUES = frozenset({"observed", "resolved", "declared"})
WORKSPACE_BINDING_STATES = frozenset({"active", "historical_unverified"})


class IndexError(RuntimeError):
    """Stable, safe error raised at the project-index boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IndexState(str, Enum):
    INDEX_READY = "INDEX_READY"
    INDEX_PARTIAL = "INDEX_PARTIAL"
    INDEX_STALE = "INDEX_STALE"
    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    INDEX_CORRUPT = "INDEX_CORRUPT"
    HISTORICAL_UNVERIFIED = "historical_unverified"


@dataclass(frozen=True)
class IndexNode:
    node_id: str
    kind: str
    path: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    content_hash: str
    attributes: tuple[tuple[str, str], ...] = ()
    extractor_id: str = ""
    extractor_version: str = ""
    provenance: str = "observed"
    start_byte: int = 0
    end_byte: int = 0

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE_VALUES:
            raise ValueError("unsupported project-index provenance")


@dataclass(frozen=True)
class IndexEdge:
    edge_id: str
    source_id: str
    target_id: str
    relation: str
    path: str = ""
    start_line: int = 1
    end_line: int = 1
    start_byte: int = 0
    end_byte: int = 0
    content_hash: str = ""
    extractor_id: str = ""
    extractor_version: str = ""
    provenance: str = "observed"

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE_VALUES:
            raise ValueError("unsupported project-index provenance")


@dataclass(frozen=True)
class CoverageGap:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class PackageDescriptor:
    """One immutable logical package boundary captured by a snapshot."""

    package_id: str
    ecosystem: str
    name: str
    root_path: str
    manifest_path: str
    manifest_hash: str


@dataclass(frozen=True)
class PackagePage:
    """One immutable, snapshot-bound page of package descriptors."""

    snapshot_id: str
    offset: int
    limit: int
    total_count: int
    packages: tuple[PackageDescriptor, ...]
    next_offset: int | None


@dataclass(frozen=True)
class SourceWindow:
    path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str


@dataclass(frozen=True)
class IndexSnapshot:
    snapshot_id: str
    workspace: str
    state: IndexState
    file_count: int
    blob_count: int
    reused_blob_count: int
    node_count: int
    edge_count: int
    gap_count: int
    manifest_hash: str = ""
    parser_set_hash: str = ""
    head: str | None = None
    workspace_id: str = ""
    binding_state: str = "active"
    packages: tuple[PackageDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if self.binding_state not in WORKSPACE_BINDING_STATES:
            raise ValueError("unsupported project-index workspace binding state")

    @property
    def trust_state(self) -> str:
        """Compatibility spelling for the workspace binding trust state."""
        return self.binding_state

    @property
    def historical_unverified(self) -> bool:
        return self.binding_state == "historical_unverified"


@dataclass(frozen=True)
class SnapshotFacts:
    snapshot: IndexSnapshot
    file_hashes: tuple[tuple[str, str], ...]
    nodes: tuple[IndexNode, ...]
    edges: tuple[IndexEdge, ...]
    gaps: tuple[CoverageGap, ...]
    packages: tuple[PackageDescriptor, ...] = ()


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content_hash: str
    body: bytes


@dataclass(frozen=True)
class IndexStatus:
    workspace: str
    snapshot_id: str | None
    state: IndexState
    required_paths: tuple[str, ...] = ()
    missing_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    binding_state: str = "active"


@dataclass(frozen=True)
class QueryResult:
    trace_id: str
    snapshot_id: str
    state: IndexState
    nodes: tuple[IndexNode, ...]
    edges: tuple[IndexEdge, ...]
    source_windows: tuple[SourceWindow, ...]
    gaps: tuple[CoverageGap, ...]
    truncated: bool


@dataclass(frozen=True)
class QueryReceipt:
    trace_id: str
    snapshot_id: str
    query: str
    mode: str
    node_kinds: tuple[str, ...]
    relations: tuple[str, ...]
    max_nodes: int
    max_depth: int
    source_lines: int
    byte_budget: int
    allow_miss_escape: bool
    miss_escape_used: bool
    returned_node_ids: tuple[str, ...]
    returned_edge_ids: tuple[str, ...]
    returned_source_windows: tuple[tuple[str, int, int, str], ...]
    gaps: tuple[CoverageGap, ...]
    truncated: bool
    package_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotDiff:
    from_snapshot_id: str
    to_snapshot_id: str
    added_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    added_node_ids: tuple[str, ...]
    removed_node_ids: tuple[str, ...]
    added_edge_ids: tuple[str, ...]
    removed_edge_ids: tuple[str, ...]
