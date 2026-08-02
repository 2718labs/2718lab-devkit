---
name: workflow-design
description: Design bounded 2718lab work with the default Fast Lane. Use only for an explicitly scoped DevKit task; cover multi-file changes, parallel work, model/effort selection, local or cross-session host dispatch, dependency fencing, verification, and completion planning.
---

# 工作流设计（默认 Fast Lane）

默认启用 Fast Lane；不要等用户重复说“高速”。只有用户明确关闭或仓库契约禁止时才降级。
这是工作流默认，不是 CLI 无参数自动启动：host 仍须传入明确 effort；`ultra` 自动激活，
其他 effort 需要 `--enable`。本 skill 只设计边界；真正的编译器是
`fastlane_compile` / `team_efficiency.py`，`fast-lane-routing` 只说明 host 如何消费 assignment。
不要把当前 DevKit 的任务、索引、缓存或 workflow 带入其他项目。

## 设计顺序

1. 读取任务卡、仓库范围和直接契约；记录 scope、歧义、风险、写入数、验证成本、
   blocker 和可用容量，交给 `mcp-tools/devkit_fastlane/scripts/team_efficiency.py` 编译。
2. 把互不重叠的工作拆成独立 assignment；每个 assignment 只有一个 writer，读取/预热
   只能只读。没有真实可消费证据就不要占槽位。
3. 使用编译器给出的精确 `model`、`reasoning_effort` 和 `host_dispatch`；不从 UI、当前
   会话或推荐文字猜模型，不让子代理继承当前模型。
4. 当 projection 的 `dispatch_policy.action=dispatch_all` 时，跨会话由 compiler 决定，
   不让 LLM 选择。只有可信 host integration 在 worktree、lease、context 和 predecessor
   fence 都验证后才机械创建独立会话/工作树；compiler 和 skill 不直接调用 host dispatch。
   `dispatch_none` 不创建，`stop` 失败关闭。
5. 将一个有界 `index_context` 交给 worker 消费；host 在 dispatch/terminal 边界各做一次查询，
   worker 不注册、同步、查询或轮询索引。
6. 终态事件、证据 hash、lease/context 通过后才 refill、集成、验收和归档；断线、过期、
   hash 不匹配一律停止或重新建立带前驱 fence 的 assignment。

## 不可变安全底线

- 不重叠写 scope，不让 worker 自己验收或修改主工作树。
- 不把 Spark 当常规槽位；只有可复现的严重 blocker、窄 scope 和清晰回滚/验证才用。
- 不把 commentary 当状态，不因“看起来空闲”补位，不声称未验证的完成。
- Fast Lane 的临时目录、工作树和普通缓存必须通过当前编译器批准的任务根；
  额度样本缓存才可由用户用 `--quota-state-path` 单独配置。不得使用 C 盘临时目录。

交付时报告真实 RED/GREEN、变更文件、commit、独立验证和剩余 concerns。
