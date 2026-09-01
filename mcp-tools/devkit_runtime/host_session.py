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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import Event, RLock, Thread, current_thread
from typing import Final, TypeAlias, cast

from . import host_envelopes
from .host_bridge import (
    FastLaneRefillRegistryRequest,
    HostBridgeError,
    InheritedHandleHostBridge,
    OperationReceipt,
    StorageAdmissionReceipt,
    _validate_storage_admission_profile,
    build_storage_admission_request,
)
from .host_scheduler_topology_adapter import (
    HostAuthoritativeActionFact,
    HostSchedulerTopologyFact,
    construct_host_scheduler_topology,
)
from .storage_intent import StorageIntent, StorageIntentError, parse_storage_intent

_NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH_PREFIX: Final = "sha256:"
_IDENTIFIER: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_FAST_LANE_TASK_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
_RAW_HASH: Final = re.compile(r"[0-9a-f]{64}\Z")
_REASON_SESSION_UNAVAILABLE: Final = "HOST_SESSION_UNAVAILABLE"
_REASON_CAPABILITY_UNAVAILABLE: Final = "HOST_CAPABILITY_UNAVAILABLE"
_REASON_EXECUTION_EVIDENCE_UNAVAILABLE: Final = "HOST_EXECUTION_EVIDENCE_UNAVAILABLE"
_MAX_HOST_WRITER_ACTIONS: Final = 9
_MAX_HOST_READER_ACTIONS: Final = 189
_STORAGE_PROFILE_SCHEMA: Final = "2718lab-devkit/storage-profile-v1"
_STORAGE_PROFILE_FIELDS: Final = frozenset(
    {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "task_id",
        "source_plan_hash",
        "index_attestation_hash",
        "execution_context_hash",
        "repository_identity",
        "workspace_manifest_hash",
        "cargo_lock_hash",
        "toolchain_digest",
        "target_triple",
        "profile",
        "features_hash",
        "build_env_class",
        "profile_hash",
        "attestation_hash",
    }
)
_STORAGE_PROFILE_BUILD_ENV_CLASSES: Final = frozenset(
    {"managed_read_only", "managed_workspace", "disabled", "external"}
)
_STORAGE_PROFILE_SCALAR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_STORAGE_INTENT_SCHEMA: Final = "2718lab.storage.intent.v1"
_STORAGE_TARGET_SCHEMA: Final = "2718lab.storage.target.v1"
_STORAGE_DESCRIPTOR_FIELDS: Final = (
    "repository_identity",
    "workspace_manifest_hash",
    "cargo_lock_hash",
    "toolchain_digest",
    "target_triple",
    "profile",
    "features_hash",
    "build_env_class",
)


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
    authoritative_actions: tuple[HostAuthoritativeActionFact, ...]


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
    dispatch_facts: tuple[object, ...] = ()
    dispatch_binding_hashes: tuple[str, ...] = ()
    storage_budget_bindings: tuple[tuple[str, int, int], ...] = field(
        default=(), repr=False
    )
    storage_profiles: tuple[dict[str, object], ...] = field(default=(), repr=False)
    registry_binding_hash: str | None = None
    evidence_expires_at: int | None = None


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
    dispatch_facts: tuple[object, ...]
    dispatch_binding_hashes: tuple[str, ...]
    issued_at: float
    expires_at: float
    binding_hash: str
    registry_binding_hash: str | None = None
    storage_budget_bindings: tuple[tuple[str, int, int], ...] = field(
        default=(), repr=False
    )
    storage_profiles: tuple[dict[str, object], ...] = field(default=(), repr=False)
    storage_intents: tuple[dict[str, object], ...] = field(default=(), repr=False)
    storage_intent_hashes: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class _CompilerRequestContext:
    call_intent_hash: str
    request_hash: str
    reasoning_effort: str
    requested_routes: tuple[HostRoute, ...]
    routing_registry_binding_hash: str
    assignment_skeletons: tuple[dict[str, object], ...] = field(repr=False)
    project_index_attestation_refs: tuple[dict[str, object], ...] = field(repr=False)
    storage_task_ids: tuple[str, ...] = ()
    storage_budget_bindings: tuple[tuple[str, int, int], ...] = field(
        default=(), repr=False
    )


@dataclass(frozen=True)
class _HostCapabilitySnapshotV2:
    call_intent_hash: str
    preparation_id: str
    host_capabilities: dict[str, object] = field(repr=False)
    scheduler_facts: dict[str, object] = field(repr=False)
    report_hash: str
    expires_at: int


@dataclass(frozen=True)
class _RoutingAttestationSnapshot:
    call_intent_hash: str
    preparation_id: str
    routing_requests: tuple[dict[str, object], ...] = field(repr=False)
    attestations: tuple[dict[str, object], ...] = field(repr=False)
    routing_request_set_hash: str
    routing_registry_binding_hash: str
    expires_at: int


@dataclass(frozen=True)
class _PendingFastLaneTerminal:
    expected: dict[str, object] = field(repr=False)
    lease_expires_at: int = field(repr=False)


