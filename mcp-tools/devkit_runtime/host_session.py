"""Private fail-closed facts from one inherited host bridge session.

This module deliberately has no dispatch, listener, worktree, persistence, or
account-usage coordinator. Its live inputs are an authenticated inherited
bridge plus same-process capability and lease-bound compiler resolvers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import Final, TypeAlias

from . import host_envelopes
from .host_bridge import (
    HostBridgeError,
    InheritedHandleHostBridge,
    OperationReceipt,
)
from .host_scheduler_topology_adapter import (
    HostSchedulerTopologyFact,
    construct_host_scheduler_topology,
)

_NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH_PREFIX: Final = "sha256:"
_IDENTIFIER: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_REASON_SESSION_UNAVAILABLE: Final = "HOST_SESSION_UNAVAILABLE"
_REASON_CAPABILITY_UNAVAILABLE: Final = "HOST_CAPABILITY_UNAVAILABLE"
_REASON_EXECUTION_EVIDENCE_UNAVAILABLE: Final = "HOST_EXECUTION_EVIDENCE_UNAVAILABLE"


class HostCapabilityState(StrEnum):
    """Evidence levels that must never be conflated by a scheduler."""

    DECLARED = "DECLARED"
    ATTESTED = "ATTESTED"
    EXECUTED = "EXECUTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class HostRoute:
    """One exact model/effort pair requested from the private host."""

    model: str
    effort: str


@dataclass(frozen=True)
class HostCapabilityFact:
    """A bearer-free fact with an explicit evidence state."""

    route: HostRoute
    capability_id: str
    state: HostCapabilityState
    attestation_hash: str | None
    binding_hash: str | None
    reason_code: str | None


@dataclass(frozen=True)
class HostSchedulingFacts:
    """Exact pairs that a scheduler may consume without a fallback decision."""

    binding_hash: str
    routes: tuple[HostRoute, ...]


@dataclass(frozen=True)
class HostUnavailableFacts:
    """Bounded non-sensitive explanation for a failed capability admission."""

    reason_code: str
    bounds: tuple[HostRoute, ...]
    facts: tuple[HostCapabilityFact, ...]


# Keep the established HostSession name while the fact remains adapter-private.
HostTopologyGroupFact = HostSchedulerTopologyFact


@dataclass(frozen=True)
class HostResolvedTopologyGroup:
    """Auditable, non-dispatching group result without paths or task leases."""

    scheduler_id: str
    coordinator_lease_id: str
    worktree_identity: str
    writer_task_ids: tuple[str, ...]
    prewarm_task_ids: tuple[str, ...]
    relay_group_binding_hash: str
    attested_capacity: int
    attestation_hash: str
    group_binding_hash: str


@dataclass(frozen=True)
class HostResolvedSchedulerTopology:
    """Private Host-only resolution of an already parsed Host projection."""

    schema: str
    relay_plan_hash: str
    relay_topology_hash: str
    projection_hash: str
    groups: tuple[HostResolvedTopologyGroup, ...]
    audit_binding_hash: str


@dataclass(frozen=True)
class _CapabilityRecord:
    """Process-private binding retained for terminal validation only."""

    fact: HostCapabilityFact
    binding: host_envelopes.EnvelopeBinding = field(repr=False)


CompilerEvidenceProvider: TypeAlias = Callable[["_CompilerPreparation"], object]


class _CompilerEvidenceHandle:
    """Opaque session-issued reference for one compiler-evidence exchange."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<HostCompilerEvidenceHandle redacted>"


@dataclass(frozen=True)
class _CompilerInvocationBinding:
    """Trusted, session-owned compiler fields resolved before provider admission."""

    request_hash: str
    reasoning_effort: str
    verified_route_result_hashes: tuple[str, ...]
    verified_lease_scope_bindings: tuple[str, ...]


CompilerInvocationResolver: TypeAlias = Callable[
    [str], _CompilerInvocationBinding | None
]
TopologyFactResolver: TypeAlias = Callable[[object], object]


