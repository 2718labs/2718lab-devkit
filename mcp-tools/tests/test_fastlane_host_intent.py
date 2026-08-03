from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import inspect
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

MCP_TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS_ROOT))

MODULE_NAME = "devkit_runtime.fastlane_host_intent"


def _hash(seed: str) -> str:
    return _canonical_hash({"seed": seed})


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _with_binding(value: dict[str, Any], binding_field: str) -> dict[str, Any]:
    bound = copy.deepcopy(value)
    bound[binding_field] = _canonical_hash(bound)
    return bound


def _refresh_root(candidate: dict[str, Any]) -> None:
    without_intent_hash = {
        key: value for key, value in candidate.items() if key != "intent_hash"
    }
    candidate["intent_hash"] = _canonical_hash(without_intent_hash)


def _intent(
    *,
    model: str = "gpt-5.6-terra",
    reasoning_effort: str = "high",
    session_scope: str = "external",
    capability_state: str = "attested",
) -> dict[str, Any]:
    projection_hash = _hash("projection")
    source_plan_hash = _hash("source-plan")
    workflow_hash = _hash("workflow")
    assignment_id = _hash("assignment-id")
    assignment_token = _hash("assignment-token")

    source = _with_binding(
        {
            "project_id": "2718-devkit",
            "registered_project_id": "2718-devkit",
            "repository": "github.com/2718lab/2718lab-devkit",
            "common_dir": "G:/2718-devkit/.git",
            "ref": "refs/heads/codex/fastlane-wiring-integration",
            "commit": "f4d5b6cadad052cf84d3055e438816f93e919ccb",
            "tree": "6d7c6d3eef5b15d8c76ef4e654ef8d790c20dfd1",
        },
        "source_binding_hash",
    )
    route = _with_binding(
        {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "routing_context_hash": _hash("routing-context"),
            "routing_result_hash": _hash("routing-result"),
            "session_scope": session_scope,
            "inherit_current_session_model": False,
            "require_explicit_route": True,
        },
        "route_binding_hash",
    )
    quota = _with_binding(
        {
            "snapshot_hash": _hash("quota-snapshot"),
            "decision_hash": _hash("quota-decision"),
            "evidence_hash": _hash("quota-evidence"),
            "ledger_epoch": 11,
            "active_lease_set_hash": _hash("active-lease-set"),
        },
        "quota_binding_hash",
    )
    predecessor = _with_binding(
        {
            "schema": "2718lab-devkit/fastlane-external-lease-predecessor-v1",
            "projection_hash": projection_hash,
            "source_plan_hash": source_plan_hash,
            "workflow_hash": workflow_hash,
            "task_id": "FASTLANE-HOST-INTENT-CONTRACT",
            "role": "terra-high-writer",
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "routing_result_hash": route["routing_result_hash"],
            "quota_evidence_hash": quota["evidence_hash"],
            "quota_snapshot_hash": quota["snapshot_hash"],
            "quota_decision_hash": quota["decision_hash"],
            "ledger_epoch": quota["ledger_epoch"],
            "active_lease_set_hash": quota["active_lease_set_hash"],
            "lease_epoch": 17,
        },
        "predecessor_hash",
    )
    assignment = _with_binding(
        {
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "predecessor": predecessor,
        },
        "assignment_binding_hash",
    )
    capability_fact = _with_binding(
        {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "state": capability_state,
            "attestation_hash": _hash("host-capability-attestation"),
        },
        "capability_binding_hash",
    )
    packets = _with_binding(
        {
            "task_packet_hash": _hash("task-packet"),
            "input_packet_hash": _hash("input-packet"),
            "index_packet_hash": _hash("index-packet"),
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "routing_context_hash": route["routing_context_hash"],
        },
        "packet_binding_hash",
    )
    lease = _with_binding(
        {
            "owner": "root/fastlane-host-intent-contract",
            "epoch": 17,
            "fencing_token": _hash("lease-fencing-token"),
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "predecessor_hash": predecessor["predecessor_hash"],
            "ledger_epoch": quota["ledger_epoch"],
            "active_lease_set_hash": quota["active_lease_set_hash"],
        },
        "lease_binding_hash",
    )
    create = _with_binding(
        {
            "request_hash": _hash("create-request"),
            "operation_id": "fastlane-host-intent-contract-create",
            "project_id": source["project_id"],
            "repository": source["repository"],
            "common_dir": source["common_dir"],
            "ref": source["ref"],
            "commit": source["commit"],
            "tree": source["tree"],
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "lease_fencing_token": lease["fencing_token"],
        },
        "create_binding_hash",
    )
    candidate = {
        "schema": "2718lab-devkit/fastlane-host-execution-intent-v1",
        "projection_hash": projection_hash,
        "source_plan_hash": source_plan_hash,
        "workflow_hash": workflow_hash,
        "assignment": assignment,
        "route": route,
        "capability_facts": [capability_fact],
        "quota": quota,
        "packets": packets,
        "source": source,
        "create": create,
        "lease": lease,
    }
    _refresh_root(candidate)
    return candidate


def _module() -> Any:
    return importlib.import_module(MODULE_NAME)


def _assert_rejected(candidate: object) -> None:
    module = _module()
    assert module.validate_host_execution_intent(candidate) is module.NO_SAFE_WORK


