---
name: workflow-design
description: Design bounded 2718lab work with the default Fast Lane. Use for multi-file changes, parallel tasks, model/effort selection, local or cross-session dispatch, dependency fencing, verification, and completion planning.
---

# 工作流设计（默认 Fast Lane）

默认启用 Fast Lane；不要等用户重复说“高速”。只有用户明确关闭或仓库契约禁止时才降级。
本 skill 设计边界，`fast-lane-routing` 执行已编译的 assignment。

## 设计顺序

1. 读取任务卡、仓库范围和直接契约；记录 scope、歧义、风险、写入数、验证成本、
   blocker 和可用容量，交给 `mcp-tools/devkit_fastlane/scripts/team_efficiency.py` 编译。
2. 把互不重叠的工作拆成独立 assignment；每个 assignment 只有一个 writer，读取/预热
   只能只读。没有真实可消费证据就不要占槽位。
3. 使用编译器给出的精确 `model`、`reasoning_effort` 和 `host_dispatch`；不从 UI、当前
   会话或推荐文字猜模型，不让子代理继承当前模型。
4. 当 projection 的 `dispatch_policy.action=dispatch_all` 时，机械地把全部 assignments
   派到独立会话/工作树；LLM 不选择是否跨会话。`dispatch_none` 不创建，`stop` 失败关闭。
5. 将一个有界 `index_context` 交给 worker 消费；host 在 dispatch/terminal 边界各做一次查询，
   worker 不注册、同步、查询或轮询索引。
6. 终态事件、证据 hash、lease/context 通过后才 refill、集成、验收和归档；断线、过期、
   hash 不匹配一律停止或重新建立带前驱 fence 的 assignment。

## 不可变安全底线

- 不重叠写 scope，不让 worker 自己验收或修改主工作树。
- 不把 Spark 当常规槽位；只有可复现的严重 blocker、窄 scope 和清晰回滚/验证才用。
- 不把 commentary 当状态，不因“看起来空闲”补位，不声称未验证的完成。
- 临时目录、缓存和工作树落在用户配置的任务根下；不使用 C 盘临时目录。

交付时报告真实 RED/GREEN、变更文件、commit、独立验证和剩余 concerns。
