"""Strict public result-envelope and projector contracts."""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devkit_atlas.models import (  # noqa: E402
    AcceptanceProjection,
    AtlasNode,
    AtlasStatus,
    GraphQueryResult,
    ImplementationPacket,
    NodeKind,
    PreparationResult,
    RenderResult,
)
from devkit_relay.compiler import RelayPlanError  # noqa: E402
from devkit_runtime.tool_result import (  # noqa: E402
    RESULT_SCHEMA,
    TOOL_ANNOTATIONS,
    ResultContractError,
    RuntimeContractError,
    envelope_failure,
    envelope_success,
    project_atlas_accept,
    project_atlas_prepare,
    project_atlas_query,
    project_atlas_render,
    project_checkpoint_create,
    project_checkpoint_restore,
    project_checkpoint_status,
    project_index_query,
    project_index_register,
    project_index_status,
    project_index_sync,
    project_relay_compile,
    project_relay_handoff,
    project_relay_integrate,
    project_relay_start,
    project_relay_status,
    result_from_exception,
)
from project_index.checkpoints import Checkpoint, RestoreResult  # noqa: E402
from project_index.models import (  # noqa: E402
    CoverageGap,
    IndexEdge,
    IndexError,
    IndexNode,
    IndexSnapshot,
    IndexState,
    IndexStatus,
    QueryResult,
    SourceWindow,
)


def _data(result: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], result["data"])


def _assert_no_null(value: object) -> None:
    assert value is not None
    if isinstance(value, dict):
        for key, item in value.items():
            assert key is not None
            _assert_no_null(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_null(item)


def test_success_and_failure_envelopes_have_exact_top_level_keys() -> None:
    success = envelope_success({})
    failure = envelope_failure("INVALID_REQUEST")

    assert success == {"schema": RESULT_SCHEMA, "ok": True, "data": {}}
    assert failure == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {"code": "INVALID_REQUEST", "message": "request rejected"},
    }
    assert set(success) == {"schema", "ok", "data"}
    assert set(failure) == {"schema", "ok", "error"}


@pytest.mark.parametrize("value", [None, [], "text", 1, True])
def test_success_requires_an_object_data_value(value: object) -> None:
    with pytest.raises(ResultContractError):
        envelope_success(value)


@pytest.mark.parametrize(
    "value",
    [
        {"value": None},
        {"nested": {"value": None}},
        {"nested": [{"value": None}]},
        {"nested": [None]},
    ],
)
def test_success_rejects_null_values_at_any_recursive_depth(value: object) -> None:
    with pytest.raises(ResultContractError):
        envelope_success(value)


def test_success_rejects_deep_nesting_before_recursion_failure() -> None:
    value: object = "leaf"
    for _ in range(40):
        value = {"child": value}

    with pytest.raises(ResultContractError):
        envelope_success({"root": value})


@pytest.mark.parametrize(
    "value",
    [
        {"items": list(range(513))},
        {"items": {str(index): index for index in range(513)}},
    ],
)
def test_success_rejects_oversized_list_and_map_cardinality(value: object) -> None:
    with pytest.raises(ResultContractError):
        envelope_success(value)


def test_success_rejects_cyclic_containers() -> None:
    value: dict[str, object] = {}
    value["self"] = value

    with pytest.raises(ResultContractError):
        envelope_success(value)


@dataclass(frozen=True)
class UnknownObject:
    value: str


@pytest.mark.parametrize(
    "value",
    [
        {"path": Path("relative.py")},
        {"body": b"source"},
        {"value": UnknownObject("opaque")},
        {"value": math.nan},
        {1: "non-string key"},
    ],
)
def test_envelope_has_no_generic_fallbacks(value: object) -> None:
    with pytest.raises(ResultContractError):
        envelope_success(value)


@pytest.mark.parametrize(
    "tool, expected",
    [
        ("project_index_register", (False, False, True, False)),
        ("project_index_sync", (False, False, True, False)),
        ("project_index_status", (True, False, True, False)),
        ("project_index_query", (False, False, True, False)),
        ("worktree_checkpoint_create", (False, False, True, False)),
        ("worktree_checkpoint_status", (True, False, True, False)),
        ("worktree_checkpoint_restore", (False, True, False, False)),
        ("atlas_query", (True, False, True, False)),
        ("atlas_prepare", (False, False, True, False)),
        ("atlas_render", (True, False, True, False)),
        ("atlas_accept", (False, False, True, False)),
        ("relay_compile", (True, False, True, False)),
        ("relay_start", (False, False, True, False)),
        ("relay_status", (True, False, True, False)),
        ("relay_handoff", (False, False, False, False)),
        ("relay_integrate", (False, True, False, False)),
    ],
)
def test_annotation_table_is_exact(tool: str, expected: tuple[bool, ...]) -> None:
    assert TOOL_ANNOTATIONS[tool] == expected
    assert len(TOOL_ANNOTATIONS) == 16


