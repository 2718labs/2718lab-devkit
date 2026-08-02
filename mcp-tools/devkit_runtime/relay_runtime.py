"""Production Relay runtime adapters with explicit private host boundaries."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from devkit_atlas.store import StoreConflictError
from devkit_relay.compiler import RelayPlanError, compile_plan
from devkit_relay.proofs import IntegrationProofResolver
from devkit_relay.service import RelayError, RelayService
from devkit_relay.store import RelayStore, RelayStoreError
from project_index.models import IndexError as ProjectIndexError

from .host_bridge import HostBridgeError
from .sqlite_snapshot import (
    SqliteSnapshotError,
    VerifiedSqliteSnapshot,
    open_verified_sqlite_snapshot,
)

_WORKER_CAPABILITY_ACTIONS = (
    "bind_endpoint",
    "heartbeat",
    "evidence",
    "terminal",
    "candidate_handoff",
)


class RelayRuntimeError(RuntimeError):
    """Stable runtime adapter failure that does not disclose host state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CapabilityBroker(Protocol):
    """Private broker surface used by the Relay runtime only."""

    @property
    def is_available(self) -> bool: ...

    def prepare_capability(
        self,
        *,
        action_id: str,
        endpoint: str,
        capabilities: Mapping[str, str],
    ) -> object: ...


