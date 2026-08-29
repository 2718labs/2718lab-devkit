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
