from __future__ import annotations

from pathlib import Path

import pytest

from devkit_astrbot.scaffold import ScaffoldError, scaffold_plugin
from devkit_astrbot.validator import validate_plugin


def test_scaffold_creates_a_valid_minimal_plugin(tmp_path: Path) -> None:
    plugin = scaffold_plugin(
        "astrbot_plugin_echo",
        tmp_path,
        author="2718lab",
        repo="https://github.com/2718lab/astrbot_plugin_echo",
    )

    assert plugin == tmp_path / "astrbot_plugin_echo"
    assert {path.name for path in plugin.iterdir()} == {
        ".gitignore",
        "CHANGELOG.md",
        "README.md",
        "main.py",
        "metadata.yaml",
    }
    assert validate_plugin(plugin).is_valid

    main = (plugin / "main.py").read_text(encoding="utf-8")
    assert "StarTools.get_data_dir" not in main
    assert "asyncio.create_task" not in main
    assert "async def initialize" not in main


def test_scaffold_rejects_invalid_or_existing_plugin_names(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="astrbot_plugin_"):
        scaffold_plugin("not-a-plugin", tmp_path)

    scaffold_plugin("astrbot_plugin_echo", tmp_path)
    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold_plugin("astrbot_plugin_echo", tmp_path)


def test_scaffold_handles_a_numeric_plugin_suffix(tmp_path: Path) -> None:
    plugin = scaffold_plugin("astrbot_plugin_123", tmp_path)

    main = (plugin / "main.py").read_text(encoding="utf-8")
    assert "class Plugin123Plugin(Star):" in main
    assert "async def plugin_123_cmd" in main


def test_scaffold_handles_a_leading_underscore_before_digits(tmp_path: Path) -> None:
    plugin = scaffold_plugin("astrbot_plugin__123", tmp_path)

    main = (plugin / "main.py").read_text(encoding="utf-8")
    assert "class Plugin123Plugin(Star):" in main
    assert "async def _123_cmd" in main
