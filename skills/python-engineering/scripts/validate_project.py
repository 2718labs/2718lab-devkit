#!/usr/bin/env python3
"""交付前自检脚本(仅标准库,需 Python >= 3.11 以使用 tomllib)。

用法:
    python scripts/validate_project.py <仓库目录>

机械检查 SKILL.md 第 5 步列出的规则,不替代人工评审(评审逻辑判断仍需对照
references/guidelines.md)。输出分 ERROR / WARN,末行统计错误数。

本脚本里的 PEP 440 版本号正则是覆盖常见写法的简化实现,不是 PEP 440 官方
Appendix B 的逐字复制;边界情况(纪元号 "1!1.0"、复杂本地版本段等)请以
https://peps.python.org/pep-0440/ 为准人工复核。预发布段同时接受短写法
(a1/b1/rc1)和长写法(alpha1/beta1/preview1/pre1/c1),以及 -/_/. 分隔符,
与 references/pyproject-reference.md §4 的合法写法表保持一致。
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# --- 常量:白名单 / 禁词 -----------------------------------------------------

ALLOWED_BUILD_BACKENDS = {"uv_build", "hatchling.build"}

# 简化版 PEP 440 校验正则:release 段 + 可选预发布/后发布/开发发布段。
# 预发布同时接受短写法(a1/b1/rc1)与长写法(alpha1/beta1/pre1/preview1/c1),
# 允许 -/_/. 分隔符(如 "1.0.0-rc.1"、"1.0.0.beta2"),不接受裸限定词不带
# 数字的写法(如 "1.0.0-alpha"),那种写法 PEP 440 之外的团队约定仍视为不合法。
PEP440_RE = re.compile(
    r"^([0-9]+!)?[0-9]+(\.[0-9]+)*"
    r"([-_.]?(alpha|beta|preview|pre|rc|c|a|b)[-_.]?[0-9]+)?"
    r"([-_.]?post[-_.]?[0-9]+)?"
    r"([-_.]?dev[-_.]?[0-9]+)?"
    r"(\+[a-zA-Z0-9]+(\.[a-zA-Z0-9]+)*)?$"
)

BANNED_WORDS_IN_DOCS_OR_CI = [
    "pip install",
    "poetry install",
    "poetry add",
    "pipenv install",
    "python -m venv",
]

# mypy 专属键(不是 pyright 的键),混入 [tool.pyright] 时静默无效
MYPY_ONLY_KEYS = [
    "disallow_untyped_defs",
    "disallow_incomplete_defs",
    "ignore_missing_imports",
    "check_untyped_defs",
    "warn_return_any",
    "no_implicit_optional",
    "files",  # mypy 常用,pyright 对应键是 include
]

errors: list[str] = []
warnings: list[str] = []


def error(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def check_pyproject(repo: Path) -> dict | None:
    pyproject_path = repo / "pyproject.toml"
    if not pyproject_path.is_file():
        error("未找到 pyproject.toml")
        return None

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        error(f"pyproject.toml 解析失败: {exc}")
        return None

    project = data.get("project")
    if not isinstance(project, dict):
        error("pyproject.toml 缺少 [project] 表")
        return data

    # PEP 621 必填字段
    if "name" not in project:
        error("[project] 缺少必填字段 name")
    if "version" not in project and "version" not in project.get("dynamic", []):
        error("[project] 缺少 version(且未通过 dynamic 声明)")

    version = project.get("version")
    if isinstance(version, str):
        if not PEP440_RE.match(version):
            error(f"version={version!r} 不是合法 PEP 440 版本号(合法写法参考 references/pyproject-reference.md §4,如 1.0.0a1 / 1.0.0alpha1 / 1.0.0.post1)")
        if version.startswith("v"):
            error(f"version={version!r} 带 v 前缀,PEP 440 不允许(与 AstrBot metadata.yaml 的约定相反,不要混用)")

    requires_python = project.get("requires-python")
    if not requires_python:
        warn("[project] 未设置 requires-python,建议至少 \">=3.10\"")
    else:
        m = re.search(r">=\s*3\.(\d+)", requires_python)
        if m:
            minor = int(m.group(1))
            if minor < 10:
                error(f"requires-python 下界为 3.{minor},低于团队最低要求 3.10(3.9 已 EOL)")
        else:
            warn(f"requires-python={requires_python!r} 未识别出 >=3.x 下界写法,人工确认")

    # build-system 白名单
    build_system = data.get("build-system")
    if not isinstance(build_system, dict):
        error("缺少 [build-system]")
    else:
        backend = build_system.get("build-backend")
        if backend not in ALLOWED_BUILD_BACKENDS:
            error(
                f"build-backend={backend!r} 不在白名单 {sorted(ALLOWED_BUILD_BACKENDS)} 内,"
                "唯二选项是 uv_build(纯 Python)或 hatchling.build(有构建脚本/C 扩展)"
            )

    # pyright 配置里混入 mypy 键
    pyright_cfg = data.get("tool", {}).get("pyright", {})
    if isinstance(pyright_cfg, dict):
        leaked = [k for k in MYPY_ONLY_KEYS if k in pyright_cfg]
        if leaked:
            error(f"[tool.pyright] 混入了 mypy 专属键: {leaked},pyright 不认识这些键(会静默无效)")

    return data


def check_src_layout(repo: Path, project: dict | None) -> None:
    src_dir = repo / "src"
    if not src_dir.is_dir():
        warn("未找到 src/ 目录(flat-layout 项目可忽略;要发布/被 import 的库应使用 src-layout)")
        return

    if project is None:
        return
    name = project.get("name")
    if not isinstance(name, str):
        return
    normalized = name.replace("-", "_").replace(".", "_")
    if not (src_dir / normalized).is_dir():
        error(f"src/ 下未找到与 [project.name]={name!r} 对应的包目录 src/{normalized}/")


def check_uv_lock(repo: Path) -> None:
    lock_path = repo / "uv.lock"
    if not lock_path.is_file():
        error("未找到 uv.lock(依赖变更须走 uv add/uv remove 并生成锁文件)")
        return

    gitignore_path = repo / ".gitignore"
    if gitignore_path.is_file():
        gitignore_text = gitignore_path.read_text(encoding="utf-8", errors="ignore")
        for line in gitignore_text.splitlines():
            stripped = line.strip()
            if stripped in {"uv.lock", "/uv.lock", "*.lock"}:
                error(f".gitignore 里的规则 {stripped!r} 会把 uv.lock 排除在版本控制外,必须提交它")

    if gitignore_path.is_file():
        gitignore_text = gitignore_path.read_text(encoding="utf-8", errors="ignore")
        if ".venv" not in gitignore_text and ".venv/" not in gitignore_text:
            warn(".gitignore 未包含 .venv/,本地虚拟环境可能被误提交")
    else:
        warn("未找到 .gitignore,无法确认 .venv/ 是否被排除")


def check_banned_words(repo: Path) -> None:
    if (repo / "setup.py").is_file():
        error("发现 setup.py,新项目禁止使用,依赖/构建统一走 uv + pyproject.toml")
    if (repo / "setup.cfg").is_file():
        error("发现 setup.cfg,新项目禁止使用")
    if (repo / "poetry.lock").is_file():
        error("发现 poetry.lock,团队统一用 uv,不使用 poetry")

    workflows_dir = repo / ".github" / "workflows"
    candidate_files = list(repo.glob("README*"))
    if workflows_dir.is_dir():
        candidate_files += list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))

    for f in candidate_files:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for word in BANNED_WORDS_IN_DOCS_OR_CI:
            if word in text:
                error(f"{f.relative_to(repo)} 中出现禁词 {word!r},应改为 uv 等价命令(见第 0 条:见到这些词就是写错了)")


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python validate_project.py <仓库目录>")
        return 2

    repo = Path(sys.argv[1]).resolve()
    if not repo.is_dir():
        print(f"目录不存在: {repo}")
        return 2

    data = check_pyproject(repo)
    project = data.get("project") if isinstance(data, dict) else None
    check_src_layout(repo, project)
    check_uv_lock(repo)
    check_banned_words(repo)

    for w in warnings:
        print(f"[WARN] {w}")
    for e in errors:
        print(f"[ERROR] {e}")

    print(f"\n{len(errors)} 个错误,{len(warnings)} 个警告")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
