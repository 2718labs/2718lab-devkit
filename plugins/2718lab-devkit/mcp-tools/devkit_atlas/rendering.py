"""Pure, deterministic in-memory rendering for verified Atlas recipes."""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .canonical import canonical_hash
from .models import AtlasError, RecipeManifest, TestSpec
from .security import (
    MAX_CHANGED_FILES,
    MAX_COMMAND_SPEC_BYTES,
    MAX_PACKET_BYTES,
    MAX_RECIPE_BYTES,
    MAX_SLOT_COUNT,
    MAX_TEMPLATE_BYTES,
    path_collision_key,
    validate_fragment,
    validate_slot_value,
)


_PLACEHOLDER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PYTHON_ARG_SLOT_TYPES = frozenset(
    {"relative_python_path", "python_identifier", "python_qualified_name"}
)
_OPERATION_KINDS = frozenset(
    {"create_python_file", "append_python_nodes", "prepend_function_body"}
)


@dataclass(frozen=True, slots=True)
class RenderedPatch:
    """The data-only output consumed by the service's public result envelope."""

    patch_candidate: str
    patch_hash: str
    bindings_hash: str
    test_specs: tuple[TestSpec, ...]
    changed_paths: tuple[str, ...]


def _fail(code: str) -> None:
    raise AtlasError(code)


def _placeholder_names(value: str, *, code: str) -> frozenset[str]:
    """Accept only complete, non-nested ``${python_identifier}`` tokens."""

    names: set[str] = set()
    cursor = 0
    while True:
        start = value.find("${", cursor)
        if start < 0:
            return frozenset(names)
        end = value.find("}", start + 2)
        if end < 0:
            _fail(code)
        name = value[start + 2 : end]
        if "${" in name or "}" in name or _PLACEHOLDER_NAME.fullmatch(name) is None:
            _fail(code)
        names.add(name)
        cursor = end + 1
        if cursor < len(value) and value[cursor] == "}":
            _fail(code)


def _exactly_one_lf(text: str, *, code: str) -> None:
    if "\r" in text or not text.endswith("\n") or text.endswith("\n\n"):
        _fail(code)


def _safe_text(value: bytes | str, *, maximum: int, code: str) -> str:
    try:
        return validate_fragment(value, max_bytes=maximum)
    except AtlasError as error:
        if error.code == "credential_detected":
            _fail("secret_detected")
        _fail(code)


def validate_bindings(
    manifest: RecipeManifest, bindings: Mapping[str, str]
) -> dict[str, str]:
    """Validate the exact v1 slot map before any template/source is read."""

    if type(bindings) is not dict or len(bindings) > MAX_SLOT_COUNT:
        _fail("binding_schema_invalid")
    slots = tuple(manifest.slots)
    if len(slots) > MAX_SLOT_COUNT or any(not slot.required for slot in slots):
        _fail("binding_schema_invalid")
    slot_names = {slot.name for slot in slots}
    if set(bindings) - slot_names:
        _fail("binding_unknown")
    if slot_names - set(bindings):
        _fail("binding_missing")
    if len(slot_names) != len(slots):
        _fail("binding_schema_invalid")
    validated: dict[str, str] = {}
    total = 0
    for slot in sorted(slots, key=lambda item: item.name):
        value = bindings.get(slot.name)
        if not isinstance(value, str):
            _fail("binding_schema_invalid")
        try:
            total += len(value.encode("utf-8"))
        except UnicodeEncodeError:
            _fail("binding_invalid")
        if total > MAX_RECIPE_BYTES:
            _fail("render_budget_exceeded")
        try:
            _safe_text(value, maximum=MAX_TEMPLATE_BYTES, code="binding_invalid")
            validated[slot.name] = validate_slot_value(slot.type, value)
        except AtlasError as error:
            if error.code == "secret_detected":
                raise
            _fail("binding_invalid")
    paths: dict[str, str] = {}
    for slot in slots:
        if slot.type != "relative_python_path":
            continue
        value = validated[slot.name]
        key = path_collision_key(value)
        if key in paths:
            _fail("path_case_collision")
        paths[key] = value
    return validated


