"""Contract tests for the Atlas deterministic model boundary."""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas import (
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
    freeze_json,
    normalize_intent_id,
    thaw_json,
    validate_candidate_path,
    validate_fragment,
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
        "TaskEpisode",
        "Intent",
        "Recipe",
        "CodeTemplate",
        "AdaptationSlot",
        "Constraint",
        "Dependency",
        "TestSpec",
        "ExecutionReceipt",
        "SourceEvidence",
        "Language",
        "Framework",
    ]
    assert [item.value for item in EdgeRelation] == [
        "SOLVES",
        "DERIVED_FROM",
        "HAS_IMPLEMENTATION",
        "HAS_SLOT",
        "CONSTRAINED_BY",
        "REQUIRES",
        "VERIFIED_BY",
        "CHANGES",
        "TESTS",
        "SUPERSEDES",
        "BUNDLED_AS",
    ]
    assert [item.value for item in AtlasStatus] == [
        "READY",
        "NO_VERIFIED_RECIPE",
        "INDEX_STALE",
        "AMBIGUOUS_MATCH",
        "UNSUPPORTED_LANGUAGE",
        "RENDER_INVALID",
        "EVIDENCE_INCOMPLETE",
        "RECIPE_QUARANTINED",
        "INGEST_PENDING",
        "ATLAS_UNAVAILABLE",
        "MODEL_UNAVAILABLE",
    ]


def test_edge_rejects_invalid_endpoint_kinds() -> None:
    recipe = AtlasNode.create(NodeKind.RECIPE, {"name": "recipe"})
    language = AtlasNode.create(NodeKind.LANGUAGE, {"name": "python"})

    with pytest.raises(ValueError, match="edge endpoints"):
        AtlasEdge.create(EdgeRelation.CHANGES, recipe, language)


@pytest.mark.parametrize(
    ("relation", "source_kind", "target_kind"),
    [
        (EdgeRelation.SOLVES, NodeKind.RECIPE, NodeKind.INTENT),
        (EdgeRelation.DERIVED_FROM, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.HAS_IMPLEMENTATION, NodeKind.RECIPE, NodeKind.CODE_TEMPLATE),
        (EdgeRelation.HAS_SLOT, NodeKind.RECIPE, NodeKind.ADAPTATION_SLOT),
        (EdgeRelation.CONSTRAINED_BY, NodeKind.RECIPE, NodeKind.CONSTRAINT),
        (EdgeRelation.REQUIRES, NodeKind.RECIPE, NodeKind.DEPENDENCY),
        (EdgeRelation.VERIFIED_BY, NodeKind.RECIPE, NodeKind.TEST_SPEC),
        (EdgeRelation.CHANGES, NodeKind.TASK_EPISODE, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.TESTS, NodeKind.TEST_SPEC, NodeKind.SOURCE_EVIDENCE),
        (EdgeRelation.SUPERSEDES, NodeKind.RECIPE, NodeKind.RECIPE),
        (EdgeRelation.BUNDLED_AS, NodeKind.RECIPE, NodeKind.SOURCE_EVIDENCE),
    ],
)
def test_edge_creation_succeeds_for_every_locked_relation(
    relation: EdgeRelation,
    source_kind: NodeKind,
    target_kind: NodeKind,
) -> None:
    source = AtlasNode.create(source_kind, {"role": "source"})
    target = AtlasNode.create(target_kind, {"role": "target"})
    edge = AtlasEdge.create(relation, source, target, created_at="2026-01-01T00:00:00Z")
    replaced = replace(edge, created_at="2026-02-01T00:00:00Z")
    assert edge.edge_id.startswith("sha256:")
    assert replaced.edge_id == edge.edge_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Python.Validation Guard ", "python.validation-guard"),
        ("python__pytest regression", "python-pytest-regression"),
    ],
)
def test_normalize_intent_id_is_deterministic(raw: str, expected: str) -> None:
    assert normalize_intent_id(raw) == expected


@pytest.mark.parametrize(
    "path", ["../secret.py", "/absolute.py", "vendor/x.py", "src/generated_pb2.py"]
)
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


@pytest.mark.parametrize(
    "payload",
    [
        [[]],
        [["a", 1]],
        {"pairs": [["a", 1]]},
        {"items": [{"name": "x"}, [1, {"nested": True}]]},
    ],
)
def test_frozen_json_round_trips_arrays_and_objects_unambiguously(
    payload: object,
) -> None:
    frozen = freeze_json(payload)
    node = AtlasNode.create(NodeKind.RECIPE, payload)
    assert thaw_json(frozen) == payload
    assert node.to_dict()["payload"] == payload
    assert canonical_json(node.payload) == canonical_json(payload)


