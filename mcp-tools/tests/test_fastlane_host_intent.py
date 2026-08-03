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
    unbound = copy.deepcopy(value)
    unbound.pop(binding_field, None)
    unbound[binding_field] = _canonical_hash(unbound)
    return unbound


def _refresh_root(candidate: dict[str, Any]) -> None:
    refreshed = _with_binding(candidate, "intent_hash")
    candidate.clear()
    candidate.update(refreshed)


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


def _require_expectation_api(module: Any) -> None:
    assert (
        "expectation"
        in inspect.signature(module.validate_host_execution_intent).parameters
    )
    assert hasattr(module, "HostExecutionExpectation")
    assert hasattr(module, "HostCapabilityExpectation")


def _expectation(
    module: Any,
    candidate: dict[str, Any],
    *,
    trust_state: str = "host-private-verified-v1",
    verified_at_epoch: int = 100,
    expires_at_epoch: int = 200,
) -> Any:
    _require_expectation_api(module)
    assignment = candidate["assignment"]
    predecessor = assignment["predecessor"]
    route = candidate["route"]
    quota = candidate["quota"]
    packets = candidate["packets"]
    source = candidate["source"]
    create = candidate["create"]
    lease = candidate["lease"]
    capability_facts = tuple(
        module.HostCapabilityExpectation(
            model=fact["model"],
            reasoning_effort=fact["reasoning_effort"],
            state=fact["state"],
            attestation_hash=fact["attestation_hash"],
        )
        for fact in candidate["capability_facts"]
    )
    return module.HostExecutionExpectation(
        verifier_key_id="desktop-host-private-key",
        trust_state=trust_state,
        receipt_hash=_hash("detached-host-receipt"),
        verified_at_epoch=verified_at_epoch,
        expires_at_epoch=expires_at_epoch,
        expected_intent_hash=candidate["intent_hash"],
        projection_hash=candidate["projection_hash"],
        source_plan_hash=candidate["source_plan_hash"],
        workflow_hash=candidate["workflow_hash"],
        assignment_id=assignment["assignment_id"],
        assignment_token=assignment["assignment_token"],
        predecessor_hash=predecessor["predecessor_hash"],
        task_id=predecessor["task_id"],
        role=predecessor["role"],
        model=route["model"],
        reasoning_effort=route["reasoning_effort"],
        routing_context_hash=route["routing_context_hash"],
        routing_result_hash=route["routing_result_hash"],
        session_scope=route["session_scope"],
        inherit_current_session_model=route["inherit_current_session_model"],
        require_explicit_route=route["require_explicit_route"],
        capability_facts=capability_facts,
        quota_snapshot_hash=quota["snapshot_hash"],
        quota_decision_hash=quota["decision_hash"],
        quota_evidence_hash=quota["evidence_hash"],
        ledger_epoch=quota["ledger_epoch"],
        active_lease_set_hash=quota["active_lease_set_hash"],
        task_packet_hash=packets["task_packet_hash"],
        input_packet_hash=packets["input_packet_hash"],
        index_packet_hash=packets["index_packet_hash"],
        project_id=source["project_id"],
        registered_project_id=source["registered_project_id"],
        repository=source["repository"],
        common_dir=source["common_dir"],
        source_ref=source["ref"],
        source_commit=source["commit"],
        source_tree=source["tree"],
        create_request_hash=create["request_hash"],
        operation_id=create["operation_id"],
        lease_owner=lease["owner"],
        lease_epoch=lease["epoch"],
        lease_fencing_token=lease["fencing_token"],
    )


def _validate(candidate: object, expectation: object) -> Any:
    module = _module()
    _require_expectation_api(module)
    return module.validate_host_execution_intent(candidate, expectation=expectation)


def _assert_rejected(candidate: object, expectation: object) -> None:
    module = _module()
    assert _validate(candidate, expectation) is module.NO_SAFE_WORK


