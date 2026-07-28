"""Frozen public data contracts for Code Atlas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, ClassVar

from .canonical import canonical_id, freeze_json, thaw_json


class AtlasError(RuntimeError):
    """A safe Atlas failure whose rendered message is its stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NodeKind(str, Enum):
    TASK_EPISODE = "TaskEpisode"
    INTENT = "Intent"
    RECIPE = "Recipe"
    CODE_TEMPLATE = "CodeTemplate"
    ADAPTATION_SLOT = "AdaptationSlot"
    CONSTRAINT = "Constraint"
    DEPENDENCY = "Dependency"
    TEST_SPEC = "TestSpec"
    EXECUTION_RECEIPT = "ExecutionReceipt"
    SOURCE_EVIDENCE = "SourceEvidence"
    LANGUAGE = "Language"
    FRAMEWORK = "Framework"


class EdgeRelation(str, Enum):
    SOLVES = "SOLVES"
    DERIVED_FROM = "DERIVED_FROM"
    HAS_IMPLEMENTATION = "HAS_IMPLEMENTATION"
    HAS_SLOT = "HAS_SLOT"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    REQUIRES = "REQUIRES"
    VERIFIED_BY = "VERIFIED_BY"
    CHANGES = "CHANGES"
    TESTS = "TESTS"
    SUPERSEDES = "SUPERSEDES"
    BUNDLED_AS = "BUNDLED_AS"


class AtlasStatus(str, Enum):
    READY = "READY"
    NO_VERIFIED_RECIPE = "NO_VERIFIED_RECIPE"
    INDEX_STALE = "INDEX_STALE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    RENDER_INVALID = "RENDER_INVALID"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    RECIPE_QUARANTINED = "RECIPE_QUARANTINED"
    INGEST_PENDING = "INGEST_PENDING"
    ATLAS_UNAVAILABLE = "ATLAS_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _serialize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    return thaw_json(value)


class _Record:
    def to_dict(self) -> dict[str, Any]:
        """Serialize the public record using ordinary JSON containers."""
        return {item.name: _serialize(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class AtlasNode(_Record):
    node_id: str
    kind: NodeKind
    payload: Any
    schema_version: str = "0.3"
    extractor_id: str = "code_atlas"
    extractor_version: str = "0.3.0"
    provenance: Any = field(default_factory=dict)
    source_hashes: tuple[str, ...] = ()
    created_at: str | None = None
    superseded_at: str | None = None
    quarantine_state: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))
        object.__setattr__(self, "provenance", freeze_json(self.provenance))
        object.__setattr__(self, "source_hashes", tuple(self.source_hashes))

    @classmethod
    def create(
        cls,
        kind: NodeKind,
        payload: Any,
        *,
        schema_version: str = "0.3",
        extractor_id: str = "code_atlas",
        extractor_version: str = "0.3.0",
        provenance: Any = None,
        source_hashes: tuple[str, ...] | list[str] = (),
        created_at: str | None = None,
        superseded_at: str | None = None,
        quarantine_state: str | None = None,
    ) -> "AtlasNode":
        identity = {
            "kind": kind.value,
            "schema_version": schema_version,
            "extractor_id": extractor_id,
            "extractor_version": extractor_version,
            "provenance": {} if provenance is None else provenance,
            "payload": payload,
            "source_hashes": list(source_hashes),
        }
        return cls(
            node_id=canonical_id(identity), kind=kind, payload=payload,
            schema_version=schema_version, extractor_id=extractor_id,
            extractor_version=extractor_version,
            provenance={} if provenance is None else provenance,
            source_hashes=tuple(source_hashes), created_at=created_at,
            superseded_at=superseded_at, quarantine_state=quarantine_state,
        )


_ENDPOINTS: dict[EdgeRelation, tuple[frozenset[NodeKind], frozenset[NodeKind]]] = {
    EdgeRelation.SOLVES: (frozenset({NodeKind.TASK_EPISODE, NodeKind.RECIPE}), frozenset({NodeKind.INTENT})),
    EdgeRelation.DERIVED_FROM: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.TASK_EPISODE, NodeKind.SOURCE_EVIDENCE})),
    EdgeRelation.HAS_IMPLEMENTATION: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.CODE_TEMPLATE})),
    EdgeRelation.HAS_SLOT: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.ADAPTATION_SLOT})),
    EdgeRelation.CONSTRAINED_BY: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.CONSTRAINT})),
    EdgeRelation.REQUIRES: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.DEPENDENCY, NodeKind.FRAMEWORK, NodeKind.LANGUAGE})),
    EdgeRelation.VERIFIED_BY: (frozenset({NodeKind.TASK_EPISODE, NodeKind.RECIPE}), frozenset({NodeKind.TEST_SPEC, NodeKind.EXECUTION_RECEIPT})),
    EdgeRelation.CHANGES: (frozenset({NodeKind.TASK_EPISODE}), frozenset({NodeKind.SOURCE_EVIDENCE})),
    EdgeRelation.TESTS: (frozenset({NodeKind.TEST_SPEC, NodeKind.SOURCE_EVIDENCE}), frozenset({NodeKind.SOURCE_EVIDENCE})),
    EdgeRelation.SUPERSEDES: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.RECIPE})),
    EdgeRelation.BUNDLED_AS: (frozenset({NodeKind.RECIPE}), frozenset({NodeKind.SOURCE_EVIDENCE})),
}


