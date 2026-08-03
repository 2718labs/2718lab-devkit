"""Pure, closed validation for role-scoped private host envelopes.

These records intentionally carry closed metadata values and content-addressed
references only.  They are not a transport, capability bearer, filesystem
message, or conversation container.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn

HOST_ENVELOPE_SCHEMA: Final = "2718lab-devkit/host-envelope-v1"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_ROLE_BY_KIND: Final = {
    "coordinator_assignment": ("coordinator", "worker"),
    "worker_terminal_result": ("worker", "coordinator"),
    "peer_evidence_handoff": ("peer", "peer"),
}
_ENVELOPE_FIELDS: Final = frozenset(
    {
        "schema",
        "kind",
        "sender_role",
        "recipient_role",
        "task_id",
        "lease_epoch",
        "assignment_token",
        "dispatch_context_hash",
        "route_hash",
        "expires_at",
        "payload",
    }
)
_MAX_BYTES_BY_KIND: Final = {
    "coordinator_assignment": 32 * 1024,
    "worker_terminal_result": 24 * 1024,
    "peer_evidence_handoff": 16 * 1024,
}
_MAX_TEXT_ITEMS: Final = 16
_MAX_ARTIFACT_REFS: Final = 16
_MAX_DIGEST_REFS: Final = 32
_MAX_RISKS: Final = 8
_ALLOWED_TEXT_VALUES: Final = {
    "assignment": frozenset(
        {"assignment.verify", "assignment.inspect", "assignment.integrate", "assignment.retry"}
    ),
    "context": frozenset(
        {"context.artifact-refs", "context.digest-refs", "context.binding-verified"}
    ),
    "result": frozenset({"result.verified", "result.failed", "result.blocked"}),
    "risk.detail": frozenset({"risk.none", "risk.bounded", "risk.unverified"}),
    "dependency": frozenset(
        {"dependency.artifact-refs", "dependency.digest-refs", "dependency.predecessor-verified"}
    ),
    "evidence": frozenset(
        {"evidence.artifact-digest", "evidence.digest-verified", "evidence.binding-verified"}
    ),
}


class HostEnvelopeError(ValueError):
    """Stable private-envelope validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EnvelopeBinding:
    """Immutable context shared by all envelope directions."""

    task_id: str
    lease_epoch: int
    assignment_token: str
    dispatch_context_hash: str
    route_hash: str
    expires_at: int


@dataclass(frozen=True)
class EnvelopeExpectation:
    """Expected recipient-side binding for one incoming envelope."""

    kind: str
    binding: EnvelopeBinding
    correlation_id: str | None = None
    peer_capability: str | None = None


def render_envelope(
    *,
    kind: str,
    binding: EnvelopeBinding,
    payload: Mapping[str, object],
    now: int,
) -> dict[str, object]:
    """Render one canonical, role-fixed envelope without performing I/O."""

    roles = _roles_for_kind(kind)
    normalized_binding = _validate_binding(binding, now=now)
    normalized_payload = _validate_payload(kind, payload)
    envelope = {
        "schema": HOST_ENVELOPE_SCHEMA,
        "kind": kind,
        "sender_role": roles[0],
        "recipient_role": roles[1],
        **normalized_binding,
        "payload": normalized_payload,
    }
    _validate_size(kind, envelope)
    return envelope


def validate_envelope(
    envelope: Mapping[str, object],
    *,
    now: int,
    expected: EnvelopeExpectation | None = None,
) -> dict[str, object]:
    """Validate and normalize exactly one role-scoped envelope."""

    if type(envelope) is not dict or set(envelope) != _ENVELOPE_FIELDS:
        _invalid()
    schema = envelope["schema"]
    kind = envelope["kind"]
    sender_role = envelope["sender_role"]
    recipient_role = envelope["recipient_role"]
    if schema != HOST_ENVELOPE_SCHEMA or type(kind) is not str:
        _invalid()
    roles = _roles_for_kind(kind)
    if sender_role != roles[0] or recipient_role != roles[1]:
        _invalid()
    binding = EnvelopeBinding(
        task_id=_required_str(envelope, "task_id"),
        lease_epoch=_required_int(envelope, "lease_epoch"),
        assignment_token=_required_str(envelope, "assignment_token"),
        dispatch_context_hash=_required_str(envelope, "dispatch_context_hash"),
        route_hash=_required_str(envelope, "route_hash"),
        expires_at=_required_int(envelope, "expires_at"),
    )
    normalized_binding = _validate_binding(binding, now=now)
    normalized_payload = _validate_payload(kind, envelope["payload"])
    normalized = {
        "schema": HOST_ENVELOPE_SCHEMA,
        "kind": kind,
        "sender_role": roles[0],
        "recipient_role": roles[1],
        **normalized_binding,
        "payload": normalized_payload,
    }
    _validate_size(kind, normalized)
    if expected is not None:
        _validate_expectation(normalized, expected, now=now)
    return normalized


