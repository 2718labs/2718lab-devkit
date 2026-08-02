"""Static AstrBot plugin validation with no AstrBot runtime dependency."""

from __future__ import annotations

import ast
import json
import keyword
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warning"]

BANNED_IMPORTS = {
    "botpy": "use AstrBot platform adapters instead of botpy",
    "discord": "use AstrBot platform adapters instead of discord.py",
    "graia": "AstrBot plugins cannot mix Graia APIs",
    "khl": "use AstrBot platform adapters instead of khl.py",
    "koishi": "AstrBot plugins cannot mix Koishi APIs",
    "nonebot": "AstrBot plugins cannot mix NoneBot APIs",
    "nonebot2": "AstrBot plugins cannot mix NoneBot APIs",
    "quart": "AstrBot v4.26+ web APIs use astrbot.api.web",
    "requests": "use an async HTTP client in an AstrBot plugin",
    "telebot": "use AstrBot platform adapters instead of pyTelegramBotAPI",
    "telegram": "use AstrBot platform adapters instead of python-telegram-bot",
}
CORE_OVERLAP_PACKAGES = {"aiohttp", "openai", "pillow", "psutil", "pydantic", "quart"}
VALID_SCHEMA_TYPES = {
    "bool",
    "file",
    "float",
    "int",
    "list",
    "object",
    "string",
    "template_list",
    "text",
}
IGNORED_SOURCE_PARTS = {".git", ".venv", "__pycache__", "tests", "venv"}
METADATA_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One static validation finding."""

    severity: Severity
    code: str
    message: str
    path: Path | None = None
    line: int | None = None

    def render(self, root: Path) -> str:
        """Render a concise, stable command-line representation."""

        location = ""
        if self.path is not None:
            try:
                location = self.path.relative_to(root).as_posix()
            except ValueError:
                location = str(self.path)
            if self.line is not None:
                location = f"{location}:{self.line}"
            location += ": "
        return f"{self.severity.upper()} {self.code}: {location}{self.message}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Immutable result returned by :func:`validate_plugin`."""

    root: Path
    diagnostics: tuple[Diagnostic, ...]

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Return required fixes."""

        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        """Return non-blocking findings."""

        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def is_valid(self) -> bool:
        """Whether no error diagnostics were reported."""

        return not self.errors

    def render(self) -> str:
        """Render the report for the CLI."""

        lines = [item.render(self.root) for item in self.diagnostics]
        lines.append(f"{len(self.errors)} errors, {len(self.warnings)} warnings")
        return "\n".join(lines)


def validate_plugin(plugin_directory: str | Path) -> ValidationReport:
    """Statically validate an AstrBot plugin without importing its code."""

    root = Path(plugin_directory).resolve()
    diagnostics: list[Diagnostic] = []
    if not root.is_dir():
        _error(
            diagnostics,
            "missing-plugin-directory",
            "Plugin directory does not exist.",
            root,
        )
        return ValidationReport(root=root, diagnostics=tuple(diagnostics))

    metadata = _check_metadata(root, diagnostics)
    uses_web_api = _check_python(root, diagnostics)
    _check_schema(root, diagnostics)
    _check_requirements(root, diagnostics)
    _check_web_api_version(root, metadata, uses_web_api, diagnostics)
    return ValidationReport(root=root, diagnostics=tuple(diagnostics))


def _check_metadata(root: Path, diagnostics: list[Diagnostic]) -> dict[str, str]:
    path = root / "metadata.yaml"
    if not path.is_file():
        _error(diagnostics, "missing-metadata", "metadata.yaml is required.", path)
        return {}

    fields = _parse_metadata(path, diagnostics)
    for field in ("name", "desc", "version", "author"):
        if not fields.get(field):
            _error(
                diagnostics,
                "missing-metadata-field",
                f"metadata.yaml requires a non-empty '{field}' field.",
                path,
            )

    name = fields.get("name", "")
    if name:
        if not name.isidentifier() or keyword.iskeyword(name):
            _error(
                diagnostics,
                "invalid-plugin-name",
                "metadata name must be a valid Python identifier.",
                path,
            )
        elif not name.startswith("astrbot_plugin_"):
            _warning(
                diagnostics,
                "plugin-name-prefix",
                "metadata name should start with 'astrbot_plugin_'.",
                path,
            )
        if name != root.name:
            _warning(
                diagnostics,
                "name-directory-mismatch",
                "metadata name does not match the plugin directory name.",
                path,
            )

    version = fields.get("version", "")
    if version and not re.fullmatch(r"v\d+(?:\.\d+){1,2}", version):
        _error(
            diagnostics,
            "invalid-metadata-version",
            "metadata version must use an AstrBot v-prefixed release version.",
            path,
        )
    if not fields.get("repo"):
        _warning(
            diagnostics,
            "missing-repo",
            "metadata repo is recommended for updates.",
            path,
        )
    if not fields.get("astrbot_version"):
        _warning(
            diagnostics,
            "missing-astrbot-version",
            "metadata astrbot_version should declare a compatibility range.",
            path,
        )
    elif fields["astrbot_version"].startswith("v"):
        _error(
            diagnostics,
            "invalid-astrbot-version",
            "astrbot_version must not start with 'v'.",
            path,
        )
    return fields


def _parse_metadata(path: Path, diagnostics: list[Diagnostic]) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _error(diagnostics, "unreadable-metadata", str(error), path)
        return fields

    for line in lines:
        if line.startswith((" ", "\t")):
            continue
        match = METADATA_LINE.match(line)
        if match is None:
            continue
        fields[match.group(1)] = _yaml_scalar(match.group(2))
    return fields


def _yaml_scalar(raw_value: str) -> str:
    value = raw_value.strip()
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None:
            value = value[:index].rstrip()
            break
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _check_python(root: Path, diagnostics: list[Diagnostic]) -> bool:
    main_path = root / "main.py"
    if not main_path.is_file():
        _error(diagnostics, "missing-main", "main.py is required.", main_path)

    star_classes: list[tuple[Path, ast.ClassDef]] = []
    uses_web_api = False
    for path in _python_sources(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except OSError as error:
            _error(diagnostics, "unreadable-python", str(error), path)
            continue
        except SyntaxError as error:
            _error(
                diagnostics,
                "python-syntax-error",
                error.msg,
                path,
                error.lineno,
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                _check_import_names(path, node.names, diagnostics)
            elif isinstance(node, ast.ImportFrom) and node.module:
                _check_import_names(path, [node], diagnostics)
                if node.module == "astrbot.api.web":
                    uses_web_api = True
            elif isinstance(node, ast.ClassDef) and _is_star_class(node):
                star_classes.append((path, node))

    if not star_classes:
        _error(
            diagnostics,
            "missing-star-class",
            "No class directly inheriting from Star was found.",
            main_path,
        )
        return uses_web_api
    if len(star_classes) > 1:
        _error(
            diagnostics,
            "multiple-star-classes",
            "Only one Star subclass may exist in a plugin.",
            root,
        )

    for path, class_node in star_classes:
        if path != main_path:
            _error(
                diagnostics,
                "star-class-outside-main",
                "The Star subclass must be defined in main.py.",
                path,
                class_node.lineno,
            )
    _check_star_class(main_path, star_classes[0][1], diagnostics)
    return uses_web_api


def _python_sources(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*.py")
        if not IGNORED_SOURCE_PARTS.intersection(path.relative_to(root).parts)
    ]


def _check_import_names(
    path: Path,
    names: list[ast.alias] | list[ast.ImportFrom],
    diagnostics: list[Diagnostic],
) -> None:
    for name in names:
        module = name.name if isinstance(name, ast.alias) else name.module
        if module is None:
            continue
        top_level = module.split(".", maxsplit=1)[0]
        reason = BANNED_IMPORTS.get(top_level)
        if reason:
            _error(
                diagnostics,
                "banned-import",
                f"Importing '{module}' is not allowed: {reason}.",
                path,
                getattr(name, "lineno", None),
            )


def _is_star_class(class_node: ast.ClassDef) -> bool:
    return any(
        _expression_name(base).split(".")[-1] == "Star" for base in class_node.bases
    )


def _expression_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _check_star_class(
    path: Path,
    class_node: ast.ClassDef,
    diagnostics: list[Diagnostic],
) -> None:
    methods = {
        method.name: method
        for method in class_node.body
        if isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    if "__del__" in methods:
        _error(
            diagnostics,
            "forbidden-dunder-del",
            "__del__ prevents AstrBot from calling terminate().",
            path,
            methods["__del__"].lineno,
        )
    if "terminate" not in methods:
        _warning(
            diagnostics,
            "missing-terminate",
            "Define terminate() directly on the Star subclass when resources need cleanup.",
            path,
            class_node.lineno,
        )

    for method in methods.values():
        decorators = [_decorator_name(decorator) for decorator in method.decorator_list]
        command_filters = [
            name
            for name in decorators
            if name
            in {"command", "filter.command", "command_group", "filter.command_group"}
        ]
        if len(command_filters) > 1:
            _error(
                diagnostics,
                "multiple-command-filters",
                "Multiple command filters are combined with AND; use alias instead.",
                path,
                method.lineno,
            )

        is_message_handler = any(
            name
            in {
                "command",
                "filter.command",
                "regex",
                "filter.regex",
                "event_message_type",
                "filter.event_message_type",
            }
            for name in decorators
        )
        if is_message_handler:
            _check_handler(path, method, diagnostics)
        if any(name in {"llm_tool", "filter.llm_tool"} for name in decorators):
            _check_llm_tool(path, method, diagnostics)


def _decorator_name(decorator: ast.expr) -> str:
    function = decorator.func if isinstance(decorator, ast.Call) else decorator
    return _expression_name(function)


def _check_handler(
    path: Path,
    method: ast.AsyncFunctionDef | ast.FunctionDef,
    diagnostics: list[Diagnostic],
) -> None:
    if not isinstance(method, ast.AsyncFunctionDef):
        _error(
            diagnostics,
            "handler-not-async",
            "AstrBot message handlers must be async def.",
            path,
            method.lineno,
        )
        return
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(method)):
        return
    if any(
        isinstance(node, ast.Return) and node.value is not None
        for node in ast.walk(method)
    ):
        _error(
            diagnostics,
            "handler-return-value",
            "Returning a value does not send a message; yield event.plain_result(...).",
            path,
            method.lineno,
        )
    else:
        _warning(
            diagnostics,
            "handler-without-yield",
            "Confirm that this handler intentionally sends no reply.",
            path,
            method.lineno,
        )


def _check_llm_tool(
    path: Path,
    method: ast.AsyncFunctionDef | ast.FunctionDef,
    diagnostics: list[Diagnostic],
) -> None:
    docstring = ast.get_docstring(method) or ""
    if "Args:" not in docstring:
        _error(
            diagnostics,
            "missing-llm-tool-args",
            "llm_tool docstrings require an Args: section.",
            path,
            method.lineno,
        )
    elif not re.search(
        r"\w+\s*\(\s*(string|number|boolean|object|array)\s*\)", docstring
    ):
        _error(
            diagnostics,
            "invalid-llm-tool-args",
            "llm_tool Args entries need a supported schema type.",
            path,
            method.lineno,
        )


def _check_schema(root: Path, diagnostics: list[Diagnostic]) -> None:
    path = root / "_conf_schema.json"
    if not path.is_file():
        return
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        _error(diagnostics, "unreadable-schema", str(error), path)
        return
    except json.JSONDecodeError as error:
        _error(diagnostics, "invalid-schema-json", error.msg, path, error.lineno)
        return
    if not isinstance(schema, dict):
        _error(
            diagnostics, "invalid-schema-root", "Schema root must be an object.", path
        )
        return
    for key, item in schema.items():
        if not isinstance(item, dict):
            _error(
                diagnostics,
                "invalid-schema-item",
                f"Schema item '{key}' must be an object.",
                path,
            )
            continue
        schema_type = item.get("type")
        if schema_type not in VALID_SCHEMA_TYPES:
            _error(
                diagnostics,
                "invalid-schema-type",
                f"Schema item '{key}' has unsupported type {schema_type!r}.",
                path,
            )
        if "description" not in item:
            _warning(
                diagnostics,
                "missing-schema-description",
                f"Schema item '{key}' should include a description.",
                path,
            )
        if "default" not in item:
            _warning(
                diagnostics,
                "missing-schema-default",
                f"Schema item '{key}' should include a default.",
                path,
            )
        if schema_type == "object" and "items" not in item:
            _error(
                diagnostics,
                "missing-object-schema-items",
                f"Object schema item '{key}' requires items.",
                path,
            )


def _check_requirements(root: Path, diagnostics: list[Diagnostic]) -> None:
    path = root / "requirements.txt"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _error(diagnostics, "unreadable-requirements", str(error), path)
        return
    for line_number, raw_line in enumerate(lines, start=1):
        requirement = raw_line.partition("#")[0].strip()
        if not requirement:
            continue
        package = re.split(r"[<>=!~\[;]", requirement, maxsplit=1)[0].strip().lower()
        if package in {"astrbot", "astrbot-api"}:
            _error(
                diagnostics,
                "astrbot-runtime-dependency",
                "Plugins must not list AstrBot itself as a requirement.",
                path,
                line_number,
            )
        if package in CORE_OVERLAP_PACKAGES and "==" in requirement:
            _error(
                diagnostics,
                "pinned-core-dependency",
                f"Do not pin AstrBot-overlapping dependency '{package}'.",
                path,
                line_number,
            )


def _check_web_api_version(
    root: Path,
    metadata: dict[str, str],
    uses_web_api: bool,
    diagnostics: list[Diagnostic],
) -> None:
    if not uses_web_api:
        return
    version_range = metadata.get("astrbot_version", "")
    lower_bound = re.search(r">=\s*(\d+)(?:\.(\d+))?", version_range)
    if lower_bound is None:
        return
    current = (int(lower_bound.group(1)), int(lower_bound.group(2) or 0))
    if current < (4, 26):
        _warning(
            diagnostics,
            "web-api-version-bound",
            "astrbot.api.web requires astrbot_version >=4.26.",
            root / "metadata.yaml",
        )


def _error(
    diagnostics: list[Diagnostic],
    code: str,
    message: str,
    path: Path | None = None,
    line: int | None = None,
) -> None:
    diagnostics.append(Diagnostic("error", code, message, path, line))


def _warning(
    diagnostics: list[Diagnostic],
    code: str,
    message: str,
    path: Path | None = None,
    line: int | None = None,
) -> None:
    diagnostics.append(Diagnostic("warning", code, message, path, line))
