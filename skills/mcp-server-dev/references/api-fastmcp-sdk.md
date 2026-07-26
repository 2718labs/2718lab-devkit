# (A) 官方 MCP Python SDK 内置 FastMCP v1.x API 参考

> 本文件只覆盖 **(A) 包**:`mcp` 官方 SDK 里 `v1.x` 分支的 `mcp.server.fastmcp.FastMCP`。
> 独立版 `fastmcp`(jlowin/Prefect,当前 3.4.4)见姊妹文件 `references/api-fastmcp-standalone.md`,**两份文件不要混着抄**。

全部事实来自 2026-07-11 抓取的 grounding report,来源标注见每节末尾。凡本文件未出现的 API 一律视为不存在,禁止使用。

## 0. 版本状态(先看这个,决定能不能用)

`mcp` 仓库现在同时有两条线:

| 分支 | 状态 | import | 能否用于生产 |
|---|---|---|---|
| `v1.x` | 稳定,维护模式 | `from mcp.server.fastmcp import FastMCP` | **可以**,本文件描述的就是这条线 |
| `main`(v2) | 预发布 alpha/beta(`2.0.0aN`/`2.0.0bN`),目标 2026-07-27 出稳定版 | `from mcp.server import MCPServer`(类名改了,breaking change) | **禁止**。仓库自己的说明就写了不要在生产用 |

见到 `from mcp.server import MCPServer` 或任何 `2.0.0a`/`2.0.0b` 版本号出现在依赖里 → 那是 v2 alpha,不是本文件描述的稳定 API,直接判违规。

来源:github.com/modelcontextprotocol/python-sdk/blob/v1.x/README.md;github.com/modelcontextprotocol/python-sdk/blob/main/README.md

## 1. 安装与构造

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="Tool Example")
```

依赖钉法:`pyproject.toml` 里写 `mcp>=1,<2`,防止环境升级时把 v2 alpha 拉进来。

来源:docs/server.md

## 2. 装饰器 —— 括号是必须的

v1.x 文档里**每一个**示例都带括号调用,没有一个裸装饰器的例子:

```python
@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    return f"Hello, {name}!"

@mcp.prompt()
def greet_user(name: str, style: str = "friendly") -> str:
    ...
```

**禁用**:裸 `@mcp.tool`、裸 `@mcp.prompt`(不带括号)。v1.x 文档中不存在这种写法,不要因为 (B) 包里能这样写就搬过来用——那是另一个包的语法。

`@mcp.resource(...)` 永远带 URI 字符串参数,两个包都一样,没有例外。

来源:README.md、docs/server.md

## 3. Context —— 只有类型注解注入这一种方式

```python
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP(name="Tool Example")

@mcp.tool()
async def my_tool(x: int, ctx: Context) -> str:
    await ctx.info("processing...")
    return str(x)
```

v1.x 文档里 `Context` 只能通过给参数打类型注解拿到,**没有**依赖注入辅助函数。

**禁用(点名)**:`get_context()`、`CurrentContext()` —— 这两个是 (B) 独立版 2.x/3.x 才有的东西,v1.x 文档从未出现过。在 (A) 包代码里见到这两个名字 = 写错了,是把 (B) 的 API 抄过来了。

### Context 已确认的方法/属性(白名单,只能用这些)

- `ctx.request_id`
- `ctx.client_id`
- `ctx.fastmcp`
- `ctx.session`
- `ctx.request_context`
- `await ctx.debug(message)`
- `await ctx.info(message)`
- `await ctx.warning(message)`
- `await ctx.error(message)`
- `await ctx.log(level, message, logger_name=None)`
- `await ctx.report_progress(progress, total=None, message=None)`
- `await ctx.read_resource(uri)`
- `await ctx.elicit(message, schema)`

只允许用以上这些。文档里没提到的 `ctx.` 方法,查不到就不要用。

来源:docs/server.md §Context

## 4. Transport —— 字符串是封闭枚举,注意连字符

```python
mcp.run()                              # 默认 stdio
mcp.run(transport="streamable-http")   # 注意:连字符,不是下划线
mcp.run(transport="sse", mount_path="/search")
```

白名单:`"stdio"`(默认,不传即用)、`"streamable-http"`、`"sse"`。

**禁用(点名)**:`transport="streamable_http"`(下划线版,文档里没有这种写法)、`transport="http"`(那是 (B) 包现行的字符串,(A) 包不用这个名字)。

来源:docs/server.md §"Streamable HTTP Transport"

## 5. 运行 / CLI

```bash
uv run mcp dev server.py                    # MCP Inspector 调试模式
uv run mcp dev server.py --with pandas --with-editable .
uv run mcp install server.py                # 装进 Claude Desktop
uv run mcp install server.py --name "My Analytics Server" -v API_KEY=abc123 -v DB_URL=... -f .env
uv run mcp run server.py                    # 直接运行(仅限 FastMCP,低层 Server 不支持)
```

文档原话:"`uv run mcp run` 或 `uv run mcp dev` 只支持用 FastMCP 写的 server,不支持低层 Server 变体"——如果代码里用的是低层 Server 变体而不是 `FastMCP`,这两个命令跑不起来(grounding 里只提到"低层 Server / low-level server variant"这个说法,没给出具体 import 路径,该路径本文件不确认,不要照抄成 `mcp.server.lowlevel.Server`)。

接入 Claude Code(v1.x README quickstart 里给的是这条,不是 `mcp install`):

```bash
claude mcp add --transport http my-server http://localhost:8000/mcp
```

来源:docs/server.md §"Running Your Server";README.md

## 6. 依赖钉法(pyproject.toml 片段)

```toml
[project]
dependencies = [
    "mcp[cli]>=1,<2",
]
```

`<2` 这条上界是关键——防止 v2 alpha(`MCPServer`,breaking API)被误装进来。

## 7. 完整最小骨架(tool + resource + prompt + context + run)

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

来源:docs/server.md、README.md 综合(逐段对应上面各节引用来源,未新增任何本文件其他章节未列出的 API)。