def test_accepts_a_fully_bound_explicit_intent_as_an_immutable_typed_value() -> None:
    module = _module()

    accepted = module.validate_host_execution_intent(_intent())

    assert isinstance(accepted, module.HostExecutionIntent)
    assert accepted.model == "gpt-5.6-terra"
    assert accepted.reasoning_effort == "high"
    assert accepted.assignment_id == _hash("assignment-id")
    assert accepted.source_commit == "f4d5b6cadad052cf84d3055e438816f93e919ccb"
    assert accepted.host_capability_facts[0].state == "attested"
    with pytest.raises(FrozenInstanceError):
        accepted.model = "gpt-5.6-luna"


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing_required_field", lambda intent: intent.pop("source")),
        ("unexpected_field", lambda intent: intent.__setitem__("extra", "nope")),
        (
            "repository_mismatch",
            lambda intent: intent["source"].__setitem__("repository", "github.com/other/repo"),
        ),
        (
            "ref_mismatch",
            lambda intent: intent["source"].__setitem__("ref", "refs/heads/main"),
        ),
        (
            "commit_mismatch",
            lambda intent: intent["source"].__setitem__("commit", "0" * 40),
        ),
        (
            "tree_mismatch",
            lambda intent: intent["source"].__setitem__("tree", "1" * 40),
        ),
        (
            "assignment_token_mismatch",
            lambda intent: intent["assignment"].__setitem__("assignment_token", _hash("other-token")),
        ),
        (
            "quota_evidence_mismatch",
            lambda intent: intent["quota"].__setitem__("evidence_hash", _hash("other-evidence")),
        ),
        (
            "ledger_epoch_mismatch",
            lambda intent: intent["quota"].__setitem__("ledger_epoch", 12),
        ),
        (
            "lease_fencing_mismatch",
            lambda intent: intent["lease"].__setitem__("fencing_token", _hash("other-fencing")),
        ),
        (
            "unregistered_project_identity",
            lambda intent: intent["source"].__setitem__("registered_project_id", "unknown-project"),
        ),
        (
            "inherited_model",
            lambda intent: intent["route"].__setitem__("inherit_current_session_model", True),
        ),
        (
            "implicit_effort",
            lambda intent: intent["route"].pop("reasoning_effort"),
        ),
    ],
)
def test_rejects_malformed_or_tampered_evidence(
    label: str,
    mutate: Any,
) -> None:
    intent = _intent()
    mutate(intent)
    _refresh_root(intent)

    _assert_rejected(intent)


def test_rejects_a_rebound_source_that_no_longer_matches_create_request() -> None:
    intent = _intent()
    intent["source"]["repository"] = "github.com/other/repo"
    intent["source"] = _with_binding(intent["source"], "source_binding_hash")
    _refresh_root(intent)

    _assert_rejected(intent)


def test_rejects_a_rebound_predecessor_that_no_longer_matches_quota() -> None:
    intent = _intent()
    intent["assignment"]["predecessor"]["quota_decision_hash"] = _hash("other-decision")
    intent["assignment"]["predecessor"] = _with_binding(
        intent["assignment"]["predecessor"],
        "predecessor_hash",
    )
    intent["assignment"] = _with_binding(intent["assignment"], "assignment_binding_hash")
    _refresh_root(intent)

    _assert_rejected(intent)


def test_rejects_a_rebound_lease_that_no_longer_matches_predecessor() -> None:
    intent = _intent()
    intent["lease"]["epoch"] = 18
    intent["lease"] = _with_binding(intent["lease"], "lease_binding_hash")
    _refresh_root(intent)

    _assert_rejected(intent)


@pytest.mark.parametrize(
    ("model", "reasoning_effort"),
    [
        ("gpt-5.3-codex-spark", "high"),
        ("gpt-5.6-terra", "ultra"),
    ],
)
def test_rejects_external_spark_or_ultra(
    model: str,
    reasoning_effort: str,
) -> None:
    _assert_rejected(_intent(model=model, reasoning_effort=reasoning_effort))


def test_luna_max_requires_an_explicit_attested_capability_fact() -> None:
    _assert_rejected(
        _intent(
            model="gpt-5.6-luna",
            reasoning_effort="max",
            capability_state="declared",
        )
    )

    module = _module()
    accepted = module.validate_host_execution_intent(
        _intent(model="gpt-5.6-luna", reasoning_effort="max")
    )
    assert isinstance(accepted, module.HostExecutionIntent)
    assert accepted.model == "gpt-5.6-luna"
    assert accepted.reasoning_effort == "max"


def test_validator_has_no_thread_worktree_process_network_or_host_api_surface() -> None:
    module = _module()
    tree = ast.parse(inspect.getsource(module))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert imported_modules.isdisjoint(
        {
            "devkit_runtime.fastlane_host_adapter",
            "devkit_runtime.host_session",
            "devkit_runtime.host_bridge",
            "subprocess",
            "socket",
            "threading",
            "urllib",
            "requests",
            "httpx",
        }
    )
    assert called_names.isdisjoint(
        {
            "create_thread",
            "fork_thread",
            "spawn_agent",
            "workflow_claim",
            "workflow_complete",
        }
    )
    assert called_attributes.isdisjoint(
        {
            "Popen",
            "call",
            "check_call",
            "check_output",
            "connect",
            "create_thread",
            "fork_thread",
            "request",
            "run",
            "urlopen",
            "worktree",
        }
    )
