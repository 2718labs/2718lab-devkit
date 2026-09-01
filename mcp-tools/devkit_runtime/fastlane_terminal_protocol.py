"""Exact Fast Lane worker terminal-result and acknowledgement protocol.

This module owns only the data-plane contract. Transport sequencing and replay
state remain in :mod:`devkit_runtime.host_bridge`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Final, cast

from . import host_envelopes

TERMINAL_RESULT_SCHEMA: Final = (
    "2718lab-devkit/fastlane-worker-terminal-result-v1"
)
TERMINAL_ACK_SCHEMA: Final = "2718lab-devkit/fastlane-worker-terminal-ack-v1"
TERMINAL_BINDING_FIELDS: Final = frozenset(
    {
        "call_intent_hash",
        "preparation_id",
        "batch_hash",
        "task_id",
        "lease_id",
        "lease_epoch",
        "task_version",
        "assignment_token",
        "dispatch_binding_hash",
        "routing_result_hash",
        "worktree_identity",
        "worktree_base",
        "integration_head",
        "predecessor_hash",
    }
)

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_FAST_LANE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_JSON_DEPTH: Final = 12
_MAX_JSON_NODES: Final = 4_096
_MAX_TERMINAL_PACKET_BYTES: Final = 24 * 1024
_TERMINAL_TTL_SECONDS: Final = 120
_INVALID: Final = "HOST_BRIDGE_FAST_LANE_TERMINAL_INVALID"
_FRAME_INVALID: Final = "HOST_BRIDGE_FRAME_INVALID"


class FastLaneTerminalProtocolError(ValueError):
    """Stable protocol failure translated by the host bridge boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_worker_terminal_result(
    value: object,
    *,
    expected: Mapping[str, object],
    expires_at: int | None = None,
    now: int,
) -> dict[str, object]:
    """Validate and normalize one exact23 authenticated terminal result."""

    fields = {
        "schema",
        *TERMINAL_BINDING_FIELDS,
        "terminal",
        "result",
        "risk",
        "artifact_refs",
        "digest_refs",
        "event_seq",
        "expires_at",
        "terminal_receipt_hash",
    }
    terminal_expires_at = value.get("expires_at") if type(value) is dict else None
    if (
        type(value) is not dict
        or set(value) != fields
        or value.get("schema") != TERMINAL_RESULT_SCHEMA
        or set(expected) != TERMINAL_BINDING_FIELDS
        or type(now) is not int
        or type(expires_at) is not int
        or expires_at <= now
        or type(terminal_expires_at) is not int
        or not now < cast(int, terminal_expires_at) <= now + _TERMINAL_TTL_SECONDS
        or cast(int, terminal_expires_at) > expires_at
        or any(value.get(field) != expected.get(field) for field in expected)
        or value.get("terminal") not in {"succeeded", "failed", "blocked"}
        or type(value.get("event_seq")) is not int
        or not 0 < cast(int, value["event_seq"]) <= 2**63 - 1
    ):
        _raise_invalid()
    if (
        type(value.get("call_intent_hash")) is not str
        or len(cast(str, value["call_intent_hash"])) != 64
        or any(
            character not in "0123456789abcdef"
            for character in cast(str, value["call_intent_hash"])
        )
        or type(value.get("preparation_id")) is not str
        or _IDENTIFIER.fullmatch(cast(str, value["preparation_id"])) is None
    ):
        _raise_invalid()
    for field_name in (
        "batch_hash",
        "dispatch_binding_hash",
        "routing_result_hash",
        "worktree_identity",
        "worktree_base",
        "integration_head",
        "predecessor_hash",
    ):
        item = value.get(field_name)
        if type(item) is not str or _DIGEST.fullmatch(item) is None:
            _raise_invalid()
    task_id = value.get("task_id")
    if type(task_id) is not str or _FAST_LANE_TASK_ID.fullmatch(task_id) is None:
        _raise_invalid()
    lease_id = value.get("lease_id")
    assignment_token = value.get("assignment_token")
    if (
        type(lease_id) is not str
        or _IDENTIFIER.fullmatch(lease_id) is None
        or type(assignment_token) is not str
        or _DIGEST.fullmatch(assignment_token) is None
    ):
        _raise_invalid()
    for field_name in ("lease_epoch", "task_version"):
        item = value.get(field_name)
        if type(item) is not int or not 0 < item <= 2**63 - 1:
            _raise_invalid()
    try:
        normalized_result = host_envelopes._required_text_items(value, "result")
        normalized_risk = host_envelopes._required_risks(value)
        normalized_artifacts = host_envelopes._required_refs(value, "artifact_refs", 16)
        normalized_digests = host_envelopes._required_refs(value, "digest_refs", 32)
    except host_envelopes.HostEnvelopeError as error:
        raise FastLaneTerminalProtocolError(_INVALID) from error
    unsigned = dict(value)
    receipt_hash = unsigned.pop("terminal_receipt_hash")
    if (
        value["result"] != normalized_result
        or value["risk"] != normalized_risk
        or value["artifact_refs"] != normalized_artifacts
        or value["digest_refs"] != normalized_digests
        or type(receipt_hash) is not str
        or _DIGEST.fullmatch(receipt_hash) is None
        or not hmac.compare_digest(receipt_hash, _private_payload_hash(unsigned))
    ):
        _raise_invalid()
    _validate_private_packet_size(value, _MAX_TERMINAL_PACKET_BYTES)
    return dict(value)


