"""Export one verified local Code Atlas recipe as an atomic promotion bundle."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
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


def _object_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return the stable file-object identity used to detect replacement."""

    return _identity(value)[:3]


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


def _capture_output_parent(parent: Path) -> tuple[int, int, int]:
    """Validate and identify the current output parent object."""

    safe_parent = _safe_output_parent(parent)
    try:
        value = safe_parent.lstat()
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    if _unsafe_status(safe_parent, value) or not stat.S_ISDIR(value.st_mode):
        raise PromotionError("promotion_output_unsafe")
    return _object_identity(value)


def _assert_current_stage(
    parent: Path,
    parent_identity: tuple[int, int, int],
    stage: Path,
    stage_identity: tuple[int, int, int],
) -> None:
    """Fail closed before a write if parent/stage lookup was redirected."""

    try:
        if _capture_output_parent(parent) != parent_identity:
            raise PromotionError("promotion_output_raced")
        value = stage.lstat()
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_output_raced") from exc
    if (
        _unsafe_status(stage, value)
        or not stat.S_ISDIR(value.st_mode)
        or _object_identity(value) != stage_identity
        or stage.parent != parent
    ):
        raise PromotionError("promotion_output_raced")


def _make_stage_parent(
    stage: Path,
    relative: str,
    directories: dict[Path, tuple[int, int, int]],
) -> Path:
    """Create exactly the known stage directories; reject a substituted path."""

    current = stage
    for component in relative.split("/")[:-1]:
        current = current / component
        known = directories.get(current)
        try:
            value = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
                value = current.lstat()
            except OSError as exc:
                raise PromotionError("promotion_write_failed") from exc
            if _unsafe_status(current, value) or not stat.S_ISDIR(value.st_mode):
                raise PromotionError("promotion_write_failed")
            directories[current] = _object_identity(value)
            continue
        except OSError as exc:
            raise PromotionError("promotion_write_failed") from exc
        if (
            known is None
            or _unsafe_status(current, value)
            or not stat.S_ISDIR(value.st_mode)
            or _object_identity(value) != known
        ):
            raise PromotionError("promotion_write_failed")
    return current


def _verify_stage_file(
    path: Path,
    expected: bytes,
    identity: tuple[int, int, int],
) -> None:
    """Verify one file without accepting replacement between verification steps."""

    try:
        before = path.lstat()
        if (
            _unsafe_status(path, before)
            or not stat.S_ISREG(before.st_mode)
            or _object_identity(before) != identity
        ):
            raise PromotionError("promotion_write_failed")
        actual = path.read_bytes()
        after = path.lstat()
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("promotion_write_failed") from exc
    if (
        _object_identity(after) != identity
        or actual != expected
        or _digest(actual) != _digest(expected)
    ):
        raise PromotionError("promotion_write_failed")


def _atomic_noreplace_directory(stage: Path, destination: Path) -> None:
    """Atomically publish a directory only if the destination is absent."""

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move_file.restype = ctypes.c_int
        if move_file(str(stage), str(destination), 0):
            return
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "destination exists", str(destination))
        raise OSError(error, "MoveFileExW failed", str(destination))
    if os.name == "posix":
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                -100,
                os.fsencode(stage),
                -100,
                os.fsencode(destination),
                1,
            )
            == 0
        ):
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "destination exists", str(destination))
        raise OSError(error, "renameat2 failed", str(destination))
    raise OSError(errno.ENOSYS, "atomic no-replace promotion is unavailable")


def _remove_stage(
    stage: Path | None,
    parent: Path,
    parent_identity: tuple[int, int, int] | None,
    stage_identity: tuple[int, int, int] | None,
    files: dict[Path, tuple[int, int, int]],
    directories: dict[Path, tuple[int, int, int]],
) -> None:
    """Remove only the still-owned stage entries; never recurse through a race."""

    if (
        stage is None
        or parent_identity is None
        or stage_identity is None
        or stage.parent != parent
        or not stage.name.startswith(_STAGE_PREFIX)
    ):
        return
    try:
        if _capture_output_parent(parent) != parent_identity:
            return
        stage_status = stage.lstat()
        if (
            _unsafe_status(stage, stage_status)
            or not stat.S_ISDIR(stage_status.st_mode)
            or _object_identity(stage_status) != stage_identity
        ):
            return
        for path, identity in sorted(
            files.items(), key=lambda item: len(item[0].parts), reverse=True
        ):
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            if (
                _unsafe_status(path, value)
                or not stat.S_ISREG(value.st_mode)
                or _object_identity(value) != identity
            ):
                continue
            path.unlink()
        for path, identity in sorted(
            directories.items(), key=lambda item: len(item[0].parts), reverse=True
        ):
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            if (
                _unsafe_status(path, value)
                or not stat.S_ISDIR(value.st_mode)
                or _object_identity(value) != identity
            ):
                continue
            path.rmdir()
        stage.rmdir()
    except (OSError, PromotionError):
        return


