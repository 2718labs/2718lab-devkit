"""Export one verified local Code Atlas recipe as an atomic promotion bundle."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[3]
MCP_TOOLS = ROOT / "mcp-tools"
if str(MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(MCP_TOOLS))

from code_atlas.canonical import canonical_json, thaw_json  # noqa: E402
from code_atlas.models import EdgeRelation, NodeKind, RecipeManifest  # noqa: E402
from code_atlas.recipes import render_pattern_card  # noqa: E402
from code_atlas.security import MAX_RECIPE_BYTES, MAX_TEMPLATE_BYTES  # noqa: E402
from code_atlas.service import hydrate_local_manifest  # noqa: E402
from code_atlas.store import AtlasStore, StoreConflictError  # noqa: E402


_HASH_PREFIX = "sha256:"
_STAGE_PREFIX = ".code-atlas-stage-"


class PromotionError(ValueError):
    """A deliberately terse CLI failure that never carries untrusted values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _unsafe_status(path: Path, value: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & 0x400
        or (callable(is_junction) and is_junction(path))
    )


def _safe_existing(
    path: Path, *, directory: bool = False, regular: bool = False
) -> Path:
    """Return an absolute path only after every existing component is safe."""

    absolute = path.absolute()
    parts = absolute.parts
    if not parts:
        raise PromotionError("promotion_data_root_unsafe")
    cursor = Path(parts[0])
    try:
        for part in parts[1:]:
            cursor /= part
            value = cursor.lstat()
            if _unsafe_status(cursor, value):
                raise PromotionError("promotion_data_root_unsafe")
        final = absolute.lstat()
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_data_root_unsafe") from exc
    if (directory and not stat.S_ISDIR(final.st_mode)) or (
        regular and not stat.S_ISREG(final.st_mode)
    ):
        raise PromotionError("promotion_data_root_unsafe")
    return absolute


def _safe_output_parent(path: Path) -> Path:
    try:
        absolute = path.absolute()
        parts = absolute.parts
        if not parts:
            raise PromotionError("promotion_output_unsafe")
        cursor = Path(parts[0])
        for part in parts[1:]:
            cursor /= part
            value = cursor.lstat()
            if _unsafe_status(cursor, value):
                raise PromotionError("promotion_output_unsafe")
        if not stat.S_ISDIR(absolute.lstat().st_mode):
            raise PromotionError("promotion_output_unsafe")
        return absolute
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc


def _overlaps(left: Path, right: Path) -> bool:
    left_value = left.absolute()
    right_value = right.absolute()
    try:
        left_value.relative_to(right_value)
        return True
    except ValueError:
        try:
            right_value.relative_to(left_value)
            return True
        except ValueError:
            return False


def _contains_plugin_cache(path: Path) -> bool:
    components = [part.casefold() for part in path.absolute().parts]
    return any(
        first == "plugins" and second == "cache"
        for first, second in zip(components, components[1:], strict=False)
    )


