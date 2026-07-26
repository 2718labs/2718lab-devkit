# 工具链命令速查 + CI 配方

命令与 flag 只用本文件里出现过的;查不到就不要用,先去官方文档确认再补充本文件。

## 1. uv 命令速查

| 命令 | 用途 | 常用 flag |
|---|---|---|
| `uv init` | 初始化新项目,生成 pyproject.toml/`.python-version`/`README.md` | `--build-backend uv`(生成 uv_build 后端配置) |
| `uv add <pkg>` | 添加运行依赖,自动写 pyproject 并更新 `uv.lock` | `--dev`(加进 dev 依赖组,等价旧版 `--group dev`) |
| `uv remove <pkg>` | 移除依赖 | |
| `uv sync` | 按 `uv.lock` 精确安装/同步虚拟环境 | `--frozen`(不更新 lock,严格按现有 lock 安装,CI 常用)、`--no-dev`(跳过 dev 依赖组) |
| `uv lock` | 重新解析依赖并写 `uv.lock` | `--check`(只校验 pyproject 与现有 lock 是否一致,不写文件,适合 CI 检查漂移) |
| `uv run <cmd>` | 在项目虚拟环境里执行命令,无需手动 activate | 例:`uv run pytest`、`uv run ruff check .` |
| `uv build` | 按 `[build-system]` 打包 wheel/sdist | 产物默认输出到 `dist/` |
| `uv python install <ver>` | 安装/管理指定 Python 解释器版本 | |
| `uv venv` | 手动创建虚拟环境(一般不需要,`uv sync`/`uv run` 会自动处理) | |

日常开发闭环:改依赖用 `uv add`/`uv remove` → 跑代码用 `uv run` → 提交前 `uv lock --check` 确认锁文件没漂移。

来源:[Astral uv Documentation: Configuring projects](https://docs.astral.sh/uv/concepts/projects/config/)、[Real Python: Managing Python Projects With uv](https://realpython.com/python-uv/)

## 2. ruff / pyright / pytest CLI 速查

```bash
uv run ruff check .              # 只检查,不改文件
uv run ruff check . --fix        # 检查并自动修复可修复的规则
uv run ruff format .             # 格式化(black 兼容输出)
uv run ruff format --check .     # 只检查格式是否需要改动,不落盘(CI 用)

uv run pyright                   # 按 pyproject [tool.pyright] 或 pyrightconfig.json 跑类型检查
uv run pyright <path>            # 只检查指定路径

uv run pytest                    # 按 [tool.pytest.ini_options] 发现并运行测试
uv run pytest -k "test_name"     # 只跑匹配名字的用例
uv run pytest -m slow            # 只跑标了 slow marker 的用例
```

## 3. pre-commit 生命周期

```bash
uv run pre-commit install        # 每次 clone 仓库后跑一次,把 hook 挂到 .git/hooks
uv run pre-commit run --all-files  # 手动对全仓库跑一遍所有 hook(不等 git commit 触发)
uv run pre-commit autoupdate     # 把 .pre-commit-config.yaml 里各 hook 的 rev 升到最新,升级后要重新跑一次 --all-files 确认
```

`pre-commit` 本身要求 Python ≥3.10 运行环境(与团队 `requires-python` 下界一致,不会构成额外门槛)。

来源:[pre-commit GitHub](https://github.com/pre-commit/pre-commit)

## 4. GitHub Actions CI 配方(只到"测试通过"为止)

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          python-version: ${{ matrix.python-version }}
      - name: Sync dependencies
        run: uv sync --frozen
      - name: Lint
        run: uv run ruff check .
      - name: Format check
        run: uv run ruff format --check .
      - name: Type check
        run: uv run pyright
      - name: Test
        run: uv run pytest
```

这份配方止步于"测试全绿"。**发布 job(build+publish to PyPI、打 tag、GitHub Release)不在本 skill 范围内,见 `oss-repo-ops`**,不要在这份 CI 里顺手加发布步骤。

来源:[Real Python: Managing Python Projects With uv](https://realpython.com/python-uv/)、[Blog: Managing a Python project with uv in 2026](https://blog.bythewood.me/posts/managing-a-python-project-with-uv-in-2026/)

## 5. 环境变量注意

`UV_PROJECT_ENVIRONMENT` 可覆盖项目虚拟环境的存放路径(默认 `.venv`)。日常开发不需要设置;仅在 CI 缓存策略或多项目共享环境等特殊场景才用。**注意变量全名是 `UV_PROJECT_ENVIRONMENT`**,不要抄成任何形近的乱码变体。

来源:[Astral uv Documentation: Configuring projects](https://docs.astral.sh/uv/concepts/projects/config/)
