# 分层工作包

复杂任务按读者拆分上下文，而不是按“文档类型”堆成一份大 spec。

## 目录结构

```text
<work-package>/
  product-brief.md
  index.md
  contracts/
    <one-shared-topic>.md
  tasks/
    <task-id>.md
```

## 读取规则

| 角色 | 默认读取 | 禁止默认读取 |
|---|---|---|
| 用户/产品经理 | `product-brief.md` 和状态摘要 | task cards、代码级 contracts、完整日志 |
| 主协调代理 | `index.md`、产品方向、当前任务状态 | 所有 task cards 的全文 |
| 执行代理 | 自己的 task card、卡片链接的 contracts | sibling cards、完整产品历史 |
| 危险 reviewer | 风险卡、最终 diff、验证证据、相关 contract | 与风险无关的任务上下文 |

## 上下文预算

- `product-brief.md`：最多 120 行，禁止代码块。
- `index.md`：最多 160 行，只保存任务 DAG、状态、链接和 dispatch 规则。
- 单张 task card：最多 220 行，一个 owner、一个主要目标、一个明确 write scope。
- contract：每个文件只定义一个共享主题；变大时按接口边界拆分。

行数是上限，不是目标。能用 30 行说清就不要写 100 行。

## Product Brief 模板

```markdown
# <Feature>

## Goal
用户最终得到什么。

## Scope
本轮包含与不包含什么。

## Direction
关键方案与取舍，不写代码和逐步实现。

## Risk Gate
哪些危险动作必须停下来询问用户。

## Done
产品层如何判断可用。
```

## Coordinator Index 模板

```markdown
# <Feature> Work Index

## Shared Contracts
- `contracts/<topic>.md`

## Tasks
- `tasks/<id>.md`: pending | ready | in_progress | blocked | done

## Dispatch
- 当前 wave：<task ids>
- 写入冲突：<none 或说明>
- 下一门禁：<条件>
```

索引不放代码、不复制任务步骤、不保存长日志。

## Task Card 模板

```markdown
# <ID> <Task>

Owner: <one-agent-role>
Depends on: <ids 或 none>

## Goal
一个可独立验收的结果。

## Context
只列本卡必须读取的 contract、源码和已确认事实。

## Write Scope
- `<exact/path>`

## Steps
详细实现步骤；代码/API 约束可写在这里。

## Acceptance
精确命令、输入和通过条件。

## Return
改动文件、真实输出、结论和阻塞项。
```

## Contract 规则

只把两个以上任务都必须遵守的稳定接口放入 contract，例如数据模型、函数签名、状态枚举或安全边界。单任务实现细节留在该任务卡。contract 变化时只通知直接依赖它的任务。

## Wave 规则

1. 先写产品 brief 和最小 index。
2. 只为依赖已满足的任务创建详细卡片。
3. 每个 agent 一张卡；一个卡片不要跨越两个可独立验收的模块。
4. 当前 wave 完成后更新 index，再生成下一 wave 卡片。
5. 用户只看方向与状态；除非主动询问，不把任务卡全文贴进对话。

## 验证

严格索引卡在注册时使用 `strict_index=true`，并在卡片验收中列出 `project_index_sync`、`project_index_query`、`trace_id`、`worktree_checkpoint_create`、`project_index_sync(bind_as="output")`、`workflow_artifact_register(kind="verification", snapshot_id=...)` 和 `workflow_complete`。遗留卡保持 `strict_index=false`；不要为此扩展用户 brief 或把细节复制进总文档。

```powershell
python mcp-tools/devkit_fastlane/scripts/validate_work_package.py <work-package>
```

验证器检查文件存在性、上下文预算、必需章节、单 owner 和精确 write scope。它不替代业务测试。

## 禁止模式

- 一份文档同时服务产品经理、协调代理和所有执行代理。
- 在 index 中嵌入所有代码、测试和逐步实现。
- 给每个 subagent 发送同一份完整设计文档。
- 提前写完所有未来 wave 的巨型计划，随后让它与现实漂移。
- 用“详情见总计划”代替任务卡的明确边界。
