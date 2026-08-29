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
from orchestrator.store import SQLiteStore, StoreError, _payload_hash


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


def test_runtime_config_load_prefers_explicit_devkit_data_root(
    tmp_path: Path,
) -> None:
    explicit_root = tmp_path / "g-drive-devkit-data"
    legacy_plugin_data = tmp_path / "legacy-plugin-data"
    codex_home = tmp_path / "codex-home"

    config = RuntimeConfig.load(
        environ={
            "CODEX_DEVKIT_DATA_ROOT": str(explicit_root),
            "PLUGIN_DATA": str(legacy_plugin_data),
            "CODEX_HOME": str(codex_home),
        }
    )

    assert config.data_root == explicit_root
    assert not explicit_root.exists()
    assert not legacy_plugin_data.exists()
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


def test_explicit_bootstrap_is_idempotent_and_prepares_expected_journal_stores(
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

    expected_journal_modes = {
        config.orchestrator_database: "delete",
        config.project_index_database: "wal",
        config.atlas_database: "wal",
        config.relay_database: "wal",
    }
    for database, expected_mode in expected_journal_modes.items():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone() == (expected_mode,)

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

    def open_continuity(*, config: RuntimeConfig, read_only: bool) -> Resource:
        raise AssertionError("read-only UoW must not open the continuity writer")

    def build_atlas(
        *,
        atlas_store: Resource,
        project_checkpoint: Resource,
        acceptance_evidence_reader: object | None = None,
    ) -> object:
        assert acceptance_evidence_reader is None
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
            open_continuity=open_continuity,
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


def test_write_uow_lazily_owns_continuity_adapter_and_closes_it_once(
    tmp_path: Path,
) -> None:
    class Resource:
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
    continuity = Resource()
    calls: list[bool] = []

    def open_continuity(*, config: RuntimeConfig, read_only: bool) -> Resource:
        assert config.data_root == tmp_path / "data"
        calls.append(read_only)
        return continuity

    factories = RuntimeAdapterFactories(
        open_project_checkpoint=lambda **_kwargs: Resource(),
        open_atlas_store=lambda **_kwargs: Resource(),
        open_continuity=open_continuity,
        build_atlas=lambda **_kwargs: object(),
        build_registry=lambda **_kwargs: object(),
        open_relay=lambda **_kwargs: Resource(),
    )
    uow = RuntimeUnitOfWork(
        config=config,
        read_only=False,
        factories=factories,
        capability_broker=None,
        integration_attestor=None,
        tool_results=object(),
    )

    assert uow._continuity_service() is continuity  # noqa: SLF001 - ownership seam
    assert uow._continuity_service() is continuity  # noqa: SLF001 - ownership seam
    uow.close()
    uow.close()

    assert calls == [False]
    assert continuity.closed == 1


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


@pytest.mark.parametrize(
    "outbox_schema",
    (
        """
        CREATE TABLE atlas_ingestion_outbox (
            ingestion_key TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            last_error_code TEXT NOT NULL,
            reason_codes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE atlas_ingestion_outbox (
            ingestion_key TEXT NOT NULL,
            acceptance_id TEXT NOT NULL UNIQUE
                REFERENCES code_task_acceptances(acceptance_id),
            payload_hash TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            last_error_code TEXT NOT NULL,
            reason_codes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
    ids=("missing-acceptance-binding", "missing-key-payload-identity"),
)
def test_default_write_uow_rejects_malformed_atlas_outbox_schema(
    tmp_path: Path, outbox_schema: str
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
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE atlas_ingestion_outbox")
        connection.execute(outbox_schema)
        connection.commit()
    finally:
        connection.close()

    root = RuntimeRoot(config)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            with root.open_uow(read_only=False) as uow:
                _ = uow.atlas
    finally:
        root.shutdown()


def _atlas_outbox_schema(
    *,
    include_payload_json: bool = True,
    partial_unique_indexes: bool = False,
    nullable_column: str | None = None,
    equality_check: str = "CHECK (ingestion_key = payload_hash)",
    state_check: str = "CHECK (state IN ('pending', 'projected', 'quarantined'))",
    attempt_check: str = "CHECK (attempt_count BETWEEN 0 AND 16)",
    column_collation: str = "",
) -> str:
    def not_null(column: str) -> str:
        return "" if nullable_column == column else " NOT NULL"

    def text_type(column: str) -> str:
        return f"TEXT{column_collation}{not_null(column)}"

    payload_json = (
        f"payload_json {text_type('payload_json')},"
        if include_payload_json
        else ""
    )
    if partial_unique_indexes:
        acceptance_id = (
            f"acceptance_id {text_type('acceptance_id')} "
            "REFERENCES code_task_acceptances(acceptance_id),"
        )
        payload_hash = f"payload_hash {text_type('payload_hash')},"
        unique_indexes = """
            CREATE UNIQUE INDEX atlas_outbox_acceptance_partial
                ON atlas_ingestion_outbox(acceptance_id)
                WHERE state = 'pending';
            CREATE UNIQUE INDEX atlas_outbox_payload_partial
                ON atlas_ingestion_outbox(payload_hash)
                WHERE state = 'pending';
        """
    else:
        acceptance_id = (
            f"acceptance_id {text_type('acceptance_id')} UNIQUE "
            "REFERENCES code_task_acceptances(acceptance_id),"
        )
        payload_hash = f"payload_hash {text_type('payload_hash')} UNIQUE,"
        unique_indexes = ""
    return f"""
        CREATE TABLE atlas_ingestion_outbox (
            ingestion_key TEXT{column_collation} PRIMARY KEY,
            {acceptance_id}
            {payload_json}
            {payload_hash}
            state {text_type('state')} {state_check},
            attempt_count INTEGER{not_null('attempt_count')} {attempt_check},
            last_error_code {text_type('last_error_code')},
            reason_codes_json {text_type('reason_codes_json')},
            created_at {text_type('created_at')},
            updated_at {text_type('updated_at')},
            {equality_check}
        );
        {unique_indexes}
    """


def _legacy_v6_schema() -> str:
    """Build the complete v6 schema without passing through the current store."""

    return f"""
        CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE workflows (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
            product_summary TEXT NOT NULL, state TEXT NOT NULL,
            version INTEGER NOT NULL, policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
            title TEXT NOT NULL, owner_role TEXT NOT NULL, state TEXT NOT NULL,
            write_scope TEXT NOT NULL, card_hash TEXT NOT NULL,
            result_hash TEXT NOT NULL, version INTEGER NOT NULL,
            task_kind TEXT NOT NULL DEFAULT 'general',
            intent_id TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
            framework TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_tasks_workflow_state ON tasks(workflow_id, state);
        CREATE TABLE code_task_acceptances (
            acceptance_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
            code_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), code_task_version INTEGER NOT NULL,
            input_snapshot_id TEXT NOT NULL, output_snapshot_id TEXT NOT NULL,
            indexed_diff_hash TEXT NOT NULL, intent_id TEXT NOT NULL,
            language TEXT NOT NULL, framework TEXT NOT NULL,
            payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            CHECK (acceptance_id = payload_hash)
        );
        CREATE INDEX idx_code_task_acceptances_workflow
            ON code_task_acceptances(workflow_id, created_at, acceptance_id);
        CREATE TABLE code_task_receipt_attestations (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), workflow_id TEXT NOT NULL REFERENCES workflows(id),
            code_task_version INTEGER NOT NULL, input_snapshot_id TEXT NOT NULL,
            output_snapshot_id TEXT NOT NULL, workspace_hash TEXT NOT NULL,
            execution_receipt_ids TEXT NOT NULL, attestation_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE (task_id, code_task_version, attestation_hash)
        );
        CREATE INDEX idx_code_task_receipt_attestations_workflow
            ON code_task_receipt_attestations(workflow_id, code_task_version, task_id);
        CREATE TABLE code_task_receipt_owners (
            receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            code_task_version INTEGER NOT NULL, attestation_hash TEXT NOT NULL,
            FOREIGN KEY (task_id, code_task_version, attestation_hash)
                REFERENCES code_task_receipt_attestations(task_id, code_task_version, attestation_hash)
        );
        CREATE INDEX idx_code_task_receipt_owners_task
            ON code_task_receipt_owners(task_id, code_task_version, attestation_hash, receipt_id);
        {_atlas_outbox_schema(column_collation=" COLLATE BINARY")}
        CREATE INDEX idx_atlas_outbox_pending
            ON atlas_ingestion_outbox(
                state ASC, created_at ASC, ingestion_key ASC
            );
        CREATE TABLE task_dependencies (
            task_id TEXT NOT NULL REFERENCES tasks(id), dependency_id TEXT NOT NULL REFERENCES tasks(id),
            PRIMARY KEY (task_id, dependency_id)
        );
        CREATE INDEX idx_task_dependencies_dependency ON task_dependencies(dependency_id);
        CREATE TABLE lease_epochs (task_id TEXT PRIMARY KEY REFERENCES tasks(id), epoch INTEGER NOT NULL);
        CREATE TABLE leases (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), owner TEXT NOT NULL,
            epoch INTEGER NOT NULL, expires_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL, host_target TEXT
        );
        CREATE TABLE events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL REFERENCES workflows(id), task_id TEXT REFERENCES tasks(id),
            event_type TEXT NOT NULL, redacted_payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX idx_events_workflow_sequence ON events(workflow_id, sequence);
        CREATE TABLE artifacts (
            content_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, safe_path TEXT NOT NULL,
            size INTEGER NOT NULL, redaction_version TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE task_inputs (task_id TEXT PRIMARY KEY REFERENCES tasks(id), input_hash TEXT NOT NULL);
        CREATE TABLE artifact_owners (content_hash TEXT PRIMARY KEY REFERENCES artifacts(content_hash), task_id TEXT NOT NULL REFERENCES tasks(id));
        CREATE TABLE task_cards (task_id TEXT PRIMARY KEY REFERENCES tasks(id), card_hash TEXT NOT NULL, card_body TEXT NOT NULL);
        CREATE TABLE task_contract_subscriptions (
            task_id TEXT NOT NULL REFERENCES tasks(id), contract_hash TEXT NOT NULL,
            PRIMARY KEY (task_id, contract_hash)
        );
        CREATE TABLE task_required_evidence (
            task_id TEXT NOT NULL REFERENCES tasks(id), position INTEGER NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY (task_id, position)
        );
        CREATE TABLE task_index_bindings (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), workspace_root TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '', input_snapshot_id TEXT NOT NULL,
            output_snapshot_id TEXT NOT NULL, task_node_ids TEXT NOT NULL,
            contract_node_ids TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
            indexed_diff_hash TEXT NOT NULL, fallback_count INTEGER NOT NULL
        );
        CREATE TABLE task_index_query_receipts (
            task_id TEXT NOT NULL REFERENCES tasks(id), trace_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL, miss_escape_used INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (task_id, trace_id)
        );
        CREATE INDEX idx_task_index_query_snapshot ON task_index_query_receipts(task_id, snapshot_id);
        CREATE TABLE task_index_verification_artifacts (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            content_hash TEXT NOT NULL REFERENCES artifacts(content_hash), snapshot_id TEXT NOT NULL,
            PRIMARY KEY (task_id, content_hash)
        );
        CREATE TABLE task_index_binding_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
            event_type TEXT NOT NULL, snapshot_id TEXT NOT NULL,
            trace_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE peer_capabilities (
            workflow_id TEXT NOT NULL REFERENCES workflows(id), sender_task_id TEXT NOT NULL REFERENCES tasks(id),
            recipient_task_id TEXT NOT NULL REFERENCES tasks(id), relationship TEXT NOT NULL,
            capability TEXT NOT NULL UNIQUE,
            PRIMARY KEY (workflow_id, sender_task_id, recipient_task_id, relationship)
        );
        CREATE TABLE messages (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL REFERENCES workflows(id), sender_task_id TEXT NOT NULL REFERENCES tasks(id),
            recipient_task_id TEXT NOT NULL REFERENCES tasks(id), correlation_id TEXT NOT NULL,
            artifact_hash TEXT NOT NULL REFERENCES artifacts(content_hash), redacted_metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL, acknowledged_at TEXT,
            UNIQUE (workflow_id, sender_task_id, recipient_task_id, correlation_id)
        );
        CREATE INDEX idx_messages_recipient_inbox ON messages(workflow_id, recipient_task_id, sequence);
    """


def _runtime_with_malformed_atlas_outbox(
    tmp_path: Path, outbox_schema: str
) -> RuntimeConfig:
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
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE atlas_ingestion_outbox")
        connection.executescript(outbox_schema)
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize(
    "outbox_schema",
    (
        _atlas_outbox_schema(include_payload_json=False),
        _atlas_outbox_schema(partial_unique_indexes=True),
        _atlas_outbox_schema(
            equality_check="CHECK (1) /* CHECK (ingestion_key = payload_hash) */"
        ),
        _atlas_outbox_schema(
            state_check=(
                "CHECK (1) "
                "/* CHECK (state IN ('pending', 'projected', 'quarantined')) */"
            )
        ),
        _atlas_outbox_schema(
            attempt_check=(
                "CHECK (1) /* CHECK (attempt_count BETWEEN 0 AND 16) */"
            )
        ),
    ),
    ids=(
        "missing-payload-json",
        "partial-unique-indexes",
        "comment-forged-key-payload-check",
        "comment-forged-state-check",
        "comment-forged-attempt-check",
    ),
)
def test_default_write_uow_rejects_atlas_outbox_validation_bypasses(
    tmp_path: Path, outbox_schema: str
) -> None:
    config = _runtime_with_malformed_atlas_outbox(tmp_path, outbox_schema)
    root = RuntimeRoot(config)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            with root.open_uow(read_only=False) as uow:
                _ = uow.atlas
    finally:
        root.shutdown()


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_entry_points_reject_malformed_current_outbox_schema(
    tmp_path: Path, entry_point: str
) -> None:
    config = _runtime_with_malformed_atlas_outbox(
        tmp_path, _atlas_outbox_schema(include_payload_json=False)
    )
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
        return

    connection = sqlite3.connect(config.orchestrator_database)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            SQLiteStore.from_prepared_connection(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "nullable_column",
    (
        "ingestion_key",
        "acceptance_id",
        "payload_json",
        "payload_hash",
        "state",
        "attempt_count",
        "last_error_code",
        "reason_codes_json",
        "created_at",
        "updated_at",
    ),
)
def test_default_write_uow_rejects_nullable_atlas_outbox_columns(
    tmp_path: Path, nullable_column: str
) -> None:
    config = _runtime_with_malformed_atlas_outbox(
        tmp_path, _atlas_outbox_schema(nullable_column=nullable_column)
    )
    root = RuntimeRoot(config)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            with root.open_uow(read_only=False) as uow:
                _ = uow.atlas
    finally:
        root.shutdown()


def test_prepared_sqlite_store_enables_foreign_keys_on_raw_connection(
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
    connection = sqlite3.connect(config.orchestrator_database)
    store = SQLiteStore.from_prepared_connection(connection)
    try:
        assert store.foreign_keys_enabled()
    finally:
        store.close()


def _insert_legacy_atlas_acceptance(
    connection: sqlite3.Connection, *, suffix: str
) -> tuple[str, str]:
    timestamp = "2026-08-09T00:00:00+00:00"
    workflow_id = "legacy-workflow"
    task_id = f"legacy-task-{suffix}"
    input_snapshot_id = f"sha256:{'e' * 64}"
    output_snapshot_id = f"sha256:{'f' * 64}"
    indexed_diff_hash = f"sha256:{'0' * 64}"
    payload_json = SQLiteStore._canonical_code_task_acceptance_payload(
        workflow_id=workflow_id,
        task_id=task_id,
        task_version=1,
        input_snapshot_id=input_snapshot_id,
        output_snapshot_id=output_snapshot_id,
        indexed_diff_hash=indexed_diff_hash,
        intent_id="legacy",
        language="python",
        framework="pytest",
    )
    acceptance_id = _payload_hash(payload_json)
    connection.execute(
        """
        INSERT INTO workflows (
            id, kind, title, product_summary, state, version, policy_version,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            workflow_id,
            "general",
            "legacy workflow",
            "legacy outbox migration fixture",
            "active",
            1,
            "legacy",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO tasks (
            id, workflow_id, title, owner_role, state, write_scope, card_hash,
            result_hash, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            workflow_id,
            "legacy task",
            "writer",
            "pending",
            "mcp-tools/orchestrator/store.py",
            f"sha256:{'c' * 64}",
            f"sha256:{'d' * 64}",
            1,
        ),
    )
    connection.execute(
        """
        INSERT INTO code_task_acceptances (
            acceptance_id, workflow_id, code_task_id, code_task_version,
            input_snapshot_id, output_snapshot_id, indexed_diff_hash, intent_id,
            language, framework, payload_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            acceptance_id,
            workflow_id,
            task_id,
            1,
            input_snapshot_id,
            output_snapshot_id,
            indexed_diff_hash,
            "legacy",
            "python",
            "pytest",
            payload_json,
            acceptance_id,
            timestamp,
        ),
    )
    return acceptance_id, timestamp


def _legacy_v6_atlas_outbox_database(
    tmp_path: Path, *, ingestion_key: str | None
) -> tuple[Path, str, str]:
    """Create a complete historical v6 store, without bootstrapping v13 first."""

    database = tmp_path / "legacy-v6-atlas-outbox.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_legacy_v6_schema())
        acceptance_id, timestamp = _insert_legacy_atlas_acceptance(
            connection, suffix="a"
        )
        payload_json = connection.execute(
            "SELECT payload_json FROM code_task_acceptances WHERE acceptance_id = ?",
            (acceptance_id,),
        ).fetchone()[0]
        outbox_ingestion_key = acceptance_id if ingestion_key is not None else None
        connection.execute(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            ("schema_version", "6"),
        )
        connection.execute(
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_json, payload_hash, state,
                attempt_count, last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_ingestion_key,
                acceptance_id,
                payload_json,
                acceptance_id,
                "pending",
                0,
                "",
                "[]",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database, acceptance_id, timestamp


def _legacy_v10_atlas_outbox_database(
    tmp_path: Path, *, ingestion_key: str | None
) -> tuple[Path, str, str]:
    database = tmp_path / "legacy-atlas-outbox.sqlite3"
    bootstrap = SQLiteStore(database)
    bootstrap.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        acceptance_id, timestamp = _insert_legacy_atlas_acceptance(
            connection, suffix="a"
        )
        payload_json = connection.execute(
            "SELECT payload_json FROM code_task_acceptances WHERE acceptance_id = ?",
            (acceptance_id,),
        ).fetchone()[0]
        outbox_ingestion_key = acceptance_id if ingestion_key is not None else None
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            ("schema_version", "10"),
        )
        connection.execute("DROP TABLE atlas_ingestion_outbox")
        connection.executescript(_atlas_outbox_schema())
        connection.execute(
            "CREATE INDEX idx_atlas_outbox_pending "
            "ON atlas_ingestion_outbox(state, created_at, ingestion_key)"
        )
        connection.execute(
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_json, payload_hash, state,
                attempt_count, last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_ingestion_key,
                acceptance_id,
                payload_json,
                acceptance_id,
                "pending",
                0,
                "",
                "[]",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database, acceptance_id, timestamp


def test_sqlite_store_migrates_v10_outbox_to_reject_null_ingestion_keys(
    tmp_path: Path,
) -> None:
    ingestion_key = f"sha256:{'a' * 64}"
    database, acceptance_id, timestamp = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=ingestion_key
    )
    ingestion_key = acceptance_id

    store = SQLiteStore(database)
    try:
        connection = store._connection
        assert connection is not None
        assert store.schema_version() == 13
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in connection.execute("PRAGMA table_info(atlas_ingestion_outbox)")
        }
        assert columns["ingestion_key"] == 1
        projection_guard = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'atlas_finalizations_require_projected_outbox' "
            "AND tbl_name = 'atlas_finalizations'"
        ).fetchone()
        assert projection_guard is not None and projection_guard[0] == 1
        finalization_identity = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_atlas_outbox_finalization_identity'"
        ).fetchone()
        assert finalization_identity is not None and finalization_identity[0] == 1
        assert tuple(
            connection.execute(
                """
                SELECT ingestion_key, acceptance_id, payload_hash, state, attempt_count
                FROM atlas_ingestion_outbox
                """
            ).fetchone()
        ) == (
            ingestion_key,
            acceptance_id,
            acceptance_id,
            "pending",
            0,
        )

        next_acceptance_id, _ = _insert_legacy_atlas_acceptance(
            connection, suffix="b"
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="NOT NULL constraint failed: atlas_ingestion_outbox.ingestion_key",
        ):
            connection.execute(
                """
                INSERT INTO atlas_ingestion_outbox (
                    ingestion_key, acceptance_id, payload_json, payload_hash, state,
                    attempt_count, last_error_code, reason_codes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    None,
                    next_acceptance_id,
                    "{}",
                    next_acceptance_id,
                    "pending",
                    0,
                    "",
                    "[]",
                    timestamp,
                    timestamp,
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize("missing_object", ("table", "pending-index"))
def test_sqlite_store_rejects_legacy_v6_outbox_drift_before_current_ddl(
    tmp_path: Path, missing_object: str
) -> None:
    database, _, _ = _legacy_v6_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        if missing_object == "table":
            connection.execute("DROP TABLE atlas_ingestion_outbox")
        else:
            connection.execute("DROP INDEX idx_atlas_outbox_pending")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreError, match="orchestrator store is not prepared"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "6"
        if missing_object == "table":
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'atlas_ingestion_outbox'"
            ).fetchone() is None
        else:
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_atlas_outbox_pending'"
            ).fetchone() is None
    finally:
        connection.close()


def test_sqlite_store_bootstraps_true_legacy_v6_empty_outbox(
    tmp_path: Path,
) -> None:
    database, _, _ = _legacy_v6_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM atlas_ingestion_outbox")
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("6",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'atlas_finalizations'"
        ).fetchone() is None
        connection.commit()
    finally:
        connection.close()

    store = SQLiteStore(database)
    try:
        assert store.schema_version() == 13
        assert store._connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0] == 0
        assert store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'atlas_finalizations'"
        ).fetchone() is not None
    finally:
        store.close()


