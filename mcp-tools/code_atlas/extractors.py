"""Language-neutral, deterministic extraction and Task-6-only rendering."""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from project_index.checkpoints import CheckpointFile
from project_index.models import CoverageGap, IndexNode, SnapshotFile

from .canonical import canonical_hash, normalize_intent_id, thaw_json
from .models import (
    AtlasEdge,
    AtlasError,
    AtlasNode,
    ConstraintSpec,
    EdgeRelation,
    ExtractionGap,
    ExtractionResult,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
)
from .security import (
    MAX_CHANGED_FILES,
    MAX_COMMAND_SPEC_BYTES,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_RECIPE_BYTES,
    MAX_SLOT_COUNT,
    MAX_TEMPLATE_BYTES,
    validate_candidate_path,
    validate_fragment,
    validate_slot_value,
)


_GAP_PRIORITY = {
    "SNAPSHOT_MISMATCH": 1,
    "VERIFICATION_FAILED": 2,
    "GENERATED_PATH": 3,
    "VENDORED_PATH": 4,
    "BINARY_CONTENT": 5,
    "SIZE_LIMIT": 6,
    "SECRET_BEARING": 7,
    "UNSUPPORTED_LANGUAGE": 8,
    "PARSER_GAP": 9,
    "UNSUPPORTED_EDIT_SHAPE": 10,
}
_RENDER_ERROR = "invalid_render_bindings"
_HASH_TEXT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLOT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class BoundExecutionReceipt:
    """A redacted receipt already bound to a single accepted task output."""

    receipt_id: str
    kind: str
    workflow_id: str
    task_id: str
    acceptance_id: str
    workspace_hash: str
    output_snapshot_id: str
    command_spec: tuple[str, ...]
    command_spec_hash: str
    input_hash: str
    output_hash: str
    exit_code: int
    success: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_spec", tuple(self.command_spec))


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    """All immutable facts the language-neutral extraction boundary receives."""

    workflow_id: str
    task_id: str
    acceptance_id: str
    task_kind: str
    intent_id: str
    workspace_hash: str
    checkpoint_id: str
    input_snapshot_id: str
    output_snapshot_id: str
    write_scope: tuple[str, ...]
    before_files: tuple[CheckpointFile, ...]
    after_files: tuple[SnapshotFile, ...]
    changed_nodes: tuple[IndexNode, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    execution_receipts: tuple[BoundExecutionReceipt, ...]

    def __post_init__(self) -> None:
        for name in (
            "write_scope",
            "before_files",
            "after_files",
            "changed_nodes",
            "coverage_gaps",
            "execution_receipts",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


class RecipeExtractor(Protocol):
    """Stable language extractor protocol for present and future languages."""

    extractor_id: str
    extractor_version: str
    languages: frozenset[str]

    @abstractmethod
    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _ObservedFile:
    path: str
    body: bytes | None
    text: str | None
    actual_hash: str
    claimed_hash: str | None


@dataclass(frozen=True, slots=True)
class _Symbol:
    byte_start: int
    name: str


@dataclass(frozen=True, slots=True)
class _OperationDraft:
    path: str
    kind: str
    text: str
    symbols: tuple[_Symbol, ...]


@dataclass(frozen=True, slots=True)
class _EpisodeArtifacts:
    episode: AtlasNode
    intent: AtlasNode
    source_evidence: tuple[AtlasNode, ...]
    receipt_nodes: tuple[AtlasNode, ...]
    verification_nodes: tuple[AtlasNode, ...]
    nodes: tuple[AtlasNode, ...]
    edges: tuple[AtlasEdge, ...]


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_TEXT.fullmatch(value) is not None


def _normalize_relative_path(value: object) -> str | None:
    """Return a data-only normalized relative path without touching the workspace."""

    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        return None
    return "/".join(parts)


def _path_is_within_scope(path: str, scope: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in scope)


def _path_classification(path: str) -> str | None:
    parts = tuple(part.casefold() for part in path.split("/"))
    if "vendor" in parts:
        return "VENDORED_PATH"
    try:
        validate_candidate_path(path)
    except AtlasError as error:
        if error.code == "generated_path":
            return "GENERATED_PATH"
        return "UNSUPPORTED_EDIT_SHAPE"
    return None


def _safe_gap(
    gaps: list[ExtractionGap],
    code: str,
    *,
    path: str | None = None,
    detail: str | None = None,
) -> None:
    safe_path = _normalize_relative_path(path) if path is not None else None
    gaps.append(ExtractionGap(code, safe_path, detail))


def _ordered_gaps(gaps: Sequence[ExtractionGap]) -> tuple[ExtractionGap, ...]:
    return tuple(
        sorted(
            gaps,
            key=lambda gap: (
                _GAP_PRIORITY.get(gap.code, len(_GAP_PRIORITY) + 1),
                gap.path or "",
                gap.detail or "",
                gap.code,
            ),
        )
    )


def _body_text(
    body: object, *, path: str, gaps: list[ExtractionGap]
) -> tuple[bytes | None, str | None]:
    if not isinstance(body, bytes):
        _safe_gap(gaps, "BINARY_CONTENT", path=path, detail="non_bytes")
        return None, None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        _safe_gap(gaps, "BINARY_CONTENT", path=path, detail="invalid_utf8")
        return body, None
    try:
        validate_fragment(text, max_bytes=max(len(body), MAX_TEMPLATE_BYTES))
    except AtlasError as error:
        if error.code == "credential_detected":
            _safe_gap(gaps, "SECRET_BEARING", path=path)
        else:
            _safe_gap(gaps, "BINARY_CONTENT", path=path, detail="unsafe_content")
        return body, None
    return body, text


def _observe_files(
    files: Sequence[object], *, gaps: list[ExtractionGap]
) -> dict[str, _ObservedFile]:
    observed: dict[str, _ObservedFile] = {}
    for item in files:
        raw_path = getattr(item, "path", None)
        path = _normalize_relative_path(raw_path)
        if path is None:
            _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="unsafe_path")
            continue
        if path in observed:
            _safe_gap(
                gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="duplicate_path"
            )
            continue
        claimed_hash = getattr(item, "content_hash", None)
        claimed = claimed_hash if isinstance(claimed_hash, str) else None
        body, text = _body_text(getattr(item, "body", None), path=path, gaps=gaps)
        actual = _sha256(body) if body is not None else ""
        if body is None or claimed != actual:
            _safe_gap(gaps, "SNAPSHOT_MISMATCH", path=path)
        observed[path] = _ObservedFile(path, body, text, actual, claimed)
    return observed


def _normalize_scope(
    values: Sequence[object], *, gaps: list[ExtractionGap]
) -> tuple[str, ...]:
    scope: list[str] = []
    for value in values:
        normalized = _normalize_relative_path(value)
        if normalized is None:
            _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="unsafe_scope")
            continue
        scope.append(normalized)
    if not scope:
        _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="empty_scope")
        return ()
    if len(set(scope)) != len(scope):
        _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="duplicate_scope")
    return tuple(sorted(scope))