@dataclass(frozen=True)
class _CompilerInvocation:
    """One redacted compiler invocation bound to its issuing host session."""

    schema: str
    preparation_id: str
    request_hash: str
    reasoning_effort: str
    verified_route_result_hashes: tuple[str, ...]
    verified_lease_scope_bindings: tuple[str, ...]
    issued_at: float
    expires_at: float
    binding_hash: str


class _CompilerPreparation:
    """A zero-field marker that a provider can only return unchanged."""

    __slots__ = ()


class HostSession:
    """Consume one authenticated inherited bridge without any fallback path."""

    def __init__(
        self,
        *,
        bridge: InheritedHandleHostBridge | None,
        clock: Callable[[], float],
        compiler_evidence_provider: CompilerEvidenceProvider | None = None,
        compiler_invocation_resolver: CompilerInvocationResolver | None = None,
        topology_fact_resolver: TopologyFactResolver | None = None,
    ) -> None:
        self._bridge = bridge
        self._compiler_evidence_provider = (
            compiler_evidence_provider if callable(compiler_evidence_provider) else None
        )
        self._compiler_invocation_resolver = (
            compiler_invocation_resolver
            if callable(compiler_invocation_resolver)
            else None
        )
        self._topology_fact_resolver = (
            topology_fact_resolver if callable(topology_fact_resolver) else None
        )
        self._compiler_evidence_lock = RLock()
        self._compiler_evidence: dict[_CompilerEvidenceHandle, _CompilerInvocation] = {}
        self._burned_preparation_ids: set[str] = set()
        self._clock = clock
        self._last_trusted_clock: float | None = None
        self._closed = False
        self._frozen = (
            bridge is None
            or not callable(clock)
            or not bridge.is_available
        )
        self._attested_capabilities: dict[tuple[str, str], _CapabilityRecord] = {}
        self._consumed_predecessors: set[tuple[str, str, str, str, str]] = set()
        self._last_unavailable: HostUnavailableFacts | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None,
        platform: str | None,
        clock: Callable[[], float],
    ) -> HostSession:
        """Accept only the configured inherited bridge selector, never a fallback."""

        try:
            bridge = InheritedHandleHostBridge.from_environment(
                environ=environ,
                platform=platform,
            )
        except HostBridgeError:
            bridge = None
        return cls(
            bridge=bridge,
            clock=clock,
        )

    @property
    def is_available(self) -> bool:
        """Report only whether this process-local private session remains usable."""

        return (
            not self._closed
            and not self._frozen
            and self._bridge is not None
            and self._bridge.is_available
        )

    @property
    def last_unavailable(self) -> HostUnavailableFacts | None:
        """Expose only bounded, bearer-free capability unavailability facts."""

        return self._last_unavailable

    def close(self) -> None:
        """Close the owned private transport once; no session can be revived."""

        with self._compiler_evidence_lock:
            if self._closed:
                return
            self._closed = True
            self._frozen = True
            self._compiler_evidence.clear()
        if self._bridge is not None:
            self._bridge.close()

    def prepare_compiler_evidence(
        self, *, preparation_id: str
    ) -> _CompilerEvidenceHandle | str:
        """Issue one opaque handle for compiler facts held only by this session."""

        with self._compiler_evidence_lock:
            provider = self._compiler_evidence_provider
            binding_resolver = self._compiler_invocation_resolver
            if (
                self._closed
                or self._frozen
                or provider is None
                or binding_resolver is None
                or not isinstance(preparation_id, str)
                or _IDENTIFIER.fullmatch(preparation_id) is None
            ):
                return _NO_SAFE_WORK
            if preparation_id in self._burned_preparation_ids:
                return _NO_SAFE_WORK
            self._burned_preparation_ids.add(preparation_id)
            if not self.is_available:
                return _NO_SAFE_WORK
            try:
                issued_at = self._read_trusted_clock()
            except (TypeError, ValueError):
                return _NO_SAFE_WORK
            expires_at = issued_at + 120
            try:
                binding = _normalized_compiler_invocation_binding(
                    binding_resolver(preparation_id)
                )
            except Exception:
                return _NO_SAFE_WORK
            preparation = _CompilerPreparation()
            material = _CompilerInvocation(
                schema="2718lab-devkit/compiler-invocation-v2",
                preparation_id=preparation_id,
                request_hash=binding.request_hash,
                reasoning_effort=binding.reasoning_effort,
                verified_route_result_hashes=binding.verified_route_result_hashes,
                verified_lease_scope_bindings=binding.verified_lease_scope_bindings,
                issued_at=issued_at,
                expires_at=expires_at,
                binding_hash=_hash(
                    {
                        "preparation_id": preparation_id,
                        "request_hash": binding.request_hash,
                        "reasoning_effort": binding.reasoning_effort,
                        "verified_route_result_hashes": binding.verified_route_result_hashes,
                        "verified_lease_scope_bindings": binding.verified_lease_scope_bindings,
                        "issued_at": issued_at,
                        "expires_at": expires_at,
                    }
                ),
            )
            material_state = _compiler_invocation_state(material)
            try:
                provider_material = provider(preparation)
            except Exception:
                return _NO_SAFE_WORK
            try:
                material_is_unchanged = (
                    _compiler_invocation_state(material) == material_state
                )
            except Exception:
                return _NO_SAFE_WORK
            if (
                self._closed
                or self._frozen
                or not self.is_available
                or provider_material is not preparation
                or not material_is_unchanged
            ):
                return _NO_SAFE_WORK
            evidence = _CompilerEvidenceHandle()
            self._compiler_evidence[evidence] = material
            return evidence

    def consume_compiler_evidence(self, evidence: object) -> object | str:
        """Exchange a session-issued handle once, rejecting public substitutes."""

        with self._compiler_evidence_lock:
            if (
                self._closed
                or self._frozen
                or type(evidence) is not _CompilerEvidenceHandle
            ):
                return _NO_SAFE_WORK
            material = self._compiler_evidence.pop(evidence, None)
            if material is None:
                return _NO_SAFE_WORK
            try:
                now = self._read_trusted_clock()
            except (TypeError, ValueError):
                return _NO_SAFE_WORK
            if now >= material.expires_at:
                return _NO_SAFE_WORK
            return material

    def resolve_scheduler_topology(
        self, topology: object
    ) -> HostResolvedSchedulerTopology | str:
        """Resolve one parsed Host topology against the private group facts."""

        from .fastlane_host_intent import ParsedHostSchedulerTopologyProjection

        with self._compiler_evidence_lock:
            resolver = self._topology_fact_resolver
            if (
                self._closed
                or self._frozen
                or not self.is_available
                or type(topology) is not ParsedHostSchedulerTopologyProjection
                or resolver is None
            ):
                return _NO_SAFE_WORK
            try:
                raw_facts = resolver(topology)
            except Exception:
                return _NO_SAFE_WORK
            return self._resolve_host_scheduler_topology(topology, raw_facts)

    def resolve_relay_host_scheduler_slot(
        self, slot: object
    ) -> HostResolvedSchedulerTopology | str:
        """Privately build, parse, and resolve aggregate topology from one slot."""

        from .fastlane_host_intent import (
            NO_SAFE_WORK,
            parse_host_scheduler_topology_projection,
            parse_relay_host_scheduler_slot,
        )

        with self._compiler_evidence_lock:
            resolver = self._topology_fact_resolver
            if (
                self._closed
                or self._frozen
                or not self.is_available
                or resolver is None
            ):
                return _NO_SAFE_WORK
            parsed_slot = parse_relay_host_scheduler_slot(slot)
            if parsed_slot == NO_SAFE_WORK:
                return _NO_SAFE_WORK
            try:
                raw_facts = resolver(parsed_slot)
                host_topology = construct_host_scheduler_topology(
                    parsed_slot, raw_facts
                )
                if host_topology == _NO_SAFE_WORK:
                    return _NO_SAFE_WORK
                parsed_topology = parse_host_scheduler_topology_projection(
                    host_topology
                )
                if parsed_topology == NO_SAFE_WORK:
                    return _NO_SAFE_WORK
            except Exception:
                return _NO_SAFE_WORK
            return self._resolve_host_scheduler_topology(parsed_topology, raw_facts)

    def _resolve_host_scheduler_topology(
        self,
        topology: object,
        raw_facts: object,
    ) -> HostResolvedSchedulerTopology | str:
        """Strongly compare every Relay hash and group field to private facts."""

        from .fastlane_host_intent import ParsedHostSchedulerTopologyProjection

        if (
            type(topology) is not ParsedHostSchedulerTopologyProjection
            or type(raw_facts) is not tuple
            or len(raw_facts) != len(topology.groups)
        ):
            return _NO_SAFE_WORK
        try:
            facts_by_scheduler: dict[str, HostTopologyGroupFact] = {}
            for fact in raw_facts:
                if (
                    type(fact) is not HostTopologyGroupFact
                    or not _is_hash(fact.plan_hash)
                    or not _is_hash(fact.topology_hash)
                    or not _is_hash(fact.group_binding_hash)
                    or _IDENTIFIER.fullmatch(fact.scheduler_id) is None
                    or _IDENTIFIER.fullmatch(fact.coordinator_lease_id) is None
                    or _IDENTIFIER.fullmatch(fact.worktree_identity) is None
                    or type(fact.writer_task_ids) is not tuple
                    or not 1 <= len(fact.writer_task_ids) <= 3
                    or any(
                        _IDENTIFIER.fullmatch(task_id) is None
                        for task_id in fact.writer_task_ids
                    )
                    or type(fact.prewarm_task_ids) is not tuple
                    or len(fact.prewarm_task_ids) > 16
                    or any(
                        _IDENTIFIER.fullmatch(task_id) is None
                        for task_id in fact.prewarm_task_ids
                    )
                    or set(fact.writer_task_ids).intersection(fact.prewarm_task_ids)
                    or not 1 <= fact.attested_capacity <= 3
                    or len(fact.writer_task_ids) > fact.attested_capacity
                    or not _is_hash(fact.attestation_hash)
                    or fact.scheduler_id in facts_by_scheduler
                ):
                    raise ValueError("topology fact is invalid")
                facts_by_scheduler[fact.scheduler_id] = fact
            groups: list[HostResolvedTopologyGroup] = []
            total_writer_tasks = 0
            for group in topology.groups:
                fact = facts_by_scheduler.get(group.scheduler_id)
                if (
                    fact is None
                    or fact.plan_hash != topology.relay_plan_hash
                    or fact.topology_hash != topology.relay_topology_hash
                    or fact.group_binding_hash != group.relay_group_binding_hash
                    or fact.coordinator_lease_id != group.coordinator_lease_id
                    or fact.worktree_identity != group.worktree_identity
                    or fact.writer_task_ids != group.writer_task_ids
                    or fact.prewarm_task_ids != group.prewarm_task_ids
                    or fact.attested_capacity != group.attested_capacity
                    or fact.attestation_hash != group.attestation_hash
                ):
                    raise ValueError("topology binding is invalid")
                total_writer_tasks += len(group.writer_task_ids)
                groups.append(
                    HostResolvedTopologyGroup(
                        scheduler_id=group.scheduler_id,
                        coordinator_lease_id=group.coordinator_lease_id,
                        worktree_identity=group.worktree_identity,
                        writer_task_ids=group.writer_task_ids,
                        prewarm_task_ids=group.prewarm_task_ids,
                        relay_group_binding_hash=group.relay_group_binding_hash,
                        attested_capacity=fact.attested_capacity,
                        attestation_hash=fact.attestation_hash,
                        group_binding_hash=group.group_binding_hash,
                    )
                )
            if total_writer_tasks > 9:
                raise ValueError("topology exceeds host capacity")
            audit_binding_hash = _hash(
                {
                    "schema": topology.schema,
                    "relay_plan_hash": topology.relay_plan_hash,
                    "relay_topology_hash": topology.relay_topology_hash,
                    "projection_hash": topology.projection_hash,
                    "groups": [
                        {
                            "scheduler_id": group.scheduler_id,
                            "coordinator_lease_id": group.coordinator_lease_id,
                            "worktree_identity": group.worktree_identity,
                            "writer_task_ids": group.writer_task_ids,
                            "prewarm_task_ids": group.prewarm_task_ids,
                            "relay_group_binding_hash": group.relay_group_binding_hash,
                            "attested_capacity": group.attested_capacity,
                            "attestation_hash": group.attestation_hash,
                            "group_binding_hash": group.group_binding_hash,
                        }
                        for group in groups
                    ],
                }
            )
        except Exception:
            return _NO_SAFE_WORK
        return HostResolvedSchedulerTopology(
            schema=topology.schema,
            relay_plan_hash=topology.relay_plan_hash,
            relay_topology_hash=topology.relay_topology_hash,
            projection_hash=topology.projection_hash,
            groups=tuple(groups),
            audit_binding_hash=audit_binding_hash,
        )

    def declare_routes(
        self, routes: tuple[HostRoute, ...] | list[HostRoute]
    ) -> tuple[HostCapabilityFact, ...]:
        """Represent caller declarations without treating them as host evidence."""

        try:
            normalized = _normalized_routes(routes)
        except (TypeError, ValueError):
            return ()
        return tuple(
            HostCapabilityFact(
                route=route,
                capability_id=_route_capability_id(route),
                state=HostCapabilityState.DECLARED,
                attestation_hash=None,
                binding_hash=None,
                reason_code=None,
            )
            for route in normalized
        )

    def attest_routes(
        self,
        *,
        binding: host_envelopes.EnvelopeBinding | Mapping[str, object],
        routes: tuple[HostRoute, ...] | list[HostRoute],
        now: int,
    ) -> tuple[HostCapabilityFact, ...] | str:
        """Return only exact bridge-attested pairs; no local model fallback exists."""

        declared = self.declare_routes(routes)
        bounds = tuple(fact.route for fact in declared)
        if not self.is_available:
            self._record_unavailable(bounds, _REASON_SESSION_UNAVAILABLE)
            return _NO_SAFE_WORK
        bridge = self._bridge
        assert bridge is not None
        try:
            if not declared:
                raise ValueError("route declarations are invalid")
            probe = bridge.send_capability_probe(
                binding=binding,
                capability_names=tuple(fact.capability_id for fact in declared),
                now=now,
            )
            report = bridge.receive_capability_report(probe=probe, now=now)
            reported_hashes = _mapping(report["capability_hashes"])
            binding_hash = _binding_hash(probe.binding, now=now)
            facts = tuple(
                HostCapabilityFact(
                    route=fact.route,
                    capability_id=fact.capability_id,
                    state=HostCapabilityState.ATTESTED,
                    attestation_hash=_required_hash(
                        reported_hashes[fact.capability_id]
                    ),
                    binding_hash=binding_hash,
                    reason_code=None,
                )
                for fact in declared
            )
        except Exception:
            self._record_unavailable(bounds, _REASON_CAPABILITY_UNAVAILABLE)
            self._freeze()
            return _NO_SAFE_WORK
        self._attested_capabilities.update(
            {
                _capability_key(fact): _CapabilityRecord(
                    fact=fact,
                    binding=probe.binding,
                )
                for fact in facts
            }
        )
        return facts

    def scheduling_facts(
        self, facts: tuple[HostCapabilityFact, ...] | list[HostCapabilityFact] | object
    ) -> HostSchedulingFacts | str:
        """Expose the exact attested tuples or fail closed without substituting one."""

        bounds = _route_bounds_from_facts(facts)
        if not self.is_available:
            self._record_unavailable(bounds, _REASON_SESSION_UNAVAILABLE)
            return _NO_SAFE_WORK
        try:
            if not isinstance(facts, (tuple, list)) or not facts:
                raise ValueError("capability facts are invalid")
            verified: list[HostRoute] = []
            expected_binding_hash: str | None = None
            for fact in facts:
                if type(fact) is not HostCapabilityFact:
                    raise ValueError("capability fact is invalid")
                if fact.state not in {
                    HostCapabilityState.ATTESTED,
                    HostCapabilityState.EXECUTED,
                }:
                    raise ValueError("capability fact is not attested")
                if fact.binding_hash is None:
                    raise ValueError("capability binding is unavailable")
                if expected_binding_hash is None:
                    expected_binding_hash = fact.binding_hash
                elif fact.binding_hash != expected_binding_hash:
                    raise ValueError("capability bindings are mixed")
                record = self._attested_capabilities.get(_capability_key(fact))
                if record is None or record.fact != fact:
                    raise ValueError("capability fact is foreign")
                verified.append(fact.route)
            assert expected_binding_hash is not None
            return HostSchedulingFacts(
                binding_hash=expected_binding_hash,
                routes=tuple(verified),
            )
        except Exception:
            self._record_unavailable(bounds, _REASON_CAPABILITY_UNAVAILABLE)
            self._freeze()
            return _NO_SAFE_WORK

    def observe_execution(
        self,
        fact: HostCapabilityFact | object,
        *,
        predecessor: OperationReceipt | object,
        now: int,
    ) -> HostCapabilityFact | str:
        """Observe a bridge-terminal receipt; this adapter never executes work."""

        bounds = _route_bounds_from_facts((fact,))
        if not self.is_available:
            self._record_unavailable(bounds, _REASON_SESSION_UNAVAILABLE)
            return _NO_SAFE_WORK
        bridge = self._bridge
        assert bridge is not None
        try:
            if (
                type(fact) is not HostCapabilityFact
                or fact.state is not HostCapabilityState.ATTESTED
                or type(predecessor) is not OperationReceipt
            ):
                raise ValueError("execution evidence is invalid")
            record = self._attested_capabilities.get(_capability_key(fact))
            if (
                record is None
                or record.fact != fact
                or predecessor.kind != "coordinator_assignment"
                or predecessor.task_id != record.binding.task_id
                or predecessor.binding != record.binding
                or _predecessor_key(predecessor, now=now) in self._consumed_predecessors
            ):
                raise ValueError("execution binding is invalid")
            terminal = bridge.receive_terminal_result(
                predecessor=predecessor,
                now=now,
                expected=host_envelopes.EnvelopeExpectation(
                    kind="worker_terminal_result",
                    binding=record.binding,
                ),
            )
            terminal_payload = _mapping(terminal["payload"])
            if (
                terminal_payload.get("correlation_id") != predecessor.correlation_id
                or terminal_payload.get("predecessor_hash") != predecessor.envelope_hash
            ):
                raise ValueError("terminal predecessor is invalid")
            executed = HostCapabilityFact(
                route=fact.route,
                capability_id=fact.capability_id,
                state=HostCapabilityState.EXECUTED,
                attestation_hash=fact.attestation_hash,
                binding_hash=fact.binding_hash,
                reason_code=None,
            )
        except Exception:
            self._record_unavailable(bounds, _REASON_EXECUTION_EVIDENCE_UNAVAILABLE)
            self._freeze()
            return _NO_SAFE_WORK
        self._consumed_predecessors.add(_predecessor_key(predecessor, now=now))
        self._attested_capabilities[_capability_key(executed)] = _CapabilityRecord(
            fact=executed,
            binding=record.binding,
        )
        return executed

    def _record_unavailable(
        self,
        routes: tuple[HostRoute, ...] | list[HostRoute],
        reason_code: str,
    ) -> None:
        bounds = _bounded_routes(routes)
        facts = tuple(
            HostCapabilityFact(
                route=route,
                capability_id=_route_capability_id(route),
                state=HostCapabilityState.UNAVAILABLE,
                attestation_hash=None,
                binding_hash=None,
                reason_code=reason_code,
            )
            for route in bounds
        )
        self._last_unavailable = HostUnavailableFacts(
            reason_code=reason_code,
            bounds=bounds,
            facts=facts,
        )

    def _freeze(self) -> None:
        with self._compiler_evidence_lock:
            self._frozen = True
            self._compiler_evidence.clear()
        if self._bridge is not None:
            self._bridge.close()

    def _read_trusted_clock(self) -> float:
        """Read one finite, non-regressing timestamp from the trusted host clock."""

        with self._compiler_evidence_lock:
            value = _trusted_clock(self._clock)
            if (
                self._last_trusted_clock is not None
                and value < self._last_trusted_clock
            ):
                raise ValueError("trusted host clock regressed")
            self._last_trusted_clock = value
            return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("expected object")
    return value


