"""Bounded, deterministic Atlas graph and packet service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from project_index.models import IndexError, SnapshotFacts
from project_index.service import ProjectIndexService
from project_index.workspace import is_workspace_id

from .canonical import canonical_hash, canonical_json, normalize_intent_id, thaw_json
from .extractors import BoundExecutionReceipt, ExtractionRequest, PythonRecipeExtractor
from .matching import (
    MatchCandidate,
    normalize_framework,
    select_recipe,
    structural_repository_signature,
)
from .models import (
    ATLAS_MATCHER_VERSION,
    AcceptanceProjection,
    AtlasEdge,
    AtlasError,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    ImplementationPacket,
    IngestionReceipt,
    NodeKind,
    PreparationResult,
    RecipeManifest,
    RenderResult,
    SlotSpec,
    TemplateOperation,
    TestSpec,
)
from .recipes import BundledRecipeLoader
from .rendering import render_patch, validate_bindings
from .security import (
    MAX_CHANGED_FILES,
    MAX_COMMAND_SPEC_BYTES,
    MAX_GRAPH_DEPTH,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
    MAX_SLOT_COUNT,
    MAX_TEMPLATE_BYTES,
    path_collision_key,
    validate_absent_workspace_path,
    validate_candidate_path,
    validate_fragment,
    validate_slot_value,
)
from .store import AtlasStore, StoreConflictError

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PREPARE_REQUEST_SCHEMA = "atlas-prepare-request/v1"
_MANIFEST_FIELDS = frozenset(
    field.name for field in fields(RecipeManifest) if field.name != "recipe_id"
)
_SLOT_FIELDS = frozenset(field.name for field in fields(SlotSpec))
_CONSTRAINT_FIELDS = frozenset(field.name for field in fields(ConstraintSpec))
_DEPENDENCY_FIELDS = frozenset(field.name for field in fields(DependencySpec))
_TEST_FIELDS = frozenset(field.name for field in fields(TestSpec))
_OPERATION_FIELDS = frozenset(field.name for field in fields(TemplateOperation))
_GAP_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PLACEHOLDER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_SLOT_NAME = re.compile(r"^path_(\d{3})$")
_SYMBOL_SLOT_NAME = re.compile(r"^symbol_(\d{3})$")
_LOCAL_SLOT_TYPES = frozenset({"relative_python_path", "python_identifier"})
_LOCAL_CONSTRAINT_KINDS = frozenset({"path_suffix", "required_symbol"})
_LOCAL_OPERATION_KINDS = frozenset({"create_python_file", "append_python_nodes"})
_LOCAL_DISCOVERY_LIMIT = MAX_GRAPH_NODES + 1
_MAX_LOCAL_SUPERSEDED_IDS = MAX_GRAPH_NODES
_LOCAL_QUARANTINE_STATES = (None, "quarantined")
_BODY_KEYS = frozenset(
    {
        "body",
        "text",
        "content",
        "fragment",
        "raw",
        "raw_text",
        "source_body",
        "source_text",
        "template_body",
        "template_text",
    }
)
_RENDER_REASON_CODES = frozenset(
    {
        "request_invalid",
        "packet_not_found",
        "packet_integrity_mismatch",
        "packet_workspace_mismatch",
        "packet_snapshot_mismatch",
        "packet_recipe_mismatch",
        "packet_template_mismatch",
        "recipe_unavailable",
        "recipe_quarantined",
        "constraint_unmet",
        "index_stale",
        "binding_schema_invalid",
        "binding_missing",
        "binding_unknown",
        "binding_invalid",
        "template_blob_missing",
        "template_blob_integrity",
        "template_blob_unsafe",
        "template_placeholder_invalid",
        "template_slot_mismatch",
        "secret_detected",
        "source_path_unsafe",
        "source_path_raced",
        "source_hash_mismatch",
        "source_encoding_invalid",
        "source_newline_invalid",
        "path_case_collision",
        "operation_unsupported",
        "operation_path_collision",
        "target_symbol_ambiguous",
        "target_layout_unsupported",
        "rendered_parse_invalid",
        "test_spec_invalid",
        "render_budget_exceeded",
    }
)
_ACCEPTANCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_EVIDENCE_BINDING_SCHEMA = "acceptance-evidence-binding/v1"
_MAX_ACCEPTANCE_EVIDENCE_IDS = 32
_RECIPE_ONLY_KINDS = frozenset(
    {
        NodeKind.RECIPE,
        NodeKind.CODE_TEMPLATE,
        NodeKind.ADAPTATION_SLOT,
        NodeKind.CONSTRAINT,
        NodeKind.LANGUAGE,
    }
)


@dataclass(frozen=True, slots=True)
class AcceptedAtlasProjectionRequest:
    """Opaque, already-accepted identifiers needed for one Atlas projection.

    This is deliberately a data-only boundary: source bodies, raw receipts,
    commands, workspace paths, retry state, and timestamps belong behind the
    constructor-injected evidence reader rather than this public request.
    """

    ingestion_key: str
    payload_hash: str
    acceptance_id: str
    workflow_id: str
    code_task_id: str
    code_task_version: int
    input_snapshot_id: str
    output_snapshot_id: str
    indexed_diff_hash: str
    intent_id: str
    language: str
    framework: str
    checkpoint_id: str
    checkpoint_hash: str
    output_query_trace_id: str
    verification_artifact_hashes: tuple[str, ...]
    execution_receipt_ids: tuple[str, ...]
    evidence_binding_hash: str

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        code_task_id: str,
        code_task_version: int,
        input_snapshot_id: str,
        output_snapshot_id: str,
        indexed_diff_hash: str,
        intent_id: str,
        language: str,
        framework: str,
        checkpoint_id: str,
        checkpoint_hash: str,
        output_query_trace_id: str,
        verification_artifact_hashes: tuple[str, ...],
        execution_receipt_ids: tuple[str, ...],
    ) -> AcceptedAtlasProjectionRequest:
        """Build the canonical core and evidence-binding identifiers."""

        payload = _acceptance_projection_core_payload(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            code_task_version=code_task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
            intent_id=intent_id,
            language=language,
            framework=framework,
        )
        payload_hash = canonical_hash(payload)
        binding = _acceptance_evidence_binding_payload(
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            code_task_version=code_task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            output_query_trace_id=output_query_trace_id,
            verification_artifact_hashes=verification_artifact_hashes,
            execution_receipt_ids=execution_receipt_ids,
        )
        return cls(
            ingestion_key=payload_hash,
            payload_hash=payload_hash,
            acceptance_id=payload_hash,
            workflow_id=workflow_id,
            code_task_id=code_task_id,
            code_task_version=code_task_version,
            input_snapshot_id=input_snapshot_id,
            output_snapshot_id=output_snapshot_id,
            indexed_diff_hash=indexed_diff_hash,
            intent_id=intent_id,
            language=language,
            framework=framework,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            output_query_trace_id=output_query_trace_id,
            verification_artifact_hashes=verification_artifact_hashes,
            execution_receipt_ids=execution_receipt_ids,
            evidence_binding_hash=canonical_hash(binding),
        )


@dataclass(frozen=True, slots=True)
class AcceptedAtlasProjectionEvidence:
    """Trusted reader output kept separate from the public projection request."""

    code_task_version: int
    language: str
    framework: str
    checkpoint_hash: str
    indexed_diff_hash: str
    output_query_trace_id: str
    verification_artifact_hashes: tuple[str, ...]
    extraction_request: ExtractionRequest


@dataclass(frozen=True, slots=True)
class _PreparedAcceptedProjection:
    """Private, authority-derived input for one reader-free Atlas projection."""

    request: AcceptedAtlasProjectionRequest
    evidence: AcceptedAtlasProjectionEvidence
    extraction: ExtractionRequest


class AcceptanceEvidenceReader(Protocol):
    """Read verified checkpoint/index/receipt evidence for one accepted task."""

    def rebuild(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> AcceptedAtlasProjectionRequest:
        """Rebuild the internal request from the four public identifiers."""

        ...

    def read(
        self, request: AcceptedAtlasProjectionRequest
    ) -> AcceptedAtlasProjectionEvidence:
        """Return the typed evidence named by ``request`` without caller data."""

        ...


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _acceptance_identifier(value: object, *, allow_empty: bool = False) -> str:
    if isinstance(value, str) and not value and allow_empty:
        return value
    if not isinstance(value, str) or _ACCEPTANCE_IDENTIFIER.fullmatch(value) is None:
        raise AtlasError("invalid_acceptance_projection")
    return value


def _bounded_identifier_hashes(value: object, *, required: bool) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_ACCEPTANCE_EVIDENCE_IDS:
        raise AtlasError("invalid_acceptance_projection")
    if required and not value:
        raise AtlasError("invalid_acceptance_projection")
    if (
        any(not _is_hash(item) for item in value)
        or tuple(sorted(value)) != value
        or len(set(value)) != len(value)
    ):
        raise AtlasError("invalid_acceptance_projection")
    return value


def _acceptance_projection_core_payload(
    *,
    workflow_id: str,
    code_task_id: str,
    code_task_version: int,
    input_snapshot_id: str,
    output_snapshot_id: str,
    indexed_diff_hash: str,
    intent_id: str,
    language: str,
    framework: str,
) -> dict[str, object]:
    """Match the ATLAS-10A core acceptance hash exactly."""

    if (
        isinstance(code_task_version, bool)
        or not isinstance(code_task_version, int)
        or not 0 <= code_task_version <= 2**63 - 1
    ):
        raise AtlasError("invalid_acceptance_projection")
    if not _is_hash(indexed_diff_hash):
        raise AtlasError("invalid_acceptance_projection")
    return {
        "framework": _acceptance_identifier(framework, allow_empty=True),
        "indexed_diff_hash": indexed_diff_hash,
        "input_snapshot_id": _acceptance_identifier(input_snapshot_id),
        "intent_id": _acceptance_identifier(intent_id),
        "language": _acceptance_identifier(language),
        "output_snapshot_id": _acceptance_identifier(output_snapshot_id),
        "task_id": _acceptance_identifier(code_task_id),
        "task_kind": "code",
        "task_version": code_task_version,
        "workflow_id": _acceptance_identifier(workflow_id),
    }


def _acceptance_evidence_binding_payload(
    *,
    workflow_id: str,
    code_task_id: str,
    code_task_version: int,
    input_snapshot_id: str,
    output_snapshot_id: str,
    indexed_diff_hash: str,
    checkpoint_id: str,
    checkpoint_hash: str,
    output_query_trace_id: str,
    verification_artifact_hashes: tuple[str, ...],
    execution_receipt_ids: tuple[str, ...],
) -> dict[str, object]:
    """Return the frozen acceptance-evidence-binding/v1 canonical payload."""

    if (
        isinstance(code_task_version, bool)
        or not isinstance(code_task_version, int)
        or not 0 <= code_task_version <= 2**63 - 1
        or not _is_hash(indexed_diff_hash)
        or not _is_hash(checkpoint_hash)
    ):
        raise AtlasError("invalid_acceptance_projection")
    return {
        "schema_version": _EVIDENCE_BINDING_SCHEMA,
        "workflow_id": _acceptance_identifier(workflow_id),
        "code_task_id": _acceptance_identifier(code_task_id),
        "code_task_version": code_task_version,
        "input_snapshot_id": _acceptance_identifier(input_snapshot_id),
        "output_snapshot_id": _acceptance_identifier(output_snapshot_id),
        "indexed_diff_hash": indexed_diff_hash,
        "checkpoint_id": _acceptance_identifier(checkpoint_id),
        "checkpoint_hash": checkpoint_hash,
        "output_query_trace_id": _acceptance_identifier(output_query_trace_id),
        "verification_artifact_hashes": list(
            _bounded_identifier_hashes(verification_artifact_hashes, required=True)
        ),
        "execution_receipt_ids": list(
            _bounded_identifier_hashes(execution_receipt_ids, required=True)
        ),
    }


def _require_object(value: object, expected: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AtlasError("malformed_local_recipe")
    return value


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise AtlasError("malformed_local_recipe")
    return value


def _contains_body_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return True
            normalized = key.casefold()
            if (
                normalized in _BODY_KEYS
                or normalized.endswith(("_body", "_text"))
                or (normalized.startswith("content_") and normalized != "content_hash")
            ):
                return True
            if _contains_body_key(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_body_key(item) for item in value)
    return False


def _safe_payload(value: object) -> bool:
    if _contains_body_key(value):
        return False
    try:
        validate_fragment(canonical_json(value), max_bytes=MAX_PACKET_BYTES)
    except (AtlasError, TypeError, ValueError):
        return False
    return True


def _placeholder_names(value: str) -> frozenset[str]:
    """Parse complete non-nested ``${python_identifier}`` placeholders."""

    names: set[str] = set()
    cursor = 0
    while (start := value.find("${", cursor)) != -1:
        end = value.find("}", start + 2)
        if end == -1:
            raise AtlasError("malformed_local_recipe")
        name = value[start + 2 : end]
        if "${" in name or _PLACEHOLDER_NAME.fullmatch(name) is None:
            raise AtlasError("malformed_local_recipe")
        names.add(name)
        cursor = end + 1
        if cursor < len(value) and value[cursor] == "}":
            raise AtlasError("malformed_local_recipe")
    return frozenset(names)


def _strict_slots(value: object) -> tuple[SlotSpec, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_SLOT_COUNT:
        raise AtlasError("malformed_local_recipe")
    slots: list[SlotSpec] = []
    expected_path_index = 0
    expected_symbol_index = 0
    for item in value:
        data = _require_object(item, _SLOT_FIELDS)
        if (
            not isinstance(data["name"], str)
            or not data["name"]
            or not isinstance(data["type"], str)
            or data["type"] not in _LOCAL_SLOT_TYPES
            or data["required"] is not True
        ):
            raise AtlasError("malformed_local_recipe")
        match = (
            _PATH_SLOT_NAME.fullmatch(data["name"])
            if data["type"] == "relative_python_path"
            else _SYMBOL_SLOT_NAME.fullmatch(data["name"])
        )
        if match is None:
            raise AtlasError("malformed_local_recipe")
        if data["type"] == "relative_python_path":
            if int(match.group(1)) != expected_path_index:
                raise AtlasError("malformed_local_recipe")
            expected_path_index += 1
        else:
            if expected_path_index == 0 or int(match.group(1)) != expected_symbol_index:
                raise AtlasError("malformed_local_recipe")
            expected_symbol_index += 1
        slots.append(SlotSpec(data["name"], data["type"], data["required"]))
    if len({item.name for item in slots}) != len(slots) or not expected_path_index:
        raise AtlasError("malformed_local_recipe")
    return tuple(slots)


def _strict_constraints(
    value: object, slots: tuple[SlotSpec, ...]
) -> tuple[ConstraintSpec, ...]:
    if not isinstance(value, list) or len(value) != len(slots):
        raise AtlasError("malformed_local_recipe")
    constraints: list[ConstraintSpec] = []
    for slot, item in zip(slots, value, strict=True):
        data = _require_object(item, _CONSTRAINT_FIELDS)
        if (
            not isinstance(data["kind"], str)
            or data["kind"] not in _LOCAL_CONSTRAINT_KINDS
            or not isinstance(data["subject"], str)
            or data["subject"] != slot.name
            or not isinstance(data["value"], str)
            or not data["value"]
        ):
            raise AtlasError("malformed_local_recipe")
        if slot.type == "relative_python_path":
            if data["kind"] != "path_suffix" or data["value"] != ".py":
                raise AtlasError("malformed_local_recipe")
        else:
            if data["kind"] != "required_symbol":
                raise AtlasError("malformed_local_recipe")
            try:
                validate_slot_value("python_identifier", data["value"])
            except AtlasError as exc:
                raise AtlasError("malformed_local_recipe") from exc
        constraints.append(ConstraintSpec(data["kind"], data["subject"], data["value"]))
    return tuple(constraints)


def _strict_dependencies(value: object) -> tuple[DependencySpec, ...]:
    if not isinstance(value, list):
        raise AtlasError("malformed_local_recipe")
    dependencies: list[DependencySpec] = []
    for item in value:
        data = _require_object(item, _DEPENDENCY_FIELDS)
        if (
            not all(isinstance(data[key], str) for key in _DEPENDENCY_FIELDS)
            or not data["name"]
            or not data["kind"]
        ):
            raise AtlasError("malformed_local_recipe")
        dependencies.append(
            DependencySpec(data["name"], data["kind"], data["specifier"])
        )
    return tuple(dependencies)


def _strict_tests(value: object, slot_names: frozenset[str]) -> tuple[TestSpec, ...]:
    if not isinstance(value, list):
        raise AtlasError("malformed_local_recipe")
    tests: list[TestSpec] = []
    for item in value:
        data = _require_object(item, _TEST_FIELDS)
        if (
            not isinstance(data["argv"], list)
            or not data["argv"]
            or any(
                not isinstance(argument, str) or not argument
                for argument in data["argv"]
            )
            or type(data["expected_exit_code"]) is not int
        ):
            raise AtlasError("malformed_local_recipe")
        try:
            command_size = sum(
                len(argument.encode("utf-8")) for argument in data["argv"]
            )
        except UnicodeEncodeError as exc:
            raise AtlasError("malformed_local_recipe") from exc
        if command_size > MAX_COMMAND_SPEC_BYTES:
            raise AtlasError("malformed_local_recipe")
        if any(
            not _placeholder_names(argument) <= slot_names for argument in data["argv"]
        ):
            raise AtlasError("malformed_local_recipe")
        tests.append(TestSpec(tuple(data["argv"]), data["expected_exit_code"]))
    return tuple(tests)


def _strict_operations(
    value: object, slots_by_name: dict[str, SlotSpec]
) -> tuple[TemplateOperation, ...]:
    path_slots = tuple(
        slot.name
        for slot in slots_by_name.values()
        if slot.type == "relative_python_path"
    )
    if not isinstance(value, list) or len(value) != len(path_slots):
        raise AtlasError("malformed_local_recipe")
    operations: list[TemplateOperation] = []
    for expected_path_slot, item in zip(path_slots, value, strict=True):
        data = _require_object(item, _OPERATION_FIELDS)
        if not all(isinstance(data[key], str) for key in _OPERATION_FIELDS):
            raise AtlasError("malformed_local_recipe")
        if not _is_hash(data["template_hash"]):
            raise AtlasError("malformed_local_recipe")
        kind = data["kind"]
        path_slot = data["path_slot"]
        if (
            kind not in _LOCAL_OPERATION_KINDS
            or path_slot != expected_path_slot
            or path_slot not in slots_by_name
            or slots_by_name[path_slot].type != "relative_python_path"
        ):
            raise AtlasError("malformed_local_recipe")
        if data["separator"] or data["target_symbol_slot"]:
            raise AtlasError("malformed_local_recipe")
        operations.append(
            TemplateOperation(
                data["kind"],
                data["path_slot"],
                data["template_hash"],
                data["separator"],
                data["target_symbol_slot"],
            )
        )
    return tuple(operations)


def _observed_manifest_payload(manifest: RecipeManifest) -> dict[str, object]:
    return {
        "schema_version": "1",
        "recipe_key": manifest.recipe_key,
        "version": manifest.version,
        "intent_id": manifest.intent_id,
        "language": {
            "name": manifest.language_name,
            "extractor_version": manifest.language_extractor_version,
        },
        "framework": None,
        "repository_signature": manifest.repository_signature,
        "layer": manifest.layer,
        "slots": [slot.to_dict() for slot in manifest.slots],
        "constraints": [constraint.to_dict() for constraint in manifest.constraints],
        "dependencies": [dependency.to_dict() for dependency in manifest.dependencies],
        "tests": [test.to_dict() for test in manifest.tests],
        "operations": [operation.to_dict() for operation in manifest.operations],
        "provenance": {
            "kind": manifest.provenance_kind,
            "source": manifest.provenance_source,
        },
    }


def _sorted_records(records: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(sorted(records, key=lambda item: canonical_json(item.to_dict())))


def hydrate_local_manifest(root: AtlasNode) -> RecipeManifest:
    """Strictly hydrate one observed local recipe root without a second codec."""

    return AtlasService._hydrate_manifest(root)


class AtlasService:
    """Join immutable bundled records and bounded local Atlas graph records."""

    def __init__(
        self,
        store: AtlasStore,
        bundled_loader: BundledRecipeLoader,
        project_index: ProjectIndexService,
        *,
        acceptance_evidence_reader: AcceptanceEvidenceReader | None = None,
    ) -> None:
        self._store = store
        self._project_index = project_index
        self._acceptance_evidence_reader = acceptance_evidence_reader
        self._projection_extractor = PythonRecipeExtractor()
        self._bundled_loader = bundled_loader
        self._bundled_manifests = tuple(bundled_loader.load())
        self._bundled_by_id = {
            manifest.recipe_id: manifest for manifest in self._bundled_manifests
        }
        bundled_graph = bundled_loader.materialize()
        self._bundled_nodes = {
            node.node_id: node for node in bundled_graph.nodes if self._safe_node(node)
        }
        self._bundled_edges = {
            edge.edge_id: edge
            for edge in bundled_graph.edges
            if edge.source_id in self._bundled_nodes
            and edge.target_id in self._bundled_nodes
        }

    @property
    def store(self) -> AtlasStore:
        """Expose the durable store as a read-only service property."""
        return self._store

    @staticmethod
    def _validate_accepted_projection_request(
        request: object,
    ) -> AcceptedAtlasProjectionRequest:
        if type(request) is not AcceptedAtlasProjectionRequest:
            raise AtlasError("invalid_acceptance_projection")
        payload = _acceptance_projection_core_payload(
            workflow_id=request.workflow_id,
            code_task_id=request.code_task_id,
            code_task_version=request.code_task_version,
            input_snapshot_id=request.input_snapshot_id,
            output_snapshot_id=request.output_snapshot_id,
            indexed_diff_hash=request.indexed_diff_hash,
            intent_id=request.intent_id,
            language=request.language,
            framework=request.framework,
        )
        binding = _acceptance_evidence_binding_payload(
            workflow_id=request.workflow_id,
            code_task_id=request.code_task_id,
            code_task_version=request.code_task_version,
            input_snapshot_id=request.input_snapshot_id,
            output_snapshot_id=request.output_snapshot_id,
            indexed_diff_hash=request.indexed_diff_hash,
            checkpoint_id=request.checkpoint_id,
            checkpoint_hash=request.checkpoint_hash,
            output_query_trace_id=request.output_query_trace_id,
            verification_artifact_hashes=request.verification_artifact_hashes,
            execution_receipt_ids=request.execution_receipt_ids,
        )
        expected_payload_hash = canonical_hash(payload)
        if (
            not _is_hash(request.ingestion_key)
            or request.payload_hash != expected_payload_hash
            or request.acceptance_id != expected_payload_hash
            or request.evidence_binding_hash != canonical_hash(binding)
        ):
            raise AtlasError("invalid_acceptance_projection")
        return request

    @staticmethod
    def _require_canonical_core_key(request: AcceptedAtlasProjectionRequest) -> None:
        if request.ingestion_key != request.payload_hash:
            raise AtlasError("invalid_acceptance_projection")

    @staticmethod
    def _binding_provenance_node(
        request: AcceptedAtlasProjectionRequest,
    ) -> AtlasNode:
        return AtlasNode.create(
            NodeKind.SOURCE_EVIDENCE,
            {
                "kind": "acceptance_evidence_binding",
                "schema_version": _EVIDENCE_BINDING_SCHEMA,
                "evidence_binding_hash": request.evidence_binding_hash,
            },
            extractor_id="acceptance-projection",
            extractor_version="1",
            provenance="observed",
            source_hashes=(request.evidence_binding_hash,),
        )

    @staticmethod
    def _projection_from_receipt(
        request: AcceptedAtlasProjectionRequest,
        receipt: IngestionReceipt,
    ) -> AcceptanceProjection:
        return AcceptanceProjection(
            acceptance_id=request.acceptance_id,
            code_task_id=request.code_task_id,
            output_snapshot_id=request.output_snapshot_id,
            atlas_ingest_state=receipt.status,
            episode_id=receipt.episode_id,
            recipe_id=receipt.recipe_id,
            reasons=receipt.reasons,
        )

    def _existing_receipt_has_binding(
        self,
        request: AcceptedAtlasProjectionRequest,
        receipt: IngestionReceipt,
    ) -> bool:
        binding = self._binding_provenance_node(request)
        graph = self._store.graph_query(
            (receipt.episode_id,),
            max_nodes=MAX_GRAPH_NODES,
            max_edges=MAX_GRAPH_EDGES,
            max_depth=1,
            byte_budget=MAX_PACKET_BYTES,
            node_kinds=None,
            relations=None,
        )
        nodes = {node.node_id: node for node in graph.nodes}
        episode = nodes.get(receipt.episode_id)
        if episode is None or episode.kind is not NodeKind.TASK_EPISODE:
            return False
        if nodes.get(binding.node_id) != binding:
            return False
        edge = AtlasEdge.create(EdgeRelation.CHANGES, episode, binding)
        return any(item.edge_id == edge.edge_id for item in graph.edges)

    @staticmethod
    def _binding_scope(value: object) -> tuple[str, ...]:
        if type(value) is not tuple or not value or len(value) > MAX_CHANGED_FILES:
            raise AtlasError("acceptance_evidence_conflict")
        values: list[str] = []
        for item in value:
            try:
                values.append(validate_candidate_path(item))
            except AtlasError as exc:
                raise AtlasError("acceptance_evidence_conflict") from exc
        if tuple(sorted(values)) != tuple(values) or len(set(values)) != len(values):
            raise AtlasError("acceptance_evidence_conflict")
        if len({path_collision_key(item) for item in values}) != len(values):
            raise AtlasError("acceptance_evidence_conflict")
        return tuple(values)

    @classmethod
    def _validate_reader_evidence(
        cls,
        request: AcceptedAtlasProjectionRequest,
        evidence: object,
    ) -> ExtractionRequest:
        if type(evidence) is not AcceptedAtlasProjectionEvidence:
            raise AtlasError("acceptance_evidence_conflict")
        extraction = evidence.extraction_request
        if type(extraction) is not ExtractionRequest:
            raise AtlasError("acceptance_evidence_conflict")
        try:
            artifact_hashes = _bounded_identifier_hashes(
                evidence.verification_artifact_hashes, required=True
            )
            if (
                extraction.workflow_id != request.workflow_id
                or extraction.task_id != request.code_task_id
                or extraction.acceptance_id != request.acceptance_id
                or extraction.task_kind != "code"
                or extraction.intent_id != request.intent_id
                or evidence.code_task_version != request.code_task_version
                or evidence.language != request.language
                or evidence.framework != request.framework
                or not _is_hash(extraction.workspace_hash)
                or extraction.checkpoint_id != request.checkpoint_id
                or extraction.input_snapshot_id != request.input_snapshot_id
                or extraction.output_snapshot_id != request.output_snapshot_id
                or evidence.indexed_diff_hash != request.indexed_diff_hash
                or evidence.checkpoint_hash != request.checkpoint_hash
                or evidence.output_query_trace_id != request.output_query_trace_id
                or artifact_hashes != request.verification_artifact_hashes
            ):
                raise AtlasError("acceptance_evidence_conflict")
            cls._binding_scope(extraction.write_scope)
            if type(extraction.execution_receipts) is not tuple:
                raise AtlasError("acceptance_evidence_conflict")
            if any(
                type(item) is not BoundExecutionReceipt
                for item in extraction.execution_receipts
            ):
                raise AtlasError("acceptance_evidence_conflict")
            receipt_ids = tuple(
                item.receipt_id for item in extraction.execution_receipts
            )
            if receipt_ids != request.execution_receipt_ids:
                raise AtlasError("acceptance_evidence_conflict")
            for receipt in extraction.execution_receipts:
                if (
                    receipt.workflow_id != request.workflow_id
                    or receipt.task_id != request.code_task_id
                    or receipt.acceptance_id != request.acceptance_id
                    or receipt.workspace_hash != extraction.workspace_hash
                    or receipt.output_snapshot_id != request.output_snapshot_id
                ):
                    raise AtlasError("acceptance_evidence_conflict")
            actual_core = _acceptance_projection_core_payload(
                workflow_id=extraction.workflow_id,
                code_task_id=extraction.task_id,
                code_task_version=evidence.code_task_version,
                input_snapshot_id=extraction.input_snapshot_id,
                output_snapshot_id=extraction.output_snapshot_id,
                indexed_diff_hash=evidence.indexed_diff_hash,
                intent_id=extraction.intent_id,
                language=evidence.language,
                framework=evidence.framework,
            )
            if canonical_hash(actual_core) != request.payload_hash:
                raise AtlasError("acceptance_evidence_conflict")
            actual_binding = _acceptance_evidence_binding_payload(
                workflow_id=extraction.workflow_id,
                code_task_id=extraction.task_id,
                code_task_version=evidence.code_task_version,
                input_snapshot_id=extraction.input_snapshot_id,
                output_snapshot_id=extraction.output_snapshot_id,
                indexed_diff_hash=evidence.indexed_diff_hash,
                checkpoint_id=extraction.checkpoint_id,
                checkpoint_hash=evidence.checkpoint_hash,
                output_query_trace_id=evidence.output_query_trace_id,
                verification_artifact_hashes=artifact_hashes,
                execution_receipt_ids=receipt_ids,
            )
            if canonical_hash(actual_binding) != request.evidence_binding_hash:
                raise AtlasError("acceptance_evidence_conflict")
        except AtlasError as exc:
            if str(exc) == "acceptance_evidence_conflict":
                raise
            raise AtlasError("acceptance_evidence_conflict") from exc
        except (TypeError, ValueError, AttributeError) as exc:
            raise AtlasError("acceptance_evidence_conflict") from exc
        return extraction

    def _read_accepted_evidence(
        self, request: AcceptedAtlasProjectionRequest
    ) -> AcceptedAtlasProjectionEvidence:
        reader = self._acceptance_evidence_reader
        if reader is None:
            raise AtlasError("acceptance_evidence_unavailable")
        try:
            evidence = reader.read(request)
        except (AtlasError, StoreConflictError):
            raise
        except Exception as exc:
            raise AtlasError("acceptance_evidence_unavailable") from exc
        if type(evidence) is not AcceptedAtlasProjectionEvidence:
            raise AtlasError("acceptance_evidence_conflict")
        return evidence

    def _prepare_projection_from_request(
        self,
        request: AcceptedAtlasProjectionRequest,
        evidence: AcceptedAtlasProjectionEvidence,
    ) -> _PreparedAcceptedProjection:
        """Validate a reader result once, without re-entering the reader."""

        request = self._validate_accepted_projection_request(request)
        self._require_canonical_core_key(request)
        extraction_request = self._validate_reader_evidence(request, evidence)
        return _PreparedAcceptedProjection(request, evidence, extraction_request)

    def _prepare_accepted_projection(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> _PreparedAcceptedProjection:
        """Rebuild and read one authoritative acceptance exactly once each."""

        reader = self._acceptance_evidence_reader
        if reader is None:
            raise AtlasError("acceptance_evidence_unavailable")
        try:
            request = reader.rebuild(
                workflow_id,
                code_task_id,
                acceptance_id,
                ingestion_key,
            )
            evidence = self._read_accepted_evidence(request)
            return self._prepare_projection_from_request(request, evidence)
        except (AtlasError, StoreConflictError):
            raise
        except Exception as exc:
            raise AtlasError("acceptance_evidence_unavailable") from exc

    @staticmethod
    def _episode_records(
        nodes: tuple[AtlasNode, ...], edges: tuple[AtlasEdge, ...]
    ) -> tuple[tuple[AtlasNode, ...], tuple[AtlasEdge, ...]]:
        episode_nodes = tuple(
            node for node in nodes if node.kind not in _RECIPE_ONLY_KINDS
        )
        ids = {node.node_id for node in episode_nodes}
        return (
            episode_nodes,
            tuple(
                edge
                for edge in edges
                if edge.source_id in ids and edge.target_id in ids
            ),
        )

    @staticmethod
    def _recipe_link_ids(
        nodes: tuple[AtlasNode, ...], edges: tuple[AtlasEdge, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Link only reusable recipe topology, not acceptance-specific evidence."""

        node_ids = tuple(
            sorted(node.node_id for node in nodes if node.kind in _RECIPE_ONLY_KINDS)
        )
        linked = set(node_ids)
        edge_ids = tuple(
            sorted(
                edge.edge_id
                for edge in edges
                if edge.source_id in linked and edge.target_id in linked
            )
        )
        return node_ids, edge_ids

    def _with_binding_provenance(
        self,
        request: AcceptedAtlasProjectionRequest,
        *,
        episode_id: str,
        recipe_id: str | None,
        nodes: tuple[AtlasNode, ...],
        edges: tuple[AtlasEdge, ...],
    ) -> tuple[tuple[AtlasNode, ...], tuple[AtlasEdge, ...]]:
        by_id = {node.node_id: node for node in nodes}
        episode = by_id.get(episode_id)
        if episode is None or episode.kind is not NodeKind.TASK_EPISODE:
            raise AtlasError("projection_extraction_invalid")
        binding = self._binding_provenance_node(request)
        by_id[binding.node_id] = binding
        by_edge = {edge.edge_id: edge for edge in edges}
        binding_edge = AtlasEdge.create(EdgeRelation.CHANGES, episode, binding)
        by_edge[binding_edge.edge_id] = binding_edge
        if recipe_id is not None:
            recipe = by_id.get(recipe_id)
            if recipe is None or recipe.kind is not NodeKind.RECIPE:
                raise AtlasError("projection_extraction_invalid")
            provenance_edge = AtlasEdge.create(
                EdgeRelation.DERIVED_FROM, recipe, binding
            )
            by_edge[provenance_edge.edge_id] = provenance_edge
        return (
            tuple(sorted(by_id.values(), key=lambda node: node.node_id)),
            tuple(sorted(by_edge.values(), key=lambda edge: edge.edge_id)),
        )

    @staticmethod
    def _recipe_blobs(
        manifest: RecipeManifest,
        bindings: object,
        declared_hashes: object,
    ) -> tuple[tuple[str, bytes, str], ...]:
        try:
            value = thaw_json(bindings)
            if (
                type(value) is not dict
                or set(value) != {"slot_values", "template_text_by_hash"}
                or type(value["template_text_by_hash"]) is not dict
                or type(declared_hashes) is not tuple
            ):
                raise AtlasError("projection_extraction_invalid")
            templates = value["template_text_by_hash"]
            expected = tuple(
                sorted({operation.template_hash for operation in manifest.operations})
            )
            if (
                not expected
                or tuple(sorted(declared_hashes)) != expected
                or set(templates) != set(expected)
            ):
                raise AtlasError("projection_extraction_invalid")
            blobs: list[tuple[str, bytes, str]] = []
            for blob_hash in expected:
                text = templates[blob_hash]
                if not isinstance(text, str) or not _is_hash(blob_hash):
                    raise AtlasError("projection_extraction_invalid")
                content = text.encode("utf-8")
                validate_fragment(content, max_bytes=MAX_TEMPLATE_BYTES)
                if "sha256:" + hashlib.sha256(content).hexdigest() != blob_hash:
                    raise AtlasError("projection_extraction_invalid")
                blobs.append((blob_hash, content, "text/x-python"))
            return tuple(blobs)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AtlasError("projection_extraction_invalid") from exc

    @staticmethod
    def _episode_status_and_reasons(
        *,
        language: str,
        gaps: object,
    ) -> tuple[AtlasStatus, tuple[str, ...]]:
        if language != "python":
            return AtlasStatus.UNSUPPORTED_LANGUAGE, ("UNSUPPORTED_LANGUAGE",)
        try:
            codes = tuple(
                sorted(
                    {
                        gap.code
                        for gap in gaps
                        if isinstance(getattr(gap, "code", None), str)
                        and _GAP_CODE.fullmatch(gap.code) is not None
                    }
                )
            )
        except TypeError as exc:
            raise AtlasError("projection_extraction_invalid") from exc
        if not codes:
            return AtlasStatus.NO_VERIFIED_RECIPE, ("NO_VERIFIED_RECIPE",)
        if codes == ("UNSUPPORTED_LANGUAGE",):
            return AtlasStatus.UNSUPPORTED_LANGUAGE, codes
        return AtlasStatus.EVIDENCE_INCOMPLETE, codes

    def accept(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> AcceptanceProjection:
        """Project exactly one public Atlas acceptance through immutable evidence."""

        try:
            prepared = self._prepare_accepted_projection(
                workflow_id,
                code_task_id,
                acceptance_id,
                ingestion_key,
            )
            return self._project_prepared_acceptance(prepared)
        except AtlasError as error:
            if error.code in {
                "ATLAS_EVIDENCE_UNAVAILABLE",
                "ATLAS_EVIDENCE_CONFLICT",
            }:
                raise
            if error.code == "acceptance_evidence_unavailable":
                raise AtlasError("ATLAS_EVIDENCE_UNAVAILABLE") from error
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error
        except StoreConflictError as error:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error

    def project_acceptance(
        self, request: AcceptedAtlasProjectionRequest
    ) -> AcceptanceProjection:
        """Project one accepted code task through verified reader evidence only."""

        request = self._validate_accepted_projection_request(request)
        existing = self._store.get_ingestion_receipt(request.ingestion_key)
        if existing is not None:
            if existing.payload_hash != request.payload_hash:
                raise StoreConflictError("ingestion receipt conflict")
        self._require_canonical_core_key(request)
        prepared = self._prepare_projection_from_request(
            request, self._read_accepted_evidence(request)
        )
        return self._project_prepared_acceptance(prepared)

    def _project_prepared_acceptance(
        self, prepared: _PreparedAcceptedProjection
    ) -> AcceptanceProjection:
        """Project a private prepared input without any evidence-reader access."""

        if type(prepared) is not _PreparedAcceptedProjection:
            raise AtlasError("invalid_acceptance_projection")
        request = self._validate_accepted_projection_request(prepared.request)
        self._require_canonical_core_key(request)
        extraction_request = self._validate_reader_evidence(request, prepared.evidence)
        if extraction_request != prepared.extraction:
            raise AtlasError("acceptance_evidence_conflict")
        existing = self._store.get_ingestion_receipt(request.ingestion_key)
        if existing is not None:
            if existing.payload_hash != request.payload_hash:
                raise StoreConflictError("ingestion receipt conflict")
        if existing is not None:
            if not self._existing_receipt_has_binding(request, existing):
                raise StoreConflictError("ingestion evidence binding conflict")
            return self._projection_from_receipt(request, existing)
        result = self._projection_extractor.extract(extraction_request)
        if result.eligible and request.language == "python":
            manifest = result.manifest
            if type(manifest) is not RecipeManifest:
                raise AtlasError("projection_extraction_invalid")
            nodes, edges = self._with_binding_provenance(
                request,
                episode_id=result.episode_id,
                recipe_id=manifest.recipe_id,
                nodes=tuple(result.nodes),
                edges=tuple(result.edges),
            )
            blobs = self._recipe_blobs(manifest, result.original_bindings, result.blobs)
            recipe_node_ids, recipe_edge_ids = self._recipe_link_ids(nodes, edges)
            receipt = IngestionReceipt(
                request.ingestion_key,
                request.payload_hash,
                AtlasStatus.READY,
                result.episode_id,
                manifest.recipe_id,
            )
            self._store.put_ingestion_bundle(
                nodes=nodes,
                edges=edges,
                manifest=manifest,
                recipe_node_ids=recipe_node_ids,
                recipe_edge_ids=recipe_edge_ids,
                blobs=blobs,
                receipt=receipt,
            )
            return self._projection_from_receipt(request, receipt)

        episode_nodes, episode_edges = self._episode_records(
            tuple(result.nodes), tuple(result.edges)
        )
        nodes, edges = self._with_binding_provenance(
            request,
            episode_id=result.episode_id,
            recipe_id=None,
            nodes=episode_nodes,
            edges=episode_edges,
        )
        status, reasons = self._episode_status_and_reasons(
            language=request.language,
            gaps=result.gaps,
        )
        receipt = IngestionReceipt(
            request.ingestion_key,
            request.payload_hash,
            status,
            result.episode_id,
            reasons=reasons,
        )
        self._store.put_ingestion_bundle(
            nodes=nodes,
            edges=edges,
            manifest=None,
            recipe_node_ids=(),
            recipe_edge_ids=(),
            blobs=(),
            receipt=receipt,
        )
        return self._projection_from_receipt(request, receipt)

    @staticmethod
    def _safe_node(node: AtlasNode) -> bool:
        if not _safe_payload(node.to_dict()):
            return False
        payload = thaw_json(node.payload)
        if not _safe_payload(payload):
            return False
        if node.kind is not NodeKind.CODE_TEMPLATE:
            return True
        return (
            isinstance(payload, dict)
            and set(payload) == {"template_hash", "kind"}
            and _is_hash(payload.get("template_hash"))
            and isinstance(payload.get("kind"), str)
            and bool(payload["kind"])
        )

    @staticmethod
    def _safe_edge(edge: AtlasEdge) -> bool:
        return _safe_payload(edge.to_dict())

    @staticmethod
    def _validate_budget(value: object, maximum: int, code: str) -> int:
        if type(value) is not int or not 0 < value <= maximum:
            raise AtlasError(code)
        return value

    @staticmethod
    def _validate_ids(value: Iterable[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise AtlasError("invalid_root_id")
        try:
            values = tuple(value)
        except TypeError as exc:
            raise AtlasError("invalid_root_id") from exc
        if any(not _is_hash(item) for item in values):
            raise AtlasError("invalid_root_id")
        return tuple(sorted(set(values)))

    @staticmethod
    def _validate_node_kinds(
        value: Iterable[NodeKind | str] | None,
    ) -> frozenset[NodeKind] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise AtlasError("invalid_node_kind")
        resolved: set[NodeKind] = set()
        try:
            values = tuple(value)
        except TypeError as exc:
            raise AtlasError("invalid_node_kind") from exc
        for item in values:
            if isinstance(item, NodeKind):
                resolved.add(item)
            elif isinstance(item, str):
                try:
                    resolved.add(NodeKind(item))
                except ValueError as exc:
                    raise AtlasError("invalid_node_kind") from exc
            else:
                raise AtlasError("invalid_node_kind")
        return frozenset(resolved)

    @staticmethod
    def _validate_relations(
        value: Iterable[EdgeRelation | str] | None,
    ) -> frozenset[EdgeRelation] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise AtlasError("invalid_relation")
        resolved: set[EdgeRelation] = set()
        try:
            values = tuple(value)
        except TypeError as exc:
            raise AtlasError("invalid_relation") from exc
        for item in values:
            if isinstance(item, EdgeRelation):
                resolved.add(item)
            elif isinstance(item, str):
                try:
                    resolved.add(EdgeRelation(item))
                except ValueError as exc:
                    raise AtlasError("invalid_relation") from exc
            else:
                raise AtlasError("invalid_relation")
        return frozenset(resolved)

    def _intent_roots(self, intent_id: str) -> tuple[str, ...]:
        normalized = normalize_intent_id(intent_id)
        if not normalized:
            raise AtlasError("invalid_intent_id")
        bundled = (
            recipe.recipe_id
            for recipe in self._bundled_manifests
            if normalize_intent_id(recipe.intent_id) == normalized
        )
        local = self._store.recipes_for_intent(normalized, limit=_LOCAL_DISCOVERY_LIMIT)
        if len(local) >= _LOCAL_DISCOVERY_LIMIT:
            raise AtlasError("too_many_roots")
        return tuple(sorted(set((*bundled, *local))))

    def _merged_graph(
        self, roots: tuple[str, ...]
    ) -> tuple[dict[str, AtlasNode], dict[str, AtlasEdge], bool]:
        local = self._store.graph_query(
            roots,
            max_nodes=MAX_GRAPH_NODES,
            max_edges=MAX_GRAPH_EDGES,
            max_depth=MAX_GRAPH_DEPTH,
            byte_budget=MAX_PACKET_BYTES,
            node_kinds=None,
            relations=None,
        )
        nodes: dict[str, AtlasNode] = {}
        truncated = local.truncated
        for node in local.nodes:
            if self._safe_node(node):
                nodes.setdefault(node.node_id, node)
            else:
                truncated = True
        for node_id, node in self._bundled_nodes.items():
            nodes.setdefault(node_id, node)
        edges: dict[str, AtlasEdge] = {}
        for edge in (*local.edges, *self._bundled_edges.values()):
            if (
                not self._safe_edge(edge)
                or edge.source_id not in nodes
                or edge.target_id not in nodes
            ):
                truncated = True
                continue
            edges.setdefault(edge.edge_id, edge)
        return nodes, edges, truncated

    @staticmethod
    def _bounded_bfs(
        nodes_by_id: dict[str, AtlasNode],
        edges_by_id: dict[str, AtlasEdge],
        *,
        roots: tuple[str, ...],
        node_kinds: frozenset[NodeKind] | None,
        relations: frozenset[EdgeRelation] | None,
        max_nodes: int,
        max_edges: int,
        max_depth: int,
        byte_budget: int,
        inherited_truncated: bool,
    ) -> GraphQueryResult:
        roots = tuple(root for root in roots if root in nodes_by_id)
        if not roots:
            return GraphQueryResult((), (), inherited_truncated)
        adjacency: dict[str, list[AtlasEdge]] = {}
        for edge in edges_by_id.values():
            adjacency.setdefault(edge.source_id, []).append(edge)
            adjacency.setdefault(edge.target_id, []).append(edge)
        for values in adjacency.values():
            values.sort(key=lambda item: item.edge_id)

        chosen: set[str] = set()
        frontier: list[str] = []
        truncated = inherited_truncated
        root_set = set(roots)
        for root in roots:
            if len(chosen) >= max_nodes:
                truncated = True
                continue
            chosen.add(root)
            frontier.append(root)
        selected_edges: set[str] = set()
        for _depth in range(max_depth):
            next_frontier: set[str] = set()
            for node_id in sorted(frontier):
                for edge in adjacency.get(node_id, ()):
                    if relations is not None and edge.relation not in relations:
                        continue
                    other = (
                        edge.target_id if edge.source_id == node_id else edge.source_id
                    )
                    if other not in nodes_by_id:
                        truncated = True
                        continue
                    if (
                        other not in root_set
                        and node_kinds is not None
                        and nodes_by_id[other].kind not in node_kinds
                    ):
                        continue
                    if other not in chosen and len(chosen) >= max_nodes:
                        truncated = True
                        continue
                    if edge.edge_id not in selected_edges:
                        if len(selected_edges) >= max_edges:
                            truncated = True
                            continue
                        selected_edges.add(edge.edge_id)
                    if other not in chosen:
                        chosen.add(other)
                        next_frontier.add(other)
            frontier = sorted(next_frontier)

        for node_id in frontier:
            for edge in adjacency.get(node_id, ()):
                if relations is not None and edge.relation not in relations:
                    continue
                other = edge.target_id if edge.source_id == node_id else edge.source_id
                if other not in nodes_by_id:
                    truncated = True
                    continue
                if (
                    other not in root_set
                    and node_kinds is not None
                    and nodes_by_id[other].kind not in node_kinds
                ):
                    continue
                if edge.edge_id not in selected_edges or other not in chosen:
                    truncated = True

        nodes = tuple(nodes_by_id[node_id] for node_id in sorted(chosen))
        edges = tuple(
            edges_by_id[edge_id]
            for edge_id in sorted(selected_edges)
            if edges_by_id[edge_id].source_id in chosen
            and edges_by_id[edge_id].target_id in chosen
        )
        while nodes or edges:
            result = GraphQueryResult(nodes, edges, truncated)
            if len(canonical_json(result.to_dict()).encode("utf-8")) <= byte_budget:
                return result
            truncated = True
            if edges:
                edges = edges[:-1]
                continue
            removable = [node for node in nodes if node.node_id not in root_set]
            if not removable:
                break
            remove_id = removable[-1].node_id
            nodes = tuple(node for node in nodes if node.node_id != remove_id)
            remaining = {node.node_id for node in nodes}
            edges = tuple(
                edge
                for edge in edges
                if edge.source_id in remaining and edge.target_id in remaining
            )
        return GraphQueryResult((), (), True)

    def graph_query(
        self,
        roots: Iterable[str] | None = None,
        *,
        root_node_ids: Iterable[str] | None = None,
        node_kinds: Iterable[NodeKind | str] | None = None,
        kinds: Iterable[NodeKind | str] | None = None,
        relations: Iterable[EdgeRelation | str] | None = None,
        intent_id: str | None = None,
        max_nodes: int = 50,
        max_edges: int = 100,
        max_depth: int = 1,
        byte_budget: int = 65_536,
    ) -> GraphQueryResult:
        """Return one stable bounded traversal over local and bundled records."""

        max_nodes = self._validate_budget(
            max_nodes, MAX_GRAPH_NODES, "invalid_graph_budget"
        )
        max_edges = self._validate_budget(
            max_edges, MAX_GRAPH_EDGES, "invalid_graph_budget"
        )
        max_depth = self._validate_budget(
            max_depth, MAX_GRAPH_DEPTH, "invalid_graph_budget"
        )
        byte_budget = self._validate_budget(
            byte_budget, MAX_PACKET_BYTES, "invalid_graph_budget"
        )
        explicit = set(self._validate_ids(roots))
        explicit.update(self._validate_ids(root_node_ids))
        if intent_id not in (None, ""):
            if not isinstance(intent_id, str):
                raise AtlasError("invalid_intent_id")
            explicit.update(self._intent_roots(intent_id))
        elif intent_id is not None and not isinstance(intent_id, str):
            raise AtlasError("invalid_intent_id")
        if len(explicit) > MAX_GRAPH_NODES:
            raise AtlasError("too_many_roots")
        if node_kinds is not None and kinds is not None:
            raise AtlasError("invalid_node_kind")
        kind_filter = self._validate_node_kinds(node_kinds if kinds is None else kinds)
        relation_filter = self._validate_relations(relations)
        if not explicit:
            return GraphQueryResult()
        root_ids = tuple(sorted(explicit))
        nodes, edges, source_truncated = self._merged_graph(root_ids)
        return self._bounded_bfs(
            nodes,
            edges,
            roots=root_ids,
            node_kinds=kind_filter,
            relations=relation_filter,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_depth=max_depth,
            byte_budget=byte_budget,
            inherited_truncated=source_truncated,
        )

    @staticmethod
    def _hydrate_manifest(root: AtlasNode) -> RecipeManifest:
        if (
            root.kind is not NodeKind.RECIPE
            or root.schema_version != "1"
            or root.extractor_id != "python-ast"
            or root.extractor_version != "1"
            or root.provenance != "observed"
            or root.created_at is not None
            or root.superseded_at is not None
            or root.quarantine_state not in _LOCAL_QUARANTINE_STATES
        ):
            raise AtlasError("malformed_local_recipe")
        payload = thaw_json(root.payload)
        if not _safe_payload(payload):
            raise AtlasError("malformed_local_recipe")
        data = _require_object(payload, _MANIFEST_FIELDS)
        if (
            type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["recipe_key"], str)
            or not isinstance(data["intent_id"], str)
            or normalize_intent_id(data["intent_id"]) != data["intent_id"]
            or data["recipe_key"] != data["intent_id"]
            or data["language_name"] != "python"
            or data["language_extractor_version"] != "1"
            or not _is_hash(data["repository_signature"])
            or data["layer"] != "local"
            or not _is_hash(data["manifest_hash"])
            or data["framework_name"] is not None
            or data["framework_specifier"] is not None
            or data["provenance_kind"] != "observed"
            or data["provenance_source"] != "accepted_task"
            or data["schema_version"] != "1"
            or data["quarantine_state"] not in _LOCAL_QUARANTINE_STATES
            or root.quarantine_state != data["quarantine_state"]
            or not isinstance(data["superseded_ids"], list)
            or len(data["superseded_ids"]) > _MAX_LOCAL_SUPERSEDED_IDS
            or any(not _is_hash(recipe_id) for recipe_id in data["superseded_ids"])
            or len(set(data["superseded_ids"])) != len(data["superseded_ids"])
            or data["superseded_ids"] != sorted(data["superseded_ids"])
        ):
            raise AtlasError("malformed_local_recipe")
        slots = _strict_slots(data["slots"])
        slots_by_name = {slot.name: slot for slot in slots}
        slot_names = frozenset(slots_by_name)
        constraints = _strict_constraints(data["constraints"], slots)
        dependencies = _strict_dependencies(data["dependencies"])
        tests = _strict_tests(data["tests"], slot_names)
        operations = _strict_operations(data["operations"], slots_by_name)
        if dependencies or tests:
            raise AtlasError("malformed_local_recipe")
        manifest = RecipeManifest(
            recipe_id=root.node_id,
            recipe_key=data["recipe_key"],
            version=data["version"],
            intent_id=data["intent_id"],
            language_name=data["language_name"],
            language_extractor_version=data["language_extractor_version"],
            repository_signature=data["repository_signature"],
            layer=data["layer"],
            manifest_hash=data["manifest_hash"],
            framework_name=data["framework_name"],
            framework_specifier=data["framework_specifier"],
            slots=slots,
            constraints=constraints,
            dependencies=dependencies,
            tests=tests,
            operations=operations,
            provenance_kind=data["provenance_kind"],
            provenance_source=data["provenance_source"],
            schema_version=data["schema_version"],
            superseded_ids=tuple(data["superseded_ids"]),
            quarantine_state=data["quarantine_state"],
        )
        rebuilt = manifest.to_dict()
        del rebuilt["recipe_id"]
        if (
            canonical_json(payload) != canonical_json(rebuilt)
            or canonical_hash(_observed_manifest_payload(manifest))
            != manifest.manifest_hash
            or root.source_hashes != (manifest.manifest_hash,)
        ):
            raise AtlasError("malformed_local_recipe")
        return manifest

    def _local_manifests(
        self, intent_id: str
    ) -> tuple[tuple[RecipeManifest, ...], bool, bool]:
        manifests: list[RecipeManifest] = []
        malformed = False
        recipe_ids = self._store.recipes_for_intent(
            intent_id, limit=_LOCAL_DISCOVERY_LIMIT
        )
        if len(recipe_ids) >= _LOCAL_DISCOVERY_LIMIT:
            return (), False, True
        for recipe_id in recipe_ids:
            result = self._store.graph_query(
                (recipe_id,),
                max_nodes=1,
                max_edges=MAX_GRAPH_EDGES,
                max_depth=MAX_GRAPH_DEPTH,
                byte_budget=MAX_PACKET_BYTES,
                node_kinds=None,
                relations=(),
            )
            if (
                result.truncated
                or len(result.nodes) != 1
                or result.nodes[0].node_id != recipe_id
                or result.nodes[0].kind is not NodeKind.RECIPE
            ):
                malformed = True
                continue
            try:
                manifests.append(hydrate_local_manifest(result.nodes[0]))
            except AtlasError:
                malformed = True
        return (
            tuple(sorted(manifests, key=lambda item: item.recipe_id)),
            malformed,
            False,
        )

    @staticmethod
    def _prepare_paths(value: Sequence[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise AtlasError("invalid_target_path")
        try:
            raw_paths = tuple(value)
        except TypeError as exc:
            raise AtlasError("invalid_target_path") from exc
        if len(raw_paths) > MAX_CHANGED_FILES:
            raise AtlasError("too_many_target_paths")
        normalized = tuple(validate_candidate_path(path) for path in raw_paths)
        if len(set(normalized)) != len(normalized):
            raise AtlasError("duplicate_target_path")
        collision_keys = tuple(path_collision_key(path) for path in normalized)
        if len(set(collision_keys)) != len(collision_keys):
            raise AtlasError("path_case_collision")
        return tuple(sorted(normalized))

    @staticmethod
    def _validate_workspace_paths(
        paths: tuple[str, ...], workspace_root: Path
    ) -> tuple[str, ...]:
        return tuple(validate_candidate_path(path, workspace_root) for path in paths)

    @staticmethod
    def _prepare_symbols(value: Sequence[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise AtlasError("invalid_target_symbol")
        try:
            raw_symbols = tuple(value)
        except TypeError as exc:
            raise AtlasError("invalid_target_symbol") from exc
        if len(raw_symbols) > MAX_SLOT_COUNT:
            raise AtlasError("too_many_target_symbols")
        normalized: list[str] = []
        for symbol in raw_symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                raise AtlasError("invalid_target_symbol")
            candidate = symbol.strip()
            try:
                normalized.append(
                    validate_slot_value("python_qualified_name", candidate)
                )
            except AtlasError as exc:
                raise AtlasError("invalid_target_symbol") from exc
        if len(set(normalized)) != len(normalized):
            raise AtlasError("duplicate_target_symbol")
        return tuple(sorted(normalized))

    @staticmethod
    def _index_failure(error: IndexError) -> PreparationResult:
        if error.code in {"INDEX_STALE", "NOT_FOUND", "INDEX_UNAVAILABLE"}:
            return PreparationResult(AtlasStatus.INDEX_STALE, reasons=("index_stale",))
        return PreparationResult(
            AtlasStatus.EVIDENCE_INCOMPLETE,
            reasons=(error.code.casefold(),),
        )

    @staticmethod
    def _evidence_descriptors(
        query_result: Any, file_hashes: dict[str, str], target_paths: tuple[str, ...]
    ) -> tuple[dict[str, object], ...]:
        descriptors: list[dict[str, object]] = [
            {
                "path": window.path,
                "start_line": window.start_line,
                "end_line": window.end_line,
                "content_hash": window.content_hash,
            }
            for window in query_result.source_windows
        ]
        descriptors.extend(
            {"path": path, "content_hash": file_hashes[path]} for path in target_paths
        )
        unique = {canonical_json(item): item for item in descriptors}
        return tuple(unique[key] for key in sorted(unique))

    @staticmethod
    def _query_text(
        intent_id: str, target_paths: tuple[str, ...], target_symbols: tuple[str, ...]
    ) -> str:
        path_stems = tuple(sorted(PurePosixPath(path).stem for path in target_paths))
        intent_terms = tuple(part for part in intent_id.split(".") if part)
        return " ".join((*target_symbols, *path_stems, *intent_terms))

    @staticmethod
    def _prepare_request_hash(
        *,
        intent_id: str,
        language: str,
        framework: str | None,
        target_paths: tuple[str, ...],
        target_symbols: tuple[str, ...],
        max_candidates: int,
        byte_budget: int,
    ) -> str:
        return canonical_hash(
            {
                "schema": _PREPARE_REQUEST_SCHEMA,
                "intent_id": intent_id,
                "language": language,
                "framework": "" if framework is None else framework,
                "target_paths": target_paths,
                "target_symbols": target_symbols,
                "max_candidates": max_candidates,
                "byte_budget": byte_budget,
            }
        )

    @staticmethod
    def _gap_codes(facts: SnapshotFacts, query: Any) -> tuple[str, ...]:
        codes = {
            gap.code.strip().upper()
            for gap in (*facts.gaps, *query.gaps)
            if isinstance(gap.code, str)
            and _GAP_CODE.fullmatch(gap.code.strip().upper()) is not None
        }
        return tuple(sorted(codes))

    def prepare(
        self,
        *,
        workspace_id: str,
        snapshot_id: str,
        intent_id: str,
        language: str,
        framework: str | None = None,
        target_paths: Sequence[str] | None = (),
        target_symbols: Sequence[str] | None = (),
        max_candidates: int = 20,
        byte_budget: int = 131_072,
    ) -> PreparationResult:
        """Prepare one bounded immutable packet without changing a workspace."""

        if type(max_candidates) is not int or not 0 < max_candidates <= MAX_GRAPH_NODES:
            raise AtlasError("invalid_max_candidates")
        byte_budget = self._validate_budget(
            byte_budget, MAX_PACKET_BYTES, "invalid_byte_budget"
        )
        if not is_workspace_id(workspace_id) or not _is_hash(snapshot_id):
            raise AtlasError("invalid_request")
        if not isinstance(intent_id, str):
            raise AtlasError("invalid_intent_id")
        normalized_intent = normalize_intent_id(intent_id)
        if not normalized_intent:
            raise AtlasError("invalid_intent_id")
        if not isinstance(language, str):
            raise AtlasError("invalid_language")
        normalized_language = language.strip().casefold()
        normalized_framework = normalize_framework(framework)
        target_paths = self._prepare_paths(target_paths)
        target_symbols = self._prepare_symbols(target_symbols)
        try:
            self._project_index.assert_current(workspace_id, snapshot_id)
            facts = self._project_index.snapshot_facts(workspace_id, snapshot_id)
            workspace_root = self._project_index.workspace_authority.resolve(
                workspace_id
            ).root
        except IndexError as exc:
            return self._index_failure(exc)
        target_paths = self._validate_workspace_paths(target_paths, workspace_root)
        if normalized_language != "python":
            return PreparationResult(
                AtlasStatus.UNSUPPORTED_LANGUAGE,
                reasons=("unsupported_language",),
            )
        file_hashes = dict(facts.file_hashes)
        if any(path not in file_hashes for path in target_paths):
            return PreparationResult(
                AtlasStatus.EVIDENCE_INCOMPLETE,
                reasons=("missing_target_path",),
            )
        symbols = {node.name for node in facts.nodes}
        symbols.update(
            node.qualified_name for node in facts.nodes if node.qualified_name
        )
        if any(symbol not in symbols for symbol in target_symbols):
            return PreparationResult(
                AtlasStatus.EVIDENCE_INCOMPLETE,
                reasons=("missing_target_symbol",),
            )
        repository_signature = structural_repository_signature(
            facts, language=normalized_language, framework=normalized_framework
        )
        local, malformed_local, discovery_limited = self._local_manifests(
            normalized_intent
        )
        if discovery_limited:
            return PreparationResult(
                AtlasStatus.AMBIGUOUS_MATCH,
                reasons=("candidate_limit_exceeded",),
            )
        matching = select_recipe(
            (*self._bundled_manifests, *local),
            intent_id=normalized_intent,
            language=normalized_language,
            framework=normalized_framework,
            repository_signature=repository_signature,
            snapshot_facts=facts,
            max_candidates=max_candidates,
        )
        candidate_ids = tuple(item.manifest.recipe_id for item in matching.candidates)
        reasons = matching.reason_codes
        if malformed_local:
            reasons = tuple(sorted(set((*reasons, "malformed_local_recipe"))))
        if matching.status is not AtlasStatus.READY or matching.winner is None:
            return PreparationResult(
                matching.status, candidate_recipe_ids=candidate_ids, reasons=reasons
            )
        winner: MatchCandidate = matching.winner
        try:
            query = self._project_index.query(
                workspace_id,
                snapshot_id,
                self._query_text(normalized_intent, target_paths, target_symbols),
                mode="lexical",
                max_nodes=MAX_GRAPH_NODES,
                max_depth=1,
                source_lines=12,
                byte_budget=min(byte_budget, MAX_PACKET_BYTES),
            )
            receipt_hash = canonical_hash(
                asdict(self._project_index.query_receipt(query.trace_id))
            )
        except IndexError as exc:
            return self._index_failure(exc)
        if query.truncated:
            return PreparationResult(
                AtlasStatus.EVIDENCE_INCOMPLETE,
                candidate_recipe_ids=candidate_ids,
                reasons=("project_query_truncated",),
            )
        graph = self.graph_query(
            roots=(winner.manifest.recipe_id,),
            max_nodes=MAX_GRAPH_NODES,
            max_edges=MAX_GRAPH_EDGES,
            max_depth=MAX_GRAPH_DEPTH,
            byte_budget=MAX_PACKET_BYTES,
        )
        if graph.truncated:
            return PreparationResult(
                AtlasStatus.EVIDENCE_INCOMPLETE,
                candidate_recipe_ids=candidate_ids,
                reasons=("recipe_graph_truncated",),
            )
        evidence = self._evidence_descriptors(query, file_hashes, target_paths)
        evidence_hashes = tuple(
            sorted(
                {
                    winner.manifest.manifest_hash,
                    *(
                        window["content_hash"]
                        for window in evidence
                        if "content_hash" in window
                    ),
                }
            )
        )
        source_hashes = tuple(sorted({file_hashes[path] for path in target_paths}))
        template_hashes = tuple(
            sorted(
                {operation.template_hash for operation in winner.manifest.operations}
            )
        )
        gaps = self._gap_codes(facts, query)
        operations = _sorted_records(winner.manifest.operations)
        slots = tuple(sorted(winner.manifest.slots, key=lambda item: item.name))
        constraints = _sorted_records(winner.manifest.constraints)
        dependencies = _sorted_records(winner.manifest.dependencies)
        tests = _sorted_records(winner.manifest.tests)
        request_hash = self._prepare_request_hash(
            intent_id=normalized_intent,
            language=normalized_language,
            framework=normalized_framework,
            target_paths=target_paths,
            target_symbols=target_symbols,
            max_candidates=max_candidates,
            byte_budget=byte_budget,
        )
        trace_id = canonical_hash(
            {
                "workspace_id": workspace_id,
                "intent_id": normalized_intent,
                "language": normalized_language,
                "framework": ""
                if normalized_framework is None
                else normalized_framework,
                "target_paths": target_paths,
                "target_symbols": target_symbols,
                "snapshot_id": snapshot_id,
                "repository_signature": repository_signature,
                "match_class": int(winner.match_class),
                "recipe_id": winner.manifest.recipe_id,
                "manifest_hash": winner.manifest.manifest_hash,
                "project_query_trace_id": query.trace_id,
                "evidence": evidence,
                "node_ids": tuple(node.node_id for node in graph.nodes),
                "edge_ids": tuple(edge.edge_id for edge in graph.edges),
                "max_candidates": max_candidates,
                "byte_budget": byte_budget,
                "matcher_version": ATLAS_MATCHER_VERSION,
                "request_hash": request_hash,
            }
        )
        provisional = ImplementationPacket(
            packet_id="",
            trace_id=trace_id,
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            recipe_id=winner.manifest.recipe_id,
            node_ids=tuple(node.node_id for node in graph.nodes),
            edge_ids=tuple(edge.edge_id for edge in graph.edges),
            evidence_windows=evidence,
            evidence_hashes=evidence_hashes,
            operations=operations,
            slots=slots,
            constraints=constraints,
            dependencies=dependencies,
            tests=tests,
            gaps=gaps,
            source_hashes=source_hashes,
            template_hashes=template_hashes,
            receipt_hashes=(receipt_hash,),
            next_action="atlas_render",
            request_hash=request_hash,
            matcher_version=ATLAS_MATCHER_VERSION,
            target_paths=target_paths,
            target_symbols=target_symbols,
        )
        packet_data = provisional.to_dict()
        del packet_data["packet_id"]
        packet = replace(provisional, packet_id=canonical_hash(packet_data))
        try:
            self._project_index.assert_current(workspace_id, snapshot_id)
        except IndexError as exc:
            return self._index_failure(exc)
        if len(canonical_json(packet.to_dict()).encode("utf-8")) > byte_budget:
            return PreparationResult(
                AtlasStatus.EVIDENCE_INCOMPLETE,
                candidate_recipe_ids=candidate_ids,
                reasons=("packet_byte_budget_exceeded",),
            )
        try:
            self._store.put_packet(packet)
            if self._store.get_packet(packet.packet_id) != packet:
                return PreparationResult(
                    AtlasStatus.ATLAS_UNAVAILABLE,
                    candidate_recipe_ids=candidate_ids,
                    reasons=("packet_readback_mismatch",),
                )
        except StoreConflictError:
            return PreparationResult(
                AtlasStatus.ATLAS_UNAVAILABLE,
                candidate_recipe_ids=candidate_ids,
                reasons=("packet_store_conflict",),
            )
        return PreparationResult(
            AtlasStatus.READY,
            packet=packet,
            candidate_recipe_ids=candidate_ids,
            reasons=reasons,
        )

    @staticmethod
    def _render_invalid(packet_id: object, *codes: str) -> RenderResult:
        safe_packet_id = packet_id if _is_hash(packet_id) else ""
        normalized = {
            code if code in _RENDER_REASON_CODES else "packet_recipe_mismatch"
            for code in codes
            if code
        }
        return RenderResult(
            AtlasStatus.RENDER_INVALID,
            safe_packet_id,
            reasons=tuple(sorted(normalized)),
        )

    def _reload_render_manifest(self, recipe_id: str) -> RecipeManifest:
        bundled = self._bundled_by_id.get(recipe_id)
        if bundled is not None:
            return bundled
        graph = self._store.graph_query(
            (recipe_id,),
            max_nodes=1,
            max_edges=MAX_GRAPH_EDGES,
            max_depth=MAX_GRAPH_DEPTH,
            byte_budget=MAX_PACKET_BYTES,
            node_kinds=None,
            relations=(),
        )
        if (
            graph.truncated
            or len(graph.nodes) != 1
            or graph.nodes[0].node_id != recipe_id
            or graph.nodes[0].kind is not NodeKind.RECIPE
        ):
            raise AtlasError("recipe_unavailable")
        try:
            manifest = hydrate_local_manifest(graph.nodes[0])
        except AtlasError as exc:
            raise AtlasError("recipe_unavailable") from exc
        if manifest.quarantine_state not in (None, "", "ready", "READY"):
            raise AtlasError("recipe_quarantined")
        return manifest

    def _verify_render_packet_recipe(
        self, packet: ImplementationPacket, manifest: RecipeManifest
    ) -> None:
        expected_templates = tuple(
            sorted({operation.template_hash for operation in manifest.operations})
        )
        graph = self.graph_query(
            roots=(manifest.recipe_id,),
            max_nodes=MAX_GRAPH_NODES,
            max_edges=MAX_GRAPH_EDGES,
            max_depth=MAX_GRAPH_DEPTH,
            byte_budget=MAX_PACKET_BYTES,
        )
        if packet.template_hashes != expected_templates or any(
            not _is_hash(item) for item in packet.template_hashes
        ):
            raise AtlasError("packet_template_mismatch")
        if (
            graph.truncated
            or packet.recipe_id != manifest.recipe_id
            or manifest.manifest_hash not in packet.evidence_hashes
            or packet.operations != _sorted_records(manifest.operations)
            or packet.slots != tuple(sorted(manifest.slots, key=lambda item: item.name))
            or packet.constraints != _sorted_records(manifest.constraints)
            or packet.dependencies != _sorted_records(manifest.dependencies)
            or packet.tests != _sorted_records(manifest.tests)
            or packet.node_ids != tuple(node.node_id for node in graph.nodes)
            or packet.edge_ids != tuple(edge.edge_id for edge in graph.edges)
        ):
            raise AtlasError("packet_recipe_mismatch")

    @staticmethod
    def _render_evidence(packet: ImplementationPacket) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for item in packet.evidence_windows:
            value = thaw_json(item)
            if (
                type(value) is not dict
                or not {"path", "content_hash"} <= set(value)
                or set(value) - {"path", "content_hash", "start_line", "end_line"}
                or not isinstance(value["path"], str)
                or not _is_hash(value["content_hash"])
            ):
                raise AtlasError("source_hash_mismatch")
            try:
                path = validate_candidate_path(value["path"])
            except AtlasError as exc:
                raise AtlasError("source_path_unsafe") from exc
            current = evidence.get(path)
            if current is not None and current != value["content_hash"]:
                raise AtlasError("source_hash_mismatch")
            evidence[path] = value["content_hash"]
        return evidence

    @staticmethod
    def _render_fact_paths(facts: SnapshotFacts) -> dict[str, tuple[str, str]]:
        values: dict[str, tuple[str, str]] = {}
        for raw_path, content_hash in facts.file_hashes:
            if not isinstance(raw_path, str) or not _is_hash(content_hash):
                raise AtlasError("source_hash_mismatch")
            try:
                path = validate_candidate_path(raw_path)
            except AtlasError as exc:
                raise AtlasError("source_path_unsafe") from exc
            key = path_collision_key(path)
            if key in values and values[key][0] != path:
                raise AtlasError("path_case_collision")
            values[key] = (path, content_hash)
        return values

    def _render_template_reader(
        self, manifest: RecipeManifest
    ) -> Callable[[str], bytes]:
        def read(template_hash: str) -> bytes:
            try:
                if manifest.layer == "bundled":
                    return self._bundled_loader.read_template(template_hash)
                return self._store.read_blob_verified(
                    template_hash, max_bytes=MAX_TEMPLATE_BYTES
                )
            except AtlasError as error:
                if error.code in {"unsafe_asset_reference"}:
                    raise AtlasError("template_blob_unsafe") from error
                if error.code in {"missing_template_blob"}:
                    raise AtlasError("template_blob_missing") from error
                raise AtlasError("template_blob_integrity") from error
            except StoreConflictError as error:
                raise AtlasError("template_blob_integrity") from error

        return read

    def render(
        self,
        workspace_id: str,
        snapshot_id: str,
        packet_id: str,
        bindings: Mapping[str, str],
    ) -> RenderResult:
        """Return one deterministic patch candidate and inert test specifications."""

        if (
            not is_workspace_id(workspace_id)
            or type(snapshot_id) is not str
            or not _is_hash(snapshot_id)
            or type(packet_id) is not str
            or not _is_hash(packet_id)
            or type(bindings) is not dict
            or len(bindings) > MAX_SLOT_COUNT
        ):
            return self._render_invalid(packet_id, "request_invalid")
        try:
            packet = self._store.get_packet_verified(packet_id)
        except StoreConflictError:
            return self._render_invalid(packet_id, "packet_integrity_mismatch")
        if packet is None:
            return self._render_invalid(packet_id, "packet_not_found")
        if packet.workspace_id != workspace_id:
            return self._render_invalid(packet_id, "packet_workspace_mismatch")
        if packet.snapshot_id != snapshot_id:
            return self._render_invalid(packet_id, "packet_snapshot_mismatch")
        if packet.next_action != "atlas_render":
            return self._render_invalid(packet_id, "packet_recipe_mismatch")
        try:
            packet_paths = self._prepare_paths(packet.target_paths)
            packet_symbols = self._prepare_symbols(packet.target_symbols)
        except AtlasError:
            return self._render_invalid(packet_id, "packet_integrity_mismatch")
        if (
            packet.matcher_version != ATLAS_MATCHER_VERSION
            or not _is_hash(packet.request_hash)
            or packet.target_paths != packet_paths
            or packet.target_symbols != packet_symbols
        ):
            return self._render_invalid(packet_id, "packet_integrity_mismatch")
        if len(canonical_json(packet.to_dict()).encode("utf-8")) > MAX_PACKET_BYTES:
            return self._render_invalid(packet_id, "packet_integrity_mismatch")
        try:
            manifest = self._reload_render_manifest(packet.recipe_id)
            self._verify_render_packet_recipe(packet, manifest)
        except StoreConflictError:
            return self._render_invalid(packet_id, "packet_recipe_mismatch")
        except AtlasError as error:
            return self._render_invalid(packet_id, error.code)
        try:
            self._project_index.assert_current(workspace_id, snapshot_id)
            facts = self._project_index.snapshot_facts(workspace_id, snapshot_id)
            workspace_root = self._project_index.workspace_authority.resolve(
                workspace_id
            ).root
            repository_signature = structural_repository_signature(
                facts,
                language=manifest.language_name,
                framework=manifest.framework_name,
            )
            rematch = select_recipe(
                (manifest,),
                intent_id=manifest.intent_id,
                language=manifest.language_name,
                framework=manifest.framework_name,
                repository_signature=repository_signature,
                snapshot_facts=facts,
                max_candidates=1,
            )
            if rematch.status is not AtlasStatus.READY:
                raise AtlasError("constraint_unmet")
            validated = validate_bindings(manifest, bindings)
            evidence = self._render_evidence(packet)
            if packet.evidence_hashes != tuple(
                sorted({manifest.manifest_hash, *evidence.values()})
            ):
                raise AtlasError("packet_recipe_mismatch")
            fact_paths = self._render_fact_paths(facts)
            source_paths: set[str] = set()
            for operation in manifest.operations:
                path = validated.get(operation.path_slot)
                if path is None:
                    raise AtlasError("binding_schema_invalid")
                try:
                    path = validate_candidate_path(path, workspace_root)
                except AtlasError as exc:
                    raise AtlasError("source_path_unsafe") from exc
                key = path_collision_key(path)
                existing = fact_paths.get(key)
                if existing is not None and existing[0] != path:
                    raise AtlasError("path_case_collision")
                if operation.kind == "create_python_file":
                    if existing is not None:
                        raise AtlasError("operation_path_collision")
                    try:
                        validate_absent_workspace_path(workspace_root, path)
                    except AtlasError as exc:
                        raise AtlasError("source_path_unsafe") from exc
                    continue
                if existing is None or path not in evidence:
                    raise AtlasError("source_hash_mismatch")
                if evidence[path] != existing[1]:
                    raise AtlasError("source_hash_mismatch")
                if evidence[path] not in packet.source_hashes:
                    raise AtlasError("source_hash_mismatch")
                source_paths.add(path)
            if (
                tuple(sorted(packet.source_hashes)) != packet.source_hashes
                or any(not _is_hash(value) for value in packet.source_hashes)
                or len(set(packet.source_hashes)) != len(packet.source_hashes)
            ):
                raise AtlasError("source_hash_mismatch")
            files = (
                self._project_index.read_snapshot_files(
                    workspace_id,
                    snapshot_id,
                    tuple(sorted(source_paths, key=path_collision_key)),
                    byte_budget=MAX_PACKET_BYTES,
                )
                if source_paths
                else ()
            )
            source_files = {item.path: item.body for item in files}
            if set(source_files) != source_paths:
                raise AtlasError("source_hash_mismatch")
            for item in files:
                expected = evidence.get(item.path)
                current = next(
                    (
                        content_hash
                        for path, content_hash in facts.file_hashes
                        if path == item.path
                    ),
                    None,
                )
                if expected != item.content_hash or current != item.content_hash:
                    raise AtlasError("source_hash_mismatch")
            rendered = render_patch(
                manifest,
                validated,
                source_files=source_files,
                snapshot_paths=tuple(path for path, _hash in facts.file_hashes),
                template_reader=self._render_template_reader(manifest),
            )
            self._project_index.assert_current(workspace_id, snapshot_id)
            result = RenderResult(
                AtlasStatus.READY,
                packet_id,
                patch_candidate=rendered.patch_candidate,
                patch_hash=rendered.patch_hash,
                bindings_hash=rendered.bindings_hash,
                test_specs=rendered.test_specs,
            )
            if len(canonical_json(result.to_dict()).encode("utf-8")) > MAX_PACKET_BYTES:
                raise AtlasError("render_budget_exceeded")
            return result
        except IndexError:
            return self._render_invalid(packet_id, "index_stale")
        except AtlasError as error:
            return self._render_invalid(packet_id, error.code)
        except (OSError, TypeError, UnicodeError, ValueError):
            return self._render_invalid(packet_id, "request_invalid")
