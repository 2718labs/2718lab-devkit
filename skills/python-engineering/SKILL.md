---
name: python-engineering
description: 团队 Python 工程基线与工具链规范。凡是要新建/改造/评审任何 Python 仓库的工程骨架都必须使用本 skill——初始化项目、写或改 pyproject.toml、配依赖管理、lint/format、类型检查、测试、pre-commit、版本号、目录布局，或提到 uv、uv.lock、ruff、pyright、mypy、pytest、PEP 621、PEP 440、src-layout、requires-python 时。即使只是"帮我加个依赖"或"配一下 ruff"也要先查本 skill,现代 Python 工具链坑多且迭代快(如 uv 项目里混用 pip install 会脱离 lockfile、1.0.0-alpha 不是合法 PEP 440 版本号、pyright 配置键与 mypy 完全不同、uv_build 后端名称易写错)。本 skill 只管工程工具链,发布 PyPI/GitHub 开源运营见 oss-repo-ops,具体框架 API 见对应框架 skill。Use this skill whenever the user works on Python project setup, packaging, linting, typing, testing, or tooling configuration in any Python repository.
---

# Python 工程基线(团队规范版)

为工作室任何 Python 仓库做工程骨架时,严格按本文件执行。**不要凭记忆写工具链配置**——uv/ruff/pyright 迭代极快,配置键、命令、默认规则集记忆极易出错(例如 ruff 默认只开 `E4/E7/E9/F`,不是想当然的一整套规则族);本文件和参考文件里的写法才是准确的。

## 共享执行层

本 skill 负责 Python 工程领域事实，不单独拥有编排。多步骤或多 Agent 任务使用
`work-methodology`、`2718lab-tools` 以及通用
`2718lab-triage` / `2718lab-investigator` / `2718lab-doc-writer` /
`2718lab-code-writer` / `2718lab-verifier` / `2718lab-risk-reviewer`。
角色权限听共享执行层，Python 工具链与验收规则听本 skill。

配套文件:

- `references/pyproject-reference.md` — 全量注解版 pyproject.toml + `[tool.ruff]`/`[tool.pyright]`/`[tool.pytest.ini_options]`/`[build-system]` 各配置键权威表、PEP 440 语法、Python EOL 表。**写任何本文件未覆盖的配置键前,必须先读它对应章节**。
- `references/toolchain-commands.md` — uv/ruff/pyright/pytest/pre-commit 命令速查与 CI 配方。
- `references/guidelines.md` — 完整团队守则(条款编号),评审时对照。
- `assets/templates/` — 可直接复制的起步文件(pyproject 两种、pre-commit 配置、.python-version、目录树示意)。
- `scripts/validate_project.py` — 交付前自检脚本,**必须运行**(见第 5 步)。

多仓库批量改造/评审的开团与自检节奏按 `work-methodology` 执行,本文件只管 Python 工程领域知识。

## 第 0 条:工具链保真(最容易被弱模型违反)

1. **依赖/环境只用 uv**:`uv init` / `uv add` / `uv remove` / `uv sync` / `uv lock` / `uv run` / `uv build` / `uv python`。**严禁**在 uv 项目里出现 `pip install`、`poetry`、`pipenv`、`conda`、`python -m venv`、手写 `requirements.txt`(新项目)、`setup.py`/`setup.cfg`。见到这些词就是写错了。旧命令 → uv 等价命令对照:

   | 旧写法 | uv 等价 |
   |---|---|
   | `pip install foo` | `uv add foo` |
   | `pip install -r requirements.txt` | `uv sync` |
   | `pip freeze > requirements.txt` | `uv lock`(生成 `uv.lock`,不是 requirements.txt) |
   | `python -m venv .venv && source .venv/bin/activate` | 不需要,直接 `uv run <cmd>` |
   | `poetry add/install/build` | `uv add` / `uv sync` / `uv build` |
   | `python setup.py sdist bdist_wheel` | `uv build` |

