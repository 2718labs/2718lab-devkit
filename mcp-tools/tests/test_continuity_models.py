"""Contract tests for the private Continuity frozen-view foundation."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_continuity.canonical import (  # noqa: E402
    canonical_frozen_view_manifest,
    canonical_hash,
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
    ReplayMetadata,
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


def _bound_receipt(
    *,
    exit_code: int = 0,
    kind: str = "command",
    command_spec: object = ("python", "-m", "pytest", "tests/test_a.py"),
    command_spec_hash: str | None = None,
) -> BoundExecutionReceipt:
    if command_spec_hash is None:
        command_spec_hash = canonical_hash(command_spec)
    return BoundExecutionReceipt(
        receipt_id="receipt-1",
        kind=kind,
        workflow_id="workflow-7",
        task_id="task-8",
        acceptance_id="acceptance-9",
        workspace_hash=HASH_A,
        output_snapshot_id="output-1",
        command_spec=command_spec,  # type: ignore[arg-type]
        command_spec_hash=command_spec_hash,
        input_hash=HASH_C,
        output_hash=HASH_A,
        exit_code=exit_code,
        success=True,
    )


def test_bound_receipt_accepts_empty_patch_like_command_spec_with_canonical_hash() -> None:
    receipt = _bound_receipt(kind="write", command_spec=())

    assert receipt.kind == "write"
    assert receipt.command_spec == ()
    assert receipt.command_spec_hash == canonical_hash(())


@pytest.mark.parametrize(
    ("kind", "command_spec"),
    (("write", ()), ("command", ("python", "-m", "pytest"))),
)
def test_bound_receipt_rejects_noncanonical_command_spec_hash(
    kind: str, command_spec: tuple[str, ...]
) -> None:
    with pytest.raises(ContinuityError, match="^COMMAND_SPEC_HASH_MISMATCH$"):
        _bound_receipt(kind=kind, command_spec=command_spec, command_spec_hash=HASH_A)


def test_bound_receipt_accepts_nonempty_canonical_command_spec_hash() -> None:
    receipt = _bound_receipt(command_spec=("python", "-m", "pytest"))

    assert receipt.command_spec_hash == canonical_hash(("python", "-m", "pytest"))


def _replay_metadata() -> ReplayMetadata:
    return ReplayMetadata(
        task_kind="code",
        intent_id="intent-1",
        workspace_hash=HASH_A,
        write_scope=("src/a.py",),
        indexed_diff_hash=HASH_B,
        language="python",
        framework="pytest",
        checkpoint_hash=HASH_C,
    )


def _v2_bound_receipt(
    metadata: ReplayMetadata | None = None,
) -> BoundExecutionReceipt:
    metadata = _replay_metadata() if metadata is None else metadata
    return BoundExecutionReceipt(
        receipt_id=HASH_B,
        kind="command",
        workflow_id="workflow-7",
        task_id="task-8",
        acceptance_id="acceptance-9",
        workspace_hash=metadata.workspace_hash,
        output_snapshot_id="output-1",
        command_spec=("python",),
        command_spec_hash=canonical_hash(("python",)),
        input_hash=HASH_B,
        output_hash=HASH_C,
        exit_code=0,
        success=True,
    )


def _v2_view_inputs(
    metadata: ReplayMetadata | None = None,
    *,
    verification_artifact_hashes: tuple[str, ...] = (HASH_A,),
    execution_receipt_ids: tuple[str, ...] = (HASH_B,),
) -> dict[str, object]:
    key = _key()
    metadata = _replay_metadata() if metadata is None else metadata
    input_snapshot_id, output_snapshot_id = "input-1", "output-1"
    checkpoint_id, query_id = "checkpoint-1", "query-1"
    artifact_hashes = verification_artifact_hashes
    receipt_ids = execution_receipt_ids
    request_payload = {
        "ingestion_key": key.ingestion_key,
        "payload_hash": key.payload_hash,
        "acceptance_id": key.acceptance_id,
        "workflow_id": key.workflow_id,
        "code_task_id": key.code_task_id,
        "code_task_version": key.code_task_version,
        "input_snapshot_id": input_snapshot_id,
        "output_snapshot_id": output_snapshot_id,
        "indexed_diff_hash": metadata.indexed_diff_hash,
        "intent_id": metadata.intent_id,
        "language": metadata.language,
        "framework": metadata.framework,
        "checkpoint_id": checkpoint_id,
        "checkpoint_hash": metadata.checkpoint_hash,
        "output_query_trace_id": query_id,
        "verification_artifact_hashes": artifact_hashes,
        "execution_receipt_ids": receipt_ids,
        "evidence_binding_hash": key.evidence_binding_hash,
    }
    evidence_payload = {
        "code_task_version": key.code_task_version,
        "language": metadata.language,
        "framework": metadata.framework,
        "checkpoint_hash": metadata.checkpoint_hash,
        "indexed_diff_hash": metadata.indexed_diff_hash,
        "output_query_trace_id": query_id,
        "verification_artifact_hashes": artifact_hashes,
    }
    return {
        "key": key,
        "entries": _entries(),
        "input_snapshot_ids": (input_snapshot_id,),
        "output_snapshot_ids": (output_snapshot_id,),
        "checkpoint_ids": (checkpoint_id,),
        "query_ids": (query_id,),
        "verification_artifact_hashes": artifact_hashes,
        "execution_receipt_ids": receipt_ids,
        "request_hash": canonical_hash(request_payload),
        "evidence_hash": canonical_hash(evidence_payload),
        "replay_metadata": metadata,
        "execution_receipts": (_v2_bound_receipt(metadata),),
    }


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


def test_replay_metadata_upgrades_manifest_to_typed_v2_identity() -> None:
    metadata = _replay_metadata()
    common = _v2_view_inputs(metadata)

    view = FrozenView.create(**common)
    manifest = canonical_frozen_view_manifest(**common)

    assert manifest["schema"] == "continuity-frozen-view/v2"
    assert manifest["replay_metadata"] == metadata.to_dict()
    assert view.replay_metadata == metadata
    assert view.manifest_json == canonical_json(manifest)


def test_empty_write_receipt_can_enter_v2_frozen_view() -> None:
    receipt = replace(
        _v2_bound_receipt(),
        kind="write",
        command_spec=(),
        command_spec_hash=canonical_hash(()),
    )

    view = FrozenView.create(**(_v2_view_inputs() | {"execution_receipts": (receipt,)}))

    assert view.execution_receipts == (receipt,)


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, intent_id="intent-2"),
        lambda value: replace(value, workspace_hash=HASH_B),
        lambda value: replace(value, write_scope=("src/b.py",)),
        lambda value: replace(value, indexed_diff_hash=HASH_C),
        lambda value: replace(value, language="rust"),
        lambda value: replace(value, framework=""),
        lambda value: replace(value, checkpoint_hash=HASH_A),
    ),
)
def test_each_replay_metadata_field_binds_identity_and_direct_constructor(
    changed: object,
) -> None:
    metadata = _replay_metadata()
    first = FrozenView.create(**_v2_view_inputs(metadata))
    altered_metadata = changed(metadata)  # type: ignore[operator]
    altered = FrozenView.create(**_v2_view_inputs(altered_metadata))

    assert first.view_id != altered.view_id
    with pytest.raises(ContinuityError):
        replace(first, replay_metadata=altered_metadata)


@pytest.mark.parametrize(
    "metadata",
    (
        lambda: ReplayMetadata("other", "intent", HASH_A, ("src/a.py",), HASH_B, "python", "", HASH_C),
        lambda: ReplayMetadata("code", "intent", HASH_A, ("/absolute",), HASH_B, "python", "", HASH_C),
        lambda: ReplayMetadata("code", "intent", HASH_A, ("node_modules/a.py",), HASH_B, "python", "", HASH_C),
        lambda: ReplayMetadata("code", "intent", HASH_A, ("src/a.py", "src/a.py"), HASH_B, "python", "", HASH_C),
        lambda: ReplayMetadata("code", "intent", HASH_A, ("src/b.py", "src/a.py"), HASH_B, "python", "", HASH_C),
        lambda: ReplayMetadata("code", "intent", HASH_A, ("src/a.py",), HASH_B, "python", "\n", HASH_C),
    ),
)
def test_replay_metadata_rejects_unsafe_or_nondeterministic_values(metadata: object) -> None:
    with pytest.raises(ContinuityError):
        metadata()  # type: ignore[operator]


def test_replay_metadata_normalizes_atlas_compatible_scope_separator() -> None:
    metadata = ReplayMetadata(
        "code", "intent", HASH_A, ("src\\a.py",), HASH_B, "python", "", HASH_C
    )

    assert metadata.write_scope == ("src/a.py",)


def test_replay_metadata_rejects_write_scope_larger_than_atlas_limit() -> None:
    scope = tuple(f"src/{index}.py" for index in range(9))

    with pytest.raises(ContinuityError):
        ReplayMetadata("code", "intent", HASH_A, scope, HASH_B, "python", "", HASH_C)


@pytest.mark.parametrize("field", ("verification_artifact_hashes", "execution_receipt_ids"))
def test_v2_replay_metadata_rejects_evidence_over_atlas_limit(field: str) -> None:
    values = tuple(f"sha256:{index:064x}" for index in range(33))

    with pytest.raises(ContinuityError):
        FrozenView.create(**_v2_view_inputs(**{field: values}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, receipt_id=HASH_C),
        lambda value: replace(value, workflow_id="workflow-8"),
        lambda value: replace(value, task_id="task-9"),
        lambda value: replace(value, acceptance_id="acceptance-10"),
        lambda value: replace(value, workspace_hash=HASH_C),
        lambda value: replace(value, output_snapshot_id="output-2"),
    ),
)
def test_v2_execution_receipts_bind_to_frozen_view_replay_context(changed: object) -> None:
    receipt = _v2_bound_receipt()

    with pytest.raises(ContinuityError):
        FrozenView.create(
            **(_v2_view_inputs() | {"execution_receipts": (changed(receipt),)})  # type: ignore[operator,arg-type]
        )


@pytest.mark.parametrize(
    "override",
    (
        {"input_snapshot_ids": ("input-1", "input-2")},
        {"output_snapshot_ids": ("output-1", "output-2")},
        {"checkpoint_ids": ("checkpoint-1", "checkpoint-2")},
        {"query_ids": ("query-1", "query-2")},
        {"verification_artifact_hashes": ()},
        {"verification_artifact_hashes": (HASH_B, HASH_A)},
        {"execution_receipt_ids": ()},
        {"execution_receipt_ids": (HASH_C, HASH_B)},
    ),
)
def test_v2_replay_metadata_requires_exact_atlas_cardinality_and_order(
    override: dict[str, object],
) -> None:
    with pytest.raises(ContinuityError):
        FrozenView.create(**(_v2_view_inputs() | override))  # type: ignore[arg-type]


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
        command_spec_hash=canonical_hash(("python", "-m", "pytest", "tests/test_a.py")),
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
            canonical_hash(("python", "--rootdir=/host-private")),
            HASH_C,
            HASH_A,
            0,
            True,
        )


@pytest.mark.parametrize(
    "atom",
    (
        "/host-private",
        "C:\\host-private",
        "\\\\server\\share",
        "//server/share",
        "file:///host-private",
        "FILE:///host-private",
        "--rootdir=/host-private",
        "--rootdir=C:\\host-private",
        "--rootdir=\\\\server\\share",
        "--rootdir=//server/share",
        "--rootdir=file:///host-private",
        "--rootdir=FILE:///host-private",
    ),
)
def test_bound_receipt_rejects_absolute_paths_in_all_command_atom_forms(atom: str) -> None:
    with pytest.raises(ContinuityError):
        _bound_receipt(command_spec=("python", atom))


@pytest.mark.parametrize(
    "factory",
    (
        lambda: FrozenEntry([], "src/a.py", HASH_A, 1),
        lambda: ChangedNode("node-1", "function", "src/a.py", HASH_A, provenance=[]),
        lambda: _bound_receipt(command_spec=None),
        lambda: FrozenView(HASH_A, HASH_B, HASH_C, _key(), None),
    ),
)
def test_model_constructors_normalize_hostile_inputs(factory: object) -> None:
    with pytest.raises(ContinuityError):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "changed",
    (
        lambda key: replace(key, workflow_id="workflow-8"),
        lambda key: replace(key, code_task_id="task-9"),
        lambda key: replace(key, code_task_version=1),
        lambda key: replace(key, acceptance_id="acceptance-10"),
        lambda key: replace(key, ingestion_key="ingest-11"),
        lambda key: replace(key, payload_hash=HASH_C),
        lambda key: replace(key, evidence_binding_hash=HASH_C),
    ),
)
def test_each_continuity_key_field_binds_key_identity(changed: object) -> None:
    key = _key()
    altered = changed(key)  # type: ignore[operator]

    assert key.key_hash != altered.key_hash


@pytest.mark.parametrize(
    "changed",
    (
        lambda entry: replace(entry, role="before_file"),
        lambda entry: replace(entry, path="src/b.py"),
        lambda entry: replace(entry, content_hash=HASH_C),
        lambda entry: replace(entry, byte_length=5),
    ),
)
def test_each_frozen_entry_field_binds_cas_identity(changed: object) -> None:
    entry = FrozenEntry("after_file", "src/a.py", HASH_A, 4)
    altered = changed(entry)  # type: ignore[operator]

    assert cas_root_identity((entry,)) != cas_root_identity((altered,))


def test_equal_frozen_view_inputs_are_byte_identical() -> None:
    common = {
        "key": _key(),
        "entries": _entries(),
        "input_snapshot_ids": ("input-1",),
        "output_snapshot_ids": ("output-1",),
        "checkpoint_ids": ("checkpoint-1",),
        "query_ids": ("query-1",),
        "verification_artifact_hashes": (HASH_C,),
        "execution_receipt_ids": ("execution-1",),
        "request_hash": HASH_A,
        "evidence_hash": HASH_B,
        "changed_nodes": (ChangedNode.from_index_node(_index_node()),),
        "coverage_gaps": (CoverageGap("src/a.py", "PARSER_GAP", "unsupported"),),
        "execution_receipts": (_bound_receipt(),),
    }

    first = FrozenView.create(**common)
    second = FrozenView.create(**common)

    assert first.manifest_json.encode("utf-8") == second.manifest_json.encode("utf-8")
    assert first.view_id == second.view_id


@pytest.mark.parametrize(
    "override",
    (
        {"input_snapshot_ids": ("input-2",)},
        {"output_snapshot_ids": ("output-2",)},
        {"checkpoint_ids": ("checkpoint-2",)},
        {"query_ids": ("query-2",)},
        {"verification_artifact_hashes": (HASH_A,)},
        {"execution_receipt_ids": ("execution-2",)},
        {"request_hash": HASH_C},
        {"evidence_hash": HASH_C},
        {"changed_nodes": (ChangedNode.from_index_node(_index_node(start_byte=13)),)},
        {"coverage_gaps": (CoverageGap("src/a.py", "PARSER_GAP", "different"),)},
        {"execution_receipts": (_bound_receipt(exit_code=1),)},
    ),
)
def test_each_manifest_payload_component_binds_view_identity(
    override: dict[str, object],
) -> None:
    common: dict[str, object] = {
        "key": _key(),
        "entries": _entries(),
        "input_snapshot_ids": ("input-1",),
        "output_snapshot_ids": ("output-1",),
        "checkpoint_ids": ("checkpoint-1",),
        "query_ids": ("query-1",),
        "verification_artifact_hashes": (HASH_C,),
        "execution_receipt_ids": ("execution-1",),
        "request_hash": HASH_A,
        "evidence_hash": HASH_B,
        "changed_nodes": (ChangedNode.from_index_node(_index_node()),),
        "coverage_gaps": (CoverageGap("src/a.py", "PARSER_GAP", "unsupported"),),
        "execution_receipts": (_bound_receipt(),),
    }
    first = FrozenView.create(**common)  # type: ignore[arg-type]
    second = FrozenView.create(**(common | override))  # type: ignore[arg-type]

    assert first.manifest_hash != second.manifest_hash
    assert first.view_id != second.view_id


@pytest.mark.parametrize(
    "changed",
    (
        lambda key, view: (replace(key, workflow_id="workflow-8"), view, "verified"),
        lambda key, view: (key, FrozenView.create(key=key, entries=(FrozenEntry("after_file", "src/b.py", HASH_A, 4),)), "verified"),
        lambda key, view: (key, view, "frozen"),
    ),
)
def test_each_continuity_receipt_input_binds_receipt_identity(changed: object) -> None:
    key = _key()
    view = FrozenView.create(key=key, entries=_entries())
    base = ContinuityReceipt.create(key=key, view_id=view.view_id, kind="verified")
    altered_key, altered_view, altered_kind = changed(key, view)  # type: ignore[operator]
    altered = ContinuityReceipt.create(
        key=altered_key,
        view_id=altered_view.view_id,
        kind=altered_kind,
    )

    assert base.receipt_hash != altered.receipt_hash


@pytest.mark.parametrize(
    "factory",
    (
        lambda: FrozenView.create(key=None, entries=_entries()),
        lambda: FrozenView.create(key=_key(), entries=_entries(), request_hash="not-a-hash"),
        lambda: FrozenView.create(key=_key(), entries=tuple(reversed(_entries()))),
        lambda: FrozenView.create(
            key=_key(),
            entries=(
                FrozenEntry("before_file", "src/a.py", HASH_A, 1),
                FrozenEntry("before_file", "src/a.py", HASH_B, 2),
            ),
        ),
    ),
)
def test_frozen_view_factory_normalizes_invalid_inputs(factory: object) -> None:
    with pytest.raises(ContinuityError):
        factory()  # type: ignore[operator]


def _view_for_metadata(**overrides: object) -> FrozenView:
    common: dict[str, object] = {
        "key": _key(),
        "entries": _entries(),
        "changed_nodes": (ChangedNode.from_index_node(_index_node()),),
        "coverage_gaps": (CoverageGap("src/a.py", "PARSER_GAP", "unsupported"),),
        "execution_receipts": (_bound_receipt(),),
    }
    return FrozenView.create(**(common | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, node_id="node-2"),
        lambda value: replace(value, kind="class"),
        lambda value: replace(value, path="src/b.py"),
        lambda value: replace(value, content_hash=HASH_B),
        lambda value: replace(value, name="other"),
        lambda value: replace(value, qualified_name="pkg.other"),
        lambda value: replace(value, start_line=3),
        lambda value: replace(value, end_line=7),
        lambda value: replace(value, attributes=(("decorator", "other"),)),
        lambda value: replace(value, extractor_id="other-parser"),
        lambda value: replace(value, extractor_version="2.0"),
        lambda value: replace(value, provenance="declared"),
        lambda value: replace(value, start_byte=13),
        lambda value: replace(value, end_byte=97),
    ),
)
def test_each_changed_node_field_binds_manifest_and_view_identity(changed: object) -> None:
    node = ChangedNode.from_index_node(_index_node())
    first = _view_for_metadata(changed_nodes=(node,))
    second = _view_for_metadata(changed_nodes=(changed(node),))  # type: ignore[operator]

    assert first.manifest_hash != second.manifest_hash
    assert first.view_id != second.view_id


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, path="src/b.py"),
        lambda value: replace(value, code="OTHER_GAP"),
        lambda value: replace(value, message="different"),
    ),
)
def test_each_coverage_gap_field_binds_manifest_and_view_identity(changed: object) -> None:
    gap = CoverageGap("src/a.py", "PARSER_GAP", "unsupported")
    first = _view_for_metadata(coverage_gaps=(gap,))
    second = _view_for_metadata(coverage_gaps=(changed(gap),))  # type: ignore[operator]

    assert first.manifest_hash != second.manifest_hash
    assert first.view_id != second.view_id


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, receipt_id="receipt-2"),
        lambda value: replace(value, kind="test"),
        lambda value: replace(value, workflow_id="workflow-8"),
        lambda value: replace(value, task_id="task-9"),
        lambda value: replace(value, acceptance_id="acceptance-10"),
        lambda value: replace(value, workspace_hash=HASH_B),
        lambda value: replace(value, output_snapshot_id="output-2"),
        lambda value: replace(
            value,
            command_spec=("python", "-m", "compileall"),
            command_spec_hash=canonical_hash(("python", "-m", "compileall")),
        ),
        lambda value: replace(value, input_hash=HASH_A),
        lambda value: replace(value, output_hash=HASH_B),
        lambda value: replace(value, exit_code=1),
        lambda value: replace(value, success=False),
    ),
)
def test_each_bound_receipt_field_binds_manifest_and_view_identity(changed: object) -> None:
    receipt = _bound_receipt()
    first = _view_for_metadata(execution_receipts=(receipt,))
    second = _view_for_metadata(execution_receipts=(changed(receipt),))  # type: ignore[operator]

    assert first.manifest_hash != second.manifest_hash
    assert first.view_id != second.view_id
