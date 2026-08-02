"""起步模板 —— (A) 官方 MCP Python SDK 内置 v1.x FastMCP。

只用 references/api-fastmcp-sdk.md 里出现过的 API。复制本文件后:
1. 改 FastMCP(name=...) 里的名字。
2. 把示例 tool/resource/prompt 换成真实业务逻辑,保留装饰器括号写法。
3. 需要日志时用 ctx.info/debug/warning/error 或标准 logging 写 stderr,不要 print()
   (stdio 模式下 stdout 是协议通道)。
"""

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
    # 本地调试: uv run mcp dev server_sdk.py
    # 接入 Codex: 在插件根目录的 .mcp.json 中声明 command/args/cwd
    # 常驻网络服务改用: mcp.run(transport="streamable-http")
    mcp.run()
