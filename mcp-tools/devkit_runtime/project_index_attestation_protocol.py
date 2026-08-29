"""Exact host-private Project Index attestation protocol."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Final

ATTESTATION_SCHEMA: Final = "2718lab-devkit/project-index-attestation-v1"
MAX_ATTESTATION_BYTES: Final = 8 * 1024
ATTESTATION_TTL_SECONDS: Final = 120

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_JSON_DEPTH: Final = 12
_MAX_JSON_NODES: Final = 4_096
_INVALID: Final = "HOST_BRIDGE_PROJECT_INDEX_ATTESTATION_INVALID"
_FRAME_INVALID: Final = "HOST_BRIDGE_FRAME_INVALID"

_COMMON_FIELDS: Final = frozenset(
    {
        "schema",
        "operation",
        "correlation_id",
        "workspace_id",
        "workspace_binding_hash",
        "root_identity_hash",
        "expires_at",
        "attestation_hash",
    }
)
_SYNC_FIELDS: Final = _COMMON_FIELDS | {
    "snapshot_id",
    "snapshot_attestation_hash",
    "head_hash",
    "manifest_hash",
    "parser_set_hash",
}
_QUERY_FIELDS: Final = _SYNC_FIELDS | {"query_receipt_hash", "index_context_hash"}
_FIELDS_BY_OPERATION: Final = {
    "register": (
        "workspace_id",
        "workspace_binding_hash",
        "root_identity_hash",
    ),
    "sync": (
        "workspace_id",
        "workspace_binding_hash",
        "root_identity_hash",
        "snapshot_id",
        "snapshot_attestation_hash",
        "head_hash",
        "manifest_hash",
        "parser_set_hash",
    ),
    "query": (
        "workspace_id",
        "workspace_binding_hash",
        "root_identity_hash",
        "snapshot_id",
        "snapshot_attestation_hash",
        "head_hash",
        "manifest_hash",
        "parser_set_hash",
        "query_receipt_hash",
        "index_context_hash",
    ),
}
_EXPECTED_FIELDS_BY_OPERATION: Final = {
    "register": _COMMON_FIELDS,
    "sync": _SYNC_FIELDS,
    "query": _QUERY_FIELDS,
}


class ProjectIndexAttestationProtocolError(ValueError):
    """Stable protocol failure translated by the host bridge boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_attestation(value: object, *, now: int) -> dict[str, object]:
    """Validate one exact register, sync, or query attestation packet."""

    if type(value) is not dict or type(now) is not int or now < 0:
        _raise_invalid()
    operation = value.get("operation")
    if type(operation) is not str:
        _raise_invalid()
    expected = _EXPECTED_FIELDS_BY_OPERATION.get(operation)
    if (
        expected is None
        or set(value) != expected
        or value.get("schema") != ATTESTATION_SCHEMA
        or type(value.get("expires_at")) is not int
        or not now < value["expires_at"] <= now + ATTESTATION_TTL_SECONDS
    ):
        _raise_invalid()
    correlation_id = value.get("correlation_id")
    if not is_index_correlation(correlation_id):
        _raise_invalid()
    digest_fields = expected - {
        "schema",
        "operation",
        "correlation_id",
        "expires_at",
    }
    if any(
        type(value.get(field_name)) is not str
        or _DIGEST.fullmatch(value[field_name]) is None
        for field_name in digest_fields
    ):
        _raise_invalid()
    unsigned = dict(value)
    attestation_hash = unsigned.pop("attestation_hash")
    if not hmac.compare_digest(attestation_hash, _private_payload_hash(unsigned)):
        _raise_invalid()
    _validate_private_packet_size(value, MAX_ATTESTATION_BYTES)
    return dict(value)


def build_attestation(
    *,
    operation: str,
    correlation_id: str,
    material: Mapping[str, object],
    now: int,
) -> dict[str, object]:
    """Build a closed sideband packet from already persisted index material."""

    fields = _FIELDS_BY_OPERATION.get(operation)
    if (
        fields is None
        or not is_index_correlation(correlation_id)
        or type(material) is not dict
        or type(now) is not int
        or now < 0
    ):
        _raise_invalid()
    facts = {field_name: material.get(field_name) for field_name in fields}
    unsigned: dict[str, object] = {
        "schema": ATTESTATION_SCHEMA,
        "operation": operation,
        "correlation_id": correlation_id,
        **facts,
        "expires_at": now + ATTESTATION_TTL_SECONDS,
    }
    unsigned["attestation_hash"] = _private_payload_hash(unsigned)
    return normalize_attestation(unsigned, now=now)


def is_index_correlation(value: object) -> bool:
    """Return whether a value is the exact opaque Project Index correlation."""

    return (
        type(value) is str
        and value.startswith("index-")
        and len(value) == 70
        and all(character in "0123456789abcdef" for character in value[6:])
    )


def _raise_invalid() -> None:
    raise ProjectIndexAttestationProtocolError(_INVALID)


def _private_payload_hash(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _validate_private_packet_size(payload: Mapping[str, object], maximum: int) -> None:
    if len(_canonical_bytes(payload)) > maximum:
        raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)


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
    except ProjectIndexAttestationProtocolError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProjectIndexAttestationProtocolError(_FRAME_INVALID) from error


def _validate_json_value(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if math.isfinite(item):
                continue
            raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
        if type(item) is list:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            if len(item) > _MAX_JSON_NODES - nodes:
                raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
            if any(type(key) is not str for key in item):
                raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
            pending.extend((child, depth + 1) for child in item.values())
            continue
        raise ProjectIndexAttestationProtocolError(_FRAME_INVALID)
