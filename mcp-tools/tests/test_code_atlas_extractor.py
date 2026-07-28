"""Red-first contract tests for the deterministic Code Atlas extractor."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from code_atlas.canonical import canonical_hash, canonical_json, thaw_json
from code_atlas.extractors import (
    BoundExecutionReceipt,
    ExtractionRequest,
    PythonRecipeExtractor,
    RecipeExtractor,
    render_operations,
)
from code_atlas.models import AtlasError, TemplateOperation
from code_atlas.security import (
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_RECIPE_BYTES,
    MAX_TEMPLATE_BYTES,
)
from code_atlas.store import AtlasStore
from project_index.checkpoints import CheckpointFile
from project_index.models import CoverageGap, IndexNode, SnapshotFile


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _scope_for(paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ("src",)
    roots = {
        path.replace("\\", "/").split("/", maxsplit=1)[0]
        if "/" in path.replace("\\", "/")
        else path.replace("\\", "/")
        for path in paths
    }
    return tuple(sorted(roots))


def _binding_hash(
    *,
    kind: str,
    request: ExtractionRequest,
    snapshot_id: str,
    files: tuple[CheckpointFile, ...] | tuple[SnapshotFile, ...],
) -> str:
    return canonical_hash(
        {
            "kind": kind,
            "workflow_id": request.workflow_id,
            "task_id": request.task_id,
            "acceptance_id": request.acceptance_id,
            "workspace_hash": request.workspace_hash,
            "checkpoint_id": request.checkpoint_id,
            "snapshot_id": snapshot_id,
            "write_scope": sorted(
                scope.replace("\\", "/") for scope in request.write_scope
            ),
            "files": sorted([[item.path, item.content_hash] for item in files]),
        }
    )


def _receipt(
    request: ExtractionRequest,
    *,
    receipt_id: str,
    kind: str,
    command_spec: tuple[str, ...],
    success: bool = True,
    exit_code: int = 0,
) -> BoundExecutionReceipt:
    return BoundExecutionReceipt(
        receipt_id=receipt_id,
        kind=kind,
        workflow_id=request.workflow_id,
        task_id=request.task_id,
        acceptance_id=request.acceptance_id,
        workspace_hash=request.workspace_hash,
        output_snapshot_id=request.output_snapshot_id,
        command_spec=command_spec,
        command_spec_hash=canonical_hash(command_spec),
        input_hash=_binding_hash(
            kind="atlas-extraction-input-v1",
            request=request,
            snapshot_id=request.input_snapshot_id,
            files=request.before_files,
        ),
        output_hash=_binding_hash(
            kind="atlas-extraction-output-v1",
            request=request,
            snapshot_id=request.output_snapshot_id,
            files=request.after_files,
        ),
        exit_code=exit_code,
        success=success,
    )


def _bind(request: ExtractionRequest) -> ExtractionRequest:
    return replace(
        request,
        execution_receipts=(
            _receipt(
                request,
                receipt_id="write-1",
                kind="write",
                command_spec=(),
            ),
            _receipt(
                request,
                receipt_id="command-1",
                kind="command",
                command_spec=("python", "-m", "pytest"),
            ),
        ),
    )


def _index_node(path: str, content_hash: str) -> IndexNode:
    return IndexNode(
        node_id=canonical_hash({"path": path, "content_hash": content_hash}),
        kind="module",
        path=path,
        name=path.rsplit("/", maxsplit=1)[-1],
        qualified_name="",
        start_line=1,
        end_line=1,
        content_hash=content_hash,
    )


def extraction_request(
    *,
    before: dict[str, bytes] | None = None,
    after: dict[str, bytes] | None = None,
    write_scope: tuple[str, ...] | None = None,
    task_kind: str = "code",
    intent_id: str = "python.validation-helper",
    coverage_gaps: tuple[CoverageGap, ...] = (),
) -> ExtractionRequest:
    before = {} if before is None else before
    after = {} if after is None else after
    before_files = tuple(
        CheckpointFile(path, _hash(body), body) for path, body in before.items()
    )
    after_files = tuple(
        SnapshotFile(path, _hash(body), body) for path, body in after.items()
    )
    paths = tuple(before) + tuple(after)
    request = ExtractionRequest(
        workflow_id="workflow-1",
        task_id="task-1",
        acceptance_id="acceptance-1",
        task_kind=task_kind,
        intent_id=intent_id,
        workspace_hash="sha256:workspace",
        checkpoint_id="checkpoint-1",
        input_snapshot_id="snapshot-before",
        output_snapshot_id="snapshot-after",
        write_scope=write_scope if write_scope is not None else _scope_for(paths),
        before_files=before_files,
        after_files=after_files,
        changed_nodes=tuple(
            _index_node(item.path, item.content_hash) for item in after_files
        ),
        coverage_gaps=coverage_gaps,
        execution_receipts=(),
    )
    return _bind(request)


def _primary_request() -> ExtractionRequest:
    return extraction_request(
        before={
            "src/guards.py": b"VALUE = 1\n",
            "tests/test_guards.py": b"from src.guards import VALUE\n",
        },
        after={
            "src/guards.py": (
                b"VALUE = 1\n\n"
                b"def is_valid(value: int) -> bool:\n"
                b"    return value > 0\n"
            ),
            "tests/test_guards.py": (
                b"from src.guards import VALUE\n\n"
                b"def test_is_valid() -> None:\n"
                b"    assert VALUE == 1\n"
            ),
        },
    )


def _codes(result: Any) -> tuple[str, ...]:
    return tuple(gap.code for gap in result.gaps)


def _python_body(size: int) -> bytes:
    assert size >= 8
    return b"x = 1\n#" + b"x" * (size - 8) + b"\n"


def _failed_command_request() -> ExtractionRequest:
    request = extraction_request(after={"src/guard.py": b"x = 1\n"})
    receipts = list(request.execution_receipts)
    receipts[1] = replace(receipts[1], success=False)
    return replace(request, execution_receipts=tuple(receipts))


def _mismatched_snapshot_request() -> ExtractionRequest:
    request = extraction_request(after={"src/guard.py": b"x = 1\n"})
    after = request.after_files[0]
    return replace(
        request,
        after_files=(replace(after, content_hash="sha256:" + "0" * 64),),
    )


def test_python_extractor_declares_v1_protocol_identity() -> None:
    extractor = PythonRecipeExtractor()
    assert extractor.extractor_id == "python-ast"
    assert extractor.extractor_version == "1"
    assert extractor.languages == frozenset({"python"})
    assert tuple(RecipeExtractor.__annotations__) == (
        "extractor_id",
        "extractor_version",
        "languages",
    )


def test_protocol_records_are_frozen_slotted_and_tuple_normalized() -> None:
    assert tuple(field.name for field in fields(BoundExecutionReceipt)) == (
        "receipt_id",
        "kind",
        "workflow_id",
        "task_id",
        "acceptance_id",
        "workspace_hash",
        "output_snapshot_id",
        "command_spec",
        "command_spec_hash",
        "input_hash",
        "output_hash",
        "exit_code",
        "success",
    )
    request = _primary_request()
    receipt = _receipt(
        request,
        receipt_id="tuple-test",
        kind="command",
        command_spec=("python",),
    )
    request = replace(
        request,
        write_scope=list(request.write_scope),
        before_files=list(request.before_files),
        after_files=list(request.after_files),
        changed_nodes=list(request.changed_nodes),
        coverage_gaps=list(request.coverage_gaps),
        execution_receipts=[receipt],
    )
    assert isinstance(request.write_scope, tuple)
    assert isinstance(request.before_files, tuple)
    assert isinstance(request.execution_receipts, tuple)
    with pytest.raises((AttributeError, TypeError)):
        request.task_id = "changed"  # type: ignore[misc]


def test_python_append_shape_round_trips_and_is_repeatable() -> None:
    request = _primary_request()
    extractor = PythonRecipeExtractor()

    first = extractor.extract(request)
    second = extractor.extract(request)

    assert first == second
    assert first.eligible is True
    assert first.gaps == ()
    assert first.manifest is not None
    assert tuple(operation.kind for operation in first.manifest.operations) == (
        "append_python_nodes",
        "append_python_nodes",
    )
    assert tuple(operation.separator for operation in first.manifest.operations) == (
        "",
        "",
    )
    assert render_operations(
        first.manifest, first.original_bindings, request.before_files
    ) == tuple(sorted(request.after_files, key=lambda item: item.path))
    envelope = thaw_json(first.original_bindings)
    assert set(envelope) == {"slot_values", "template_text_by_hash"}
    assert tuple(sorted(envelope["template_text_by_hash"])) == first.blobs
    assert {node.kind.value for node in first.nodes} >= {
        "TaskEpisode",
        "Intent",
        "SourceEvidence",
        "ExecutionReceipt",
        "TestSpec",
        "Recipe",
        "Language",
        "CodeTemplate",
        "AdaptationSlot",
        "Constraint",
    }
    serialized = canonical_json(
        {
            "nodes": [node.to_dict() for node in first.nodes],
            "edges": [edge.to_dict() for edge in first.edges],
        }
    )
    assert "return value > 0" not in serialized
    assert "from src.guards import VALUE" not in serialized


@pytest.mark.parametrize(
    "metadata_kind", ["changed_nodes", "receipts", "coverage_gaps"]
)
def test_graph_metadata_budget_returns_a_bounded_compact_episode(
    metadata_kind: str,
) -> None:
    request = _primary_request()
    if metadata_kind == "changed_nodes":
        node = request.changed_nodes[0]
        request = replace(
            request,
            changed_nodes=tuple(
                replace(node, node_id=f"changed-node-{index}") for index in range(250)
            ),
        )
    elif metadata_kind == "receipts":
        request = replace(
            request,
            execution_receipts=(
                request.execution_receipts[0],
                *(
                    _receipt(
                        request,
                        receipt_id=f"command-{index}",
                        kind="command",
                        command_spec=("python", "-m", "pytest", str(index)),
                    )
                    for index in range(250)
                ),
            ),
        )
    else:
        request = replace(
            request,
            coverage_gaps=tuple(
                CoverageGap(
                    "src/guards.py",
                    f"GAP_{index}",
                    f"atlas-fake-coverage-{index}",
                )
                for index in range(250)
            ),
        )

    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is False
    assert _codes(result) == ("SIZE_LIMIT",)
    assert result.episode_id
    assert any(node.kind.value == "TaskEpisode" for node in result.nodes)
    assert len(result.nodes) <= MAX_GRAPH_NODES
    assert len(result.edges) <= MAX_GRAPH_EDGES


def test_empty_write_receipt_is_a_valid_canonical_task9_binding() -> None:
    request = _primary_request()
    write = replace(
        request.execution_receipts[0],
        command_spec=(),
        command_spec_hash=canonical_hash(()),
    )
    request = replace(
        request,
        execution_receipts=(write, request.execution_receipts[1]),
    )

    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is True
    assert result.gaps == ()


def test_empty_command_receipt_is_still_a_verification_failure() -> None:
    request = _primary_request()
    command = replace(
        request.execution_receipts[1],
        command_spec=(),
        command_spec_hash=canonical_hash(()),
    )
    request = replace(
        request,
        execution_receipts=(request.execution_receipts[0], command),
    )

    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is False
    assert "VERIFICATION_FAILED" in _codes(result)
    assert "SNAPSHOT_MISMATCH" not in _codes(result)


def test_created_python_file_round_trips() -> None:
    request = extraction_request(
        before={},
        after={
            "pkg/new_guard.py": b"def is_valid(value: int) -> bool:\n    return value > 0\n"
        },
    )
    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is True
    assert result.manifest is not None
    assert tuple(operation.kind for operation in result.manifest.operations) == (
        "create_python_file",
    )
    assert (
        render_operations(
            result.manifest, result.original_bindings, request.before_files
        )
        == request.after_files
    )


@pytest.mark.parametrize(
    ("fixture_name", "case_request", "reason"),
    [
        (
            "rename-only",
            extraction_request(
                before={"src/old.py": b"x = 1\n"},
                after={"src/new.py": b"x = 1\n"},
            ),
            "UNSUPPORTED_EDIT_SHAPE",
        ),
        (
            "in-place-replacement",
            extraction_request(
                before={"src/guard.py": b"x = 1\n"},
                after={"src/guard.py": b"x = 2\n"},
            ),
            "UNSUPPORTED_EDIT_SHAPE",
        ),
        (
            "documentation-only",
            extraction_request(after={"docs/readme.txt": b"documentation\n"}),
            "UNSUPPORTED_LANGUAGE",
        ),
        (
            "generated-file",
            extraction_request(after={"src/generated_pb2.py": b"x = 1\n"}),
            "GENERATED_PATH",
        ),
        (
            "vendor-file",
            extraction_request(after={"vendor/build/file.py": b"x = 1\n"}),
            "VENDORED_PATH",
        ),
        (
            "binary-file",
            extraction_request(after={"src/binary.py": b"x = b'\x00'\n"}),
            "BINARY_CONTENT",
        ),
        (
            "secret-fragment",
            extraction_request(
                after={"src/secret.py": b"API_KEY = 'atlas-fake-secret'\n"}
            ),
            "SECRET_BEARING",
        ),
        (
            "oversize-fragment",
            extraction_request(
                after={"src/large.py": _python_body(MAX_TEMPLATE_BYTES + 1)}
            ),
            "SIZE_LIMIT",
        ),
        (
            "parser-gap",
            extraction_request(
                after={"src/guard.py": b"x = 1\n"},
                coverage_gaps=(
                    CoverageGap(
                        path="src/guard.py",
                        code="PARSE_ERROR",
                        message="atlas-fake-secret-in-gap-message",
                    ),
                ),
            ),
            "PARSER_GAP",
        ),
        (
            "failed-test-receipt",
            _failed_command_request(),
            "VERIFICATION_FAILED",
        ),
        (
            "snapshot-mismatch",
            _mismatched_snapshot_request(),
            "SNAPSHOT_MISMATCH",
        ),
    ],
)
def test_unsupported_inputs_are_episode_only(
    fixture_name: str, case_request: ExtractionRequest, reason: str
) -> None:
    result = PythonRecipeExtractor().extract(case_request)

    assert result.eligible is False, fixture_name
    assert reason in _codes(result), fixture_name
    assert result.manifest is None, fixture_name
    assert result.episode_id
    assert any(node.kind.value == "TaskEpisode" for node in result.nodes)


def _duplicate_after_request() -> ExtractionRequest:
    request = extraction_request(after={"src/guard.py": b"x = 1\n"})
    return _bind(replace(request, after_files=(request.after_files[0],) * 2))


@pytest.mark.parametrize(
    "case_request",
    [
        _duplicate_after_request(),
        extraction_request(after={"../escape.py": b"x = 1\n"}),
        extraction_request(
            after={"src/out_of_scope.py": b"x = 1\n"}, write_scope=("tests",)
        ),
        extraction_request(before={"src/deleted.py": b"x = 1\n"}, after={}),
        extraction_request(
            before={"src/noop.py": b"x = 1\n"},
            after={"src/noop.py": b"x = 1\n"},
        ),
    ],
)
def test_duplicate_unsafe_out_of_scope_delete_and_noop_are_edit_shape_gaps(
    case_request: ExtractionRequest,
) -> None:
    result = PythonRecipeExtractor().extract(case_request)

    assert result.eligible is False
    assert "UNSUPPORTED_EDIT_SHAPE" in _codes(result)
    assert result.manifest is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda receipt: replace(receipt, workflow_id="other-workflow"),
            "SNAPSHOT_MISMATCH",
        ),
        (lambda receipt: replace(receipt, task_id="other-task"), "SNAPSHOT_MISMATCH"),
        (
            lambda receipt: replace(receipt, acceptance_id="other-acceptance"),
            "SNAPSHOT_MISMATCH",
        ),
        (
            lambda receipt: replace(receipt, workspace_hash="sha256:other"),
            "SNAPSHOT_MISMATCH",
        ),
        (
            lambda receipt: replace(receipt, output_snapshot_id="other-output"),
            "SNAPSHOT_MISMATCH",
        ),
        (
            lambda receipt: replace(receipt, input_hash="sha256:" + "0" * 64),
            "SNAPSHOT_MISMATCH",
        ),
        (
            lambda receipt: replace(receipt, output_hash="sha256:" + "0" * 64),
            "SNAPSHOT_MISMATCH",
        ),
        (
            lambda receipt: replace(receipt, command_spec_hash="sha256:" + "0" * 64),
            "SNAPSHOT_MISMATCH",
        ),
        (lambda receipt: replace(receipt, kind="other"), "VERIFICATION_FAILED"),
        (lambda receipt: replace(receipt, success=False), "VERIFICATION_FAILED"),
        (lambda receipt: replace(receipt, exit_code=1), "VERIFICATION_FAILED"),
    ],
)
def test_every_receipt_binding_and_category_is_checked(
    mutate: Any, reason: str
) -> None:
    request = _primary_request()
    invalid = replace(request.execution_receipts[0], **{})
    invalid = mutate(invalid)
    request = replace(
        request, execution_receipts=(invalid, request.execution_receipts[1])
    )

    assert reason in _codes(PythonRecipeExtractor().extract(request))


@pytest.mark.parametrize("kind", ["write", "command"])
def test_both_bound_receipt_categories_are_required(kind: str) -> None:
    request = _primary_request()
    request = replace(
        request,
        execution_receipts=tuple(
            receipt for receipt in request.execution_receipts if receipt.kind == kind
        ),
    )

    assert "VERIFICATION_FAILED" in _codes(PythonRecipeExtractor().extract(request))


def test_gaps_collect_all_applicable_failures_in_stable_priority_order() -> None:
    request = extraction_request(
        after={
            "vendor/build/secret.py": b"API_KEY = 'atlas-fake-secret'\n",
            "docs/readme.txt": b"documentation\n",
        }
    )
    first_after = replace(request.after_files[0], content_hash="sha256:" + "0" * 64)
    failed_command = replace(request.execution_receipts[1], success=False)
    request = replace(
        request,
        after_files=(first_after, request.after_files[1]),
        execution_receipts=(request.execution_receipts[0], failed_command),
    )

    codes = _codes(PythonRecipeExtractor().extract(request))

    assert "SNAPSHOT_MISMATCH" in codes
    assert "VERIFICATION_FAILED" in codes
    assert "VENDORED_PATH" in codes
    assert "SECRET_BEARING" in codes
    assert "UNSUPPORTED_LANGUAGE" in codes
    assert codes.index("SNAPSHOT_MISMATCH") < codes.index("VERIFICATION_FAILED")
    assert codes.index("VERIFICATION_FAILED") < codes.index("VENDORED_PATH")
    assert codes.index("VENDORED_PATH") < codes.index("SECRET_BEARING")
    assert codes.index("SECRET_BEARING") < codes.index("UNSUPPORTED_LANGUAGE")
    assert "GENERATED_PATH" not in codes


@pytest.mark.parametrize(
    ("suffix", "eligible"),
    [
        (b"\ndef helper() -> int:\n    return 1\n", True),
        (b"\nasync def helper() -> int:\n    return 1\n", True),
        (b"\nclass Helper:\n    pass\n", True),
        (b"\nvalue = 1\n", False),
    ],
)
def test_append_accepts_only_top_level_python_nodes(
    suffix: bytes, eligible: bool
) -> None:
    before = b"BASE = 1\n"
    request = extraction_request(
        before={"src/module.py": before},
        after={"src/module.py": before + suffix},
    )
    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is eligible
    if not eligible:
        assert "UNSUPPORTED_EDIT_SHAPE" in _codes(result)


def test_created_file_allows_complete_python_and_untouched_gap_does_not_block() -> None:
    request = extraction_request(
        after={"src/new.py": b"VALUE = 1\n"},
        coverage_gaps=(CoverageGap("other.py", "PARSE_ERROR", "not relevant"),),
    )

    assert PythonRecipeExtractor().extract(request).eligible is True


def test_invalid_complete_python_output_is_a_parser_gap() -> None:
    request = extraction_request(after={"src/bad.py": b"def broken(:\n"})

    assert "PARSER_GAP" in _codes(PythonRecipeExtractor().extract(request))


def test_template_and_aggregate_size_boundaries() -> None:
    exact = _python_body(MAX_TEMPLATE_BYTES)
    over = _python_body(MAX_TEMPLATE_BYTES + 1)
    assert len(exact) == MAX_TEMPLATE_BYTES
    assert len(over) == MAX_TEMPLATE_BYTES + 1
    assert (
        PythonRecipeExtractor()
        .extract(extraction_request(after={"src/exact.py": exact}))
        .eligible
        is True
    )
    assert "SIZE_LIMIT" in _codes(
        PythonRecipeExtractor().extract(extraction_request(after={"src/over.py": over}))
    )

    exact_total = {f"src/exact_{index}.py": exact for index in range(4)}
    over_total = {
        "src/over_0.py": exact,
        "src/over_1.py": exact,
        "src/over_2.py": exact,
        "src/over_3.py": _python_body(MAX_TEMPLATE_BYTES - 1),
        "src/over_4.py": b"x\n",
    }
    assert sum(len(body) for body in exact_total.values()) == MAX_RECIPE_BYTES
    assert sum(len(body) for body in over_total.values()) == MAX_RECIPE_BYTES + 1
    assert (
        PythonRecipeExtractor().extract(extraction_request(after=exact_total)).eligible
        is True
    )
    assert "SIZE_LIMIT" in _codes(
        PythonRecipeExtractor().extract(extraction_request(after=over_total))
    )


def test_changed_file_count_has_exact_eight_and_nine_file_boundaries() -> None:
    eight = {f"src/eight_{index}.py": b"VALUE = 1\n" for index in range(8)}
    nine = {f"src/nine_{index}.py": b"VALUE = 1\n" for index in range(9)}

    assert (
        PythonRecipeExtractor().extract(extraction_request(after=eight)).eligible
        is True
    )
    assert "SIZE_LIMIT" in _codes(
        PythonRecipeExtractor().extract(extraction_request(after=nine))
    )


@pytest.mark.parametrize(
    ("before", "suffix"),
    [
        (b"VALUE = 1\n", b"def helper() -> int:\n    return VALUE\n"),
        (b"VALUE = 1\r\n", b"def helper() -> int:\r\n    return VALUE\r\n"),
        (
            b"VALUE = 1\n",
            b"# leading comment remains exact\n\ndef helper() -> int:\n    return VALUE\n",
        ),
    ],
)
def test_append_is_exact_for_lf_crlf_and_comment_prefixes(
    before: bytes, suffix: bytes
) -> None:
    request = extraction_request(
        before={"src/exact.py": before},
        after={"src/exact.py": before + suffix},
    )
    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is True
    assert result.manifest is not None
    assert result.manifest.operations[0].separator == ""
    assert (
        render_operations(
            result.manifest, result.original_bindings, request.before_files
        )
        == request.after_files
    )


@pytest.mark.parametrize(
    ("before", "reason"),
    [
        (b"API_KEY = 'atlas-before-fake-secret'\n", "SECRET_BEARING"),
        (b"VALUE = b'\x00'\n", "BINARY_CONTENT"),
    ],
)
def test_binary_and_secret_scans_cover_the_entire_unchanged_before_prefix(
    before: bytes, reason: str
) -> None:
    request = extraction_request(
        before={"src/prefix.py": before},
        after={"src/prefix.py": before + b"\ndef helper() -> int:\n    return 1\n"},
    )

    assert reason in _codes(PythonRecipeExtractor().extract(request))


def test_symbol_slots_are_lexical_and_never_change_comments_or_strings() -> None:
    body = (
        b"def alpha() -> str:\n"
        b"    # alpha must remain a comment\n"
        b"    return alpha.__name__ + 'alpha'\n"
    )
    request = extraction_request(
        before={"src/lexical.py": b""}, after={"src/lexical.py": body}
    )
    result = PythonRecipeExtractor().extract(request)

    assert result.eligible is True
    envelope = thaw_json(result.original_bindings)
    template = next(iter(envelope["template_text_by_hash"].values()))
    assert template.count("${") == 2
    assert "# alpha must remain a comment" in template
    assert "'alpha'" in template
    assert (
        render_operations(
            result.manifest, result.original_bindings, request.before_files
        )
        == request.after_files
    )


def test_literal_placeholder_syntax_is_never_reinterpreted() -> None:
    request = extraction_request(
        after={"src/literal.py": b"def example() -> str:\n    return '${literal}'\n"}
    )

    assert "UNSUPPORTED_EDIT_SHAPE" in _codes(PythonRecipeExtractor().extract(request))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-slot",
        "extra-slot",
        "missing-template",
        "extra-template",
        "wrong-hash",
        "bad-slot",
        "bad-operation",
        "undeclared",
    ],
)
def test_render_rejects_every_bad_internal_envelope_without_partial_output(
    mutation: str,
) -> None:
    request = _primary_request()
    result = PythonRecipeExtractor().extract(request)
    assert result.manifest is not None
    manifest = result.manifest
    envelope = thaw_json(result.original_bindings)
    slot_values = envelope["slot_values"]
    templates = envelope["template_text_by_hash"]

    if mutation == "missing-slot":
        slot_values.pop(next(iter(slot_values)))
    elif mutation == "extra-slot":
        slot_values["unexpected"] = "value"
    elif mutation == "missing-template":
        templates.pop(next(iter(templates)))
    elif mutation == "extra-template":
        text = "VALUE = 1\n"
        templates[_hash(text.encode("utf-8"))] = text
    elif mutation == "wrong-hash":
        first = next(iter(templates))
        templates[first] = templates[first] + "# changed\n"
    elif mutation == "bad-slot":
        slot_values[manifest.operations[0].path_slot] = "../outside.py"
    elif mutation == "bad-operation":
        manifest = replace(
            manifest,
            operations=(
                replace(manifest.operations[0], template_hash="sha256:" + "0" * 64),
            ),
        )
    else:
        text = "def ${undeclared}() -> None:\n    pass\n"
        template_hash = _hash(text.encode("utf-8"))
        templates.clear()
        templates[template_hash] = text
        manifest = replace(
            manifest,
            operations=(
                TemplateOperation(
                    kind="append_python_nodes",
                    path_slot=manifest.operations[0].path_slot,
                    template_hash=template_hash,
                ),
            ),
        )

    with pytest.raises(AtlasError) as captured:
        render_operations(manifest, envelope, request.before_files)
    assert captured.value.code == "invalid_render_bindings"


def test_episode_graph_serialization_and_sqlite_never_retain_source_or_receipt_secrets(
    tmp_path: Path,
) -> None:
    before = b"API_KEY = 'atlas-before-fake-secret'\n"
    request = extraction_request(
        before={"src/secret.py": before},
        after={"src/secret.py": before + b"\ndef helper() -> int:\n    return 1\n"},
        coverage_gaps=(
            CoverageGap(
                "src/secret.py",
                "PARSE_ERROR",
                "atlas-fake-gap-message",
            ),
        ),
    )
    changed = replace(
        request.changed_nodes[0],
        attributes=(("ignored", "atlas-fake-index-attribute"),),
    )
    command = replace(
        request.execution_receipts[1],
        command_spec=("python", "--atlas-fake-command-secret"),
        command_spec_hash=canonical_hash(("python", "--atlas-fake-command-secret")),
    )
    request = replace(
        request,
        changed_nodes=(changed,),
        execution_receipts=(request.execution_receipts[0], command),
    )
    result = PythonRecipeExtractor().extract(request)
    graph_json = canonical_json(
        {
            "nodes": [node.to_dict() for node in result.nodes],
            "edges": [edge.to_dict() for edge in result.edges],
        }
    )

    store = AtlasStore(tmp_path / "atlas.sqlite3", tmp_path / "cas")
    try:
        store.put_nodes(result.nodes)
        store.put_edges(result.edges)
    finally:
        store.close()
    sqlite_bytes = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    for forbidden in (
        "atlas-before-fake-secret",
        "atlas-fake-gap-message",
        "atlas-fake-index-attribute",
        "atlas-fake-command-secret",
    ):
        assert forbidden not in graph_json
        assert forbidden.encode("utf-8") not in sqlite_bytes


def test_fresh_process_renders_from_result_to_dict_canonical_json_envelope(
    tmp_path: Path,
) -> None:
    request = _primary_request()
    result = PythonRecipeExtractor().extract(request)
    assert result.manifest is not None
    payload_path = tmp_path / "render-payload.json"
    payload_path.write_text(
        canonical_json(
            {
                "manifest": result.manifest.to_dict(),
                "original_bindings": result.to_dict()["original_bindings"],
                "before_files": [
                    {
                        "path": item.path,
                        "content_hash": item.content_hash,
                        "body": base64.b64encode(item.body).decode("ascii"),
                    }
                    for item in request.before_files
                ],
            }
        ),
        encoding="utf-8",
    )
    mcp_tools = Path(__file__).resolve().parents[1]
    script = (
        "import base64, json, sys\n"
        "from code_atlas.extractors import render_operations\n"
        "from code_atlas.models import (ConstraintSpec, DependencySpec, RecipeManifest, SlotSpec, TemplateOperation, TestSpec)\n"
        "from project_index.checkpoints import CheckpointFile\n"
        "with open(sys.argv[1], encoding='utf-8') as stream:\n"
        "    payload = json.load(stream)\n"
        "data = payload['manifest']\n"
        "manifest = RecipeManifest(\n"
        "    **{**data,\n"
        "       'slots': tuple(SlotSpec(**item) for item in data['slots']),\n"
        "       'constraints': tuple(ConstraintSpec(**item) for item in data['constraints']),\n"
        "       'dependencies': tuple(DependencySpec(**item) for item in data['dependencies']),\n"
        "       'tests': tuple(TestSpec(**item) for item in data['tests']),\n"
        "       'operations': tuple(TemplateOperation(**item) for item in data['operations'])}\n"
        ")\n"
        "before = tuple(CheckpointFile(item['path'], item['content_hash'], base64.b64decode(item['body'])) for item in payload['before_files'])\n"
        "bindings = payload['original_bindings']\n"
        "rendered = render_operations(manifest, bindings, before)\n"
        "print(','.join(item.content_hash for item in rendered))\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        str(mcp_tools) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(payload_path)],
        cwd=mcp_tools,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ",".join(
        item.content_hash
        for item in sorted(request.after_files, key=lambda item: item.path)
    )
