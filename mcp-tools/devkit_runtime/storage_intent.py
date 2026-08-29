"""Canonical, path-free storage intent validation for the DevKit runtime.

The DevKit emits this value as an input to the authenticated Host storage
firewall.  This module only validates and normalizes the value; it never
chooses a filesystem path, creates a directory, or claims ownership of a
storage lease.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


STORAGE_INTENT_SCHEMA: Final = "2718lab.storage.intent.v1"
TARGET_DESCRIPTOR_SCHEMA: Final = "2718lab.storage.target.v1"
STORAGE_TARGET_KEY_INVALID: Final = "STORAGE_TARGET_KEY_INVALID"

_INTENT_FIELDS: Final = frozenset(
    {
        "schema",
        "task_id",
        "plan_binding",
        "context_hash",
        "storage_intent_hash",
        "requested_bytes",
        "requested_files",
        "target_descriptor",
    }
)
_TARGET_FIELDS: Final = frozenset(
    {
        "schema",
        "artifact_kind",
        "repository_identity",
        "workspace_manifest_hash",
        "cargo_lock_hash",
        "toolchain_digest",
        "target_triple",
        "profile",
        "features_hash",
        "build_env_class",
    }
)
_TARGET_FIELD_ORDER: Final = (
    "schema",
    "artifact_kind",
    "repository_identity",
    "workspace_manifest_hash",
    "cargo_lock_hash",
    "toolchain_digest",
    "target_triple",
    "profile",
    "features_hash",
    "build_env_class",
)
_ARTIFACT_KINDS: Final = frozenset(
    {"cargo-target", "python-cache", "mcp-package", "fastlane-task"}
)
_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_MAX_IDENTIFIER_BYTES: Final = 256
_MAX_U64: Final = (1 << 64) - 1


class StorageIntentError(ValueError):
    """A malformed storage intent represented by a stable error code."""

    code: str

    def __init__(self, code: str = STORAGE_TARGET_KEY_INVALID) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StorageIntent:
    """Validated immutable storage admission input.

    ``target_descriptor`` is copied into a read-only mapping so callers cannot
    mutate the target semantics after validation.  The record contains no
    absolute path or owner/lease field; those remain Host-owned facts.
    """

    task_id: str
    plan_binding: str
    context_hash: str
    storage_intent_hash: str
    requested_bytes: int
    requested_files: int
    target_descriptor: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.target_descriptor, Mapping):
            raise StorageIntentError()
        descriptor = dict(self.target_descriptor)
        if any(
            type(key) is not str or type(value) is not str
            for key, value in descriptor.items()
        ):
            raise StorageIntentError()
        object.__setattr__(
            self,
            "target_descriptor",
            MappingProxyType(descriptor),
        )

    def to_dict(self) -> dict[str, object]:
        """Return a plain JSON-compatible projection for a Host boundary."""
        return {
            "schema": STORAGE_INTENT_SCHEMA,
            "task_id": self.task_id,
            "plan_binding": self.plan_binding,
            "context_hash": self.context_hash,
            "storage_intent_hash": self.storage_intent_hash,
            "requested_bytes": self.requested_bytes,
            "requested_files": self.requested_files,
            "target_descriptor": dict(self.target_descriptor),
        }


def parse_storage_intent(value: object) -> StorageIntent:
    """Parse one exact, canonical storage intent or fail closed.

    Unknown fields are rejected before any supplied digest is compared.  The
    intent digest binds the task/context, requested budgets, and complete
    canonical target descriptor, as defined by the 1.1.3 storage contract.
    """
    mapping = _exact_mapping(value, _INTENT_FIELDS)
    descriptor = _exact_mapping(mapping["target_descriptor"], _TARGET_FIELDS)

    if mapping["schema"] != STORAGE_INTENT_SCHEMA:
        raise StorageIntentError()
    if descriptor["schema"] != TARGET_DESCRIPTOR_SCHEMA:
        raise StorageIntentError()

    task_id = _bounded_identifier(mapping["task_id"])
    plan_binding = _digest(mapping["plan_binding"])
    context_hash = _digest(mapping["context_hash"])
    requested_bytes = _bounded_positive(mapping["requested_bytes"])
    requested_files = _bounded_positive(mapping["requested_files"])
    target = _canonical_target(descriptor)

    storage_intent_hash = _digest(mapping["storage_intent_hash"])
    expected = _sha256_json(
        {
            "target_descriptor": target,
            "task_id": task_id,
            "plan_binding": plan_binding,
            "context_hash": context_hash,
            "requested_bytes": requested_bytes,
            "requested_files": requested_files,
        }
    )
    if expected != storage_intent_hash:
        raise StorageIntentError()

    return StorageIntent(
        task_id=task_id,
        plan_binding=plan_binding,
        context_hash=context_hash,
        storage_intent_hash=storage_intent_hash,
        requested_bytes=requested_bytes,
        requested_files=requested_files,
        target_descriptor=MappingProxyType(target),
    )


def _exact_mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise StorageIntentError()
    try:
        keys = tuple(value.keys())
    except (AttributeError, TypeError, ValueError) as error:
        raise StorageIntentError() from error
    if len(keys) != len(fields) or any(type(key) is not str for key in keys):
        raise StorageIntentError()
    if frozenset(keys) != fields:
        raise StorageIntentError()
    try:
        return {key: value[key] for key in keys}
    except (KeyError, TypeError, ValueError) as error:
        raise StorageIntentError() from error


def _canonical_target(descriptor: Mapping[str, object]) -> dict[str, str]:
    artifact_kind = descriptor["artifact_kind"]
    if type(artifact_kind) is not str or artifact_kind not in _ARTIFACT_KINDS:
        raise StorageIntentError()

    target: dict[str, str] = {}
    for field in _TARGET_FIELD_ORDER:
        raw = descriptor[field]
        if type(raw) is not str:
            raise StorageIntentError()
        if field.endswith("_hash") or field in {
            "repository_identity",
            "toolchain_digest",
            "features_hash",
        }:
            _digest(raw)
        elif field not in {"schema", "artifact_kind"}:
            _bounded_identifier(raw)
        target[field] = raw
    return target


def _bounded_identifier(value: object) -> str:
    if type(value) is not str:
        raise StorageIntentError()
    if not value or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise StorageIntentError()
    if _IDENTIFIER.fullmatch(value) is None:
        raise StorageIntentError()
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise StorageIntentError()
    return value


def _bounded_positive(value: object) -> int:
    if type(value) is not int or not 0 < value <= _MAX_U64:
        raise StorageIntentError()
    return value


def _sha256_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise StorageIntentError() from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["StorageIntent", "StorageIntentError", "parse_storage_intent"]
