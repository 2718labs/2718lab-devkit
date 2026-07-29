---
name: work-methodology
description: Use when a 2718lab engineering task spans multiple files or agents, needs framework/API grounding, risks a context-heavy design document, or is approaching completion without fresh verification evidence.
---

# 2718lab 工程工作方法

核心原则：给用户方向，给代理边界，给执行证据。不要让任何角色读取与自己无关的完整大文档。

按需读取：

- 多代理或需要持久计划：读 `references/work-packages.md`。
- 需要创建、领取或恢复任务：读 `references/orchestration-runtime.md`。
- 需要选择 team 形状或写 dispatch：读 `references/team-patterns.md`。
- 需要生成安全的本地启动计划、恢复包、Todo 状态、契约/缓存检查、手工工件 wave，或基于 `ImplementationPacket.to_dict()` / observed `GraphQueryResult.to_dict()` 的 Atlas 证据 wave 与工作流注册计划：读 `references/efficiency-automation.md`。
- 不确定框架/API：读 `references/grounding-discipline.md`。
- 准备交付：读 `references/verification-checklist.md`。

## 过程保真

1. 不凭记忆写框架/API 签名。优先查本地源码，再查领域 skill、官方文档和上游讨论。
2. 没有本次真实执行证据，不宣称完成、修复或测试通过。
3. 不伪造命令输出、文件内容、测试结果或代理状态。
4. 低风险任务不自动创建审查代理。发现安全、凭据、数据删除/迁移、生产发布、远程写入或不可逆操作时，先向用户说明具体风险并询问；用户明确同意后，才启动额外审查或高成本模型。

## 最小充分工程

1. 只实现用户目标和验收条件要求的最小闭环；不要把“可以更完整”当成扩 scope 的理由。
2. 索引已返回所需源码、调用关系或文档节点时，不得重复扫描全仓；只对索引明确缺失或过期的局部内容回源。
3. 与当前改动无关、且不构成安全风险、真实回归或共享契约冲突的问题，只记录为非阻塞项，不得扩展当前 scope、另开任务或增加模型成本。
4. 需求已满足、受影响路径验证通过且没有阻塞风险时，立即停止并交付；不得为了形式完整继续补文档、重构、格式化或重复验证。

## 开工优先

1. 用户目标包含生产实现，且首个 Patch 的接口、路径和验收已明确时，完成一次有界索引查询与写前检查点后立即进入实现。不得先创建“设计冻结”波次、Mermaid 图或后续任务卡来证明流程完整。
2. 只有用户明确要求设计交付，或两个以上独立实现任务确实共享尚未稳定的 contract 时，文档才能成为实现前置。contract 验证通过后必须直接解锁实现，不追加未由验收或已识别风险要求的“更硬”检查。
3. 每类验收运行一次足以证明结论的主路径和受影响回归；通过后停止。验证失败时只修复该失败暴露的问题，不顺手增加新的门禁。
4. 任务卡和计划不得硬编码带版本号的 Codex 插件缓存路径。优先调用 MCP 工具；必须写命令时使用稳定插件源路径或在运行时解析当前版本。

## 缺陷处理门槛

Bug 不可能被一次性根除。修复工作的目标是消除已复现、会造成实质损害的问题，而不是清空所有潜在缺陷。

- 阻断交付：数据或历史丢失、崩溃、无回复、权限或隐私越界、安装失败、主要流程稳定回归。
- 本轮处理：有稳定复现且修复成本与影响相称的问题。
- 非阻塞：纯理论风险、低概率边角、无稳定复现的猜测、仅改善形式一致性的重构。
- 停止条件：目标复现已消失，受影响旧路径通过，且没有新的阻断问题。达到条件后立即交付，不继续追求“零 bug”。

## 工作流程

### 1. 判定任务形状

| 形状 | 做法 |
|---|---|
| 单点修改、答疑、小脚本 | 主代理直接完成，不创建持久工作包 |
| 跨多文件探索 | 分派只读探索卡，主代理汇总 |
| 两个以上独立实现单元 | 建分层工作包，按任务卡扇出 |
| 共享接口尚未稳定 | 主代理先写小型 contract，再分派 |
| 高风险或不可逆 | 进入危险门禁，先问用户 |

### 2. 分层保存上下文

复杂任务必须使用工作包：

- `product-brief.md`：给用户/产品经理，只写目标、范围、方向、风险门槛和完成标准。
- `index.md`：给协调代理，只写任务 DAG、状态、共享契约链接和当前 wave。
- `tasks/*.md`：一张卡只交给一个 owner，包含详细步骤、写入范围和验收命令。
- `contracts/*.md`：只保存跨任务共享且稳定的接口；每个文件一个主题。

主代理不创建巨型总计划。执行代理只读取自己的任务卡和卡片明确链接的 contract，不读取全部 sibling cards。格式与限制见 `references/work-packages.md`。

### 3. 使用插件编排

多代理任务优先使用 `2718lab-tools` 的可执行编排：SQLite 保存任务 DAG、租约、事件和内容哈希，MCP 返回 ready wave、claim 结果和角色化上下文。Markdown 工作包是面向人和 agent 的投影视图，不是权威状态。

严格索引任务在 `workflow_register_task` 时标记 `strict_index=true`：先 `project_index_sync`，再 `project_index_query` 取得 `trace_id`，创建 `worktree_checkpoint_create`，写入后以 `project_index_sync(bind_as="output")` 和再次查询确认输出；最后登记 `workflow_artifact_register(kind="verification", snapshot_id=...)`，才可 `workflow_complete`。旧任务保持 `strict_index=false`。

如果 MCP 不可用，允许按 `references/work-packages.md` 使用文件降级，但必须标记 `DEGRADED_SKILL_ONLY`，关闭并发写入和崩溃恢复承诺，并向用户说明这不等价于完整插件。

### 4. 接地后再写

列出不能百分之百确认的接口并逐个查证。查不到时选择可被现有证据支持的保守实现，并明确记录限制；不把编译器当 API 文档。

### 5. 调度与回传

按 `references/team-patterns.md` 选择最小 team。每个 dispatch 必须给出任务卡绝对路径、允许写入的路径、依赖、验收命令和禁止事项。代理只回传：改动文件、真实命令输出、结论和阻塞项。

### 6. 验证与交付

按 `references/verification-checklist.md` 运行受影响路径。向用户只报告方向性结果、关键证据、风险/限制和下一外部动作；代理级实现细节留在任务卡和执行记录中。

## 领域路由

| 领域 | Skill |
|---|---|
| AstrBot | `astrbot-plugin-dev` |
| MCP / FastMCP | `mcp-server-dev` |
| Python 工程 | `python-engineering` |
| 发布、CI、市场 | `oss-repo-ops` |

过程规则听本 skill，领域接口听对应 skill。
