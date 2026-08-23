from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import inspect
import json
import sys
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

MCP_TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS_ROOT))

MODULE_NAME = "devkit_runtime.fastlane_host_intent"


@dataclass(frozen=True, slots=True)
class _ExpiredPublicExpectation:
    expires_at_epoch: int = 0


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
    predecessor = _with_binding(
        {
            "schema": "2718lab-devkit/fastlane-external-lease-predecessor-v2",
            "projection_hash": projection_hash,
            "source_plan_hash": source_plan_hash,
            "workflow_hash": workflow_hash,
            "task_id": "FASTLANE-HOST-INTENT-CONTRACT",
            "role": "terra-high-writer",
            "assignment_id": assignment_id,
            "assignment_token": assignment_token,
            "routing_result_hash": route["routing_result_hash"],
            "ledger_epoch": 11,
            "active_lease_set_hash": _hash("active-lease-set"),
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
            "ledger_epoch": predecessor["ledger_epoch"],
            "active_lease_set_hash": predecessor["active_lease_set_hash"],
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
        "schema": "2718lab-devkit/fastlane-host-execution-intent-v2",
        "projection_hash": projection_hash,
        "source_plan_hash": source_plan_hash,
        "workflow_hash": workflow_hash,
        "assignment": assignment,
        "route": route,
        "capability_facts": [capability_fact],
        "packets": packets,
        "source": source,
        "create": create,
        "lease": lease,
    }
    _refresh_root(candidate)
    return candidate


def _module() -> Any:
    return importlib.import_module(MODULE_NAME)


def _require_non_authorizing_api(module: Any) -> None:
    assert (
        "expectation"
        in inspect.signature(module.validate_host_execution_intent).parameters
    )
    assert hasattr(module, "ParsedHostExecutionIntent")
    assert hasattr(module, "ParsedHostCapabilityFact")
    assert hasattr(module, "HostExecutionExpectationProjection")
    assert hasattr(module, "HostCapabilityExpectation")
    assert hasattr(module, "parse_host_execution_intent")
    assert hasattr(module, "matches_host_execution_expectation")
    assert not hasattr(module, "HostExecutionIntent")


def _expectation(module: Any, candidate: dict[str, Any]) -> Any:
    _require_non_authorizing_api(module)
    assignment = candidate["assignment"]
    predecessor = assignment["predecessor"]
    route = candidate["route"]
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
    return module.HostExecutionExpectationProjection(
        candidate_intent_hash=candidate["intent_hash"],
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
        ledger_epoch=predecessor["ledger_epoch"],
        active_lease_set_hash=predecessor["active_lease_set_hash"],
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


def _parse(candidate: object) -> Any:
    module = _module()
    _require_non_authorizing_api(module)
    return module.parse_host_execution_intent(candidate)


def _matches(parsed: object, expectation: object) -> bool:
    module = _module()
    _require_non_authorizing_api(module)
    return module.matches_host_execution_expectation(parsed, expectation)


def _assert_parse_rejected(candidate: object) -> None:
    module = _module()
    assert _parse(candidate) is module.NO_SAFE_WORK


def _assert_hard_gate(candidate: object, expectation: object | None = None) -> None:
    module = _module()
    assert (
        module.validate_host_execution_intent(candidate, expectation=expectation)
        is module.NO_SAFE_WORK
    )


def _refresh_predecessor_and_assignment(candidate: dict[str, Any]) -> str:
    assignment = candidate["assignment"]
    assignment["predecessor"] = _with_binding(
        assignment["predecessor"],
        "predecessor_hash",
    )
    assignment = _with_binding(assignment, "assignment_binding_hash")
    candidate["assignment"] = assignment
    return assignment["predecessor"]["predecessor_hash"]


def _topology_group(
    scheduler_id: str,
    worktree_identity: str,
    writer_task_ids: list[str],
    prewarm_task_ids: list[str],
) -> dict[str, Any]:
    return _with_binding(
        {
            "scheduler_id": scheduler_id,
            "coordinator_lease_id": f"lease-{scheduler_id}",
            "worktree_identity": worktree_identity,
            "writer_task_ids": writer_task_ids,
            "prewarm_task_ids": prewarm_task_ids,
        },
        "group_binding_hash",
    )


def _topology() -> dict[str, Any]:
    candidate = {
        "schema": "2718lab-devkit/scheduler-topology-v1",
        "plan_hash": _hash("topology-plan"),
        "groups": [
            _topology_group("scheduler-a", "wt-a", ["writer-a"], ["prewarm-a"]),
            _topology_group("scheduler-b", "wt-b", ["writer-b"], ["prewarm-b"]),
        ],
    }
    return _with_binding(candidate, "topology_hash")


def _host_topology() -> dict[str, Any]:
    relay_topology = _topology()
    groups = []
    for relay_group in relay_topology["groups"]:
        groups.append(
            _with_binding(
                {
                    "scheduler_id": relay_group["scheduler_id"],
                    "coordinator_lease_id": relay_group["coordinator_lease_id"],
                    "worktree_identity": relay_group["worktree_identity"],
                    "writer_task_ids": relay_group["writer_task_ids"],
                    "prewarm_task_ids": relay_group["prewarm_task_ids"],
                    "relay_group_binding_hash": relay_group["group_binding_hash"],
                    "attested_capacity": 3,
                    "attestation_hash": _hash(
                        f"host-capacity-{relay_group['scheduler_id']}"
                    ),
                },
                "group_binding_hash",
            )
        )
    return _with_binding(
        {
            "schema": "2718lab-devkit/host-scheduler-topology-v1",
            "relay_plan_hash": relay_topology["plan_hash"],
            "relay_topology_hash": relay_topology["topology_hash"],
            "groups": groups,
        },
        "projection_hash",
    )


def test_host_topology_projection_keeps_only_opaque_auditable_group_bindings() -> None:
    module = _module()

    parsed = module.parse_host_scheduler_topology_projection(_host_topology())

    assert parsed.schema == "2718lab-devkit/host-scheduler-topology-v1"
    assert parsed.groups[0].scheduler_id == "scheduler-a"
    assert parsed.groups[0].coordinator_lease_id == "lease-scheduler-a"
    assert parsed.groups[0].worktree_identity == "wt-a"
    assert parsed.groups[0].writer_task_ids == ("writer-a",)
    assert parsed.groups[0].prewarm_task_ids == ("prewarm-a",)
    assert "path" not in repr(parsed).lower()
    assert "quota" not in repr(parsed).lower()
    assert "model" not in repr(parsed).lower()


def test_host_topology_projection_requires_its_exact_schema_and_rejects_relay_v1() -> None:
    module = _module()
    relay_topology = _topology()
    host_group = _with_binding(
        {
            "scheduler_id": "scheduler-a",
            "coordinator_lease_id": "lease-scheduler-a",
            "worktree_identity": "wt-a",
            "writer_task_ids": ["writer-a"],
            "prewarm_task_ids": ["prewarm-a"],
            "relay_group_binding_hash": relay_topology["groups"][0]["group_binding_hash"],
            "attested_capacity": 1,
            "attestation_hash": _hash("host-capacity-a"),
        },
        "group_binding_hash",
    )
    host_projection = _with_binding(
        {
            "schema": "2718lab-devkit/host-scheduler-topology-v1",
            "relay_plan_hash": relay_topology["plan_hash"],
            "relay_topology_hash": relay_topology["topology_hash"],
            "groups": [host_group],
        },
        "projection_hash",
    )

    parsed = module.parse_host_scheduler_topology_projection(host_projection)

    assert isinstance(parsed, module.ParsedHostSchedulerTopologyProjection)
    assert parsed.schema == "2718lab-devkit/host-scheduler-topology-v1"
    assert parsed.relay_plan_hash == relay_topology["plan_hash"]
    assert parsed.groups[0].relay_group_binding_hash == relay_topology["groups"][0]["group_binding_hash"]
    assert module.parse_host_scheduler_topology_projection(relay_topology) is module.NO_SAFE_WORK


@pytest.mark.parametrize(
    "mutate",
    [
        lambda topology: topology["groups"][0].__setitem__(
            "writer_task_ids", ["writer-a", "writer-a-2", "writer-a-3", "writer-a-4"]
        ),
        lambda topology: topology["groups"].append(
            _topology_group("scheduler-c", "wt-c", ["writer-a"], [])
        ),
        lambda topology: topology["groups"][0].__setitem__(
            "prewarm_task_ids", ["writer-a"]
        ),
        lambda topology: topology["groups"][0].__setitem__(
            "worktree_identity", "G:/raw/worktree/path"
        ),
    ],
)
def test_topology_v1_rejects_writer_conflicts_and_nonopaque_worktree_data(
    mutate: Any,
) -> None:
    module = _module()
    topology = _host_topology()
    mutate(topology)
    topology["groups"] = [
        _with_binding(group, "group_binding_hash") for group in topology["groups"]
    ]
    topology = _with_binding(topology, "projection_hash")

    assert (
        module.parse_host_scheduler_topology_projection(topology)
        is module.NO_SAFE_WORK
    )


def test_public_validation_is_hard_gated_for_every_public_expectation() -> None:
    module = _module()
    candidate = _intent()
    expectation = _expectation(module, candidate)
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert _matches(parsed, expectation)
    _assert_hard_gate(candidate)
    _assert_hard_gate(candidate, expectation)
    _assert_hard_gate(candidate, _ExpiredPublicExpectation())
    _assert_hard_gate(candidate, {"public": "expectation"})
    _assert_hard_gate(candidate, object())


def test_parse_and_fieldwise_match_are_immutable_but_non_authorizing() -> None:
    module = _module()
    candidate = _intent()
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert parsed.model == "gpt-5.6-terra"
    assert parsed.reasoning_effort == "high"
    assert parsed.assignment_id == _hash("assignment-id")
    assert parsed.source_commit == "f4d5b6cadad052cf84d3055e438816f93e919ccb"
    assert parsed.parsed_capability_facts[0].state == "attested"
    assert _matches(parsed, _expectation(module, candidate))
    _assert_hard_gate(candidate, _expectation(module, candidate))
    with pytest.raises(FrozenInstanceError):
        parsed.model = "gpt-5.6-luna"


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
            "removed_quota_object",
            lambda intent: intent.__setitem__("quota", {"used": 1}),
        ),
        (
            "ledger_epoch_mismatch",
            lambda intent: intent["assignment"]["predecessor"].__setitem__(
                "ledger_epoch", 12
            ),
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
def test_parser_rejects_malformed_or_tampered_evidence(
    label: str,
    mutate: Any,
) -> None:
    candidate = _intent()
    mutate(candidate)
    _refresh_root(candidate)

    _assert_parse_rejected(candidate)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("repository", "github.com/attacker/other-repo"),
        ("ref", "refs/heads/attacker"),
        ("commit", "0" * 40),
        ("tree", "1" * 40),
    ],
)
def test_parser_accepts_but_matcher_differentiates_a_fully_rebound_source_mutation(
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
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert not _matches(parsed, _expectation(module, baseline))
    _assert_hard_gate(candidate, _expectation(module, baseline))


def test_parser_accepts_but_matcher_differentiates_a_rebound_assignment_token() -> None:
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
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert not _matches(parsed, _expectation(module, baseline))
    _assert_hard_gate(candidate, _expectation(module, baseline))


def test_parser_accepts_but_matcher_differentiates_a_rebound_lease_ledger() -> None:
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    candidate["assignment"]["predecessor"]["ledger_epoch"] = 12
    predecessor_hash = _refresh_predecessor_and_assignment(candidate)
    candidate["lease"]["ledger_epoch"] = 12
    candidate["lease"]["predecessor_hash"] = predecessor_hash
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    _refresh_root(candidate)
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert not _matches(parsed, _expectation(module, baseline))
    _assert_hard_gate(candidate, _expectation(module, baseline))


def test_parser_accepts_but_matcher_differentiates_gpt_attacker_route_and_fact() -> (
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
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert not _matches(parsed, _expectation(module, baseline))
    _assert_hard_gate(candidate, _expectation(module, baseline))


def test_parser_accepts_but_matcher_differentiates_a_rebound_lease_fence() -> None:
    module = _module()
    baseline = _intent()
    candidate = copy.deepcopy(baseline)
    fencing_token = _hash("attacker-fencing-token")
    candidate["lease"]["fencing_token"] = fencing_token
    candidate["create"]["lease_fencing_token"] = fencing_token
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    candidate["create"] = _with_binding(candidate["create"], "create_binding_hash")
    _refresh_root(candidate)
    parsed = _parse(candidate)

    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert not _matches(parsed, _expectation(module, baseline))
    _assert_hard_gate(candidate, _expectation(module, baseline))


def test_parser_rejects_zero_lease_epoch() -> None:
    candidate = _intent()
    candidate["assignment"]["predecessor"]["lease_epoch"] = 0
    predecessor_hash = _refresh_predecessor_and_assignment(candidate)
    candidate["lease"]["epoch"] = 0
    candidate["lease"]["predecessor_hash"] = predecessor_hash
    candidate["lease"] = _with_binding(candidate["lease"], "lease_binding_hash")
    _refresh_root(candidate)

    _assert_parse_rejected(candidate)


@pytest.mark.parametrize(
    ("model", "reasoning_effort"),
    [
        ("gpt-5.3-codex-spark", "high"),
        ("gpt-5.6-terra", "ultra"),
    ],
)
def test_parser_rejects_spark_or_ultra(
    model: str,
    reasoning_effort: str,
) -> None:
    _assert_parse_rejected(_intent(model=model, reasoning_effort=reasoning_effort))


def test_parser_rejects_non_external_session() -> None:
    _assert_parse_rejected(_intent(session_scope="local"))


def test_luna_max_parses_and_matches_but_still_cannot_be_admitted() -> None:
    module = _module()
    rejected = _intent(
        model="gpt-5.6-luna",
        reasoning_effort="max",
        capability_state="declared",
    )
    _assert_parse_rejected(rejected)

    candidate = _intent(model="gpt-5.6-luna", reasoning_effort="max")
    parsed = _parse(candidate)
    assert isinstance(parsed, module.ParsedHostExecutionIntent)
    assert _matches(parsed, _expectation(module, candidate))
    _assert_hard_gate(candidate, _expectation(module, candidate))


def test_module_has_no_host_authorization_or_side_effect_surface() -> None:
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

    assert not hasattr(module, "HostExecutionIntent")
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
