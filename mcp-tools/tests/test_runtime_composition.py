from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from devkit_relay.service import RelayError
from devkit_runtime.bootstrap import RuntimeBootstrap
from devkit_runtime.composition import RuntimeRoot
from devkit_runtime.config import RuntimeConfig, RuntimeConfigError
from devkit_runtime.relay_runtime import RelayRuntime
from devkit_runtime.uow import RuntimeAdapterFactories, RuntimeUnitOfWork


def test_runtime_config_load_prefers_plugin_data_without_writing(
    tmp_path: Path,
) -> None:
    plugin_data = tmp_path / "plugin-data"
    codex_home = tmp_path / "codex-home"

    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(plugin_data), "CODEX_HOME": str(codex_home)}
    )

    assert config.data_root == plugin_data
    assert config.orchestrator_database == plugin_data / "orchestrator.sqlite3"
    assert config.project_index_database == plugin_data / "project-index.sqlite3"
    assert not plugin_data.exists()
    assert not codex_home.exists()


def test_runtime_config_scopes_shared_plugin_data_by_project_root(
    tmp_path: Path,
) -> None:
    plugin_data = tmp_path / "plugin-data"
    scratch_base = tmp_path / "scratch"
    scratch_base.mkdir()
    project_a = tmp_path / "AstrContinuum"
    project_b = tmp_path / "2718-devkit"
    project_a.mkdir()
    project_b.mkdir()

    config_a = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(plugin_data),
            "CODEX_TASK_TEMP": str(scratch_base),
            "CODEX_PROJECT_ROOT": str(project_a),
            "CODEX_THREAD_ID": "thread-a",
        }
    )
    config_b = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(plugin_data),
            "CODEX_TASK_TEMP": str(scratch_base),
            "CODEX_PROJECT_ROOT": str(project_b),
            "CODEX_THREAD_ID": "thread-b",
        }
    )

    assert config_a.data_root != config_b.data_root
    assert config_a.scratch_root != config_b.scratch_root
    assert config_a.data_root.parent == config_b.data_root.parent
    assert config_a.data_root.parent.name == "scoped-v1"
    assert config_a.data_root.is_relative_to(plugin_data)
    assert config_b.data_root.is_relative_to(plugin_data)
    assert config_a.scratch_root.is_relative_to(scratch_base)
    assert config_b.scratch_root.is_relative_to(scratch_base)


def test_runtime_config_uses_thread_scope_when_project_root_is_unavailable(
    tmp_path: Path,
) -> None:
    plugin_data = tmp_path / "plugin-data"
    scratch_base = tmp_path / "scratch"
    scratch_base.mkdir()

    config_a = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(plugin_data),
            "CODEX_TASK_TEMP": str(scratch_base),
            "CODEX_THREAD_ID": "thread-a",
        }
    )
    config_b = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(plugin_data),
            "CODEX_TASK_TEMP": str(scratch_base),
            "CODEX_THREAD_ID": "thread-b",
        }
    )

    assert config_a.data_root != config_b.data_root
    assert config_a.scratch_root != config_b.scratch_root


def test_runtime_config_rejects_untrusted_project_scope(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    data_file = tmp_path / "not-a-directory"
    data_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeConfigError) as relative:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "data"),
                "CODEX_TASK_TEMP": str(scratch_root),
                "CODEX_PROJECT_ROOT": "relative-project",
            }
        )
    assert relative.value.code == "DATA_ROOT_INVALID"

    with pytest.raises(RuntimeConfigError) as file_scope:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "data"),
                "CODEX_TASK_TEMP": str(scratch_root),
                "CODEX_PROJECT_ROOT": str(data_file),
            }
        )
    assert file_scope.value.code == "PROJECT_SCOPE_INVALID"

    with pytest.raises(RuntimeConfigError) as invalid_id:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(tmp_path / "data"),
                "CODEX_TASK_TEMP": str(scratch_root),
                "CODEX_THREAD_ID": "thread\\nsecret",
            }
        )
    assert invalid_id.value.code == "PROJECT_SCOPE_INVALID"


def test_runtime_config_rejects_relative_data_root() -> None:
    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(environ={"PLUGIN_DATA": "relative-plugin-data"})

    assert caught.value.code == "DATA_ROOT_INVALID"


def test_runtime_config_rejects_existing_file_or_reparse_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_file = tmp_path / "data-file"
    data_file.write_text("not a directory", encoding="utf-8")
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_file),
                "CODEX_TASK_TEMP": str(scratch_root),
            }
        )

    assert caught.value.code == "DATA_ROOT_INVALID"

    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda candidate: Path(candidate) == data_root,
        raising=False,
    )
    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_root),
                "CODEX_TASK_TEMP": str(scratch_root),
            }
        )

    assert caught.value.code == "DATA_ROOT_INVALID"