def _required_hash(value: object) -> str:
    if not _is_hash(value):
        raise ValueError("expected hash")
    assert isinstance(value, str)
    return value


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(_HASH_PREFIX) + 64
        and value.startswith(_HASH_PREFIX)
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _required_positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected positive integer")
    return value


def _required_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected non-negative integer")
    return value


def _strict_hash_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and len(value) <= 16
        and all(_is_hash(item) for item in value)
        and tuple(sorted(value)) == value
        and len(set(value)) == len(value)
    )


def _normalized_compiler_invocation_binding(
    value: object,
) -> _CompilerInvocationBinding:
    if type(value) is not _CompilerInvocationBinding:
        raise ValueError("compiler invocation binding is unavailable")
    if (
        not _is_hash(value.request_hash)
        or value.reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}
        or not _strict_hash_tuple(value.verified_route_result_hashes)
        or not _strict_hash_tuple(value.verified_lease_scope_bindings)
    ):
        raise ValueError("compiler invocation binding is invalid")
    return _CompilerInvocationBinding(
        request_hash=value.request_hash,
        reasoning_effort=value.reasoning_effort,
        verified_route_result_hashes=value.verified_route_result_hashes,
        verified_lease_scope_bindings=value.verified_lease_scope_bindings,
    )


