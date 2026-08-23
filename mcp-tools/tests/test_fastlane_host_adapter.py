from __future__ import annotations

import ast
import importlib
import inspect
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))


def _adapter() -> object:
    return importlib.import_module("devkit_runtime.fastlane_host_adapter")


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def test_adapter_fails_closed_when_verified_host_facts_are_missing() -> None:
    adapter = _adapter()

    assert (
        adapter.compile_fast_lane_with_host_facts(
            {}, reasoning_effort="ultra", verified_host_facts=None
        )
        == adapter.NO_SAFE_WORK
    )


def test_adapter_exposes_no_forgeable_verified_host_facts_marker() -> None:
    adapter = _adapter()

    assert not hasattr(adapter, "VerifiedHostFacts")
    assert not hasattr(adapter, "_CONSTRUCTION_CAPABILITY")


def test_unverified_capability_values_cannot_bootstrap_verified_host_facts() -> None:
    adapter = _adapter()
    from devkit_runtime.host_session import HostSession

    unavailable_session = HostSession(
        bridge=None,
        clock=lambda: 1.0,
    )

    assert (
        adapter.prepare_verified_host_facts(
            unavailable_session,
            capability_facts=(),
        )
        == adapter.NO_SAFE_WORK
    )


def test_adapter_contains_no_host_execution_calls() -> None:
    adapter = _adapter()
    tree = ast.parse(inspect.getsource(adapter))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_names.update(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )

    assert called_names.isdisjoint(
        {
            "spawn_agent",
            "create_thread",
            "fork_thread",
            "workflow_claim",
            "workflow_complete",
            "worktree",
            "checkout",
            "archive",
            "project_index_query",
        }
    )


@pytest.mark.parametrize(
    ("kind", "sender_role", "recipient_role"),
    (
        ("coordinator_to_worker", "coordinator", "worker"),
        ("worker_to_coordinator", "worker", "coordinator"),
        ("peer_handoff", "peer", "peer"),
    ),
)
def test_role_transfers_are_hash_only_bounded_projections(
    kind: str, sender_role: str, recipient_role: str
) -> None:
    adapter = _adapter()

    transfer = adapter.project_role_transfer(
        kind=kind,
        task_id="FAST-LANE-ADAPTER",
        role="execution",
        assignment_token=_hash("a"),
        context_hash=_hash("b"),
        summary_hash=_hash("c"),
        artifact_hashes=(_hash("d"),),
        digest_hashes=(_hash("e"),),
    )

    assert transfer["sender_role"] == sender_role
    assert transfer["recipient_role"] == recipient_role
    assert transfer["summary_hash"] == _hash("c")
    assert transfer["artifact_hashes"] == [_hash("d")]
    assert transfer["digest_hashes"] == [_hash("e")]
    assert "raw" not in transfer


def test_role_transfer_rejects_raw_or_path_like_content() -> None:
    adapter = _adapter()
    common = {
        "kind": "worker_to_coordinator",
        "task_id": "FAST-LANE-ADAPTER",
        "role": "execution",
        "assignment_token": _hash("a"),
        "context_hash": _hash("b"),
        "summary_hash": _hash("c"),
        "artifact_hashes": (_hash("d"),),
        "digest_hashes": (_hash("e"),),
    }

    assert (
        adapter.project_role_transfer(
            **{**common, "summary_hash": "raw prompt or secret"}
        )
        == adapter.NO_SAFE_WORK
    )
    assert (
        adapter.project_role_transfer(
            **{**common, "artifact_hashes": (r"D:\\private\\raw.log",)}
        )
        == adapter.NO_SAFE_WORK
    )


def test_role_transfer_fails_closed_when_hash_sequence_raises() -> None:
    adapter = _adapter()

    class ExplodingSequence(Sequence[str]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> str:
            del index
            raise RuntimeError("untrusted sequence iteration")

    assert (
        adapter.project_role_transfer(
            kind="worker_to_coordinator",
            task_id="FAST-LANE-ADAPTER",
            role="execution",
            assignment_token=_hash("a"),
            context_hash=_hash("b"),
            summary_hash=_hash("c"),
            artifact_hashes=ExplodingSequence(),
            digest_hashes=(_hash("e"),),
        )
        == adapter.NO_SAFE_WORK
    )