@pytest.mark.parametrize(
    "payload",
    [[["a", 1]], {"pairs": [["a", 1]]}, {"nested": [["a", 1], {"x": []}]}],
)
def test_freeze_json_is_idempotent_for_tagged_containers(payload: object) -> None:
    first = freeze_json(payload)
    second = freeze_json(first)
    assert second == first
    assert thaw_json(second) == payload


@pytest.mark.parametrize(
    "fragment",
    [
        "GITHUB_TOKEN=ghp_exampleSecret123",
        "AWS_SECRET_ACCESS_KEY=exampleSecret123456",
        "token = exampleSecret123",
        "Authorization: Bearer abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
        "OPENAI_API_KEY=sk-exampleSecret123",
        "MY_GITHUB_TOKEN=ghp_exampleSecret123",
        "DATABASE_PASSWORD=exampleSecret123",
        "SERVICE_ACCESS_TOKEN=exampleSecret123",
        "CLIENT_SECRET=exampleSecret123",
        "sk-exampleSecret123",
        "ghp_exampleSecret123",
        "export OPENAI_API_KEY=sk-exampleSecret123",
        '{"OPENAI_API_KEY":"sk-exampleSecret123"}',
        'OPENAI_API_KEY: "sk-exampleSecret123"',
        'value = "ghp_exampleSecret123"',
        '{"value":"sk-exampleSecret123"}',
    ],
)
def test_fragment_rejects_credentials_without_returning_them(fragment: str) -> None:
    with pytest.raises(ValueError):
        validate_fragment(fragment)


@pytest.mark.parametrize("key", ["apiKey", "accessToken", "clientSecret", "privateKey"])
@pytest.mark.parametrize("syntax", ["assignment", "json", "yaml"])
def test_fragment_rejects_camel_case_credential_aliases(key: str, syntax: str) -> None:
    value = "exampleSecret123"
    fragment = {
        "assignment": f"{key}={value}",
        "json": f'{{"{key}":"{value}"}}',
        "yaml": f"{key}: {value}",
    }[syntax]

    with pytest.raises(AtlasError, match="credential_detected"):
        validate_fragment(fragment)


@pytest.mark.parametrize(
    "fragment",
    [
        "The apiKey belongs to the example configuration.",
        "apiKeyFactory = build_client()",
        "accessTokenizer = tokenize(value)",
        "clientSecretary = notify_team()",
        "privateKeyring = keyring.open()",
    ],
)
def test_fragment_allows_non_credential_alias_substrings(fragment: str) -> None:
    assert validate_fragment(fragment) == fragment


@pytest.mark.parametrize("fragment", ["bad\x01text", b"bad\x1ftext"])
def test_fragment_rejects_control_characters(fragment: str | bytes) -> None:
    with pytest.raises(ValueError):
        validate_fragment(fragment)


def test_fragment_allows_standard_text_whitespace() -> None:
    assert validate_fragment("one\ttwo\nthree\rfour") == "one\ttwo\nthree\rfour"


def test_provenance_must_be_locked_value() -> None:
    with pytest.raises(ValueError, match="provenance"):
        AtlasNode.create(NodeKind.RECIPE, {}, provenance="invented")


def test_schema_default_is_locked_to_one() -> None:
    node = AtlasNode.create(NodeKind.RECIPE, {})
    assert node.schema_version == "1"


def test_manifest_version_is_an_integer() -> None:
    with pytest.raises(ValueError, match="version"):
        RecipeManifest(
            recipe_id="r",
            recipe_key="key",
            version=True,
            intent_id="intent",
            language_name="python",
            language_extractor_version="1",
            repository_signature="repo",
            layer="layer",
            manifest_hash="hash",
        )


def test_locked_public_field_names_and_exports() -> None:
    assert "workspace_id" in ImplementationPacket.__dataclass_fields__
    assert "workspace" not in ImplementationPacket.__dataclass_fields__
    assert "workspace_root" not in ImplementationPacket.__dataclass_fields__
    assert "request_hash" in ImplementationPacket.__dataclass_fields__
    assert "matcher_version" in ImplementationPacket.__dataclass_fields__
    assert "target_paths" in ImplementationPacket.__dataclass_fields__
    assert "target_symbols" in ImplementationPacket.__dataclass_fields__
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
    with pytest.raises(ValueError):
        validate_slot_value("python_identifier", "class")
    with pytest.raises(ValueError):
        validate_slot_value("python_qualified_name", "package.class")
    with pytest.raises(ValueError):
        validate_candidate_path("src/module.py:secret")
    with pytest.raises(ValueError):
        validate_candidate_path("Vendor/X.py")


def test_candidate_path_rejects_workspace_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(ValueError):
        validate_candidate_path("linked/file.py", workspace=tmp_path)