def _binding_files(files: Sequence[object]) -> list[list[str]] | None:
    values: list[list[str]] = []
    for item in files:
        path = _normalize_relative_path(getattr(item, "path", None))
        content_hash = getattr(item, "content_hash", None)
        if path is None or not isinstance(content_hash, str):
            return None
        values.append([path, content_hash])
    return sorted(values)


def _binding_hash(
    *,
    kind: str,
    request: ExtractionRequest,
    snapshot_id: object,
    scope: tuple[str, ...],
    files: Sequence[object],
) -> str | None:
    file_values = _binding_files(files)
    if file_values is None or not isinstance(snapshot_id, str):
        return None
    return canonical_hash(
        {
            "kind": kind,
            "workflow_id": request.workflow_id,
            "task_id": request.task_id,
            "acceptance_id": request.acceptance_id,
            "workspace_hash": request.workspace_hash,
            "checkpoint_id": request.checkpoint_id,
            "snapshot_id": snapshot_id,
            "write_scope": list(scope),
            "files": file_values,
        }
    )


def _valid_command_spec(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, tuple):
        return None
    if any(not isinstance(part, str) or not part for part in value):
        return None
    try:
        byte_count = sum(len(part.encode("utf-8")) for part in value)
    except UnicodeError:
        return None
    if byte_count > MAX_COMMAND_SPEC_BYTES:
        return None
    return value


def _receipt_command_spec(value: object, *, kind: object) -> tuple[str, ...] | None:
    spec = _valid_command_spec(value)
    if spec is None or kind not in {"command", "write"}:
        return None
    if kind == "command" and not spec:
        return None
    return spec


def _metadata_exceeds_graph_budget(request: ExtractionRequest) -> bool:
    """Bound all task metadata before any graph fan-out is materialized."""

    scope_count = len(request.write_scope)
    before_count = len(request.before_files)
    after_count = len(request.after_files)
    changed_count = len(request.changed_nodes)
    coverage_count = len(request.coverage_gaps)
    receipt_count = len(request.execution_receipts)
    metadata_count = (
        scope_count
        + before_count
        + after_count
        + changed_count
        + coverage_count
        + receipt_count
    )
    if metadata_count > MAX_GRAPH_NODES:
        return True

    path_upper_bound = before_count + after_count
    source_upper_bound = 1 + path_upper_bound + changed_count
    receipt_node_upper_bound = 1 + receipt_count
    verification_upper_bound = 1 + receipt_count
    episode_node_upper_bound = (
        2 + source_upper_bound + receipt_node_upper_bound + verification_upper_bound
    )
    episode_edge_upper_bound = (
        1
        + source_upper_bound
        + receipt_node_upper_bound
        + verification_upper_bound * (1 + source_upper_bound)
    )
    recipe_node_upper_bound = 2 + path_upper_bound + (2 * MAX_SLOT_COUNT)
    recipe_edge_upper_bound = (
        3
        + source_upper_bound
        + path_upper_bound
        + (2 * MAX_SLOT_COUNT)
        + receipt_node_upper_bound
        + verification_upper_bound
    )
    return (
        episode_node_upper_bound + recipe_node_upper_bound > MAX_GRAPH_NODES
        or episode_edge_upper_bound + recipe_edge_upper_bound > MAX_GRAPH_EDGES
    )


