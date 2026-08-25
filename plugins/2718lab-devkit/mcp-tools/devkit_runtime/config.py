"""Pure runtime configuration for the process-lifetime composition root."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from devkit_runtime.project_authority import (
    ProjectAuthority,
    ProjectAuthorityError,
    RuntimeProjectAuthorityProvider,
)

_SCOPE_DIRECTORY = "scoped-v1"
_PROJECT_DIRECTORY = "projects-v2"
_DATA_ROOT_ENV = "CODEX_DEVKIT_DATA_ROOT"
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PROJECT_ROOT_ENV_NAMES = ("CODEX_PROJECT_ROOT", "CODEX_WORKSPACE_ROOT")
_PROJECT_ID_ENV_NAMES = (
    "CODEX_PROJECT_ID",
    "CODEX_WORKSPACE_ID",
    "CODEX_THREAD_ID",
)


class RuntimeConfigError(RuntimeError):
    """Stable configuration failure without host-path disclosure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved durable paths without creating or opening any resource."""

    data_root: Path
    scratch_root: Path
    project_authority: ProjectAuthority | None = None
    storage_layout: str = "legacy-compat"
    authority_provider: RuntimeProjectAuthorityProvider | None = None

    def require_project_authority(self) -> ProjectAuthority:
        """Revalidate the module-minted provider at a runtime trust boundary."""

        authority = _current_project_authority(self.authority_provider)
        if (
            authority != self.project_authority
            or self.storage_layout != _PROJECT_DIRECTORY
            or self.data_root.parent.name != _PROJECT_DIRECTORY
            or self.data_root.name != authority.project_id
            or self.scratch_root.name != authority.project_id
        ):
            raise RuntimeConfigError("PROJECT_AUTHORITY_PROVIDER_INVALID")
        return authority

    @property
    def orchestrator_database(self) -> Path:
        return self.data_root / "orchestrator.sqlite3"

    @property
    def project_index_database(self) -> Path:
        return self.data_root / "project-index.sqlite3"

    @property
    def checkpoint_cas_root(self) -> Path:
        return self.data_root / "checkpoint-cas"

    @property
    def continuity_database(self) -> Path:
        return self.data_root / "continuity.sqlite3"

    @property
    def continuity_cas_root(self) -> Path:
        return self.data_root / "continuity-cas"

    @property
    def atlas_database(self) -> Path:
        return self.data_root / "atlas.sqlite3"

    @property
    def relay_database(self) -> Path:
        return self.data_root / "relay.sqlite3"

    @property
    def relay_capability_key(self) -> Path:
        return self.data_root / "relay-capability.key"

    @property
    def relay_proof_registry_database(self) -> Path:
        return self.data_root / "relay-proof-registry.sqlite3"

    @classmethod
    def load(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        protected_roots: Iterable[str | Path] = (),
        authority_provider: RuntimeProjectAuthorityProvider | None = None,
    ) -> RuntimeConfig:
        values = os.environ if environ is None else environ
        explicit_data_root = values.get(_DATA_ROOT_ENV)
        if explicit_data_root:
            data_base = _absolute_path(explicit_data_root)
        else:
            plugin_data = values.get("PLUGIN_DATA")
            if plugin_data:
                data_base = _absolute_path(plugin_data)
            else:
                codex_home = values.get("CODEX_HOME")
                if codex_home:
                    data_base = _absolute_path(codex_home) / "data" / "2718lab-devkit"
                else:
                    data_base = Path.home() / ".codex" / "data" / "2718lab-devkit"
        if authority_provider is None:
            project_authority = None
            scope = _resolve_scope(values)
            data_root = _scoped_root(data_base, scope)
            storage_layout = "legacy-compat"
        else:
            project_authority = _current_project_authority(authority_provider)
            scope = None
            data_root = _authority_root(data_base, project_authority)
            storage_layout = _PROJECT_DIRECTORY
        protected = tuple(_absolute_path(item) for item in protected_roots)
        if project_authority is not None:
            protected = (*protected, project_authority.project_root)
        if not _safe_directory_path(data_root, require_exists=False) or any(
            not _safe_directory_path(root, require_exists=False) for root in protected
        ):
            raise RuntimeConfigError("DATA_ROOT_INVALID")
        if any(_paths_overlap(data_root, root) for root in protected):
            raise RuntimeConfigError("DATA_ROOT_INVALID")
        config = cls(
            data_root=data_root,
            scratch_root=_resolve_scratch(
                values,
                data_root,
                protected,
                scope,
                project_authority,
            ),
            project_authority=project_authority,
            storage_layout=storage_layout,
            authority_provider=authority_provider,
        )
        if authority_provider is not None:
            config.require_project_authority()
        return config


def _absolute_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeConfigError("DATA_ROOT_INVALID")
    return Path(os.path.abspath(path))


def _current_project_authority(
    provider: RuntimeProjectAuthorityProvider | None,
) -> ProjectAuthority:
    if type(provider) is not RuntimeProjectAuthorityProvider:
        raise RuntimeConfigError("PROJECT_AUTHORITY_PROVIDER_INVALID")
    try:
        return provider.current()
    except ProjectAuthorityError as exc:
        raise RuntimeConfigError("PROJECT_AUTHORITY_INVALID") from exc


def _resolve_scratch(
    values: Mapping[str, str],
    data_root: Path,
    protected_roots: tuple[Path, ...],
    scope: tuple[str, str] | None,
    project_authority: ProjectAuthority | None,
) -> Path:
    for name in ("CODEX_TASK_TEMP", "TMPDIR", "TEMP", "TMP"):
        configured = values.get(name)
        if configured:
            scratch_base = _absolute_path(configured)
            if not _safe_existing_directory(scratch_base):
                raise RuntimeConfigError("DATA_ROOT_INVALID")
            scratch_root = (
                _scoped_root(scratch_base, scope)
                if project_authority is None
                else _authority_root(scratch_base, project_authority)
            )
            if not _safe_directory_path(scratch_root, require_exists=False) or any(
                _paths_overlap(scratch_root, root)
                for root in (data_root, *protected_roots)
            ):
                raise RuntimeConfigError("DATA_ROOT_INVALID")
            return scratch_root
    if project_authority is None:
        fallback = data_root.parent / ".2718lab-devkit-scratch"
    else:
        fallback = (
            data_root.parent.parent
            / ".2718lab-devkit-scratch"
            / _PROJECT_DIRECTORY
            / project_authority.project_id
        )
    if not _safe_directory_path(fallback, require_exists=False) or any(
        _paths_overlap(fallback, root) for root in (data_root, *protected_roots)
    ):
        raise RuntimeConfigError("DATA_ROOT_INVALID")
    return fallback


def _resolve_scope(values: Mapping[str, str]) -> tuple[str, str] | None:
    """Resolve a host-provided project identity without persisting its path."""

    for name in _PROJECT_ROOT_ENV_NAMES:
        raw = values.get(name)
        if not raw:
            continue
        candidate = _absolute_path(raw)
        if not _safe_existing_directory(candidate):
            raise RuntimeConfigError("PROJECT_SCOPE_INVALID")
        return "project-root", os.path.normcase(str(candidate))

    for name in _PROJECT_ID_ENV_NAMES:
        raw = values.get(name)
        if not raw:
            continue
        if _SCOPE_ID.fullmatch(raw) is None:
            raise RuntimeConfigError("PROJECT_SCOPE_INVALID")
        return name.casefold(), raw
    return None


def _scoped_root(base: Path, scope: tuple[str, str] | None) -> Path:
    if scope is None:
        return base
    kind, value = scope
    digest = hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()
    return base / _SCOPE_DIRECTORY / f"{kind}-{digest}"


def _authority_root(base: Path, authority: ProjectAuthority) -> Path:
    return base / _PROJECT_DIRECTORY / authority.project_id


def _safe_existing_directory(path: Path) -> bool:
    return _safe_directory_path(path, require_exists=True)


def _safe_directory_path(path: Path, *, require_exists: bool) -> bool:
    """Reject unsafe existing path components without creating missing ones."""

    exists = False
    for candidate in (path, *path.parents):
        try:
            status = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if not stat.S_ISDIR(status.st_mode) or _is_reparse_or_link(candidate, status):
            return False
        if candidate == path:
            exists = True
    return exists or not require_exists


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


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_value = os.path.normcase(os.fspath(left))
        right_value = os.path.normcase(os.fspath(right))
        common = os.path.commonpath((left_value, right_value))
    except ValueError:
        return False
    return common in {left_value, right_value}
