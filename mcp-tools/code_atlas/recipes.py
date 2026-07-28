"""Strict, deterministic loading for bundled Code Atlas recipe assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import replace
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
    validate_fragment,
)


_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")
_PLACEHOLDER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
_SLOT_TYPES = frozenset(
    {
        "relative_python_path",
        "python_identifier",
        "python_qualified_name",
        "python_expression",
        "python_statement_block",
        "single_line_text",
    }
)
_LOCKED_SEEDS = {
    "python-fastmcp-read-tool.json": (
        "python.fastmcp.read-only-tool",
        "sha256:090d5bb247d1fa88d5ae65f39451ca4f0bf176a50716ef6c647752d10a1a029b",
        "sha256:c0bd2bd01da25b333bac5dcb06f953242ecaa80c772d84bc48025e70a58e6950",
    ),
    "python-pytest-regression.json": (
        "python.pytest-regression",
        "sha256:29572bd77a897bbb035fbf6cb79a21ff1a2298144b8c60dcd0d6a12231b45f4c",
        "sha256:d07c7a977b330d26fe6486ab5e07f02855117f092ad19b0b08b2aafb136aef6c",
    ),
    "python-validation-guard.json": (
        "python.validation-guard",
        "sha256:c6f2adacb33c5a5037da8559b1b4b550b1cf25dbcfd834d377aaacb6f51ed59a",
        "sha256:47820213e5b67e968dff12d9509b8990d7aa1a6465a85b631599628589f8d8de",
    ),
}
_LOCKED_TEMPLATE_HASHES = frozenset(seed[2] for seed in _LOCKED_SEEDS.values())


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


def _recipe_payload(recipe: RecipeManifest) -> dict[str, Any]:
    payload = recipe.to_dict()
    del payload["recipe_id"]
    return payload


def _recipe_node(recipe: RecipeManifest) -> AtlasNode:
    return AtlasNode.create(
        NodeKind.RECIPE,
        _recipe_payload(recipe),
        provenance="declared",
        source_hashes=(recipe.manifest_hash,),
    )


def _placeholder_names(text: str, *, code: str) -> set[str]:
    """Parse complete, non-nested ``${python_identifier}`` placeholders."""
    names: set[str] = set()
    cursor = 0
    while (start := text.find("${", cursor)) != -1:
        end = text.find("}", start + 2)
        if end == -1:
            _error(code)
        name = text[start + 2 : end]
        if "${" in name or not _PLACEHOLDER_NAME.fullmatch(name):
            _error(code)
        names.add(name)
        cursor = end + 1
    return names


class BundledRecipeLoader:
    """Read only configured bundled assets and project them into Atlas records."""

    def __init__(self, asset_root: str | Path) -> None:
        self._root = Path(asset_root)

    @staticmethod
    def _assert_safe_component(path: Path) -> None:
        try:
            stat_result = path.lstat()
            is_junction = getattr(os.path, "isjunction", None)
            if (
                path.is_symlink()
                or getattr(stat_result, "st_file_attributes", 0) & 0x400
                or (callable(is_junction) and is_junction(path))
            ):
                raise AtlasError("unsafe_asset_reference")
        except AtlasError:
            raise
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    def _safe_child(self, relative: str, *, allow_missing_final: bool = False) -> Path:
        try:
            self._assert_safe_component(self._root)
            root = self._root.resolve(strict=True)
            self._assert_safe_component(root)
            normalized = validate_candidate_path(relative)
            candidate = root.joinpath(*normalized.split("/"))
        except (OSError, ValueError) as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        cursor = root
        parts = normalized.split("/")
        for index, part in enumerate(parts):
            cursor /= part
            if allow_missing_final and index == len(parts) - 1 and not cursor.exists():
                continue
            self._assert_safe_component(cursor)
        return candidate

    def _safe_read(self, relative: str, *, max_bytes: int, missing_code: str) -> bytes:
        path = self._safe_child(relative, allow_missing_final=True)
        try:
            pre = path.lstat()
        except FileNotFoundError:
            _error(missing_code)
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        self._assert_safe_component(path)
        if not stat.S_ISREG(pre.st_mode):
            _error("unsafe_asset_reference")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or self._identity(
                opened
            ) != self._identity(pre):
                _error("unsafe_asset_reference")
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(65_536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
            if self._identity(after) != self._identity(opened):
                _error("unsafe_asset_reference")
        except AtlasError:
            raise
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise AtlasError("unsafe_asset_reference") from exc
        try:
            post = path.lstat()
            self._safe_child(relative)
        except AtlasError:
            raise
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        if self._identity(post) != self._identity(pre):
            _error("unsafe_asset_reference")
        return b"".join(chunks)

    def _blob(self, template_hash: str) -> bytes:
        match = _HASH.fullmatch(template_hash)
        if match is None:
            _error("invalid_template_hash")
        missing_code = (
            "bundled_asset_mismatch"
            if template_hash in _LOCKED_TEMPLATE_HASHES
            else "missing_template_blob"
        )
        body = self._safe_read(
            f"templates/sha256/{match.group(1)}",
            max_bytes=MAX_TEMPLATE_BYTES,
            missing_code=missing_code,
        )
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
        try:
            validate_fragment(body)
        except AtlasError as exc:
            raise AtlasError("invalid_template_blob") from exc
        return body

    def read_template(self, template_hash: str) -> bytes:
        """Return one locked bundled template through the shared safe reader."""

        return self._blob(template_hash)

    def _parse(self, filename: str) -> RecipeManifest:
        try:
            raw = self._safe_read(
                f"recipes/{filename}",
                max_bytes=MAX_RECIPE_BYTES,
                missing_code="missing_recipe_assets",
            )
            if len(raw) > MAX_RECIPE_BYTES:
                _error()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AtlasError("invalid_recipe") from exc
        item = _object(data, _TOP_LEVEL)
        try:
            canonical = canonical_json(item).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise AtlasError("invalid_recipe") from exc
        if raw != canonical:
            _error("noncanonical_manifest")
        if (
            item["schema_version"] != "1"
            or isinstance(item["version"], bool)
            or not isinstance(item["version"], int)
            or item["version"] <= 0
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
        if (
            not all(isinstance(language[key], str) for key in _LANGUAGE)
            or language["name"] != "python"
            or language["extractor_version"] != "1"
        ):
            _error()
        framework = item["framework"]
        if framework is not None:
            framework = _object(framework, _FRAMEWORK)
            if not all(
                isinstance(framework[key], str) and framework[key] for key in _FRAMEWORK
            ):
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
        if any(
            not isinstance(slot, dict)
            or not isinstance(slot.get("name"), str)
            or not isinstance(slot.get("type"), str)
            or type(slot.get("required")) is not bool
            for slot in slots_data
        ):
            _error("invalid_slots")
        slots = tuple(SlotSpec(**_object(slot, _SLOT)) for slot in slots_data)
        if any(
            not slot.name
            or slot.type not in _SLOT_TYPES
            or not isinstance(slot.required, bool)
            for slot in slots
        ) or len({slot.name for slot in slots}) != len(slots):
            _error("invalid_slots")
        slot_names = {slot.name for slot in slots}
        constraints_data = item["constraints"]
        if not isinstance(constraints_data, list):
            _error()
        if any(
            not isinstance(value, dict)
            or not isinstance(value.get("kind"), str)
            or not isinstance(value.get("subject"), str)
            for value in constraints_data
        ):
            _error()
        constraints = tuple(
            ConstraintSpec(**_object(value, _CONSTRAINT)) for value in constraints_data
        )
        if any(
            value.kind not in {"path_suffix", "required_symbol"}
            or value.subject not in slot_names
            or not isinstance(value.value, str)
            or not value.value
            for value in constraints
        ):
            _error("invalid_constraint")
        dependencies_data = item["dependencies"]
        if not isinstance(dependencies_data, list):
            _error()
        if any(
            not isinstance(value, dict)
            or not all(isinstance(value.get(key), str) for key in _DEPENDENCY)
            for value in dependencies_data
        ):
            _error()
        dependencies = tuple(
            DependencySpec(**_object(value, _DEPENDENCY)) for value in dependencies_data
        )
        if any(not value.name or not value.kind for value in dependencies):
            _error()
        tests_data = item["tests"]
        if not isinstance(tests_data, list) or not tests_data:
            _error()
        if any(
            not isinstance(value, dict)
            or not isinstance(value.get("argv"), list)
            or not value["argv"]
            or any(not isinstance(arg, str) for arg in value["argv"])
            or type(value.get("expected_exit_code")) is not int
            for value in tests_data
        ):
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
        if any(
            not _placeholder_names(argument, code="invalid_test_spec") <= slot_names
            for spec in tests
            for argument in spec.argv
        ):
            _error("invalid_test_spec")
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
            if not all(
                isinstance(operation[key], str)
                for key in ("kind", "path_slot", "template_hash")
            ):
                _error("invalid_operation")
            if operation["kind"] == "append_python_nodes" and set(operation) != {
                "kind",
                "path_slot",
                "template_hash",
                "separator",
            }:
                _error("invalid_operation")
            if operation["kind"] == "prepend_function_body" and set(operation) != {
                "kind",
                "path_slot",
                "template_hash",
                "target_symbol_slot",
            }:
                _error("invalid_operation")
            if "separator" in operation and not isinstance(operation["separator"], str):
                _error("invalid_operation")
            if "target_symbol_slot" in operation and not isinstance(
                operation["target_symbol_slot"], str
            ):
                _error("invalid_operation")
            if (
                operation["kind"] not in _OPERATIONS
                or operation["path_slot"] not in slot_names
                or next(
                    slot for slot in slots if slot.name == operation["path_slot"]
                ).type
                != "relative_python_path"
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
            if (
                operation["kind"] == "prepend_function_body"
                and next(
                    slot
                    for slot in slots
                    if slot.name == operation["target_symbol_slot"]
                ).type
                != "python_qualified_name"
            ):
                _error("invalid_operation")
            body = self.read_template(_text(operation["template_hash"]))
            placeholders = _placeholder_names(
                body.decode("utf-8"), code="invalid_template_placeholder"
            )
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
        manifest = RecipeManifest(
            recipe_id="",
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
        return replace(manifest, recipe_id=_recipe_node(manifest).node_id)

    def load(self) -> tuple[RecipeManifest, ...]:
        recipes_dir = self._safe_child("recipes")
        if not recipes_dir.is_dir():
            _error("missing_recipe_assets")
        try:
            paths = sorted(recipes_dir.iterdir(), key=lambda value: value.name)
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        if {path.name for path in paths} != set(_LOCKED_SEEDS):
            _error("bundled_asset_mismatch")
        for path in paths:
            self._assert_safe_component(path)
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    _error("unsafe_asset_reference")
            except AtlasError:
                raise
            except OSError as exc:
                raise AtlasError("unsafe_asset_reference") from exc
        recipes = tuple(self._parse(path.name) for path in paths)
        if len({recipe.intent_id for recipe in recipes}) != len(recipes):
            _error("duplicate_recipe")
        for path, recipe in zip(paths, recipes, strict=True):
            intent_id, manifest_hash, template_hash = _LOCKED_SEEDS[path.name]
            if (
                recipe.intent_id != intent_id
                or recipe.manifest_hash != manifest_hash
                or tuple(item.template_hash for item in recipe.operations)
                != (template_hash,)
            ):
                _error("bundled_asset_mismatch")
        templates_dir = self._safe_child("templates/sha256")
        try:
            template_names = {path.name for path in templates_dir.iterdir()}
        except OSError as exc:
            raise AtlasError("unsafe_asset_reference") from exc
        if template_names != {value[7:] for value in _LOCKED_TEMPLATE_HASHES}:
            _error("bundled_asset_mismatch")
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
            recipe_node = _recipe_node(recipe)
            node_options = {
                "provenance": "declared",
                "source_hashes": (recipe.manifest_hash,),
            }
            intent = AtlasNode.create(
                NodeKind.INTENT, {"intent_id": recipe.intent_id}, **node_options
            )
            language = AtlasNode.create(
                NodeKind.LANGUAGE,
                {
                    "name": recipe.language_name,
                    "extractor_version": recipe.language_extractor_version,
                },
                **node_options,
            )
            evidence = AtlasNode.create(
                NodeKind.SOURCE_EVIDENCE,
                {
                    "kind": "bundled",
                    "manifest_hash": recipe.manifest_hash,
                    "source": recipe.provenance_source,
                },
                **node_options,
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
                    **node_options,
                )
                nodes.append(framework)
                edges.append(
                    AtlasEdge.create(EdgeRelation.REQUIRES, recipe_node, framework)
                )
            for operation in recipe.operations:
                template = AtlasNode.create(
                    NodeKind.CODE_TEMPLATE,
                    {"template_hash": operation.template_hash, "kind": operation.kind},
                    **node_options,
                )
                nodes.append(template)
                edges.append(
                    AtlasEdge.create(
                        EdgeRelation.HAS_IMPLEMENTATION, recipe_node, template
                    )
                )
            for slot in recipe.slots:
                node = AtlasNode.create(
                    NodeKind.ADAPTATION_SLOT, slot.to_dict(), **node_options
                )
                nodes.append(node)
                edges.append(AtlasEdge.create(EdgeRelation.HAS_SLOT, recipe_node, node))
            for constraint in recipe.constraints:
                node = AtlasNode.create(
                    NodeKind.CONSTRAINT, constraint.to_dict(), **node_options
                )
                nodes.append(node)
                edges.append(
                    AtlasEdge.create(EdgeRelation.CONSTRAINED_BY, recipe_node, node)
                )
            for dependency in recipe.dependencies:
                node = AtlasNode.create(
                    NodeKind.DEPENDENCY, dependency.to_dict(), **node_options
                )
                nodes.append(node)
                edges.append(AtlasEdge.create(EdgeRelation.REQUIRES, recipe_node, node))
            for test in recipe.tests:
                node = AtlasNode.create(
                    NodeKind.TEST_SPEC, test.to_dict(), **node_options
                )
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
        f"- Manifest hash: `{recipe.manifest_hash}`",
        "",
        "## Applicability",
        "",
        f"- Language: `{recipe.language_name}` (extractor `{recipe.language_extractor_version}`)",
        f"- Framework: `{recipe.framework_name or 'none'}`{f' ({recipe.framework_specifier})' if recipe.framework_name else ''}",
        f"- Repository signature: `{recipe.repository_signature}`",
        "",
        "## Slots",
        "",
    ]
    lines.extend(
        f"- `{slot.name}`: `{slot.type}` ({'required' if slot.required else 'optional'})"
        for slot in recipe.slots
    )
    lines.extend(("", "## Constraints", ""))
    lines.extend(
        f"- `{constraint.kind}` on `{constraint.subject}`: `{constraint.value}`"
        for constraint in recipe.constraints
    )
    lines.extend(("", "## Dependencies", ""))
    lines.extend(
        f"- `{dependency.name}` ({dependency.kind} {dependency.specifier})"
        for dependency in recipe.dependencies
    )
    lines.extend(("", "## Operations", ""))
    lines.extend(
        f"- `{operation.kind}` via `{operation.template_hash}` on `{operation.path_slot}`"
        for operation in recipe.operations
    )
    lines.extend(("", "## Verification", ""))
    lines.extend(
        f"- `{' '.join(test.argv)}` (exit {test.expected_exit_code})"
        for test in recipe.tests
    )
    lines.extend(
        (
            "",
            "## Provenance",
            "",
            f"- `{recipe.provenance_kind}` from `{recipe.provenance_source}`",
        )
    )
    return "\n".join(lines) + "\n"
