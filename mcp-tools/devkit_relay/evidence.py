"""HMAC-scoped Relay capabilities and bounded evidence validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

from .canonical import canonical_bytes

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_ENDPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CAPABILITY_FIELDS = frozenset(
    {
        "schema",
        "workflow_id",
        "task_id",
        "action",
        "epoch",
        "endpoint",
        "scope",
        "expires_at",
    }
)


class RelayCapabilityError(RuntimeError):
    """Stable capability validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CapabilityClaims:
    """Verified immutable claims carried by a Relay capability."""

    workflow_id: str
    task_id: str
    action: str
    epoch: int
    endpoint: str
    scope: str
    expires_at: int


class CapabilitySigner:
    """Issue and verify short-lived, action-specific HMAC capabilities."""

    _SCHEMA = "2718lab-devkit/relay-capability-v1"

    def __init__(self, secret: bytes | str, *, lifetime_seconds: int = 3_600) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if type(secret) is not bytes or len(secret) < 16:
            raise ValueError("capability secret is invalid")
        if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 86_400:
            raise ValueError("capability lifetime is invalid")
        self._secret = secret
        self._lifetime_seconds = lifetime_seconds

    def issue(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
        scope: str,
        expires_at: int | None = None,
    ) -> str:
        """Sign one exact worker or Sol operation without persisting its secret."""

        self._validate_fields(
            workflow_id=workflow_id,
            task_id=task_id,
            action=action,
            epoch=epoch,
            endpoint=endpoint,
            scope=scope,
        )
        expiry = (
            int(time.time()) + self._lifetime_seconds
            if expires_at is None
            else expires_at
        )
        if type(expiry) is not int or expiry <= int(time.time()):
            raise ValueError("capability expiry is invalid")
        payload = {
            "schema": self._SCHEMA,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "action": action,
            "epoch": epoch,
            "endpoint": endpoint,
            "scope": scope,
            "expires_at": expiry,
        }
        encoded = _b64encode(canonical_bytes(payload))
        signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}"

    def verify(
        self,
        token: object,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
        scope: str,
        now: int | None = None,
    ) -> CapabilityClaims:
        """Verify every bound claim before a caller can mutate Relay state."""

        self._validate_fields(
            workflow_id=workflow_id,
            task_id=task_id,
            action=action,
            epoch=epoch,
            endpoint=endpoint,
            scope=scope,
        )
        if type(token) is not str or token.count(".") != 1:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        encoded, signature = token.split(".", 1)
        expected = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        try:
            decoded = json.loads(_b64decode(encoded).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID") from error
        if type(decoded) is not dict or set(decoded) != _CAPABILITY_FIELDS:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        if decoded.get("schema") != self._SCHEMA:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        try:
            self._validate_fields(
                workflow_id=decoded["workflow_id"],
                task_id=decoded["task_id"],
                action=decoded["action"],
                epoch=decoded["epoch"],
                endpoint=decoded["endpoint"],
                scope=decoded["scope"],
            )
        except ValueError as error:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID") from error
        if decoded["scope"] != scope:
            raise RelayCapabilityError("RELAY_CAPABILITY_SCOPE")
        if (
            decoded["workflow_id"] != workflow_id
            or decoded["task_id"] != task_id
            or decoded["action"] != action
            or decoded["epoch"] != epoch
            or decoded["endpoint"] != endpoint
        ):
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        current = int(time.time()) if now is None else now
        if type(current) is not int or current < 0:
            raise RelayCapabilityError("RELAY_CAPABILITY_INVALID")
        if type(decoded["expires_at"]) is not int or decoded["expires_at"] <= current:
            raise RelayCapabilityError("RELAY_CAPABILITY_EXPIRED")
        return CapabilityClaims(
            workflow_id=workflow_id,
            task_id=task_id,
            action=action,
            epoch=epoch,
            endpoint=endpoint,
            scope=scope,
            expires_at=decoded["expires_at"],
        )

    @staticmethod
    def _validate_fields(
        *,
        workflow_id: object,
        task_id: object,
        action: object,
        epoch: object,
        endpoint: object,
        scope: object,
    ) -> None:
        if (
            type(workflow_id) is not str
            or _IDENTIFIER.fullmatch(workflow_id) is None
            or type(task_id) is not str
            or _IDENTIFIER.fullmatch(task_id) is None
            or type(action) is not str
            or _IDENTIFIER.fullmatch(action) is None
            or type(epoch) is not int
            or epoch < 1
            or type(endpoint) is not str
            or _ENDPOINT.fullmatch(endpoint) is None
            or type(scope) is not str
            or scope not in {"worker", "sol"}
        ):
            raise ValueError("capability claims are invalid")


def validate_evidence(value: object) -> dict[str, str]:
    """Normalize one bounded immutable evidence receipt."""

    if type(value) is not dict or set(value) != {"kind", "selector", "digest"}:
        raise ValueError("evidence is invalid")
    kind = value["kind"]
    selector = value["selector"]
    digest = value["digest"]
    if (
        type(kind) is not str
        or _IDENTIFIER.fullmatch(kind) is None
        or type(selector) is not str
        or not selector.strip()
        or len(selector) > 2_048
        or "\r" in selector
        or "\n" in selector
        or type(digest) is not str
        or _DIGEST.fullmatch(digest) is None
    ):
        raise ValueError("evidence is invalid")
    return {"kind": kind, "selector": selector.strip(), "digest": digest}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