def _validate_receipts(
    request: ExtractionRequest,
    *,
    scope: tuple[str, ...],
    gaps: list[ExtractionGap],
) -> None:
    input_hash = _binding_hash(
        kind="atlas-extraction-input-v1",
        request=request,
        snapshot_id=request.input_snapshot_id,
        scope=scope,
        files=request.before_files,
    )
    output_hash = _binding_hash(
        kind="atlas-extraction-output-v1",
        request=request,
        snapshot_id=request.output_snapshot_id,
        scope=scope,
        files=request.after_files,
    )
    command_count = 0
    write_count = 0
    for receipt in request.execution_receipts:
        if not isinstance(receipt, BoundExecutionReceipt):
            _safe_gap(gaps, "VERIFICATION_FAILED", detail="invalid_receipt")
            continue
        raw_spec = _valid_command_spec(receipt.command_spec)
        spec = _receipt_command_spec(receipt.command_spec, kind=receipt.kind)
        binding_mismatch = (
            receipt.workflow_id != request.workflow_id
            or receipt.task_id != request.task_id
            or receipt.acceptance_id != request.acceptance_id
            or receipt.workspace_hash != request.workspace_hash
            or receipt.output_snapshot_id != request.output_snapshot_id
            or input_hash is None
            or output_hash is None
            or receipt.input_hash != input_hash
            or receipt.output_hash != output_hash
            or raw_spec is None
            or (
                raw_spec is not None
                and receipt.command_spec_hash != canonical_hash(raw_spec)
            )
        )
        if binding_mismatch:
            _safe_gap(gaps, "SNAPSHOT_MISMATCH", detail="receipt_binding")
        if receipt.kind == "command":
            command_count += 1
        elif receipt.kind == "write":
            write_count += 1
        else:
            _safe_gap(gaps, "VERIFICATION_FAILED", detail="receipt_kind")
        if (
            receipt.success is not True
            or type(receipt.exit_code) is not int
            or receipt.exit_code != 0
        ):
            _safe_gap(gaps, "VERIFICATION_FAILED", detail="receipt_result")
        if spec is None:
            _safe_gap(gaps, "VERIFICATION_FAILED", detail="command_spec")
    if command_count == 0:
        _safe_gap(gaps, "VERIFICATION_FAILED", detail="missing_command")
    if write_count == 0:
        _safe_gap(gaps, "VERIFICATION_FAILED", detail="missing_write")


def _ast_tree(text: str, *, path: str, gaps: list[ExtractionGap]) -> ast.Module | None:
    try:
        return ast.parse(text, filename=path)
    except (SyntaxError, ValueError, TypeError):
        _safe_gap(gaps, "PARSER_GAP", path=path)
        return None


def _node_byte_start(text: str, node: ast.AST) -> int:
    line = getattr(node, "lineno", 1)
    column = getattr(node, "col_offset", 0)
    lines = text.splitlines(keepends=True)
    prefix = "".join(lines[: max(line - 1, 0)]).encode("utf-8")
    return len(prefix) + column


def _new_symbols(tree: ast.Module, text: str) -> tuple[_Symbol, ...]:
    symbols = [
        _Symbol(_node_byte_start(text, node), node.name)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return tuple(sorted(symbols, key=lambda symbol: (symbol.byte_start, symbol.name)))


def _append_draft(
    *,
    path: str,
    before: _ObservedFile,
    after: _ObservedFile,
    gaps: list[ExtractionGap],
) -> _OperationDraft | None:
    if before.body is None or after.body is None or after.text is None:
        return None
    if not after.body.startswith(before.body):
        _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="not_append")
        return None
    suffix = after.body[len(before.body) :]
    if not suffix:
        _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="noop")
        return None
    try:
        suffix_text = suffix.decode("utf-8")
    except UnicodeDecodeError:
        _safe_gap(gaps, "BINARY_CONTENT", path=path, detail="suffix_utf8")
        return None
    full_tree = _ast_tree(after.text, path=path, gaps=gaps)
    suffix_tree = _ast_tree(suffix_text, path=path, gaps=gaps)
    if full_tree is None or suffix_tree is None:
        return None
    if not suffix_tree.body or any(
        not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in suffix_tree.body
    ):
        _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="append_shape")
        return None
    return _OperationDraft(
        path=path,
        kind="append_python_nodes",
        text=suffix_text,
        symbols=_new_symbols(suffix_tree, suffix_text),
    )


def _create_draft(
    *, path: str, after: _ObservedFile, gaps: list[ExtractionGap]
) -> _OperationDraft | None:
    if after.text is None:
        return None
    tree = _ast_tree(after.text, path=path, gaps=gaps)
    if tree is None:
        return None
    return _OperationDraft(
        path=path,
        kind="create_python_file",
        text=after.text,
        symbols=_new_symbols(tree, after.text),
    )


def _gaps_for_coverage(
    gaps: list[ExtractionGap],
    coverage_gaps: Sequence[CoverageGap],
    touched_paths: set[str],
) -> None:
    for gap in coverage_gaps:
        path = _normalize_relative_path(getattr(gap, "path", None))
        if path is not None and path in touched_paths:
            _safe_gap(
                gaps,
                "PARSER_GAP",
                path=path,
                detail=canonical_hash({"code": getattr(gap, "code", "")}),
            )


