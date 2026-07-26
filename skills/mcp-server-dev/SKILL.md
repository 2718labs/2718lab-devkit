---
name: mcp-server-dev
description: 团队 MCP (Model Context Protocol) 服务器开发规范与 FastMCP API 参考。凡是涉及用 Python 构建 MCP 服务器的任务都必须使用本 skill——新建/编写/修改/评审/调试/打包 MCP server,提到 FastMCP、mcp.server.fastmcp、@mcp.tool、MCP tools/resources/prompts、stdio/Streamable HTTP/SSE 传输、mcp dev/mcp install、claude mcp add、Claude Desktop/Claude Code 接入,或要把已有 Python 功能暴露成 MCP 工具时。即使只写一小段 FastMCP 代码片段也要先查本 skill,因为存在两个同名但 API 相异的 FastMCP 包(官方 SDK 内置 v1.x 的 mcp.server.fastmcp 与 jlowin 独立版 fastmcp 3.x),装饰器括号、transport 字符串、Context 获取方式全都不同,凭记忆写必然混串。Use this skill whenever the user mentions MCP servers, FastMCP, Model Context Protocol tooling, or exposing Python functions as MCP tools/resources/prompts.
---

# MCP Server 开发(团队规范版)

为团队用 Python 构建 MCP server 时,严格按本文件执行。**不要凭记忆写 FastMCP API**——市面上有两个同名叫 "FastMCP" 的包,当前 API 已经分叉(装饰器括号、Context 获取方式、transport 字符串全不一样),凭印象写必然把两边的语法混在一起。本文件和 references 里的写法才算数。

> 边界:给 AstrBot 写插件(哪怕插件内部要调 MCP client)用 `astrbot-plugin-dev`,本 skill 只管独立的 MCP server。

## 共享执行层

本 skill 负责 MCP/FastMCP 领域事实，不单独拥有编排。多步骤或多 Agent 任务使用
`work-methodology`、`2718lab-tools` 以及通用
`2718lab-triage` / `2718lab-investigator` / `2718lab-doc-writer` /
`2718lab-code-writer` / `2718lab-verifier` / `2718lab-risk-reviewer`。
角色权限听共享执行层，MCP API 与验收规则听本 skill。

配套文件:

- `references/api-fastmcp-sdk.md` — (A) 官方 MCP Python SDK 内置 v1.x `mcp.server.fastmcp` 全量 API。
- `references/api-fastmcp-standalone.md` — (B) 独立版 `fastmcp`(jlowin/Prefect,当前 3.4.4)全量 API。
- `references/packaging-and-integration.md` — 打包、`mcp dev`/`mcp install`/`fastmcp run`、接入 Claude Desktop/Code、传输选型、部署。
- `assets/templates/` — 两套可直接复制的起步模板(A 版 + B 版 + pyproject.toml)。
- `scripts/validate_mcp_server.py` — 交付前机械自检脚本,**必须运行**(见第 5 步)。

## 第 0 条:双包保真(最容易被弱模型违反)

1. 写任何 import/装饰器/方法前,先确认目标是 **(A) SDK 内置 v1.x** 还是 **(B) 独立版 3.x**,两边 API **禁止混用**。同一个文件里同时出现 `from mcp.server.fastmcp import ...` 和 `from fastmcp import ...` = 写错了。
2. 白名单制:只允许用两份 reference 文件里出现过的 API。查不到就不用,改用文档确认存在的写法。**grounding/reference 没覆盖 = 视为不存在**。
3. 见到下面这些词,不用看上下文,直接判违规(高频幻觉点名清单):

