# pyproject.toml 全量注解 + 配置键权威表

本文件是 SKILL.md 第 3 步底稿的展开版。写任何 SKILL.md 未覆盖的键之前,先在本文件对应章节确认存在再写;查不到就不要用。

## 1. `[project]`(PEP 621 元数据)

```toml
[project]
name = "my-tool"                     # 必填。发布名可用连字符/点/下划线;import 名会把连字符、点规范化为下划线
version = "0.1.0"                    # 必填(除非用 dynamic = ["version"]),必须合法 PEP 440,见第 4 节
description = "一句话描述"            # 建议填,PyPI 页面摘要用它
readme = "README.md"                 # 建议填,否则 PyPI 页面无正文
requires-python = ">=3.10"           # 必填(强烈建议),下界写法,见第 5 节 EOL 表
license = "MIT"                      # 建议填(SPDX 表达式字符串);发布/许可选型见 oss-repo-ops
authors = [{ name = "Team", email = "team@example.com" }]
dependencies = [
    "httpx>=0.27",
]

[project.optional-dependencies]      # 可选功能分组,用户用 `pip install pkg[extra]` 装
docs = ["mkdocs>=1.6"]

[project.scripts]                    # CLI 入口点,`uv run mytool` 或安装后直接 `mytool` 可用
mytool = "my_tool.cli:main"
```

**陷阱**:
- `name` 含连字符时,顶层 `import` 语句必须用下划线版本(`my-tool` → `import my_tool`)。这是 PyPA 打包规范的规范化规则,不是 uv 特有行为。
- `version` **不带 `v` 前缀**。注意这与 AstrBot `metadata.yaml` 里 `version: v1.0.0` 必须带 `v` 前缀的约定正好相反——两套生态的约定不要混用,写错一个都会导致对应工具链解析异常或过审失败。
- `dependencies` 里的开发/测试专用工具(ruff、pyright、pytest 等)**不要**放在这里,放 `[dependency-groups]`(见第 2 节),否则最终用户 `pip install` 你的包时会被迫连带装上这些开发工具。