2. **lint/format 只用 ruff**:`ruff check .` + `ruff format .`。**严禁**引入 black/flake8/isort/autopep8——ruff 单工具全替代,混装必然打架(规则重复报错或格式化结果互相打架)。**注意**:ruff 零配置下默认只启用 `E4`/`E7`/`E9`/`F` 四类,`W`/`I`/`N`/`D`/`UP`/`SIM`/`FURB` 等规则族必须在 `[tool.ruff.lint] select` 里显式加入,不是开箱自带,不要凭印象以为全开了。
3. **类型检查默认 pyright**。**严禁把 mypy 的 ini 配置键写进 pyright 配置**(两者键名完全不同,如 mypy 的 `disallow_untyped_defs` 在 pyright 里不存在,对应写法是 `typeCheckingMode`)。pyright 配置只用 `[tool.pyright]` 或 `pyrightconfig.json`,键名不确定就查 `references/pyproject-reference.md` 对应章节,查不到不许写。
4. **版本号必须是合法 PEP 440**:`1.0.0a1` / `1.0.0rc1` / `1.0.0.post1` / `1.0.0.dev1`。**`1.0.0-alpha` 这类带连字符的裸 SemVer 预发布格式不合法**,pip/uv 会拒绝或做非预期的规范化。
5. ruff 规则码(E/F/W/I/N/D/UP/SIM/FURB…)、配置键、build backend 名称一律照抄 `references/` 或模板里确认存在的写法,不许凭记忆编造或自创变体。

## 工作流程(按顺序执行)

### 第 1 步:判断任务类型

| 任务 | 做法 |
|---|---|
| 新建项目 | 复制 `assets/templates/` 对应模板 → `uv init` 校准 → 改占位符 |
| 加/改依赖 | `uv add` / `uv remove`,**禁止**手改 `uv.lock` 或只改 pyproject 不 lock |
| 配工具(ruff/pyright/pytest) | 读 `references/pyproject-reference.md` 对应节,照抄配置键 |
| 修/搭 CI | `references/toolchain-commands.md` 里的 CI 配方 |
| 代码/仓库评审 | 对照下方「硬性规则」+ `guidelines.md` 逐条检查,输出带条款号问题清单 |
| 发布 PyPI / 打 tag / 写 release CI | **转 `oss-repo-ops` skill,本 skill 不管** |
| AstrBot 插件仓库 | 布局与依赖规则以 `astrbot-plugin-dev` 为准(插件无 pyproject、用 requirements.txt、运行在宿主进程,与本 skill 冲突时它赢) |
| MCP server 项目 | 框架 API 与协议细节查 `mcp-server-dev`,本 skill 只管其 pyproject/测试/lint 外围 |

### 第 2 步:项目结构

- 判断标准:**要发布到 PyPI 或会被 `import`** 的库/CLI → 必须 src-layout(`src/<package_name>/`)。只跑 `uv run` 的脚本/应用 → 可以 flat-layout(代码直接放仓库根或简单子目录)。
- 明确不打包、不发布、只通过 `uv run` 启动的应用,在 pyproject 中写 `[tool.uv] package = false`,可以不声明 `[build-system]`;一旦项目需要构建 wheel、发布或作为包安装,仍必须使用下方受支持的构建后端。
- src-layout 树示意见 `assets/templates/src_layout.txt`。
- `uv.lock` **必须**进 git(跨平台锁文件,团队协作与 CI 复现依赖靠它);`.venv/` **必须**进 `.gitignore`(本地生成物,不进版本控制)。
- `.python-version` 钉住解释器版本(如 `3.12`),`uv` 与 `pyright` 都会读它,避免"我电脑上是对的"。

### 第 3 步:pyproject.toml 标准底稿

下面是需要打包的库/CLI 底稿,占位符 `<package_name>` 替换成真实包名。只运行、不打包的 flat-layout 应用改用 `assets/templates/pyproject-app.toml`,由 `[tool.uv] package = false` 明确关闭打包。

