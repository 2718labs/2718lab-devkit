"""Conservative local project-index maintenance entry point."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .composition import RuntimeRoot
from .config import RuntimeConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devkit-index-maintenance")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("preview")
    apply = subcommands.add_parser("apply")
    apply.add_argument("--preview-id", required=True)
    compact = subcommands.add_parser("compact")
    compact.add_argument("--allow-full-rewrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    plugin_root = Path(__file__).resolve().parents[2]
    config = RuntimeConfig.load(protected_roots=(plugin_root,))
    root = RuntimeRoot(config)
    with root.open_uow(read_only=False) as uow:
        index = uow.project_checkpoint.project_index
        if arguments.command == "preview":
            result = index.preview_retention(uow.index_retention_references())
        elif arguments.command == "apply":
            with uow.index_retention_fence() as protected_snapshot_ids:
                result = index.apply_retention(
                    arguments.preview_id, protected_snapshot_ids
                )
        else:
            with uow.index_retention_fence():
                result = index.compact_storage(
                    allow_full_rewrite=arguments.allow_full_rewrite
                )
    print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))
    return 0 if result.blocked_reason is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
