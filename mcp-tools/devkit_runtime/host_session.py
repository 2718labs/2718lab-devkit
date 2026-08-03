"""Private fail-closed facts from one inherited host bridge session.

This module deliberately has no dispatch, listener, worktree, or persistence
operation.  Its only live inputs are a previously authenticated inherited
bridge and a same-process trusted callback that retains the verifier returned
by the official Codex quota provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from devkit_fastlane.scripts.codex_account_quota import QuotaSnapshotEvidence
from devkit_fastlane.scripts.fastlane_quota_balance import _verified_snapshot

from . import host_envelopes
from .host_bridge import (
    HostBridgeError,
    InheritedHandleHostBridge,
    OperationReceipt,
)

_NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH_PREFIX: Final = "sha256:"
_IDENTIFIER: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_REASON_SESSION_UNAVAILABLE: Final = "HOST_SESSION_UNAVAILABLE"
_REASON_QUOTA_EXPECTATION_UNAVAILABLE: Final = "HOST_QUOTA_EXPECTATION_UNAVAILABLE"
_REASON_QUOTA_UNAVAILABLE: Final = "HOST_QUOTA_UNAVAILABLE"
_REASON_CAPABILITY_UNAVAILABLE: Final = "HOST_CAPABILITY_UNAVAILABLE"
_REASON_EXECUTION_EVIDENCE_UNAVAILABLE: Final = "HOST_EXECUTION_EVIDENCE_UNAVAILABLE"


@dataclass(frozen=True)
class HostQuotaAttestation:
    """Private, same-process bindings for one bridge-delivered quota snapshot."""

    request_id: str
    account_id_hash: str
    source_id_hash: str
    main_limit_id: str
    spark_limit_id: str
    capacity_hash: str
    snapshot_seq: int
    evidence: QuotaSnapshotEvidence = field(repr=False)


@dataclass(frozen=True)
class HostQuotaExpectation:
    """Trusted constructor-time identities that a quota session cannot rebind."""

    account_id_hash: str
    source_id_hash: str
    key_id: str
    main_limit_id: str
    spark_limit_id: str
    capacity_hash: str
    ledger_epoch: int
    active_lease_set_hash: str
    snapshot_seq_high_water: int


@dataclass(frozen=True)
class HostQuotaFacts:
    """Bearer-free facts admitted only after exact private attestation."""

    request_id: str
    account_id_hash: str
    source_id_hash: str
    main_limit_id: str
    spark_limit_id: str
    snapshot_hash: str
    snapshot_seq: int
    snapshot: Mapping[str, Any]


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


@dataclass(frozen=True)
class _CapabilityRecord:
    """Process-private binding retained for terminal validation only."""

    fact: HostCapabilityFact
    binding: host_envelopes.EnvelopeBinding = field(repr=False)


QuotaEvidenceResolver: TypeAlias = Callable[
    [str, Mapping[str, object]], HostQuotaAttestation | None
]
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
    snapshot_hash: str
    snapshot_seq: int
    quota_binding_hash: str
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
        quota_evidence_resolver: QuotaEvidenceResolver,
        clock: Callable[[], float],
        quota_expectation: HostQuotaExpectation | None = None,
        compiler_evidence_provider: CompilerEvidenceProvider | None = None,
        compiler_invocation_resolver: CompilerInvocationResolver | None = None,
    ) -> None:
        self._bridge = bridge
        self._quota_evidence_resolver = quota_evidence_resolver
        self._compiler_evidence_provider = (
            compiler_evidence_provider if callable(compiler_evidence_provider) else None
        )
        self._compiler_invocation_resolver = (
            compiler_invocation_resolver
            if callable(compiler_invocation_resolver)
            else None
        )
        self._compiler_evidence_lock = RLock()
        self._compiler_evidence: dict[_CompilerEvidenceHandle, _CompilerInvocation] = {}
        self._compiler_materials: dict[_CompilerPreparation, _CompilerInvocation] = {}
        self._burned_preparation_ids: set[str] = set()
        self._has_fresh_quota = False
        self._fresh_quota: HostQuotaFacts | None = None
        self._fresh_quota_issued_at: float | None = None
        self._fresh_quota_valid_until: float | None = None
        self._clock = clock
        self._last_trusted_clock: float | None = None
        try:
            self._quota_expectation = _normalize_quota_expectation(quota_expectation)
        except (TypeError, ValueError):
            self._quota_expectation = None
        self._closed = False
        self._frozen = (
            bridge is None
            or not callable(quota_evidence_resolver)
            or not callable(clock)
            or not bridge.is_available
        )
        self._seen_snapshot_hashes: set[str] = set()
        self._last_snapshot_seq: int | None = (
            None
            if self._quota_expectation is None
            else self._quota_expectation.snapshot_seq_high_water
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
        quota_evidence_resolver: QuotaEvidenceResolver,
        clock: Callable[[], float],
        quota_expectation: HostQuotaExpectation | None = None,
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
            quota_evidence_resolver=quota_evidence_resolver,
            clock=clock,
            quota_expectation=quota_expectation,
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
            self._has_fresh_quota = False
            self._fresh_quota = None
            self._fresh_quota_issued_at = None
            self._fresh_quota_valid_until = None
            self._compiler_evidence.clear()
            self._compiler_materials.clear()
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
            quota = self._fresh_quota
            issued_at = self._fresh_quota_issued_at
            valid_until = self._fresh_quota_valid_until
            if (
                not self.is_available
                or not self._has_fresh_quota
                or quota is None
                or issued_at is None
                or valid_until is None
            ):
                return _NO_SAFE_WORK
            try:
                now = self._read_trusted_clock()
            except (TypeError, ValueError):
                return _NO_SAFE_WORK
            self._has_fresh_quota = False
            self._fresh_quota = None
            self._fresh_quota_issued_at = None
            self._fresh_quota_valid_until = None
            expires_at = min(issued_at + 120, valid_until)
            if now >= expires_at:
                return _NO_SAFE_WORK
            quota_binding_hash = _hash(
                {
                    "account_id_hash": quota.account_id_hash,
                    "main_limit_id": quota.main_limit_id,
                    "request_id": quota.request_id,
                    "snapshot_hash": quota.snapshot_hash,
                    "snapshot_seq": quota.snapshot_seq,
                    "source_id_hash": quota.source_id_hash,
                    "spark_limit_id": quota.spark_limit_id,
                }
            )
            try:
                binding = _normalized_compiler_invocation_binding(
                    binding_resolver(preparation_id)
                )
            except Exception:
                return _NO_SAFE_WORK
            preparation = _CompilerPreparation()
            self._compiler_materials[preparation] = _CompilerInvocation(
                schema="2718lab-devkit/compiler-invocation-v1",
                preparation_id=preparation_id,
                request_hash=binding.request_hash,
                reasoning_effort=binding.reasoning_effort,
                verified_route_result_hashes=binding.verified_route_result_hashes,
                verified_lease_scope_bindings=binding.verified_lease_scope_bindings,
                issued_at=issued_at,
                expires_at=expires_at,
                snapshot_hash=quota.snapshot_hash,
                snapshot_seq=quota.snapshot_seq,
                quota_binding_hash=quota_binding_hash,
                binding_hash=_hash(
                    {
                        "expires_at": expires_at,
                        "issued_at": issued_at,
                        "preparation_id": preparation_id,
                        "quota_binding_hash": quota_binding_hash,
                        "reasoning_effort": binding.reasoning_effort,
                        "request_hash": binding.request_hash,
                        "route_result_hashes": binding.verified_route_result_hashes,
                        "schema": "2718lab-devkit/compiler-invocation-v1",
                        "lease_scope_bindings": binding.verified_lease_scope_bindings,
                    }
                ),
            )
            try:
                provider_material = provider(preparation)
            except Exception:
                self._compiler_materials.pop(preparation, None)
                return _NO_SAFE_WORK
            if (
                self._closed
                or self._frozen
                or not self.is_available
                or provider_material is not preparation
            ):
                self._compiler_materials.pop(preparation, None)
                return _NO_SAFE_WORK
            material = self._compiler_materials.pop(preparation, None)
            if material is None:
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

    def read_quota(self) -> HostQuotaFacts | str:
        """Return one fresh, fully bound quota fact set or ``NO_SAFE_WORK``."""

        if not self.is_available:
            self._record_unavailable((), _REASON_SESSION_UNAVAILABLE)
            return _NO_SAFE_WORK
        if self._quota_expectation is None:
            self._record_unavailable((), _REASON_QUOTA_EXPECTATION_UNAVAILABLE)
            return _NO_SAFE_WORK
        bridge = self._bridge
        assert bridge is not None
        request_id = f"quota-{secrets.token_hex(16)}"
        try:
            bridge.request_quota_snapshot(request_id=request_id)
            transport_snapshot = copy.deepcopy(
                bridge.receive_quota_snapshot(request_id=request_id)
            )
            attestation = self._quota_evidence_resolver(
                request_id, copy.deepcopy(transport_snapshot)
            )
            facts = self._verify_quota(
                request_id=request_id,
                snapshot=transport_snapshot,
                attestation=attestation,
            )
            issued_at = self._read_trusted_clock()
            valid_until = _valid_until_timestamp(facts.snapshot)
        except Exception:
            self._record_unavailable((), _REASON_QUOTA_UNAVAILABLE)
            self._freeze()
            return _NO_SAFE_WORK
        with self._compiler_evidence_lock:
            if self._closed or self._frozen or not self.is_available:
                return _NO_SAFE_WORK
            self._has_fresh_quota = True
            self._fresh_quota = facts
            self._fresh_quota_issued_at = issued_at
            self._fresh_quota_valid_until = valid_until
        return facts

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

    def _verify_quota(
        self,
        *,
        request_id: str,
        snapshot: Mapping[str, object],
        attestation: HostQuotaAttestation | None,
    ) -> HostQuotaFacts:
        expectation = self._quota_expectation
        if expectation is None:
            raise ValueError("quota expectation is unavailable")
        if type(attestation) is not HostQuotaAttestation:
            raise ValueError("quota attestation is unavailable")
        evidence = attestation.evidence
        if type(evidence) is not QuotaSnapshotEvidence:
            raise ValueError("quota evidence is unavailable")
        if attestation.request_id != request_id or dict(evidence.snapshot) != dict(
            snapshot
        ):
            raise ValueError("quota evidence is not request-bound")
        evaluation_time = _utc_z(self._read_trusted_clock())
        verified, _ = _verified_snapshot(
            snapshot,
            trusted_key_resolver=evidence.key_resolver,
            evaluation_time_utc_z=evaluation_time,
        )
        source = _mapping(verified["source"])
        capacity = _mapping(verified["capacity"])
        snapshot_hash = _required_hash(verified["snapshot_hash"])
        snapshot_seq = _required_positive_int(verified["snapshot_seq"])
        if (
            attestation.source_id_hash != source.get("source_id_hash")
            or source.get("source_id_hash") != _OFFICIAL_QUOTA_SOURCE_ID_HASH
            or attestation.source_id_hash != expectation.source_id_hash
            or evidence.key_id != source.get("key_id")
            or evidence.key_id != expectation.key_id
            or not _is_hash(attestation.account_id_hash)
            or attestation.account_id_hash != evidence.account_id_hash
            or attestation.account_id_hash != expectation.account_id_hash
            or attestation.main_limit_id != evidence.main_limit_id
            or attestation.spark_limit_id != evidence.spark_limit_id
            or attestation.main_limit_id != "codex"
            or attestation.main_limit_id != expectation.main_limit_id
            or not isinstance(attestation.spark_limit_id, str)
            or not attestation.spark_limit_id
            or attestation.spark_limit_id != expectation.spark_limit_id
            or attestation.capacity_hash != _hash(capacity)
            or _hash(capacity) != expectation.capacity_hash
            or capacity.get("ledger_epoch") != expectation.ledger_epoch
            or capacity.get("active_lease_set_hash")
            != expectation.active_lease_set_hash
            or attestation.snapshot_seq != snapshot_seq
        ):
            raise ValueError("quota evidence binding is invalid")
        if snapshot_hash in self._seen_snapshot_hashes or (
            self._last_snapshot_seq is not None
            and snapshot_seq <= self._last_snapshot_seq
        ):
            raise ValueError("quota snapshot was replayed")
        self._seen_snapshot_hashes.add(snapshot_hash)
        self._last_snapshot_seq = snapshot_seq
        return HostQuotaFacts(
            request_id=request_id,
            account_id_hash=attestation.account_id_hash,
            source_id_hash=_required_hash(source["source_id_hash"]),
            main_limit_id=attestation.main_limit_id,
            spark_limit_id=attestation.spark_limit_id,
            snapshot_hash=snapshot_hash,
            snapshot_seq=snapshot_seq,
            snapshot=_readonly_mapping(verified),
        )

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
            self._has_fresh_quota = False
            self._fresh_quota = None
            self._fresh_quota_issued_at = None
            self._fresh_quota_valid_until = None
            self._compiler_evidence.clear()
            self._compiler_materials.clear()
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


def _mapping(value: object) -> Mapping[str, Any]:
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


def _normalize_quota_expectation(
    value: HostQuotaExpectation | None,
) -> HostQuotaExpectation | None:
    if value is None:
        return None
    if type(value) is not HostQuotaExpectation:
        raise ValueError("quota expectation is invalid")
    if (
        not _is_hash(value.account_id_hash)
        or not _is_hash(value.source_id_hash)
        or not _is_hash(value.key_id)
        or value.main_limit_id != "codex"
        or type(value.spark_limit_id) is not str
        or _IDENTIFIER.fullmatch(value.spark_limit_id) is None
        or not _is_hash(value.capacity_hash)
        or not _is_hash(value.active_lease_set_hash)
    ):
        raise ValueError("quota expectation identity is invalid")
    ledger_epoch = _required_nonnegative_int(value.ledger_epoch)
    snapshot_seq_high_water = _required_nonnegative_int(value.snapshot_seq_high_water)
    return HostQuotaExpectation(
        account_id_hash=value.account_id_hash,
        source_id_hash=value.source_id_hash,
        key_id=value.key_id,
        main_limit_id=value.main_limit_id,
        spark_limit_id=value.spark_limit_id,
        capacity_hash=value.capacity_hash,
        ledger_epoch=ledger_epoch,
        active_lease_set_hash=value.active_lease_set_hash,
        snapshot_seq_high_water=snapshot_seq_high_water,
    )


def _trusted_clock(clock: Callable[[], float]) -> float:
    value = float(clock())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("trusted host clock is invalid")
    return value


def _valid_until_timestamp(snapshot: Mapping[str, Any]) -> float:
    value = snapshot.get("valid_until_utc_z")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("signed quota validity is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("signed quota validity is invalid") from error
    if parsed.tzinfo != UTC:
        raise ValueError("signed quota validity is invalid")
    timestamp = parsed.timestamp()
    if not math.isfinite(timestamp):
        raise ValueError("signed quota validity is invalid")
    return timestamp


def _utc_z(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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


_OFFICIAL_QUOTA_SOURCE_ID_HASH: Final = _hash("codex-app-server-account-rate-limits")


def _readonly_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(value) for key, value in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(dict(value))
    assert isinstance(frozen, Mapping)
    return frozen


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
    "HostQuotaAttestation",
    "HostQuotaExpectation",
    "HostQuotaFacts",
    "HostCapabilityFact",
    "HostCapabilityState",
    "HostRoute",
    "HostSchedulingFacts",
    "HostUnavailableFacts",
    "HostSession",
    "QuotaEvidenceResolver",
]