@dataclass(frozen=True, slots=True)
class AtlasEdge(_Record):
    edge_id: str
    relation: EdgeRelation
    source_id: str
    target_id: str
    source_kind: NodeKind
    target_kind: NodeKind
    payload: Any = field(default_factory=dict)
    schema_version: str = "0.3"
    provenance: Any = field(default_factory=dict)
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))
        object.__setattr__(self, "provenance", freeze_json(self.provenance))

    @classmethod
    def create(
        cls, relation: EdgeRelation, source: AtlasNode, target: AtlasNode, *, payload: Any = None,
        schema_version: str = "0.3", provenance: Any = None, created_at: str | None = None,
    ) -> "AtlasEdge":
        allowed_source, allowed_target = _ENDPOINTS[relation]
        if source.kind not in allowed_source or target.kind not in allowed_target:
            raise AtlasError("invalid_edge_endpoints")
        identity = {
            "relation": relation.value, "source_id": source.node_id, "target_id": target.node_id,
            "schema_version": schema_version, "provenance": {} if provenance is None else provenance,
            "payload": {} if payload is None else payload,
        }
        return cls(
            edge_id=canonical_id(identity), relation=relation, source_id=source.node_id,
            target_id=target.node_id, source_kind=source.kind, target_kind=target.kind,
            payload={} if payload is None else payload, schema_version=schema_version,
            provenance={} if provenance is None else provenance, created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class SlotSpec(_Record):
    name: str
    type: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class ConstraintSpec(_Record):
    kind: str
    subject: str
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_json(self.value))


@dataclass(frozen=True, slots=True)
class DependencySpec(_Record):
    name: str
    kind: str
    specifier: str = ""


@dataclass(frozen=True, slots=True)
class TestSpec(_Record):
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))


@dataclass(frozen=True, slots=True)
class TemplateOperation(_Record):
    kind: str
    path_slot: str
    template_hash: str
    separator: str = ""
    target_symbol_slot: str = ""


@dataclass(frozen=True, slots=True)
class RecipeManifest(_Record):
    recipe_id: str
    recipe_key: str
    version: str
    intent_id: str
    language_name: str
    language_extractor_version: str
    repository_signature: str
    layer: str
    manifest_hash: str
    framework_name: str | None = None
    framework_specifier: str | None = None
    slots: tuple[SlotSpec, ...] = ()
    constraints: tuple[ConstraintSpec, ...] = ()
    dependencies: tuple[DependencySpec, ...] = ()
    tests: tuple[TestSpec, ...] = ()
    operations: tuple[TemplateOperation, ...] = ()
    provenance_kind: str = ""
    provenance_source: str = ""
    schema_version: str = "0.3"
    superseded_ids: tuple[str, ...] = ()
    quarantine_state: str | None = None

    def __post_init__(self) -> None:
        for name in ("slots", "constraints", "dependencies", "tests", "operations", "superseded_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class GraphQueryResult(_Record):
    nodes: tuple[AtlasNode, ...] = ()
    edges: tuple[AtlasEdge, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))


@dataclass(frozen=True, slots=True)
class ImplementationPacket(_Record):
    packet_id: str
    trace_id: str
    workspace_id: str
    snapshot_id: str
    recipe_id: str
    node_ids: tuple[str, ...] = ()
    edge_ids: tuple[str, ...] = ()
    evidence_windows: tuple[Any, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    operations: tuple[TemplateOperation, ...] = ()
    slots: tuple[SlotSpec, ...] = ()
    constraints: tuple[ConstraintSpec, ...] = ()
    dependencies: tuple[DependencySpec, ...] = ()
    tests: tuple[TestSpec, ...] = ()
    gaps: tuple[str, ...] = ()
    source_hashes: tuple[str, ...] = ()
    template_hashes: tuple[str, ...] = ()
    receipt_hashes: tuple[str, ...] = ()
    next_action: str = ""

    def __post_init__(self) -> None:
        for name in ("node_ids", "edge_ids", "evidence_windows", "evidence_hashes", "operations", "slots", "constraints", "dependencies", "tests", "gaps", "source_hashes", "template_hashes", "receipt_hashes"):
            value = tuple(freeze_json(item) if name == "evidence_windows" else item for item in getattr(self, name))
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class PreparationResult(_Record):
    status: AtlasStatus
    packet: ImplementationPacket | None = None
    candidate_recipe_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_recipe_ids", tuple(self.candidate_recipe_ids))
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class RenderResult(_Record):
    status: AtlasStatus
    packet_id: str
    patch_candidate: str = ""
    patch_hash: str = ""
    bindings_hash: str = ""
    test_specs: tuple[TestSpec, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_specs", tuple(self.test_specs))
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class ExtractionGap(_Record):
    code: str
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult(_Record):
    eligible: bool
    manifest: RecipeManifest | None = None
    bindings: Any = field(default_factory=dict)
    gaps: tuple[ExtractionGap, ...] = ()
    nodes: tuple[AtlasNode, ...] = ()
    edges: tuple[AtlasEdge, ...] = ()
    blobs: tuple[str, ...] = ()
    episode_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bindings", freeze_json(self.bindings))
        for name in ("gaps", "nodes", "edges", "blobs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class IngestionReceipt(_Record):
    ingestion_key: str
    payload_hash: str
    status: AtlasStatus
    episode_id: str
    recipe_id: str | None = None
    reasons: tuple[str, ...] = ()
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class AcceptanceProjection(_Record):
    acceptance_id: str
    code_task_id: str
    output_snapshot_id: str
    atlas_ingest_state: AtlasStatus
    episode_id: str
    recipe_id: str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
