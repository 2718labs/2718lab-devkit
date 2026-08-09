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
from orchestrator.store import SQLiteStore, StoreError


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
) -> str:
    def not_null(column: str) -> str:
        return "" if nullable_column == column else " NOT NULL"

    payload_json = (
        f"payload_json TEXT{not_null('payload_json')},"
        if include_payload_json
        else ""
    )
    if partial_unique_indexes:
        acceptance_id = (
            f"acceptance_id TEXT{not_null('acceptance_id')} "
            "REFERENCES code_task_acceptances(acceptance_id),"
        )
        payload_hash = f"payload_hash TEXT{not_null('payload_hash')},"
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
            f"acceptance_id TEXT{not_null('acceptance_id')} UNIQUE "
            "REFERENCES code_task_acceptances(acceptance_id),"
        )
        payload_hash = f"payload_hash TEXT{not_null('payload_hash')} UNIQUE,"
        unique_indexes = ""
    return f"""
        CREATE TABLE atlas_ingestion_outbox (
            ingestion_key TEXT PRIMARY KEY,
            {acceptance_id}
            {payload_json}
            {payload_hash}
            state TEXT{not_null('state')} {state_check},
            attempt_count INTEGER{not_null('attempt_count')} {attempt_check},
            last_error_code TEXT{not_null('last_error_code')},
            reason_codes_json TEXT{not_null('reason_codes_json')},
            created_at TEXT{not_null('created_at')},
            updated_at TEXT{not_null('updated_at')},
            {equality_check}
        );
        {unique_indexes}
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
    timestamp = "2026-08-09T00:00:00Z"
    workflow_id = "legacy-workflow"
    task_id = f"legacy-task-{suffix}"
    acceptance_id = f"sha256:{suffix * 64}"
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
            f"sha256:{'e' * 64}",
            f"sha256:{'f' * 64}",
            f"sha256:{'0' * 64}",
            "legacy",
            "python",
            "pytest",
            "{}",
            acceptance_id,
            timestamp,
        ),
    )
    return acceptance_id, timestamp


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
            """
            INSERT INTO atlas_ingestion_outbox (
                ingestion_key, acceptance_id, payload_json, payload_hash, state,
                attempt_count, last_error_code, reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingestion_key,
                acceptance_id,
                "{}",
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

    store = SQLiteStore(database)
    try:
        assert store.schema_version() == 12
        columns = {
            str(row["name"]): int(row["notnull"])
            for row in store._connection.execute(
                "PRAGMA table_info(atlas_ingestion_outbox)"
            )
        }
        assert columns["ingestion_key"] == 1
        assert tuple(
            store._connection.execute(
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
            store._connection, suffix="b"
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="NOT NULL constraint failed: atlas_ingestion_outbox.ingestion_key",
        ):
            store._connection.execute(
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
        assert store._connection.execute(
            "SELECT COUNT(*) FROM atlas_ingestion_outbox"
        ).fetchone()[0] == 1
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


def _legacy_v10_incomplete_atlas_outbox_database(
    tmp_path: Path,
) -> tuple[Path, str, str]:
    ingestion_key = f"sha256:{'a' * 64}"
    database, acceptance_id, timestamp = _legacy_v10_atlas_outbox_database(
        tmp_path, ingestion_key=ingestion_key
    )
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
        assert store.schema_version() == 12
        assert tuple(
            tuple(row)
            for row in store._connection.execute(
                "SELECT key, value FROM schema_metadata"
            )
        ) == (("schema_version", "12"),)
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
            (("schema_version", "13"),),
            (("schema_version", "13"),),
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
    finally:
        connection.close()
