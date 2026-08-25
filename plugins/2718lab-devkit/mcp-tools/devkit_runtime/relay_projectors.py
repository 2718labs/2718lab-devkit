"""Closed public projectors for the proof-aware Relay v2 boundary.

Relay service/store results are internal records.  This module accepts only
the known result shapes, drops host-private fields, and delegates the final
recursive no-null/size checks to the R2 result envelope.
"""

from __future__ import annotations

import math
import re
from typing import NoReturn, cast

from devkit_runtime.tool_result import ResultContractError, envelope_success

_START_SCHEMA = "2718lab-devkit/relay-start-result-v1"
_STATUS_SCHEMA = "2718lab-devkit/relay-status-v2"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_SECRET = re.compile(
    r"(?ix)(?:\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"private[_-]?key|password|secret)\s*[:=]\s*\S+|"
    r"-----begin\s+[a-z\s]*private\s+key-----)"
)

_MAX_STRING = 65_536
_MAX_ACTIONS = 64
_MAX_TASKS = 64
_MAX_LEASES = 128
_MAX_CANDIDATES = 64
_MAX_PROOFS = 64
_MAX_EVIDENCE = 128
_MAX_PRIVATE_DEPTH = 32
_MAX_PRIVATE_ITEMS = 512


def _fail(message: str) -> NoReturn:
    raise ResultContractError(message)


def _exact_dict(value: object, keys: set[str], detail: str) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail(detail)
    result = cast(dict[object, object], value)
    if any(type(key) is not str for key in result) or set(result) != keys:
        raise _fail(detail)
    return cast(dict[str, object], value)


