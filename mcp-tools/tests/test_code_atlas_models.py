"""Contract tests for the Code Atlas deterministic model boundary."""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas import (
    AtlasEdge,
    AtlasError,
    AtlasNode,
    AtlasStatus,
    EdgeRelation,
    ExtractionResult,
    ImplementationPacket,
    NodeKind,
    RecipeManifest,
    canonical_id,
    canonical_json,
    normalize_intent_id,
    validate_candidate_path,
    validate_slot_value,
)


def test_node_id_excludes_mutable_metadata_when_replaced() -> None:
    first = AtlasNode.create(
        NodeKind.RECIPE,
        {"intent_id": "python.validation-guard"},
        created_at="2026-01-01T00:00:00Z",
        quarantine_state="pending",
    )
    second = replace(
        first,
        created_at="2026-02-01T00:00:00Z",
        quarantine_state="accepted",
    )

    assert first.node_id == second.node_id


def test_enums_match_locked_contract_values() -> None:
    assert [item.value for item in NodeKind] == [
        "TaskEpisode", "Intent", "Recipe", "CodeTemplate", "AdaptationSlot", "Constraint",
        "Dependency", "TestSpec", "ExecutionReceipt", "SourceEvidence", "Language", "Framework",
    ]
    assert [item.value for item in EdgeRelation] == [
        "SOLVES", "DERIVED_FROM", "HAS_IMPLEMENTATION", "HAS_SLOT", "CONSTRAINED_BY", "REQUIRES",
        "VERIFIED_BY", "CHANGES", "TESTS", "SUPERSEDES", "BUNDLED_AS",
    ]
    assert [item.value for item in AtlasStatus] == [
        "READY", "NO_VERIFIED_RECIPE", "INDEX_STALE", "AMBIGUOUS_MATCH", "UNSUPPORTED_LANGUAGE",
        "RENDER_INVALID", "EVIDENCE_INCOMPLETE", "RECIPE_QUARANTINED", "INGEST_PENDING",
        "ATLAS_UNAVAILABLE", "MODEL_UNAVAILABLE",
    ]


def test_edge_rejects_invalid_endpoint_kinds() -> None:
    recipe = AtlasNode.create(NodeKind.RECIPE, {"name": "recipe"})
    template = AtlasNode.create(NodeKind.CODE_TEMPLATE, {"name": "template"})

    language = AtlasNode.create(NodeKind.LANGUAGE, {"name": "python"})

    with pytest.raises(ValueError, match="edge endpoints"):
        AtlasEdge.create(EdgeRelation.CHANGES, recipe, language)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Python.Validation Guard ", "python.validation-guard"),
        ("python__pytest regression", "python-pytest-regression"),
    ],
)
def test_normalize_intent_id_is_deterministic(raw: str, expected: str) -> None:
    assert normalize_intent_id(raw) == expected


@pytest.mark.parametrize("path", ["../secret.py", "/absolute.py", "vendor/x.py", "src/generated_pb2.py"])
def test_candidate_path_rejects_unsafe_or_generated_paths(path: str) -> None:
    with pytest.raises(AtlasError):
        validate_candidate_path(path)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_id("Recipe", {"weight": value})


def test_tuple_payload_is_immutable_and_serializes_as_json_containers() -> None:
    node = AtlasNode.create(NodeKind.RECIPE, {"intent_id": ["python", "guard"]})
    assert node.payload == (("intent_id", ("python", "guard")),)
    assert node.to_dict()["payload"] == {"intent_id": ["python", "guard"]}


def test_provenance_must_be_locked_value() -> None:
    with pytest.raises(ValueError, match="provenance"):
        AtlasNode.create(NodeKind.RECIPE, {}, provenance="invented")


def test_schema_default_is_locked_to_one() -> None:
    node = AtlasNode.create(NodeKind.RECIPE, {})
    assert node.schema_version == "1"


def test_manifest_version_is_an_integer() -> None:
    with pytest.raises(ValueError, match="version"):
        RecipeManifest(
            recipe_id="r", recipe_key="key", version=True, intent_id="intent", language_name="python",
            language_extractor_version="1", repository_signature="repo", layer="layer", manifest_hash="hash",
        )


def test_locked_public_field_names_and_exports() -> None:
    assert "workspace" in ImplementationPacket.__dataclass_fields__
    assert "workspace_id" not in ImplementationPacket.__dataclass_fields__
    assert "original_bindings" in ExtractionResult.__dataclass_fields__
    assert "bindings" not in ExtractionResult.__dataclass_fields__
    assert callable(canonical_json)
    assert callable(validate_candidate_path)


def test_slot_and_path_edge_cases() -> None:
    with pytest.raises(ValueError):
        validate_candidate_path("SRC/GENERATED_PB2.PY")
    with pytest.raises(ValueError):
        validate_slot_value("python_statement_block", "return 1")
    assert validate_slot_value("python_statement_block", "value = 1") == "value = 1"
