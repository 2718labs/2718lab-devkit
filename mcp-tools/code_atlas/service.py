"""Bounded, deterministic Code Atlas graph and packet service."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, fields, replace
from pathlib import PurePosixPath
from typing import Any

from project_index.models import IndexError, SnapshotFacts
from project_index.service import ProjectIndexService

from .canonical import canonical_hash, canonical_json, normalize_intent_id, thaw_json
from .matching import (
    MatchCandidate,
    normalize_framework,
    structural_repository_signature,
    select_recipe,
)
from .models import (
    AtlasEdge,
    AtlasError,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    ImplementationPacket,
    NodeKind,
    PreparationResult,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec,
)
from .recipes import BundledRecipeLoader
from .security import (
    MAX_CHANGED_FILES,
    MAX_COMMAND_SPEC_BYTES,
    MAX_GRAPH_DEPTH,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
    MAX_SLOT_COUNT,
    validate_candidate_path,
    validate_fragment,
    validate_slot_value,
)
from .store import AtlasStore, StoreConflictError


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
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


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


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


class CodeAtlasService:
    """Join immutable bundled records and bounded local Atlas graph records."""

    def __init__(
        self,
        store: AtlasStore,
        bundled_loader: BundledRecipeLoader,
        project_index: ProjectIndexService,
    ) -> None:
        self._store = store
        self._project_index = project_index
        self._bundled_manifests = tuple(bundled_loader.load())
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
                manifests.append(self._hydrate_manifest(result.nodes[0]))
            except AtlasError:
                malformed = True
        return (
            tuple(sorted(manifests, key=lambda item: item.recipe_id)),
            malformed,
            False,
        )

    @staticmethod
    def _prepare_paths(
        value: Sequence[str] | None, workspace: str | PurePosixPath
    ) -> tuple[str, ...]:
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
        normalized = tuple(
            validate_candidate_path(path, workspace) for path in raw_paths
        )
        if len(set(normalized)) != len(normalized):
            raise AtlasError("duplicate_target_path")
        return tuple(sorted(normalized))

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
        workspace: str,
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
        if not isinstance(workspace, str) or not isinstance(snapshot_id, str):
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
        target_paths = self._prepare_paths(target_paths, workspace)
        target_symbols = self._prepare_symbols(target_symbols)
        if normalized_language != "python":
            return PreparationResult(
                AtlasStatus.UNSUPPORTED_LANGUAGE,
                reasons=("unsupported_language",),
            )
        try:
            self._project_index.assert_current(workspace, snapshot_id)
            facts = self._project_index.snapshot_facts(workspace, snapshot_id)
        except IndexError as exc:
            return self._index_failure(exc)
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
                workspace,
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
        trace_id = canonical_hash(
            {
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
            }
        )
        provisional = ImplementationPacket(
            packet_id="",
            trace_id=trace_id,
            workspace=facts.snapshot.workspace,
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
            next_action="code_atlas_render",
        )
        packet_data = provisional.to_dict()
        del packet_data["packet_id"]
        packet = replace(provisional, packet_id=canonical_hash(packet_data))
        try:
            self._project_index.assert_current(workspace, snapshot_id)
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
