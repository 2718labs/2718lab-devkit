"""Process-lifetime composition root with invocation-scoped resources."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .config import RuntimeConfig, RuntimeConfigError
from .uow import (
    RuntimeAdapterFactories,
    RuntimeUnitOfWork,
    ToolResultAdapter,
    open_runtime_uow,
)

_UowFactory = Callable[..., object]


class HostBootstrapAuthority(Protocol):
    """Host-owned bootstrap authority; it is never accepted from an MCP call."""

    def verify_bootstrap_capability(
        self, attestation: Mapping[str, object]
    ) -> bool: ...

    def resolve_bootstrap_root(
        self, *, bootstrap_root_identity: str, attestation_hash: str
    ) -> str: ...


class _ProjectIndexBootstrapHost:
    """Adapt existing Project Index operations to one fixed private authority."""

    def __init__(
        self,
        *,
        registry_binding: dict[str, object],
        authority: HostBootstrapAuthority,
        project_index: object,
    ) -> None:
        self._binding = dict(registry_binding)
        self._authority = authority
        self._project_index = project_index

    def project_index_register(
        self, *, bootstrap_root_identity: str, attestation_hash: str
    ) -> dict[str, object]:
        root = self._authority.resolve_bootstrap_root(
            bootstrap_root_identity=bootstrap_root_identity,
            attestation_hash=attestation_hash,
        )
        register = getattr(self._project_index, "project_index_register", None)
        if type(root) is not str or not root or not callable(register):
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("BOOTSTRAP_HOST_OPERATION_FAILED")
        return {"workspace_id": register(root)}

    def project_index_sync(
        self, *, workspace_id: str, attestation_hash: str
    ) -> dict[str, object]:
        if attestation_hash != self._binding.get("attestation_hash"):
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("BOOTSTRAP_IDENTITY_MISMATCH")
        sync = getattr(self._project_index, "sync", None)
        if not callable(sync):
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("BOOTSTRAP_HOST_OPERATION_FAILED")
        snapshot = sync(workspace_id)
        from .relay_runtime import project_index_bootstrap_index_identity

        result: dict[str, object] = {
            "workspace_id": getattr(snapshot, "workspace_id", None),
            "attested_input_snapshot_id": self._binding.get(
                "attested_input_snapshot_id"
            ),
            "initial_manifest_hash": getattr(snapshot, "manifest_hash", None),
            "initial_entry_count": getattr(snapshot, "file_count", None),
            "index_snapshot_id": getattr(snapshot, "snapshot_id", None),
        }
        result["index_identity"] = project_index_bootstrap_index_identity(result)
        return result


class HostRelayBootstrap:
    """The only private V2/V3 Relay bootstrap route for one RuntimeRoot."""

    _SCHEMAS = frozenset(
        {
            "2718lab-devkit/relay-compile-request-v2",
            "2718lab-devkit/relay-compile-request-v3",
        }
    )

    def __init__(self, root: RuntimeRoot, authority: HostBootstrapAuthority) -> None:
        self._root = root
        self._authority = authority

    def compile(
        self,
        request: Mapping[str, object],
        *,
        clock: Callable[[], float] = time.time,
    ) -> dict[str, object]:
        """Compile a host-authorized bootstrap without a public authority input."""

        self._require_bootstrap_schema(request)
        with self._root.open_uow(read_only=False) as uow:
            return self._compile_from_uow(request, uow=uow, clock=clock)

    def _compile_from_uow(
        self,
        request: Mapping[str, object],
        *,
        uow: object,
        clock: Callable[[], float],
    ) -> dict[str, object]:
        """Recheck the schema inside the UoW boundary before bootstrap IO."""

        self._require_bootstrap_schema(request)
        project_checkpoint = getattr(uow, "project_checkpoint", None)
        project_index = getattr(project_checkpoint, "project_index", None)
        atlas_store = getattr(uow, "atlas_store", None)
        if project_index is None or atlas_store is None:
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("RUNTIME_DEPENDENCY_UNAVAILABLE")
        return self._compile_request(
            request,
            project_index=project_index,
            atlas_store=atlas_store,
            clock=clock,
        )

    def _compile_request(
        self,
        request: Mapping[str, object],
        *,
        project_index: object,
        atlas_store: object,
        clock: Callable[[], float],
    ) -> dict[str, object]:
        self._require_bootstrap_schema(request)
        from devkit_relay.compiler import compile_plan

        from .relay_runtime import (
            ProductionRegistryResolver,
            ProjectIndexBootstrapTransport,
        )

        registry = ProductionRegistryResolver(
            project_index,
            atlas_store,
            clock=clock,
            bootstrap_capability_verifier=(
                lambda attestation: (
                    self._authority.verify_bootstrap_capability(attestation) is True
                )
            ),
        )
        descriptor = cast(
            dict[str, object], compile_plan(request, registry_resolver=registry)
        )
        if "bootstrap_receipt" in request:
            return descriptor
        project_binding = request.get("project_binding")
        if (
            type(project_binding) is not dict
            or project_binding.get("mode") != "new_empty_bootstrap"
        ):
            return descriptor
        registry_binding = registry.resolve_new_empty_bootstrap(project_binding)
        host = _ProjectIndexBootstrapHost(
            registry_binding=registry_binding,
            authority=self._authority,
            project_index=project_index,
        )
        receipt = ProjectIndexBootstrapTransport(host, clock=clock).execute(
            registry_binding
        )
        recompile = dict(request)
        recompile["input_snapshot_id"] = receipt["index_snapshot_id"]
        recompile["bootstrap_receipt"] = receipt
        return cast(
            dict[str, object], compile_plan(recompile, registry_resolver=registry)
        )

    @classmethod
    def _require_bootstrap_schema(cls, request: Mapping[str, object]) -> None:
        if type(request) is not dict or request.get("schema") not in cls._SCHEMAS:
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("BOOTSTRAP_HOST_REQUEST_INVALID")


@dataclass(frozen=True)
class RuntimeAvailability:
    """Optional process-lifetime providers that are usable for this root."""

    capability_broker: bool
    integration_attestor: bool
    host_session: bool


class RuntimeRoot:
    """Keep only configuration and factories across tool invocations."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        uow_factory: _UowFactory | None = None,
        adapter_factories: RuntimeAdapterFactories | None = None,
        capability_broker: object | None = None,
        integration_attestor: object | None = None,
        host_session: object | None = None,
        host_bootstrap_authority: HostBootstrapAuthority | None = None,
    ) -> None:
        self._config = config
        self._uow_factory = uow_factory
        self._adapter_factories = adapter_factories
        self._tool_results = ToolResultAdapter()
        self._capability_broker = capability_broker
        self._integration_attestor = integration_attestor
        self._host_session = host_session
        self._host_bootstrap_authority = host_bootstrap_authority
        self._process_id = os.getpid()
        self._closed = False

    @property
    def availability(self) -> RuntimeAvailability:
        """Expose optional-provider availability without forcing startup work."""

        if self._closed or os.getpid() != self._process_id:
            return RuntimeAvailability(
                capability_broker=False,
                integration_attestor=False,
                host_session=False,
            )
        return RuntimeAvailability(
            capability_broker=_provider_is_available(self._capability_broker),
            integration_attestor=_provider_is_available(self._integration_attestor),
            host_session=_provider_is_available(self._host_session),
        )

    def open_uow(self, *, read_only: bool) -> RuntimeUnitOfWork:
        """Create a new call-owned UoW without caching a connection."""

        self._assert_open_in_current_process()
        self._assert_project_authority_for_uow()
        if self._uow_factory is not None:
            return cast(
                RuntimeUnitOfWork,
                self._uow_factory(config=self._config, read_only=read_only),
            )
        return open_runtime_uow(
            config=self._config,
            read_only=read_only,
            capability_broker=self._capability_broker,
            integration_attestor=self._integration_attestor,
            host_session=self._host_session,
            factories=self._adapter_factories,
            tool_results=self._tool_results,
        )

    def host_relay_bootstrap(self) -> HostRelayBootstrap:
        """Return the root-owned V2/V3 bootstrap factory or fail before a UoW."""

        self._assert_open_in_current_process()
        authority = self._host_bootstrap_authority
        if not _is_host_bootstrap_authority(authority):
            from .relay_runtime import RelayRuntimeError

            raise RelayRuntimeError("BOOTSTRAP_HOST_AUTHORITY_UNAVAILABLE")
        return HostRelayBootstrap(self, cast(HostBootstrapAuthority, authority))

    def shutdown(self) -> None:
        """Close owned optional providers exactly once."""

        if self._closed:
            return
        if os.getpid() != self._process_id:
            self._closed = True
            return
        self._closed = True
        closed_ids: set[int] = set()
        first_error: Exception | None = None
        for provider in (
            self._capability_broker,
            self._integration_attestor,
            self._host_session,
        ):
            if provider is None or id(provider) in closed_ids:
                continue
            closed_ids.add(id(provider))
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    def _assert_open_in_current_process(self) -> None:
        if self._closed:
            raise RuntimeConfigError("RUNTIME_CLOSED")
        if os.getpid() != self._process_id:
            raise RuntimeConfigError("RUNTIME_PROCESS_MISMATCH")

    def _assert_project_authority_for_uow(self) -> None:
        """Fence each UoW open against an invalid projects-v2 authority."""

        if self._config.storage_layout == "legacy-compat":
            if (
                self._config.project_authority is not None
                or self._config.authority_provider is not None
            ):
                raise RuntimeConfigError("PROJECT_AUTHORITY_PROVIDER_INVALID")
            return
        if self._config.storage_layout == "projects-v2":
            self._config.require_project_authority()
            return
        raise RuntimeConfigError("PROJECT_AUTHORITY_PROVIDER_INVALID")


def _provider_is_available(provider: object | None) -> bool:
    if provider is None:
        return False
    available = getattr(provider, "is_available", True)
    return bool(available() if callable(available) else available)


def _is_host_bootstrap_authority(value: object | None) -> bool:
    return (
        value is not None
        and callable(getattr(value, "verify_bootstrap_capability", None))
        and callable(getattr(value, "resolve_bootstrap_root", None))
    )