class ProductionRegistryResolver:
    """Bind Relay compilation to current Project Index and Atlas truth."""

    def __init__(self, project_index: object, atlas_store: object) -> None:
        self._project_index = project_index
        self._atlas_store = atlas_store

    def resolve(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        input_snapshot_id: str,
        atlas_packet_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """Return only the compiler's exact current registry binding."""

        try:
            assert_current = getattr(self._project_index, "assert_current")
            if not callable(assert_current):
                raise TypeError
            assert_current(workspace_id, input_snapshot_id)
        except ProjectIndexError as error:
            if error.code == "INDEX_STALE":
                raise RelayPlanError("registry_binding_stale") from None
            if error.code == "INDEX_CORRUPT":
                raise RelayPlanError("registry_binding_corrupt") from None
            raise RelayPlanError("registry_binding_unavailable") from None
        except Exception as error:
            raise RelayPlanError("registry_binding_unavailable") from error

        try:
            get_packet_verified = getattr(self._atlas_store, "get_packet_verified")
            if not callable(get_packet_verified):
                raise TypeError
            for packet_id in atlas_packet_ids:
                packet = get_packet_verified(packet_id)
                if packet is None:
                    raise RelayPlanError("registry_binding_stale")
                if (
                    getattr(packet, "workspace_id", None) != workspace_id
                    or getattr(packet, "snapshot_id", None) != input_snapshot_id
                ):
                    raise RelayPlanError("registry_binding_stale")
        except RelayPlanError:
            raise
        except StoreConflictError as error:
            raise RelayPlanError("registry_binding_corrupt") from error
        except Exception as error:
            raise RelayPlanError("registry_binding_unavailable") from error

        return {
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
            "input_snapshot_id": input_snapshot_id,
            "atlas_packet_ids": list(atlas_packet_ids),
            "current": True,
        }


class RelayCapabilitySecretProvider:
    """Read one pre-bootstraped binary Relay signing key without creating it."""

    def __init__(self, key_path: str | Path) -> None:
        self._key_path = Path(key_path)

    def load(self) -> bytes:
        """Return the exact 32-byte secret or fail closed without path disclosure."""

        try:
            metadata = self._key_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            secret = self._key_path.read_bytes()
        except (OSError, ValueError) as error:
            raise RelayRuntimeError("RELAY_CAPABILITY_INVALID") from error
        if len(secret) != 32:
            raise RelayRuntimeError("RELAY_CAPABILITY_INVALID")
        return secret


@dataclass
class RelayReadRuntime:
    """One invocation-owned Relay status reader from a verified SQLite copy."""

    _store: RelayStore
    _snapshot: VerifiedSqliteSnapshot
    _closed: bool = False

    def status(self, workflow_id: str) -> dict[str, object]:
        try:
            return self._store.status(workflow_id)
        except RelayStoreError as error:
            raise RelayRuntimeError(error.code) from None
        except KeyError as error:
            raise RelayRuntimeError("RELAY_REQUEST_INVALID") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._store.close()
        finally:
            self._snapshot.close()

    def __enter__(self) -> RelayReadRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def open_relay_ro(
    database_path: str | Path, *, scratch_root: str | Path | None = None
) -> RelayReadRuntime:
    """Open Relay status from a verified read-only DB/WAL snapshot only."""

    snapshot: VerifiedSqliteSnapshot | None = None
    store: RelayStore | None = None
    try:
        snapshot = open_verified_sqlite_snapshot(
            database_path, scratch_root=scratch_root
        )
        connection = snapshot.connect()
        connection.row_factory = sqlite3.Row
        store = RelayStore.__new__(RelayStore)
        store._connection = connection
        store._assert_schema_compatible()
        store._assert_schema_shape()
        return RelayReadRuntime(_store=store, _snapshot=snapshot)
    except (SqliteSnapshotError, RelayStoreError, sqlite3.Error, OSError) as error:
        if store is not None:
            store.close()
        if snapshot is not None:
            snapshot.close()
        if isinstance(error, RelayStoreError):
            raise RelayRuntimeError(error.code) from None
        raise RelayRuntimeError("RELAY_STORAGE_ERROR") from error
    except Exception:
        if store is not None:
            store.close()
        if snapshot is not None:
            snapshot.close()
        raise


class RelayRuntime:
    """Start Relay only after a private broker can receive capability bearers."""

    def __init__(
        self, relay_service: RelayService, *, capability_broker: CapabilityBroker | None
    ) -> None:
        self._relay_service = relay_service
        self._capability_broker = capability_broker

    @classmethod
    def from_secret_provider(
        cls,
        store: RelayStore,
        *,
        capability_secret_provider: RelayCapabilitySecretProvider,
        capability_broker: CapabilityBroker | None,
        integration_proof_resolver: IntegrationProofResolver | None = None,
    ) -> RelayRuntime:
        """Construct a Relay service from a pre-existing capability key only."""

        return cls(
            RelayService(
                store,
                capability_secret=capability_secret_provider.load(),
                integration_proof_resolver=integration_proof_resolver,
            ),
            capability_broker=capability_broker,
        )

    def compile(
        self, request: Mapping[str, object], *, registry: ProductionRegistryResolver
    ) -> dict[str, object]:
        """Compile through the production registry and preserve Relay errors."""

        try:
            return compile_plan(request, registry_resolver=registry)
        except RelayPlanError as error:
            raise RelayError(_relay_plan_error_code(error.code)) from None

    def start(self, request: Mapping[str, object]) -> dict[str, object]:
        """Gate all Relay writes on broker availability, then deliver privately."""

        broker = self._available_broker()
        if broker is None:
            raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE")
        result = self._relay_service.start(request)
        try:
            actions = result["host_actions"]
            if type(actions) is not list:
                raise TypeError
            for action in actions:
                self._deliver_worker_capabilities(broker, action)
        except RelayError:
            raise
        except (HostBridgeError, TypeError, ValueError, KeyError):
            raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE") from None
        except Exception:
            raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE") from None
        return result

    def status(self, workflow_id: object) -> dict[str, object]:
        """Delegate status without requiring a private broker."""

        return self._relay_service.status(workflow_id)

    def _available_broker(self) -> CapabilityBroker | None:
        broker = self._capability_broker
        if broker is None:
            return None
        try:
            return broker if broker.is_available is True else None
        except Exception:
            return None

    def _deliver_worker_capabilities(
        self, broker: CapabilityBroker, action: object
    ) -> None:
        if type(action) is not dict:
            raise TypeError
        action_id = action.get("action_id")
        workflow_id = action.get("workflow_id")
        task_id = action.get("task_id")
        lease = action.get("lease")
        if (
            type(action_id) is not str
            or type(workflow_id) is not str
            or type(task_id) is not str
            or type(lease) is not dict
            or type(lease.get("epoch")) is not int
        ):
            raise TypeError
        endpoint = f"bridge/{action_id}"
        capabilities = {
            lifecycle_action: self._relay_service.issue_worker_capability(
                workflow_id=workflow_id,
                task_id=task_id,
                action=lifecycle_action,
                epoch=lease["epoch"],
                endpoint=endpoint,
            )
            for lifecycle_action in _WORKER_CAPABILITY_ACTIONS
        }
        broker.prepare_capability(
            action_id=action_id, endpoint=endpoint, capabilities=capabilities
        )


def _relay_plan_error_code(code: str) -> str:
    if code == "registry_binding_stale":
        return "RELAY_PLAN_STALE"
    if code == "registry_binding_unavailable":
        return "RELAY_PLAN_STALE"
    if code == "registry_binding_corrupt":
        return "RELAY_PLAN_INVALID"
    return "RELAY_PLAN_INVALID"