def normalize_worker_terminal_ack(
    value: object, *, terminal_result: Mapping[str, object]
) -> dict[str, object]:
    """Validate and normalize an exact terminal acknowledgement."""

    fields = {
        "schema",
        "call_intent_hash",
        "preparation_id",
        "batch_hash",
        "task_id",
        "terminal_receipt_hash",
        "accepted_event_seq",
        "refill_trigger_hash",
        "ack_hash",
    }
    unsigned = dict(value) if type(value) is dict else {}
    ack_hash = unsigned.pop("ack_hash", None)
    if (
        type(value) is not dict
        or set(value) != fields
        or value.get("schema") != TERMINAL_ACK_SCHEMA
        or any(
            value.get(field) != terminal_result.get(field)
            for field in (
                "call_intent_hash",
                "preparation_id",
                "batch_hash",
                "task_id",
                "terminal_receipt_hash",
            )
        )
        or type(value.get("accepted_event_seq")) is not int
        or cast(int, value["accepted_event_seq"])
        < cast(int, terminal_result.get("event_seq"))
        or type(value.get("refill_trigger_hash")) is not str
        or _DIGEST.fullmatch(cast(str, value["refill_trigger_hash"])) is None
        or type(ack_hash) is not str
        or _DIGEST.fullmatch(ack_hash) is None
        or not hmac.compare_digest(ack_hash, _private_payload_hash(unsigned))
    ):
        _raise_invalid()
    return dict(value)


def validate_terminal_correlation(value: object) -> None:
    """Require the opaque ``terminal-<hex64>`` correlation namespace."""

    if (
        type(value) is not str
        or len(value) != 73
        or not value.startswith("terminal-")
        or any(character not in "0123456789abcdef" for character in value[9:])
    ):
        _raise_invalid()


def _raise_invalid() -> None:
    raise FastLaneTerminalProtocolError(_INVALID)


def _private_payload_hash(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _validate_private_packet_size(payload: Mapping[str, object], maximum: int) -> None:
    if len(_canonical_bytes(payload)) > maximum:
        raise FastLaneTerminalProtocolError(_FRAME_INVALID)


def _canonical_bytes(value: object) -> bytes:
    try:
        _validate_json_value(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except FastLaneTerminalProtocolError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise FastLaneTerminalProtocolError(_FRAME_INVALID) from error


def _validate_json_value(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise FastLaneTerminalProtocolError(_FRAME_INVALID)
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if math.isfinite(item):
                continue
            raise FastLaneTerminalProtocolError(_FRAME_INVALID)
        if type(item) is list:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise FastLaneTerminalProtocolError(_FRAME_INVALID)
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise FastLaneTerminalProtocolError(_FRAME_INVALID)
            if any(type(key) is not str for key in item):
                raise FastLaneTerminalProtocolError(_FRAME_INVALID)
            pending.extend((child, depth + 1) for child in item.values())
            continue
        raise FastLaneTerminalProtocolError(_FRAME_INVALID)
