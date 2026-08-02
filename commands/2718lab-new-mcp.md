---
description: 用 mcp-server-dev 规范起一个新的 MCP 服务器(先选 A/B 包,再复制模板)
argument-hint: [服务器名] [sdk|standalone]
---

# /2718lab-new-mcp

按 `mcp-server-dev` skill 起一个 MCP 服务器骨架。

目标:$ARGUMENTS

步骤:

1. 读 `mcp-server-dev` skill(第 0 条「双包保真」+ 第 2 步「选包决策」)。**两个同名 FastMCP 包 API 不同,禁止混用。**
2. 选包:默认 (A) 官方 SDK 内置 `mcp.server.fastmcp`(依赖最小、求稳);要 `CurrentContext()` / `tags`/`meta`/`timeout` 等高级装饰器参数才选 (B) 独立版 `fastmcp`。
3. 复制模板:从 `skills/mcp-server-dev/assets/templates/` 选择 (A) `server_sdk.py` 或 (B) `server_standalone.py`,并配上同目录的 `pyproject.toml`。
4. 一个项目只用一个包:依赖里不能同时出现 `mcp[cli]` 和 `fastmcp`;(A) 一律钉 `mcp>=1,<2` 防 v2 alpha 混入。
5. 实现 tool/resource/prompt:只用两份 `references/api-fastmcp-*.md` 里出现过的 API;transport 字符串逐字取白名单;`@mcp.resource(...)` 永远带 URI 参数;stdio 模式不用 `print()`。
6. 交付前运行 `python skills/mcp-server-dev/scripts/validate_mcp_server.py <server目录或文件>`。

交付前红队 → `/2718lab-review`。