def test_runtime_config_prefers_task_scratch_and_rejects_data_overlap(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
            "TMPDIR": str(tmp_path / "ignored-tmpdir"),
        }
    )

    assert config.scratch_root == scratch_root

    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_root),
                "CODEX_TASK_TEMP": str(data_root),
            }
        )

    assert caught.value.code == "DATA_ROOT_INVALID"


def test_runtime_config_rejects_overlap_with_protected_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_root),
                "CODEX_TASK_TEMP": str(scratch_root),
            },
            protected_roots=(scratch_root,),
        )

    assert caught.value.code == "DATA_ROOT_INVALID"


def test_runtime_config_rejects_reparse_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda candidate: Path(candidate) == scratch_root,
        raising=False,
    )

    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeConfig.load(
            environ={
                "PLUGIN_DATA": str(data_root),
                "CODEX_TASK_TEMP": str(scratch_root),
            }
        )

    assert caught.value.code == "DATA_ROOT_INVALID"


def test_explicit_bootstrap_is_idempotent_and_prepares_wal_stores(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )

    prepared_registry: list[Path] = []

    def prepare_proof_registry(database_path: Path) -> None:
        prepared_registry.append(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_marker (id INTEGER)"
            )

    assert not data_root.exists()
    RuntimeBootstrap.run(config, proof_registry_bootstrap=prepare_proof_registry)

    databases = (
        config.orchestrator_database,
        config.project_index_database,
        config.atlas_database,
        config.relay_database,
        config.relay_proof_registry_database,
    )
    assert all(path.is_file() for path in databases)
    assert config.checkpoint_cas_root.is_dir()
    first_key = config.relay_capability_key.read_bytes()
    assert len(first_key) == 32
    assert prepared_registry == [config.relay_proof_registry_database]

    for database in databases[:-1]:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    RuntimeBootstrap.run(config, proof_registry_bootstrap=prepare_proof_registry)
    assert config.relay_capability_key.read_bytes() == first_key


def test_bootstrap_dependency_failure_is_closed_before_storage_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )

    def unavailable() -> object:
        raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE")

    monkeypatch.setattr(
        "devkit_runtime.bootstrap._load_proof_registry_bootstrap", unavailable
    )
    with pytest.raises(RuntimeConfigError) as caught:
        RuntimeBootstrap.run(config)

    assert caught.value.code == "RUNTIME_DEPENDENCY_UNAVAILABLE"
    assert not data_root.exists()


