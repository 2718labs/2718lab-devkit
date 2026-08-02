"""Conservative language detection and shell-free verification commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class LanguageKind(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    RUST = "rust"
    GO = "go"


@dataclass(frozen=True)
class CommandSpec:
    """A command that can be inspected and executed without a shell."""

    executable: str
    args: tuple[str, ...]
    cwd: str
    env: tuple[tuple[str, str], ...] = ()
    network: bool = False
    timeout_seconds: int = 300
    output_limit_bytes: int = 1_000_000
    requires_gate: bool = False
    gate_reason: str = ""


@dataclass(frozen=True)
class ProjectProfile:
    kind: LanguageKind
    evidence: tuple[str, ...]
    commands: tuple[CommandSpec, ...]


@dataclass(frozen=True)
class DetectionResult:
    profiles: tuple[ProjectProfile, ...]
    blocked_reason: str | None = None


_JS_LOCKFILES: dict[str, str] = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
_JS_VERIFY_SCRIPTS = ("test", "lint", "typecheck")


def detect_repository(
    root: str | Path, *, task_temp_root: str | Path
) -> DetectionResult:
    """Detect supported build systems and return deterministic command specs.

    The returned values are data only. Callers retain responsibility for risk
    gates and for invoking ``executable`` with ``args`` without a shell.
    """

    repository = Path(root).resolve()
    task_temp = Path(task_temp_root).resolve()
    if not repository.is_dir():
        return DetectionResult((), "repository_not_found")

    profiles: list[ProjectProfile] = []
    if (repository / "pyproject.toml").is_file():
        profiles.append(_python_profile(repository))

    if (repository / "package.json").is_file():
        javascript = _javascript_profile(repository)
        if isinstance(javascript, str):
            return DetectionResult((), javascript)
        profiles.append(javascript)

    if (repository / "Cargo.toml").is_file():
        profiles.append(_rust_profile(repository, task_temp))

    if (repository / "go.mod").is_file() or (repository / "go.work").is_file():
        profiles.append(_go_profile(repository))

    if not profiles:
        return DetectionResult((), "unknown_build_system")
    return DetectionResult(tuple(profiles))


def _python_profile(repository: Path) -> ProjectProfile:
    return ProjectProfile(
        LanguageKind.PYTHON,
        ("pyproject.toml",),
        (_command("python", ("-m", "pytest"), repository),),
    )


def _javascript_profile(repository: Path) -> ProjectProfile | str:
    lockfiles = tuple(name for name in _JS_LOCKFILES if (repository / name).is_file())
    if not lockfiles:
        return "missing_javascript_lockfile"
    if len(lockfiles) != 1:
        return "conflicting_javascript_lockfiles"

    try:
        package_data: Any = json.loads(
            (repository / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "invalid_package_json"
    scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
    if not isinstance(scripts, dict):
        return "invalid_package_scripts"

    manager = _JS_LOCKFILES[lockfiles[0]]
    commands = tuple(
        _command(
            manager,
            _javascript_args(manager, script_name),
            repository,
            requires_gate=True,
            gate_reason="package_lifecycle_script",
        )
        for script_name in _JS_VERIFY_SCRIPTS
        if isinstance(scripts.get(script_name), str)
    )
    if not commands:
        return "javascript_no_verification_scripts"
    return ProjectProfile(
        LanguageKind.JAVASCRIPT, ("package.json", *lockfiles), commands
    )


def _javascript_args(manager: str, script_name: str) -> tuple[str, ...]:
    if manager == "npm":
        return ("run", script_name)
    return (script_name,)


def _rust_profile(repository: Path, task_temp: Path) -> ProjectProfile:
    target = (task_temp / "cargo-target").resolve()
    return ProjectProfile(
        LanguageKind.RUST,
        ("Cargo.toml",),
        (
            _command(
                "cargo", ("test",), repository, env=(("CARGO_TARGET_DIR", str(target)),)
            ),
        ),
    )


def _go_profile(repository: Path) -> ProjectProfile:
    evidence = tuple(
        name for name in ("go.mod", "go.work") if (repository / name).is_file()
    )
    return ProjectProfile(
        LanguageKind.GO,
        evidence,
        (_command("go", ("test", "./..."), repository),),
    )


def _command(
    executable: str,
    args: tuple[str, ...],
    cwd: Path,
    *,
    env: tuple[tuple[str, str], ...] = (),
    requires_gate: bool = False,
    gate_reason: str = "",
) -> CommandSpec:
    return CommandSpec(
        executable=executable,
        args=args,
        cwd=str(cwd),
        env=env,
        requires_gate=requires_gate,
        gate_reason=gate_reason,
    )
