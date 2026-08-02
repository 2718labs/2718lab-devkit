---
name: python-engineering
description: Set up, modify, review, test, package, or lint a Python repository. Use for pyproject.toml, uv/uv.lock, ruff, pyright, pytest, pre-commit, layout, dependencies, and CI tooling.
---

# Python 工程基线

这是 Python 工具链的短说明书；详细配置按需读取：

- `references/pyproject-reference.md`：配置键、PEP 440 和构建后端。
- `references/toolchain-commands.md`：uv/ruff/pyright/pytest/pre-commit/CI 命令。
- `references/guidelines.md`：工程评审条款；`assets/templates/`：起步文件。
- `scripts/validate_project.py`：项目结构与配置自检。

## 硬约束

1. 依赖和环境只用 `uv add/remove/sync/lock/run/build`；锁文件随依赖变更更新，
   不在 uv 项目里混用 pip/poetry/conda 或手写新 requirements。
2. lint/format 用 ruff；类型检查用 pyright；测试放 `tests/test_*.py`。配置键先查
   reference，不凭记忆把 mypy 配置写进 pyright。
3. 可发布/可 import 的库使用 `src/<package>/`；应用脚本可 flat-layout；`.venv/` 不进 git，
   `uv.lock` 进 git；版本遵循合法 PEP 440，禁止裸连字符预发布或 `v` 前缀。
4. 公共 API 写类型，文件和命令保持可复现；避免无理由精确钉死依赖。

## 交付

运行 `python scripts/validate_project.py <repo>`，再执行 `uv lock --check`、
`uv run ruff check`、`uv run pyright`、`uv run pytest`（按仓库配置取舍）。发布、tag、Release
转到 `oss-repo-ops`；AstrBot 专属布局转到 `astrbot-plugin-dev`；MCP 协议细节转到
`mcp-server-dev`。
