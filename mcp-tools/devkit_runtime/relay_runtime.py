"""Production Relay runtime adapters with explicit private host boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
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
_HASH_PREFIX = "sha256:"
_BOOTSTRAP_ATTESTATION_SCHEMA = (
    "2718lab-devkit/new-project-bootstrap-attestation-v1"
)
_PROJECT_BINDING_SCHEMA = "2718lab-devkit/project-binding-v1"
_BOOTSTRAP_REGISTRY_BINDING_SCHEMA = (
    "2718lab-devkit/project-registry-bootstrap-binding-v1"
)
_BOOTSTRAP_RECEIPT_SCHEMA = "2718lab-devkit/project-index-bootstrap-receipt-v1"
_PROJECT_BINDING_KEYS = frozenset(
    {
        "schema",
        "mode",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "attestation",
        "binding_hash",
    }
)
_BOOTSTRAP_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "initial_manifest_hash",
        "initial_entry_count",
        "state",
        "capability_epoch",
        "capability_hash",
        "attested_input_snapshot_id",
        "issued_at",
        "expires_at",
        "attestation_hash",
    }
)
_BOOTSTRAP_REGISTRY_BINDING_KEYS = frozenset(
    {
        "schema",
        "mode",
        "bootstrap_only",
        "workflow_id",
        "workspace_id",
        "repository_id",
        "project_id",
        "bootstrap_root_identity",
        "initial_manifest_hash",
        "initial_entry_count",
        "capability_epoch",
        "capability_hash",
        "attested_input_snapshot_id",
        "issued_at",
        "expires_at",
        "attestation_hash",
        "binding_hash",
    }
)
_BOOTSTRAP_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "attestation_hash",
        "workspace_id",
        "attested_input_snapshot_id",
        "initial_manifest_hash",
        "index_snapshot_id",
        "index_identity",
        "issued_at",
        "expires_at",
        "receipt_hash",
    }
)

BootstrapCapabilityVerifier = Callable[[Mapping[str, object]], bool]


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


class HostActionAdmission(Protocol):
    """Host-private action gate with its own live physical-slot authority."""

    @property
    def is_available(self) -> bool: ...

    def admit_relay_actions(self, actions: object) -> bool: ...


class ProjectIndexBootstrapHostOperations(Protocol):
    """Private host operations used only after a bootstrap binding is verified."""

    def project_index_register(
        self, *, bootstrap_root_identity: str, attestation_hash: str
    ) -> Mapping[str, object]: ...

    def project_index_sync(
        self, *, workspace_id: str, attestation_hash: str
    ) -> Mapping[str, object]: ...


class ProductionRegistryResolver:
    """Bind Relay compilation to current Project Index and Atlas truth."""

    def __init__(
        self,
        project_index: object,
        atlas_store: object,
        *,
        clock: Callable[[], float] = time.time,
        bootstrap_capability_verifier: BootstrapCapabilityVerifier | None = None,
    ) -> None:
        self._project_index = project_index
        self._atlas_store = atlas_store
        self._clock = clock
        self._bootstrap_capability_verifier = bootstrap_capability_verifier

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

    def resolve_new_empty_bootstrap(
        self, project_binding: Mapping[str, object]
    ) -> dict[str, object]:
        """Return a bootstrap-only binding for one current host-attested empty root."""

        binding = _exact_mapping(
            project_binding,
            _PROJECT_BINDING_KEYS,
            code="BOOTSTRAP_ATTESTATION_INVALID",
        )
        if (
            binding["schema"] != _PROJECT_BINDING_SCHEMA
            or binding["mode"] != "new_empty_bootstrap"
            or not _is_hash(binding["binding_hash"])
            or binding["binding_hash"]
            != _canonical_hash(_without_hash(binding, "binding_hash"))
        ):
            raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_INVALID")
        attestation = _exact_mapping(
            binding["attestation"],
            _BOOTSTRAP_ATTESTATION_KEYS,
            code="BOOTSTRAP_ATTESTATION_INVALID",
        )
        if (
            attestation["schema"] != _BOOTSTRAP_ATTESTATION_SCHEMA
            or not _is_hash(attestation["attestation_hash"])
            or attestation["attestation_hash"]
            != _canonical_hash(_without_hash(attestation, "attestation_hash"))
        ):
            raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_INVALID")
        identity_keys = (
            "workflow_id",
            "workspace_id",
            "repository_id",
            "project_id",
            "bootstrap_root_identity",
        )
        if (
            type(attestation["workflow_id"]) is not str
            or not attestation["workflow_id"]
            or any(
                not _is_hash(attestation[key])
                for key in identity_keys
                if key != "workflow_id"
            )
            or any(binding[key] != attestation[key] for key in identity_keys)
            or not _is_hash(attestation["initial_manifest_hash"])
            or not _is_hash(attestation["capability_hash"])
            or not _is_hash(attestation["attested_input_snapshot_id"])
            or type(attestation["capability_epoch"]) is not int
            or attestation["capability_epoch"] < 1
        ):
            raise RelayRuntimeError("BOOTSTRAP_IDENTITY_MISMATCH")
        if (
            attestation["state"] != "new_empty"
            or type(attestation["initial_entry_count"]) is not int
            or attestation["initial_entry_count"] != 0
        ):
            raise RelayRuntimeError("BOOTSTRAP_PROJECT_NOT_EMPTY")
        now = _trusted_time(self._clock)
        issued_at = _timestamp(
            attestation["issued_at"], code="BOOTSTRAP_ATTESTATION_STALE"
        )
        expires_at = _timestamp(
            attestation["expires_at"], code="BOOTSTRAP_ATTESTATION_STALE"
        )
        if (
            issued_at > now
            or now >= expires_at
            or expires_at - issued_at > 120
            or expires_at <= issued_at
        ):
            raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_STALE")
        verifier = self._bootstrap_capability_verifier
        try:
            verified = callable(verifier) and verifier(dict(attestation)) is True
        except Exception:
            verified = False
        if not verified:
            raise RelayRuntimeError("BOOTSTRAP_IDENTITY_MISMATCH")

        result: dict[str, object] = {
            "schema": _BOOTSTRAP_REGISTRY_BINDING_SCHEMA,
            "mode": "new_empty_bootstrap",
            "bootstrap_only": True,
            "workflow_id": attestation["workflow_id"],
            "workspace_id": attestation["workspace_id"],
            "repository_id": attestation["repository_id"],
            "project_id": attestation["project_id"],
            "bootstrap_root_identity": attestation["bootstrap_root_identity"],
            "initial_manifest_hash": attestation["initial_manifest_hash"],
            "initial_entry_count": 0,
            "capability_epoch": attestation["capability_epoch"],
            "capability_hash": attestation["capability_hash"],
            "attested_input_snapshot_id": attestation[
                "attested_input_snapshot_id"
            ],
            "issued_at": issued_at,
            "expires_at": expires_at,
            "attestation_hash": attestation["attestation_hash"],
        }
        result["binding_hash"] = _canonical_hash(result)
        return result

    def validate_bootstrap_recompile(
        self,
        *,
        project_binding: Mapping[str, object],
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate the complete bootstrap lineage before a later indexed request.

        This method does not register, synchronize, or promote an index. A caller
        must still issue a normal indexed recompile, whose ``resolve`` call keeps
        the existing current Project Index and Atlas checks.
        """

        registry_binding = self.resolve_new_empty_bootstrap(project_binding)
        return validate_project_index_bootstrap_receipt(
            registry_binding,
            receipt,
            clock=self._clock,
        )