def test_index_projectors_are_closed_and_source_windows_never_include_text() -> None:
    snapshot = IndexSnapshot(
        snapshot_id="snapshot-1",
        workspace="workspace-1",
        workspace_id="workspace-1",
        state=IndexState.INDEX_READY,
        file_count=1,
        blob_count=1,
        reused_blob_count=0,
        node_count=1,
        edge_count=0,
        gap_count=1,
        manifest_hash="sha256:" + "a" * 64,
        parser_set_hash="sha256:" + "b" * 64,
    )
    status = IndexStatus(
        workspace="workspace-1",
        snapshot_id="snapshot-1",
        state=IndexState.INDEX_READY,
        gaps=(CoverageGap("src/a.py", "GAP", "bounded reason"),),
    )
    node = IndexNode(
        node_id="node-1",
        kind="function",
        path="src/a.py",
        name="run",
        qualified_name="a.run",
        start_line=1,
        end_line=2,
        content_hash="sha256:" + "c" * 64,
    )
    edge = IndexEdge("edge-1", "node-1", "node-1", "CALLS")
    query = QueryResult(
        trace_id="trace-1",
        snapshot_id="snapshot-1",
        state=IndexState.INDEX_READY,
        nodes=(node,),
        edges=(edge,),
        source_windows=(
            SourceWindow(
                "src/a.py", 1, 2, "print('must stay private')", "sha256:" + "d" * 64
            ),
        ),
        gaps=(),
        truncated=False,
    )

    sync_data = _data(project_index_sync(snapshot))
    status_data = _data(project_index_status(status))
    query_data = _data(project_index_query(query))
    _assert_no_null(sync_data)
    _assert_no_null(status_data)
    _assert_no_null(query_data)

    assert set(sync_data) == {
        "workspace_id",
        "snapshot_id",
        "state",
        "file_count",
        "blob_count",
        "reused_blob_count",
        "node_count",
        "edge_count",
        "gap_count",
        "manifest_hash",
        "parser_set_hash",
        "binding_state",
    }
    assert set(status_data) == {
        "workspace_id",
        "snapshot_id",
        "state",
        "required_paths",
        "missing_paths",
        "changed_paths",
        "gaps",
        "binding_state",
    }
    assert set(query_data) == {
        "trace_id",
        "snapshot_id",
        "state",
        "nodes",
        "edges",
        "source_windows",
        "gaps",
        "truncated",
    }
    assert query_data["source_windows"] == [
        {
            "path": "src/a.py",
            "start_line": 1,
            "end_line": 2,
            "content_hash": "sha256:" + "d" * 64,
        }
    ]
    assert "text" not in str(query_data)
    assert "print('must stay private')" not in str(query_data)


def test_checkpoint_projectors_reject_legacy_workspace_root() -> None:
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        workflow_id="workflow-1",
        task_id="task-1",
        owner="owner-1",
        lease_epoch=1,
        workspace_root="C:/host/source",
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        write_scope=("src",),
        write_scope_hash="sha256:" + "a" * 64,
        manifest_hash="sha256:" + "b" * 64,
        cas_root_hash="sha256:" + "c" * 64,
        entry_count=1,
        kind="checkpoint",
    )
    with pytest.raises(ResultContractError):
        project_checkpoint_status(checkpoint)


def test_checkpoint_projectors_expose_only_bounded_metadata() -> None:
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        workflow_id="workflow-1",
        task_id="task-1",
        owner="owner-1",
        lease_epoch=1,
        workspace_root="",
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        write_scope=("src",),
        write_scope_hash="sha256:" + "a" * 64,
        manifest_hash="sha256:" + "b" * 64,
        cas_root_hash="sha256:" + "c" * 64,
        entry_count=1,
        kind="checkpoint",
    )
    restored = RestoreResult("checkpoint-1", "rescue-1", "snapshot-2", ("src/a.py",))

    create_data = _data(project_checkpoint_create(checkpoint))
    status_data = _data(project_checkpoint_status(checkpoint))
    restore_data = _data(project_checkpoint_restore(restored))
    _assert_no_null(create_data)
    _assert_no_null(status_data)
    _assert_no_null(restore_data)

    assert set(create_data) == {
        "checkpoint_id",
        "workflow_id",
        "task_id",
        "owner",
        "lease_epoch",
        "workspace_id",
        "snapshot_id",
        "write_scope",
        "write_scope_hash",
        "manifest_hash",
        "cas_root_hash",
        "entry_count",
        "kind",
    }
    assert set(restore_data) == {
        "checkpoint_id",
        "rescue_checkpoint_id",
        "restored_snapshot_id",
        "changed_paths",
    }


