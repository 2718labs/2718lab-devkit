from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import devkit_runtime.project_authority as project_authority_module
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.project_authority import (
    ProjectAuthority,
    ProjectAuthorityError,
    ProjectAuthorityReceipt,
    ProjectPhysicalBinding,
)


def test_authority_receipt_reopens_same_physical_project_with_same_identity(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    issued = ProjectAuthority.issue(project_root)
    persisted = issued.receipt.to_json()
    receipt = ProjectAuthorityReceipt.from_json(persisted)
    reopened = ProjectAuthority.reopen(project_root, receipt)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    environment = {
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CODEX_TASK_TEMP": str(scratch_root),
    }
    issued_config = RuntimeConfig.load(
        environ=environment,
        project_authority=issued,
    )
    reopened_config = RuntimeConfig.load(
        environ=environment,
        project_authority=reopened,
    )

    identity_material = {
        "authority_nonce": receipt.authority_nonce,
        "domain": "2718lab/project-authority/v1",
        "physical_binding": {
            "device_id": str(receipt.physical_binding.device_id),
            "file_id": str(receipt.physical_binding.file_id),
            "scheme": "os-stat-directory-v1",
        },
    }
    canonical = json.dumps(
        identity_material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    assert reopened.project_id == issued.project_id
    assert reopened.receipt == issued.receipt
    assert reopened_config.data_root == issued_config.data_root
    assert reopened.project_id == hashlib.sha256(canonical).hexdigest()
    assert persisted == issued.receipt.to_json()


def test_authority_cannot_be_issued_without_an_existing_directory(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("not a project", encoding="utf-8")

    for candidate in (missing, not_a_directory):
        with pytest.raises(ProjectAuthorityError) as caught:
            ProjectAuthority.issue(candidate)
        assert caught.value.code == "PROJECT_ROOT_INVALID"


def test_authority_rejects_reparse_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda candidate: Path(candidate) == project_root,
        raising=False,
    )

    with pytest.raises(ProjectAuthorityError) as caught:
        ProjectAuthority.issue(project_root)

    assert caught.value.code == "PROJECT_ROOT_INVALID"


def test_authority_rejects_identity_that_changes_during_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    read_once = project_authority_module._read_physical_binding_once
    reads = 0

    def changing_binding(path: Path) -> ProjectPhysicalBinding:
        nonlocal reads
        binding = read_once(path)
        reads += 1
        if reads == 2:
            return ProjectPhysicalBinding(
                device_id=binding.device_id,
                file_id=binding.file_id + 1,
            )
        return binding

    monkeypatch.setattr(
        project_authority_module,
        "_read_physical_binding_once",
        changing_binding,
    )

    with pytest.raises(ProjectAuthorityError) as caught:
        ProjectAuthority.issue(project_root)

    assert caught.value.code == "PROJECT_ROOT_UNSTABLE"


def test_replacement_at_same_path_cannot_reopen_original_authority(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    original_root = tmp_path / "original-project"
    project_root.mkdir()
    issued = ProjectAuthority.issue(project_root)
    project_root.rename(original_root)
    project_root.mkdir()

    with pytest.raises(ProjectAuthorityError) as caught:
        ProjectAuthority.reopen(project_root, issued.receipt)

    assert caught.value.code == "PROJECT_AUTHORITY_MISMATCH"


def test_distinct_project_roots_cannot_collide_through_same_caller_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    monkeypatch.setattr(
        project_authority_module.secrets,
        "token_hex",
        lambda _: "01" * 32,
    )
    authority_a = ProjectAuthority.issue(project_a)
    authority_b = ProjectAuthority.issue(project_b)
    shared_environment = {
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CODEX_TASK_TEMP": str(tmp_path / "scratch"),
        "CODEX_PROJECT_ID": "caller-selected-id",
    }
    (tmp_path / "scratch").mkdir()

    config_a = RuntimeConfig.load(
        environ=shared_environment,
        project_authority=authority_a,
    )
    config_b = RuntimeConfig.load(
        environ=shared_environment,
        project_authority=authority_b,
    )

    assert authority_a.project_id != authority_b.project_id
    assert config_a.data_root != config_b.data_root
    assert config_a.data_root.parent.name == "projects-v2"
    assert config_b.data_root.parent.name == "projects-v2"


def test_caller_selected_ids_cannot_choose_authority_ownership(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    authority = ProjectAuthority.issue(project_root)
    common = {
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CODEX_TASK_TEMP": str(scratch_root),
    }

    config_a = RuntimeConfig.load(
        environ={**common, "CODEX_PROJECT_ID": "project-a"},
        project_authority=authority,
    )
    config_b = RuntimeConfig.load(
        environ={**common, "CODEX_PROJECT_ID": "project-b"},
        project_authority=authority,
    )

    assert config_a.project_authority == authority
    assert config_b.project_authority == authority
    assert config_a.data_root == config_b.data_root
    assert config_a.data_root == (
        tmp_path / "plugin-data" / "projects-v2" / authority.project_id
    )


def test_legacy_caller_scope_is_explicitly_not_project_authority(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "plugin-data"),
            "CODEX_TASK_TEMP": str(scratch_root),
            "CODEX_PROJECT_ID": "legacy-caller-id",
        }
    )

    assert config.project_authority is None
    assert config.storage_layout == "legacy-compat"
    assert config.data_root.parent.name == "scoped-v1"
