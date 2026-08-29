from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


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


def _host_storage_intent(
    *, task_id: str, plan_binding: str, context_hash: str
) -> dict[str, object]:
    context = _storage_binding_context()
    descriptor = {
        "schema": "2718lab.storage.target.v1",
        "artifact_kind": "fastlane-task",
        **{
            key: value
            for key, value in context.items()
            if key != "execution_context_hash"
        },
    }
    intent = {
        "schema": "2718lab.storage.intent.v1",
        "task_id": task_id,
        "plan_binding": plan_binding,
        "context_hash": context_hash,
        "requested_bytes": 4096,
        "requested_files": 8,
        "target_descriptor": descriptor,
    }
    intent["storage_intent_hash"] = _canonical_hash(
        {
            key: intent[key]
            for key in (
                "target_descriptor",
                "task_id",
                "plan_binding",
                "context_hash",
                "requested_bytes",
                "requested_files",
            )
        }
    )
    return intent


def test_host_intent_requires_one_canonical_storage_binding_and_typed_failures() -> None:
    from test_fastlane_host_intent import _intent, _with_binding
    from devkit_runtime.fastlane_host_intent import (
        NO_SAFE_WORK,
        STORAGE_TARGET_KEY_INVALID,
        StorageIntentError,
        parse_host_execution_intent,
        validate_host_execution_intent,
    )

    legacy = _intent()
    legacy_result = parse_host_execution_intent(legacy)
    assert isinstance(legacy_result, StorageIntentError)
    assert legacy_result.code == STORAGE_TARGET_KEY_INVALID
    assert validate_host_execution_intent(legacy) is NO_SAFE_WORK

    candidate = _intent()
    task_id = candidate["assignment"]["predecessor"]["task_id"]
    source_plan_hash = candidate["source_plan_hash"]
    context_hash = "sha256:" + "4" * 64
    storage_intent = _host_storage_intent(
        task_id=task_id,
        plan_binding=source_plan_hash,
        context_hash=context_hash,
    )
    candidate["storage_intent"] = storage_intent
    candidate["execution_context_hash"] = context_hash
    candidate["intent_hash"] = _canonical_hash(
        {key: value for key, value in candidate.items() if key != "intent_hash"}
    )
    assert parse_host_execution_intent(candidate).storage_intent is not None

    duplicate = copy.deepcopy(candidate)
    duplicate["assignment"]["storage_intent"] = storage_intent
    duplicate["assignment"] = _with_binding(
        duplicate["assignment"], "assignment_binding_hash"
    )
    duplicate["intent_hash"] = _canonical_hash(
        {key: value for key, value in duplicate.items() if key != "intent_hash"}
    )
    duplicate_result = parse_host_execution_intent(duplicate)
    conflict = copy.deepcopy(candidate)
    conflict["storage_intent"] = _host_storage_intent(
        task_id=task_id,
        plan_binding=source_plan_hash,
        context_hash="sha256:" + "3" * 64,
    )
    conflict = _with_binding(conflict, "intent_hash")
    conflict_result = parse_host_execution_intent(conflict)
    for result in (duplicate_result, conflict_result):
        assert isinstance(result, StorageIntentError)
        assert result.code == STORAGE_TARGET_KEY_INVALID


def _real_storage_request() -> tuple[
    object,
    object,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    str | None,
]:
    test_module_path = (
        Path(__file__).resolve().parents[1]
        / "devkit_fastlane"
        / "tests"
        / "test_team_efficiency.py"
    )
    spec = importlib.util.spec_from_file_location(
        "storage_test_team_efficiency_tests", test_module_path
    )
    assert spec is not None and spec.loader is not None
    tests_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests_module)

    previous_task_temp = os.environ.get("CODEX_TASK_TEMP")
    if previous_task_temp is None:
        os.environ["CODEX_TASK_TEMP"] = str(
            Path(__file__).resolve().parents[3]
            / ".codex-task-temp"
        )
    fixture = tests_module.TeamEfficiencyTests("runTest")
    fixture.setUp()
    helper = tests_module.load_efficiency()
    request = fixture.fast_lane_request(helper)
    task_ids = [item["task_id"] for item in request["execution_contexts"]]
    descriptor = {
        key: value
        for key, value in _storage_binding_context().items()
        if key != "execution_context_hash"
    }
    for context in request["execution_contexts"]:
        context.update(
            {
                **descriptor,
                "execution_context_hash": _canonical_hash(
                    {"storage_context": context["task_id"]}
                ),
            }
        )
    request["storage_budgets"] = {
        task_id: {"bytes": 4096 + index, "files": 8 + index}
        for index, task_id in enumerate(task_ids)
    }
    host_status = fixture.fast_lane_host_status(helper, request)
    route_request = host_status["routing_context"]["routes"][0]["request"]
    host = copy.deepcopy(route_request["host_capabilities"])
    host["models"] = [
        {**model, "efforts": sorted(model["efforts"])} for model in host["models"]
    ]
    scheduler = route_request["scheduler_facts"]
    return fixture, helper, request, host, scheduler, previous_task_temp


def test_real_prepare_entry_binds_initial_successor_and_missing_facts_fail_closed() -> None:
    fixture, helper, request, host, scheduler, previous_task_temp = _real_storage_request()
    try:
        prepared = helper.prepare_authenticated_v5_routing_from_request(
            request,
            index_context_hash=helper._sha256_json({"index": "storage-real-entry"}),
            host_capabilities=host,
            scheduler_facts=scheduler,
        )
        for wave in (prepared["units"], prepared["remaining_units"]):
            assert wave
            for unit in wave:
                intent = unit["storage_intent"]
                assert intent["task_id"] == unit["task"]["task_id"]
                assert intent["plan_binding"] == prepared["source_plan_hash"]
                assert intent["context_hash"] == _canonical_hash(
                    {"storage_context": unit["task"]["task_id"]}
                )

        missing = copy.deepcopy(request)
        for context in missing["execution_contexts"]:
            context.pop("execution_context_hash")
        with pytest.raises(ValueError, match="STORAGE_POLICY_MISSING"):
            helper.prepare_authenticated_v5_routing_from_request(
                missing,
                index_context_hash=helper._sha256_json({"index": "storage-real-entry"}),
                host_capabilities=host,
                scheduler_facts=scheduler,
            )
    finally:
        fixture.tearDown()
        if previous_task_temp is None:
            os.environ.pop("CODEX_TASK_TEMP", None)
        else:
            os.environ["CODEX_TASK_TEMP"] = previous_task_temp