def _substitute_names(text: str, replacements: Mapping[str, str]) -> str:
    if not replacements:
        return text
    offsets: list[int] = []
    total = 0
    for line in text.splitlines(keepends=True):
        offsets.append(total)
        total += len(line)
    if not offsets:
        offsets.append(0)
    spans: list[tuple[int, int, str]] = []
    stream = io.StringIO(text)
    try:
        tokens = tokenize.generate_tokens(stream.readline)
        for token in tokens:
            if token.type != tokenize.NAME or token.string not in replacements:
                continue
            start_line, start_column = token.start
            end_line, end_column = token.end
            if start_line < 1 or end_line < 1:
                raise ValueError("invalid token coordinates")
            start = offsets[start_line - 1] + start_column
            end = offsets[end_line - 1] + end_column
            spans.append((start, end, replacements[token.string]))
    except (tokenize.TokenError, IndentationError, ValueError) as error:
        raise AtlasError("invalid_template_tokens") from error
    output: list[str] = []
    cursor = 0
    for start, end, replacement in spans:
        if start < cursor or end < start:
            raise AtlasError("invalid_template_tokens")
        output.extend((text[cursor:start], replacement))
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


def _placeholder_names(text: str) -> set[str]:
    names: set[str] = set()
    cursor = 0
    while True:
        start = text.find("${", cursor)
        if start == -1:
            return names
        end = text.find("}", start + 2)
        if end == -1:
            raise AtlasError(_RENDER_ERROR)
        name = text[start + 2 : end]
        if "${" in name or _SLOT_NAME.fullmatch(name) is None:
            raise AtlasError(_RENDER_ERROR)
        names.add(name)
        cursor = end + 1


def _render_template(text: str, values: Mapping[str, str]) -> str:
    result: list[str] = []
    cursor = 0
    while True:
        start = text.find("${", cursor)
        if start == -1:
            result.append(text[cursor:])
            return "".join(result)
        end = text.find("}", start + 2)
        if end == -1:
            raise AtlasError(_RENDER_ERROR)
        name = text[start + 2 : end]
        if name not in values:
            raise AtlasError(_RENDER_ERROR)
        result.extend((text[cursor:start], values[name]))
        cursor = end + 1


def _recipe_payload(manifest: RecipeManifest) -> dict[str, Any]:
    payload = manifest.to_dict()
    del payload["recipe_id"]
    return payload


def _unique_nodes(nodes: Sequence[AtlasNode]) -> tuple[AtlasNode, ...]:
    return tuple(
        sorted(
            {node.node_id: node for node in nodes}.values(),
            key=lambda item: item.node_id,
        )
    )


def _unique_edges(edges: Sequence[AtlasEdge]) -> tuple[AtlasEdge, ...]:
    return tuple(
        sorted(
            {edge.edge_id: edge for edge in edges}.values(),
            key=lambda item: item.edge_id,
        )
    )


def _safe_metadata_hash(value: object) -> str:
    return canonical_hash({"value": value if isinstance(value, str) else ""})


