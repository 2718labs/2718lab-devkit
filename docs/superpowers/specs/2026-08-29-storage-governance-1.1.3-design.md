# 2718lab DevKit 1.1.3 存储治理设计

## 状态与选型

本文是 1.1.3 的实现基线。选定方案为“宿主权威的存储准入、持久租约账本与显式清理三层方案”（方案 A）。本设计只定义边界和证据，不执行清理、不改变现有用户文件。

## 背景与问题界定

G 盘空间下降的主要证据指向重复的 Cargo `target` 根：主 Host target、集成 target、孤立的 rmcp target 各自保存了大量 `incremental` 和 `deps` 文件。当前恢复树的 DevKit 源码约为数百 MiB，Codex/DevKit 会话数据不是这次增长的主因。1.1.3 的首要修复是阻止同一构建语义因任务目录、worktree 或重启而无限复制 target；会话治理是独立的保守收尾措施，不能被用来解释或掩盖 target 泄漏。

本机所有临时产物继续限定在 `G:\2718lab\_codex\.codex-task-temp` 下。宿主是路径、进程、磁盘统计和删除动作的唯一权威；DevKit 只产生经过规范化和哈希绑定的 storage intent，不能声称已经取得租约或已经清理。

## 目标与非目标

目标是：为 Cargo、Python、MCP 打包和 Fast Lane 任务提供确定性产物根；在写入前预留文件数、字节数和最低剩余空间；跨重启恢复租约；以 preview/candidate/recheck/apply 证据链执行有限清理；在 GitHub 可达且用户明确授权精确路径时才允许源代码清理；对归档会话只做 CAS 去重。

非目标是：全盘扫描后按大小删除、自动删除未知目录、活动任务、脏 worktree、源代码或活动/未归档会话；从目录名推断拥有者；以“已上传 GitHub”单独替代路径授权；引入远程会话同步、压缩策略或绕过 Fast Lane 的 route/lease/context/capability 证明。

## 方案比较

### 方案 A：Host 权威三层治理（选定）

Host 统一分配确定性 target 根，持久化 task storage lease ledger，所有清理都经过候选哈希和再次核验。优点是能同时约束 DevKit 与 Codex Host，能恢复中断状态，且删除范围可审计；代价是需要 ledger 迁移、进程身份证明和少量 Host 接口。适合当前重复 target 的根因和“不能误删”的要求。

### 方案 B：每个插件自行配额与定时清理

每个插件管理自己的 target 和临时目录。实现初期较小，但无法识别跨插件的重复 Cargo target，重启后也无法确认 owner；Host、DevKit 和插件会互相绕过配额。该方案不选。

### 方案 C：先上传远端，再删除本地

把构建产物、源码和会话先归档到远端，再由远端策略回收本地。它依赖网络、远端权限和同步语义，且不能解决本地 target 在上传前的无界增长。它可作为将来归档扩展，不进入 1.1.3 的删除路径。

## 三层架构

### 第一层：存储准入与确定性根（Storage Firewall）

Host 在进程启动前验证 approved task root、磁盘统计、target key、当前 lease 和预算。写入进程只能收到 Host 分配的绝对产物根；未登记的 `CARGO_TARGET_DIR`、临时根或 package cache 一律拒绝。低空间、统计失败、策略缺失、路径越出批准根或预算不足均 fail-closed，不通过杀死其他进程来“腾空间”。

### 第二层：所有权、配额与重启恢复（Lease Ledger）

Host 将每个任务的 generated storage 写入持久账本。账本以事务和原子替换保存，包含预留、心跳、过期、重启代数、进程启动身份及最后 receipt。活动租约不因清理扫描而失效；没有足够证据的过期租约进入 recovery/quarantine，而不是直接删除。

### 第三层：显式清理与远端/会话保护（Cleanup Governance）

清理是独立的、有限的 preview 到 apply 流程。仅有账本标记为 disposable、无 owner、内容哈希仍相同且位于批准 generated 根中的候选才能 apply。源代码清理还要经过 GitHub reachability 和用户精确路径授权。会话只允许对不可变、无活动引用的 archived CAS 对象做去重。

