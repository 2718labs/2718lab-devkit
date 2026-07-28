# Bugkiller for Codex

## Goal

把 `2718lab-devkit` 从一组开发 Skills 升级成 Codex 原生 Bugkiller 插件：能持久编排分诊、复现、定位、修复、验证和外部交付，并按任务难度控制模型成本。

## Scope

本轮包含可执行编排运行时、Bugkiller Skill、MCP 工具、插件内 Luna/Terra/Sol 角色定义、Python/TS/JS/Rust/Go 适配、Git/GitHub 门禁和当前 Codex 插件配置刷新。

不建设 WebUI，不自动合并 PR，不把任意 shell 隐藏在 MCP 内，不在脏主工作区直接修改。

## Direction

- 简单任务走线性状态机；复杂任务走可恢复的 DAG wave。
- SQLite 保存权威状态、租约、事件、证据哈希和预算；Markdown 只是角色化投影。
- 用户只看到方向和状态；每个 agent 只拿自己的任务卡、直接契约和必要证据。
- 允许依赖两端或共同契约订阅者点对点通信；MCP 只返回已授权的最小直投指令，发送 agent 调用宿主注入的 `collaboration.send_message`，SQLite mailbox 是离线恢复的权威来源，协调器不转发正文。
- Luna 负责低成本分诊和仓库地图，Terra 负责定位、实现和验证，Sol 默认预算为零。
- 普通任务不自动审查；危险任务先询问用户，再决定是否启用 reviewer 或 Sol。
- Codex Skill 负责可见交互和显式创建子代理；MCP 负责确定性编排，不替模型偷偷执行仓库命令。

## Risk Gate

认证、凭据、隐私、数据删除/迁移、供应链、生产发布、远程写入和不可逆操作必须先说明具体风险并询问。

`commit`、`push`、创建 PR 分别确认，批准对象绑定仓库、diff、测试证据、remote/ref 和 PR payload；任一变化使批准失效。

## Done

- 低风险 fixture 能从创建任务运行到验证完成，普通路径不调用 reviewer/Sol。
- 复杂任务能按依赖并发领取，旧租约不能覆盖新 worker，角色上下文不泄露 sibling cards。
- peer 消息只在允许关系内投递，带 correlation id、artifact hash、TTL/配额和 recipient ack，且不扩大权限或 write scope。
- 四种语言适配、审批/effect 恢复、脱敏和提示注入边界有自动化测试。
- 插件从长期 2718lab 源刷新到当前 Codex 配置，新任务能发现 Skill、插件内 Agents 和 MCP 工具。
