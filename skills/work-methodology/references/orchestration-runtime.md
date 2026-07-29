# 可执行编排运行时

Skill 负责触发和解释，运行时负责确定性状态。没有运行时的文件流程是降级模式，不是完整插件。

## 两种执行形状

- 简单任务使用 linear state machine：`NEW -> READY -> RUNNING -> VERIFYING -> DONE`，避免为单点修复创建完整 DAG。
- 复杂任务使用 DAG wave：依赖满足的任务进入 `ready`，协调器一次只分派当前 wave；后续卡片按需生成。

两种形状共享同一事件、租约、预算和证据存储。

## 权威状态

SQLite 是 task、dependency、lease、attempt、event、evidence hash 和 budget 的权威来源。filesystem Markdown is a projection for humans and agents, not the authoritative scheduler state.

投影文件可以删除并重建；数据库状态不能通过手改 Markdown 推进。状态变化必须通过 MCP 工具事务完成。

## 最小工具面

| Tool | 作用 |
|---|---|
| `workflow_create` | 创建简单状态机或复杂 DAG 工作流 |
| `workflow_register_task` | 注册一张任务卡及依赖、owner role、write scope、内容哈希 |
| `workflow_ready` | 返回依赖已满足且未被领取的当前 wave |
| `workflow_claim` | 用租约和 fencing token 原子领取任务；可同时绑定宿主返回的 Codex agent target |
| `workflow_endpoint_bind` | 在当前 owner/epoch 下绑定或更换实际子代理 target |
| `workflow_complete` | 保存结构化结果，释放租约并解锁下游任务 |
| `workflow_status` | 返回产品摘要或协调器状态，不返回全部卡片全文 |
| `workflow_context` | 返回 role-scoped context：一张任务卡、直接 contracts 和必要证据 |
| `workflow_artifact_register` | 以有效任务租约登记受限 artifact 的 hash、安全路径、大小和脱敏版本，不接收正文 |
| `workflow_peers` | 返回当前任务可通信 peer 的最小身份、关系和 capability，不返回 sibling card |
| `workflow_message_send` | 校验授权后写入 durable mailbox，并为在线 peer 返回直接 `send_message` 的投递指令 |
| `workflow_inbox` | 领取方读取自己的未确认 mailbox 项，不扫描其他任务消息 |
| `workflow_artifact_resolve` | 领取方以当前租约把自己的 delivery hash 解析为受限 artifact 路径与元数据 |
| `workflow_message_ack` | 以 recipient 和 delivery id 确认已处理消息，保留审计摘要 |
| `workflow_cancel` | 冻结新 claim，撤销租约并记录取消事件 |

插件将这个通用协调 MCP 注册为 `2718lab-tools`；Bugkiller 只是使用它的一种上层工作流。MCP 只管理编排数据，不在服务内部启动模型或隐藏 shell 命令。Codex 主代理读取 ready wave 后显式创建子代理。

## Strict index write gate

Legacy registrations retain `strict_index=false`. A strict task follows this exact sequence: `project_index_sync` -> `strict_index=true` -> `project_index_query` -> `trace_id` -> `worktree_checkpoint_create` -> `project_index_sync(bind_as="output")` -> `project_index_query` -> `trace_id` -> `workflow_artifact_register(kind="verification", snapshot_id=...)` -> `workflow_complete`. The first query and trace bind the input snapshot, the checkpoint precedes writes, and the output sync/query bind the verification snapshot before completion. Only Sol may call the acceptance completion gate after review.

## 点对点通信

Durable handoff order is `workflow_artifact_register -> workflow_message_send -> workflow_inbox -> workflow_artifact_resolve -> workflow_message_ack`. Direct chat may wake a worker after durable delivery but is never task context, evidence, handoff, or acceptance source of truth.

消息仅可在已注册的 dependency edge 两端，或同一 workflow 中显式订阅同一 contract 的任务之间发送；订阅关系由任务注册时固定，不能由消息创建。消息不会扩大接收方的 role、lease、contract access 或 write scope。

`workflow_peers` 仅返回调用任务的允许 peer。协调器从 `spawn_agent` 结果取得实际 target；不同 Codex 宿主可能返回 UUID agent id 或 `/root/...` canonical task name。协调器把该值交给 worker 在 claim 时绑定，或在 worker claim 后用当前 owner/epoch 调 `workflow_endpoint_bind`，不得从 workflow task id 猜测 target。只有未过期租约且绑定 target 的 receiver 才会获得 direct instruction。发送 agent 必须原样调用返回的 `collaboration.send_message({target,message})`；这才是实际的 direct `send_message`，MCP 进程本身不能调用宿主工具。host message 只是固定字段的短唤醒，不含正文或日志。

接收 agent 被唤醒后执行 `workflow_inbox -> workflow_artifact_resolve -> 读取 safe_path -> workflow_message_ack`。coordinator does not relay message bodies，且不把它写进 event payload。无论宿主唤醒是否成功，`workflow_message_send` 都会在 SQLite durable mailbox 建立接收方私有的待确认记录；`workflow_inbox` 是恢复和离线投递来源。租约换代会清除旧 target，避免把新消息投给已经失效的 worker。

每条记录包含不可变 `message_id`、workflow/sender/recipient task id、`correlation_id`、正文或附件的 `artifact_hash`、创建/过期时间和 redacted metadata。正文作为受限 artifact 保存，以 hash 去重；事件只保存 hash 和安全摘要。发送端、接收端及 workflow 都有消息数和字节配额；超过配额、过期 TTL、无效 hash 或非允许 peer 必须以稳定错误拒绝。ack 只允许 recipient 在 TTL 内执行，幂等且不删除审计记录；过期正文不再投递。

## 效率规则

1. 用 content hash 去重 repository map、task card、contract、测试命令和验证输出；相同输入不重复采集。
2. `workflow_context` 不返回 sibling task cards、完整聊天历史或不相关日志。
3. Sol owns architecture, dispatch, review, integration, and final acceptance. Terra High handles routine bounded work; Terra Max handles complex work; Sol High is explicit exceptional escalation only. Luna is unavailable and is never spawned or substituted.
4. 同一 write scope 只允许一个有效租约；只读任务可以并行。
5. 任务完成只回传结构化摘要、artifact hashes 和阻塞项；长日志保存在证据路径。
6. 普通低风险流程不创建 reviewer。高风险先问用户，用户允许后再分派危险审查。
7. 协调器仅协调 peer capability、delivery id 和 artifact hash；不得将消息正文作为 status、context 或 agent-to-agent relay 的一部分。

## 恢复

每次 claim 产生单调递增 fencing token。旧 worker 在租约过期后恢复时，`complete` 必须因 token 过期而拒绝。协调器根据事件日志和任务哈希重建 ready wave，不重放已确认完成的任务。

严格任务重领按当前阶段的已绑定快照校验工作区：尚未绑定输出时使用输入快照，已经绑定输出时使用该任务的输出快照。输出快照仍匹配时必须原地签发新 epoch；不得要求代理临时移走、删除或重建合法任务输出来恢复输入快照。工作区与当前阶段快照不一致时才恢复检查点或阻塞。新 epoch 只需重新登记依赖租约的查询与证据，文件内容不重做。

## 降级模式

MCP 或 SQLite 不可用时：

- 标记 workflow 为 `DEGRADED_SKILL_ONLY`。
- 只允许一个写入 agent，任务按 index 串行推进。
- Markdown 保存方向和卡片，但不声称提供原子 claim、租约隔离或崩溃恢复。
- 恢复完整运行时前，不执行高风险或远程副作用。