## 确定性 target key 与数据流

`target_key` 不含绝对路径、task id、随机数或 worktree 临时目录名，按 UTF-8 canonical JSON 计算：

```text
{
  "schema":"2718lab.storage.target.v1",
  "artifact_kind":"cargo-target",
  "repository_identity":<canonical remote/repository id>,
  "workspace_manifest_hash":<manifest hash>,
  "cargo_lock_hash":<Cargo.lock hash>,
  "toolchain_digest":<rustc/cargo/toolchain digest>,
  "target_triple":<target triple>,
  "profile":<profile>,
  "features_hash":<sorted feature set hash>,
  "build_env_class":<attested environment class>
}
```

`target_key = SHA-256(canonical_json)`，Host 将其映射到批准根下的固定目录，并拒绝同 key 的不相容字段。不同构建语义必须产生不同 key；相同 key 的并发写入须先取得同一 target-family exclusive lease，不能靠共享目录的偶然行为。Python/MCP 产物使用同样的 schema 规则，以 `artifact_kind` 和其锁文件/解释器摘要替换 Cargo 字段。

数据流固定为：DevKit Fast Lane 编译器产生绑定 `task_id/plan_binding/context_hash/storage_intent` 的请求；Host 验证 route、lease、context、capability 后规范化 intent，计算 target key，读取 G 盘统计并在 ledger 中 reserve；Host 启动 worker 并注入已分配的产物根；worker 周期性回报 bytes/files/receipt；Host 在 terminal receipt 后释放 lease；只有随后独立生成的 cleanup preview 才能进入清理链。Project Index 缺失不会给 storage intent 赋予路径或 owner，仍须 Host 自动重建并回 receipt。

## Task Storage Lease Ledger

每条记录的 schema 为 `2718lab.storage.lease.v1`，至少包含以下字段：

| 字段 | 约束 |
| --- | --- |
| `ledger_epoch`, `schema_version` | 单调 epoch 与精确 schema；迁移前后可核验 |
| `lease_id`, `task_id`, `assignment_id`, `plan_binding` | 唯一、不可改写并绑定 Fast Lane receipt |
| `project_identity`, `repository_identity`, `worktree_identity` | Host 已证明的项目与 worktree 身份 |
| `artifact_kind`, `target_key`, `path_identity` | generated 类型、确定性 key、批准根内规范绝对路径 |
| `owner_epoch`, `owner_kind`, `process_id`, `process_start_time`, `host_instance_id` | 防 PID 重用的 owner 证明；不是目录名推断 |
| `state` | `reserved`、`active`、`released`、`recovery_pending`、`quarantined` 或 `cleanup_eligible` |
| `created_at`, `last_heartbeat`, `expires_at`, `restart_generation` | 单调时钟与重启恢复信息 |
| `reserved_bytes`, `reserved_files`, `observed_bytes`, `observed_files` | 预留与实测值均不可超过策略 |
| `free_space_before`, `free_space_after_reserve`, `free_space_floor` | 准入时的磁盘证据 |
| `candidate_hash`, `receipt_hash`, `release_reason`, `cleanup_policy_hash` | 清理和终结证据绑定 |

状态变更只能由 Host 事务完成：`reserved -> active -> released`；重启或 owner 证明缺失时为 `recovery_pending`；任何路径、内容、owner 或账本不确定时为 `quarantined`；`cleanup_eligible` 只是候选资格，不是删除动作。

## 配额、文件数与剩余空间门槛

Host policy 必须明确登记 `task_byte_limit`、`task_file_limit`、`target_family_byte_limit`、`target_family_file_limit`、`global_reserved_byte_limit`、`global_reserved_file_limit`、`free_space_floor_bytes` 和 `emergency_floor_bytes`；缺少或溢出任何值返回 `STORAGE_POLICY_MISSING`。准入要求同时满足：

```text
requested_bytes <= task_byte_limit
requested_files <= task_file_limit
family_observed + family_reserved + requested <= family_limit
global_reserved + requested <= global_reserved_limit
free_before - (global_reserved + requested) >= free_space_floor_bytes
```