def _digest(data: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _manifest_from_store(store: AtlasStore, recipe_id: str) -> RecipeManifest:
    metadata = store.recipe_metadata(recipe_id)
    if (
        metadata is None
        or metadata.get("recipe_id") != recipe_id
        or metadata.get("layer") != "local"
        or metadata.get("state") not in {"", "ready", "READY", None}
    ):
        raise PromotionError("promotion_recipe_invalid")
    graph = store.graph_query(
        (recipe_id,),
        max_nodes=200,
        max_edges=400,
        max_depth=4,
        byte_budget=MAX_RECIPE_BYTES,
    )
    roots = [node for node in graph.nodes if node.node_id == recipe_id]
    if graph.truncated or len(roots) != 1 or roots[0].kind is not NodeKind.RECIPE:
        raise PromotionError("promotion_recipe_invalid")
    try:
        manifest = hydrate_local_manifest(roots[0])
    except Exception as exc:
        raise PromotionError("promotion_recipe_invalid") from exc
    expected = {
        "intent_id": manifest.intent_id,
        "language": manifest.language_name,
        "framework": manifest.framework_name or "",
        "layer": manifest.layer,
        "version": manifest.version,
        "manifest_hash": manifest.manifest_hash,
        "repository_signature": manifest.repository_signature,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise PromotionError("promotion_recipe_invalid")
    if manifest.quarantine_state not in (None, "", "ready", "READY"):
        raise PromotionError("promotion_recipe_invalid")
    template_hashes = tuple(
        sorted({operation.template_hash for operation in manifest.operations})
    )
    if not template_hashes or len(template_hashes) > 8:
        raise PromotionError("promotion_recipe_invalid")
    template_nodes: dict[str, str] = {}
    implementation_edges: set[tuple[str, str]] = set()
    for node in graph.nodes:
        if node.kind is not NodeKind.CODE_TEMPLATE:
            continue
        payload = thaw_json(node.payload)
        if (
            type(payload) is not dict
            or set(payload) != {"template_hash", "kind"}
            or not isinstance(payload["template_hash"], str)
            or not isinstance(payload["kind"], str)
        ):
            raise PromotionError("promotion_recipe_invalid")
        if node.node_id in template_nodes:
            raise PromotionError("promotion_recipe_invalid")
        template_nodes[node.node_id] = payload["template_hash"]
    for edge in graph.edges:
        if edge.relation is EdgeRelation.HAS_IMPLEMENTATION:
            implementation_edges.add((edge.source_id, edge.target_id))
    if len(template_nodes) != len(template_hashes) or set(
        template_nodes.values()
    ) != set(template_hashes):
        raise PromotionError("promotion_recipe_invalid")
    if implementation_edges != {(recipe_id, node_id) for node_id in template_nodes}:
        raise PromotionError("promotion_recipe_invalid")
    return manifest


def _write_file(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PromotionError("promotion_write_failed") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _remove_stage(stage: Path | None, parent: Path) -> None:
    if (
        stage is None
        or stage.parent != parent
        or not stage.name.startswith(_STAGE_PREFIX)
    ):
        return
    try:
        value = stage.lstat()
        if _unsafe_status(stage, value) or not stat.S_ISDIR(value.st_mode):
            return
        shutil.rmtree(stage)
    except OSError:
        return


def _build_files(store: AtlasStore, manifest: RecipeManifest) -> dict[str, bytes]:
    templates: dict[str, bytes] = {}
    total = 0
    for template_hash in sorted({item.template_hash for item in manifest.operations}):
        try:
            body = store.read_blob_verified(template_hash, max_bytes=MAX_TEMPLATE_BYTES)
        except StoreConflictError as exc:
            raise PromotionError("promotion_recipe_invalid") from exc
        total += len(body)
        if total > MAX_RECIPE_BYTES:
            raise PromotionError("promotion_recipe_invalid")
        templates[f"templates/sha256/{template_hash.removeprefix(_HASH_PREFIX)}"] = body
    files = {
        "manifest.json": canonical_json(manifest.to_dict()).encode("utf-8") + b"\n",
        "pattern-card.md": render_pattern_card(manifest).encode("utf-8"),
        **templates,
    }
    if len(files) + 1 > 11:
        raise PromotionError("promotion_recipe_invalid")
    records = [
        {"path": path, "sha256": _digest(body), "size": len(body)}
        for path, body in sorted(files.items())
    ]
    receipt = {
        "schema_version": "1",
        "recipe_id": manifest.recipe_id,
        "manifest_hash": manifest.manifest_hash,
        "records": records,
    }
    files["promotion-receipt.json"] = canonical_json(receipt).encode("utf-8") + b"\n"
    return files


def _write_bundle(parent: Path, output: Path, files: dict[str, bytes]) -> None:
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=parent))
        value = stage.lstat()
        if _unsafe_status(stage, value) or not stat.S_ISDIR(value.st_mode):
            raise PromotionError("promotion_write_failed")
        for relative, body in sorted(files.items()):
            target = stage.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_file(target, body)
            _fsync_directory(target.parent)
        for relative, expected in files.items():
            target = stage.joinpath(*relative.split("/"))
            try:
                if target.read_bytes() != expected or _digest(
                    target.read_bytes()
                ) != _digest(expected):
                    raise PromotionError("promotion_write_failed")
            except OSError as exc:
                raise PromotionError("promotion_write_failed") from exc
        _fsync_directory(stage)
        try:
            output.lstat()
        except FileNotFoundError:
            pass
        else:
            raise PromotionError("promotion_output_raced")
        try:
            os.rename(stage, output)
        except FileExistsError as exc:
            raise PromotionError("promotion_output_raced") from exc
        except OSError as exc:
            raise PromotionError("promotion_write_failed") from exc
        stage = None
        _fsync_directory(parent)
    finally:
        _remove_stage(stage, parent)


def export_recipe(data_root: Path, recipe_id: str, output: Path) -> None:
    durable_root = _safe_existing(data_root, directory=True)
    if _overlaps(durable_root, ROOT):
        raise PromotionError("promotion_data_root_unsafe")
    if not isinstance(recipe_id, str) or not (
        recipe_id.startswith(_HASH_PREFIX)
        and len(recipe_id) == len(_HASH_PREFIX) + 64
        and all(character in "0123456789abcdef" for character in recipe_id[7:])
    ):
        raise PromotionError("promotion_recipe_invalid")
    parent = _safe_output_parent(output.parent)
    final = output.absolute()
    if not final.name or _contains_plugin_cache(final):
        raise PromotionError("promotion_output_unsafe")
    if _overlaps(final, durable_root) or _overlaps(final, ROOT):
        raise PromotionError("promotion_output_unsafe")
    try:
        existing = final.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    if existing is not None:
        if _unsafe_status(final, existing):
            raise PromotionError("promotion_output_unsafe")
        raise PromotionError("promotion_output_exists")
    try:
        store = AtlasStore.open_readonly(
            durable_root / "code-atlas.sqlite3",
            durable_root / "code-atlas-cas",
        )
    except StoreConflictError as exc:
        raise PromotionError("promotion_db_open_failed") from exc
    try:
        try:
            manifest = _manifest_from_store(store, recipe_id)
            files = _build_files(store, manifest)
        except PromotionError:
            raise
        except (OSError, StoreConflictError, TypeError, ValueError) as exc:
            raise PromotionError("promotion_recipe_invalid") from exc
    finally:
        store.close()
    _write_bundle(parent, final, files)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="export_recipe.py")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--recipe-id", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    try:
        values = _parser().parse_args(None if argv is None else list(argv))
        export_recipe(Path(values.data_root), values.recipe_id, Path(values.output))
    except PromotionError as error:
        print(error.code, file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print("promotion_write_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