class PythonRecipeExtractor:
    """A pure, bounded Python v1 recipe extractor."""

    extractor_id = "python-ast"
    extractor_version = "1"
    languages = frozenset({"python"})

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        intent = (
            normalize_intent_id(request.intent_id)
            if isinstance(request.intent_id, str)
            else ""
        )
        if _metadata_exceeds_graph_budget(request):
            artifacts = self._build_episode(
                request,
                intent,
                {},
                {},
                (),
                compact=True,
            )
            return ExtractionResult(
                eligible=False,
                gaps=(ExtractionGap("SIZE_LIMIT", detail="metadata_budget"),),
                nodes=artifacts.nodes,
                edges=artifacts.edges,
                episode_id=artifacts.episode.node_id,
            )
        gaps: list[ExtractionGap] = []
        scope = _normalize_scope(request.write_scope, gaps=gaps)
        before = _observe_files(request.before_files, gaps=gaps)
        after = _observe_files(request.after_files, gaps=gaps)
        paths = tuple(sorted(set(before) | set(after)))

        if (
            not isinstance(request.task_kind, str)
            or request.task_kind.strip().casefold() != "code"
        ):
            _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="task_kind")
        if not intent:
            _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="intent")
        if not all(
            isinstance(value, str) and value
            for value in (
                request.workspace_hash,
                request.checkpoint_id,
                request.input_snapshot_id,
                request.output_snapshot_id,
            )
        ):
            _safe_gap(gaps, "SNAPSHOT_MISMATCH", detail="request_binding")
        if not paths:
            _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", detail="no_changes")
        elif len(paths) > MAX_CHANGED_FILES:
            _safe_gap(gaps, "SIZE_LIMIT", detail="changed_file_count")

        drafts: list[_OperationDraft] = []
        for path in paths:
            before_file = before.get(path)
            after_file = after.get(path)
            if not _path_is_within_scope(path, scope):
                _safe_gap(
                    gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="out_of_scope"
                )
            classification = _path_classification(path)
            if classification is not None:
                _safe_gap(gaps, classification, path=path)
            if not path.endswith(".py"):
                _safe_gap(gaps, "UNSUPPORTED_LANGUAGE", path=path)
                continue
            if before_file is not None and after_file is None:
                _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="deleted")
                continue
            if after_file is None:
                continue
            if after_file.text is None:
                continue
            if before_file is None:
                draft = _create_draft(path=path, after=after_file, gaps=gaps)
            elif before_file.body == after_file.body:
                _safe_gap(gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="noop")
                draft = None
            else:
                draft = _append_draft(
                    path=path,
                    before=before_file,
                    after=after_file,
                    gaps=gaps,
                )
            if draft is None:
                continue
            if "${" in draft.text:
                _safe_gap(
                    gaps,
                    "UNSUPPORTED_EDIT_SHAPE",
                    path=path,
                    detail="literal_placeholder",
                )
                continue
            size = len(draft.text.encode("utf-8"))
            if size > MAX_TEMPLATE_BYTES:
                _safe_gap(gaps, "SIZE_LIMIT", path=path, detail="template_bytes")
            if len({symbol.name for symbol in draft.symbols}) != len(draft.symbols):
                _safe_gap(
                    gaps, "UNSUPPORTED_EDIT_SHAPE", path=path, detail="duplicate_symbol"
                )
                continue
            drafts.append(draft)

        if sum(len(draft.text.encode("utf-8")) for draft in drafts) > MAX_RECIPE_BYTES:
            _safe_gap(gaps, "SIZE_LIMIT", detail="recipe_bytes")
        if len(drafts) + sum(len(draft.symbols) for draft in drafts) > MAX_SLOT_COUNT:
            _safe_gap(gaps, "SIZE_LIMIT", detail="slot_count")
        _gaps_for_coverage(gaps, request.coverage_gaps, set(paths))
        _validate_receipts(request, scope=scope, gaps=gaps)
        artifacts = self._build_episode(request, intent, before, after, paths)
        ordered_gaps = _ordered_gaps(gaps)
        if ordered_gaps or len(drafts) != len(paths):
            return ExtractionResult(
                eligible=False,
                gaps=ordered_gaps,
                nodes=artifacts.nodes,
                edges=artifacts.edges,
                episode_id=artifacts.episode.node_id,
            )

        sorted_drafts = tuple(sorted(drafts, key=lambda draft: draft.path))
        try:
            first = self._build_eligible(request, intent, sorted_drafts, artifacts)
            second = self._build_eligible(request, intent, sorted_drafts, artifacts)
        except AtlasError:
            return ExtractionResult(
                eligible=False,
                gaps=(
                    ExtractionGap("UNSUPPORTED_EDIT_SHAPE", detail="template_build"),
                ),
                nodes=artifacts.nodes,
                edges=artifacts.edges,
                episode_id=artifacts.episode.node_id,
            )
        if (
            first.manifest is None
            or second.manifest is None
            or first.manifest.manifest_hash != second.manifest.manifest_hash
            or first.nodes != second.nodes
            or first.edges != second.edges
            or first.blobs != second.blobs
            or first.original_bindings != second.original_bindings
        ):
            return ExtractionResult(
                eligible=False,
                gaps=(
                    ExtractionGap("UNSUPPORTED_EDIT_SHAPE", detail="non_deterministic"),
                ),
                nodes=artifacts.nodes,
                edges=artifacts.edges,
                episode_id=artifacts.episode.node_id,
            )
        try:
            rendered = render_operations(
                first.manifest, first.original_bindings, request.before_files
            )
        except AtlasError:
            rendered = ()
        expected_after = tuple(
            SnapshotFile(item.path, item.actual_hash, item.body)
            for item in sorted(after.values(), key=lambda item: item.path)
            if item.body is not None
        )
        if rendered != expected_after:
            return ExtractionResult(
                eligible=False,
                gaps=(ExtractionGap("UNSUPPORTED_EDIT_SHAPE", detail="round_trip"),),
                nodes=artifacts.nodes,
                edges=artifacts.edges,
                episode_id=artifacts.episode.node_id,
            )
        return first

    def _build_episode(
        self,
        request: ExtractionRequest,
        intent: str,
        before: Mapping[str, _ObservedFile],
        after: Mapping[str, _ObservedFile],
        paths: Sequence[str],
        *,
        compact: bool = False,
    ) -> _EpisodeArtifacts:
        source_hashes = tuple(
            value
            for value in (
                request.workspace_hash,
                request.input_snapshot_id,
                request.output_snapshot_id,
            )
            if _is_hash(value)
        )
        options = {
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "provenance": "observed",
            "source_hashes": source_hashes,
        }
        emitted_paths: Sequence[str] = () if compact else paths
        emitted_changed_nodes: Sequence[IndexNode] = (
            () if compact else request.changed_nodes
        )
        emitted_receipts: Sequence[BoundExecutionReceipt] = (
            () if compact else request.execution_receipts
        )
        episode = AtlasNode.create(
            NodeKind.TASK_EPISODE,
            {
                "workflow_id_hash": _safe_metadata_hash(request.workflow_id),
                "task_id_hash": _safe_metadata_hash(request.task_id),
                "acceptance_id_hash": _safe_metadata_hash(request.acceptance_id),
                "workspace_hash": request.workspace_hash
                if _is_hash(request.workspace_hash)
                else "",
                "checkpoint_id_hash": _safe_metadata_hash(request.checkpoint_id),
                "input_snapshot_id_hash": _safe_metadata_hash(
                    request.input_snapshot_id
                ),
                "output_snapshot_id_hash": _safe_metadata_hash(
                    request.output_snapshot_id
                ),
                "task_kind": "code" if request.task_kind == "code" else "invalid",
            },
            **options,
        )
        intent_node = AtlasNode.create(
            NodeKind.INTENT,
            {"intent_id": intent},
            **options,
        )
        summary_source = AtlasNode.create(
            NodeKind.SOURCE_EVIDENCE,
            {
                "kind": "task_change_set",
                "path_count": len(paths),
                "input_snapshot_id_hash": _safe_metadata_hash(
                    request.input_snapshot_id
                ),
                "output_snapshot_id_hash": _safe_metadata_hash(
                    request.output_snapshot_id
                ),
            },
            **options,
        )
        source_evidence = [summary_source]
        for path in emitted_paths:
            before_file = before.get(path)
            after_file = after.get(path)
            source_evidence.append(
                AtlasNode.create(
                    NodeKind.SOURCE_EVIDENCE,
                    {
                        "path": path,
                        "before_hash": ""
                        if before_file is None
                        else before_file.actual_hash,
                        "after_hash": ""
                        if after_file is None
                        else after_file.actual_hash,
                        "before_bytes": 0
                        if before_file is None or before_file.body is None
                        else len(before_file.body),
                        "after_bytes": 0
                        if after_file is None or after_file.body is None
                        else len(after_file.body),
                    },
                    **options,
                )
            )
        touched_paths = set(emitted_paths)
        for changed_node in emitted_changed_nodes:
            if not isinstance(changed_node, IndexNode):
                continue
            path = _normalize_relative_path(changed_node.path)
            if path is None or path not in touched_paths:
                continue
            source_evidence.append(
                AtlasNode.create(
                    NodeKind.SOURCE_EVIDENCE,
                    {
                        "kind": "index_node",
                        "path": path,
                        "node_id_hash": _safe_metadata_hash(changed_node.node_id),
                        "content_hash": changed_node.content_hash
                        if _is_hash(changed_node.content_hash)
                        else "",
                        "start_byte": changed_node.start_byte
                        if type(changed_node.start_byte) is int
                        else 0,
                        "end_byte": changed_node.end_byte
                        if type(changed_node.end_byte) is int
                        else 0,
                        "name_hash": _safe_metadata_hash(changed_node.name),
                    },
                    **options,
                )
            )
        receipt_nodes: list[AtlasNode] = [
            AtlasNode.create(
                NodeKind.EXECUTION_RECEIPT,
                {
                    "kind": "bound_receipt_summary",
                    "receipt_count": len(request.execution_receipts),
                },
                **options,
            )
        ]
        verification_nodes: list[AtlasNode] = [
            AtlasNode.create(
                NodeKind.TEST_SPEC,
                {"kind": "bound_verification", "expected_exit_code": 0},
                **options,
            )
        ]
        for receipt in emitted_receipts:
            if not isinstance(receipt, BoundExecutionReceipt):
                continue
            receipt_node = AtlasNode.create(
                NodeKind.EXECUTION_RECEIPT,
                {
                    "receipt_id_hash": _safe_metadata_hash(receipt.receipt_id),
                    "kind": receipt.kind
                    if receipt.kind in {"command", "write"}
                    else "invalid",
                    "command_spec_hash": receipt.command_spec_hash
                    if _is_hash(receipt.command_spec_hash)
                    else "",
                    "input_hash": receipt.input_hash
                    if _is_hash(receipt.input_hash)
                    else "",
                    "output_hash": receipt.output_hash
                    if _is_hash(receipt.output_hash)
                    else "",
                    "exit_code": receipt.exit_code
                    if type(receipt.exit_code) is int
                    else -1,
                    "success": receipt.success is True,
                },
                **options,
            )
            receipt_nodes.append(receipt_node)
            if receipt.kind == "command":
                verification_nodes.append(
                    AtlasNode.create(
                        NodeKind.TEST_SPEC,
                        {
                            "kind": "command_receipt",
                            "command_spec_hash": receipt.command_spec_hash
                            if _is_hash(receipt.command_spec_hash)
                            else "",
                            "expected_exit_code": 0,
                        },
                        **options,
                    )
                )
        nodes = [
            episode,
            intent_node,
            *source_evidence,
            *receipt_nodes,
            *verification_nodes,
        ]
        edges: list[AtlasEdge] = [
            AtlasEdge.create(EdgeRelation.SOLVES, episode, intent_node)
        ]
        for source in source_evidence:
            edges.append(AtlasEdge.create(EdgeRelation.CHANGES, episode, source))
        for receipt_node in receipt_nodes:
            edges.append(
                AtlasEdge.create(EdgeRelation.VERIFIED_BY, episode, receipt_node)
            )
        for verification in verification_nodes:
            edges.append(
                AtlasEdge.create(EdgeRelation.VERIFIED_BY, episode, verification)
            )
            for source in source_evidence:
                edges.append(AtlasEdge.create(EdgeRelation.TESTS, verification, source))
        return _EpisodeArtifacts(
            episode=episode,
            intent=intent_node,
            source_evidence=tuple(source_evidence),
            receipt_nodes=tuple(receipt_nodes),
            verification_nodes=tuple(verification_nodes),
            nodes=_unique_nodes(nodes),
            edges=_unique_edges(edges),
        )

    def _build_eligible(
        self,
        request: ExtractionRequest,
        intent: str,
        drafts: tuple[_OperationDraft, ...],
        artifacts: _EpisodeArtifacts,
    ) -> ExtractionResult:
        slots: list[SlotSpec] = []
        constraints: list[ConstraintSpec] = []
        slot_values: dict[str, str] = {}
        path_slots: dict[str, str] = {}
        symbol_slots: dict[tuple[str, int, str], str] = {}
        symbol_index = 0
        for index, draft in enumerate(drafts):
            path_name = f"path_{index:03d}"
            path_slots[draft.path] = path_name
            slots.append(SlotSpec(path_name, "relative_python_path"))
            slot_values[path_name] = draft.path
            constraints.append(ConstraintSpec("path_suffix", path_name, ".py"))
            names = [symbol.name for symbol in draft.symbols]
            if len(set(names)) != len(names):
                raise AtlasError("duplicate_new_symbol")
            for symbol in draft.symbols:
                name = f"symbol_{symbol_index:03d}"
                symbol_index += 1
                symbol_slots[(draft.path, symbol.byte_start, symbol.name)] = name
                slots.append(SlotSpec(name, "python_identifier"))
                slot_values[name] = symbol.name
                constraints.append(ConstraintSpec("required_symbol", name, symbol.name))
        if len(slots) > MAX_SLOT_COUNT:
            raise AtlasError("too_many_slots")

        templates: dict[str, str] = {}
        operations: list[TemplateOperation] = []
        template_nodes: list[AtlasNode] = []
        for draft in drafts:
            replacements = {
                symbol.name: "${"
                + symbol_slots[(draft.path, symbol.byte_start, symbol.name)]
                + "}"
                for symbol in draft.symbols
            }
            template_text = _substitute_names(draft.text, replacements)
            template_hash = _sha256(template_text.encode("utf-8"))
            templates[template_hash] = template_text
            operations.append(
                TemplateOperation(
                    kind=draft.kind,
                    path_slot=path_slots[draft.path],
                    template_hash=template_hash,
                    separator="",
                )
            )
        manifest_payload = {
            "schema_version": "1",
            "recipe_key": intent,
            "version": 1,
            "intent_id": intent,
            "language": {"name": "python", "extractor_version": self.extractor_version},
            "framework": None,
            "repository_signature": request.workspace_hash
            if _is_hash(request.workspace_hash)
            else _safe_metadata_hash(request.workspace_hash),
            "layer": "local",
            "slots": [slot.to_dict() for slot in slots],
            "constraints": [constraint.to_dict() for constraint in constraints],
            "dependencies": [],
            "tests": [],
            "operations": [operation.to_dict() for operation in operations],
            "provenance": {"kind": "observed", "source": "accepted_task"},
        }
        manifest_hash = canonical_hash(manifest_payload)
        manifest = RecipeManifest(
            recipe_id="",
            recipe_key=intent,
            version=1,
            intent_id=intent,
            language_name="python",
            language_extractor_version=self.extractor_version,
            repository_signature=manifest_payload["repository_signature"],
            layer="local",
            manifest_hash=manifest_hash,
            slots=tuple(slots),
            constraints=tuple(constraints),
            operations=tuple(operations),
            provenance_kind="observed",
            provenance_source="accepted_task",
        )
        options = {
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "provenance": "observed",
            "source_hashes": (manifest_hash,),
        }
        recipe = AtlasNode.create(NodeKind.RECIPE, _recipe_payload(manifest), **options)
        manifest = replace(manifest, recipe_id=recipe.node_id)
        language = AtlasNode.create(
            NodeKind.LANGUAGE,
            {"name": "python", "extractor_version": self.extractor_version},
            **options,
        )
        for operation in operations:
            template_nodes.append(
                AtlasNode.create(
                    NodeKind.CODE_TEMPLATE,
                    {"template_hash": operation.template_hash, "kind": operation.kind},
                    **options,
                )
            )
        slot_nodes = [
            AtlasNode.create(NodeKind.ADAPTATION_SLOT, slot.to_dict(), **options)
            for slot in slots
        ]
        constraint_nodes = [
            AtlasNode.create(NodeKind.CONSTRAINT, constraint.to_dict(), **options)
            for constraint in constraints
        ]
        nodes = [
            *artifacts.nodes,
            recipe,
            language,
            *template_nodes,
            *slot_nodes,
            *constraint_nodes,
        ]
        edges = list(artifacts.edges)
        edges.extend(
            (
                AtlasEdge.create(EdgeRelation.SOLVES, recipe, artifacts.intent),
                AtlasEdge.create(EdgeRelation.DERIVED_FROM, recipe, artifacts.episode),
                AtlasEdge.create(EdgeRelation.REQUIRES, recipe, language),
            )
        )
        for source in artifacts.source_evidence:
            edges.append(AtlasEdge.create(EdgeRelation.DERIVED_FROM, recipe, source))
        for node in template_nodes:
            edges.append(
                AtlasEdge.create(EdgeRelation.HAS_IMPLEMENTATION, recipe, node)
            )
        for node in slot_nodes:
            edges.append(AtlasEdge.create(EdgeRelation.HAS_SLOT, recipe, node))
        for node in constraint_nodes:
            edges.append(AtlasEdge.create(EdgeRelation.CONSTRAINED_BY, recipe, node))
        for node in (*artifacts.receipt_nodes, *artifacts.verification_nodes):
            edges.append(AtlasEdge.create(EdgeRelation.VERIFIED_BY, recipe, node))
        bindings = {
            "slot_values": {name: slot_values[name] for name in sorted(slot_values)},
            "template_text_by_hash": {
                name: templates[name] for name in sorted(templates)
            },
        }
        return ExtractionResult(
            eligible=True,
            manifest=manifest,
            original_bindings=bindings,
            nodes=_unique_nodes(nodes),
            edges=_unique_edges(edges),
            blobs=tuple(sorted(templates)),
            episode_id=artifacts.episode.node_id,
        )