| 出现的写法 | 问题 |
|---|---|
| (A) 包里裸 `@mcp.tool` / 裸 `@mcp.prompt`(不带括号) | v1.x 文档没有这种写法,是把 (B) 的语法搬过来了 |
| (A) 包里 `get_context()` / `CurrentContext()` | v1.x 没有依赖注入辅助函数,这是 (B) 2.x/3.x 才有的 |
| (A) 包里 `transport="streamable_http"`(下划线) | 文档里是连字符 `"streamable-http"` |
| (A) 包里 `transport="http"` | (A) 包不用这个字符串,那是 (B) 包现行的名字 |
| (B) 包里 `transport="streamable-http"` | (B) 现行文档字符串是裸的 `"http"`,连字符版是 (A) 包的,别抄错包 |
| (B) 3.x 里 `enabled=` 装饰器参数 | 3.0.0 起已弃用,改用 `mcp.enable()` / `mcp.disable()` |
| `from mcp.server import MCPServer` | 这是 SDK `main` 分支的 v2 **alpha**,禁止生产使用,不是稳定的 (A) 包 |
| `mcp.run(transport="websocket")` / `WSTransport` 当服务端 transport | 两包文档都没有这个,v3 升级指南写明已从客户端 transport 移除 |
| `mcp.serve()`、`Server().tool()` 这类臆造签名 | 两包都不存在,是把别的框架/旧记忆拼进来了 |

4. transport 字符串是封闭枚举,只能逐字取自对应包 reference 文件里的白名单,不能"看起来差不多"就用。
5. `@mcp.resource(...)` 永远带 URI 参数括号,两包皆然。
6. 版本敏感:用了 (B) 的 `CurrentContext()`(v2.14+)/`get_context()`(v2.2.11+)要在 pyproject 依赖下界体现出来;(A) 一律钉 `mcp>=1,<2`,防止 v2 alpha 混进来。

## 工作流程(按顺序执行)

### 第 1 步:判断任务类型

| 任务 | 做法 |
|---|---|
| 新建 server | 先做第 2 步选包决策 → 复制 `assets/templates/` 对应文件 → 改占位符 → 实现功能 |
| 加 tool/resource/prompt | 确认现有项目用的是哪个包 → 读对应 api-reference 章节 → 照抄其代码样式改写 |
| 代码评审 | 对照第 0/2/4 条逐条检查,跑 `validate_mcp_server.py` 辅助,输出评审格式(见文末) |
| 打包/发布 | 读 `packaging-and-integration.md`;仓库层面的发布/CI/版本流程转 `oss-repo-ops` |
| 接入 Claude Desktop/Code | 读 `packaging-and-integration.md` 第 3/4 节 |

### 第 2 步:选包决策

| 维度 | (A) SDK 内置 v1.x | (B) 独立版 3.x |
|---|---|---|
| 安装/导入 | `from mcp.server.fastmcp import FastMCP` | `from fastmcp import FastMCP` |
| 版本状态 | v1.x 稳定/维护模式(生产可用) | 当前 3.4.4;注意 3.x 相对 2.x 有 breaking changes |
| 装饰器括号 | 必须带括号 `@mcp.tool()` | 简单情形可裸 `@mcp.tool`,带参数才加括号 |
| Context 获取 | 只有类型注解注入 `ctx: Context` | 三种:`CurrentContext()` 依赖注入(v2.14+,首选)、类型注解(兼容)、`get_context()`(嵌套调用,v2.2.11+) |
| Transport 字符串 | `"streamable-http"`(连字符)、`"sse"`、默认 stdio | `transport="http"`(现行推荐名)、`"sse"`(legacy)、默认 stdio |
| CLI | `uv run mcp dev/install/run server.py` | `fastmcp run server.py:mcp --transport ...` |
| 默认选包策略 | 求稳、生产环境、最少依赖 → 选 (A) | 要 `CurrentContext()`、`tags`/`meta`/`timeout` 等高级装饰器参数、或 (B) 独有特性 → 选 (B) |

不确定选哪个:没有强需求时默认 (A)(v1.x,依赖面最小、是官方 SDK 自带)。

### 第 3 步:标准骨架

**(A) SDK 内置 v1.x:**

