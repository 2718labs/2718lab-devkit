from __future__ import annotations

import hashlib
import json


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _intent_with_unknown_descriptor_key(
    key: str, value: str
) -> dict[str, object]:
    descriptor: dict[str, str] = {
        "schema": "2718lab.storage.target.v1",
        "artifact_kind": "cargo-target",
        "repository_identity": "sha256:" + "d" * 64,
        "workspace_manifest_hash": "sha256:" + "e" * 64,
        "cargo_lock_hash": "sha256:" + "f" * 64,
        "toolchain_digest": "sha256:" + "1" * 64,
        "target_triple": "x86_64-pc-windows-msvc",
        "profile": "dev",
        "features_hash": "sha256:" + "2" * 64,
        "build_env_class": "windows-msvc",
    }
    intent: dict[str, object] = {
        "schema": "2718lab.storage.intent.v1",
        "task_id": "task-01",
        "plan_binding": "sha256:" + "a" * 64,
        "context_hash": "sha256:" + "b" * 64,
        "requested_bytes": 1,
        "requested_files": 1,
        "target_descriptor": descriptor,
    }
    intent["storage_intent_hash"] = _canonical_hash(
        {
            "target_descriptor": descriptor,
            "task_id": intent["task_id"],
            "plan_binding": intent["plan_binding"],
            "context_hash": intent["context_hash"],
            "requested_bytes": intent["requested_bytes"],
            "requested_files": intent["requested_files"],
        }
    )
    descriptor[key] = value
    return intent


def _assert_storage_intent_rejected(value: dict[str, object]) -> None:
    from devkit_runtime.storage_intent import StorageIntentError, parse_storage_intent

    try:
        parse_storage_intent(value)
    except StorageIntentError as error:
        assert error.code == "STORAGE_TARGET_KEY_INVALID"
    else:
        raise AssertionError("invalid descriptor was accepted")


def test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key() -> None:
    _assert_storage_intent_rejected(
        _intent_with_unknown_descriptor_key("path", "G:/unapproved")
    )


def test_storage_intent_rejects_plain_unknown_descriptor_key() -> None:
    _assert_storage_intent_rejected(
        _intent_with_unknown_descriptor_key("unexpected", "cache")
    )


def test_storage_intent_rejects_isolated_surrogate_with_stable_code() -> None:
    value = _intent_with_unknown_descriptor_key("unexpected", "cache")
    descriptor = value["target_descriptor"]
    assert isinstance(descriptor, dict)
    descriptor.pop("unexpected")
    descriptor["target_triple"] = "\ud800"

    from devkit_runtime.storage_intent import StorageIntentError, parse_storage_intent

    try:
        parse_storage_intent(value)
    except StorageIntentError as error:
        assert error.code == "STORAGE_TARGET_KEY_INVALID"
    else:
        raise AssertionError("invalid surrogate was accepted")


class _StorageBindingRoutingCore:
    def load_policy_v5(self) -> dict[str, object]:
        return {}

    def policy_hash_v5(self, policy: object) -> str:
        del policy
        return _canonical_hash({"policy": "storage-binding"})

    def _normalise_request_v5(
        self, request: dict[str, object], policy: object
    ) -> dict[str, object]:
        del policy
        return request

    def v5_request_binding_hash(self, request: object) -> str:
        return _canonical_hash(request)

    def route_v5(
        self, request: dict[str, object], *, policy: object
    ) -> dict[str, object]:
        del policy
        task = request["task"]
        assert isinstance(task, dict)
        return {
            "schema": "2718lab-devkit/fastlane-routing-result-v5",
            "status": "resolved",
            "task_id": task["task_id"],
            "route": {
                "model": "gpt-5.6-luna",
                "effort": "max",
                "inherit_current_session_model": False,
            },
        }


