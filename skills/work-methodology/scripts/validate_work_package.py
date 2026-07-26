"""Validate a layered 2718lab work package.

A package separates the product-facing direction from coordinator metadata and
agent-scoped implementation cards. The validator intentionally checks shape,
not business correctness.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PRODUCT_BRIEF = "product-brief.md"
INDEX = "index.md"
TASKS = "tasks"
MAX_PRODUCT_LINES = 120
MAX_INDEX_LINES = 160
MAX_TASK_LINES = 220
PRODUCT_HEADINGS = ("Goal", "Scope", "Direction", "Risk Gate", "Done")
INDEX_HEADINGS = ("Shared Contracts", "Tasks", "Dispatch")
TASK_HEADINGS = ("Goal", "Context", "Write Scope", "Steps", "Acceptance", "Return")
OWNER_RE = re.compile(r"^Owner:\s*([A-Za-z0-9._-]+)\s*$", re.MULTILINE)
TASK_REF_RE = re.compile(r"`(tasks/[A-Za-z0-9._-]+\.md)`")
WRITE_SCOPE_RE = re.compile(
    r"^##\s+Write Scope\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
WRITE_PATH_RE = re.compile(r"^\s*-\s+`([^`]+)`", re.MULTILINE)
READ_ONLY_SCOPE_RE = re.compile(r"^\s*-\s+none\s*$", re.IGNORECASE)
STRICT_INDEX_SEQUENCE = (
    "project_index_sync",
    "workflow_register_task",
    "strict_index=true",
)
STRICT_TASK_SEQUENCE = (
    "project_index_query",
    "trace_id",
    "worktree_checkpoint_create",
    'project_index_sync(bind_as="output")',
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)
STRICT_READ_ONLY_TASK_SEQUENCE = (
    "project_index_query",
    "trace_id",
    'workflow_artifact_register(kind="verification", snapshot_id=...)',
    "workflow_complete",
)
DOCUMENTATION_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})


def _read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"missing required file: {path.name}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path.name}: must be UTF-8")
        return ""


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _require_headings(
    path: Path, text: str, headings: tuple[str, ...], errors: list[str]
) -> None:
    for heading in headings:
        if not re.search(
            rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE | re.IGNORECASE
        ):
            errors.append(f"{path.name}: missing heading '## {heading}'")


def _require_ordered_markers(
    path: Path,
    text: str,
    markers: tuple[str, ...],
    errors: list[str],
) -> None:
    cursor = 0
    for marker in markers:
        position = text.find(marker, cursor)
        if position < 0:
            errors.append(
                f"{path.name}: strict index missing or out-of-order marker: {marker}"
            )
            continue
        cursor = position + len(marker)


def _write_scope_paths(text: str) -> tuple[str, ...]:
    section = WRITE_SCOPE_RE.search(text)
    if section is None:
        return ()
    return tuple(WRITE_PATH_RE.findall(section.group("body")))


def _is_read_only_scope(text: str) -> bool:
    section = WRITE_SCOPE_RE.search(text)
    if section is None:
        return False
    return READ_ONLY_SCOPE_RE.fullmatch(section.group("body").strip()) is not None


def _is_documentation_scope(paths: tuple[str, ...]) -> bool:
    if not paths:
        return False
    for value in paths:
        normalized = value.replace("\\", "/").lower()
        suffix = Path(normalized).suffix
        if (
            not normalized.startswith(("docs/", "documentation/"))
            and suffix not in DOCUMENTATION_SUFFIXES
        ):
            return False
    return True


def _validate_product_brief(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_PRODUCT_LINES:
        errors.append(
            f"product-brief.md: {lines} lines exceeds the 120-line product budget"
        )
    if "```" in text:
        errors.append("product-brief.md: code fences belong in task cards or contracts")
    _require_headings(path, text, PRODUCT_HEADINGS, errors)


def _validate_index(root: Path, path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_INDEX_LINES:
        errors.append(
            f"index.md: {lines} lines exceeds the 160-line coordinator budget"
        )
    _require_headings(path, text, INDEX_HEADINGS, errors)
    for relative in TASK_REF_RE.findall(text):
        if not (root / relative).is_file():
            errors.append(f"index.md: referenced task does not exist: {relative}")


def _validate_task(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    lines = _line_count(text)
    if lines > MAX_TASK_LINES:
        errors.append(
            f"{path.name}: {lines} lines exceeds the 220-line task-card budget"
        )

    owner_lines = re.findall(r"^Owner:\s*(.+?)\s*$", text, re.MULTILINE)
    owner = OWNER_RE.search(text)
    if len(owner_lines) != 1 or owner is None:
        errors.append(f"{path.name}: task card must declare exactly one owner")

    _require_headings(path, text, TASK_HEADINGS, errors)
    write_scope = WRITE_SCOPE_RE.search(text)
    if write_scope is None or (
        not WRITE_PATH_RE.search(write_scope.group("body"))
        and not _is_read_only_scope(text)
    ):
        errors.append(f"{path.name}: Write Scope must list at least one exact path")


def _validate_strict_task(path: Path, errors: list[str]) -> None:
    text = _read(path, errors)
    if not text:
        return
    if _is_read_only_scope(text):
        _require_ordered_markers(path, text, STRICT_READ_ONLY_TASK_SEQUENCE, errors)
        return

    _require_ordered_markers(path, text, STRICT_TASK_SEQUENCE, errors)

    paths = _write_scope_paths(text)
    if _is_documentation_scope(paths):
        return

    owner_match = OWNER_RE.search(text)
    owner = owner_match.group(1).lower() if owner_match else ""
    if "luna" in owner or "terra" in owner:
        errors.append(f"{path.name}: Luna/Terra must never receive a code write scope")
    if "sol" not in owner:
        errors.append(f"{path.name}: code write scope requires a Sol code writer")
    for marker in ("gpt-5.6-sol", "ultra"):
        if marker not in text:
            errors.append(f"{path.name}: code writer dispatch missing marker: {marker}")


def validate_work_package(
    root: Path | str,
    *,
    strict_index: bool = False,
) -> list[str]:
    package_root = Path(root).resolve()
    errors: list[str] = []
    if not package_root.is_dir():
        return [f"work package directory does not exist: {package_root}"]

    _validate_product_brief(package_root / PRODUCT_BRIEF, errors)
    index_path = package_root / INDEX
    _validate_index(package_root, index_path, errors)
    if strict_index:
        index_text = _read(index_path, errors)
        if index_text:
            _require_ordered_markers(
                index_path,
                index_text,
                STRICT_INDEX_SEQUENCE,
                errors,
            )

    tasks_dir = package_root / TASKS
    task_files = sorted(tasks_dir.glob("*.md")) if tasks_dir.is_dir() else []
    if not task_files:
        errors.append("tasks/: at least one agent-scoped task card is required")
    for task in task_files:
        _validate_task(task, errors)
        if strict_index:
            _validate_strict_task(task, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package", type=Path, help="Directory containing product-brief.md and index.md"
    )
    parser.add_argument(
        "--strict-index",
        action="store_true",
        help="Require index-first routing and strict completion gates",
    )
    args = parser.parse_args(argv)
    errors = validate_work_package(args.package, strict_index=args.strict_index)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: layered work package is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
