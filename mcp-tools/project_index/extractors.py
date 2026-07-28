"""Standard-library parsers that turn source bytes into mechanical graph facts."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .models import CoverageGap, IndexEdge, IndexNode


@dataclass(frozen=True)
class SourceFile:
    path: str
    content_hash: str
    data: bytes
    text: str | None


@dataclass(frozen=True)
class ExtractorSpec:
    extractor_id: str
    extractor_version: str


@dataclass(frozen=True)
class ParsedNode:
    local_id: str
    kind: str
    name: str
    qualified_name: str
    qualified_style: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    provenance: str
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ParsedEdge:
    source_local_id: str
    target_local_id: str
    relation: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    provenance: str


@dataclass(frozen=True)
class ParsedReference:
    source_local_id: str
    relation: str
    target_kind: str
    module: str
    name: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class ParsedGap:
    code: str
    message: str


@dataclass(frozen=True)
class ParsedExtraction:
    extractor_id: str
    extractor_version: str
    nodes: tuple[ParsedNode, ...]
    edges: tuple[ParsedEdge, ...]
    references: tuple[ParsedReference, ...]
    gaps: tuple[ParsedGap, ...]


@dataclass(frozen=True)
class BoundReference:
    source_id: str
    path: str
    content_hash: str
    extractor_id: str
    extractor_version: str
    relation: str
    target_kind: str
    module: str
    name: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class Extraction:
    nodes: tuple[IndexNode, ...]
    edges: tuple[IndexEdge, ...]
    gaps: tuple[CoverageGap, ...]
    references: tuple[BoundReference, ...] = ()


_EXTRACTOR_VERSION = "1"
_BUILTIN_NAMES = frozenset(dir(builtins))
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_MARKDOWN_CHECKBOX = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.+?)[ \t]*$")
_MARKDOWN_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_WORK_OWNER = re.compile(r"^[ \t]*Owner:[ \t]*(.+?)[ \t]*$", re.IGNORECASE)
_WORK_DEPENDS = re.compile(r"^[ \t]*Depends[ \t]+on:[ \t]*(.+?)[ \t]*$", re.IGNORECASE)
_WORK_SCOPE_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+`([^`]+)`[ \t]*$")
_YAML_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]*(.*))?$")


def extractor_for(source: SourceFile) -> ExtractorSpec:
    """Return the parser identity used in the durable cache key."""
    if source.text is None:
        return ExtractorSpec("binary", _EXTRACTOR_VERSION)
    suffix = PurePosixPath(source.path).suffix.casefold()
    extractor_id = {
        ".py": "python.ast",
        ".md": "markdown.structure",
        ".markdown": "markdown.structure",
        ".json": "json.stdlib",
        ".toml": "toml.stdlib",
        ".yaml": "yaml.conservative",
        ".yml": "yaml.conservative",
    }.get(suffix, "text.fallback")
    return ExtractorSpec(extractor_id, _EXTRACTOR_VERSION)


def parse_source(source: SourceFile) -> ParsedExtraction:
    """Parse one blob into a path-neutral, serializable artifact."""
    spec = extractor_for(source)
    builder = _Builder(source, spec)
    file_id = builder.node(
        kind="file",
        name="",
        qualified_name="",
        qualified_style="path",
        start_line=1,
        end_line=_line_count(source.text),
        start_byte=0,
        end_byte=len(source.data),
    )
    if source.text is None:
        builder.gap("BINARY_FILE", "file is not valid UTF-8 text")
    elif spec.extractor_id == "python.ast":
        _parse_python(source, file_id, builder)
    elif spec.extractor_id == "markdown.structure":
        _parse_markdown(source, file_id, builder)
    elif spec.extractor_id == "json.stdlib":
        _parse_config(source, file_id, "json", builder)
    elif spec.extractor_id == "toml.stdlib":
        _parse_config(source, file_id, "toml", builder)
    elif spec.extractor_id == "yaml.conservative":
        _parse_yaml(source, file_id, builder)
    else:
        builder.gap(
            "UNSUPPORTED_PARSER",
            "no structural parser for this text format",
        )
    return builder.build()


def extract_files(files: Sequence[SourceFile]) -> Extraction:
    """Parse and assemble a complete manifest without using a durable cache."""
    parsed = tuple(parse_source(source) for source in files)
    return assemble_extractions(files, parsed)


def assemble_extractions(
    files: Sequence[SourceFile], parsed: Sequence[ParsedExtraction]
) -> Extraction:
    """Bind cached artifacts to paths and conservatively resolve references."""
    if len(files) != len(parsed):
        raise ValueError("source and parsed extraction counts differ")
    bound = tuple(
        _materialize(source, artifact) for source, artifact in zip(files, parsed)
    )
    nodes = [node for extraction in bound for node in extraction.nodes]
    edges = [edge for extraction in bound for edge in extraction.edges]
    gaps = [gap for extraction in bound for gap in extraction.gaps]
    references = [
        reference for extraction in bound for reference in extraction.references
    ]

    module_nodes: dict[str, list[IndexNode]] = {}
    symbols: dict[tuple[str, str], list[IndexNode]] = {}
    for node in nodes:
        if PurePosixPath(node.path).suffix.casefold() != ".py":
            continue
        module_name = _module_name(node.path)
        if node.kind == "file":
            module_nodes.setdefault(module_name, []).append(node)
        elif node.kind in {"function", "test", "class", "test_suite"}:
            symbols.setdefault((module_name, node.name), []).append(node)

    for reference in references:
        candidates: Sequence[IndexNode]
        if reference.target_kind == "module":
            candidates = _module_candidates(
                _absolute_module(reference.path, reference.module), module_nodes
            )
        elif reference.target_kind == "local":
            candidates = symbols.get((_module_name(reference.path), reference.name), ())
        elif reference.target_kind == "symbol":
            module = _absolute_module(reference.path, reference.module)
            candidates = symbols.get((module, reference.name), ())
        else:
            candidates = ()
        if len(candidates) == 1:
            edges.append(
                _edge(
                    source_id=reference.source_id,
                    target_id=candidates[0].node_id,
                    relation=reference.relation,
                    path=reference.path,
                    start_line=reference.start_line,
                    end_line=reference.end_line,
                    start_byte=reference.start_byte,
                    end_byte=reference.end_byte,
                    content_hash=reference.content_hash,
                    extractor_id=reference.extractor_id,
                    extractor_version=reference.extractor_version,
                    provenance="resolved",
                )
            )
        else:
            target = reference.module
            if reference.name:
                target = f"{target}.{reference.name}" if target else reference.name
            gaps.append(
                CoverageGap(
                    reference.path,
                    "PYTHON_UNRESOLVED_REFERENCE",
                    f"reference at line {reference.start_line} could not be resolved: {target}",
                )
            )

    unique_nodes = {node.node_id: node for node in nodes}
    unique_edges = {edge.edge_id: edge for edge in edges}
    return Extraction(
        tuple(sorted(unique_nodes.values(), key=_node_sort_key)),
        tuple(sorted(unique_edges.values(), key=_edge_sort_key)),
        tuple(sorted(set(gaps), key=lambda gap: (gap.path, gap.code, gap.message))),
    )


def serialize_parsed_extraction(value: ParsedExtraction) -> str:
    """Encode an artifact without adding path-dependent data."""
    return json.dumps(
        asdict(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def deserialize_parsed_extraction(payload: str) -> ParsedExtraction:
    raw = json.loads(payload)
    return ParsedExtraction(
        extractor_id=str(raw["extractor_id"]),
        extractor_version=str(raw["extractor_version"]),
        nodes=tuple(
            ParsedNode(
                local_id=str(item["local_id"]),
                kind=str(item["kind"]),
                name=str(item["name"]),
                qualified_name=str(item["qualified_name"]),
                qualified_style=str(item["qualified_style"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                start_byte=int(item["start_byte"]),
                end_byte=int(item["end_byte"]),
                provenance=str(item["provenance"]),
                attributes=tuple(
                    (str(key), str(value)) for key, value in item["attributes"]
                ),
            )
            for item in raw["nodes"]
        ),
        edges=tuple(ParsedEdge(**item) for item in raw["edges"]),
        references=tuple(ParsedReference(**item) for item in raw["references"]),
        gaps=tuple(ParsedGap(**item) for item in raw["gaps"]),
    )


class _Builder:
    def __init__(self, source: SourceFile, spec: ExtractorSpec) -> None:
        self.source = source
        self.spec = spec
        self.offsets = _ByteOffsets(source)
        self.nodes: list[ParsedNode] = []
        self.edges: list[ParsedEdge] = []
        self.references: list[ParsedReference] = []
        self.gaps: list[ParsedGap] = []

    def node(
        self,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        qualified_style: str,
        start_line: int,
        end_line: int,
        start_byte: int,
        end_byte: int,
        provenance: str = "observed",
        attributes: tuple[tuple[str, str], ...] = (),
    ) -> str:
        local_id = f"n{len(self.nodes)}"
        self.nodes.append(
            ParsedNode(
                local_id,
                kind,
                name,
                qualified_name,
                qualified_style,
                start_line,
                end_line,
                start_byte,
                end_byte,
                provenance,
                tuple(sorted(attributes)),
            )
        )
        return local_id

    def edge(
        self,
        source_local_id: str,
        target_local_id: str,
        relation: str,
        span: tuple[int, int, int, int],
        provenance: str = "observed",
    ) -> None:
        self.edges.append(
            ParsedEdge(source_local_id, target_local_id, relation, *span, provenance)
        )

    def reference(
        self,
        source_local_id: str,
        relation: str,
        target_kind: str,
        module: str,
        name: str,
        span: tuple[int, int, int, int],
    ) -> None:
        self.references.append(
            ParsedReference(source_local_id, relation, target_kind, module, name, *span)
        )

    def gap(self, code: str, message: str) -> None:
        self.gaps.append(ParsedGap(code, message))

    def build(self) -> ParsedExtraction:
        return ParsedExtraction(
            self.spec.extractor_id,
            self.spec.extractor_version,
            tuple(self.nodes),
            tuple(self.edges),
            tuple(self.references),
            tuple(sorted(set(self.gaps), key=lambda gap: (gap.code, gap.message))),
        )


class _ByteOffsets:
    def __init__(self, source: SourceFile) -> None:
        text = source.text or ""
        self.lines = text.splitlines(keepends=True) or [""]
        prefix = len(source.data) - len(text.encode("utf-8"))
        position = max(prefix, 0)
        self.starts: list[int] = []
        for line in self.lines:
            self.starts.append(position)
            position += len(line.encode("utf-8"))

    def line_span(
        self, line_number: int, start_column: int = 0, end_column: int | None = None
    ) -> tuple[int, int, int, int]:
        index = max(0, min(line_number - 1, len(self.lines) - 1))
        visible = self.lines[index].rstrip("\r\n")
        if end_column is None:
            end_column = len(visible)
        start_byte = self.starts[index] + len(visible[:start_column].encode("utf-8"))
        end_byte = self.starts[index] + len(visible[:end_column].encode("utf-8"))
        return line_number, line_number, start_byte, end_byte

    def block_span(self, start_line: int, end_line: int) -> tuple[int, int, int, int]:
        _, _, start_byte, _ = self.line_span(start_line)
        _, _, _, end_byte = self.line_span(end_line)
        return start_line, end_line, start_byte, end_byte

    def ast_span(self, node: ast.AST) -> tuple[int, int, int, int]:
        start_line = int(getattr(node, "lineno", 1))
        end_line = int(getattr(node, "end_lineno", start_line))
        start = self.starts[start_line - 1] + int(getattr(node, "col_offset", 0))
        end = self.starts[end_line - 1] + int(getattr(node, "end_col_offset", 0))
        return start_line, end_line, start, end


def _parse_python(source: SourceFile, file_id: str, builder: _Builder) -> None:
    assert source.text is not None
    try:
        tree = ast.parse(source.text, filename="<project-index-blob>")
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None)
        detail = f" at line {line}" if line else ""
        builder.gap("PYTHON_PARSE_ERROR", f"Python parse failed{detail}")
        return

    module_aliases: dict[str, str] = {}
    symbol_aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", 1)[0]
                module_aliases[binding] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name != "*":
                    symbol_aliases[alias.asname or alias.name] = (module, alias.name)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.parents: list[str] = [file_id]
            self.names: list[str] = []
            self.call_owners: list[str | None] = [None]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_definition(
                node, "test_suite" if node.name.startswith("Test") else "class"
            )

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_definition(
                node, "test" if node.name.startswith("test_") else "function"
            )

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_definition(
                node, "test" if node.name.startswith("test_") else "function"
            )

        def _visit_definition(
            self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str
        ) -> None:
            span = builder.offsets.ast_span(node)
            item = builder.node(
                kind=kind,
                name=node.name,
                qualified_name=".".join((*self.names, node.name)),
                qualified_style="module",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
            )
            builder.edge(self.parents[-1], item, "contains", span)
            self.parents.append(item)
            self.names.append(node.name)
            self.call_owners.append(item)
            self.generic_visit(node)
            self.call_owners.pop()
            self.names.pop()
            self.parents.pop()

        def visit_Import(self, node: ast.Import) -> None:
            span = builder.offsets.ast_span(node)
            for alias in node.names:
                self._record_import(alias.name, span)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            span = builder.offsets.ast_span(node)
            self._record_import("." * node.level + (node.module or ""), span)

        def _record_import(self, module: str, span: tuple[int, int, int, int]) -> None:
            item = builder.node(
                kind="import",
                name=module,
                qualified_name=f"import:{module}@{span[0]}",
                qualified_style="module",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
            )
            builder.edge(self.parents[-1], item, "contains", span)
            builder.reference(file_id, "imports", "module", module, "", span)

        def visit_Call(self, node: ast.Call) -> None:
            owner = self.call_owners[-1]
            if owner is not None:
                span = builder.offsets.ast_span(node)
                target = _python_call_target(node.func, module_aliases, symbol_aliases)
                if target is None:
                    builder.gap(
                        "PYTHON_DYNAMIC_REFERENCE",
                        f"dynamic call at line {span[0]} was not resolved",
                    )
                elif target[0] != "builtin":
                    builder.reference(
                        owner, "calls", target[0], target[1], target[2], span
                    )
            self.generic_visit(node)

    Visitor().visit(tree)


def _python_call_target(
    node: ast.expr,
    module_aliases: Mapping[str, str],
    symbol_aliases: Mapping[str, tuple[str, str]],
) -> tuple[str, str, str] | None:
    if isinstance(node, ast.Name):
        if node.id in symbol_aliases:
            module, name = symbol_aliases[node.id]
            return "symbol", module, name
        if node.id in _BUILTIN_NAMES:
            return "builtin", "", node.id
        return "local", "", node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id in module_aliases:
            parts.reverse()
            module = module_aliases[current.id]
            if len(parts) > 1:
                module = ".".join((module, *parts[:-1]))
            return "symbol", module, parts[-1]
        return None
    return None


def _parse_markdown(source: SourceFile, file_id: str, builder: _Builder) -> None:
    assert source.text is not None
    lines = source.text.splitlines()
    frontmatter_end = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() in {"---", "..."}:
                frontmatter_end = index + 1
                break
        if frontmatter_end:
            for line_number in range(2, frontmatter_end):
                match = _YAML_TOP_LEVEL_KEY.match(lines[line_number - 1])
                if not match:
                    continue
                span = builder.offsets.line_span(
                    line_number, match.start(1), match.end(1)
                )
                key = match.group(1)
                value = (match.group(2) or "").strip()
                item = builder.node(
                    kind="frontmatter_key",
                    name=key,
                    qualified_name=key,
                    qualified_style="literal",
                    start_line=span[0],
                    end_line=span[1],
                    start_byte=span[2],
                    end_byte=span[3],
                    provenance="declared",
                    attributes=(("value", value),),
                )
                builder.edge(file_id, item, "contains", span, "declared")

    heading_stack: list[tuple[int, str]] = []
    current_parent = file_id
    in_write_scope = False
    fence: tuple[str, int, str, int, str] | None = None
    for line_number, line in enumerate(lines, 1):
        if line_number <= frontmatter_end:
            continue
        stripped = line.lstrip(" ")
        indentation = len(line) - len(stripped)
        fence_match = _MARKDOWN_FENCE.match(stripped) if indentation <= 3 else None
        if fence is not None:
            marker, marker_size, language, start_line, parent = fence
            if (
                fence_match
                and fence_match.group(1)[0] == marker
                and len(fence_match.group(1)) >= marker_size
            ):
                span = builder.offsets.block_span(start_line, line_number)
                item = builder.node(
                    kind="code_fence",
                    name=language or "code",
                    qualified_name=f"fence@{start_line}",
                    qualified_style="path_prefix",
                    start_line=span[0],
                    end_line=span[1],
                    start_byte=span[2],
                    end_byte=span[3],
                    attributes=(("language", language),),
                )
                builder.edge(parent, item, "contains", span)
                fence = None
            continue
        if fence_match:
            marker_text = fence_match.group(1)
            fence = (
                marker_text[0],
                len(marker_text),
                fence_match.group(2).strip().split(None, 1)[0]
                if fence_match.group(2).strip()
                else "",
                line_number,
                current_parent,
            )
            continue

        heading_match = _MARKDOWN_HEADING.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            name = heading_match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parent = heading_stack[-1][1] if heading_stack else file_id
            span = builder.offsets.line_span(line_number)
            item = builder.node(
                kind="heading",
                name=name,
                qualified_name=f"#{_slug(name)}",
                qualified_style="path_prefix",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
                provenance="declared",
                attributes=(("level", str(level)),),
            )
            builder.edge(parent, item, "contains", span, "declared")
            heading_stack.append((level, item))
            current_parent = item
            in_write_scope = name.casefold() == "write scope"
            continue

        owner_match = _WORK_OWNER.match(line)
        if owner_match:
            value = owner_match.group(1).strip()
            span = builder.offsets.line_span(
                line_number, owner_match.start(1), owner_match.end(1)
            )
            item = builder.node(
                kind="work_package_owner",
                name=value,
                qualified_name=f"owner:{value}",
                qualified_style="literal",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
                provenance="declared",
                attributes=(("field", "Owner"),),
            )
            builder.edge(current_parent, item, "declares", span, "declared")

        depends_match = _WORK_DEPENDS.match(line)
        if depends_match:
            raw_dependencies = depends_match.group(1)
            search_from = depends_match.start(1)
            for dependency in (
                part for part in re.split(r"[\s,]+", raw_dependencies) if part
            ):
                start_column = line.find(dependency, search_from)
                search_from = start_column + len(dependency)
                span = builder.offsets.line_span(
                    line_number, start_column, start_column + len(dependency)
                )
                item = builder.node(
                    kind="work_package_dependency",
                    name=dependency,
                    qualified_name=f"depends:{dependency}",
                    qualified_style="literal",
                    start_line=span[0],
                    end_line=span[1],
                    start_byte=span[2],
                    end_byte=span[3],
                    provenance="declared",
                    attributes=(("field", "Depends on"),),
                )
                builder.edge(current_parent, item, "declares", span, "declared")

        if in_write_scope:
            scope_match = _WORK_SCOPE_ITEM.match(line)
            if scope_match:
                value = scope_match.group(1).strip().replace("\\", "/")
                span = builder.offsets.line_span(
                    line_number, scope_match.start(1), scope_match.end(1)
                )
                item = builder.node(
                    kind="work_package_write_scope",
                    name=value,
                    qualified_name=f"write-scope:{value}",
                    qualified_style="literal",
                    start_line=span[0],
                    end_line=span[1],
                    start_byte=span[2],
                    end_byte=span[3],
                    provenance="declared",
                    attributes=(("field", "Write Scope"),),
                )
                builder.edge(current_parent, item, "declares", span, "declared")

        checkbox_match = _MARKDOWN_CHECKBOX.match(line)
        if checkbox_match:
            name = checkbox_match.group(2).strip()
            span = builder.offsets.line_span(
                line_number, checkbox_match.start(), checkbox_match.end()
            )
            item = builder.node(
                kind="checkbox",
                name=name,
                qualified_name=f"checkbox@{line_number}",
                qualified_style="path_prefix",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
                provenance="declared",
                attributes=(
                    ("checked", str(checkbox_match.group(1).casefold() == "x").lower()),
                ),
            )
            builder.edge(current_parent, item, "contains", span, "declared")

        for link_match in _MARKDOWN_LINK.finditer(line):
            label, target = link_match.groups()
            span = builder.offsets.line_span(
                line_number, link_match.start(), link_match.end()
            )
            item = builder.node(
                kind="link",
                name=label.strip(),
                qualified_name=target,
                qualified_style="literal",
                start_line=span[0],
                end_line=span[1],
                start_byte=span[2],
                end_byte=span[3],
                provenance="declared",
                attributes=(("target", target),),
            )
            builder.edge(current_parent, item, "links", span, "declared")

    if fence is not None:
        _, _, language, start_line, parent = fence
        end_line = max(start_line, len(lines))
        span = builder.offsets.block_span(start_line, end_line)
        item = builder.node(
            kind="code_fence",
            name=language or "code",
            qualified_name=f"fence@{start_line}",
            qualified_style="path_prefix",
            start_line=span[0],
            end_line=span[1],
            start_byte=span[2],
            end_byte=span[3],
            attributes=(("language", language),),
        )
        builder.edge(parent, item, "contains", span)
        builder.gap(
            "MARKDOWN_UNCLOSED_FENCE", f"code fence at line {start_line} is not closed"
        )


def _parse_config(
    source: SourceFile, file_id: str, parser: str, builder: _Builder
) -> None:
    assert source.text is not None
    try:
        value = (
            json.loads(source.text) if parser == "json" else tomllib.loads(source.text)
        )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError):
        builder.gap(f"{parser.upper()}_PARSE_ERROR", f"{parser.upper()} parse failed")
        return
    if not isinstance(value, Mapping):
        return
    locations = _config_locations(source.text, parser)
    parent_ids: dict[str, str] = {"": file_id}
    for qualified_name, key in _walk_config_keys(value):
        parent_name = qualified_name.rpartition(".")[0]
        location = locations.get(qualified_name, locations.get(key, (1, 0, len(key))))
        span = builder.offsets.line_span(*location)
        item = builder.node(
            kind="config_key",
            name=key,
            qualified_name=qualified_name,
            qualified_style="literal",
            start_line=span[0],
            end_line=span[1],
            start_byte=span[2],
            end_byte=span[3],
            provenance="declared",
        )
        builder.edge(
            parent_ids.get(parent_name, file_id), item, "contains", span, "declared"
        )
        parent_ids[qualified_name] = item


def _parse_yaml(source: SourceFile, file_id: str, builder: _Builder) -> None:
    assert source.text is not None
    for line_number, line in enumerate(source.text.splitlines(), 1):
        match = _YAML_TOP_LEVEL_KEY.match(line)
        if not match:
            continue
        key = match.group(1)
        span = builder.offsets.line_span(line_number, match.start(1), match.end(1))
        item = builder.node(
            kind="config_key",
            name=key,
            qualified_name=key,
            qualified_style="literal",
            start_line=span[0],
            end_line=span[1],
            start_byte=span[2],
            end_byte=span[3],
            provenance="declared",
        )
        builder.edge(file_id, item, "contains", span, "declared")
    builder.gap(
        "YAML_PARTIAL", "only explicit top-level plain mapping keys were indexed"
    )


def _materialize(source: SourceFile, parsed: ParsedExtraction) -> Extraction:
    spec = extractor_for(source)
    if (parsed.extractor_id, parsed.extractor_version) != (
        spec.extractor_id,
        spec.extractor_version,
    ):
        raise ValueError("cached parser identity does not match source")
    node_ids: dict[str, str] = {}
    nodes: list[IndexNode] = []
    for item in parsed.nodes:
        name = PurePosixPath(source.path).name if item.kind == "file" else item.name
        qualified_name = _bound_qualified_name(source.path, item)
        node = _node(
            kind=item.kind,
            path=source.path,
            name=name,
            qualified_name=qualified_name,
            start_line=item.start_line,
            end_line=item.end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            content_hash=source.content_hash,
            extractor_id=parsed.extractor_id,
            extractor_version=parsed.extractor_version,
            provenance=item.provenance,
            attributes=item.attributes,
        )
        node_ids[item.local_id] = node.node_id
        nodes.append(node)
    edges = tuple(
        _edge(
            source_id=node_ids[item.source_local_id],
            target_id=node_ids[item.target_local_id],
            relation=item.relation,
            path=source.path,
            start_line=item.start_line,
            end_line=item.end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            content_hash=source.content_hash,
            extractor_id=parsed.extractor_id,
            extractor_version=parsed.extractor_version,
            provenance=item.provenance,
        )
        for item in parsed.edges
    )
    references = tuple(
        BoundReference(
            source_id=node_ids[item.source_local_id],
            path=source.path,
            content_hash=source.content_hash,
            extractor_id=parsed.extractor_id,
            extractor_version=parsed.extractor_version,
            relation=item.relation,
            target_kind=item.target_kind,
            module=item.module,
            name=item.name,
            start_line=item.start_line,
            end_line=item.end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
        )
        for item in parsed.references
    )
    gaps = tuple(CoverageGap(source.path, gap.code, gap.message) for gap in parsed.gaps)
    return Extraction(tuple(nodes), edges, gaps, references)


def _bound_qualified_name(path: str, node: ParsedNode) -> str:
    if node.qualified_style == "path":
        return path
    if node.qualified_style == "path_prefix":
        return f"{path}{node.qualified_name}"
    if node.qualified_style == "module":
        module = _module_name(path)
        return (
            f"{module}.{node.qualified_name}"
            if module and node.qualified_name
            else module or node.qualified_name
        )
    return node.qualified_name


def _node(
    *,
    kind: str,
    path: str,
    name: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    start_byte: int,
    end_byte: int,
    content_hash: str,
    extractor_id: str,
    extractor_version: str,
    provenance: str,
    attributes: tuple[tuple[str, str], ...] = (),
) -> IndexNode:
    payload = (
        "node",
        kind,
        path,
        name,
        qualified_name,
        start_line,
        end_line,
        start_byte,
        end_byte,
        content_hash,
        extractor_id,
        extractor_version,
        provenance,
        attributes,
    )
    return IndexNode(
        node_id=_identifier(payload),
        kind=kind,
        path=path,
        name=name,
        qualified_name=qualified_name,
        start_line=start_line,
        end_line=end_line,
        content_hash=content_hash,
        attributes=attributes,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        provenance=provenance,
        start_byte=start_byte,
        end_byte=end_byte,
    )


def _edge(
    *,
    source_id: str,
    target_id: str,
    relation: str,
    path: str,
    start_line: int,
    end_line: int,
    start_byte: int,
    end_byte: int,
    content_hash: str,
    extractor_id: str,
    extractor_version: str,
    provenance: str,
) -> IndexEdge:
    payload = (
        "edge",
        source_id,
        target_id,
        relation,
        path,
        start_line,
        end_line,
        start_byte,
        end_byte,
        content_hash,
        extractor_id,
        extractor_version,
        provenance,
    )
    return IndexEdge(
        edge_id=_identifier(payload),
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        path=path,
        start_line=start_line,
        end_line=end_line,
        start_byte=start_byte,
        end_byte=end_byte,
        content_hash=content_hash,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        provenance=provenance,
    )


def _identifier(parts: object) -> str:
    payload = json.dumps(
        parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _walk_config_keys(
    value: Mapping[str, Any], prefix: str = ""
) -> Iterable[tuple[str, str]]:
    for raw_key in sorted(value, key=str):
        key = str(raw_key)
        qualified = f"{prefix}.{key}" if prefix else key
        yield qualified, key
        child = value[raw_key]
        if isinstance(child, Mapping):
            yield from _walk_config_keys(child, qualified)


def _config_locations(text: str, parser: str) -> dict[str, tuple[int, int, int]]:
    locations: dict[str, tuple[int, int, int]] = {}
    if parser == "json":
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(r'"((?:[^"\\]|\\.)+)"\s*:', line):
                try:
                    key = str(json.loads(f'"{match.group(1)}"'))
                except json.JSONDecodeError:
                    continue
                locations.setdefault(key, (line_number, match.start(1), match.end(1)))
        return locations

    table = ""
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        table_match = re.match(r"^\[([^\[\]]+)\]$", stripped)
        if table_match:
            table = table_match.group(1).strip()
            column = line.find(table_match.group(1))
            parts = table.split(".")
            for index in range(1, len(parts) + 1):
                locations.setdefault(
                    ".".join(parts[:index]), (line_number, column, column + len(table))
                )
            continue
        key_match = re.match(r"^([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*=", stripped)
        if key_match:
            key = key_match.group(1)
            column = line.find(key)
            qualified = f"{table}.{key}" if table else key
            location = (line_number, column, column + len(key))
            locations.setdefault(qualified, location)
            locations.setdefault(key.rsplit(".", 1)[-1], location)
    return locations


def _module_name(path: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute_module(path: str, module: str) -> str:
    if not module.startswith("."):
        return module
    level = len(module) - len(module.lstrip("."))
    remainder = module[level:]
    current = _module_name(path).split(".")
    if PurePosixPath(path).name != "__init__.py" and current:
        current.pop()
    if level > 1:
        current = current[: max(0, len(current) - level + 1)]
    return ".".join((*current, *((remainder,) if remainder else ())))


def _module_candidates(
    module: str, module_nodes: Mapping[str, Sequence[IndexNode]]
) -> Sequence[IndexNode]:
    return module_nodes.get(module, ())


def _line_count(text: str | None) -> int:
    if not text:
        return 1
    return len(text.splitlines()) or 1


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _node_sort_key(node: IndexNode) -> tuple[object, ...]:
    return (
        node.path,
        node.start_byte,
        node.end_byte,
        node.kind,
        node.qualified_name,
        node.node_id,
    )


def _edge_sort_key(edge: IndexEdge) -> tuple[object, ...]:
    return (
        edge.path,
        edge.start_byte,
        edge.end_byte,
        edge.relation,
        edge.source_id,
        edge.target_id,
    )