class _StorageBindingApi:
    def __init__(self) -> None:
        self.core = _StorageBindingRoutingCore()

    def _mapping(self, value: object, field: str) -> dict[str, object]:
        assert isinstance(value, dict), field
        return value

    def _task_id(self, value: object, field: str) -> str:
        assert isinstance(value, str), field
        return value

    def _normalised_scopes(self, value: object, field: str = "scope") -> list[str]:
        assert isinstance(value, list), field
        return value

    def _canonical_json(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _sha256_json(self, value: object) -> str:
        return _canonical_hash(value)

    def _hash(self, value: object, field: str) -> str:
        assert isinstance(value, str), field
        return value

    def _text(self, value: object, field: str, *, maximum: int) -> str:
        assert isinstance(value, str) and len(value) <= maximum, field
        return value

    def _exact_keys(
        self, value: dict[str, object], expected: frozenset[str], field: str
    ) -> None:
        assert set(value) == expected, field

    def _fast_lane_routing_core(self) -> _StorageBindingRoutingCore:
        return self.core


def _storage_binding_unit(task_id: str, dispatch_order: int) -> dict[str, object]:
    return {
        "task": {
            "schema": "2718lab-devkit/task-routing-profile-v5",
            "task_id": task_id,
            "role": "execution",
            "access": "workspace_write",
            "write_scope_count": 1,
            "overlap_risk": "none",
            "overlap_count": 0,
        },
        "dependency_state": {"task_id": task_id},
        "write_scope": [f"src/{task_id}.py"],
        "concurrency_mode": "parallel",
        "dispatch_order": dispatch_order,
        "index_context_hash": "sha256:" + "b" * 64,
        "workflow_id_hash": "sha256:" + "c" * 64,
        "storage_budget": {"bytes": 4096, "files": 8},
    }


def _storage_binding_context() -> dict[str, object]:
    return {
        "execution_context_hash": "sha256:" + "4" * 64,
        "repository_identity": "sha256:" + "5" * 64,
        "workspace_manifest_hash": "sha256:" + "6" * 64,
        "cargo_lock_hash": "sha256:" + "7" * 64,
        "toolchain_digest": "sha256:" + "8" * 64,
        "target_triple": "x86_64-pc-windows-msvc",
        "profile": "dev",
        "features_hash": "sha256:" + "9" * 64,
        "build_env_class": "windows-msvc",
    }


def _storage_binding_request(
    api: _StorageBindingApi, unit: dict[str, object], source_plan_hash: str
) -> tuple[dict[str, object], dict[str, object]]:
    task = unit["task"]
    assert isinstance(task, dict)
    request: dict[str, object] = {
        "task": task,
        "scheduler_facts": {"route_epoch": 1},
        "child_route_attestation": None,
    }
    binding_hash = api.core.v5_request_binding_hash(request)
    attestation: dict[str, object] = {
        "request_binding_hash": binding_hash,
        "attestation": {
            "status": "attested",
            "request_binding_hash": binding_hash,
        },
    }
    attestation_payload = attestation["attestation"]
    assert isinstance(attestation_payload, dict)
    attestation_payload["attestation_hash"] = _canonical_hash(
        {
            key: value
            for key, value in attestation_payload.items()
            if key != "attestation_hash"
        }
    )
    item = {
        "task_id": task["task_id"],
        "request_binding_hash": binding_hash,
        "attestation": attestation_payload,
    }
    del source_plan_hash
    return request, item


def test_every_fastlane_wave_carries_plan_context_bound_storage_intent() -> None:
    from devkit_fastlane.scripts.authenticated_v5_planner import compile_skeletons

    api = _StorageBindingApi()
    plan_hash = "sha256:" + "a" * 64
    context = _storage_binding_context()
    first_unit = _storage_binding_unit("task-01", 0)
    successor_unit = _storage_binding_unit("task-02", 1)

    first_request, first_attestation = _storage_binding_request(
        api, first_unit, plan_hash
    )
    successor_request, successor_attestation = _storage_binding_request(
        api, successor_unit, plan_hash
    )
    first = compile_skeletons(
        api,
        [first_unit],
        source_plan_hash=plan_hash,
        routing_requests=[first_request],
        attestation_items=[first_attestation],
        context=context,
    )
    successor = compile_skeletons(
        api,
        [successor_unit],
        source_plan_hash=plan_hash,
        routing_requests=[successor_request],
        attestation_items=[successor_attestation],
        context=context,
    )

    for wave in (first["assignment_skeletons"], successor["assignment_skeletons"]):
        for assignment in wave:
            intent = assignment["storage_intent"]
            assert intent["task_id"] == assignment["task_id"]
            assert intent["plan_binding"] == plan_hash
            assert intent["context_hash"] == context["execution_context_hash"]
