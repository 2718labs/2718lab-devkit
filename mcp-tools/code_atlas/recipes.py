"""Strict, deterministic loading for bundled Code Atlas recipe assets."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, canonical_json, normalize_intent_id
from .models import (
    AtlasEdge,
    AtlasError,
    AtlasNode,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec,
)
from .security import (
    MAX_RECIPE_BYTES,
    MAX_SLOT_COUNT,
    MAX_TEMPLATE_BYTES,
    validate_candidate_path,
)


_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "recipe_key",
        "version",
        "intent_id",
        "language",
        "framework",
        "repository_signature",
        "slots",
        "constraints",
        "dependencies",
        "tests",
        "operations",
        "provenance",
    }
)
_SLOT = frozenset({"name", "type", "required"})
_LANGUAGE = frozenset({"name", "extractor_version"})
_FRAMEWORK = frozenset({"name", "specifier"})
_CONSTRAINT = frozenset({"kind", "subject", "value"})
_DEPENDENCY = frozenset({"name", "kind", "specifier"})
_TEST = frozenset({"argv", "expected_exit_code"})
_OPERATION = frozenset(
    {"kind", "path_slot", "template_hash", "separator", "target_symbol_slot"}
)
_PROVENANCE = frozenset({"kind", "source"})
_OPERATIONS = frozenset({"append_python_nodes", "prepend_function_body"})


def _error(code: str = "invalid_recipe") -> None:
    raise AtlasError(code)


def _object(
    value: Any, required: frozenset[str], *, optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - required - optional
        or required - set(value)
    ):
        _error()
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        _error()
    return value


class BundledRecipeLoader:
    """Read only configured bundled assets and project them into Atlas records."""

    def __init__(self, asset_root: str | Path) -> None:
        self._root = Path(asset_root).resolve()

    def _safe_child(self, relative: str) -> Path:
        normalized = validate_candidate_path(relative)
        candidate = self._root.joinpath(*normalized.split("/"))
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        cursor = self._root
        for part in normalized.split("/"):
            cursor /= part
            if cursor.is_symlink():
                raise AtlasError("unsafe_asset_reference")
        return candidate

    def _blob(self, template_hash: str) -> bytes:
        match = _HASH.fullmatch(template_hash)
        if match is None:
            _error("invalid_template_hash")
        blob_path = self._safe_child(f"templates/sha256/{match.group(1)}")
        if not blob_path.is_file():
            _error("missing_template_blob")
        try:
            body = blob_path.read_bytes()
        except OSError as exc:
            raise AtlasError("invalid_template_blob") from exc
        if len(body) > MAX_TEMPLATE_BYTES or hashlib.sha256(
            body
        ).hexdigest() != match.group(1):
            _error("template_hash_mismatch")
        if not body.endswith(b"\n") or body.endswith(b"\n\n"):
            _error("invalid_template_blob")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AtlasError("invalid_template_blob") from exc
        return body

    def _parse(self, path: Path) -> RecipeManifest:
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_RECIPE_BYTES:
                _error()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AtlasError("invalid_recipe") from exc
        item = _object(data, _TOP_LEVEL)
        if raw != canonical_json(item).encode("utf-8") + b"\n":
            _error("noncanonical_manifest")
        if (
            item["schema_version"] != "1"
            or isinstance(item["version"], bool)
            or not isinstance(item["version"], int)
        ):
            _error()
        intent_id = _text(item["intent_id"])
        if (
            not intent_id
            or normalize_intent_id(intent_id) != intent_id
            or item["recipe_key"] != intent_id
        ):
            _error("invalid_intent_id")
        language = _object(item["language"], _LANGUAGE)
        if language["name"] != "python" or language["extractor_version"] != "1":
            _error()
        framework = item["framework"]
        if framework is not None:
            framework = _object(framework, _FRAMEWORK)
            if not framework["name"] or not framework["specifier"]:
                _error()
        if not isinstance(item["repository_signature"], str):
            _error()
        provenance = _object(item["provenance"], _PROVENANCE)
        if provenance != {"kind": "bundled", "source": "2718lab-devkit"}:
            _error()
        slots_data = item["slots"]
        if (
            not isinstance(slots_data, list)
            or not slots_data
            or len(slots_data) > MAX_SLOT_COUNT
        ):
            _error("invalid_slots")
        slots = tuple(SlotSpec(**_object(slot, _SLOT)) for slot in slots_data)
        if any(
            not slot.name or not slot.type or not isinstance(slot.required, bool)
            for slot in slots
        ) or len({slot.name for slot in slots}) != len(slots):
            _error("invalid_slots")
        slot_names = {slot.name for slot in slots}
        constraints_data = item["constraints"]
        if not isinstance(constraints_data, list):
            _error()
        constraints = tuple(
            ConstraintSpec(**_object(value, _CONSTRAINT)) for value in constraints_data
        )
        if any(
            not value.kind or value.subject not in slot_names for value in constraints
        ):
            _error()
        dependencies_data = item["dependencies"]
        if not isinstance(dependencies_data, list):
            _error()
        dependencies = tuple(
            DependencySpec(**_object(value, _DEPENDENCY)) for value in dependencies_data
        )
        if any(not value.name or not value.kind for value in dependencies):
            _error()
        tests_data = item["tests"]
        if not isinstance(tests_data, list) or not tests_data:
            _error()
        tests = tuple(TestSpec(**_object(value, _TEST)) for value in tests_data)
        if any(
            not spec.argv
            or any(not isinstance(arg, str) for arg in spec.argv)
            or isinstance(spec.expected_exit_code, bool)
            or not isinstance(spec.expected_exit_code, int)
            for spec in tests
        ):
            _error()
        operations_data = item["operations"]
        if not isinstance(operations_data, list) or not operations_data:
            _error()
        operations = []
        for value in operations_data:
            operation = _object(
                value,
                frozenset({"kind", "path_slot", "template_hash"}),
                optional=frozenset({"separator", "target_symbol_slot"}),
            )
            if (
                operation["kind"] not in _OPERATIONS
                or operation["path_slot"] not in slot_names
            ):
                _error("invalid_operation")
            if (
                operation["kind"] == "append_python_nodes"
                and operation.get("separator", "") != "\n\n"
            ):
                _error("invalid_operation")
            if (
                operation["kind"] == "prepend_function_body"
                and operation.get("target_symbol_slot") not in slot_names
            ):
                _error("invalid_operation")
            body = self._blob(_text(operation["template_hash"]))
            placeholders = set(_PLACEHOLDER.findall(body.decode("utf-8")))
            if not placeholders <= slot_names:
                _error("undeclared_placeholder")
            operations.append(
                TemplateOperation(
                    kind=operation["kind"],
                    path_slot=operation["path_slot"],
                    template_hash=operation["template_hash"],
                    separator=operation.get("separator", ""),
                    target_symbol_slot=operation.get("target_symbol_slot", ""),
                )
            )
        manifest_hash = canonical_hash(item)
        return RecipeManifest(
            recipe_id=manifest_hash,
            recipe_key=intent_id,
            version=item["version"],
            intent_id=intent_id,
            language_name=language["name"],
            language_extractor_version=language["extractor_version"],
            repository_signature=item["repository_signature"],
            layer="bundled",
            manifest_hash=manifest_hash,
            framework_name=None if framework is None else framework["name"],
            framework_specifier=None if framework is None else framework["specifier"],
            slots=slots,
            constraints=constraints,
            dependencies=dependencies,
            tests=tests,
            operations=tuple(operations),
            provenance_kind=provenance["kind"],
            provenance_source=provenance["source"],
            schema_version=item["schema_version"],
        )

    def load(self) -> tuple[RecipeManifest, ...]:
        recipes_dir = self._safe_child("recipes")
        if not recipes_dir.is_dir():
            _error("missing_recipe_assets")
        paths = sorted(recipes_dir.glob("*.json"), key=lambda value: value.name)
        if len(paths) != 3 or any(path.is_symlink() for path in paths):
            _error("invalid_recipe_assets")
        recipes = tuple(self._parse(path) for path in paths)
        if len({recipe.intent_id for recipe in recipes}) != len(recipes):
            _error("duplicate_recipe")
        return tuple(
            sorted(recipes, key=lambda recipe: (recipe.intent_id, recipe.recipe_id))
        )

    def validate(self) -> tuple[()]:
        self.load()
        return ()

    def materialize(self) -> GraphQueryResult:
        nodes: list[AtlasNode] = []
        edges: list[AtlasEdge] = []
        for recipe in self.load():
            recipe_node = AtlasNode.create(NodeKind.RECIPE, recipe.to_dict())
            intent = AtlasNode.create(NodeKind.INTENT, {"intent_id": recipe.intent_id})
            language = AtlasNode.create(
                NodeKind.LANGUAGE,
                {
                    "name": recipe.language_name,
                    "extractor_version": recipe.language_extractor_version,
                },
            )
            evidence = AtlasNode.create(
                NodeKind.SOURCE_EVIDENCE,
                {
                    "kind": "bundled",
                    "manifest_hash": recipe.manifest_hash,
                    "source": recipe.provenance_source,
                },
            )
            nodes.extend((recipe_node, intent, language, evidence))
            edges.extend(
                (
                    AtlasEdge.create(EdgeRelation.SOLVES, recipe_node, intent),
                    AtlasEdge.create(EdgeRelation.REQUIRES, recipe_node, language),
                    AtlasEdge.create(EdgeRelation.BUNDLED_AS, recipe_node, evidence),
                )
            )
            if recipe.framework_name is not None:
                framework = AtlasNode.create(
                    NodeKind.FRAMEWORK,
                    {
                        "name": recipe.framework_name,
                        "specifier": recipe.framework_specifier,
                    },
                )
                nodes.append(framework)
                edges.append(
                    AtlasEdge.create(EdgeRelation.REQUIRES, recipe_node, framework)
                )
            for operation in recipe.operations:
                template = AtlasNode.create(
                    NodeKind.CODE_TEMPLATE,
                    {"template_hash": operation.template_hash, "kind": operation.kind},
                )
                nodes.append(template)
                edges.append(
                    AtlasEdge.create(
                        EdgeRelation.HAS_IMPLEMENTATION, recipe_node, template
                    )
                )
            for slot in recipe.slots:
                node = AtlasNode.create(NodeKind.ADAPTATION_SLOT, slot.to_dict())
                nodes.append(node)
                edges.append(AtlasEdge.create(EdgeRelation.HAS_SLOT, recipe_node, node))
            for constraint in recipe.constraints:
                node = AtlasNode.create(NodeKind.CONSTRAINT, constraint.to_dict())
                nodes.append(node)
                edges.append(
                    AtlasEdge.create(EdgeRelation.CONSTRAINED_BY, recipe_node, node)
                )
            for dependency in recipe.dependencies:
                node = AtlasNode.create(NodeKind.DEPENDENCY, dependency.to_dict())
                nodes.append(node)
                edges.append(AtlasEdge.create(EdgeRelation.REQUIRES, recipe_node, node))
            for test in recipe.tests:
                node = AtlasNode.create(NodeKind.TEST_SPEC, test.to_dict())
                nodes.append(node)
                edges.append(
                    AtlasEdge.create(EdgeRelation.VERIFIED_BY, recipe_node, node)
                )
        unique_nodes = {node.node_id: node for node in nodes}
        unique_edges = {edge.edge_id: edge for edge in edges}
        return GraphQueryResult(
            tuple(sorted(unique_nodes.values(), key=lambda node: node.node_id)),
            tuple(sorted(unique_edges.values(), key=lambda edge: edge.edge_id)),
        )


def render_pattern_card(recipe: RecipeManifest) -> str:
    """Return a stable, display-only Markdown projection of a recipe."""
    lines = [
        f"# Pattern Card: {recipe.intent_id}",
        "",
        f"- Recipe ID: `{recipe.recipe_id}`",
        f"- Intent ID: `{recipe.intent_id}`",
        f"- Layer: `{recipe.layer}`",
        "",
        "## Slots",
        "",
    ]
    lines.extend(
        f"- `{slot.name}`: `{slot.type}` ({'required' if slot.required else 'optional'})"
        for slot in recipe.slots
    )
    lines.extend(("", "## Verification", ""))
    lines.extend(
        f"- `{' '.join(test.argv)}` (exit {test.expected_exit_code})"
        for test in recipe.tests
    )
    return "\n".join(lines) + "\n"
