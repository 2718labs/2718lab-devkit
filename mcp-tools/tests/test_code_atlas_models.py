"""Contract tests for the Code Atlas deterministic model boundary."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_atlas.canonical import canonical_json, normalize_intent_id
from code_atlas.models import AtlasEdge, AtlasError, AtlasNode, AtlasStatus, EdgeRelation, NodeKind
from code_atlas.security import validate_candidate_path


def test_node_id_excludes_mutable_metadata() -> None:
    first = AtlasNode.create(
        NodeKind.RECIPE,
        {"intent_id": "python.validation-guard"},
        created_at="2026-01-01T00:00:00Z",
        quarantine_state="pending",
    )
    second = AtlasNode.create(
        NodeKind.RECIPE,
        {"intent_id": "python.validation-guard"},
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

    with pytest.raises(AtlasError, match="invalid_edge_endpoints"):
        AtlasEdge.create(EdgeRelation.SOLVES, recipe, template)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Python Validation Guard ", "python.validation-guard"),
        ("python/validation_guard", "python.validation-guard"),
    ],
)
def test_normalize_intent_id_is_deterministic(raw: str, expected: str) -> None:
    assert normalize_intent_id(raw) == expected


@pytest.mark.parametrize("path", ["../escape.py", "src/generated_file.generated.py", "api_pb2.py"])
def test_candidate_path_rejects_unsafe_or_generated_paths(path: str) -> None:
    with pytest.raises(AtlasError):
        validate_candidate_path(path)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})