心跳按实测 bytes/files 重新核验。超出 byte/file 限额或 `free_space` 低于 floor 时，阻止新的 storage reservation 并返回稳定错误；不自动终止不属于本 lease 的进程，不删除活动目录。低于 emergency floor 时进入全局 pressure 状态，只允许释放、恢复和只读 preview；统计失败同样 fail-closed。

## 重启恢复

Host 启动先锁定 ledger，读取上一次 epoch 并增加 `restart_generation`。对每条 `active`/`reserved` 记录，只有同时匹配 `host_instance_id`、进程 PID、进程启动时间、owner epoch 和有效心跳的进程才可恢复为 active；其他记录转为 `recovery_pending`。Host 重新测量批准根并验证 target key、ledger path 和内容 manifest；缺失 receipt、路径变化、脏状态、未知文件或锁检测失败均转为 `quarantined`。恢复未完成前不允许 apply。中断中的 apply 带有 journal；重启后必须重新执行 candidate hash 和 owner 检查，不能按“已开始删除”继续盲删。

## Preview、候选哈希、复核与 Apply

1. `preview` 只读扫描 ledger 已登记的 approved generated roots，按规范路径排序，记录每个候选的 `path_identity`、类型、bytes、files、content/manifest hash、owner 状态、dirty/source/session 分类、原因、ledger epoch 和 policy hash，生成 `candidate_hash = SHA-256(canonical manifest)`。
2. 调用者提交精确的 `candidate_hash`、`policy_hash`、`ledger_epoch` 和有限 batch 上限；Host 取得 storage writer fence 后重新 stat、重新哈希并重新读取 lease/process/lock/Git 状态。
3. 任一值变化返回 `STORAGE_CANDIDATE_STALE`，释放 fence，不产生删除。任一候选是 unknown、active、dirty、source 或 session，整个候选项保持保护并记录稳定错误。
4. 只有全部复核通过的 disposable generated candidates 才能 apply；每个动作写入 journal 和 receipt，删除后立即验证路径不存在、账本状态为 released，并报告实际 bytes/files。部分失败保留未完成项并返回 `STORAGE_APPLY_INCOMPLETE`，不得扩大下一批范围。

清理永远不扫描批准根之外的目录，不跟随 reparse point，不依据大小、最近时间或目录名称猜测资格。未知、活动、脏 worktree、任意 source 文件、活动或未归档 session 永不自动删除。

## GitHub 可达性与精确路径授权

任何源代码、worktree 或本地代码目录的删除都是独立的 `source_cleanup` 操作。Host 必须同时证明：精确 commit 已在配置的 GitHub remote/repository 可达；remote identity 与本地 repository identity 匹配；worktree `status --porcelain` 为空；分支不是当前/受保护分支；没有 active lease、运行进程、待审查候选或未完成 receipt；并持有未过期的 `path_authorization`，其绑定 exact absolute path、repository identity、commit/tree hash、授权者、签发时间和 expiry。上传成功或 GitHub 可达本身不能替代该授权。remote 不可达、commit 不可证明、路径不精确或任一 owner 状态不明，分别返回 `GITHUB_REACHABILITY_UNAVAILABLE`、`GITHUB_COMMIT_NOT_REACHABLE` 或 `PATH_AUTHORIZATION_REQUIRED`，本地代码保持不变。

## 归档会话的 CAS 去重

CAS 去重不是通用清理。仅当 session 状态为 `archived`、对象不可变、content hash 与长度匹配、没有活动会话/lease/checkpoint/reference、且 ledger CAS transaction 成功时，Host 才能把重复对象的引用原子地指向唯一对象，再删除重复对象并写 receipt。任何 hash、引用计数、状态或锁读取失败都保留两个对象并返回 `STORAGE_CAS_MISMATCH` 或 `STORAGE_CAS_REFERENCE_ACTIVE`。活跃、未归档、内容不完整或未知来源的 session 不参与去重。

## 稳定错误