def _refresh_predecessor_and_assignment(candidate: dict[str, Any]) -> str:
    assignment = candidate["assignment"]
    assignment["predecessor"] = _with_binding(
        assignment["predecessor"],
        "predecessor_hash",
    )
    assignment = _with_binding(assignment, "assignment_binding_hash")
    candidate["assignment"] = assignment
    return assignment["predecessor"]["predecessor_hash"]


def test_rejects_a_candidate_without_a_host_private_expectation() -> None:
    module = _module()

    assert module.validate_host_execution_intent(_intent()) is module.NO_SAFE_WORK


def test_accepts_a_host_bound_explicit_intent_as_an_immutable_typed_value() -> None:
    module = _module()
    candidate = _intent()

    accepted = _validate(candidate, _expectation(module, candidate))

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
            lambda intent: intent["source"].__setitem__(
                "repository", "github.com/other/repo"
            ),
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
            lambda intent: intent["assignment"].__setitem__(
                "assignment_token", _hash("other-token")
            ),
        ),
        (
            "quota_evidence_mismatch",
            lambda intent: intent["quota"].__setitem__(
                "evidence_hash", _hash("other-evidence")
            ),
        ),
        (
            "ledger_epoch_mismatch",
            lambda intent: intent["quota"].__setitem__("ledger_epoch", 12),
        ),
        (
            "lease_fencing_mismatch",
            lambda intent: intent["lease"].__setitem__(
                "fencing_token", _hash("other-fencing")
            ),
        ),
        (
            "unregistered_project_identity",
            lambda intent: intent["source"].__setitem__(
                "registered_project_id", "unknown-project"
            ),
        ),
        (
            "inherited_model",
            lambda intent: intent["route"].__setitem__(
                "inherit_current_session_model", True
            ),
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
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    mutate(candidate)
    _refresh_root(candidate)

    _assert_rejected(candidate, _expectation(module, baseline))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("repository", "github.com/attacker/other-repo"),
        ("ref", "refs/heads/attacker"),
        ("commit", "0" * 40),
        ("tree", "1" * 40),
    ],
)
def test_rejects_a_fully_rebound_source_mutation_against_host_expectation(
    field: str,
    replacement: str,
) -> None:
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    candidate["source"][field] = replacement
    candidate["create"][field] = replacement
    candidate["source"] = _with_binding(candidate["source"], "source_binding_hash")
    candidate["create"] = _with_binding(candidate["create"], "create_binding_hash")
    _refresh_root(candidate)

    assert candidate["intent_hash"] != baseline["intent_hash"]
    _assert_rejected(candidate, _expectation(module, baseline))


def test_rejects_a_fully_rebound_assignment_token_mutation_against_host_expectation() -> (
    None
):
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    assignment_token = _hash("attacker-assignment-token")
    candidate["assignment"]["assignment_token"] = assignment_token
    candidate["assignment"]["predecessor"]["assignment_token"] = assignment_token
    candidate["packets"]["assignment_token"] = assignment_token
    candidate["lease"]["assignment_token"] = assignment_token
    candidate["create"]["assignment_token"] = assignment_token
    predecessor_hash = _refresh_predecessor_and_assignment(candidate)
    candidate["lease"]["predecessor_hash"] = predecessor_hash
    candidate["packets"] = _with_binding(candidate["packets"], "packet_binding_hash")
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    candidate["create"] = _with_binding(candidate["create"], "create_binding_hash")
    _refresh_root(candidate)

    assert candidate["intent_hash"] != baseline["intent_hash"]
    _assert_rejected(candidate, _expectation(module, baseline))


def test_rejects_a_fully_rebound_quota_ledger_mutation_against_host_expectation() -> (
    None
):
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    candidate["quota"]["ledger_epoch"] = 12
    candidate["quota"] = _with_binding(candidate["quota"], "quota_binding_hash")
    candidate["assignment"]["predecessor"]["ledger_epoch"] = 12
    predecessor_hash = _refresh_predecessor_and_assignment(candidate)
    candidate["lease"]["ledger_epoch"] = 12
    candidate["lease"]["predecessor_hash"] = predecessor_hash
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    _refresh_root(candidate)

    assert candidate["intent_hash"] != baseline["intent_hash"]
    _assert_rejected(candidate, _expectation(module, baseline))


