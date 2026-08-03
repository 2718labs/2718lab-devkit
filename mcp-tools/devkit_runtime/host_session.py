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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
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
    evidence: QuotaSnapshotEvidence


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


@dataclass(frozen=True)
class HostSchedulingFacts:
    """Exact pairs that a scheduler may consume without a fallback decision."""

    routes: tuple[HostRoute, ...]


QuotaEvidenceResolver: TypeAlias = Callable[
    [str, Mapping[str, object]], HostQuotaAttestation | None
]


class HostSession:
    """Consume one authenticated inherited bridge without any fallback path."""

    def __init__(
        self,
        *,
        bridge: InheritedHandleHostBridge | None,
        quota_evidence_resolver: QuotaEvidenceResolver,
        clock: Callable[[], float],
    ) -> None:
        self._bridge = bridge
        self._quota_evidence_resolver = quota_evidence_resolver
        self._clock = clock
        self._closed = False
        self._frozen = (
            bridge is None
            or not callable(quota_evidence_resolver)
            or not callable(clock)
            or not bridge.is_available
        )
        self._seen_snapshot_hashes: set[str] = set()
        self._last_snapshot_seq: int | None = None
        self._attested_capabilities: dict[str, HostCapabilityFact] = {}

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None,
        platform: str | None,
        quota_evidence_resolver: QuotaEvidenceResolver,
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
            quota_evidence_resolver=quota_evidence_resolver,
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

    def close(self) -> None:
        """Close the owned private transport once; no session can be revived."""

        if self._closed:
            return
        self._closed = True
        self._frozen = True
        if self._bridge is not None:
            self._bridge.close()

    def read_quota(self) -> HostQuotaFacts | str:
        """Return one fresh, fully bound quota fact set or ``NO_SAFE_WORK``."""

        if not self.is_available:
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
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK
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

        if not self.is_available:
            return _NO_SAFE_WORK
        bridge = self._bridge
        assert bridge is not None
        try:
            declared = self.declare_routes(routes)
            if not declared:
                raise ValueError("route declarations are invalid")
            probe = bridge.send_capability_probe(
                binding=binding,
                capability_names=tuple(fact.capability_id for fact in declared),
                now=now,
            )
            report = bridge.receive_capability_report(probe=probe, now=now)
            reported_hashes = _mapping(report["capability_hashes"])
            facts = tuple(
                HostCapabilityFact(
                    route=fact.route,
                    capability_id=fact.capability_id,
                    state=HostCapabilityState.ATTESTED,
                    attestation_hash=_required_hash(reported_hashes[fact.capability_id]),
                )
                for fact in declared
            )
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK
        self._attested_capabilities = {
            fact.capability_id: fact for fact in facts
        }
        return facts

    def scheduling_facts(
        self, facts: tuple[HostCapabilityFact, ...] | list[HostCapabilityFact] | object
    ) -> HostSchedulingFacts | str:
        """Expose the exact attested tuples or fail closed without substituting one."""

        if not self.is_available:
            return _NO_SAFE_WORK
        try:
            if type(facts) not in {tuple, list} or not facts:
                raise ValueError("capability facts are invalid")
            verified: list[HostRoute] = []
            for fact in facts:
                if type(fact) is not HostCapabilityFact:
                    raise ValueError("capability fact is invalid")
                if fact.state not in {
                    HostCapabilityState.ATTESTED,
                    HostCapabilityState.EXECUTED,
                }:
                    raise ValueError("capability fact is not attested")
                if self._attested_capabilities.get(fact.capability_id) != fact:
                    raise ValueError("capability fact is foreign")
                verified.append(fact.route)
            return HostSchedulingFacts(routes=tuple(verified))
        except Exception:
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

        if not self.is_available:
            return _NO_SAFE_WORK
        bridge = self._bridge
        assert bridge is not None
        try:
            if (
                type(fact) is not HostCapabilityFact
                or fact.state is not HostCapabilityState.ATTESTED
                or self._attested_capabilities.get(fact.capability_id) != fact
                or type(predecessor) is not OperationReceipt
            ):
                raise ValueError("execution evidence is invalid")
            bridge.receive_terminal_result(predecessor=predecessor, now=now)
            executed = HostCapabilityFact(
                route=fact.route,
                capability_id=fact.capability_id,
                state=HostCapabilityState.EXECUTED,
                attestation_hash=fact.attestation_hash,
            )
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK
        self._attested_capabilities[executed.capability_id] = executed
        return executed

    def _verify_quota(
        self,
        *,
        request_id: str,
        snapshot: Mapping[str, object],
        attestation: HostQuotaAttestation | None,
    ) -> HostQuotaFacts:
        if type(attestation) is not HostQuotaAttestation:
            raise ValueError("quota attestation is unavailable")
        evidence = attestation.evidence
        if type(evidence) is not QuotaSnapshotEvidence:
            raise ValueError("quota evidence is unavailable")
        if attestation.request_id != request_id or dict(evidence.snapshot) != dict(snapshot):
            raise ValueError("quota evidence is not request-bound")
        evaluation_time = _utc_z(_trusted_clock(self._clock))
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
            or evidence.key_id != source.get("key_id")
            or not _is_hash(attestation.account_id_hash)
            or attestation.account_id_hash != evidence.account_id_hash
            or attestation.main_limit_id != evidence.main_limit_id
            or attestation.spark_limit_id != evidence.spark_limit_id
            or attestation.main_limit_id != "codex"
            or not isinstance(attestation.spark_limit_id, str)
            or not attestation.spark_limit_id
            or attestation.capacity_hash != _hash(capacity)
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

    def _freeze(self) -> None:
        self._frozen = True
        if self._bridge is not None:
            self._bridge.close()


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


def _trusted_clock(clock: Callable[[], float]) -> float:
    value = float(clock())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("trusted host clock is invalid")
    return value


def _utc_z(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _hash(value: object) -> str:
    return _HASH_PREFIX + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


_OFFICIAL_QUOTA_SOURCE_ID_HASH: Final = _hash(
    "codex-app-server-account-rate-limits"
)


def _readonly_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(value) for key, value in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(dict(value))
    assert isinstance(frozen, Mapping)
    return frozen


def _normalized_routes(
    routes: tuple[HostRoute, ...] | list[HostRoute],
) -> tuple[HostRoute, ...]:
    if type(routes) not in {tuple, list} or not routes or len(routes) > 16:
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
    return "route-" + hashlib.sha256(
        _canonical_json({"model": route.model, "effort": route.effort}).encode("utf-8")
    ).hexdigest()


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
    "HostQuotaFacts",
    "HostCapabilityFact",
    "HostCapabilityState",
    "HostRoute",
    "HostSchedulingFacts",
    "HostSession",
    "QuotaEvidenceResolver",
]
