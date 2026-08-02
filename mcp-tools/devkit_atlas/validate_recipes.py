"""Validate bundled Atlas recipe assets without changing them."""

from __future__ import annotations

import sys
from pathlib import Path


MCP_TOOLS = Path(__file__).resolve().parents[1]
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from devkit_atlas import ASSET_ROOT  # noqa: E402
from devkit_atlas.recipes import BundledRecipeLoader  # noqa: E402


def main() -> int:
    try:
        recipes = BundledRecipeLoader(ASSET_ROOT).load()
    except Exception:
        return 1
    print(f"Atlas recipes valid: {len(recipes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