def test_rejects_a_fully_rebound_route_and_capability_attack_against_host_expectation() -> (
    None
):
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    candidate["route"]["model"] = "gpt-attacker"
    candidate["capability_facts"][0]["model"] = "gpt-attacker"
    candidate["route"] = _with_binding(candidate["route"], "route_binding_hash")
    candidate["capability_facts"][0] = _with_binding(
        candidate["capability_facts"][0],
        "capability_binding_hash",
    )
    _refresh_root(candidate)

    assert candidate["intent_hash"] != baseline["intent_hash"]
    _assert_rejected(candidate, _expectation(module, baseline))


def test_rejects_a_fully_rebound_lease_fence_mutation_against_host_expectation() -> (
    None
):
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    fencing_token = _hash("attacker-fencing-token")
    candidate["lease"]["fencing_token"] = fencing_token
    candidate["create"]["lease_fencing_token"] = fencing_token
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    candidate["create"] = _with_binding(candidate["create"], "create_binding_hash")
    _refresh_root(candidate)

    assert candidate["intent_hash"] != baseline["intent_hash"]
    _assert_rejected(candidate, _expectation(module, baseline))


def test_rejects_zero_lease_epoch_even_when_candidate_and_expectation_match() -> None:
    module = _module()
    candidate = _intent()
    candidate["assignment"]["predecessor"]["lease_epoch"] = 0
    predecessor_hash = _refresh_predecessor_and_assignment(candidate)
    candidate["lease"]["epoch"] = 0
    candidate["lease"]["predecessor_hash"] = predecessor_hash
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    _refresh_root(candidate)

    _assert_rejected(candidate, _expectation(module, candidate))


@pytest.mark.parametrize(
    ("trust_state", "verified_at_epoch", "expires_at_epoch"),
    [
        ("untrusted", 100, 200),
        ("host-private-verified-v1", 100, 100),
        ("host-private-verified-v1", 0, 200),
    ],
)
def test_rejects_untrusted_or_expired_host_expectation(
    trust_state: str,
    verified_at_epoch: int,
    expires_at_epoch: int,
) -> None:
    module = _module()
    candidate = _intent()
    expectation = _expectation(
        module,
        candidate,
        trust_state=trust_state,
        verified_at_epoch=verified_at_epoch,
        expires_at_epoch=expires_at_epoch,
    )

    _assert_rejected(candidate, expectation)


def test_rejects_a_non_expectation_context() -> None:
    _assert_rejected(_intent(), {"trust_state": "host-private-verified-v1"})


@pytest.mark.parametrize(
    ("model", "reasoning_effort"),
    [
        ("gpt-5.3-codex-spark", "high"),
        ("gpt-5.6-terra", "ultra"),
    ],
)
def test_rejects_spark_or_ultra_even_when_host_expectation_matches(
    model: str,
    reasoning_effort: str,
) -> None:
    module = _module()
    candidate = _intent(model=model, reasoning_effort=reasoning_effort)

    _assert_rejected(candidate, _expectation(module, candidate))


def test_rejects_non_external_session_even_when_host_expectation_matches() -> None:
    module = _module()
    candidate = _intent(session_scope="local")

    _assert_rejected(candidate, _expectation(module, candidate))


def test_luna_max_requires_an_explicit_attested_host_capability_fact() -> None:
    module = _module()
    rejected = _intent(
        model="gpt-5.6-luna",
        reasoning_effort="max",
        capability_state="declared",
    )
    _assert_rejected(rejected, _expectation(module, rejected))

    candidate = _intent(model="gpt-5.6-luna", reasoning_effort="max")
    accepted = _validate(candidate, _expectation(module, candidate))
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