def _compiler_invocation_state(value: _CompilerInvocation) -> tuple[object, ...]:
    """Capture every canonical field before a provider callback can run."""

    return (
        value.schema,
        value.preparation_id,
        value.request_hash,
        value.reasoning_effort,
        value.verified_route_result_hashes,
        value.verified_lease_scope_bindings,
        value.issued_at,
        value.expires_at,
        value.binding_hash,
    )


def _trusted_clock(clock: Callable[[], float]) -> float:
    value = float(clock())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("trusted host clock is invalid")
    return value


def _hash(value: object) -> str:
    return (
        _HASH_PREFIX
        + hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


def _bounded_routes(value: object) -> tuple[HostRoute, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    try:
        return _normalized_routes(value)
    except (TypeError, ValueError):
        return ()


def _route_bounds_from_facts(value: object) -> tuple[HostRoute, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    routes = [
        fact.route
        for fact in value
        if type(fact) is HostCapabilityFact and type(fact.route) is HostRoute
    ]
    return _bounded_routes(routes)


def _capability_key(
    fact: HostCapabilityFact,
) -> tuple[str, str]:
    if type(fact) is not HostCapabilityFact or fact.binding_hash is None:
        raise ValueError("capability binding is unavailable")
    return fact.capability_id, fact.binding_hash


def _binding_hash(binding: host_envelopes.EnvelopeBinding, *, now: int) -> str:
    return _hash(host_envelopes.binding_mapping(binding, now=now))


def _predecessor_key(
    predecessor: OperationReceipt,
    *,
    now: int,
) -> tuple[str, str, str, str, str]:
    return (
        predecessor.kind,
        predecessor.task_id,
        predecessor.correlation_id,
        predecessor.envelope_hash,
        _binding_hash(predecessor.binding, now=now),
    )


def _normalized_routes(
    routes: object,
) -> tuple[HostRoute, ...]:
    if not isinstance(routes, (tuple, list)) or not routes or len(routes) > 16:
        raise ValueError("route declarations are invalid")
    normalized: list[HostRoute] = []
    for route in routes:
        if type(route) is not HostRoute:
            raise ValueError("route declaration is invalid")
        if (
            type(route.model) is not str
            or _IDENTIFIER.fullmatch(route.model) is None
            or type(route.effort) is not str
            or _IDENTIFIER.fullmatch(route.effort) is None
            or route.effort == "ultra"
        ):
            raise ValueError("route declaration is invalid")
        normalized.append(HostRoute(model=route.model, effort=route.effort))
    if len(set(normalized)) != len(normalized):
        raise ValueError("route declarations are duplicated")
    return tuple(sorted(normalized, key=lambda route: (route.model, route.effort)))


def _route_capability_id(route: HostRoute) -> str:
    return (
        "route-"
        + hashlib.sha256(
            _canonical_json({"model": route.model, "effort": route.effort}).encode(
                "utf-8"
            )
        ).hexdigest()
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "HostCapabilityFact",
    "HostCapabilityState",
    "HostRoute",
    "HostSchedulingFacts",
    "HostUnavailableFacts",
    "HostSession",
]
