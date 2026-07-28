# (B) 独立版 FastMCP(jlowin/Prefect,当前 3.4.4)API 参考

> 本文件只覆盖 **(B) 包**:PyPI 上的 `fastmcp`(`from fastmcp import FastMCP`),github.com/jlowin/fastmcp,当前 3.4.4。
> 官方 SDK 内置的 v1.x `mcp.server.fastmcp` 见姊妹文件 `references/api-fastmcp-sdk.md`,**两份文件不要混着抄**——同一个项目里同时出现 `from fastmcp import ...` 和 `from mcp.server.fastmcp import ...` 就是混包,直接判违规。

全部事实来自 2026-07-11 抓取的 grounding report,来源标注见每节末尾。凡本文件未出现的 API 一律视为不存在,禁止使用。

## 0. 版本状态

PyPI 实测 3.4.4(pypi.org/pypi/fastmcp/json,2026-07-11 查得)。README 自己说"FastMCP 1.0 已在 2024 年被并入官方 MCP Python SDK"——也就是说 (A) 包 v1.x 是这条血脉的旧快照,现在两边已经分叉,API 不能互换。

3.x 相对 2.x 有 breaking changes(官方有专门的"Upgrading from FastMCP v2"指南),`enabled=` 装饰器参数在 3.0.0 起弃用。写代码前确认目标就是当前 3.x,不要照抄网上 2.x 时代的旧教程。

来源:github.com/jlowin/fastmcp README;pypi.org/pypi/fastmcp/json;gofastmcp.com/getting-started/upgrading/from-fastmcp-2

## 1. 安装与构造

```python
from fastmcp import FastMCP

mcp = FastMCP("My MCP Server")
```

来源:gofastmcp.com/getting-started/quickstart、gofastmcp.com/servers/server

## 2. 装饰器 —— 简单情形可以裸用,带参数才加括号

```python
@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@mcp.resource("data://config")     # resource 永远要 URI 参数,所以永远带括号
def get_config() -> dict:
    return {"version": "1.0"}

@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    ...
```

需要传参数时才加括号:

```python
@mcp.tool(name="find_products", description="...", tags={"catalog"}, meta={"team": "search"})
def find_products(query: str) -> list[dict]:
    ...
```

### `@mcp.tool` 全参数表(带括号调用时可传)

| 参数 | 说明 | 备注 |
|---|---|---|
| `name` | 覆盖对外暴露的工具名 | |
| `description` | 覆盖工具描述 | 不传则取 docstring |
| `tags` | `set[str]`,分类/过滤标签 | |
| `enabled` | 是否启用 | **3.0.0 起弃用**,改用 `mcp.enable()` / `mcp.disable()` |
| `icons` | 图标信息 | |
| `annotations` | 工具标注(如只读、幂等等提示) | |
| `meta` | 任意元数据 dict | |
| `timeout` | 执行超时 | |
| `version` | 工具版本标记 | |
| `output_schema` | 显式指定输出 JSON schema | |
| `run_in_thread` | 是否放到线程池执行(适合阻塞函数) | |

来源:gofastmcp.com/getting-started/quickstart、gofastmcp.com/servers/server、gofastmcp.com/servers/tools

## 3. Context —— 三种获取方式都有文档

### 3.1 依赖注入(v2.14+ 引入,首选写法)

```python
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context

mcp = FastMCP("My MCP Server")

@mcp.tool
async def process_file(file_uri: str, ctx: Context = CurrentContext()) -> str:
    await ctx.info(f"Processing {file_uri}")
    return "done"
```

### 3.2 类型注解注入(兼容写法,和 (A) 包唯一支持的方式一样)

```python
from fastmcp import FastMCP, Context

mcp = FastMCP("My MCP Server")

@mcp.tool
async def process_file(file_uri: str, ctx: Context) -> str:
    ...
```

### 3.3 `get_context()`(v2.2.11 起,用于嵌套调用/非工具函数里取 context)

```python
from fastmcp.server.dependencies import get_context

def helper():
    ctx = get_context()
    ...
```

三种都是 (B) 包合法写法。**版本门槛**:`CurrentContext()` 需要 fastmcp>=2.14,`get_context()` 需要 fastmcp>=2.2.11 ——如果 pyproject 里钉的版本低于这个下界又用了对应特性,判违规。

来源:gofastmcp.com/servers/context

## 4. Transport —— 现行字符串是 `"http"`,不是 `"streamable-http"`

```python
mcp.run()                                                  # STDIO,默认
mcp.run(transport="http", host="127.0.0.1", port=8000)     # Streamable HTTP,当前推荐名
mcp.run(transport="sse", host="127.0.0.1", port=8000)      # SSE,文档明确标"Legacy",新项目不要用
```

白名单:`"stdio"`(默认)、`"http"`、`"sse"`(legacy)。

**重要分叉点,别和 (A) 包搞混**:v3 升级指南写明 `sse_path`/`streamable_http_path`/`stateless_http` 等传输相关设置已经从 `FastMCP()` 构造函数搬到了 `run()` / `run_http_async()` / `http_app()` 的参数里;而且现行文档用的字符串是裸的 `"http"`,不是 `(A)` 包那个 `"streamable-http"`。**在 (B) 包代码里写 `transport="streamable-http"` 是把 (A) 包的字符串抄错了包,判违规**;也没有 `transport="streamable_http"`(下划线)这种写法。

**两包都没有**:`mcp.run(transport="websocket")` / `WSTransport` 作为服务端 transport——v3 升级指南明确说 `WSTransport` 已从客户端 transport 列表移除,当前两个包文档都没有服务端 websocket transport。见到就是幻觉。

来源:gofastmcp.com/deployment/running-server、gofastmcp.com/cli/running、gofastmcp.com/getting-started/upgrading/from-fastmcp-2

## 5. 运行 / CLI

```python
if __name__ == "__main__":
    mcp.run()   # 默认 stdio
```

```bash
fastmcp run my_server.py:mcp                              # stdio;CLI 直接 import mcp 对象,不走 __main__ 块
fastmcp run my_server.py:mcp --transport http --port 8000
```

`--transport` 允许值:`http` / `stdio` / `sse`(三选一,与上面 run() 的白名单一致)。

来源:gofastmcp.com/getting-started/quickstart、gofastmcp.com/cli/running

## 6. 完整最小骨架(tool + resource + prompt + context + run)

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

来源:上述各节引用来源综合,未新增本文件其他章节未列出的 API。
