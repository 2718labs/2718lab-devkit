"""Explicit one-time preparation for runtime-owned durable storage."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path

from .config import RuntimeConfig, RuntimeConfigError

_ProofRegistryBootstrap = Callable[[Path], None]


class RuntimeBootstrap:
    """Create and migrate runtime storage only when explicitly requested."""

    @staticmethod
    def run(
        config: RuntimeConfig,
        *,
        proof_registry_bootstrap: _ProofRegistryBootstrap | None = None,
    ) -> None:
        """Prepare every durable store and key without retaining connections."""

        bootstrap_proof_registry = (
            _load_proof_registry_bootstrap()
            if proof_registry_bootstrap is None
            else proof_registry_bootstrap
        )
        if not callable(bootstrap_proof_registry):
            raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE")
        try:
            config.data_root.mkdir(parents=True, exist_ok=True)
            config.scratch_root.mkdir(parents=True, exist_ok=True)
            _bootstrap_stores(config, bootstrap_proof_registry)
            _ensure_secret_key(config.relay_capability_key)
        except RuntimeConfigError:
            raise
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise RuntimeConfigError("DATA_ROOT_UNAVAILABLE") from error


def _load_proof_registry_bootstrap() -> _ProofRegistryBootstrap:
    """Resolve the upstream finalizer lazily so an interface break is closed."""

    try:
        from .relay_proof_registry import bootstrap_relay_proof_registry
    except (ImportError, AttributeError) as error:
        raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE") from error
    if not callable(bootstrap_relay_proof_registry):
        raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE")
    return bootstrap_relay_proof_registry


def _bootstrap_stores(
    config: RuntimeConfig, proof_registry_bootstrap: _ProofRegistryBootstrap
) -> None:
    from devkit_atlas.receipts import ReceiptRepository
    from devkit_atlas.store import AtlasStore
    from devkit_continuity.store import _bootstrap_store
    from devkit_relay.store import RelayStore
    from orchestrator.store import SQLiteStore
    from project_index.checkpoints import CheckpointService
    from project_index.service import ProjectIndexService

    orchestrator: SQLiteStore | None = None
    project_index: ProjectIndexService | None = None
    checkpoints: CheckpointService | None = None
    atlas: AtlasStore | None = None
    relay: RelayStore | None = None
    continuity = None
    try:
        orchestrator = SQLiteStore(config.orchestrator_database)
        project_index = ProjectIndexService(config.project_index_database)
        checkpoints = CheckpointService(
            config.project_index_database,
            config.checkpoint_cas_root,
            project_index,
            workspace_authority=project_index.workspace_authority,
        )
        atlas = AtlasStore(config.atlas_database)
        relay = RelayStore(config.relay_database)
        continuity = _bootstrap_store(
            config.continuity_database, config.continuity_cas_root, config.scratch_root
        )
        proof_registry_bootstrap(config.relay_proof_registry_database)
        ReceiptRepository(config.data_root)._load_or_create_evidence_key()
    finally:
        for resource in (continuity, relay, atlas, checkpoints, project_index, orchestrator):
            if resource is not None:
                resource.close()


def _ensure_secret_key(path: Path) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if len(existing) != 32:
            raise RuntimeConfigError("DATA_ROOT_INVALID")
        return

    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(descriptor, secrets.token_bytes(32))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