接口只返回固定 code、canonical detail 和 receipt identity。核心 code 为：`STORAGE_ROOT_NOT_APPROVED`、`STORAGE_TARGET_KEY_INVALID`、`STORAGE_POLICY_MISSING`、`STORAGE_QUOTA_EXCEEDED`、`STORAGE_FILE_LIMIT_EXCEEDED`、`STORAGE_FREE_SPACE_FLOOR`、`STORAGE_STAT_UNAVAILABLE`、`STORAGE_LEASE_CONFLICT`、`STORAGE_RECOVERY_REQUIRED`、`STORAGE_CANDIDATE_STALE`、`STORAGE_PROTECTED_UNKNOWN`、`STORAGE_PROTECTED_ACTIVE`、`STORAGE_PROTECTED_DIRTY`、`STORAGE_PROTECTED_SOURCE`、`STORAGE_PROTECTED_SESSION`、`STORAGE_APPLY_INCOMPLETE`、`STORAGE_POSTCHECK_FAILED`、`GITHUB_REACHABILITY_UNAVAILABLE`、`GITHUB_COMMIT_NOT_REACHABLE`、`PATH_AUTHORIZATION_REQUIRED`、`STORAGE_CAS_MISMATCH` 和 `STORAGE_CAS_REFERENCE_ACTIVE`。同一事实和输入必须得到同一 code，不以自然语言猜测替换 code。

## 最小 TDD 与编译验收

1. target key 的相同语义复用、任一构建字段变化分叉；
2. byte/file/floor/global reservation 四类准入和超限 fail-closed；
3. ledger reserve/heartbeat/release 及重启后 PID 重用防护；
4. preview hash 在内容、owner、ledger epoch 变化后失效；
5. unknown/active/dirty/source/session 五类保护零删除；
6. GitHub reachable 但无 exact path authorization 仍拒绝；
7. archived CAS 等哈希去重可提交，不匹配保持双对象；
8. 旧 ledger/无 ledger 的迁移和回滚保持未知根保护。

每项先产生可复现 RED，再以最小改动转 GREEN；不添加与边界无关的大型测试矩阵，不以单测替代 Host 运行证据。实现验收至少包括 Host 受影响 crate 的 `cargo check --locked -j1`、DevKit 变更 Python 文件的 `py_compile`、manifest/schema 校验、`git diff --check`，以及一个受控的 preview/apply 运行回执。磁盘 pressure 或统计失败时测试/build 也应停止新增 target 并报告稳定错误。

## 迁移与回滚

迁移使用 `storage-ledger-v1` 的事务性新表/文件和原子提交，先生成只读 inventory 与备份，再把已知 generated 根登记为 `recovery_pending`；旧 target、未知目录和 source 不因迁移自动删除。迁移失败恢复旧账本快照，所有产物保留。1.1.2 客户端没有 storage intent 时，Host 只允许受保护的兼容观察，不允许无账本写入。

回滚 1.1.3 时停止新的 storage admission，完成或标记现有 lease 的 recovery receipt，保留 ledger、journal 和所有未验证根；1.1.2 可读取既有代码但不能执行 1.1.3 的 apply。重新启用时从 ledger epoch 继续，不重建随机 target。任何部分迁移或 apply 失败均可回到上一个 ledger snapshot，绝不通过 `reset`、全盘删除或隐式路径扩张恢复。

## 1.1.3 版本边界

本版本交付：确定性 generated target 根；Host storage lease ledger；task/family/global byte-file-free gates；重启恢复与 pressure fail-closed；preview/candidate/recheck/apply 和 receipts；GitHub reachability 加 exact path authorization；archived session CAS dedupe；稳定错误、迁移、回滚和最小验收工具。

本版本不交付：远程 session/Atlas 同步、自动删除源代码或普通 session、全盘清理器、透明压缩、跨机器共享 target、额度绕过、Fast Lane route/lease/context/capability 降级，以及未经过用户精确授权的分支/worktree 删除。1.1.3 的完成条件是这些边界在 Host 与 DevKit 的生产路径中均有实现和 receipt 证据，而不是仅有设计文档或局部静态测试。