@dataclass(frozen=True)
class _CompletedStorageProfile:
    """Local completion tied to a verified preparation and this exact bridge."""

    profile: dict[str, object] = field(repr=False)
    bridge: InheritedHandleHostBridge = field(repr=False)
    expires_at: int
    requested_bytes: int
    requested_files: int


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
        host_action_capacity_resolver: Callable[[], object] | None = None,
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
        self._host_action_capacity_resolver = (
            host_action_capacity_resolver
            if callable(host_action_capacity_resolver)
            else None
        )
        self._compiler_evidence_lock = RLock()
        self._compiler_evidence: dict[_CompilerEvidenceHandle, _CompilerInvocation] = {}
        self._compiler_request_contexts: dict[str, _CompilerRequestContext] = {}
        self._completed_storage_profiles: dict[str, _CompletedStorageProfile] = {}
        self._capability_snapshots_v2: dict[
            tuple[str, str], _HostCapabilitySnapshotV2
        ] = {}
        self._preparation_expiry_caps: dict[str, int] = {}
        self._routing_attestation_snapshots: dict[
            tuple[str, str], _RoutingAttestationSnapshot
        ] = {}
        self._pending_fast_lane_terminals: dict[
            tuple[str, str], _PendingFastLaneTerminal
        ] = {}
        # One reader owns the framed inbound sequence for all active batches;
        # callbacks are multiplexed by the authenticated batch hash.
        # Batch identities share one session-level reader; this set is only
        # bookkeeping for callbacks, never a per-batch receiver registry.
        self._fast_lane_active_batches: set[str] = set()
        self._fast_lane_terminal_thread: Thread | None = None
        self._fast_lane_refill_callbacks: dict[
            str, Callable[[Mapping[str, object]], object]
        ] = {}
        self._fast_lane_terminal_stop = Event()
        self._fast_lane_refill_receipts: dict[str, object] = {}
        self._fast_lane_refill_registries: dict[str, object] = {}
        self._project_index_query_attestations: dict[str, dict[str, object]] = {}
        self._burned_preparation_ids: set[str] = set()
        self._clock = clock
        self._last_trusted_clock: float | None = None
        self._closed = False
        self._frozen = bridge is None or not callable(clock) or not bridge.is_available
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
        session = cls(
            bridge=bridge,
            clock=clock,
        )
        if bridge is not None:
            session._compiler_evidence_provider = lambda preparation: preparation
            session._compiler_invocation_resolver = (
                session._resolve_bridge_compiler_invocation
            )
        return session

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

    @property
    def has_active_fast_lane_terminal_receiver(self) -> bool:
        """Whether this session currently owns its multiplexed terminal reader."""

        with self._compiler_evidence_lock:
            thread = self._fast_lane_terminal_thread
            return thread is not None and thread.is_alive()

    def resolve_capability_snapshot_v2(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        expires_at_ceiling: int | None = None,
    ) -> _HostCapabilitySnapshotV2 | None:
        """Resolve and retain one generation-bound V5 Host/scheduler snapshot."""

        bridge = self._bridge
        if (
            bridge is None
            or not self.is_available
            or type(call_intent_hash) is not str
            or len(call_intent_hash) != 64
            or any(
                character not in "0123456789abcdef" for character in call_intent_hash
            )
            or type(preparation_id) is not str
            or _IDENTIFIER.fullmatch(preparation_id) is None
            or (
                expires_at_ceiling is not None
                and (type(expires_at_ceiling) is not int or expires_at_ceiling <= 0)
            )
        ):
            return None
        key = (call_intent_hash, preparation_id)
        if key in self._capability_snapshots_v2:
            return None
        try:
            now = int(self._read_trusted_clock())
            probe = bridge.send_capability_probe_v2(
                call_intent_hash=call_intent_hash,
                preparation_id=preparation_id,
                now=now,
            )
            report = bridge.receive_capability_report_v2(probe=probe, now=now)
            host = cast(dict[str, object], report["host_capabilities"])
            scheduler = cast(dict[str, object], report["scheduler_facts"])
            snapshot_expires_at = probe.expires_at
            if expires_at_ceiling is not None:
                if now >= expires_at_ceiling:
                    return None
                snapshot_expires_at = min(snapshot_expires_at, expires_at_ceiling)
            if snapshot_expires_at <= now:
                return None
            snapshot = _HostCapabilitySnapshotV2(
                call_intent_hash=call_intent_hash,
                preparation_id=preparation_id,
                host_capabilities=dict(host),
                scheduler_facts=dict(scheduler),
                report_hash=_required_hash(report["report_hash"]),
                expires_at=snapshot_expires_at,
            )
        except Exception:
            return None
        self._capability_snapshots_v2[key] = snapshot
        self._preparation_expiry_caps[preparation_id] = snapshot.expires_at
        return snapshot

    def resolve_routing_attestations(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        routing_requests: Sequence[Mapping[str, object]],
    ) -> _RoutingAttestationSnapshot | None:
        """Resolve one V5 route set after the same-generation capability report."""

        bridge = self._bridge
        key = (call_intent_hash, preparation_id)
        capability_snapshot = self._capability_snapshots_v2.get(key)
        if (
            bridge is None
            or not self.is_available
            or capability_snapshot is None
            or key in self._routing_attestation_snapshots
        ):
            return None
        try:
            now = int(self._read_trusted_clock())
            if now >= capability_snapshot.expires_at:
                return None
            request = bridge.send_routing_attestation_request(
                call_intent_hash=call_intent_hash,
                preparation_id=preparation_id,
                routing_requests=routing_requests,
                now=now,
            )
            response = bridge.receive_routing_attestation_response(
                request=request, now=now
            )
            raw_attestations = response["attestations"]
            assert type(raw_attestations) is list
            snapshot = _RoutingAttestationSnapshot(
                call_intent_hash=call_intent_hash,
                preparation_id=preparation_id,
                routing_requests=tuple(dict(item) for item in request.routing_requests),
                attestations=tuple(dict(item) for item in raw_attestations),
                routing_request_set_hash=request.routing_request_set_hash,
                routing_registry_binding_hash=_required_hash(
                    response["routing_registry_binding_hash"]
                ),
                expires_at=request.expires_at,
            )
        except Exception:
            return None
        self._routing_attestation_snapshots[key] = snapshot
        return snapshot

    def close(self) -> None:
        """Close the owned private transport once; no session can be revived."""

        # Admission owns the business lock across its single-reader round trip.
        # Wake it first without closing/reusing descriptors while it restores
        # transport mode; only close fds after that owner releases this lock.
        if self._bridge is not None:
            self._bridge.cancel_read()
        with self._compiler_evidence_lock:
            if self._closed:
                return
            self._closed = True
            self._frozen = True
            self._compiler_evidence.clear()
            self._compiler_request_contexts.clear()
            self._completed_storage_profiles.clear()
            self._capability_snapshots_v2.clear()
            self._preparation_expiry_caps.clear()
            self._routing_attestation_snapshots.clear()
            self._pending_fast_lane_terminals.clear()
            self._fast_lane_refill_callbacks.clear()
            self._fast_lane_active_batches.clear()
            self._project_index_query_attestations.clear()
            self._fast_lane_terminal_stop.set()
            terminal_thread = self._fast_lane_terminal_thread
        if self._bridge is not None:
            # Closing the descriptor unblocks the single multiplexed receiver;
            # join it here so no detached reader can outlive this session.
            self._bridge.close()
        if (
            terminal_thread is not None
            and terminal_thread is not current_thread()
            and terminal_thread.is_alive()
        ):
            terminal_thread.join(timeout=0.25)

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
            try:
                binding = _normalized_compiler_invocation_binding(
                    binding_resolver(preparation_id)
                )
            except Exception:
                return _NO_SAFE_WORK
            expires_at = (
                binding.evidence_expires_at
                if binding.evidence_expires_at is not None
                else issued_at + 120
            )
            expiry_cap = self._preparation_expiry_caps.pop(preparation_id, None)
            if not issued_at < expires_at <= issued_at + 120 or (
                expiry_cap is not None and expires_at > expiry_cap
            ):
                return _NO_SAFE_WORK
            preparation = _CompilerPreparation()
            material = _CompilerInvocation(
                schema="2718lab-devkit/compiler-invocation-v2",
                preparation_id=preparation_id,
                request_hash=binding.request_hash,
                reasoning_effort=binding.reasoning_effort,
                verified_route_result_hashes=binding.verified_route_result_hashes,
                verified_lease_scope_bindings=binding.verified_lease_scope_bindings,
                dispatch_facts=binding.dispatch_facts,
                dispatch_binding_hashes=binding.dispatch_binding_hashes,
                issued_at=issued_at,
                expires_at=expires_at,
                binding_hash="",
                registry_binding_hash=binding.registry_binding_hash,
                storage_budget_bindings=binding.storage_budget_bindings,
                storage_profiles=binding.storage_profiles,
            )
            material = replace(
                material,
                binding_hash=_hash(_compiler_invocation_binding_material(material)),
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
            # Only the actual authenticated profile round trip can enroll a
            # reference; an injected resolver/provider cannot mint authority.
            bridge = self._bridge
            if (
                bridge is not None
                and binding_resolver == self._resolve_bridge_compiler_invocation
            ):
                budgets = {
                    task: (byte_count, files)
                    for task, byte_count, files in material.storage_budget_bindings
                }
                completed_profiles: dict[str, _CompletedStorageProfile] = {}
                for profile in material.storage_profiles:
                    if not bridge.has_completed_storage_profile(profile):
                        return _NO_SAFE_WORK
                    attestation = cast(str, profile["attestation_hash"])
                    byte_count, files = budgets[cast(str, profile["task_id"])]
                    completed = _CompletedStorageProfile(
                        profile=dict(profile),
                        bridge=bridge,
                        expires_at=int(expires_at),
                        requested_bytes=byte_count,
                        requested_files=files,
                    )
                    previous = completed_profiles.get(
                        attestation
                    ) or self._completed_storage_profiles.get(attestation)
                    if previous is not None and previous != completed:
                        return _NO_SAFE_WORK
                    completed_profiles[attestation] = completed
                self._completed_storage_profiles.update(completed_profiles)
            self._compiler_evidence[evidence] = material
            return evidence

    def bind_compiler_request(
        self,
        *,
        preparation_id: str,
        call_intent_hash: str,
        request_hash: str,
        reasoning_effort: str,
        requested_routes: tuple[HostRoute, ...],
        assignment_skeletons: tuple[dict[str, object], ...],
        project_index_attestation_refs: tuple[dict[str, object], ...],
        routing_registry_binding_hash: str,
        storage_task_ids: tuple[str, ...] = (),
        storage_budget_bindings: tuple[tuple[str, int, int], ...] = (),
    ) -> bool:
        """Bind public request identity before a private registry round trip."""

        with self._compiler_evidence_lock:
            if (
                self._closed
                or self._frozen
                or self._compiler_invocation_resolver
                != self._resolve_bridge_compiler_invocation
                or _IDENTIFIER.fullmatch(preparation_id) is None
                or type(call_intent_hash) is not str
                or len(call_intent_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in call_intent_hash
                )
                or not _is_hash(request_hash)
                or not _is_hash(routing_registry_binding_hash)
                or reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}
                or type(storage_task_ids) is not tuple
                or len(storage_task_ids) > 16
                or any(
                    type(task_id) is not str
                    or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
                    for task_id in storage_task_ids
                )
                or len(set(storage_task_ids)) != len(storage_task_ids)
                or type(storage_budget_bindings) is not tuple
            ):
                return False
            try:
                normalized_routes = _normalized_routes(requested_routes)
                normalized_storage_budgets = _normalized_storage_budget_bindings(
                    storage_budget_bindings
                )
                skeleton_task_ids = tuple(
                    _required_fast_lane_task_id(_mapping(item).get("task_id"))
                    for item in assignment_skeletons
                )
                trusted_now = int(self._read_trusted_clock())
            except (TypeError, ValueError):
                return False
            if (
                bool(storage_task_ids) != bool(normalized_storage_budgets)
                or (storage_task_ids and storage_task_ids != skeleton_task_ids)
                or (
                    normalized_storage_budgets
                    and {
                        task_id
                        for task_id, _requested_bytes, _requested_files in normalized_storage_budgets
                    }
                    != set(skeleton_task_ids)
                )
            ):
                return False
            routing_snapshot = self._routing_attestation_snapshots.get(
                (call_intent_hash, preparation_id)
            )
            if (
                routing_snapshot is None
                or routing_snapshot.routing_registry_binding_hash
                != routing_registry_binding_hash
                or trusted_now >= routing_snapshot.expires_at
            ):
                return False
            if preparation_id in self._compiler_request_contexts:
                return False
            self._compiler_request_contexts[preparation_id] = _CompilerRequestContext(
                call_intent_hash=call_intent_hash,
                request_hash=request_hash,
                reasoning_effort=reasoning_effort,
                requested_routes=normalized_routes,
                assignment_skeletons=assignment_skeletons,
                project_index_attestation_refs=project_index_attestation_refs,
                routing_registry_binding_hash=routing_registry_binding_hash,
                storage_task_ids=storage_task_ids,
                storage_budget_bindings=normalized_storage_budgets,
            )
            return True

    def _resolve_bridge_compiler_invocation(
        self, preparation_id: str
    ) -> _CompilerInvocationBinding | None:
        context = self._compiler_request_contexts.pop(preparation_id, None)
        bridge = self._bridge
        if context is None or bridge is None or not self.is_available:
            return None
        now = int(self._read_trusted_clock())
        request = bridge.send_compiler_evidence_request(
            preparation_id=preparation_id,
            call_intent_hash=context.call_intent_hash,
            request_hash=context.request_hash,
            reasoning_effort=context.reasoning_effort,
            requested_route_pairs=tuple(
                {"model": route.model, "effort": route.effort}
                for route in context.requested_routes
            ),
            assignment_skeletons=context.assignment_skeletons,
            project_index_attestation_refs=context.project_index_attestation_refs,
            routing_registry_binding_hash=context.routing_registry_binding_hash,
            now=now,
        )

        response = bridge.receive_compiler_evidence_response(request=request, now=now)
        from .fastlane_host_adapter import _dispatch_fact_from_mapping

        facts_value = response["dispatch_facts"]
        assert type(facts_value) is list
        route_hashes = cast(list[object], response["verified_route_result_hashes"])
        lease_hashes = cast(list[object], response["verified_lease_scope_bindings"])
        dispatch_hashes = cast(list[object], response["dispatch_binding_hashes"])
        dispatch_facts = tuple(
            _dispatch_fact_from_mapping(value) for value in facts_value
        )
        storage_profiles: tuple[dict[str, object], ...] = ()
        if context.storage_task_ids:
            skeleton_task_ids = tuple(
                _required_fast_lane_task_id(_mapping(item).get("task_id"))
                for item in context.assignment_skeletons
            )
            if skeleton_task_ids != context.storage_task_ids:
                return None
            profiles: list[dict[str, object]] = []
            for skeleton, index_ref, fact in zip(
                context.assignment_skeletons,
                context.project_index_attestation_refs,
                dispatch_facts,
                strict=True,
            ):
                skeleton_mapping = _mapping(skeleton)
                index_mapping = _mapping(index_ref)
                task_id = _required_fast_lane_task_id(skeleton_mapping.get("task_id"))
                source_plan_hash = _required_hash(
                    skeleton_mapping.get("source_plan_hash")
                )
                index_attestation_hash = _required_hash(
                    index_mapping.get("attestation_hash")
                )
                if (
                    index_mapping.get("task_id") != task_id
                    or fact.task_id != task_id
                    or fact.source_plan_hash != source_plan_hash
                    or fact.index_context_hash
                    != skeleton_mapping.get("index_context_hash")
                ):
                    return None
                profile_request = bridge.send_storage_profile_request(
                    call_intent_hash=context.call_intent_hash,
                    preparation_id=preparation_id,
                    task_id=task_id,
                    source_plan_hash=source_plan_hash,
                    index_attestation_hash=index_attestation_hash,
                )
                profiles.append(
                    bridge.receive_storage_profile_response(request=profile_request)
                )
            storage_profiles = tuple(profiles)
        return _CompilerInvocationBinding(
            request_hash=_required_hash(response["request_hash"]),
            reasoning_effort=str(response["reasoning_effort"]),
            verified_route_result_hashes=tuple(
                _required_hash(value) for value in route_hashes
            ),
            verified_lease_scope_bindings=tuple(
                _required_hash(value) for value in lease_hashes
            ),
            dispatch_facts=dispatch_facts,
            dispatch_binding_hashes=tuple(
                _required_hash(value) for value in dispatch_hashes
            ),
            registry_binding_hash=_required_hash(response["registry_binding_hash"]),
            evidence_expires_at=_required_positive_int(response["expires_at"]),
            storage_budget_bindings=context.storage_budget_bindings,
            storage_profiles=storage_profiles,
        )

    def send_project_index_attestation(
        self, attestation: Mapping[str, object]
    ) -> dict[str, object] | None:
        """Emit a persisted index attestation only when this bridge is live."""

        bridge = self._bridge
        if bridge is None or not self.is_available:
            return None
        now = int(self._read_trusted_clock())
        sent = bridge.send_project_index_attestation(attestation=attestation, now=now)
        if sent.get("operation") == "query":
            correlation_id = sent.get("correlation_id")
            if type(correlation_id) is not str:
                self._freeze()
                return None
            self._project_index_query_attestations[correlation_id] = dict(sent)
        return sent

    def project_index_query_attestation(
        self, *, correlation_id: str
    ) -> dict[str, object] | None:
        """Resolve a Host-preselected, same-generation query attestation once."""

        try:
            now = int(self._read_trusted_clock())
        except (TypeError, ValueError):
            return None
        value = self._project_index_query_attestations.pop(correlation_id, None)
        if (
            value is None
            or value.get("operation") != "query"
            or value.get("correlation_id") != correlation_id
            or type(value.get("expires_at")) is not int
            or not now < cast(int, value["expires_at"])
        ):
            return None
        return dict(value)

    def request_storage_admission(
        self, intent: StorageIntent, *, profile_attestation_hash: str
    ) -> StorageAdmissionReceipt | str:
        """Ask Host before dispatch; no profile hash or local cache admits work.

        Profile-v1 keeps Host generation/index deadlines opaque. Their final
        validation belongs to Rust's completed-profile/Sent records. Locally
        the same bridge, accepted preparation, exact profile and budgets are
        mandatory, and the Host decision cannot extend preparation authority.
        """
        with self._compiler_evidence_lock:
            bridge = self._bridge
            if not self.is_available or bridge is None:
                return "STORAGE_STAT_UNAVAILABLE"
            terminal_thread = self._fast_lane_terminal_thread
            if terminal_thread is not None and terminal_thread.is_alive():
                return "STORAGE_STAT_UNAVAILABLE"
            if type(profile_attestation_hash) is not str:
                return "STORAGE_TARGET_KEY_INVALID"
            completed = self._completed_storage_profiles.get(profile_attestation_hash)
            if completed is None or completed.bridge is not bridge:
                return "STORAGE_TARGET_KEY_INVALID"
            try:
                now = int(self._read_trusted_clock())
                if (
                    now >= completed.expires_at
                    or not bridge.has_completed_storage_profile(completed.profile)
                ):
                    return "STORAGE_STAT_UNAVAILABLE"
                if type(intent) is not StorageIntent:
                    return "STORAGE_TARGET_KEY_INVALID"
                parsed = parse_storage_intent(intent.to_dict())
                _validate_storage_admission_profile(parsed, completed.profile)
                if (parsed.requested_bytes, parsed.requested_files) != (
                    completed.requested_bytes,
                    completed.requested_files,
                ):
                    return "STORAGE_LEASE_CONFLICT"
                request = build_storage_admission_request(
                    parsed, profile_attestation_hash=profile_attestation_hash
                )
                receipt = bridge.request_storage_admission(
                    request,
                    now=now,
                    expires_at=completed.expires_at,
                    clock=self._read_trusted_clock,
                )
                # Re-read the trusted clock after blocking I/O. A valid frame
                # received after its deadline must not authorize a writer.
                if int(self._read_trusted_clock()) >= receipt.expires_at:
                    return "STORAGE_STAT_UNAVAILABLE"
                return receipt
            except StorageIntentError as error:
                return error.code
            except HostBridgeError as error:
                if error.code in {
                    "STORAGE_TARGET_KEY_INVALID",
                    "STORAGE_LEASE_CONFLICT",
                }:
                    return error.code
                self._freeze()
                return "STORAGE_STAT_UNAVAILABLE"
            except (TypeError, ValueError):
                self._freeze()
                return "STORAGE_STAT_UNAVAILABLE"

    def storage_profiles_for_compiler_evidence(
        self, evidence: object
    ) -> tuple[dict[str, object], ...] | str:
        """Expose copies of profile facts only through a live opaque handle.

        This is deliberately narrower than consuming compiler evidence: the
        adapter must construct and bind its local storage-intent proof before
        the one-shot compiler material can be consumed for dispatch.
        """

        with self._compiler_evidence_lock:
            if (
                self._closed
                or self._frozen
                or type(evidence) is not _CompilerEvidenceHandle
            ):
                return _NO_SAFE_WORK
            material = self._compiler_evidence.get(evidence)
            if material is None:
                return _NO_SAFE_WORK
            try:
                now = self._read_trusted_clock()
            except (TypeError, ValueError):
                return _NO_SAFE_WORK
            if (
                now >= material.expires_at
                or material.binding_hash
                != _hash(_compiler_invocation_binding_material(material))
                or bool(material.storage_budget_bindings)
                != bool(material.storage_profiles)
            ):
                return _NO_SAFE_WORK
            return tuple(dict(profile) for profile in material.storage_profiles)

    def bind_storage_intent_proof(
        self,
        evidence: object,
        *,
        storage_budgets: object,
        storage_intents: object,
    ) -> bool:
        """Seal post-Host budgets and intent hashes into compiler state once.

        The Host profile exchange is already complete at this point.  This
        method accepts no profile replacement: it validates the adapter's
        intents against the retained Host profiles and dispatch facts, then
        folds the exact budgets and intents into the private invocation hash.
        """

        with self._compiler_evidence_lock:
            if (
                self._closed
                or self._frozen
                or type(evidence) is not _CompilerEvidenceHandle
            ):
                return False
            material = self._compiler_evidence.get(evidence)
            if material is None:
                return False
            try:
                now = self._read_trusted_clock()
                budget_bindings = _normalized_storage_budget_bindings(storage_budgets)
                if (
                    now >= material.expires_at
                    or not budget_bindings
                    or budget_bindings != material.storage_budget_bindings
                    or not material.storage_profiles
                    or material.storage_intents
                    or material.storage_intent_hashes
                    or material.binding_hash
                    != _hash(_compiler_invocation_binding_material(material))
                ):
                    return False
                intents = _normalized_storage_intent_proof(
                    storage_intents,
                    budget_bindings=budget_bindings,
                    profiles=material.storage_profiles,
                    dispatch_facts=material.dispatch_facts,
                    preparation_id=material.preparation_id,
                )
                intent_hashes = tuple(
                    _required_hash(intent["storage_intent_hash"]) for intent in intents
                )
                rebound = replace(
                    material,
                    storage_intents=intents,
                    storage_intent_hashes=intent_hashes,
                    binding_hash="",
                )
                rebound = replace(
                    rebound,
                    binding_hash=_hash(_compiler_invocation_binding_material(rebound)),
                )
            except (KeyError, TypeError, ValueError):
                return False
            self._compiler_evidence[evidence] = rebound
            return True

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
            if now >= material.expires_at or material.binding_hash != _hash(
                _compiler_invocation_binding_material(material)
            ):
                return _NO_SAFE_WORK
            return material

    def compiler_evidence_expires_at(self, evidence: object) -> int | None:
        """Return only the expiry of a still-live opaque session handle."""

        with self._compiler_evidence_lock:
            if type(evidence) is not _CompilerEvidenceHandle:
                return None
            material = self._compiler_evidence.get(evidence)
            if material is None or not math.isfinite(material.expires_at):
                return None
            return int(material.expires_at)

    def send_fast_lane_dispatch_batch(
        self,
        *,
        batch: Mapping[str, object],
        binding: host_envelopes.EnvelopeBinding,
        correlation_id: str,
        now: int,
        call_intent_hash: str | None = None,
        preparation_id: str | None = None,
        refill_callback: Callable[[Mapping[str, object]], object] | None = None,
    ) -> OperationReceipt | str:
        """Forward a compiled batch only on this session's authenticated bridge."""

        bridge = self._bridge
        if not self.is_available or bridge is None:
            return _NO_SAFE_WORK
        try:
            # Validate and publish the whole batch under the session lock.  A
            # second dispatch may arrive while the receiver is blocked, but it
            # can never race the pending-map update or the bridge sequence.
            with self._compiler_evidence_lock:
                if self._closed or self._frozen:
                    return _NO_SAFE_WORK
                if (
                    type(call_intent_hash) is not str
                    or len(call_intent_hash) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in call_intent_hash
                    )
                    or type(preparation_id) is not str
                    or _IDENTIFIER.fullmatch(preparation_id) is None
                ):
                    raise ValueError("missing Fast Lane terminal binding")
                assignments = batch.get("assignments")
                batch_hash = batch.get("batch_hash")
                if (
                    type(assignments) is not list
                    or not assignments
                    or not _is_hash(batch_hash)
                ):
                    raise ValueError("invalid Fast Lane terminal binding")
                pending: list[tuple[tuple[str, str], _PendingFastLaneTerminal]] = []
                seen_tasks: set[str] = set()
                for assignment in assignments:
                    if type(assignment) is not dict:
                        raise ValueError("invalid Fast Lane terminal binding")
                    route = assignment.get("route")
                    if type(route) is not dict:
                        raise ValueError("invalid Fast Lane terminal binding")
                    expected = {
                        "call_intent_hash": call_intent_hash,
                        "preparation_id": preparation_id,
                        "batch_hash": batch_hash,
                        "task_id": assignment.get("task_id"),
                        "lease_id": assignment.get("lease_id"),
                        "lease_epoch": assignment.get("lease_epoch"),
                        "task_version": assignment.get("task_version"),
                        "assignment_token": assignment.get("assignment_token"),
                        "dispatch_binding_hash": assignment.get(
                            "dispatch_binding_hash"
                        ),
                        "routing_result_hash": route.get("routing_result_hash"),
                        "worktree_identity": assignment.get("worktree_identity"),
                        "worktree_base": assignment.get("worktree_base"),
                        "integration_head": assignment.get("integration_head"),
                        "predecessor_hash": assignment.get("predecessor_hash"),
                    }
                    task_id = assignment.get("task_id")
                    if type(task_id) is not str or task_id in seen_tasks:
                        raise ValueError("invalid Fast Lane terminal binding")
                    seen_tasks.add(task_id)
                    key = (cast(str, batch_hash), task_id)
                    if key in self._pending_fast_lane_terminals:
                        raise ValueError("duplicate Fast Lane terminal binding")
                    pending.append(
                        (
                            key,
                            _PendingFastLaneTerminal(
                                expected=expected,
                                # The envelope binding is host-issued and is
                                # the only expiry available on the dispatch
                                # wire.  Terminal receipts cannot extend it.
                                lease_expires_at=binding.expires_at,
                            ),
                        )
                    )
                receipt = bridge.send_fast_lane_dispatch_batch(
                    batch=batch,
                    binding=binding,
                    correlation_id=correlation_id,
                    now=now,
                )
                self._pending_fast_lane_terminals.update(pending)
                if (
                    refill_callback is not None
                    and not self.start_fast_lane_terminal_receiver(
                        batch_hash=cast(str, batch_hash),
                        refill_callback=refill_callback,
                    )
                ):
                    raise ValueError("Fast Lane terminal receiver was not started")
                return receipt
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK

    def send_fast_lane_refill_registry(
        self,
        *,
        call_intent_hash: str,
        preparation_id: str,
        source_plan_hash: str,
        index_context_hash: str,
        routing_registry_binding_hash: str,
        source_plan_task_ids: Sequence[str],
        initial_skeletons: Sequence[Mapping[str, object]],
        remaining_skeletons: Sequence[Mapping[str, object]],
        index_attestation_refs: Sequence[Mapping[str, object]],
        skeleton_package_hash: str,
        now: int,
    ) -> FastLaneRefillRegistryRequest | str:
        """Publish one host-authenticated queue for successor Fast Lane waves."""

        bridge = self._bridge
        if bridge is None or not self.is_available:
            return _NO_SAFE_WORK
        try:
            with self._compiler_evidence_lock:
                if self._closed or self._frozen:
                    return _NO_SAFE_WORK
                routing_snapshot = self._routing_attestation_snapshots.get(
                    (call_intent_hash, preparation_id)
                )
                if (
                    routing_snapshot is None
                    or routing_snapshot.routing_registry_binding_hash
                    != routing_registry_binding_hash
                    or type(now) is not int
                    or now < 0
                    or now >= routing_snapshot.expires_at
                ):
                    return _NO_SAFE_WORK
                request = bridge.send_fast_lane_refill_registry_request(
                    call_intent_hash=call_intent_hash,
                    preparation_id=preparation_id,
                    source_plan_hash=source_plan_hash,
                    index_context_hash=index_context_hash,
                    routing_registry_binding_hash=routing_registry_binding_hash,
                    source_plan_task_ids=source_plan_task_ids,
                    initial_skeletons=initial_skeletons,
                    remaining_skeletons=remaining_skeletons,
                    index_attestation_refs=index_attestation_refs,
                    skeleton_package_hash=skeleton_package_hash,
                    now=now,
                )
                self._fast_lane_refill_registries[request.queue_registry_hash] = request
                return request
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK

    def receive_fast_lane_worker_terminal(
        self,
        *,
        correlation_id: str,
        batch_hash: str,
        task_id: str,
        accepted_event_seq: int,
        refill_trigger_hash: str,
    ) -> dict[str, object] | str:
        """Consume and acknowledge one stored batch assignment, retaining peers."""

        bridge = self._bridge
        key = (batch_hash, task_id)
        try:
            with self._compiler_evidence_lock:
                pending = self._pending_fast_lane_terminals.get(key)
                if (
                    bridge is None
                    or not self.is_available
                    or pending is None
                    or (
                        self._fast_lane_terminal_thread is not None
                        and self._fast_lane_terminal_thread.is_alive()
                    )
                ):
                    return _NO_SAFE_WORK
                now = int(self._read_trusted_clock())
                terminal = bridge.receive_fast_lane_worker_terminal_result(
                    correlation_id=correlation_id,
                    expected=pending.expected,
                    expires_at=pending.lease_expires_at,
                    now=now,
                )
                ack = bridge.send_fast_lane_worker_terminal_ack(
                    terminal_result=terminal,
                    correlation_id=correlation_id,
                    accepted_event_seq=accepted_event_seq,
                    refill_trigger_hash=refill_trigger_hash,
                )
        except Exception:
            self._freeze()
            return _NO_SAFE_WORK
        with self._compiler_evidence_lock:
            self._pending_fast_lane_terminals.pop(key, None)
        return ack

    def start_fast_lane_terminal_receiver(
        self,
        *,
        batch_hash: str,
        refill_callback: Callable[[Mapping[str, object]], object],
    ) -> bool:
        """Register a batch on the session's shared terminal receiver.

        There is exactly one inbound framed reader per session.  Additional
        batches attach their callback to that reader instead of competing for
        the transport sequence.
        """

        if not _is_hash(batch_hash) or not callable(refill_callback):
            return False
        with self._compiler_evidence_lock:
            if (
                not self.is_available
                or not any(
                    key[0] == batch_hash for key in self._pending_fast_lane_terminals
                )
                or batch_hash in self._fast_lane_refill_callbacks
            ):
                return False
            self._fast_lane_refill_callbacks[batch_hash] = refill_callback
            thread = self._fast_lane_terminal_thread
            if thread is not None and thread.is_alive():
                self._fast_lane_active_batches.add(batch_hash)
                return True
            self._fast_lane_active_batches.clear()
            thread = Thread(
                target=self._run_fast_lane_terminal_receiver,
                name="devkit-fastlane-terminal-multiplex",
                daemon=False,
            )
            self._fast_lane_terminal_thread = thread
            self._fast_lane_active_batches.add(batch_hash)
            try:
                thread.start()
            except Exception:
                self._fast_lane_terminal_thread = None
                self._fast_lane_active_batches.discard(batch_hash)
                self._fast_lane_refill_callbacks.pop(batch_hash, None)
                raise
        return True

    def _run_fast_lane_terminal_receiver(self) -> None:
        bridge = self._bridge
        if bridge is None:
            self._freeze()
            return
        try:
            while not self._fast_lane_terminal_stop.is_set():
                with self._compiler_evidence_lock:
                    pending = {
                        key: dict(value.expected)
                        for key, value in self._pending_fast_lane_terminals.items()
                    }
                    expires_at_by_assignment = {
                        key: value.lease_expires_at
                        for key, value in self._pending_fast_lane_terminals.items()
                    }
                if not pending:
                    return
                now = int(self._read_trusted_clock())
                correlation_id, terminal = (
                    bridge.receive_next_fast_lane_worker_terminal_result(
                        expected_by_assignment=pending,
                        expires_at_by_assignment=expires_at_by_assignment,
                        now=now,
                    )
                )
                batch_hash = cast(str, terminal["batch_hash"])
                task_id = cast(str, terminal["task_id"])
                event_seq = cast(int, terminal["event_seq"])
                receipt_hash = cast(str, terminal["terminal_receipt_hash"])
                remaining = sorted(
                    key[1]
                    for key in pending
                    if key[0] == batch_hash and key != (batch_hash, task_id)
                )
                descriptor: dict[str, object] = {
                    "schema": "team-efficiency/fast-lane-refill-trigger-v1",
                    "trigger": "slot_terminal_event",
                    "dispatch_at": "next_host_dispatch_boundary",
                    "polling": False,
                    "batch_hash": batch_hash,
                    "task_id": task_id,
                    "terminal_receipt_hash": receipt_hash,
                    "accepted_event_seq": event_seq,
                    "remaining_task_ids": remaining,
                }
                refill_trigger_hash = _hash(descriptor)
                bridge.send_fast_lane_worker_terminal_ack(
                    terminal_result=terminal,
                    correlation_id=correlation_id,
                    accepted_event_seq=event_seq,
                    refill_trigger_hash=refill_trigger_hash,
                )
                with self._compiler_evidence_lock:
                    self._pending_fast_lane_terminals.pop((batch_hash, task_id), None)
                    refill_callback = self._fast_lane_refill_callbacks.get(batch_hash)
                if refill_callback is not None:
                    result = refill_callback(
                        {**descriptor, "refill_trigger_hash": refill_trigger_hash}
                    )
                    with self._compiler_evidence_lock:
                        self._fast_lane_refill_receipts[receipt_hash] = result
        except Exception:
            if not self._closed:
                self._freeze()
        finally:
            with self._compiler_evidence_lock:
                current = self._fast_lane_terminal_thread
                if current is current_thread():
                    self._fast_lane_terminal_thread = None
                    self._fast_lane_active_batches.clear()
                    if not self._pending_fast_lane_terminals:
                        self._fast_lane_refill_callbacks.clear()

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

    def admit_relay_actions(self, actions: object) -> bool:
        """Admit only exact Relay actions within live Host physical-slot capacity."""

        from .fastlane_host_intent import (
            NO_SAFE_WORK,
            parse_relay_host_scheduler_slot,
        )

        with self._compiler_evidence_lock:
            capacity_resolver = self._host_action_capacity_resolver
            if (
                self._closed
                or self._frozen
                or not self.is_available
                or capacity_resolver is None
                or type(actions) is not list
            ):
                return False
            if not actions:
                return True
            try:
                writer_capacity, reader_capacity = _normalized_host_action_capacity(
                    capacity_resolver()
                )
            except Exception:
                return False
            if len(actions) > _MAX_HOST_WRITER_ACTIONS + _MAX_HOST_READER_ACTIONS:
                return False
            writer_count = 0
            reader_count = 0
            seen_task_ids: set[str] = set()
            resolved_topology: HostResolvedSchedulerTopology | None = None
            for action in actions:
                if type(action) is not dict:
                    return False
                task_id = action.get("task_id")
                task_contract = action.get("task_contract")
                if (
                    action.get("kind") != "codex.spawn_agent"
                    or type(task_id) is not str
                    or _IDENTIFIER.fullmatch(task_id) is None
                    or task_id in seen_task_ids
                    or type(task_contract) is not dict
                    or task_contract.get("task_id") != task_id
                ):
                    return False
                task_kind = task_contract.get("kind")
                if task_kind not in {"implementation", "prewarm", "design"}:
                    return False
                slot = parse_relay_host_scheduler_slot(
                    action.get("relay_host_scheduler_slot")
                )
                if slot == NO_SAFE_WORK:
                    return False
                if resolved_topology is None:
                    resolved = self.resolve_relay_host_scheduler_slot(
                        action["relay_host_scheduler_slot"]
                    )
                    if not isinstance(resolved, HostResolvedSchedulerTopology):
                        return False
                    resolved_topology = resolved
                resolved = resolved_topology
                if resolved is None:
                    return False
                matching_groups = [
                    group
                    for group in resolved.groups
                    if group.scheduler_id == slot.scheduler_id
                    and group.coordinator_lease_id == slot.coordinator_lease_id
                    and group.worktree_identity == slot.worktree_identity
                    and group.relay_group_binding_hash == slot.group_binding_hash
                ]
                if (
                    resolved.relay_plan_hash != slot.plan_hash
                    or resolved.relay_topology_hash != slot.topology_hash
                    or len(matching_groups) != 1
                ):
                    return False
                group = matching_groups[0]
                if task_kind == "implementation":
                    if (
                        slot.read_only
                        or slot.writer_slot is None
                        or slot.writer_slot > len(group.writer_task_ids)
                        or group.writer_task_ids[slot.writer_slot - 1] != task_id
                    ):
                        return False
                    writer_count += 1
                    target = task_id
                elif task_kind == "prewarm":
                    target = task_contract.get("prewarm_for_task_id")
                    if (
                        not slot.read_only
                        or slot.writer_slot is not None
                        or task_id not in group.prewarm_task_ids
                        or type(target) is not str
                        or _IDENTIFIER.fullmatch(target) is None
                        or target not in group.writer_task_ids
                    ):
                        return False
                    reader_count += 1
                else:
                    design_target = task_contract.get("design_for_task_id")
                    if (
                        not slot.read_only
                        or slot.writer_slot is not None
                        or type(design_target) is not str
                        or _IDENTIFIER.fullmatch(design_target) is None
                        or design_target not in group.writer_task_ids
                    ):
                        return False
                    target = design_target
                    reader_count += 1
                if (
                    HostAuthoritativeActionFact(
                        plan_hash=slot.plan_hash,
                        task_id=task_id,
                        kind=task_kind,
                        target=target,
                        group_binding_hash=slot.group_binding_hash,
                    )
                    not in group.authoritative_actions
                ):
                    return False
                seen_task_ids.add(task_id)
            if resolved_topology is None:
                return False
            return (
                writer_count <= writer_capacity
                and reader_count <= reader_capacity
                and writer_count
                <= sum(group.attested_capacity for group in resolved_topology.groups)
            )

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
            canonical_actions_by_scheduler: dict[
                str, tuple[HostAuthoritativeActionFact, ...]
            ] = {}
            seen_task_ids: set[str] = set()
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
                    or type(fact.authoritative_actions) is not tuple
                    or len(fact.authoritative_actions) > 22
                    or any(
                        type(action) is not HostAuthoritativeActionFact
                        or not _is_hash(action.plan_hash)
                        or _IDENTIFIER.fullmatch(action.task_id) is None
                        or action.kind not in {"implementation", "prewarm", "design"}
                        or _IDENTIFIER.fullmatch(action.target) is None
                        or not _is_hash(action.group_binding_hash)
                        or action.plan_hash != fact.plan_hash
                        or action.group_binding_hash != fact.group_binding_hash
                        or (
                            action.kind == "implementation"
                            and (
                                action.task_id not in fact.writer_task_ids
                                or action.target != action.task_id
                            )
                        )
                        or (
                            action.kind == "prewarm"
                            and (
                                action.task_id not in fact.prewarm_task_ids
                                or action.target not in fact.writer_task_ids
                            )
                        )
                        or (
                            action.kind == "design"
                            and (
                                action.task_id in fact.writer_task_ids
                                or action.task_id in fact.prewarm_task_ids
                                or action.target not in fact.writer_task_ids
                            )
                        )
                        for action in fact.authoritative_actions
                    )
                    or len({action.task_id for action in fact.authoritative_actions})
                    != len(fact.authoritative_actions)
                    or fact.scheduler_id in facts_by_scheduler
                ):
                    raise ValueError("topology fact is invalid")
                canonical_actions = _canonical_authoritative_actions(
                    fact.authoritative_actions
                )
                fact_task_ids = {
                    *fact.writer_task_ids,
                    *fact.prewarm_task_ids,
                    *(
                        action.task_id
                        for action in canonical_actions
                        if action.kind == "design"
                    ),
                }
                if seen_task_ids.intersection(fact_task_ids):
                    raise ValueError("topology task identities are not unique")
                seen_task_ids.update(fact_task_ids)
                facts_by_scheduler[fact.scheduler_id] = fact
                canonical_actions_by_scheduler[fact.scheduler_id] = canonical_actions
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
                        authoritative_actions=canonical_actions_by_scheduler[
                            group.scheduler_id
                        ],
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
                            "authoritative_actions": [
                                {
                                    "plan_hash": action.plan_hash,
                                    "task_id": action.task_id,
                                    "kind": action.kind,
                                    "target": action.target,
                                    "group_binding_hash": action.group_binding_hash,
                                }
                                for action in group.authoritative_actions
                            ],
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
            self._compiler_request_contexts.clear()
            self._completed_storage_profiles.clear()
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


