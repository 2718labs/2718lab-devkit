from __future__ import annotations


def test_storage_intent_rejects_absolute_path_and_unknown_descriptor_key() -> None:
    from devkit_runtime.storage_intent import StorageIntentError, parse_storage_intent

    value = {
        "schema": "2718lab.storage.intent.v1",
        "task_id": "task-01",
        "plan_binding": "sha256:" + "a" * 64,
        "context_hash": "sha256:" + "b" * 64,
        "storage_intent_hash": "sha256:" + "c" * 64,
        "requested_bytes": 1,
        "requested_files": 1,
        "target_descriptor": {
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
            "path": "G:/unapproved",
        },
    }

    try:
        parse_storage_intent(value)
    except StorageIntentError as error:
        assert error.code == "STORAGE_TARGET_KEY_INVALID"
    else:
        raise AssertionError("invalid descriptor was accepted")
