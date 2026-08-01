from __future__ import annotations

from pathlib import Path

from devkit_astrbot.validator import validate_plugin

VALID_METADATA = """\
name: astrbot_plugin_echo
display_name: Echo
desc: Echoes a message.
version: v0.1.0
author: 2718lab
repo: https://github.com/2718lab/astrbot_plugin_echo
astrbot_version: \">=4.16,<5\"
"""

VALID_MAIN = """\
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class EchoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("echo")
    async def echo(self, event: AstrMessageEvent):
        \"\"\"Echo the request.\"\"\"
        yield event.plain_result("ok")

    async def terminate(self):
        pass
"""


def write_plugin(
    tmp_path: Path,
    *,
    metadata: str = VALID_METADATA,
    main: str = VALID_MAIN,
) -> Path:
    plugin = tmp_path / "astrbot_plugin_echo"
    plugin.mkdir()
    (plugin / "metadata.yaml").write_text(metadata, encoding="utf-8")
    (plugin / "main.py").write_text(main, encoding="utf-8")
    return plugin


def error_codes(plugin: Path) -> set[str]:
    return {diagnostic.code for diagnostic in validate_plugin(plugin).errors}


def test_valid_minimal_plugin_has_no_diagnostics(tmp_path: Path) -> None:
    report = validate_plugin(write_plugin(tmp_path))

    assert report.is_valid
    assert report.diagnostics == ()


def test_validator_detects_framework_lifecycle_and_handler_traps(
    tmp_path: Path,
) -> None:
    main = """\
import requests

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class EchoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def __del__(self):
        pass

    @filter.command("echo")
    @filter.command("repeat")
    async def echo(self, event: AstrMessageEvent):
        return "not sent"

    async def terminate(self):
        pass
"""

    codes = error_codes(write_plugin(tmp_path, main=main))

    assert {
        "banned-import",
        "forbidden-dunder-del",
        "multiple-command-filters",
        "handler-return-value",
    } <= codes


def test_validator_checks_schema_and_runtime_dependency(tmp_path: Path) -> None:
    plugin = write_plugin(tmp_path)
    (plugin / "_conf_schema.json").write_text(
        '{"mode": {"type": "dict", "description": "Mode", "default": {}}}',
        encoding="utf-8",
    )
    (plugin / "requirements.txt").write_text("astrbot==4.26.5\n", encoding="utf-8")

    codes = error_codes(plugin)

    assert "invalid-schema-type" in codes
    assert "astrbot-runtime-dependency" in codes


def test_validator_requires_metadata_and_entrypoint(tmp_path: Path) -> None:
    plugin = tmp_path / "empty"
    plugin.mkdir()

    codes = error_codes(plugin)

    assert "missing-main" in codes
    assert "missing-metadata" in codes
