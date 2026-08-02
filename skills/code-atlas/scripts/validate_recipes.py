"""Validate bundled Code Atlas recipe assets without changing them."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp-tools"))

from code_atlas.recipes import BundledRecipeLoader  # noqa: E402


def main() -> int:
    try:
        recipes = BundledRecipeLoader(
            PLUGIN_ROOT / "skills" / "code-atlas" / "assets"
        ).load()
    except Exception:
        return 1
    print(f"Code Atlas recipes valid: {len(recipes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
