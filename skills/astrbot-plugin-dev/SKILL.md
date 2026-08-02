---
name: astrbot-plugin-dev
description: Build, review, debug, test, or publish an AstrBot v4 plugin or IM bot through 2718lab DevKit. Use only when the user explicitly requests this DevKit module or the task is scoped to its AstrBot repository; never load it for an unrelated project.
---

# AstrBot 插件开发

范围门：只处理明确属于 2718lab/AstrBot 的任务；不携带 DevKit 的任务、缓存或
工作流状态到其他项目。

把本 skill 当作 AstrBot 的短说明书。不要凭记忆拼 API；先读所需章节：

- `references/api-reference.md`：确认导入、事件、消息、LLM 工具、配置、Web API。
- `references/guidelines.md`：评审、兼容性和发布条款。
- `assets/templates/`：新插件的起步模板。
- `scripts/validate_plugin.py`：交付前机械检查。

## 硬约束

1. 只从 `astrbot.api.*` 导入；不要混入 NoneBot、Koishi、Graia、Quart 等框架。
2. 新插件先复制模板。目录名和 `metadata.yaml:name` 使用
   `astrbot_plugin_<name>`；版本为三段格式，兼容下界写在引号内。
3. `main.py` 只放唯一 `Star` 子类；业务逻辑放普通子模块。
4. 不定义 `__del__`；在主类直接定义幂等 `initialize`/`terminate`，取消任务并关闭会话。
5. handler 发消息用 `yield event.plain_result(...)`；异常用
   `logger.exception` 并返回友好文案；LLM 工具参数写完整 `Args:` docstring。
6. 持久化只写 `StarTools.get_data_dir()`；网络调用有超时；危险指令加管理员权限。
7. Web API 按当前 FastAPI 入口查 reference，不写 Quart。

## 交付

在插件目录运行 `python scripts/validate_plugin.py <plugin-dir>`，错误必须为 0；
再按 `python-engineering` 做语法、测试和工具链验证。发布运营转到
`oss-repo-ops`，MCP 桥接转到 `mcp-server-dev`。