def canonical_envelope_bytes(
    envelope: Mapping[str, object],
    *,
    now: int,
    expected: EnvelopeExpectation | None = None,
) -> bytes:
    """Return canonical bytes only for a valid, role-scoped envelope."""

    return _canonical_bytes(validate_envelope(envelope, now=now, expected=expected))


def envelope_hash(
    envelope: Mapping[str, object],
    *,
    now: int,
    expected: EnvelopeExpectation | None = None,
) -> str:
    """Return a canonical SHA-256 reference for a valid envelope."""

    return "sha256:" + hashlib.sha256(
        canonical_envelope_bytes(envelope, now=now, expected=expected)
    ).hexdigest()


def binding_mapping(binding: EnvelopeBinding, *, now: int) -> dict[str, object]:
    """Return the normalized binding for private bridge packet validation."""

    return _validate_binding(binding, now=now)


def validate_binding_mapping(
    value: Mapping[str, object], *, now: int
) -> EnvelopeBinding:
    """Validate exact binding data received in a private bridge packet."""

    if type(value) is not dict or set(value) != {
        "task_id",
        "lease_epoch",
        "assignment_token",
        "dispatch_context_hash",
        "route_hash",
        "expires_at",
    }:
        _invalid()
    binding = EnvelopeBinding(
        task_id=_required_str(value, "task_id"),
        lease_epoch=_required_int(value, "lease_epoch"),
        assignment_token=_required_str(value, "assignment_token"),
        dispatch_context_hash=_required_str(value, "dispatch_context_hash"),
        route_hash=_required_str(value, "route_hash"),
        expires_at=_required_int(value, "expires_at"),
    )
    _validate_binding(binding, now=now)
    return binding


def _roles_for_kind(kind: str) -> tuple[str, str]:
    if type(kind) is not str or kind not in _ROLE_BY_KIND:
        _invalid()
    return _ROLE_BY_KIND[kind]


def _validate_binding(binding: EnvelopeBinding, *, now: int) -> dict[str, object]:
    _validate_now(now)
    if type(binding) is not EnvelopeBinding:
        _invalid()
    if _IDENTIFIER.fullmatch(binding.task_id) is None:
        _invalid()
    if (
        type(binding.lease_epoch) is not int
        or binding.lease_epoch < 1
        or binding.lease_epoch > 2**63 - 1
    ):
        _invalid()
    _validate_digest(binding.assignment_token)
    _validate_digest(binding.dispatch_context_hash)
    _validate_digest(binding.route_hash)
    if type(binding.expires_at) is not int:
        _invalid()
    if binding.expires_at <= now:
        raise HostEnvelopeError("HOST_ENVELOPE_EXPIRED")
    return {
        "task_id": binding.task_id,
        "lease_epoch": binding.lease_epoch,
        "assignment_token": binding.assignment_token,
        "dispatch_context_hash": binding.dispatch_context_hash,
        "route_hash": binding.route_hash,
        "expires_at": binding.expires_at,
    }


def _validate_expectation(
    envelope: Mapping[str, object], expectation: EnvelopeExpectation, *, now: int
) -> None:
    if type(expectation) is not EnvelopeExpectation:
        _invalid()
    expected_roles = _roles_for_kind(expectation.kind)
    expected_binding = _validate_binding(expectation.binding, now=now)
    if (
        envelope["kind"] != expectation.kind
        or envelope["sender_role"] != expected_roles[0]
        or envelope["recipient_role"] != expected_roles[1]
        or any(envelope[field] != value for field, value in expected_binding.items())
    ):
        raise HostEnvelopeError("HOST_ENVELOPE_BINDING_INVALID")
    payload = envelope["payload"]
    assert type(payload) is dict
    if expectation.correlation_id is not None:
        _validate_identifier(expectation.correlation_id)
        if payload.get("correlation_id") != expectation.correlation_id:
            raise HostEnvelopeError("HOST_ENVELOPE_BINDING_INVALID")
    if expectation.peer_capability is not None:
        _validate_digest(expectation.peer_capability)
        if payload.get("peer_capability") != expectation.peer_capability:
            raise HostEnvelopeError("HOST_ENVELOPE_BINDING_INVALID")


