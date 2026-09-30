#!/usr/bin/env python3
"""Read-only package preflight; never probes hosts, grants access, or dispatches."""

from __future__ import annotations

import argparse
import json
import stat
import tomllib
from pathlib import Path

SCHEMA = "2718lab-devkit/package-compatibility-v1"
IDENTITY = (
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
)
NAME = "2718lab-devkit"
MAX_BYTES = 262144


class PackageError(ValueError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PackageError("duplicate_json_key")
        result[key] = value
    return result


def _read(root: Path, relative: str) -> str:
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise PackageError("symlink_not_allowed")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise PackageError("file_type_or_size_invalid")
    return path.read_text(encoding="utf-8")


def inspect_package(root: Path) -> dict:
    """Validate this package's dual manifests without invoking any command."""
    checks = []
    if root.is_symlink() or not root.is_dir():
        return {
            "schema": SCHEMA,
            "ok": False,
            "checks": [{"check": "root", "ok": False, "reason": "directory_required"}],
            "execution_authorized": False,
        }

    def check(name, operation):
        try:
            operation()
            checks.append({"check": name, "ok": True})
        except (
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ) as exc:
            reason = (
                str(exc)
                if isinstance(exc, PackageError)
                else "invalid_or_missing_package_data"
            )
            checks.append({"check": name, "ok": False, "reason": reason})

    data = {}
    for label, relative in (
        ("portable_manifest", "plugin.json"),
        ("codex_manifest", ".codex-plugin/plugin.json"),
        ("portable_mcp", "mcp.json"),
        ("codex_mcp", ".mcp.json"),
    ):

        def load(label=label, relative=relative):
            value = json.loads(_read(root, relative), object_pairs_hook=_pairs)
            if type(value) is not dict:
                raise PackageError("object_required")
            data[label] = value

        check(label, load)

    def identity():
        portable, legacy = data["portable_manifest"], data["codex_manifest"]
        allowed = {"$schema", *IDENTITY, "extensions"}
        legacy_allowed = {*IDENTITY, "mcpServers", "interface"}
        if set(legacy) != legacy_allowed:
            raise PackageError("legacy_manifest_fields_differ")
        if (
            set(portable) != allowed
            or portable["$schema"]
            != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        ):
            raise PackageError("portable_manifest_fields_differ")
        if portable["name"] != NAME or any(
            portable[key] != legacy[key] for key in IDENTITY
        ):
            raise PackageError("manifest_identity_drift")
        extension = portable["extensions"]
        if extension != {"com.openai": {"interface": legacy["interface"]}}:
            raise PackageError("openai_overlay_drift")
        if legacy.get("mcpServers") != "./.mcp.json":
            raise PackageError("legacy_mcp_pointer_drift")
        version = tomllib.loads(_read(root, "mcp-tools/pyproject.toml"))["project"][
            "version"
        ]
        if version != portable["version"]:
            raise PackageError("python_version_drift")

    check("identity_and_overlay", identity)

    def transports():
        portable, legacy = data["portable_mcp"], data["codex_mcp"]
        if (
            set(portable) != {"$schema", "mcpServers"}
            or portable["$schema"]
            != "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        ):
            raise PackageError("portable_mcp_fields_differ")
        if set(portable["mcpServers"]) != {NAME} or set(legacy["mcpServers"]) != {NAME}:
            raise PackageError("server_inventory_drift")
        new, old = portable["mcpServers"][NAME], legacy["mcpServers"][NAME]
        expected = {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "--locked", "--no-dev", "python", "server.py"],
            "cwd": "./mcp-tools",
            "env": {
                "CODEX_DEVKIT_DATA_ROOT": "${PLUGIN_DATA}/runtime",
                "UV_PROJECT_ENVIRONMENT": "${PLUGIN_DATA}/python",
                "UV_CACHE_DIR": "${PLUGIN_DATA}/uv-cache",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        }
        if new != expected:
            raise PackageError("portable_launch_contract_drift")
        if (
            old.get("command") != new["command"]
            or old.get("args") != new["args"]
            or old.get("cwd") != "mcp-tools"
        ):
            raise PackageError("legacy_launch_contract_drift")
        _read(root, "mcp-tools/server.py")
        _read(root, "mcp-tools/uv.lock")

    check("stdio_launch_equivalence", transports)

    def skills():
        folder = root / "skills"
        if folder.is_symlink() or not folder.is_dir():
            raise PackageError("skills_directory_missing")
        entries = list(folder.iterdir())
        if len(entries) > 32:
            raise PackageError("skills_inventory_unbounded")
        names = []
        for path in entries:
            if path.is_symlink():
                raise PackageError("symlink_not_allowed")
            if not path.is_dir():
                continue
            text = _read(root, "skills/" + path.name + "/SKILL.md")
            if (
                not text.startswith("---\n")
                or "\nname: " + path.name + "\n" not in text.split("\n---", 1)[0] + "\n"
            ):
                raise PackageError("skill_identity_drift")
            names.append(path.name)
        if not {"fast-lane-routing", "code-atlas", "workflow-design"}.issubset(names):
            raise PackageError("required_manual_missing")

    check("packaged_skill_identity", skills)
    return {
        "schema": SCHEMA,
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "execution_authorized": False,
        "not_verified": [
            "client_installation",
            "host_environment_forwarding",
            "private_broker_attestation",
            "model_or_effort_availability",
            "agent_dispatch",
        ],
        "required_host_inputs": [
            "durable_data_root",
            "project_or_thread_scope",
            "actual_dispatch_capabilities",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    args = parser.parse_args()
    result = inspect_package(args.plugin_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