class ProjectIndexBootstrapTransport:
    """Run exactly register then sync for a verified bootstrap-only binding."""

    def __init__(
        self,
        host: ProjectIndexBootstrapHostOperations,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._host = host
        self._clock = clock

    def execute(self, registry_binding: Mapping[str, object]) -> dict[str, object]:
        binding = _validated_bootstrap_registry_binding(
            registry_binding, clock=self._clock
        )
        host = self._host
        register = getattr(host, "project_index_register", None)
        sync = getattr(host, "project_index_sync", None)
        if not callable(register) or not callable(sync):
            raise RelayRuntimeError("BOOTSTRAP_HOST_OPERATION_FAILED")
        try:
            registered = _exact_mapping(
                register(
                    bootstrap_root_identity=binding["bootstrap_root_identity"],
                    attestation_hash=binding["attestation_hash"],
                ),
                frozenset({"workspace_id"}),
                code="BOOTSTRAP_HOST_OPERATION_FAILED",
            )
            if registered["workspace_id"] != binding["workspace_id"]:
                raise RelayRuntimeError("BOOTSTRAP_IDENTITY_MISMATCH")
            synchronized = _exact_mapping(
                sync(
                    workspace_id=binding["workspace_id"],
                    attestation_hash=binding["attestation_hash"],
                ),
                frozenset(
                    {
                        "workspace_id",
                        "attested_input_snapshot_id",
                        "initial_manifest_hash",
                        "initial_entry_count",
                        "index_snapshot_id",
                        "index_identity",
                    }
                ),
                code="BOOTSTRAP_HOST_OPERATION_FAILED",
            )
        except RelayRuntimeError:
            raise
        except Exception as error:
            raise RelayRuntimeError("BOOTSTRAP_HOST_OPERATION_FAILED") from error
        expected_index_identity = project_index_bootstrap_index_identity(synchronized)
        if (
            synchronized["workspace_id"] != binding["workspace_id"]
            or synchronized["attested_input_snapshot_id"]
            != binding["attested_input_snapshot_id"]
            or synchronized["initial_manifest_hash"]
            != binding["initial_manifest_hash"]
            or synchronized["initial_entry_count"] != 0
            or not _is_hash(synchronized["index_snapshot_id"])
            or synchronized["index_identity"] != expected_index_identity
        ):
            raise RelayRuntimeError("BOOTSTRAP_IDENTITY_MISMATCH")
        issued_at = _trusted_time(self._clock)
        binding_expires_at = _timestamp(
            binding["expires_at"], code="BOOTSTRAP_ATTESTATION_STALE"
        )
        receipt: dict[str, object] = {
            "schema": _BOOTSTRAP_RECEIPT_SCHEMA,
            "attestation_hash": binding["attestation_hash"],
            "workspace_id": binding["workspace_id"],
            "attested_input_snapshot_id": binding["attested_input_snapshot_id"],
            "initial_manifest_hash": binding["initial_manifest_hash"],
            "index_snapshot_id": synchronized["index_snapshot_id"],
            "index_identity": synchronized["index_identity"],
            "issued_at": issued_at,
            "expires_at": min(issued_at + 120, binding_expires_at),
        }
        receipt["receipt_hash"] = _canonical_hash(receipt)
        return validate_project_index_bootstrap_receipt(
            binding, receipt, clock=self._clock
        )


def validate_project_index_bootstrap_receipt(
    registry_binding: Mapping[str, object],
    receipt: Mapping[str, object],
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Validate one exact bootstrap receipt without promoting it to current index."""

    binding = _validated_bootstrap_registry_binding(registry_binding, clock=clock)
    value = _exact_mapping(
        receipt,
        _BOOTSTRAP_RECEIPT_KEYS,
        code="BOOTSTRAP_RECEIPT_INVALID",
    )
    if (
        value["schema"] != _BOOTSTRAP_RECEIPT_SCHEMA
        or not _is_hash(value["receipt_hash"])
        or value["receipt_hash"] != _canonical_hash(_without_hash(value, "receipt_hash"))
        or value["attestation_hash"] != binding["attestation_hash"]
        or value["workspace_id"] != binding["workspace_id"]
        or value["attested_input_snapshot_id"]
        != binding["attested_input_snapshot_id"]
        or value["initial_manifest_hash"] != binding["initial_manifest_hash"]
        or not _is_hash(value["index_snapshot_id"])
        or value["index_identity"]
        != project_index_bootstrap_index_identity(value)
    ):
        raise RelayRuntimeError("BOOTSTRAP_RECEIPT_INVALID")
    now = _trusted_time(clock)
    issued_at = _timestamp(value["issued_at"], code="BOOTSTRAP_RECEIPT_STALE")
    expires_at = _timestamp(value["expires_at"], code="BOOTSTRAP_RECEIPT_STALE")
    binding_expires_at = _timestamp(
        binding["expires_at"], code="BOOTSTRAP_RECEIPT_STALE"
    )
    if (
        issued_at > now
        or now >= expires_at
        or expires_at - issued_at > 120
        or expires_at > binding_expires_at
    ):
        raise RelayRuntimeError("BOOTSTRAP_RECEIPT_STALE")
    return dict(value)


def _validated_bootstrap_registry_binding(
    value: Mapping[str, object], *, clock: Callable[[], float]
) -> dict[str, object]:
    binding = _exact_mapping(
        value,
        _BOOTSTRAP_REGISTRY_BINDING_KEYS,
        code="BOOTSTRAP_ATTESTATION_INVALID",
    )
    now = _trusted_time(clock)
    expires_at = _timestamp(
        binding["expires_at"], code="BOOTSTRAP_ATTESTATION_INVALID"
    )
    if (
        binding["schema"] != _BOOTSTRAP_REGISTRY_BINDING_SCHEMA
        or binding["mode"] != "new_empty_bootstrap"
        or binding["bootstrap_only"] is not True
        or binding["initial_entry_count"] != 0
        or not _is_hash(binding["binding_hash"])
        or binding["binding_hash"]
        != _canonical_hash(_without_hash(binding, "binding_hash"))
        or not _is_hash(binding["attestation_hash"])
        or not _is_hash(binding["workspace_id"])
        or not _is_hash(binding["bootstrap_root_identity"])
        or now >= expires_at
    ):
        raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_INVALID")
    return dict(binding)


def _exact_mapping(
    value: object, keys: frozenset[str], *, code: str
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RelayRuntimeError(code)
    return dict(value)


def _without_hash(value: Mapping[str, object], field: str) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != field}


def _canonical_hash(value: object) -> str:
    return _HASH_PREFIX + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _is_hash(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_HASH_PREFIX)
        and len(value) == len(_HASH_PREFIX) + 64
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _trusted_time(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except Exception as error:
        raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_STALE") from error
    if not value > 0 or value == float("inf") or value != value:
        raise RelayRuntimeError("BOOTSTRAP_ATTESTATION_STALE")
    return value


def _timestamp(value: object, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RelayRuntimeError(code)
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise RelayRuntimeError(code)
    return result


def project_index_bootstrap_index_identity(value: Mapping[str, object]) -> str:
    """Derive the exact index identity carried by a bootstrap receipt."""

    return _canonical_hash(
        {
            "workspace_id": value.get("workspace_id"),
            "attested_input_snapshot_id": value.get("attested_input_snapshot_id"),
            "initial_manifest_hash": value.get("initial_manifest_hash"),
            "index_snapshot_id": value.get("index_snapshot_id"),
        }
    )


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
        self,
        relay_service: RelayService,
        *,
        capability_broker: CapabilityBroker | None,
        host_session: HostActionAdmission | None = None,
    ) -> None:
        self._relay_service = relay_service
        self._capability_broker = capability_broker
        self._host_session = host_session

    @classmethod
    def from_secret_provider(
        cls,
        store: RelayStore,
        *,
        capability_secret_provider: RelayCapabilitySecretProvider,
        capability_broker: CapabilityBroker | None,
        host_session: HostActionAdmission | None = None,
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
            host_session=host_session,
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
        """Journal start, admit at Host, then deliver private capabilities."""

        broker = self._available_broker()
        if broker is None:
            raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE")
        result = self._relay_service.start(request)
        attempt = self._relay_service.start_attempt(request.get("idempotency_key"))
        attempt_id = attempt.get("attempt_id")
        attempt_state = attempt.get("state")
        if type(attempt_id) is not str or type(attempt_state) is not str:
            raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE")
        if attempt_state == "aborted":
            error_code = attempt.get("error_code")
            if type(error_code) is not str or error_code not in {
                "RELAY_HOST_SESSION_UNAVAILABLE",
                "RELAY_HOST_ACTION_REJECTED",
            }:
                raise RelayError("RELAY_CAPABILITY_BROKER_UNAVAILABLE")
            raise RelayError(str(error_code))
        if attempt_state == "delivered":
            return result
        try:
            actions = result["host_actions"]
            if type(actions) is not list:
                raise TypeError
            admitted_actions: list[object] = [
                action
                for action in actions
                if type(action) is dict and "relay_host_scheduler_slot" in action
            ]
            if attempt_state == "prepared":
                try:
                    if admitted_actions:
                        self._admit_host_actions(admitted_actions)
                except RelayError as error:
                    if error.code in {
                        "RELAY_HOST_SESSION_UNAVAILABLE",
                        "RELAY_HOST_ACTION_REJECTED",
                    }:
                        self._relay_service.abort_start_attempt(
                            attempt_id, error_code=error.code
                        )
                    raise
                self._relay_service.mark_start_admitted(attempt_id)
            elif attempt_state != "admitted":
                raise TypeError
            for action in actions:
                self._deliver_worker_capabilities(broker, action)
            self._relay_service.mark_start_delivered(attempt_id)
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

    def _admit_host_actions(self, actions: list[object]) -> None:
        """Require live Host slot admission before issuing any bearer capability."""

        host_session = self._available_host_session()
        if host_session is None:
            raise RelayError("RELAY_HOST_SESSION_UNAVAILABLE")
        try:
            admitted = host_session.admit_relay_actions(actions)
        except Exception:
            admitted = False
        if admitted is not True:
            raise RelayError("RELAY_HOST_ACTION_REJECTED")

    def _available_host_session(self) -> HostActionAdmission | None:
        host_session = self._host_session
        if host_session is None:
            return None
        try:
            if host_session.is_available is True and callable(
                getattr(host_session, "admit_relay_actions", None)
            ):
                return host_session
        except Exception:
            return None
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