```python
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP(name="Tool Example")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting."""
    return f"Hello, {name}!"


@mcp.prompt()
def greet_user(name: str, style: str = "friendly") -> str:
    """Generate a greeting prompt."""
    return f"Please write a {style} greeting for {name}."


@mcp.tool()
async def long_task(x: int, ctx: Context) -> str:
    """A tool that reports progress via Context."""
    await ctx.info(f"starting with x={x}")
    await ctx.report_progress(progress=1, total=2)
    return str(x)


if __name__ == "__main__":
    mcp.run()
```

**(B) 独立版 3.x:**

```python
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context

mcp = FastMCP("My MCP Server")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.resource("data://config")
def get_config() -> dict:
    """Return server config."""
    return {"version": "1.0"}


@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    """Generate an analysis prompt for the given data points."""
    return f"Analyze these {len(data_points)} data points."


@mcp.tool
async def process_file(file_uri: str, ctx: Context = CurrentContext()) -> str:
    """A tool that logs progress via injected Context."""
    await ctx.info(f"Processing {file_uri}")
    return "done"


if __name__ == "__main__":
    mcp.run()
```

新建项目直接复制 `assets/templates/server_sdk.py`(A 版)或 `assets/templates/server_standalone.py`(B 版)+ `assets/templates/pyproject.toml` 为底稿,不要手打。

### 第 4 步:实现功能时的硬性规则

1. 一个项目只选一个包,`pyproject.toml` 依赖里不能同时出现 `mcp[cli]` 和 `fastmcp`。
2. (A) 包装饰器一律带括号:`@mcp.tool()` / `@mcp.prompt()`;(B) 包无参数时可裸用,一旦要传 `name`/`tags`/`meta` 等参数必须加括号。
3. Context 只用第 0 条白名单允许的获取方式;方法调用只用对应 reference 文件列出的方法,没列出的不要猜。
4. transport 字符串只从对应包白名单里选,拼写、连字符/下划线都不能凭感觉。
5. tool 函数写返回类型注解(结构化输出依赖它);`@mcp.resource(...)` 永远带 URI 参数。
6. stdio 模式下日志不能走 `print()`(会污染协议的 stdout 通道)——(A)/(B) 都用 `ctx.info/debug/warning/error` 或标准 `logging` 写 stderr。
7. 通用 Python 工程规范(测试、typing、异步写法、项目布局)不在本 skill 范围,遵循 `python-engineering`。

### 第 5 步:交付前自检(必须执行,不可跳过)

```bash
python3 <skill目录>/scripts/validate_mcp_server.py <server目录或文件>
```

机械检查:语法(py_compile)、混包检测、按包别检查第 0 条点名清单里的每一项、transport 字符串合法性、`@mcp.resource` 是否带 URI、tool 返回类型注解、stdio server 里的 `print()`、(A) 项目 pyproject 是否钉 `mcp<2`。**输出"0 个错误"才能交付**;警告逐条人工判断。

若环境无法运行脚本:`python3 -m py_compile server.py` 验证语法,再人工对照第 0/2/4 条逐条核对一遍导入、装饰器、Context 写法、transport 字符串。

### 第 6 步:交付物说明

交付时向用户说明本地调试方式:(A) 用 `uv run mcp dev server.py` 起 Inspector;(B) 用 `fastmcp run server.py:mcp` 或加 `--transport http`。接入 Claude Desktop/Code 的具体命令、pyproject 依赖钉法、传输选型建议,详见 `packaging-and-integration.md`。仓库发布(README/LICENSE/CI/tag)转 `oss-repo-ops`,不在本 skill 范围。

## 代码评审输出格式

```
【强制-违规】(A) 包里用了裸 @mcp.tool → v1.x 文档没有这种写法,必须加括号 @mcp.tool()。
【要求-建议改】(B) 包工具函数缺返回类型注解,建议补上以便结构化输出。
【通过】transport 字符串、Context 获取方式、pyproject 依赖钉法均无问题。
```

评审时同样运行 `scripts/validate_mcp_server.py` 辅助,但人工检查不能省(脚本只覆盖机械规则)。评审流程与分工纪律另见 `work-methodology`。
