---
name: devkit-overview
description: Navigate the 2718lab DevKit skills and select the smallest module contract for a task. Use at the start of any DevKit, repository, plugin, MCP, release, bug-fix, indexing, or workflow request.
---

# 2718lab DevKit 模块总览

先选一个最小模块 skill，再读取它的 references；不要把所有模块说明一次性装进上下文。
多模块任务先使用 `workflow-design`，默认走 Fast Lane。

| 模块 | 说明书 | 负责什么 |
| --- | --- | --- |
| Fast Lane 执行 | `fast-lane-routing` | 显式模型/effort、自动跨会话、索引 packet、终态补位 |
| 工作流设计 | `workflow-design` | 评分、拆分、槽位、依赖、验证和收口 |
| AstrBot | `astrbot-plugin-dev` | Star 插件、事件、配置、Web API、市场兼容 |
| Bugkiller | `bugkiller` | 有界修复、证据、交接、审查和验收 |
| Code Atlas | `code-atlas` | 本地配方、路由和证据 handoff |
| MCP | `mcp-server-dev` | MCP tool/resource/prompt、传输和打包 |
| OSS 发布 | `oss-repo-ops` | README、许可证、CI、版本、Release、市场提交 |
| Python 工程 | `python-engineering` | uv、依赖、ruff、pyright、pytest、布局 |

遇到跨模块任务，只加载 `workflow-design` 加上实际写入模块；不要重新启用已移除的
长篇 Work Methodology skill。