@pytest.mark.parametrize(
    "semantic_drift", ("column-nocase", "pending-nocase-desc")
)
def test_sqlite_store_rejects_legacy_v6_semantic_shape_drift(
    tmp_path: Path, semantic_drift: str
) -> None:
    database, _, _ = _legacy_v6_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        if semantic_drift == "column-nocase":
            connection.execute("DROP TABLE atlas_ingestion_outbox")
            connection.executescript(
                _atlas_outbox_schema(column_collation=" COLLATE NOCASE")
            )
            connection.execute(
                "CREATE INDEX idx_atlas_outbox_pending "
                "ON atlas_ingestion_outbox(state ASC, created_at ASC, ingestion_key ASC)"
            )
        else:
            connection.execute("DROP INDEX idx_atlas_outbox_pending")
            connection.execute(
                "CREATE INDEX idx_atlas_outbox_pending "
                "ON atlas_ingestion_outbox("
                "state COLLATE NOCASE DESC, created_at ASC, ingestion_key ASC)"
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreError, match="orchestrator store is not prepared"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("6",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "outbox_update",
    (
        {"state": "projected", "attempt_count": 0, "last_error_code": "ERR"},
        {"state": "quarantined", "attempt_count": 0, "last_error_code": ""},
        {"state": "pending", "attempt_count": 1, "last_error_code": ""},
        {"state": "pending", "attempt_count": 0, "last_error_code": "ERR"},
        {
            "state": "pending",
            "attempt_count": 0,
            "last_error_code": "",
            "created_at": "2026-08-09T00:00:00+08:00",
            "updated_at": "2026-08-09T00:00:00+08:00",
        },
    ),
    ids=(
        "projected-error",
        "quarantined-missing-error",
        "pending-retry-missing-error",
        "pending-initial-error",
        "non-utc-timestamp",
    ),
)
def test_sqlite_store_rejects_legacy_atlas_outbox_row_contract_drift(
    tmp_path: Path, outbox_update: dict[str, object]
) -> None:
    database, _, _ = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        assignments = {
            "state": outbox_update.get("state", "pending"),
            "attempt_count": outbox_update.get("attempt_count", 0),
            "last_error_code": outbox_update.get("last_error_code", ""),
            "created_at": outbox_update.get(
                "created_at", "2026-08-09T00:00:00+00:00"
            ),
            "updated_at": outbox_update.get(
                "updated_at", "2026-08-09T00:00:00+00:00"
            ),
        }
        connection.execute(
            """
            UPDATE atlas_ingestion_outbox
            SET state = ?, attempt_count = ?, last_error_code = ?,
                created_at = ?, updated_at = ?
            """,
            tuple(assignments.values()),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreError, match="legacy atlas outbox row is invalid"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "10"
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute(
                "PRAGMA table_info(atlas_ingestion_outbox)"
            ).fetchall()
        }
        assert columns["ingestion_key"] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("binding_drift", ("identity", "payload-both"))
def test_sqlite_store_rejects_legacy_outbox_acceptance_binding_drift(
    tmp_path: Path, binding_drift: str
) -> None:
    database, acceptance_id, _ = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        if binding_drift == "identity":
            other_acceptance_id, _ = _insert_legacy_atlas_acceptance(
                connection, suffix="b"
            )
            connection.execute(
                "UPDATE atlas_ingestion_outbox SET acceptance_id = ?",
                (other_acceptance_id,),
            )
        else:
            connection.execute(
                "UPDATE code_task_acceptances SET payload_json = ? "
                "WHERE acceptance_id = ?",
                ('{"different":true}', acceptance_id),
            )
            connection.execute(
                "UPDATE atlas_ingestion_outbox SET payload_json = ?",
                ('{"different":true}',),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreError, match="legacy atlas outbox row is invalid"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_sqlite_store_bootstraps_verified_legacy_empty_outbox(
    tmp_path: Path,
) -> None:
    database, _, _ = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=f"sha256:{'a' * 64}"
    )
    connection = sqlite3.connect(database)
    try:
        for trigger_name in (
            "atlas_finalizations_no_update",
            "atlas_finalizations_no_delete",
            "atlas_finalizations_require_projected_outbox",
        ):
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute("DROP TABLE atlas_finalizations")
        connection.execute("DELETE FROM atlas_ingestion_outbox")
        connection.commit()
    finally:
        connection.close()

    store = SQLiteStore(database)
    try:
        assert store.schema_version() == 13
        assert store._connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_sqlite_store_fails_closed_for_legacy_null_outbox_key(tmp_path: Path) -> None:
    database, _, _ = _legacy_v10_atlas_outbox_database(tmp_path, ingestion_key=None)

    with pytest.raises(StoreError, match="legacy atlas outbox row is invalid"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT ingestion_key FROM atlas_ingestion_outbox"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "10"
    finally:
        connection.close()


def test_sqlite_store_fails_closed_for_noncanonical_finalization_guard_during_v10_outbox_rebuild(
    tmp_path: Path,
) -> None:
    ingestion_key = f"sha256:{'a' * 64}"
    database, _, _ = _legacy_v10_atlas_outbox_database(
        tmp_path,
        ingestion_key=ingestion_key,
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "DROP TRIGGER atlas_finalizations_require_projected_outbox"
        )
        connection.execute(
            """
            CREATE TRIGGER atlas_finalizations_require_projected_outbox
            BEFORE INSERT ON atlas_finalizations
            BEGIN
                SELECT 1;
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StoreError, match="orchestrator store is not prepared"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("10",)
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(atlas_ingestion_outbox)")
        }
        assert columns["ingestion_key"] == 0
    finally:
        connection.close()


def _legacy_v10_incomplete_atlas_outbox_database(
    tmp_path: Path,
) -> tuple[Path, str, str]:
    ingestion_key = f"sha256:{'a' * 64}"
    database, acceptance_id, timestamp = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=ingestion_key
    )
    ingestion_key = acceptance_id
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE atlas_ingestion_outbox")
        connection.executescript(_atlas_outbox_schema(include_payload_json=False))
        connection.execute(
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_hash, state, attempt_count,
                last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingestion_key,
                acceptance_id,
                acceptance_id,
                "pending",
                0,
                "",
                "[]",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database, ingestion_key, acceptance_id


def test_sqlite_store_rolls_back_incomplete_v10_outbox_upgrade(
    tmp_path: Path,
) -> None:
    database, ingestion_key, acceptance_id = _legacy_v10_incomplete_atlas_outbox_database(
        tmp_path
    )

    with pytest.raises(StoreError):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in connection.execute("PRAGMA table_info(atlas_ingestion_outbox)")
        }
        assert "payload_json" not in columns
        assert columns["ingestion_key"] == 0
        metadata_columns = {
            str(row["name"]): int(row["notnull"])
            for row in connection.execute("PRAGMA table_info(schema_metadata)")
        }
        assert metadata_columns["key"] == 0
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_metadata_v12'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "10"
        assert tuple(
            connection.execute(
                """
                SELECT ingestion_key, acceptance_id, payload_hash, state, attempt_count
                FROM atlas_ingestion_outbox
                """
            ).fetchone()
        ) == (
            ingestion_key,
            acceptance_id,
            acceptance_id,
            "pending",
            0,
        )
    finally:
        connection.close()


def _malformed_v11_nullable_atlas_outbox_database(
    tmp_path: Path,
) -> tuple[Path, str, str]:
    ingestion_key = f"sha256:{'a' * 64}"
    database, acceptance_id, _ = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=ingestion_key
    )
    ingestion_key = acceptance_id
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            ("11",),
        )
        connection.commit()
    finally:
        connection.close()
    return database, ingestion_key, acceptance_id


def test_sqlite_store_preserves_malformed_v11_nullable_outbox(
    tmp_path: Path,
) -> None:
    database, ingestion_key, acceptance_id = _malformed_v11_nullable_atlas_outbox_database(
        tmp_path
    )

    with pytest.raises(StoreError):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in connection.execute("PRAGMA table_info(atlas_ingestion_outbox)")
        }
        assert columns["ingestion_key"] == 0
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "11"
        assert tuple(
            connection.execute(
                """
                SELECT ingestion_key, acceptance_id, payload_hash, state, attempt_count
                FROM atlas_ingestion_outbox
                """
            ).fetchone()
        ) == (
            ingestion_key,
            acceptance_id,
            acceptance_id,
            "pending",
            0,
        )
    finally:
        connection.close()


def _bootstrapped_runtime_config(tmp_path: Path) -> RuntimeConfig:
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
    return config


def test_runtime_bootstrap_prepares_private_continuity_storage(tmp_path: Path) -> None:
    config = _bootstrapped_runtime_config(tmp_path)
    assert config.continuity_database.exists()
    assert config.continuity_cas_root.is_dir()


def _runtime_with_orphaned_atlas_outbox(tmp_path: Path) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    ingestion_key = f"sha256:{'a' * 64}"
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        assert not connection.execute("PRAGMA foreign_keys").fetchone()[0]
        connection.execute(
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_json, payload_hash, state,
                attempt_count, last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingestion_key,
                f"sha256:{'b' * 64}",
                "{}",
                ingestion_key,
                "pending",
                0,
                "",
                "[]",
                "2026-08-09T00:00:00Z",
                "2026-08-09T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_rejects_orphaned_atlas_outbox_foreign_key(
    tmp_path: Path, entry_point: str
) -> None:
    config = _runtime_with_orphaned_atlas_outbox(tmp_path)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
        return

    connection = sqlite3.connect(config.orchestrator_database)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore.from_prepared_connection(connection)
            store.close()
    finally:
        connection.close()


def _runtime_with_noncanonical_schema_metadata(tmp_path: Path) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute("CREATE TABLE schema_metadata (key TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            (("schema_version", "11"), ("schema_version", "10")),
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_rejects_noncanonical_schema_metadata(
    tmp_path: Path, entry_point: str
) -> None:
    config = _runtime_with_noncanonical_schema_metadata(tmp_path)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
        return

    connection = sqlite3.connect(config.orchestrator_database)
    try:
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore.from_prepared_connection(connection)
            store.close()
    finally:
        connection.close()


def _runtime_with_null_schema_metadata_key(tmp_path: Path) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            (("schema_version", "11"), (None, "invalid-null-key")),
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_rejects_null_schema_metadata_key(
    tmp_path: Path, entry_point: str
) -> None:
    config = _runtime_with_null_schema_metadata_key(tmp_path)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
    else:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            with pytest.raises(StoreError, match="orchestrator store is not prepared"):
                store = SQLiteStore.from_prepared_connection(connection)
                store.close()
        finally:
            connection.close()

    connection = sqlite3.connect(config.orchestrator_database)
    try:
        columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(schema_metadata)")
        }
        assert columns["key"] == 0
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key IS NULL"
        ).fetchone()[0] == "invalid-null-key"
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "11"
    finally:
        connection.close()


def _legacy_metadata_database(
    tmp_path: Path,
    *,
    version: str,
    rows: tuple[tuple[str, str], ...] | None = None,
) -> Path:
    database = tmp_path / f"legacy-schema-metadata-{version}.sqlite3"
    bootstrap = SQLiteStore(database)
    bootstrap.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        metadata_rows = (("schema_version", version),) if rows is None else rows
        connection.executemany(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            metadata_rows,
        )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.mark.parametrize("legacy_version", ("10", "11"))
def test_sqlite_store_migrates_trustworthy_legacy_schema_metadata(
    tmp_path: Path, legacy_version: str
) -> None:
    database = _legacy_metadata_database(tmp_path, version=legacy_version)

    store = SQLiteStore(database)
    try:
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in store._connection.execute("PRAGMA table_info(schema_metadata)")
        }
        assert columns["key"] == 1
        assert store.schema_version() == 13
        assert tuple(
            tuple(row)
            for row in store._connection.execute(
                "SELECT key, value FROM schema_metadata"
            )
        ) == (("schema_version", "13"),)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("rows", "expected_rows"),
    (
        ((), ()),
        (
            (("schema_version", "11"), ("legacy_marker", "preserved")),
            (("legacy_marker", "preserved"), ("schema_version", "11")),
        ),
    ),
    ids=("empty", "extra-row"),
)
def test_sqlite_store_rejects_untrusted_legacy_schema_metadata(
    tmp_path: Path,
    rows: tuple[tuple[str, str], ...],
    expected_rows: tuple[tuple[str, str], ...],
) -> None:
    database = _legacy_metadata_database(tmp_path, version="11", rows=rows)

    with pytest.raises(StoreError, match="orchestrator store is not prepared"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(schema_metadata)")
        }
        assert columns["key"] == 0
        assert tuple(
            connection.execute(
                "SELECT key, value FROM schema_metadata ORDER BY key, value"
            )
        ) == expected_rows
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_metadata_v12'"
        ).fetchone() is None
    finally:
        connection.close()


def _runtime_with_strict_schema_metadata_rows(
    tmp_path: Path,
    rows: tuple[tuple[str, str], ...],
) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DELETE FROM schema_metadata")
        connection.executemany(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
@pytest.mark.parametrize(
    ("rows", "expected_rows"),
    (
        (
            (("schema_version", "11"),),
            (("schema_version", "11"),),
        ),
        (
            (("schema_version", "14"),),
            (("schema_version", "14"),),
        ),
        ((), ()),
        (
            (("schema_version", "12"), ("legacy_marker", "preserved")),
            (("legacy_marker", "preserved"), ("schema_version", "12")),
        ),
    ),
    ids=("legacy-version", "future-version", "empty", "extra-row"),
)
def test_sqlite_store_rejects_noncanonical_strict_schema_metadata(
    tmp_path: Path,
    entry_point: str,
    rows: tuple[tuple[str, str], ...],
    expected_rows: tuple[tuple[str, str], ...],
) -> None:
    config = _runtime_with_strict_schema_metadata_rows(tmp_path, rows)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
    else:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            with pytest.raises(StoreError, match="orchestrator store is not prepared"):
                store = SQLiteStore.from_prepared_connection(connection)
                store.close()
        finally:
            connection.close()

    connection = sqlite3.connect(config.orchestrator_database)
    try:
        columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(schema_metadata)")
        }
        assert columns["key"] == 1
        assert tuple(
            connection.execute(
                "SELECT key, value FROM schema_metadata ORDER BY key, value"
            )
        ) == expected_rows
    finally:
        connection.close()


def _runtime_with_generated_schema_metadata_column(tmp_path: Path) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL,
                generated_marker TEXT GENERATED ALWAYS AS (key || value) VIRTUAL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            ("schema_version", "12"),
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_rejects_generated_schema_metadata_column(
    tmp_path: Path,
    entry_point: str,
) -> None:
    config = _runtime_with_generated_schema_metadata_column(tmp_path)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
    else:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            with pytest.raises(StoreError, match="orchestrator store is not prepared"):
                store = SQLiteStore.from_prepared_connection(connection)
                store.close()
        finally:
            connection.close()

    connection = sqlite3.connect(config.orchestrator_database)
    connection.row_factory = sqlite3.Row
    try:
        assert {
            str(row["name"])
            for row in connection.execute("PRAGMA table_xinfo(schema_metadata)")
        } == {"key", "value", "generated_marker"}
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()["value"] == "12"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_metadata_v12'"
        ).fetchone() is None
    finally:
        connection.close()


def _runtime_with_generated_schema_metadata_value(tmp_path: Path) -> RuntimeConfig:
    config = _bootstrapped_runtime_config(tmp_path)
    connection = sqlite3.connect(config.orchestrator_database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT GENERATED ALWAYS AS (
                    CASE key WHEN 'schema_version' THEN '12' ELSE '' END
                ) STORED NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_metadata (key) VALUES (?)",
            ("schema_version",),
        )
        connection.commit()
    finally:
        connection.close()
    return config


@pytest.mark.parametrize("entry_point", ("constructor", "prepared"))
def test_sqlite_store_rejects_generated_schema_metadata_value(
    tmp_path: Path,
    entry_point: str,
) -> None:
    config = _runtime_with_generated_schema_metadata_value(tmp_path)
    if entry_point == "constructor":
        with pytest.raises(StoreError, match="orchestrator store is not prepared"):
            store = SQLiteStore(config.orchestrator_database)
            store.close()
    else:
        connection = sqlite3.connect(config.orchestrator_database)
        try:
            with pytest.raises(StoreError, match="orchestrator store is not prepared"):
                store = SQLiteStore.from_prepared_connection(connection)
                store.close()
        finally:
            connection.close()

    connection = sqlite3.connect(config.orchestrator_database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"]): (int(row["notnull"]), int(row["hidden"]))
            for row in connection.execute("PRAGMA table_xinfo(schema_metadata)")
        }
        assert columns == {"key": (1, 0), "value": (1, 3)}
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()["value"] == "12"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_metadata_v12'"
        ).fetchone() is None
    finally:
        connection.close()


def _legacy_database_with_generated_schema_metadata_value(tmp_path: Path) -> Path:
    database = tmp_path / "legacy-generated-schema-metadata-value.sqlite3"
    bootstrap = SQLiteStore(database)
    bootstrap.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE schema_metadata")
        connection.execute(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT GENERATED ALWAYS AS (
                    CASE key WHEN 'schema_version' THEN '11' ELSE '' END
                ) STORED NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_metadata (key) VALUES (?)",
            ("schema_version",),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def test_sqlite_store_rejects_legacy_generated_schema_metadata_value(
    tmp_path: Path,
) -> None:
    database = _legacy_database_with_generated_schema_metadata_value(tmp_path)

    with pytest.raises(StoreError, match="orchestrator store is not prepared"):
        SQLiteStore(database)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"]): (int(row["notnull"]), int(row["hidden"]))
            for row in connection.execute("PRAGMA table_xinfo(schema_metadata)")
        }
        assert columns == {"key": (0, 0), "value": (1, 3)}
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()["value"] == "11"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_metadata_v12'"
        ).fetchone() is None
    finally:
        connection.close()