def test_atlas_projectors_use_locked_data_keys_and_omit_raw_payloads() -> None:
    query = project_atlas_query(
        GraphQueryResult(
            nodes=(AtlasNode("node-1", NodeKind.RECIPE, {"private": "payload"}),)
        )
    )
    prepare = project_atlas_prepare(
        PreparationResult(
            AtlasStatus.READY,
            packet=ImplementationPacket(
                "packet-1", "trace-1", "workspace-1", "snapshot-1", "recipe-1"
            ),
        )
    )
    render = project_atlas_render(RenderResult(AtlasStatus.READY, "packet-1"))
    accept = project_atlas_accept(
        AcceptanceProjection(
            "acceptance-1",
            "task-1",
            "snapshot-2",
            AtlasStatus.READY,
            "episode-1",
            "recipe-1",
        )
    )
    _assert_no_null(query)
    _assert_no_null(prepare)
    _assert_no_null(render)
    _assert_no_null(accept)

    assert set(_data(query)) == {"nodes", "edges", "truncated"}
    assert set(_data(prepare)) == {
        "status",
        "packet",
        "candidate_recipe_ids",
        "reasons",
    }
    assert set(_data(render)) == {
        "status",
        "packet_id",
        "patch_candidate",
        "patch_hash",
        "bindings_hash",
        "test_specs",
        "reasons",
    }
    assert set(_data(accept)) == {
        "acceptance_id",
        "code_task_id",
        "output_snapshot_id",
        "atlas_ingest_state",
        "episode_id",
        "recipe_id",
        "reasons",
    }


def test_atlas_exact_keys_fail_closed_when_required_values_are_missing() -> None:
    with pytest.raises(ResultContractError):
        project_atlas_prepare(PreparationResult(AtlasStatus.READY))
    with pytest.raises(ResultContractError):
        project_atlas_accept(
            AcceptanceProjection(
                "acceptance-1",
                "task-1",
                "snapshot-2",
                AtlasStatus.READY,
                "episode-1",
            )
        )


@pytest.mark.parametrize(
    "value",
    [
        {"workspace_root": "C:/host/source"},
        {"absolute": "/etc/passwd"},
        {"bearer": "Bearer eyJsecret"},
        {"secret": "API_KEY=hidden"},
        {"proof": {"full": "private"}},
        {"receipt": {"stdout": "private"}},
        {"text": "source body"},
    ],
)
def test_public_envelope_rejects_leakage_vectors(value: object) -> None:
    with pytest.raises(ResultContractError):
        envelope_success(value)


def test_public_envelope_rejects_oversize_data() -> None:
    with pytest.raises(ResultContractError):
        envelope_success({"value": "x" * 1024}, byte_budget=64)


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (
            RuntimeContractError("DATA_ROOT_INVALID", "internal root"),
            "DATA_ROOT_INVALID",
        ),
        (IndexError("INDEX_STALE", "internal index"), "INDEX_STALE"),
        (RelayPlanError("invalid_request"), "RELAY_PLAN_INVALID"),
        (ValueError("invalid"), "INVALID_REQUEST"),
        (sqlite3.OperationalError("locked"), "STORAGE_ERROR"),
        (RuntimeError("unexpected"), "INTERNAL_ERROR"),
    ],
)
def test_domain_error_precedence_and_safe_messages(
    exception: Exception, code: str
) -> None:
    result = result_from_exception(exception)
    assert result == {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "error": {"code": code, "message": "request rejected"},
    }


def test_base_exception_is_never_converted() -> None:
    with pytest.raises(KeyboardInterrupt):
        result_from_exception(KeyboardInterrupt())


