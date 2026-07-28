# 团队守则(条款编号版)

评审时引用条款号,如「4.3.2」。每条给「违规示例 → 正确写法」。

## 1. 依赖管理

**1.1** 一切依赖变更走 `uv add` / `uv remove`,禁止手改 pyproject 后不跑 `uv lock`。
- 违规:直接编辑 pyproject `dependencies` 加一行 `"httpx>=0.27"`,不管 `uv.lock`。
- 正确:`uv add "httpx>=0.27"`,命令自动写 pyproject 并同步 `uv.lock`。

**1.2** 开发工具进 `[dependency-groups]`,不进 `[project.dependencies]`。
- 违规:`dependencies = ["ruff", "pytest", "myapp-core"]`。
- 正确:`dependencies = ["myapp-core"]`;`[dependency-groups] dev = ["ruff", "pytest"]`。

**1.3** 版本约束用下界 `>=`,禁止无理由 `==` 钉死。
- 违规:`"requests==2.31.0"`(无兼容性理由)。
- 正确:`"requests>=2.31"`。确有已知不兼容版本时,可以用 `>=2.31,<3.0` 这类范围,但不要裸 `==`。

**1.4** `requires-python = ">=3.10"` 起步,新项目优先 `>=3.11`。
- 违规:`requires-python = ">=3.8"`(3.8/3.9 均已 EOL)。
- 正确:`requires-python = ">=3.10"` 或更高。

**1.5** 禁止在 uv 项目里出现 `pip install`、`poetry`、`pipenv`、`conda`、手写 `requirements.txt`(新项目)、`setup.py`/`setup.cfg`。
- 违规:README 里写"先 `pip install -r requirements.txt`"。
- 正确:README 里写"先 `uv sync`"。

## 2. 项目布局

**2.1** 要发布 PyPI 或被 `import` 的库/CLI 用 src-layout;纯 `uv run` 脚本/应用可 flat-layout。
- 违规:一个要发到 PyPI 的库把代码直接放仓库根目录,和 `pyproject.toml`/`tests/` 混在一起,容易在未安装状态下也能被误 import 到"看起来能跑但发布后缺文件"。
- 正确:代码放 `src/<package_name>/`。

**2.2** `uv.lock` 进 git,`.venv/` 进 `.gitignore`。
- 违规:`.gitignore` 里没有 `.venv/`,或者把 `uv.lock` 也排除了。
- 正确:`.gitignore` 含 `.venv/`;`uv.lock` 正常提交。

**2.3** `.python-version` 钉住解释器版本,与 `requires-python` 下界不矛盾。
- 违规:`.python-version` 写 `3.9`,但 `requires-python = ">=3.10"`。
- 正确:两者一致或 `.python-version` ≥ 下界,如 `.python-version` = `3.12`。

## 3. Lint / Format

**3.1** 只用 ruff,禁止同时装 black/flake8/isort/autopep8。
- 违规:pyproject 里既有 `[tool.ruff]` 又有 `[tool.black]`。
- 正确:只保留 `[tool.ruff]` / `[tool.ruff.lint]` / `[tool.ruff.format]`。

**3.2** 需要的规则族必须显式 `select`,不要以为默认全开。
- 违规:pyproject 没写 `[tool.ruff.lint] select`,却指望 `import` 排序(`I`)规则生效。
- 正确:`select = ["E4", "E7", "E9", "F", "I"]` 显式列出 `I`。

**3.3** 规则码/配置键只用 `references/pyproject-reference.md` 确认存在的写法。
- 违规:凭印象写 `select = ["ALL"]` 后又不清楚会启用什么,或编造不存在的规则码。
- 正确:先查权威表,按需选择明确的规则族前缀。

## 4. 类型检查

**4.1** 默认 pyright,至少 `standard` 模式。
- 违规:`typeCheckingMode` 缺省或写 `"off"`。
- 正确:`typeCheckingMode = "standard"`(或团队要求更严格时 `"strict"`)。

**4.2** 禁止把 mypy 专属键写进 `[tool.pyright]`。
- 违规:`[tool.pyright]` 里出现 `disallow_untyped_defs = true`(pyright 不认识,静默无效)。
- 正确:用 `typeCheckingMode` 综合档位,或查权威表里的 `reportXxx` 对应键。

**4.3** 公共 API(对外函数/方法签名)全部类型注解。
- 违规:导出的函数没有参数/返回值注解。
- 正确:`def parse(data: bytes) -> dict[str, Any]: ...`

## 5. 测试

**5.1** 测试放 `tests/`,命名 `test_*.py`。
- 违规:测试散落在源码目录里叫 `check.py`。
- 正确:`tests/test_parser.py`。

**5.2** 数据驱动测试用 `@pytest.mark.parametrize`。
- 违规:同一逻辑复制 5 个 `test_case1`…`test_case5` 函数。
- 正确:一个函数 + `@pytest.mark.parametrize("input,expected", [...])`。

**5.3** 新代码禁止 `unittest.TestCase` 风格。
- 违规:新写的测试类继承 `unittest.TestCase`。
- 正确:普通函数 + `assert`,pytest 原生写法。

## 6. 版本号

**6.1** 版本号单一来源 = pyproject `[project.version]`。
- 违规:`__init__.py` 里手写 `__version__ = "1.0.0"` 且和 pyproject 版本号不同步。
- 正确:只在 pyproject 维护;需要运行时读取时用 `importlib.metadata.version(...)` 从已安装的包元数据读取,而不是再手写一份常量。

**6.2** 版本号必须合法 PEP 440。
- 违规:`version = "1.0.0-alpha"`。
- 正确:`version = "1.0.0a1"`。

## 7. CI

**7.1** CI 用 `uv sync --frozen` 而不是 `uv sync`(避免 CI 意外重新解析出与本地不同的依赖树)。
- 违规:CI 脚本用裸 `uv sync`。
- 正确:`uv sync --frozen`,配合本地开发者定期手动 `uv lock` 更新。

**7.2** CI 只到"测试全绿"为止,发布相关 job 转 `oss-repo-ops`。
- 违规:同一个 workflow 文件里 CI job 和 PyPI 发布 job 混在一起,且发布触发条件写得含糊。
- 正确:测试 workflow 独立;发布相关内容按 `oss-repo-ops` 的规范单独处理。

## 8. 评审清单附录

评审一个 Python 仓库时按此顺序过一遍,逐条标注条款号:

1. `requires-python` 是否 ≥3.10(1.4)
2. `dependencies` 是否混入开发工具(1.2)
3. `uv.lock` 是否存在且与 pyproject 一致(1.1,可用 `uv lock --check`)
4. `.venv/` 是否被 `.gitignore` 排除,`uv.lock` 是否没被误排除(2.2)
5. 是否存在 `pip install`/`poetry`/`setup.py` 等禁词(1.5)
6. `[tool.ruff.lint] select` 是否显式列出团队需要的规则族(3.2)
7. `[tool.pyright]` 是否混入 mypy 键(4.2)
8. 测试是否在 `tests/`、是否用 `parametrize`(5.1、5.2)
9. `version` 是否合法 PEP 440(6.2)
10. CI 是否用 `--frozen` 且不含发布步骤(7.1、7.2)
