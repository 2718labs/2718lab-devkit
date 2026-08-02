---
name: mcp-server-dev
description: Build, review, test, package, or integrate a Python MCP server. Use for FastMCP, mcp.server.fastmcp, @mcp.tool, resources/prompts, stdio/HTTP transports, or Codex .mcp.json integration.
---

# MCP Server 开发

这是 MCP 的短说明书；细节按需读取：

- `references/api-fastmcp-sdk.md`：官方 SDK 内置 v1.x（A）。
- `references/api-fastmcp-standalone.md`：独立 `fastmcp` 3.x（B）。
- `references/packaging-and-integration.md`：传输、CLI、打包和 Codex 接入。
- `assets/templates/`：A/B 起步模板；`scripts/validate_mcp_server.py`：交付检查。

## 先选包，再写代码

1. 默认选 A：`from mcp.server.fastmcp import FastMCP`，生产依赖 `mcp>=1,<2`，
   工具装饰器写 `@mcp.tool()`，HTTP transport 用文档中的连字符形式。
2. 只有确实需要独立包能力才选 B：`from fastmcp import FastMCP`，遵循 B 的
   装饰器和 transport；不要把 A/B 的 import、Context、transport 混在同一项目。
3. API 不在对应 reference 中就停止并查证；不要凭记忆臆造 `mcp.serve()`、
   `Server().tool()`、WebSocket server transport 或裸参数。

## 实现边界

- tool/resource/prompt 使用所选包的完整括号和 URI 约定；stdio stdout 只输出协议，
  日志写 stderr/logging。
- 保持工具输入有界、结果可序列化、错误稳定；不要把宿主私有凭据或路径放进公共响应。
- `mcp.run()` 只作为明确的进程入口；`.mcp.json` 的命令、cwd 和环境按 reference 配置。

## 交付

运行 `python scripts/validate_mcp_server.py <server-or-project>`，再用
`python-engineering` 的 uv/ruff/pyright/pytest 验证。发现混包或无法确认 API 时 fail-closed，
不要用另一个 FastMCP 包“试试看”。