def test_relay_projectors_do_not_return_capabilities_or_host_paths() -> None:
    status = {
        "schema": "2718lab-devkit/relay-status-v1",
        "workflow_id": "workflow-1",
        "run": {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "plan_hash": "sha256:" + "a" * 64,
            "workspace_id": "workspace-1",
            "input_snapshot_id": "sha256:" + "b" * 64,
            "base_commit": "a" * 40,
            "capacity": 1,
            "schedule_version": 1,
        },
        "schedule_version": 1,
        "tasks": [
            {
                "task_id": "task-1",
                "kind": "verification",
                "priority": 1,
                "state": "completed",
                "task_version": 1,
                "scope_owner": None,
                "candidate_id": None,
                "last_lease_epoch": 1,
            }
        ],
        "leases": [
            {
                "lease_id": "lease-1",
                "task_id": "task-1",
                "action_id": "action-1",
                "epoch": 1,
                "task_version": 1,
                "lease_kind": "worker",
                "endpoint": None,
                "state": "released",
                "released_at": None,
            }
        ],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "task_id": "task-1",
                "originating_epoch": 1,
                "branch": "branch-1",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "diff_hash": "sha256:" + "c" * 64,
                "evidence_hashes": [],
                "pr_reference": None,
                "status": "prepared",
                "review_digest": None,
                "integration_commit": None,
            }
        ],
        "outstanding_action_ids": [],
        "refill_directives": [],
        "queues": {
            "prepared_prewarms": [],
            "ready": [],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        },
    }
    projected = project_relay_status(status)
    _assert_no_null(projected)
    assert "capability" not in str(projected).casefold()
    assert "host_actions" not in _data(projected)
    assert set(_data(projected)) == {
        "workflow_id",
        "run",
        "schedule_version",
        "tasks",
        "leases",
        "candidates",
        "outstanding_action_ids",
        "refill_directives",
        "queues",
    }


def test_relay_compile_and_start_projectors_are_bounded() -> None:
    route = {
        "route_class": "terra_high",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
    }
    plan = {
        "schema": "2718lab-devkit/relay-plan-v1",
        "workflow_id": "workflow-1",
        "workspace_binding": {
            "workspace_id": "workspace-1",
            "input_snapshot_id": "sha256:" + "a" * 64,
            "atlas_packet_ids": [],
        },
        "base_commit": "b" * 40,
        "capacity": 1,
        "runtime_policy_id": "2718lab-devkit/relay-runtime-policy-v1",
        "tasks": [
            {
                "task_id": "task-1",
                "kind": "verification",
                "title": "Verify",
                "objective": "Run verification",
                "priority": 1,
                "dependencies": [],
                "write_scope": [],
                "route": route,
                "constraints": [],
                "acceptance_criteria": [],
                "atlas_packet_ids": [],
                "required_evidence": [],
                "prewarm_for_task_id": None,
                "retry_policy": {"max_attempts": 1, "retryable_codes": []},
            }
        ],
        "dependencies": [],
        "conflicts": [],
        "queues": {
            "prepared_prewarms": [],
            "ready": ["task-1"],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        },
        "plan_hash": "sha256:" + "c" * 64,
    }
    start = {
        "schema": "2718lab-devkit/relay-start-result-v1",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "schedule_version": 1,
        "host_actions": [
            {
                "action_id": "action-1",
                "kind": "codex.spawn_agent",
                "workflow_id": "workflow-1",
                "task_id": "task-1",
                "lease": {"lease_id": "lease-1", "epoch": 1, "task_version": 1},
                "route": route,
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
            }
        ],
    }

    compile_result = project_relay_compile(plan)
    start_result = project_relay_start(start)
    _assert_no_null(compile_result)
    _assert_no_null(start_result)

    assert set(_data(compile_result)) == set(plan)
    assert set(_data(start_result)) == {
        "workflow_id",
        "run_id",
        "schedule_version",
        "actions",
    }
    compile_tasks = cast(list[object], _data(compile_result)["tasks"])
    assert "prewarm_for_task_id" not in cast(dict[str, object], compile_tasks[0])


def test_relay_mutation_projectors_have_closed_result_keys() -> None:
    mutation = {
        "workflow_id": "workflow-1",
        "schedule_version": 2,
        "task": {
            "task_id": "task-1",
            "kind": "verification",
            "priority": 1,
            "state": "completed",
            "task_version": 2,
            "scope_owner": None,
            "candidate_id": None,
            "last_lease_epoch": 1,
        },
    }
    for projector in (project_relay_handoff, project_relay_integrate):
        result = projector(mutation)
        _assert_no_null(result)
        task_data = cast(dict[str, object], _data(result)["task"])
        assert "scope_owner" not in task_data
        assert "candidate_id" not in task_data
        assert set(_data(result)) <= {
            "workflow_id",
            "run_id",
            "schedule_version",
            "task",
            "candidate",
            "host_actions",
        }


def test_register_projector_returns_only_opaque_workspace_id() -> None:
    result = project_index_register("workspace-1")
    _assert_no_null(result)
    assert result == {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "data": {"workspace_id": "workspace-1"},
    }
