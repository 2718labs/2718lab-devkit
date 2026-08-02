---
description: 用 python-engineering 规范初始化一个 Python 项目(uv + ruff + pyproject)
argument-hint: [项目名] [lib|app]
---

# /2718lab-new-python

按 `python-engineering` skill 初始化 Python 工程骨架。

目标:$ARGUMENTS

步骤:

1. 读 `python-engineering` skill。工具链凭据以 skill 的 `references/` 为准,不凭记忆写命令。
2. 复制模板:库(lib)用 `skills/python-engineering/assets/templates/pyproject.toml`,应用(app)用 `pyproject-app.toml`;再附 `.pre-commit-config.yaml`、`.python-version`。
3. 目录布局:库走 src-layout(见 `assets/templates/src_layout.txt`),脚本/应用可 flat。`requires-python` 与 ruff/pyright/pytest 配置按 skill 基线填。
4. 依赖用 `uv add`,不要在 uv 项目里混用 `pip install`(会脱离 lockfile)。版本号遵循 PEP 440。
5. 交付前运行 `python skills/python-engineering/scripts/validate_project.py <项目目录>`。

要发 PyPI/GitHub → `oss-repo-ops` skill。
