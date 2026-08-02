"""Process-lifetime composition root with invocation-scoped resources."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from .config import RuntimeConfig, RuntimeConfigError
from .uow import (
    RuntimeAdapterFactories,
    RuntimeUnitOfWork,
    ToolResultAdapter,
    open_runtime_uow,
)

_UowFactory = Callable[..., object]


@dataclass(frozen=True)
class RuntimeAvailability:
    """Optional process-lifetime providers that are usable for this root."""

    capability_broker: bool
    integration_attestor: bool


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
    ) -> None:
        self._config = config
        self._uow_factory = uow_factory
        self._adapter_factories = adapter_factories
        self._tool_results = ToolResultAdapter()
        self._capability_broker = capability_broker
        self._integration_attestor = integration_attestor
        self._process_id = os.getpid()
        self._closed = False

    @property
    def availability(self) -> RuntimeAvailability:
        """Expose optional-provider availability without forcing startup work."""

        if self._closed or os.getpid() != self._process_id:
            return RuntimeAvailability(
                capability_broker=False,
                integration_attestor=False,
            )
        return RuntimeAvailability(
            capability_broker=_provider_is_available(self._capability_broker),
            integration_attestor=_provider_is_available(self._integration_attestor),
        )

    def open_uow(self, *, read_only: bool) -> RuntimeUnitOfWork:
        """Create a new call-owned UoW without caching a connection."""

        self._assert_open_in_current_process()
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
            factories=self._adapter_factories,
            tool_results=self._tool_results,
        )

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
        for provider in (self._capability_broker, self._integration_attestor):
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


def _provider_is_available(provider: object | None) -> bool:
    if provider is None:
        return False
    available = getattr(provider, "is_available", True)
    return bool(available() if callable(available) else available)
