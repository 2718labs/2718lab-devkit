"""起步模板 —— (B) 独立版 fastmcp(jlowin/Prefect,当前 3.4.4)。

只用 references/api-fastmcp-standalone.md 里出现过的 API。复制本文件后:
1. 改 FastMCP(...) 里的名字。
2. 把示例 tool/resource/prompt 换成真实业务逻辑。简单 tool 可以裸装饰器,
   一旦要传 name/tags/meta 等参数就必须加括号 @mcp.tool(...)。
3. 需要日志时用 ctx.info/debug/warning/error 或标准 logging 写 stderr,不要 print()
   (stdio 模式下 stdout 是协议通道)。
"""

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
    # 本地调试: fastmcp run server_standalone.py:mcp
    # 常驻网络服务: fastmcp run server_standalone.py:mcp --transport http --port 8000
    mcp.run()