```toml
[project]
name = "<package-name>"              # PyPI/仓库名可用连字符,import 名会自动变下划线(见下方陷阱)
version = "0.1.0"                    # 唯一版本来源,必须合法 PEP 440,禁止 "v" 前缀、禁止裸连字符预发布
description = "一句话描述这个项目。"
requires-python = ">=3.10"           # 3.9 已 EOL(2025-10);新项目建议直接 >=3.11
dependencies = [
    # "httpx>=0.27",                 # 用下界 >=,禁止无理由 == 钉死
]

[dependency-groups]                  # 开发工具进这里,不进 dependencies(用户装包不需要它们)
dev = [
    "ruff>=0.8",
    "pyright>=1.1",
    "pytest>=8.0",
]

# 需要打包时:纯 Python 库/CLI 用 uv_build;有构建脚本或 C 扩展用 hatchling —— 唯二选项,后端字符串逐字照抄,不要自创
[build-system]
requires = ["uv_build>=0.8.3,<0.9.0"] # 版本上界随 uv 版本走,新建项目用 `uv init` 生成后照抄实际值
build-backend = "uv_build"

[tool.ruff]
line-length = 88
target-version = "py310"

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP"]   # 默认只有 E4/E7/E9/F,其余规则族按需显式加入

[tool.pyright]
typeCheckingMode = "standard"
include = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

hatchling 版本的 `[build-system]`(有构建脚本/C 扩展时用这个,不要两者混用):

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**陷阱提醒**:`name` 里的连字符只影响发布名,`import` 时用下划线(如 `name = "my-tool"` → `import my_tool`)。这与 AstrBot `metadata.yaml` 里 `version` 要求带 `v` 前缀的约定**相反**——PEP 440 版本号**禁止** `v` 前缀,两套约定不要串。

### 第 4 步:硬性规则

**依赖**
1. 一切依赖变更走 `uv add` / `uv remove`;手改 pyproject 的 `dependencies` 后必须跟一次 `uv lock` 同步锁文件,禁止让 `uv.lock` 与 pyproject 脱节。
2. 开发工具(ruff/pyright/pytest 等)进 `[dependency-groups]`,不进 `[project.dependencies]`。
3. 版本约束用下界 `>=`,禁止无理由 `==` 钉死(会阻止安全更新与依赖解析)。
4. `requires-python = ">=3.10"` 起步(3.9 已于 2025-10 EOL,3.10 将于 2026-10 EOL);新项目直接考虑 `>=3.11`。

**质量**
5. `ruff check .` 和 `ruff format .` 双跑,配置全进 pyproject `[tool.ruff]` / `[tool.ruff.lint]`,不要再引入 `.flake8`/`setup.cfg` 之类的旧配置文件。
6. pyright 至少 `standard` 模式,对外公共 API(函数签名、返回值)全部加类型注解。
7. 测试放 `tests/`,文件名 `test_*.py`,数据驱动场景用 `@pytest.mark.parametrize`,新代码禁止用 `unittest.TestCase` 风格。

**工程**
8. `pre-commit` 装 ruff / ruff-format / pyright 三个 hook,`pre-commit install` 写进仓库上手文档(README 或类似位置)。
9. 版本号单一来源 = pyproject `[project.version]`,必须合法 PEP 440,不需要在别处(如 `__version__.py`)重复手写并保持同步。
10. 不手动 `activate` 虚拟环境,一律 `uv run <cmd>` 或 `uv run pytest` 这种形式执行。
11. 只有明确的 no-package 应用可以省略 `[build-system]`,且必须声明 `[tool.uv] package = false`;库、CLI 包和任何会构建/安装/发布的项目仍必须声明受支持的构建后端。

### 第 5 步:交付前自检(必须执行,不可跳过)

先跑本 skill 自带的自检脚本:

```bash
python scripts/validate_project.py <仓库目录>
```

再跑四连:

```bash
uv lock --check       # 校验 pyproject 与 uv.lock 是否一致(也可用 uv sync --frozen)
uv run ruff check .
uv run pyright
uv run pytest
```

**0 错误才能交付**。`validate_project.py` 机械检查:pyproject 可解析且 PEP 621 必填字段齐全、`version` 合法 PEP 440、`requires-python` 下界 ≥3.10、打包项目的 `build-backend` 属于白名单二选一(`uv_build` / `hatchling.build`)、无构建后端的应用明确声明 `[tool.uv] package = false`、src-layout 目录名与 `[project.name]` 一致、`uv.lock` 存在且未被 `.gitignore` 误排除、禁词扫描(文档/CI 里出现 `setup.py`、`poetry`、`pip install`)、mypy 专属键(如 `disallow_untyped_defs`)混入 `[tool.pyright]`。

若环境无法跑脚本,逐条人工核对上面第 2-4 步,并额外用 `python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"` 验证 pyproject 语法合法。

### 第 6 步:交付物说明

交付时向用户说明:克隆仓库后 `uv sync` 装依赖 + `pre-commit install` 装 git hook 即可开始干活;要发布到 PyPI / 打 tag / 建 release CI → 转 `oss-repo-ops` skill,本 skill 到"测试全绿"为止,不管发布运营。

## 代码评审输出格式

```
【强制-违规】4.1 pyproject 手改了 dependencies 但未运行 uv lock,uv.lock 已过期。运行 `uv lock` 同步。
【要求-建议改】4.3 依赖用了 `==1.2.3` 精确钉死,无充分理由,建议改 `>=1.2.3`。
【通过】build-system / 类型检查 / 测试布局无问题。
```

评审时同样运行 `scripts/validate_project.py` 辅助,但人工检查不能省(脚本只覆盖机械规则)。
