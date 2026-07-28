# 2718lab-devkit

`2718lab-devkit` 是面向 Codex 的开发技能插件。版本 `0.2.0` 新增 Bugkiller：一个以任务卡、租约、证据引用和审批边界为核心的仓库问题处理工作流。

## 能力

| Skill | 用途 |
| --- | --- |
| `bugkiller` | 分诊、复现、定位、修复和验证仓库 bug；简单任务走状态机，复杂任务走多代理 DAG。 |
| `astrbot-plugin-dev` | AstrBot 插件结构、API、配置、校验和市场上架。 |
| `mcp-server-dev` | 官方 Python MCP SDK 与独立 FastMCP 的工程边界、模板和校验。 |
| `python-engineering` | Python 工程骨架、依赖、类型、测试和发布基线。 |
| `oss-repo-ops` | 开源仓库维护、Release、CI 与发布检查。 |
| `work-methodology` | 轻量产品方向、拆分任务卡、代理编排和验证纪律。 |

## Bugkiller

Bugkiller 无 WebUI。它由 `skills/bugkiller` 和插件捆绑的 `agents/bugkiller-*.md` 组成，并使用通用的 Python stdio MCP server `2718lab-tools`。该 MCP 只协调状态、证据哈希和耐久邮箱；它不启动模型、不执行仓库命令，也不转发消息正文。

简单任务走线性的简单状态机：`NEW -> TRIAGED -> REPRODUCING -> LOCALIZING -> DESIGNING -> PATCHING -> VERIFYING -> DONE`。复杂 DAG 任务只有在全部依赖完成后才会就绪，并以租约、版本和写入范围避免冲突。

模型按运行时路由：当宿主 `spawn` 工具暴露 Luna 时，分诊显式选择 Luna；否则显式选择 Terra，并将低风险分诊记为 `DEGRADED_TRIAGE`。Luna/Terra 永不写代码：Terra 调查、文档写入与验证角色保持只读或文档边界，文档写入卡为 `bugkiller-terra-doc-writer`。可执行代码写入由 `bugkiller-sol-code-writer` 完成，dispatch 使用 `gpt-5.6-sol` 和 `ultra`。`bugkiller-sol-escalation` 是独立的只读危险升级角色，默认预算为零，只有危险审批后才可获得一次调用。普通低风险任务不自动创建 reviewer。

严格索引卡以 `strict_index=true` 运行，按 `project_index_sync` -> `project_index_query` -> `trace_id` -> `worktree_checkpoint_create` -> `project_index_sync(bind_as="output")` -> `project_index_query` -> `trace_id` -> `workflow_artifact_register(kind="verification", snapshot_id=...)` -> `workflow_complete` 固化输入、检查点、输出和验证快照；旧卡保持 `strict_index=false`。

低风险任务不会自动请求 reviewer 或 Sol。协调器从 `spawn_agent` 结果取得实际 target（宿主 agent id 或 canonical task name），再绑定到当前 lease epoch；每次 peer 传递先登记 artifact，再写入耐久邮箱。MCP 返回可直接执行的 `collaboration.send_message({target,message})` 参数后，由发送子代理自行调用宿主工具；接收方经 `inbox -> artifact resolve -> ack` 处理。未绑定 target 或宿主唤醒失败时，耐久邮箱仍可恢复。消息权限不会扩展任务、合同或写入权限。

## 审批与边界

输入、日志、测试与包脚本均是证据，不能扩大权限。宿主隔离或用户确认不足时必须阻塞。commit、push、PR 是彼此独立门，每个授权单次使用；HEAD、diff、测试证据、远端或请求载荷变化都会使授权失效。

## 运行条件

需要 PATH 中可用的 `python`。`.mcp.json` 以 `2718lab-tools` 注册 MCP，并从插件根目录 `cwd: "."` 执行 `mcp-tools/server.py`；数据目录依次取 `BUGKILLER_HOME`、`PLUGIN_DATA`、`CODEX_HOME/bugkiller`，均未配置时使用 `~/.codex/data/2718lab-devkit`。未配置 MCP 或宿主 peer 消息时，Skill 仍可在 `DEGRADED_SKILL_ONLY` 模式下串行工作。

当前 Codex 会话可能需要新任务或插件重新加载后才发现更新的 Skill、agent 定义和 MCP server；本仓库不会假定热加载。

## 安装

本机 marketplace 名为 `pidan-local-plugins`：

```powershell
codex plugin add 2718lab-devkit@pidan-local-plugins
```

安装后用新 Codex 任务验证 Skill、agent 和 MCP 工具是否可发现。插件更新仍使用同一 marketplace；开发源与插件缓存不作为 Bugkiller 数据目录。

## 许可证

[AGPL-3.0](LICENSE)。