def _substitute(text: str, bindings: Mapping[str, str]) -> str:
    """Replace only original complete tokens, never tokens created by a value."""

    def replacement(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return bindings[name]
        except KeyError:
            _fail("template_placeholder_invalid")

    return _PLACEHOLDER_TOKEN.sub(replacement, text)


def _template_text(
    template_hash: str,
    template_reader: Callable[[str], bytes],
    *,
    total: int,
) -> tuple[str, int]:
    try:
        body = template_reader(template_hash)
    except AtlasError:
        raise
    except Exception as exc:
        raise AtlasError("template_blob_integrity") from exc
    if not isinstance(body, bytes) or len(body) > MAX_TEMPLATE_BYTES:
        _fail("template_blob_integrity")
    total += len(body)
    if total > MAX_RECIPE_BYTES:
        _fail("render_budget_exceeded")
    text = _safe_text(body, maximum=MAX_TEMPLATE_BYTES, code="template_blob_integrity")
    _exactly_one_lf(text, code="template_blob_integrity")
    return text, total


def _source_text(body: bytes) -> str:
    text = _safe_text(body, maximum=MAX_PACKET_BYTES, code="source_encoding_invalid")
    _exactly_one_lf(text, code="source_newline_invalid")
    try:
        ast.parse(text, mode="exec")
    except SyntaxError as exc:
        raise AtlasError("target_layout_unsupported") from exc
    return text


def _complete_python(text: str) -> None:
    _safe_text(text, maximum=MAX_PACKET_BYTES, code="render_budget_exceeded")
    _exactly_one_lf(text, code="rendered_parse_invalid")
    try:
        ast.parse(text, mode="exec")
    except SyntaxError as exc:
        raise AtlasError("rendered_parse_invalid") from exc


def _target_function(
    tree: ast.Module, qualified_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    parts = qualified_name.split(".")
    candidates: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    if len(parts) == 1:
        candidates.extend(
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == parts[0]
        )
    elif len(parts) == 2:
        for item in tree.body:
            if not isinstance(item, ast.ClassDef) or item.name != parts[0]:
                continue
            candidates.extend(
                child
                for child in item.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == parts[1]
            )
    if len(candidates) != 1:
        _fail("target_symbol_ambiguous")
    return candidates[0]


def _prepend_function_body(source: str, qualified_name: str, template: str) -> str:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise AtlasError("target_layout_unsupported") from exc
    target = _target_function(tree, qualified_name)
    if not target.body or target.body[0].lineno <= target.lineno:
        _fail("target_layout_unsupported")
    lines = source.splitlines(keepends=True)
    first_body = target.body[0]
    if first_body.lineno <= 0 or first_body.lineno > len(lines):
        _fail("target_layout_unsupported")
    match = re.match(r"^([ \t]+)\S", lines[first_body.lineno - 1])
    if match is None:
        _fail("target_layout_unsupported")
    indent = match.group(1)
    if " " in indent and "\t" in indent:
        _fail("target_layout_unsupported")
    for statement in target.body:
        if statement.lineno <= 0 or statement.lineno > len(lines):
            _fail("target_layout_unsupported")
        statement_match = re.match(r"^([ \t]+)\S", lines[statement.lineno - 1])
        if statement_match is None or statement_match.group(1) != indent:
            _fail("target_layout_unsupported")
    is_docstring = (
        isinstance(first_body, ast.Expr)
        and isinstance(getattr(first_body, "value", None), ast.Constant)
        and isinstance(first_body.value.value, str)
    )
    if is_docstring:
        end_line = getattr(first_body, "end_lineno", None)
        if not isinstance(end_line, int) or end_line < first_body.lineno:
            _fail("target_layout_unsupported")
        insert_at = end_line
    else:
        insert_at = first_body.lineno - 1
    if not 0 <= insert_at <= len(lines):
        _fail("target_layout_unsupported")
    indented = "".join(
        indent + line if line != "\n" else indent + "\n"
        for line in template.splitlines(keepends=True)
    )
    return "".join((*lines[:insert_at], indented, *lines[insert_at:]))


def _test_specs(
    manifest: RecipeManifest,
    bindings: Mapping[str, str],
    *,
    used_slots: set[str],
) -> tuple[TestSpec, ...]:
    slot_types = {slot.name: slot.type for slot in manifest.slots}
    rendered: list[TestSpec] = []
    total = 0
    for spec in manifest.tests:
        if not spec.argv or type(spec.expected_exit_code) is not int:
            _fail("test_spec_invalid")
        arguments: list[str] = []
        command_size = 0
        for argument in spec.argv:
            if not isinstance(argument, str) or not argument:
                _fail("test_spec_invalid")
            names = _placeholder_names(argument, code="template_placeholder_invalid")
            if not names <= set(slot_types):
                _fail("template_placeholder_invalid")
            if any(slot_types[name] not in _PYTHON_ARG_SLOT_TYPES for name in names):
                _fail("test_spec_invalid")
            used_slots.update(names)
            candidate = _substitute(argument, bindings)
            _safe_text(
                candidate, maximum=MAX_COMMAND_SPEC_BYTES, code="test_spec_invalid"
            )
            if not candidate or any(ord(character) < 32 for character in candidate):
                _fail("test_spec_invalid")
            try:
                size = len(candidate.encode("utf-8"))
            except UnicodeEncodeError:
                _fail("test_spec_invalid")
            command_size += size
            total += size
            if command_size > MAX_COMMAND_SPEC_BYTES or total > MAX_COMMAND_SPEC_BYTES:
                _fail("test_spec_invalid")
            arguments.append(candidate)
        rendered.append(TestSpec(tuple(arguments), spec.expected_exit_code))
    return tuple(rendered)


def render_patch(
    manifest: RecipeManifest,
    bindings: Mapping[str, str],
    *,
    source_files: Mapping[str, bytes],
    snapshot_paths: Iterable[str],
    template_reader: Callable[[str], bytes],
) -> RenderedPatch:
    """Render the three v1 operations entirely in memory and return a diff."""

    if len(manifest.operations) > MAX_CHANGED_FILES:
        _fail("render_budget_exceeded")
    validated = validate_bindings(manifest, bindings)
    snapshot = tuple(snapshot_paths)
    snapshot_by_key: dict[str, str] = {}
    for path in snapshot:
        if not isinstance(path, str):
            _fail("source_path_unsafe")
        key = path_collision_key(path)
        if key in snapshot_by_key and snapshot_by_key[key] != path:
            _fail("path_case_collision")
        snapshot_by_key[key] = path
    total = sum(len(value.encode("utf-8")) for value in validated.values())
    templates: dict[str, str] = {}
    used_slots: set[str] = set()
    changed: dict[str, tuple[str, str]] = {}
    changed_keys: dict[str, str] = {}
    for operation in manifest.operations:
        if operation.kind not in _OPERATION_KINDS:
            _fail("operation_unsupported")
        if operation.path_slot not in validated:
            _fail("binding_schema_invalid")
        path = validated[operation.path_slot]
        key = path_collision_key(path)
        if key in changed_keys:
            _fail("operation_path_collision")
        if key in snapshot_by_key and snapshot_by_key[key] != path:
            _fail("path_case_collision")
        changed_keys[key] = path
        used_slots.add(operation.path_slot)
        template = templates.get(operation.template_hash)
        if template is None:
            template, total = _template_text(
                operation.template_hash, template_reader, total=total
            )
            templates[operation.template_hash] = template
        placeholders = _placeholder_names(template, code="template_placeholder_invalid")
        slot_names = {slot.name for slot in manifest.slots}
        if not placeholders <= slot_names:
            _fail("template_placeholder_invalid")
        used_slots.update(placeholders)
        rendered_template = _substitute(template, validated)
        if operation.kind == "create_python_file":
            if manifest.layer != "local" or manifest.provenance_kind != "observed":
                _fail("operation_unsupported")
            if path in snapshot_by_key.values():
                _fail("operation_path_collision")
            before = ""
            after = rendered_template
        else:
            if path not in source_files:
                _fail("source_hash_mismatch")
            before = _source_text(source_files[path])
            if operation.kind == "append_python_nodes":
                if not isinstance(operation.separator, str):
                    _fail("operation_unsupported")
                separator = _safe_text(
                    operation.separator,
                    maximum=MAX_TEMPLATE_BYTES,
                    code="operation_unsupported",
                )
                if "\r" in separator or (manifest.layer == "local" and separator):
                    _fail("operation_unsupported")
                after = before + separator + rendered_template
            else:
                if manifest.layer != "bundled" or not operation.target_symbol_slot:
                    _fail("operation_unsupported")
                if operation.target_symbol_slot not in validated:
                    _fail("binding_schema_invalid")
                used_slots.add(operation.target_symbol_slot)
                after = _prepend_function_body(
                    before,
                    validated[operation.target_symbol_slot],
                    rendered_template,
                )
        _complete_python(after)
        changed[path] = (before, after)
    specs = _test_specs(manifest, validated, used_slots=used_slots)
    declared = {slot.name for slot in manifest.slots}
    if used_slots != declared:
        _fail("template_slot_mismatch")
    patch_parts: list[str] = []
    for path in sorted(changed, key=path_collision_key):
        before, after = changed[path]
        patch_parts.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=0,
                lineterm="\n",
            )
        )
    patch = "".join(patch_parts)
    if not patch:
        _fail("rendered_parse_invalid")
    _safe_text(patch, maximum=MAX_PACKET_BYTES, code="render_budget_exceeded")
    if "\r" in patch or len(patch.encode("utf-8")) > MAX_PACKET_BYTES:
        _fail("render_budget_exceeded")
    return RenderedPatch(
        patch_candidate=patch,
        patch_hash="sha256:" + hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        bindings_hash=canonical_hash(
            {name: validated[name] for name in sorted(validated)}
        ),
        test_specs=specs,
        changed_paths=tuple(sorted(changed, key=path_collision_key)),
    )
