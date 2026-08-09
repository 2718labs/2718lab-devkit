"""Contract tests for the private Continuity frozen-view foundation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_continuity.canonical import (  # noqa: E402
    canonical_frozen_view_manifest,
    canonical_json,
    cas_root_identity,
    manifest_identity,
    receipt_identity,
    view_identity,
)
from devkit_continuity.models import (  # noqa: E402
    BoundExecutionReceipt,
    ChangedNode,
    ContinuityAttempt,
    ContinuityError,
    ContinuityKey,
    ContinuityPointer,
    ContinuityReceipt,
    CoverageGap,
    FrozenEntry,
    FrozenView,
)
from devkit_runtime.config import RuntimeConfig  # noqa: E402
from project_index.models import IndexNode  # noqa: E402

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _key() -> ContinuityKey:
    return ContinuityKey(
        workflow_id="workflow-7",
        code_task_id="task-8",
        code_task_version=0,
        acceptance_id="acceptance-9",
        ingestion_key="ingest-10",
        payload_hash=HASH_A,
        evidence_binding_hash=HASH_B,
    )


def _entries() -> tuple[FrozenEntry, ...]:
    return (
        FrozenEntry("after_file", "src/a.py", HASH_A, 4),
        FrozenEntry("before_file", "src/a.py", HASH_B, 3),
    )


def _index_node(*, start_byte: int = 12) -> IndexNode:
    return IndexNode(
        node_id="node-1",
        kind="function",
        path="src/a.py",
        name="run",
        qualified_name="pkg.run",
        start_line=2,
        end_line=6,
        content_hash=HASH_A,
        attributes=(("decorator", "route"), ("visibility", "public")),
        extractor_id="python-ast",
        extractor_version="1.2",
        provenance="resolved",
        start_byte=start_byte,
        end_byte=96,
    )


def test_canonical_json_is_utf8_sorted_and_compact() -> None:
    assert canonical_json({"z": "雪", "a": [True, None]}) == '{"a":[true,null],"z":"雪"}'


def test_frozen_view_factory_binds_complete_canonical_manifest() -> None:
    key = _key()
    entries = _entries()
    view = FrozenView.create(
        key=key,
        entries=entries,
        input_snapshot_ids=("input-1",),
        output_snapshot_ids=("output-1",),
        checkpoint_ids=("checkpoint-1",),
        query_ids=("query-1",),
        verification_artifact_hashes=(HASH_C,),
        execution_receipt_ids=("execution-1",),
    )

    manifest = canonical_frozen_view_manifest(
        key,
        entries,
        input_snapshot_ids=("input-1",),
        output_snapshot_ids=("output-1",),
        checkpoint_ids=("checkpoint-1",),
        query_ids=("query-1",),
        verification_artifact_hashes=(HASH_C,),
        execution_receipt_ids=("execution-1",),
    )
    assert manifest["schema"] == "continuity-frozen-view/v1"
    assert manifest["key"]["evidence_binding_hash"] == HASH_B
    assert manifest["entries"] == [
        {"role": "after_file", "path": "src/a.py", "content_hash": HASH_A, "byte_length": 4},
        {"role": "before_file", "path": "src/a.py", "content_hash": HASH_B, "byte_length": 3},
    ]
    assert manifest["input_snapshot_ids"] == ["input-1"]
    assert manifest["output_snapshot_ids"] == ["output-1"]
    assert manifest["checkpoint_ids"] == ["checkpoint-1"]
    assert manifest["query_ids"] == ["query-1"]
    assert manifest["verification_artifact_hashes"] == [HASH_C]
    assert manifest["execution_receipt_ids"] == ["execution-1"]
    assert view.manifest_hash == manifest_identity(manifest)
    assert view.cas_root_hash == cas_root_identity(entries)
    assert view.view_id == view_identity(view.manifest_hash, view.cas_root_hash)
    assert view.manifest_json == canonical_json(manifest)


def test_identity_domains_are_explicit_and_distinct() -> None:
    payload = {"x": 1}
    identities = {
        manifest_identity(payload),
        cas_root_identity((FrozenEntry("before_file", "a", HASH_A, 1),)),
        view_identity(HASH_A, HASH_B),
        receipt_identity(_key(), HASH_A, "verified"),
    }
    assert len(identities) == 4
    assert all(value.startswith("sha256:") and len(value) == 71 for value in identities)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ContinuityKey("w", "t", -1, "a", "i", HASH_A, HASH_B),
        lambda: ContinuityKey("w", "t", 0, "a", "i", "not-a-hash", HASH_B),
        lambda: FrozenEntry("other", "a", HASH_A, 1),
        lambda: FrozenEntry("before_file", "/absolute", HASH_A, 1),
        lambda: FrozenEntry("before_file", "a/../b", HASH_A, 1),
        lambda: FrozenEntry("before_file", "a", HASH_A, -1),
        lambda: ContinuityPointer("w", "t", 0, HASH_A, 0, 1),
        lambda: ContinuityPointer("w", "t", 0, HASH_A, 1, 0),
        lambda: ContinuityAttempt(_key(), 0, "open", HASH_A, HASH_B),
        lambda: ContinuityReceipt(_key(), HASH_A, "invalid", "verified"),
    ],
)
def test_models_reject_invalid_values(factory: object) -> None:
    with pytest.raises(ContinuityError):
        factory()  # type: ignore[operator]


def test_changed_node_rejects_control_characters_in_identifiers() -> None:
    with pytest.raises(ContinuityError):
        ChangedNode("node\n1", "function", "src/a.py", HASH_A)


def test_view_rejects_unordered_or_duplicate_role_path_entries() -> None:
    duplicate = (
        FrozenEntry("before_file", "a", HASH_A, 1),
        FrozenEntry("before_file", "a", HASH_B, 1),
    )
    unordered = tuple(reversed(_entries()))
    with pytest.raises(ContinuityError):
        FrozenView(HASH_A, HASH_B, HASH_C, _key(), duplicate)
    with pytest.raises(ContinuityError):
        FrozenView(HASH_A, HASH_B, HASH_C, _key(), unordered)


@pytest.mark.parametrize("forged_field", ("view_id", "manifest_hash", "cas_root_hash"))
def test_direct_frozen_view_rejects_forged_canonical_identities(
    forged_field: str,
) -> None:
    view = FrozenView.create(key=_key(), entries=_entries())
    values = {
        "view_id": view.view_id,
        "manifest_hash": view.manifest_hash,
        "cas_root_hash": view.cas_root_hash,
    }
    values[forged_field] = HASH_A

    with pytest.raises(ContinuityError):
        FrozenView(
            values["view_id"],
            values["manifest_hash"],
            values["cas_root_hash"],
            view.key,
            view.entries,
        )


def test_direct_continuity_receipt_rejects_forged_receipt_hash() -> None:
    view = FrozenView.create(key=_key(), entries=_entries())

    with pytest.raises(ContinuityError):
        ContinuityReceipt(_key(), view.view_id, HASH_A, "verified")


def test_runtime_config_exposes_continuity_locations_without_io(tmp_path: Path) -> None:
    root = tmp_path / "data"
    config = RuntimeConfig.load(environ={"PLUGIN_DATA": str(root)})
    assert config.continuity_database == root / "continuity.sqlite3"
    assert config.continuity_cas_root == root / "continuity-cas"
    assert not root.exists()


def test_claimed_attempt_allows_no_view_or_receipt_but_later_states_do_not() -> None:
    claimed = ContinuityAttempt(_key(), 1, "claimed", None, None)
    assert claimed.view_id is None
    assert claimed.receipt_hash is None
    with pytest.raises(ContinuityError):
        ContinuityAttempt(_key(), 1, "frozen", None, None)
    with pytest.raises(ContinuityError):
        ContinuityAttempt(_key(), 1, "claimed", HASH_A, None)


def test_view_manifest_retains_typed_replay_metadata_and_order() -> None:
    receipt = BoundExecutionReceipt(
        receipt_id="receipt-1",
        kind="command",
        workflow_id="workflow-7",
        task_id="task-8",
        acceptance_id="acceptance-9",
        workspace_hash=HASH_A,
        output_snapshot_id="output-1",
        command_spec=("python", "-m", "pytest", "tests/test_a.py"),
        command_spec_hash=HASH_B,
        input_hash=HASH_C,
        output_hash=HASH_A,
        exit_code=0,
        success=True,
    )
    common = {
        "key": _key(),
        "entries": _entries(),
        "request_hash": HASH_A,
        "evidence_hash": HASH_B,
        "coverage_gaps": (CoverageGap("src/a.py", "PARSER_GAP", "unsupported"),),
        "execution_receipts": (receipt,),
    }
    first = FrozenView.create(
        **common,
        changed_nodes=(
            ChangedNode("node-a", "function", "src/a.py", HASH_A),
            ChangedNode("node-b", "class", "src/b.py", HASH_B),
        ),
    )
    same = FrozenView.create(
        **common,
        changed_nodes=(
            ChangedNode("node-a", "function", "src/a.py", HASH_A),
            ChangedNode("node-b", "class", "src/b.py", HASH_B),
        ),
    )
    reordered = FrozenView.create(
        **common,
        changed_nodes=(
            ChangedNode("node-b", "class", "src/b.py", HASH_B),
            ChangedNode("node-a", "function", "src/a.py", HASH_A),
        ),
    )

    assert first.key.key_hash == same.key.key_hash
    assert first.manifest_json == same.manifest_json
    assert first.view_id == same.view_id
    assert first.view_id != reordered.view_id
    manifest = canonical_frozen_view_manifest(
        _key(),
        _entries(),
        request_hash=HASH_A,
        evidence_hash=HASH_B,
        changed_nodes=first.changed_nodes,
        coverage_gaps=first.coverage_gaps,
        execution_receipts=first.execution_receipts,
    )
    assert manifest["request_hash"] == HASH_A
    assert manifest["evidence_hash"] == HASH_B
    assert manifest["changed_nodes"][0]["node_id"] == "node-a"
    assert manifest["coverage_gaps"][0]["code"] == "PARSER_GAP"
    assert manifest["execution_receipts"][0]["command_spec"] == [
        "python",
        "-m",
        "pytest",
        "tests/test_a.py",
    ]


def test_changed_node_converts_full_index_node_metadata_losslessly() -> None:
    source = _index_node()

    changed = ChangedNode.from_index_node(source)

    assert changed.to_dict() == {
        "node_id": "node-1",
        "kind": "function",
        "path": "src/a.py",
        "name": "run",
        "qualified_name": "pkg.run",
        "start_line": 2,
        "end_line": 6,
        "content_hash": HASH_A,
        "attributes": [["decorator", "route"], ["visibility", "public"]],
        "extractor_id": "python-ast",
        "extractor_version": "1.2",
        "provenance": "resolved",
        "start_byte": 12,
        "end_byte": 96,
    }


def test_retained_changed_node_metadata_binds_manifest_and_view_identity() -> None:
    first = FrozenView.create(
        key=_key(), entries=_entries(), changed_nodes=(ChangedNode.from_index_node(_index_node()),)
    )
    second = FrozenView.create(
        key=_key(),
        entries=_entries(),
        changed_nodes=(ChangedNode.from_index_node(_index_node(start_byte=13)),),
    )

    assert first.manifest_hash != second.manifest_hash
    assert first.view_id != second.view_id


@pytest.mark.parametrize(
    "metadata",
    [
        {"changed_nodes": ("not-a-node",)},
        {"coverage_gaps": ("not-a-gap",)},
        {"execution_receipts": ("not-a-receipt",)},
    ],
)
def test_view_factory_rejects_untyped_replay_metadata(metadata: dict[str, tuple[str]]) -> None:
    with pytest.raises(ContinuityError):
        FrozenView.create(key=_key(), entries=_entries(), **metadata)


def test_bound_receipt_rejects_host_absolute_paths_in_command_arguments() -> None:
    with pytest.raises(ContinuityError):
        BoundExecutionReceipt(
            "receipt-1",
            "command",
            "workflow-7",
            "task-8",
            "acceptance-9",
            HASH_A,
            "output-1",
            ("python", "--rootdir=/host-private"),
            HASH_B,
            HASH_C,
            HASH_A,
            0,
            True,
        )