来源:[PEP 621](https://peps.python.org/pep-0621/)、[packaging.python.org: pyproject.toml 规范](https://packaging.python.org/en/latest/specifications/pyproject-toml/)

## 2. `[dependency-groups]`(PEP 735,开发依赖分组)

```toml
[dependency-groups]
dev = [
    "ruff>=0.8",
    "pyright>=1.1",
    "pytest>=8.0",
]
lint = ["ruff>=0.8"]                 # 也可以按用途拆更细的组,dev 组可以 include 它们
```

`uv sync` 默认会装 `dev` 组;`uv sync --no-dev` 可跳过。这是当前 uv 推荐的开发依赖写法,取代早期一些项目里直接把开发工具塞进 `dependencies` 或用 `[tool.uv.dev-dependencies]` 的旧写法。

来源:[Astral uv: Managing dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/)

## 3. 打包模式与 `[build-system]`

**选项 0:只运行、不打包的应用 → 明确关闭 package 模式**

```toml
[tool.uv]
package = false
```

只有不构建 wheel、不发布到 PyPI、不会作为包安装的 flat-layout 应用可以使用这个模式并省略 `[build-system]`。`uv run`、`uv sync` 和锁文件仍正常工作;一旦需要构建、安装或发布,删除这项并从下面两个构建后端中选择一个。

**打包选项 A:纯 Python 项目 → uv_build**(uv v0.6 起为 `uv init --package` 的默认后端)

```toml
[build-system]
requires = ["uv_build>=0.8.3,<0.9.0"]   # 版本号随你本地 uv 版本走,以 `uv init` 实际生成的为准,不要凭记忆瞎填
build-backend = "uv_build"
```

**打包选项 B:有构建脚本或 C 扩展 → hatchling**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**注意**:后端字符串是 `"uv_build"`(下划线,不是 `"uv.build"` 或 `"uv-build"`),`requires` 里的包名同样是 `uv_build`(不是裸 `uv`)。两个打包后端不要在同一个项目里混用或抄错字符串——写错会导致 `uv build` / `pip install .` 直接失败。新建项目时优先让 `uv init --package` 自动生成这一节并照抄其产出的版本号,而不是手写。

来源:[Astral uv: Configuring projects](https://docs.astral.sh/uv/concepts/projects/config/)、[Astral uv: The uv build backend](https://docs.astral.sh/uv/concepts/build-backend/)、[pydevtools: Why does uv use Hatch as a build backend?](https://pydevtools.com/handbook/explanation/why-does-uv-use-hatch-as-a-backend/)

## 4. PEP 440 版本号语法

合法形式(release 段任意长度 `N(.N)*`,后面可选一个预发布/后发布/开发发布段):

| 写法 | 含义 | 合法? |
|---|---|---|
| `1.0.0` | 正式版 | 合法 |
| `1.0.0a1` / `1.0.0alpha1` | alpha 预发布(推荐无空格短写法 `a1`) | 合法 |
| `1.0.0b1` | beta 预发布 | 合法 |
| `1.0.0rc1` | release candidate | 合法 |
| `1.0.0.post1` | 后发布(元数据修正,不改代码) | 合法 |
| `1.0.0.dev1` | 开发版 | 合法 |
| `1.0.0+local.1` | 本地版本标识(不能出现在发布到 PyPI 的包里) | 语法合法,但公共索引会拒绝 |
| `1.0.0-alpha` | 裸连字符 SemVer 预发布写法 | **不合法**,会被拒绝或触发非预期规范化 |
| `v1.0.0` | 带 `v` 前缀 | **不合法**(`[project.version]` 字段本身不允许前缀;工具在别处展示时才可能加 `v`) |

版本号写入 pyproject 前,凭上表快速核对;不确定的复杂写法(本地版本段、纪元 `1!1.0.0` 等)去官方规范查,不要自己拼。

来源:[PEP 440: Version Identification and Dependency Specification](https://peps.python.org/pep-0440/)(权威语法定义,完整正则见该文 Appendix B;本文件只给结论表,不复述其正则)

## 5. Python 版本与 `requires-python`

| Python 版本 | EOL(标准支持结束) | 新项目是否建议起步 |
|---|---|---|
| 3.9 | 2025-10(已过期) | 否,已 EOL |
| 3.10 | 2026-10 | 可以,但已接近末期,是当前 skill 的建议下界 |
| 3.11+ | 更晚 | 建议,新项目优先 `>=3.11` |

`requires-python` 写下界即可,不建议加上界(除非有已知的破坏性变更需要避开)。

来源:[endoflife.date: Python](https://endoflife.date/python)、[HeroDevs: Python 3.10 End of Life](https://www.herodevs.com/blog-posts/python-3-10-end-of-life-october-2026-security-and-migration-guide/)

## 6. `[tool.ruff]` / `[tool.ruff.lint]` 键表

```toml
[tool.ruff]
line-length = 88                     # 默认就是 88(与 black 一致),显式写出便于团队看到约定
target-version = "py310"             # 影响哪些语法升级建议(UP 规则族)生效,对齐 requires-python 下界
extend-exclude = ["migrations"]      # 额外排除目录,叠加在内置默认排除列表之上

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F"]     # 零配置默认值就是这四个,必须显式列出才能加别的规则族
ignore = []                          # 全局忽略的规则码
unfixable = []                       # 禁止 --fix 自动修的规则码(有些自动修改风险高时用)

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["D"]                    # 测试文件不强制 docstring 规则

[tool.ruff.format]
quote-style = "double"               # formatter 独立配置节,默认已是 double,一般不用改
```

**默认规则族陷阱**:零配置(`ruff check .` 不带任何 `[tool.ruff.lint]`)只启用 `E4`(缩进相关)、`E7`(语句相关,如用 `==` 判断 `None`)、`E9`(语法/运行时错误)、`F`(Pyflakes,未用导入/未定义名等)。以下规则族**不是默认开的**,团队常用但必须显式 `select` 才生效:

| 规则族前缀 | 含义 | 默认是否开启 |
|---|---|---|
| `E4`/`E7`/`E9` | pycodestyle 缩进/语句/运行时错误子集 | 默认开 |
| `F` | Pyflakes(未用导入、未定义名等) | 默认开 |
| `W` | pycodestyle 警告(如行尾空格) | 需显式 select |
| `I` | isort(import 排序) | 需显式 select |
| `N` | pep8-naming | 需显式 select |
| `D` | pydocstyle(docstring 规范) | 需显式 select |
| `UP` | pyupgrade(语法升级建议) | 需显式 select |
| `SIM` | flake8-simplify | 需显式 select |
| `FURB` | refurb(现代化写法建议) | 需显式 select |

规则码含义不确定时,只用上表里出现过的前缀;要开新的规则族,先确认它在 ruff 规则总览里真实存在,不要凭印象编码。

来源:[Astral ruff: Configuration](https://docs.astral.sh/ruff/configuration/)、[Astral ruff: The Formatter](https://docs.astral.sh/ruff/formatter/)

## 7. `[tool.pyright]` 键表 与 mypy → pyright 迁移

```toml
[tool.pyright]
include = ["src"]                    # 只检查这些目录
exclude = ["**/node_modules", "**/__pycache__", "build"]
typeCheckingMode = "standard"        # off / basic / standard / strict 四档
pythonVersion = "3.10"               # 对齐 requires-python 下界
reportMissingImports = true          # reportXxx 系列键控制单条诊断的开关/级别(true/false/"error"/"warning"/"none")
```

等价的独立文件写法是仓库根目录放 `pyrightconfig.json`(同样的键,JSON 语法),二选一,不要两个同时存在导致互相覆盖读取顺序混乱。

**mypy → pyright 键名不通用**,常见误用对照:

| mypy(`mypy.ini` / `[tool.mypy]`) | pyright 对应写法 |
|---|---|
| `disallow_untyped_defs` | 通过 `typeCheckingMode = "standard"`/`"strict"` 综合控制,无同名单键 |
| `ignore_missing_imports` | `reportMissingImports = false` |
| `strict = true` | `typeCheckingMode = "strict"` |
| `files = [...]` | `include = [...]` |
| `exclude = "regex"` | `exclude = ["glob", ...]`(glob 而非正则) |

**严禁**把左列键名原样写进 `[tool.pyright]`——pyright 不认识这些键,通常会被静默忽略(配置没生效但不报错),排查成本很高。不确定的键先在本表或 pyright 官方文档确认。

来源:[Microsoft pyright: mypy comparison](https://github.com/microsoft/pyright/blob/main/docs/mypy-comparison.md)、[pydevtools: How do Python type checkers compare?](https://pydevtools.com/handbook/explanation/how-do-mypy-pyright-and-ty-compare/)

## 8. `[tool.pytest.ini_options]` 键表

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]                # 限定测试发现目录,加快收集速度
python_files = ["test_*.py"]         # 测试文件命名模式(默认已是 test_*.py 和 *_test.py)
markers = [
    "slow: 标记运行较慢的测试",       # 自定义 marker 需要在这里声明,否则 pytest 会警告未注册
]
addopts = "-ra"                      # 追加的默认 CLI 参数,-ra 显示所有非通过用例的简报
```

数据驱动测试统一用 `@pytest.mark.parametrize("input,expected", [...])`,不要为同一逻辑手写多个 `test_xxx_case1/2/3` 函数。

来源:[pytest Documentation: Getting Started](https://docs.pytest.org/en/stable/getting-started.html)

## 9. 环境变量:`UV_PROJECT_ENVIRONMENT`

uv 项目的虚拟环境默认建在项目根目录的 `.venv/`。需要覆盖路径时用环境变量 `UV_PROJECT_ENVIRONMENT`(不是任何形近的变体拼写),例如 CI 里想把 venv 建到缓存目录时设置它。日常开发不需要设置,`uv run`/`uv sync` 会自动处理。

来源:[Astral uv: Configuring projects](https://docs.astral.sh/uv/concepts/projects/config/)
