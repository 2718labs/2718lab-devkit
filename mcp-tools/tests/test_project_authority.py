from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

import devkit_runtime.project_authority as project_authority_module
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig, RuntimeConfigError
from devkit_runtime.project_authority import (
    ProjectAuthority,
    ProjectAuthorityError,
    ProjectAuthorityReceipt,
    ProjectFence,
    ProjectPhysicalBinding,
    RuntimeProjectAuthorityProvider,
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
    assert reopened.project_fence() == issued.project_fence()
    assert reopened.project_id == hashlib.sha256(canonical).hexdigest()
    assert persisted == issued.receipt.to_json()


def test_project_fence_is_canonical_and_deterministic_across_reopen(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    issued = ProjectAuthority.issue(project_root)
    reopened = ProjectAuthority.reopen(
        project_root,
        ProjectAuthorityReceipt.from_json(issued.receipt.to_json()),
    )

    issued_fence = issued.project_fence()
    reopened_fence = reopened.project_fence()
    expected_material = {
        "authority_nonce": issued.receipt.authority_nonce,
        "binding_version": 1,
        "domain": "2718lab/project-fence/v1",
        "physical_binding": {
            "device_id": str(issued.receipt.physical_binding.device_id),
            "file_id": str(issued.receipt.physical_binding.file_id),
            "scheme": "os-stat-directory-v1",
        },
        "project_id": issued.project_id,
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            expected_material,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()

    assert issued_fence == reopened_fence
    assert issued_fence.schema == "team-efficiency/project-fence-v1"
    assert issued_fence.project_id == issued.project_id
    assert issued_fence.binding_digest == expected_digest
    assert issued_fence.binding_version == 1
    assert isinstance(issued_fence, ProjectFence)


def test_project_fence_changes_when_authority_receipt_or_binding_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    nonces = iter(("01" * 32, "01" * 32, "02" * 32))
    monkeypatch.setattr(
        project_authority_module.secrets,
        "token_hex",
        lambda _: next(nonces),
    )

    same_binding_new_receipt = ProjectAuthority.issue(project_a)
    different_binding_same_receipt_nonce = ProjectAuthority.issue(project_b)
    replacement_receipt = ProjectAuthority.issue(project_a)

    assert (
        same_binding_new_receipt.project_fence()
        != replacement_receipt.project_fence()
    )
    assert (
        same_binding_new_receipt.project_fence()
        != different_binding_same_receipt_nonce.project_fence()
    )


def test_project_fence_revalidates_authority_and_is_not_a_runtime_provider(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    original_root = tmp_path / "original-project"
    scratch_root = tmp_path / "scratch"
    project_root.mkdir()
    scratch_root.mkdir()
    authority = ProjectAuthority.issue(project_root)
    fence = authority.project_fence()
    project_root.rename(original_root)
    project_root.mkdir()

    with pytest.raises(ProjectAuthorityError) as revalidated:
        authority.project_fence()
    assert revalidated.value.code == "PROJECT_AUTHORITY_MISMATCH"

    with pytest.raises(RuntimeConfigError) as provider:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "plugin-data"),
                "CODEX_TASK_TEMP": str(scratch_root),
            },
            authority_provider=fence,  # type: ignore[arg-type]
        )
    assert provider.value.code == "PROJECT_AUTHORITY_PROVIDER_INVALID"


def test_project_fence_has_no_caller_construction_or_json_rehydration_api() -> None:
    with pytest.raises(TypeError):
        ProjectFence(
            schema="team-efficiency/project-fence-v1",
            project_id="01" * 32,
            binding_digest="02" * 32,
            binding_version=1,
        )

    assert not hasattr(ProjectFence, "from_json")
    assert not hasattr(ProjectFence, "from_project_root")


def test_project_fence_has_no_module_construction_token() -> None:
    assert not hasattr(project_authority_module, "_FENCE_CONSTRUCTION_TOKEN")


def test_public_provider_minting_cannot_activate_projects_v2_without_host_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    plugin_data = tmp_path / "plugin-data"
    scratch_root = tmp_path / "scratch"
    project_root.mkdir()
    scratch_root.mkdir()

    with pytest.raises(ProjectAuthorityError) as caught:
        RuntimeProjectAuthorityProvider.issue(project_root)

    assert caught.value.code == "PROJECT_AUTHORITY_UNAVAILABLE"
    assert not plugin_data.exists()


def test_public_provider_reopen_cannot_activate_projects_v2_without_host_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    receipt = ProjectAuthority.issue(project_root).receipt

    with pytest.raises(ProjectAuthorityError) as caught:
        RuntimeProjectAuthorityProvider.reopen(project_root, receipt)

    assert caught.value.code == "PROJECT_AUTHORITY_UNAVAILABLE"


def test_provider_constructor_has_no_module_token_without_host_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    authority = ProjectAuthority.issue(project_root)

    assert not hasattr(project_authority_module, "_PROVIDER_CONSTRUCTION_TOKEN")
    with pytest.raises(ProjectAuthorityError) as caught:
        RuntimeProjectAuthorityProvider(authority, _token=object())

    assert caught.value.code == "PROJECT_AUTHORITY_UNAVAILABLE"


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

    assert authority_a.project_id != authority_b.project_id
    assert authority_a.project_fence() != authority_b.project_fence()


def test_caller_selected_ids_remain_legacy_scope_without_host_admission(
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
    )
    config_b = RuntimeConfig.load(
        environ={**common, "CODEX_PROJECT_ID": "project-b"},
    )

    assert config_a.project_authority is None
    assert config_b.project_authority is None
    assert config_a.storage_layout == "legacy-compat"
    assert config_b.storage_layout == "legacy-compat"
    assert config_a.data_root != config_b.data_root
    assert authority.project_id not in str(config_a.data_root)
    assert authority.project_id not in str(config_b.data_root)


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


def test_runtime_config_rejects_authority_without_module_minted_provider(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    authority = ProjectAuthority.issue(project_root)
    environment = {
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CODEX_TASK_TEMP": str(scratch_root),
    }

    with pytest.raises(RuntimeConfigError) as direct:
        RuntimeConfig.load(
            environ=environment,
            authority_provider=authority,  # type: ignore[arg-type]
        )
    assert direct.value.code == "PROJECT_AUTHORITY_PROVIDER_INVALID"

    forged = RuntimeConfig(
        data_root=tmp_path / "plugin-data" / "projects-v2" / authority.project_id,
        scratch_root=scratch_root,
        project_authority=authority,
        storage_layout="projects-v2",
    )
    with pytest.raises(RuntimeConfigError) as manually_constructed:
        forged.require_project_authority()
    assert manually_constructed.value.code == "PROJECT_AUTHORITY_PROVIDER_INVALID"


@pytest.mark.parametrize(
    "binding",
    (
        cast(ProjectPhysicalBinding, {"device_id": "1", "file_id": "2"}),
        ProjectPhysicalBinding(device_id=cast(int, "1"), file_id=2),
    ),
)
def test_malformed_runtime_receipt_types_fail_with_stable_error(
    tmp_path: Path,
    binding: ProjectPhysicalBinding,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    receipt = ProjectAuthorityReceipt(
        authority_nonce="01" * 32,
        physical_binding=binding,
        project_id="02" * 32,
    )

    with pytest.raises(ProjectAuthorityError) as caught:
        ProjectAuthority.reopen(project_root, receipt)

    assert caught.value.code == "PROJECT_AUTHORITY_RECEIPT_INVALID"


@pytest.mark.parametrize("read_only", (True, False))
def test_runtime_root_rejects_forged_projects_v2_config_before_uow_factory(
    tmp_path: Path,
    read_only: bool,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    authority = ProjectAuthority.issue(project_root)
    forged = RuntimeConfig(
        data_root=tmp_path / "plugin-data" / "projects-v2" / authority.project_id,
        scratch_root=tmp_path / "scratch" / "projects-v2" / authority.project_id,
        project_authority=authority,
        storage_layout="projects-v2",
    )
    factory_calls: list[bool] = []
    root = RuntimeRoot(
        forged,
        uow_factory=lambda *, config, read_only: factory_calls.append(read_only),
    )

    with pytest.raises(RuntimeConfigError) as caught:
        root.open_uow(read_only=read_only)

    assert caught.value.code == "PROJECT_AUTHORITY_PROVIDER_INVALID"
    assert factory_calls == []


@pytest.mark.parametrize("read_only", (True, False))
def test_runtime_root_rejects_authority_disguised_as_legacy_config(
    tmp_path: Path,
    read_only: bool,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    authority = ProjectAuthority.issue(project_root)
    forged = RuntimeConfig(
        data_root=tmp_path / "plugin-data",
        scratch_root=tmp_path / "scratch",
        project_authority=authority,
        storage_layout="legacy-compat",
    )
    factory_calls: list[bool] = []
    root = RuntimeRoot(
        forged,
        uow_factory=lambda *, config, read_only: factory_calls.append(read_only),
    )

    with pytest.raises(RuntimeConfigError) as caught:
        root.open_uow(read_only=read_only)

    assert caught.value.code == "PROJECT_AUTHORITY_PROVIDER_INVALID"
    assert factory_calls == []


@pytest.mark.parametrize("read_only", (True, False))
def test_runtime_root_keeps_explicit_legacy_config_openable(
    tmp_path: Path,
    read_only: bool,
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "plugin-data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    opened: list[bool] = []
    uow = object()
    root = RuntimeRoot(
        config,
        uow_factory=lambda *, config, read_only: opened.append(read_only) or uow,
    )

    assert root.open_uow(read_only=read_only) is uow
    assert opened == [read_only]