def test_runtime_root_constructor_is_pure_and_opens_fresh_call_scopes(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    opened: list[object] = []

    def create_uow(*, config: RuntimeConfig, read_only: bool) -> object:
        assert config.data_root == data_root
        assert read_only
        uow = object()
        opened.append(uow)
        return uow

    root = RuntimeRoot(config, uow_factory=create_uow)

    assert not data_root.exists()
    assert root.open_uow(read_only=True) is not root.open_uow(read_only=True)
    assert len(opened) == 2


def test_runtime_root_degrades_optional_providers_and_shuts_them_down(
    tmp_path: Path,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    broker = Provider()
    attestor = Provider()
    root = RuntimeRoot(
        config,
        uow_factory=lambda **_: object(),
        capability_broker=broker,
        integration_attestor=attestor,
    )

    assert root.availability.capability_broker is True
    assert root.availability.integration_attestor is True

    root.shutdown()
    root.shutdown()

    assert broker.closed == 1
    assert attestor.closed == 1
    with pytest.raises(RuntimeConfigError):
        root.open_uow(read_only=True)


def test_runtime_root_reports_absent_optional_providers(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )

    root = RuntimeRoot(config, uow_factory=lambda **_: object())

    assert root.availability.capability_broker is False
    assert root.availability.integration_attestor is False


def test_runtime_root_keeps_private_host_session_lifecycle_outside_uow(
    tmp_path: Path,
) -> None:
    class HostSession:
        is_available = True

        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    session = HostSession()
    root = RuntimeRoot(
        config,
        uow_factory=lambda **_: object(),
        host_session=session,
    )

    assert root.availability.host_session is True
    assert root.open_uow(read_only=True) is not session
    root.shutdown()
    root.shutdown()

    assert session.closed == 1


def test_runtime_root_shutdown_closes_every_provider_before_reraising(
    tmp_path: Path,
) -> None:
    class FailingProvider:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1
            raise RuntimeError("close failure")

    class Provider:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    broker = FailingProvider()
    attestor = Provider()
    root = RuntimeRoot(
        config,
        uow_factory=lambda **_: object(),
        capability_broker=broker,
        integration_attestor=attestor,
    )

    with pytest.raises(RuntimeError, match="close failure"):
        root.shutdown()

    root.shutdown()
    assert broker.closed == 1
    assert attestor.closed == 1


def test_runtime_root_wires_fresh_call_scoped_typed_adapters(tmp_path: Path) -> None:
    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    opened: list[Resource] = []
    relay_inputs: list[tuple[bool, object | None, object | None]] = []
    broker = object()
    attestor = object()

    def resource(name: str) -> Resource:
        value = Resource(name)
        opened.append(value)
        return value

    def open_project_checkpoint(*, config: RuntimeConfig, read_only: bool) -> Resource:
        assert config.data_root == tmp_path / "data"
        assert read_only is True
        return resource("project-checkpoint")

    def open_atlas_store(*, config: RuntimeConfig, read_only: bool) -> Resource:
        assert config.data_root == tmp_path / "data"
        assert read_only is True
        return resource("atlas-store")

    def build_atlas(*, atlas_store: Resource, project_checkpoint: Resource) -> object:
        return ("atlas", atlas_store.name, project_checkpoint.name)

    def build_registry(
        *, atlas_store: Resource, project_checkpoint: Resource
    ) -> object:
        return ("registry", atlas_store.name, project_checkpoint.name)

    def open_relay(
        *,
        config: RuntimeConfig,
        read_only: bool,
        capability_broker: object | None,
        integration_attestor: object | None,
    ) -> Resource:
        assert config.data_root == tmp_path / "data"
        relay_inputs.append((read_only, capability_broker, integration_attestor))
        return resource("relay")

    root = RuntimeRoot(
        config,
        adapter_factories=RuntimeAdapterFactories(
            open_project_checkpoint=open_project_checkpoint,
            open_atlas_store=open_atlas_store,
            build_atlas=build_atlas,
            build_registry=build_registry,
            open_relay=open_relay,
        ),
        capability_broker=broker,
        integration_attestor=attestor,
    )

    assert not config.data_root.exists()
    first = root.open_uow(read_only=True)
    second = root.open_uow(read_only=True)
    assert isinstance(first, RuntimeUnitOfWork)
    assert first.project_checkpoint is not second.project_checkpoint
    assert first.atlas == ("atlas", "atlas-store", "project-checkpoint")
    assert first.registry == ("registry", "atlas-store", "project-checkpoint")
    assert first.relay is not second.relay
    assert relay_inputs == [(True, broker, attestor), (True, broker, attestor)]

    first.close()
    first.close()
    second.close()

    assert all(resource.closed == 1 for resource in opened)
    with pytest.raises(RuntimeConfigError) as caught:
        first.relay
    assert caught.value.code == "RUNTIME_CLOSED"


def test_runtime_root_default_uow_is_lazy_and_pid_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(tmp_path / "data"),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )
    current_pid = [100]
    monkeypatch.setattr("devkit_runtime.composition.os.getpid", lambda: current_pid[0])
    root = RuntimeRoot(config)

    assert isinstance(root.open_uow(read_only=True), RuntimeUnitOfWork)
    assert not config.data_root.exists()

    current_pid[0] = 101
    with pytest.raises(RuntimeConfigError) as caught:
        root.open_uow(read_only=True)
    assert caught.value.code == "RUNTIME_PROCESS_MISMATCH"


def test_default_read_uow_wires_real_adapters_without_durable_read_writes(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )

    def prepare_proof_registry(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_marker (id INTEGER)"
            )

    RuntimeBootstrap.run(config, proof_registry_bootstrap=prepare_proof_registry)
    before = sorted(
        path.relative_to(data_root).as_posix()
        for path in data_root.rglob("*")
        if path.is_file()
    )
    root = RuntimeRoot(config)
    first = root.open_uow(read_only=True)
    second = root.open_uow(read_only=True)
    try:
        assert first.project_checkpoint is not second.project_checkpoint
        assert first.atlas_store.schema_version() == 1
        assert first.atlas is not None
        assert first.registry is not None
        assert first.relay is not second.relay
    finally:
        first.close()
        second.close()

    after = sorted(
        path.relative_to(data_root).as_posix()
        for path in data_root.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_default_write_uow_preserves_relay_zero_write_broker_gate(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    config = RuntimeConfig.load(
        environ={
            "PLUGIN_DATA": str(data_root),
            "CODEX_TASK_TEMP": str(scratch_root),
        }
    )

    def prepare_proof_registry(database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_marker (id INTEGER)"
            )

    RuntimeBootstrap.run(config, proof_registry_bootstrap=prepare_proof_registry)
    with sqlite3.connect(config.relay_database.as_uri() + "?mode=ro", uri=True) as conn:
        before = conn.execute("SELECT COUNT(*) FROM relay_v3_runs").fetchone()

    root = RuntimeRoot(config)
    with root.open_uow(read_only=False) as uow:
        assert uow.project_checkpoint is not None
        assert uow.atlas_store.schema_version() == 1
        with pytest.raises(RelayError) as caught:
            cast(RelayRuntime, uow.relay).start({})

    assert caught.value.code == "RELAY_CAPABILITY_BROKER_UNAVAILABLE"
    with sqlite3.connect(config.relay_database.as_uri() + "?mode=ro", uri=True) as conn:
        after = conn.execute("SELECT COUNT(*) FROM relay_v3_runs").fetchone()
    assert after == before