def render_operations(
    manifest: RecipeManifest,
    original_bindings: object,
    before_files: Sequence[CheckpointFile],
) -> tuple[SnapshotFile, ...]:
    """Render a Task-6 internal envelope without caches or workspace access."""

    try:
        if not isinstance(manifest, RecipeManifest):
            raise AtlasError(_RENDER_ERROR)
        envelope = thaw_json(original_bindings)
        if not isinstance(envelope, dict) or set(envelope) != {
            "slot_values",
            "template_text_by_hash",
        }:
            raise AtlasError(_RENDER_ERROR)
        slot_values = envelope["slot_values"]
        template_texts = envelope["template_text_by_hash"]
        if not isinstance(slot_values, dict) or not isinstance(template_texts, dict):
            raise AtlasError(_RENDER_ERROR)
        slots = tuple(manifest.slots)
        if not slots or len(slots) > MAX_SLOT_COUNT:
            raise AtlasError(_RENDER_ERROR)
        slot_by_name: dict[str, SlotSpec] = {}
        for slot in slots:
            if (
                not isinstance(slot, SlotSpec)
                or not isinstance(slot.name, str)
                or _SLOT_NAME.fullmatch(slot.name) is None
                or slot.name in slot_by_name
            ):
                raise AtlasError(_RENDER_ERROR)
            slot_by_name[slot.name] = slot
        if set(slot_values) != set(slot_by_name) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in slot_values.items()
        ):
            raise AtlasError(_RENDER_ERROR)
        validated_values = {
            name: validate_slot_value(slot.type, slot_values[name])
            for name, slot in slot_by_name.items()
        }
        operations = tuple(manifest.operations)
        if not operations:
            raise AtlasError(_RENDER_ERROR)
        referenced_hashes: list[str] = []
        operation_paths: set[str] = set()
        used_slots: set[str] = set()
        for operation in operations:
            if not isinstance(operation, TemplateOperation):
                raise AtlasError(_RENDER_ERROR)
            if operation.kind not in {"create_python_file", "append_python_nodes"}:
                raise AtlasError(_RENDER_ERROR)
            if operation.separator != "" or operation.target_symbol_slot:
                raise AtlasError(_RENDER_ERROR)
            if operation.path_slot not in slot_by_name:
                raise AtlasError(_RENDER_ERROR)
            if slot_by_name[operation.path_slot].type != "relative_python_path":
                raise AtlasError(_RENDER_ERROR)
            path = validated_values[operation.path_slot]
            if path in operation_paths:
                raise AtlasError(_RENDER_ERROR)
            operation_paths.add(path)
            referenced_hashes.append(operation.template_hash)
            used_slots.add(operation.path_slot)
        if (
            any(not _is_hash(template_hash) for template_hash in referenced_hashes)
            or set(template_texts) != set(referenced_hashes)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in template_texts.items()
            )
        ):
            raise AtlasError(_RENDER_ERROR)
        templates: dict[str, str] = {}
        aggregate_bytes = 0
        for template_hash in referenced_hashes:
            text = template_texts[template_hash]
            validate_fragment(text, max_bytes=MAX_TEMPLATE_BYTES)
            if _sha256(text.encode("utf-8")) != template_hash:
                raise AtlasError(_RENDER_ERROR)
            aggregate_bytes += len(text.encode("utf-8"))
            templates[template_hash] = text
            names = _placeholder_names(text)
            if not names <= set(slot_by_name):
                raise AtlasError(_RENDER_ERROR)
            used_slots.update(names)
        if aggregate_bytes > MAX_RECIPE_BYTES or used_slots != set(slot_by_name):
            raise AtlasError(_RENDER_ERROR)
        before: dict[str, CheckpointFile] = {}
        for item in before_files:
            if not isinstance(item, CheckpointFile):
                raise AtlasError(_RENDER_ERROR)
            path = _normalize_relative_path(item.path)
            if path is None or path in before or not isinstance(item.body, bytes):
                raise AtlasError(_RENDER_ERROR)
            if _sha256(item.body) != item.content_hash:
                raise AtlasError(_RENDER_ERROR)
            before[path] = item
        output: dict[str, bytes] = {}
        for operation in operations:
            path = validated_values[operation.path_slot]
            text = _render_template(
                templates[operation.template_hash], validated_values
            )
            body = text.encode("utf-8")
            if operation.kind == "create_python_file":
                if path in before:
                    raise AtlasError(_RENDER_ERROR)
                output[path] = body
            else:
                source = before.get(path)
                if source is None:
                    raise AtlasError(_RENDER_ERROR)
                output[path] = source.body + body
        return tuple(
            SnapshotFile(path, _sha256(body), body)
            for path, body in sorted(output.items())
        )
    except (AtlasError, AttributeError, KeyError, TypeError, UnicodeError, ValueError):
        raise AtlasError(_RENDER_ERROR) from None


__all__ = [
    "BoundExecutionReceipt",
    "ExtractionRequest",
    "PythonRecipeExtractor",
    "RecipeExtractor",
    "render_operations",
]