def _safe_string(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise _fail("invalid Relay public string")
    if not allow_empty and not value:
        raise _fail("empty Relay public string")
    if len(value.encode("utf-8")) > _MAX_STRING:
        raise _fail("oversize Relay public string")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise _fail("control character in Relay public string")
    if _ABSOLUTE_PATH.match(value) or _BEARER.search(value) or _SECRET.search(value):
        raise _fail("sensitive Relay public string")
    return value


def _identifier(value: object) -> str:
    result = _safe_string(value)
    if _IDENTIFIER.fullmatch(result) is None:
        raise _fail("invalid Relay public identifier")
    return result


def _digest(value: object) -> str:
    result = _safe_string(value)
    if _DIGEST.fullmatch(result) is None:
        raise _fail("invalid Relay digest")
    return result


def _commit(value: object) -> str:
    result = _safe_string(value)
    if _COMMIT.fullmatch(result) is None:
        raise _fail("invalid Relay commit")
    return result


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _fail("invalid Relay count")
    return value


def _positive_int(value: object) -> int:
    result = _nonnegative_int(value)
    if result == 0:
        raise _fail("invalid Relay version")
    return result


def _bounded_list(value: object, maximum: int) -> list[object]:
    if type(value) not in (list, tuple):
        raise _fail("invalid Relay list")
    items = value if type(value) is list else cast(tuple[object, ...], value)
    if len(items) > maximum:
        raise _fail("oversize Relay list")
    return list(items)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _safe_string(value)


def _private_payload(
    value: object, *, depth: int = 0, active: set[int] | None = None
) -> None:
    """Validate dropped internal payloads without serializing or exposing them."""

    if depth > _MAX_PRIVATE_DEPTH:
        raise _fail("deep Relay private payload")
    if value is None or type(value) in (bool, int, str):
        if type(value) is str:
            _safe_string(value, allow_empty=True)
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _fail("invalid Relay private payload")
        return
    if type(value) not in (list, tuple, dict):
        raise _fail("unknown Relay private payload")
    seen = active if active is not None else set()
    marker = id(value)
    if marker in seen:
        raise _fail("cyclic Relay private payload")
    seen.add(marker)
    try:
        if type(value) is dict:
            if len(value) > _MAX_PRIVATE_ITEMS or any(
                type(key) is not str for key in cast(dict[object, object], value)
            ):
                raise _fail("invalid Relay private payload")
            for key, item in cast(dict[str, object], value).items():
                _safe_string(key, allow_empty=False)
                _private_payload(item, depth=depth + 1, active=seen)
        else:
            items = cast(list[object] | tuple[object, ...], value)
            if len(items) > _MAX_PRIVATE_ITEMS:
                raise _fail("oversize Relay private payload")
            for item in items:
                _private_payload(item, depth=depth + 1, active=seen)
    finally:
        seen.remove(marker)


def _route(value: object) -> dict[str, str]:
    route = _exact_dict(
        value, {"route_class", "model", "reasoning_effort"}, "invalid Relay route"
    )
    return {
        "route_class": _identifier(route["route_class"]),
        "model": _safe_string(route["model"]),
        "reasoning_effort": _identifier(route["reasoning_effort"]),
    }


def _lease_ref(value: object) -> dict[str, object]:
    lease = _exact_dict(
        value, {"lease_id", "epoch", "task_version"}, "invalid Relay lease"
    )
    return {
        "lease_id": _identifier(lease["lease_id"]),
        "epoch": _positive_int(lease["epoch"]),
        "task_version": _positive_int(lease["task_version"]),
    }


def _action(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail("invalid Relay start action")
    raw = cast(dict[object, object], value)
    base_keys = {
        "action_id",
        "kind",
        "workflow_id",
        "task_id",
        "lease",
        "route",
        "model",
        "reasoning_effort",
    }
    private_keys = {"task_contract", "worktree_bootstrap", "read_contract"}
    if (
        any(type(key) is not str for key in raw)
        or not set(raw).issubset(base_keys | private_keys)
        or not base_keys.issubset(raw)
    ):
        raise _fail("invalid Relay start action")
    action = cast(dict[str, object], value)
    for key in ("task_contract", "worktree_bootstrap", "read_contract"):
        if key in action:
            _private_payload(action[key])
    return {
        "action_id": _identifier(action["action_id"]),
        "kind": _identifier(action["kind"]),
        "workflow_id": _identifier(action["workflow_id"]),
        "task_id": _identifier(action["task_id"]),
        "lease": _lease_ref(action["lease"]),
        "route": _route(action["route"]),
        "model": _safe_string(action["model"]),
        "reasoning_effort": _identifier(action["reasoning_effort"]),
    }


def project_relay_start(result: object) -> dict[str, object]:
    value = _exact_dict(
        result,
        {"schema", "workflow_id", "run_id", "schedule_version", "host_actions"},
        "invalid Relay start result",
    )
    if value["schema"] != _START_SCHEMA:
        raise _fail("unsupported Relay start schema")
    return envelope_success(
        {
            "workflow_id": _identifier(value["workflow_id"]),
            "run_id": _identifier(value["run_id"]),
            "schedule_version": _nonnegative_int(value["schedule_version"]),
            "actions": [
                _action(item)
                for item in _bounded_list(value["host_actions"], _MAX_ACTIONS)
            ],
        }
    )


def _run(value: object) -> dict[str, object]:
    run = _exact_dict(
        value,
        {
            "run_id",
            "workflow_id",
            "plan_hash",
            "workspace_id",
            "input_snapshot_id",
            "base_commit",
            "integration_head",
            "integration_version",
            "capacity",
            "schedule_version",
        },
        "invalid Relay run",
    )
    capacity = _positive_int(run["capacity"])
    return {
        "run_id": _identifier(run["run_id"]),
        "workflow_id": _identifier(run["workflow_id"]),
        "plan_hash": _digest(run["plan_hash"]),
        "workspace_id": _identifier(run["workspace_id"]),
        "input_snapshot_id": _digest(run["input_snapshot_id"]),
        "base_commit": _commit(run["base_commit"]),
        "integration_head": _commit(run["integration_head"]),
        "integration_version": _nonnegative_int(run["integration_version"]),
        "capacity": capacity,
        "schedule_version": _nonnegative_int(run["schedule_version"]),
    }


def _task(value: object) -> dict[str, object]:
    task = _exact_dict(
        value,
        {
            "task_id",
            "kind",
            "priority",
            "state",
            "task_version",
            "scope_owner",
            "candidate_id",
            "last_lease_epoch",
        },
        "invalid Relay status task",
    )
    result: dict[str, object] = {
        "task_id": _identifier(task["task_id"]),
        "kind": _identifier(task["kind"]),
        "priority": _positive_int(task["priority"]),
        "state": _identifier(task["state"]),
        "task_version": _positive_int(task["task_version"]),
        "last_lease_epoch": _positive_int(task["last_lease_epoch"]),
    }
    for key in ("scope_owner", "candidate_id"):
        optional = task[key]
        if optional is not None:
            result[key] = _identifier(optional)
    return result


def _lease(value: object) -> dict[str, object]:
    lease = _exact_dict(
        value,
        {
            "lease_id",
            "task_id",
            "action_id",
            "epoch",
            "task_version",
            "lease_kind",
            "endpoint",
            "state",
            "released_at",
        },
        "invalid Relay status lease",
    )
    result: dict[str, object] = {
        "lease_id": _identifier(lease["lease_id"]),
        "task_id": _identifier(lease["task_id"]),
        "action_id": _identifier(lease["action_id"]),
        "epoch": _positive_int(lease["epoch"]),
        "task_version": _positive_int(lease["task_version"]),
        "lease_kind": _identifier(lease["lease_kind"]),
        "state": _identifier(lease["state"]),
    }
    _optional_string(lease["endpoint"])
    released_at = _optional_string(lease["released_at"])
    if released_at is not None:
        result["released_at"] = released_at
    return result


def _candidate(value: object) -> dict[str, object]:
    candidate = _exact_dict(
        value,
        {
            "candidate_id",
            "task_id",
            "originating_epoch",
            "branch",
            "base_commit",
            "head_commit",
            "diff_hash",
            "evidence_hashes",
            "pr_reference",
            "status",
            "review_digest",
            "integration_commit",
            "integration_tree",
            "integration_proof_id",
        },
        "invalid Relay candidate",
    )
    result: dict[str, object] = {
        "candidate_id": _identifier(candidate["candidate_id"]),
        "task_id": _identifier(candidate["task_id"]),
        "originating_epoch": _positive_int(candidate["originating_epoch"]),
        "branch": _safe_string(candidate["branch"]),
        "base_commit": _commit(candidate["base_commit"]),
        "head_commit": _commit(candidate["head_commit"]),
        "diff_hash": _digest(candidate["diff_hash"]),
        "evidence_hashes": [
            _digest(item)
            for item in _bounded_list(candidate["evidence_hashes"], _MAX_EVIDENCE)
        ],
        "status": _identifier(candidate["status"]),
    }
    for key in ("pr_reference", "review_digest"):
        optional = _optional_string(candidate[key])
        if optional is not None:
            result[key] = _digest(optional) if key == "review_digest" else optional
    for key in ("integration_commit", "integration_tree"):
        optional = candidate[key]
        if optional is not None:
            result[key] = _commit(optional)
    proof_id = candidate["integration_proof_id"]
    if proof_id is not None:
        result["integration_proof_id"] = _digest(proof_id)
    return result


def _directive(value: object) -> dict[str, object]:
    directive = _exact_dict(
        value,
        {
            "directive_id",
            "workflow_id",
            "task_id",
            "expected_schedule_version",
            "route",
            "relay_start_request",
        },
        "invalid Relay refill directive",
    )
    _private_payload(directive["relay_start_request"])
    return {
        "directive_id": _identifier(directive["directive_id"]),
        "workflow_id": _identifier(directive["workflow_id"]),
        "task_id": _identifier(directive["task_id"]),
        "expected_schedule_version": _nonnegative_int(
            directive["expected_schedule_version"]
        ),
        "route": _route(directive["route"]),
    }


def _evidence(value: object) -> dict[str, object]:
    evidence = _exact_dict(
        value, {"evidence_id", "kind", "selector", "digest"}, "invalid Relay evidence"
    )
    return {
        "evidence_id": _identifier(evidence["evidence_id"]),
        "kind": _identifier(evidence["kind"]),
        "selector": _safe_string(evidence["selector"]),
        "digest": _digest(evidence["digest"]),
    }


def _proof_summary(value: object) -> dict[str, object]:
    proof = _exact_dict(
        value,
        {
            "proof_id",
            "expectation_hash",
            "task_id",
            "candidate_id",
            "integration_version",
            "predecessor_commit",
            "final_commit",
            "final_tree",
            "attestor_id",
            "attestor_version",
        },
        "invalid Relay proof summary",
    )
    return {
        "proof_id": _digest(proof["proof_id"]),
        "expectation_hash": _digest(proof["expectation_hash"]),
        "task_id": _identifier(proof["task_id"]),
        "candidate_id": _identifier(proof["candidate_id"]),
        "integration_version": _positive_int(proof["integration_version"]),
        "predecessor_commit": _commit(proof["predecessor_commit"]),
        "final_commit": _commit(proof["final_commit"]),
        "final_tree": _commit(proof["final_tree"]),
        "attestor_id": _identifier(proof["attestor_id"]),
        "attestor_version": _identifier(proof["attestor_version"]),
    }


def _queue(value: object, maximum: int) -> list[dict[str, object]]:
    return [_task(item) for item in _bounded_list(value, maximum)]


def project_relay_status(status: object) -> dict[str, object]:
    value = _exact_dict(
        status,
        {
            "schema",
            "workflow_id",
            "run",
            "schedule_version",
            "tasks",
            "leases",
            "candidates",
            "integration_proofs",
            "outstanding_action_ids",
            "refill_directives",
            "queues",
        },
        "invalid Relay status",
    )
    if value["schema"] != _STATUS_SCHEMA:
        raise _fail("unsupported Relay status schema")
    workflow_id = _identifier(value["workflow_id"])
    run = _run(value["run"])
    if run["workflow_id"] != workflow_id:
        raise _fail("Relay run identity mismatch")
    schedule_version = _nonnegative_int(value["schedule_version"])
    if schedule_version != run["schedule_version"]:
        raise _fail("Relay schedule identity mismatch")
    tasks = [_task(item) for item in _bounded_list(value["tasks"], _MAX_TASKS)]
    task_ids = {cast(str, item["task_id"]) for item in tasks}
    candidates = [
        _candidate(item) for item in _bounded_list(value["candidates"], _MAX_CANDIDATES)
    ]
    candidates_by_id = {cast(str, item["candidate_id"]): item for item in candidates}
    if len(candidates_by_id) != len(candidates):
        raise _fail("duplicate Relay candidate")
    for candidate in candidates:
        if cast(str, candidate["task_id"]) not in task_ids:
            raise _fail("Relay candidate task identity mismatch")
    proofs = [
        _proof_summary(item)
        for item in _bounded_list(value["integration_proofs"], _MAX_PROOFS)
    ]
    predecessor = cast(str, run["base_commit"])
    previous_version = 0
    for proof in proofs:
        version = cast(int, proof["integration_version"])
        candidate_id = cast(str, proof["candidate_id"])
        candidate = candidates_by_id.get(candidate_id)
        if (
            version != previous_version + 1
            or proof["predecessor_commit"] != predecessor
            or cast(str, proof["task_id"]) not in task_ids
            or candidate is None
            or candidate["task_id"] != proof["task_id"]
            or candidate.get("integration_proof_id") != proof["proof_id"]
            or candidate.get("integration_commit") != proof["final_commit"]
            or candidate.get("integration_tree") != proof["final_tree"]
        ):
            raise _fail("Relay proof predecessor or identity mismatch")
        predecessor = cast(str, proof["final_commit"])
        previous_version = version
    if (
        run["integration_version"] != previous_version
        or run["integration_head"] != predecessor
    ):
        raise _fail("Relay proof chain mismatch")
    queues = _exact_dict(
        value["queues"],
        {
            "prepared_prewarms",
            "ready",
            "running_slots",
            "review_integration",
            "terminal",
        },
        "invalid Relay queues",
    )
    return envelope_success(
        {
            "workflow_id": workflow_id,
            "run": run,
            "schedule_version": schedule_version,
            "tasks": tasks,
            "leases": [
                _lease(item) for item in _bounded_list(value["leases"], _MAX_LEASES)
            ],
            "candidates": candidates,
            "integration_proofs": proofs,
            "outstanding_action_ids": [
                _identifier(item)
                for item in _bounded_list(
                    value["outstanding_action_ids"], _MAX_EVIDENCE
                )
            ],
            "refill_directives": [
                _directive(item)
                for item in _bounded_list(value["refill_directives"], _MAX_EVIDENCE)
            ],
            "queues": {
                name: _queue(queues[name], _MAX_TASKS)
                for name in (
                    "prepared_prewarms",
                    "ready",
                    "running_slots",
                    "review_integration",
                    "terminal",
                )
            },
        }
    )


def _mutation(value: object, *, allow_evidence: bool) -> dict[str, object]:
    base = {"workflow_id", "schedule_version", "task"}
    candidates = base | {"candidate"}
    evidence = base | {"evidence"}
    if type(value) is not dict:
        raise _fail("invalid Relay mutation result")
    keys = set(cast(dict[object, object], value))
    allowed = [base, candidates, evidence] if allow_evidence else [base, candidates]
    if keys not in allowed:
        raise _fail("invalid Relay mutation result")
    data: dict[str, object] = {
        "workflow_id": _identifier(value["workflow_id"]),
        "schedule_version": _nonnegative_int(value["schedule_version"]),
        "task": _task(value["task"]),
    }
    if "candidate" in value:
        data["candidate"] = _candidate(value["candidate"])
    if "evidence" in value:
        data["evidence"] = _evidence(value["evidence"])
    return data


def project_relay_handoff(result: object) -> dict[str, object]:
    return envelope_success(_mutation(result, allow_evidence=True))


def project_relay_integrate(result: object) -> dict[str, object]:
    return envelope_success(_mutation(result, allow_evidence=False))
