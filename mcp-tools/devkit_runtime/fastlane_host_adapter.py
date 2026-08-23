"""Private, inert Fast Lane host-boundary adapter.

Only an opaque compiler-evidence handle issued by ``HostSession`` may cross
this boundary. Public capability claims remain insufficient to start work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Final

from .host_session import HostCapabilityFact, HostSession

NO_SAFE_WORK: Final = "NO_SAFE_WORK"
_HASH: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LABEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TRANSFER_ROLES: Final = {
    "coordinator_to_worker": ("coordinator", "worker"),
    "worker_to_coordinator": ("worker", "coordinator"),
    "peer_handoff": ("peer", "peer"),
}
_SCHEDULER_ROLES: Final = frozenset(
    {"execution", "verification", "prewarm", "review", "design_probe"}
)


def prepare_verified_host_facts(
    session: object,
    *,
    capability_facts: Sequence[HostCapabilityFact] | object,
) -> str:
    """Accept no public substitute for session-owned compiler evidence."""

    if (
        type(session) is not HostSession
        or not isinstance(capability_facts, Sequence)
        or isinstance(capability_facts, (str, bytes, bytearray))
    ):
        return NO_SAFE_WORK
    return NO_SAFE_WORK


def compile_fast_lane_with_host_facts(
    request: object,
    *,
    reasoning_effort: object,
    verified_host_facts: object,
) -> str:
    """Return only ``NO_SAFE_WORK`` at this public, bearer-free boundary."""

    del request, reasoning_effort, verified_host_facts
    return NO_SAFE_WORK


def project_role_transfer(
    *,
    kind: object,
    task_id: object,
    role: object,
    assignment_token: object,
    context_hash: object,
    summary_hash: object,
    artifact_hashes: object,
    digest_hashes: object,
) -> dict[str, object] | str:
    """Project one bounded, hash-only role handoff without sending its body."""

    roles = _TRANSFER_ROLES.get(kind) if type(kind) is str else None
    normalized_task_id = _label(task_id)
    normalized_role = _label(role)
    token = _digest(assignment_token)
    context = _digest(context_hash)
    summary = _digest(summary_hash)
    artifacts = _digest_list(artifact_hashes, maximum=16)
    digests = _digest_list(digest_hashes, maximum=32)
    if (
        roles is None
        or normalized_task_id is None
        or normalized_role not in _SCHEDULER_ROLES
        or token is None
        or context is None
        or summary is None
        or artifacts is None
        or digests is None
    ):
        return NO_SAFE_WORK
    transfer: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-host-transfer-v1",
        "kind": kind,
        "sender_role": roles[0],
        "recipient_role": roles[1],
        "task_id": normalized_task_id,
        "role": normalized_role,
        "assignment_token": token,
        "context_hash": context,
        "summary_hash": summary,
        "artifact_hashes": artifacts,
        "digest_hashes": digests,
    }
    transfer["transfer_hash"] = _canonical_hash(transfer)
    return transfer


def _label(value: object) -> str | None:
    if type(value) is not str or _LABEL.fullmatch(value) is None:
        return None
    return value


def _digest(value: object) -> str | None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        return None
    return value


def _digest_list(value: object, *, maximum: int) -> list[str] | None:
    try:
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            return None
        if len(value) > maximum:
            return None
        normalized = [_digest(item) for item in value]
    except Exception:
        return None
    if any(item is None for item in normalized):
        return None
    digests = [item for item in normalized if item is not None]
    if len(set(digests)) != len(digests):
        return None
    return digests


def _canonical_hash(value: object) -> str:
    return (
        "sha256:"
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


__all__ = [
    "NO_SAFE_WORK",
    "compile_fast_lane_with_host_facts",
    "prepare_verified_host_facts",
    "project_role_transfer",
]