def _prepare_readonly_scratch(
    parent: Path, parent_identity: tuple[int, int, int]
) -> Path:
    """Create a verified sibling scratch root without entering durable data."""

    scratch = parent / ".code-atlas-export-scratch"
    try:
        scratch.mkdir(exist_ok=True)
        value = scratch.lstat()
    except OSError as exc:
        raise PromotionError("promotion_output_unsafe") from exc
    if (
        _unsafe_status(scratch, value)
        or not stat.S_ISDIR(value.st_mode)
        or _capture_output_parent(parent) != parent_identity
    ):
        raise PromotionError("promotion_output_raced")
    return scratch


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


def _write_bundle(
    parent: Path,
    output: Path,
    files: dict[str, bytes],
    *,
    expected_parent_identity: tuple[int, int, int] | None = None,
) -> None:
    stage: Path | None = None
    parent_identity: tuple[int, int, int] | None = None
    stage_identity: tuple[int, int, int] | None = None
    owned_files: dict[Path, tuple[int, int, int]] = {}
    owned_directories: dict[Path, tuple[int, int, int]] = {}
    try:
        parent_identity = _capture_output_parent(parent)
        if (
            expected_parent_identity is not None
            and parent_identity != expected_parent_identity
        ):
            raise PromotionError("promotion_output_raced")
        stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=parent))
        value = stage.lstat()
        if _unsafe_status(stage, value) or not stat.S_ISDIR(value.st_mode):
            raise PromotionError("promotion_output_raced")
        stage_identity = _object_identity(value)
        _assert_current_stage(parent, parent_identity, stage, stage_identity)
        for relative, body in sorted(files.items()):
            _assert_current_stage(parent, parent_identity, stage, stage_identity)
            target = stage.joinpath(*relative.split("/"))
            _make_stage_parent(stage, relative, owned_directories)
            _assert_current_stage(parent, parent_identity, stage, stage_identity)
            _write_file(target, body)
            try:
                target_status = target.lstat()
            except OSError as exc:
                _assert_current_stage(parent, parent_identity, stage, stage_identity)
                raise PromotionError("promotion_write_failed") from exc
            if _unsafe_status(target, target_status) or not stat.S_ISREG(
                target_status.st_mode
            ):
                raise PromotionError("promotion_write_failed")
            owned_files[target] = _object_identity(target_status)
            _assert_current_stage(parent, parent_identity, stage, stage_identity)
            _fsync_directory(target.parent)
        for relative, expected in files.items():
            target = stage.joinpath(*relative.split("/"))
            _assert_current_stage(parent, parent_identity, stage, stage_identity)
            identity = owned_files.get(target)
            if identity is None:
                raise PromotionError("promotion_write_failed")
            _verify_stage_file(target, expected, identity)
        _fsync_directory(stage)
        _assert_current_stage(parent, parent_identity, stage, stage_identity)
        try:
            _atomic_noreplace_directory(stage, output)
        except FileExistsError as exc:
            raise PromotionError("promotion_output_raced") from exc
        except OSError as exc:
            raise PromotionError("promotion_write_failed") from exc
        try:
            if _capture_output_parent(parent) != parent_identity:
                raise PromotionError("promotion_output_raced")
            output_status = output.lstat()
        except PromotionError:
            raise
        except OSError as exc:
            raise PromotionError("promotion_output_raced") from exc
        if (
            _unsafe_status(output, output_status)
            or not stat.S_ISDIR(output_status.st_mode)
            or _object_identity(output_status) != stage_identity
        ):
            raise PromotionError("promotion_output_raced")
        stage = None
        _fsync_directory(parent)
    finally:
        _remove_stage(
            stage,
            parent,
            parent_identity,
            stage_identity,
            owned_files,
            owned_directories,
        )


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
    initial_parent_identity = _capture_output_parent(parent)
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
    scratch = _prepare_readonly_scratch(parent, initial_parent_identity)
    try:
        store = AtlasStore.open_readonly(
            durable_root / "code-atlas.sqlite3",
            durable_root / "code-atlas-cas",
            scratch_root=scratch,
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
    if _capture_output_parent(parent) != initial_parent_identity:
        raise PromotionError("promotion_output_raced")
    _write_bundle(
        parent,
        final,
        files,
        expected_parent_identity=initial_parent_identity,
    )


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
