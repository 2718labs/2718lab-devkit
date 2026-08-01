"""Create an inert, minimal AstrBot plugin skeleton on explicit request."""

from __future__ import annotations

import json
import keyword
from pathlib import Path

from .templates import TEMPLATES


class ScaffoldError(ValueError):
    """Raised when a scaffold target or plugin name is unsafe to use."""


def scaffold_plugin(
    plugin_name: str,
    destination: str | Path,
    *,
    author: str = "2718lab",
    repo: str | None = None,
    display_name: str | None = None,
) -> Path:
    """Create ``destination/plugin_name`` and return its path.

    The function only writes to the caller-selected destination. It does not
    import AstrBot, discover a data directory, or activate the generated plugin.
    """

    _validate_plugin_name(plugin_name)
    output_root = Path(destination)
    target = output_root / plugin_name
    if target.exists():
        raise ScaffoldError(f"Plugin directory already exists: {target}")

    rendered_display_name = display_name or _default_display_name(plugin_name)
    rendered_repo = repo or f"https://github.com/2718lab/{plugin_name}"
    suffix = plugin_name.removeprefix("astrbot_plugin_")
    values = {
        "plugin_name": plugin_name,
        "class_name": _class_name(plugin_name),
        "command": suffix.strip("_").replace("_", "-"),
        "handler_name": f"{_identifier_base(suffix)}_cmd",
        "display_name": rendered_display_name,
        "display_name_yaml": _yaml_string(rendered_display_name),
        "description": "A minimal AstrBot plugin.",
        "description_yaml": _yaml_string("A minimal AstrBot plugin."),
        "author_yaml": _yaml_string(author),
        "repo_yaml": _yaml_string(rendered_repo),
    }
    rendered_files = {
        relative_path: _render(template, values)
        for relative_path, template in TEMPLATES.items()
    }

    output_root.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    for relative_path, content in rendered_files.items():
        (target / relative_path).write_text(content, encoding="utf-8")
    return target


def _validate_plugin_name(plugin_name: str) -> None:
    if not plugin_name.startswith("astrbot_plugin_"):
        raise ScaffoldError("Plugin name must start with 'astrbot_plugin_'.")
    if not plugin_name.isidentifier() or keyword.iskeyword(plugin_name):
        raise ScaffoldError("Plugin name must be a valid Python identifier.")
    if not plugin_name.removeprefix("astrbot_plugin_").strip("_"):
        raise ScaffoldError("Plugin name must include a non-empty suffix.")


def _class_name(plugin_name: str) -> str:
    name_parts = _identifier_base(plugin_name.removeprefix("astrbot_plugin_")).split(
        "_"
    )
    class_stem = "".join(part.capitalize() for part in name_parts)
    if class_stem[0].isdigit():
        class_stem = "Plugin" + class_stem
    return class_stem + "Plugin"


def _identifier_base(suffix: str) -> str:
    return f"plugin_{suffix}" if suffix[0].isdigit() else suffix


def _default_display_name(plugin_name: str) -> str:
    return plugin_name.removeprefix("astrbot_plugin_").replace("_", " ").title()


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template
