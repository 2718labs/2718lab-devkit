"""Host-owned project authority identity primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_AUTHORITY_DOMAIN = "2718lab/project-authority/v1"
_RECEIPT_SCHEMA = "2718lab/project-authority-receipt/v1"
_BINDING_SCHEME = "os-stat-directory-v1"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CONSTRUCTION_TOKEN = object()


class ProjectAuthorityError(RuntimeError):
    """Stable project-authority failure without host-path disclosure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProjectPhysicalBinding:
    """Stable physical identity of one safe project-root directory."""

    device_id: int
    file_id: int

    def _identity_payload(self) -> dict[str, str]:
        return {
            "device_id": str(self.device_id),
            "file_id": str(self.file_id),
            "scheme": _BINDING_SCHEME,
        }


@dataclass(frozen=True)
class ProjectAuthorityReceipt:
    """Non-secret receipt persisted and selected only by the host."""

    authority_nonce: str
    physical_binding: ProjectPhysicalBinding
    project_id: str

    def to_json(self) -> str:
        _validate_receipt(self)
        return _canonical_json(
            {
                "authority_nonce": self.authority_nonce,
                "physical_binding": self.physical_binding._identity_payload(),
                "project_id": self.project_id,
                "schema": _RECEIPT_SCHEMA,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> ProjectAuthorityReceipt:
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "authority_nonce",
            "physical_binding",
            "project_id",
            "schema",
        }:
            raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")
        binding = payload.get("physical_binding")
        if (
            payload.get("schema") != _RECEIPT_SCHEMA
            or not isinstance(binding, dict)
            or set(binding) != {"device_id", "file_id", "scheme"}
            or binding.get("scheme") != _BINDING_SCHEME
        ):
            raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")
        receipt = cls(
            authority_nonce=_receipt_string(payload, "authority_nonce"),
            physical_binding=ProjectPhysicalBinding(
                device_id=_receipt_identifier(binding, "device_id"),
                file_id=_receipt_identifier(binding, "file_id"),
            ),
            project_id=_receipt_string(payload, "project_id"),
        )
        _validate_receipt(receipt)
        return receipt


@dataclass(frozen=True, init=False)
class ProjectAuthority:
    """A live authority after binding a trusted receipt to a physical root."""

    project_root: Path
    receipt: ProjectAuthorityReceipt

    def __init__(
        self,
        project_root: Path,
        receipt: ProjectAuthorityReceipt,
        *,
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("ProjectAuthority must be issued or reopened by the host")
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "receipt", receipt)

    @property
    def project_id(self) -> str:
        return self.receipt.project_id

    @classmethod
    def issue(cls, project_root: str | Path) -> ProjectAuthority:
        root = _absolute_project_root(project_root)
        binding = _stable_physical_binding(root)
        nonce = secrets.token_hex(32)
        receipt = ProjectAuthorityReceipt(
            authority_nonce=nonce,
            physical_binding=binding,
            project_id=_derive_project_id(nonce, binding),
        )
        _validate_receipt(receipt)
        return cls(root, receipt, _token=_CONSTRUCTION_TOKEN)

    @classmethod
    def reopen(
        cls,
        project_root: str | Path,
        receipt: ProjectAuthorityReceipt,
    ) -> ProjectAuthority:
        _validate_receipt(receipt)
        root = _absolute_project_root(project_root)
        binding = _stable_physical_binding(root)
        if binding != receipt.physical_binding:
            raise ProjectAuthorityError("PROJECT_AUTHORITY_MISMATCH")
        return cls(root, receipt, _token=_CONSTRUCTION_TOKEN)

    def revalidate(self) -> None:
        _validate_receipt(self.receipt)
        if _stable_physical_binding(self.project_root) != self.receipt.physical_binding:
            raise ProjectAuthorityError("PROJECT_AUTHORITY_MISMATCH")


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _derive_project_id(nonce: str, binding: ProjectPhysicalBinding) -> str:
    material = {
        "authority_nonce": nonce,
        "domain": _AUTHORITY_DOMAIN,
        "physical_binding": binding._identity_payload(),
    }
    return hashlib.sha256(_canonical_json(material).encode("ascii")).hexdigest()


def _validate_receipt(receipt: ProjectAuthorityReceipt) -> None:
    if (
        _SHA256_HEX.fullmatch(receipt.authority_nonce) is None
        or _SHA256_HEX.fullmatch(receipt.project_id) is None
        or receipt.physical_binding.device_id <= 0
        or receipt.physical_binding.file_id <= 0
        or receipt.project_id
        != _derive_project_id(receipt.authority_nonce, receipt.physical_binding)
    ):
        raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")


def _receipt_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")
    return value


def _receipt_identifier(payload: dict[str, Any], name: str) -> int:
    raw = payload.get(name)
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")
    value = int(raw)
    if value <= 0 or raw != str(value):
        raise ProjectAuthorityError("PROJECT_AUTHORITY_RECEIPT_INVALID")
    return value


def _absolute_project_root(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ProjectAuthorityError("PROJECT_ROOT_INVALID")
    return Path(os.path.abspath(path))


def _stable_physical_binding(path: Path) -> ProjectPhysicalBinding:
    first = _read_physical_binding_once(path)
    second = _read_physical_binding_once(path)
    if first != second:
        raise ProjectAuthorityError("PROJECT_ROOT_UNSTABLE")
    return first


def _read_physical_binding_once(path: Path) -> ProjectPhysicalBinding:
    root_status: os.stat_result | None = None
    for candidate in (path, *path.parents):
        try:
            status = candidate.lstat()
        except (FileNotFoundError, OSError) as exc:
            raise ProjectAuthorityError("PROJECT_ROOT_INVALID") from exc
        if not stat.S_ISDIR(status.st_mode) or _is_reparse_or_link(candidate, status):
            raise ProjectAuthorityError("PROJECT_ROOT_INVALID")
        if candidate == path:
            root_status = status
    if root_status is None or root_status.st_dev <= 0 or root_status.st_ino <= 0:
        raise ProjectAuthorityError("PROJECT_ROOT_IDENTITY_UNAVAILABLE")
    return ProjectPhysicalBinding(
        device_id=int(root_status.st_dev),
        file_id=int(root_status.st_ino),
    )


def _is_reparse_or_link(path: Path, status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & 0x400:
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if not callable(is_junction):
        return False
    try:
        return bool(is_junction(path))
    except OSError:
        return True