def _validate_payload(kind: str, payload: object) -> dict[str, object]:
    if type(payload) is not dict:
        _invalid()
    if kind == "coordinator_assignment":
        if set(payload) != {
            "correlation_id",
            "assignment",
            "context",
            "artifact_refs",
            "digest_refs",
        }:
            _invalid()
        return {
            "correlation_id": _required_identifier(payload, "correlation_id"),
            "assignment": _required_text(payload, "assignment"),
            "context": _required_text_items(payload, "context"),
            "artifact_refs": _required_refs(payload, "artifact_refs", _MAX_ARTIFACT_REFS),
            "digest_refs": _required_refs(payload, "digest_refs", _MAX_DIGEST_REFS),
        }
    if kind == "worker_terminal_result":
        if set(payload) != {
            "correlation_id",
            "predecessor_hash",
            "terminal",
            "result",
            "risk",
            "artifact_refs",
            "digest_refs",
        }:
            _invalid()
        terminal = payload["terminal"]
        if type(terminal) is not str or terminal not in {"succeeded", "failed", "blocked"}:
            _invalid()
        return {
            "correlation_id": _required_identifier(payload, "correlation_id"),
            "predecessor_hash": _required_digest(payload, "predecessor_hash"),
            "terminal": terminal,
            "result": _required_text_items(payload, "result"),
            "risk": _required_risks(payload),
            "artifact_refs": _required_refs(payload, "artifact_refs", _MAX_ARTIFACT_REFS),
            "digest_refs": _required_refs(payload, "digest_refs", _MAX_DIGEST_REFS),
        }
    if kind == "peer_evidence_handoff":
        if set(payload) != {
            "correlation_id",
            "peer_capability",
            "dependency",
            "evidence",
            "artifact_refs",
            "digest_refs",
        }:
            _invalid()
        return {
            "correlation_id": _required_identifier(payload, "correlation_id"),
            "peer_capability": _required_digest(payload, "peer_capability"),
            "dependency": _required_text_items(payload, "dependency"),
            "evidence": _required_text_items(payload, "evidence"),
            "artifact_refs": _required_refs(payload, "artifact_refs", _MAX_ARTIFACT_REFS),
            "digest_refs": _required_refs(payload, "digest_refs", _MAX_DIGEST_REFS),
        }
    _invalid()


def _required_risks(payload: Mapping[str, object]) -> list[dict[str, str]]:
    risks = payload.get("risk")
    if type(risks) is not list or len(risks) > _MAX_RISKS:
        _invalid()
    normalized: list[dict[str, str]] = []
    codes: set[str] = set()
    for risk in risks:
        if type(risk) is not dict or set(risk) != {"code", "detail"}:
            _invalid()
        code = risk.get("code")
        detail = risk.get("detail")
        _validate_identifier(code)
        _validate_text(detail, purpose="risk.detail")
        assert type(code) is str
        assert type(detail) is str
        if code in codes:
            _invalid()
        codes.add(code)
        normalized.append({"code": code, "detail": detail})
    return sorted(normalized, key=lambda risk: risk["code"])


def _required_refs(
    payload: Mapping[str, object], field: str, maximum: int
) -> list[str]:
    refs = payload.get(field)
    if type(refs) is not list or len(refs) > maximum:
        _invalid()
    normalized: list[str] = []
    for reference in refs:
        _validate_digest(reference)
        assert type(reference) is str
        normalized.append(reference)
    if len(set(normalized)) != len(normalized):
        _invalid()
    return sorted(normalized)


def _required_text_items(payload: Mapping[str, object], field: str) -> list[str]:
    values = payload.get(field)
    if type(values) is not list or len(values) > _MAX_TEXT_ITEMS:
        _invalid()
    normalized: list[str] = []
    for value in values:
        _validate_text(value, purpose=field)
        assert type(value) is str
        normalized.append(value)
    return normalized


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    _validate_text(value, purpose=field)
    assert type(value) is str
    return value


def _required_identifier(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    _validate_identifier(value)
    assert type(value) is str
    return value


def _required_digest(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    _validate_digest(value)
    assert type(value) is str
    return value


def _required_str(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str:
        _invalid()
    return value


def _required_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int:
        _invalid()
    return value


def _validate_identifier(value: object) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _invalid()


def _validate_digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _invalid()


def _validate_text(value: object, *, purpose: str) -> None:
    """Accept only a purpose-bound metadata enum or an opaque digest reference."""

    allowed = _ALLOWED_TEXT_VALUES.get(purpose)
    if (
        type(value) is not str
        or allowed is None
        or (_DIGEST.fullmatch(value) is None and value not in allowed)
    ):
        _invalid()


def _validate_now(now: int) -> None:
    if type(now) is not int or now < 0:
        _invalid()


def _validate_size(kind: str, envelope: Mapping[str, object]) -> None:
    if len(_canonical_bytes(envelope)) > _MAX_BYTES_BY_KIND[kind]:
        _invalid()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise HostEnvelopeError("HOST_ENVELOPE_INVALID") from error


def _invalid() -> NoReturn:
    raise HostEnvelopeError("HOST_ENVELOPE_INVALID")