def _required_fast_lane_task_id(value: object) -> str:
    if type(value) is not str or _FAST_LANE_TASK_ID.fullmatch(value) is None:
        raise ValueError("expected Fast Lane task id")
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


def _optional_ordered_hash_tuple(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) <= 16
        and all(_is_hash(item) for item in value)
        and len(set(value)) == len(value)
    )


def _normalized_storage_budget_bindings(
    value: object,
) -> tuple[tuple[str, int, int], ...]:
    """Normalize exact task budgets retained outside the public skeleton."""

    if type(value) is not tuple or len(value) > 16:
        raise ValueError("compiler storage budgets are invalid")
    normalized: list[tuple[str, int, int]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 3:
            raise ValueError("compiler storage budget is invalid")
        task_id, requested_bytes, requested_files = item
        if (
            type(task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
            or type(requested_bytes) is not int
            or not 0 < requested_bytes <= (1 << 64) - 1
            or type(requested_files) is not int
            or not 0 < requested_files <= (1 << 64) - 1
        ):
            raise ValueError("compiler storage budget is invalid")
        normalized.append((task_id, requested_bytes, requested_files))
    normalized.sort(key=lambda item: item[0])
    if len({task_id for task_id, _bytes, _files in normalized}) != len(normalized):
        raise ValueError("compiler storage budgets are duplicated")
    return tuple(normalized)


def _normalized_storage_profiles(
    value: object,
) -> tuple[dict[str, object], ...]:
    if type(value) is not tuple or len(value) > 16:
        raise ValueError("compiler storage profiles are invalid")
    normalized: list[dict[str, object]] = []
    for profile in value:
        mapping = dict(_mapping(profile))
        if set(mapping) != _STORAGE_PROFILE_FIELDS:
            raise ValueError("compiler storage profile fields are invalid")
        if mapping.get("schema") != _STORAGE_PROFILE_SCHEMA:
            raise ValueError("compiler storage profile schema is invalid")
        call_intent_hash = mapping.get("call_intent_hash")
        preparation_id = mapping.get("preparation_id")
        task_id = mapping.get("task_id")
        if (
            type(call_intent_hash) is not str
            or _RAW_HASH.fullmatch(call_intent_hash) is None
            or type(preparation_id) is not str
            or _IDENTIFIER.fullmatch(preparation_id) is None
            or type(task_id) is not str
            or _FAST_LANE_TASK_ID.fullmatch(task_id) is None
        ):
            raise ValueError("compiler storage profile binding is invalid")
        target_triple = mapping.get("target_triple")
        if (
            type(target_triple) is not str
            or _STORAGE_PROFILE_SCALAR.fullmatch(target_triple) is None
            or mapping.get("profile") != "dev"
        ):
            raise ValueError("compiler storage profile scalar is invalid")
        for field_name in (
            "source_plan_hash",
            "index_attestation_hash",
            "execution_context_hash",
            "repository_identity",
            "workspace_manifest_hash",
            "cargo_lock_hash",
            "toolchain_digest",
            "features_hash",
            "profile_hash",
            "attestation_hash",
        ):
            _required_hash(mapping.get(field_name))
        if mapping.get("build_env_class") not in _STORAGE_PROFILE_BUILD_ENV_CLASSES:
            raise ValueError("compiler storage profile build environment is invalid")
        unsigned = {
            field_name: mapping[field_name]
            for field_name in _STORAGE_PROFILE_FIELDS
            if field_name not in {"profile_hash", "attestation_hash"}
        }
        if mapping["profile_hash"] != _hash(unsigned):
            raise ValueError("compiler storage profile hash is invalid")
        normalized.append(
            {
                field_name: mapping[field_name]
                for field_name in sorted(_STORAGE_PROFILE_FIELDS)
            }
        )
    task_ids = [cast(str, profile["task_id"]) for profile in normalized]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("compiler storage profile tasks are duplicated")
    return tuple(normalized)


def _normalized_storage_intent_proof(
    value: object,
    *,
    budget_bindings: tuple[tuple[str, int, int], ...],
    profiles: tuple[dict[str, object], ...],
    dispatch_facts: tuple[object, ...],
    preparation_id: str,
) -> tuple[dict[str, object], ...]:
    """Tie adapter-built intents back to this invocation's Host facts once."""

    if (
        type(value) is not tuple
        or not value
        or len(value) > 16
        or len(value) != len(profiles)
        or len(value) != len(dispatch_facts)
        or _IDENTIFIER.fullmatch(preparation_id) is None
    ):
        raise ValueError("compiler storage intent proof is invalid")
    budget_by_task = {
        task_id: (requested_bytes, requested_files)
        for task_id, requested_bytes, requested_files in budget_bindings
    }
    try:
        fact_task_ids = tuple(
            _required_fast_lane_task_id(getattr(fact, "task_id"))
            for fact in dispatch_facts
        )
        fact_source_plan_hashes = tuple(
            _required_hash(getattr(fact, "source_plan_hash")) for fact in dispatch_facts
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("compiler storage dispatch facts are invalid") from error
    if len(set(fact_task_ids)) != len(fact_task_ids) or set(budget_by_task) != set(
        fact_task_ids
    ):
        raise ValueError("compiler storage budget binding is invalid")

    from .storage_intent import parse_storage_intent

    normalized: list[dict[str, object]] = []
    for intent, profile, task_id, source_plan_hash in zip(
        value,
        profiles,
        fact_task_ids,
        fact_source_plan_hashes,
        strict=True,
    ):
        if type(intent) is not dict:
            raise ValueError("compiler storage intent is invalid")
        parsed = parse_storage_intent(intent)
        normalized_intent = parsed.to_dict()
        if normalized_intent != intent:
            raise ValueError("compiler storage intent is not canonical")
        if (
            profile.get("preparation_id") != preparation_id
            or profile.get("task_id") != task_id
            or profile.get("source_plan_hash") != source_plan_hash
            or parsed.task_id != task_id
            or parsed.plan_binding != source_plan_hash
            or parsed.context_hash != profile.get("execution_context_hash")
            or (parsed.requested_bytes, parsed.requested_files)
            != budget_by_task.get(task_id)
        ):
            raise ValueError("compiler storage intent binding is invalid")
        descriptor = {
            "schema": _STORAGE_TARGET_SCHEMA,
            "artifact_kind": "fastlane-task",
            **{
                field_name: profile.get(field_name)
                for field_name in _STORAGE_DESCRIPTOR_FIELDS
            },
        }
        if (
            parsed.to_dict().get("schema") != _STORAGE_INTENT_SCHEMA
            or parsed.to_dict().get("target_descriptor") != descriptor
        ):
            raise ValueError("compiler storage target binding is invalid")
        normalized.append(normalized_intent)
    return tuple(normalized)


def _compiler_invocation_binding_material(
    value: _CompilerInvocation,
) -> dict[str, object]:
    """Render private invocation proof without extending legacy wire schemas."""

    material: dict[str, object] = {
        "preparation_id": value.preparation_id,
        "request_hash": value.request_hash,
        "reasoning_effort": value.reasoning_effort,
        "verified_route_result_hashes": value.verified_route_result_hashes,
        "verified_lease_scope_bindings": value.verified_lease_scope_bindings,
        "issued_at": value.issued_at,
        "expires_at": value.expires_at,
    }
    if value.registry_binding_hash is not None:
        material["registry_binding_hash"] = value.registry_binding_hash
    if value.dispatch_binding_hashes:
        material["dispatch_binding_hashes"] = value.dispatch_binding_hashes
    if value.storage_budget_bindings:
        material["storage_budget_bindings"] = [
            {
                "task_id": task_id,
                "bytes": requested_bytes,
                "files": requested_files,
            }
            for task_id, requested_bytes, requested_files in value.storage_budget_bindings
        ]
    if value.storage_profiles:
        material["storage_profiles"] = [
            dict(profile) for profile in value.storage_profiles
        ]
    if value.storage_intents:
        material["storage_intents"] = [dict(intent) for intent in value.storage_intents]
    if value.storage_intent_hashes:
        material["storage_intent_hashes"] = value.storage_intent_hashes
    return material


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
        or type(value.dispatch_facts) is not tuple
        or len(value.dispatch_facts) > 16
        or not _optional_ordered_hash_tuple(value.dispatch_binding_hashes)
        or len(value.dispatch_facts) != len(value.dispatch_binding_hashes)
        or type(value.storage_budget_bindings) is not tuple
        or type(value.storage_profiles) is not tuple
        or len(value.storage_profiles) > 16
        or (
            value.registry_binding_hash is not None
            and not _is_hash(value.registry_binding_hash)
        )
        or (
            value.evidence_expires_at is not None
            and type(value.evidence_expires_at) is not int
        )
    ):
        raise ValueError("compiler invocation binding is invalid")
    storage_budget_bindings = _normalized_storage_budget_bindings(
        value.storage_budget_bindings
    )
    storage_profiles = _normalized_storage_profiles(value.storage_profiles)
    if bool(storage_budget_bindings) != bool(storage_profiles):
        raise ValueError("compiler storage proof is incomplete")
    if storage_budget_bindings and {
        task_id
        for task_id, _requested_bytes, _requested_files in storage_budget_bindings
    } != {cast(str, profile["task_id"]) for profile in storage_profiles}:
        raise ValueError("compiler storage profile budget bindings are invalid")
    return _CompilerInvocationBinding(
        request_hash=value.request_hash,
        reasoning_effort=value.reasoning_effort,
        verified_route_result_hashes=value.verified_route_result_hashes,
        verified_lease_scope_bindings=value.verified_lease_scope_bindings,
        dispatch_facts=value.dispatch_facts,
        dispatch_binding_hashes=value.dispatch_binding_hashes,
        storage_budget_bindings=storage_budget_bindings,
        storage_profiles=storage_profiles,
        registry_binding_hash=value.registry_binding_hash,
        evidence_expires_at=value.evidence_expires_at,
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
        value.dispatch_facts,
        value.dispatch_binding_hashes,
        value.storage_budget_bindings,
        tuple(_hash(profile) for profile in value.storage_profiles),
        tuple(_hash(intent) for intent in value.storage_intents),
        value.storage_intent_hashes,
        value.issued_at,
        value.expires_at,
        value.binding_hash,
        value.registry_binding_hash,
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


def _canonical_authoritative_actions(
    actions: tuple[HostAuthoritativeActionFact, ...],
) -> tuple[HostAuthoritativeActionFact, ...]:
    return tuple(
        sorted(
            actions,
            key=lambda action: (
                action.task_id,
                action.kind,
                action.target,
                action.plan_hash,
                action.group_binding_hash,
            ),
        )
    )


def _normalized_host_action_capacity(value: object) -> tuple[int, int]:
    if type(value) is not dict or set(value) != {"writer_capacity", "reader_capacity"}:
        raise ValueError("host action capacity is invalid")
    writer_capacity = value["writer_capacity"]
    reader_capacity = value["reader_capacity"]
    if (
        type(writer_capacity) is not int
        or type(reader_capacity) is not int
        or not 0 <= writer_capacity <= _MAX_HOST_WRITER_ACTIONS
        or not 0 <= reader_capacity <= _MAX_HOST_READER_ACTIONS
    ):
        raise ValueError("host action capacity is invalid")
    return writer_capacity, reader_capacity


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
