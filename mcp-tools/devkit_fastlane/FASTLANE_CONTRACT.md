---
name: fastlane-runtime-contract
description: Contract for the bounded Fast Lane compiler and its host execution boundary.
---

# 2718lab Fast Lane runtime 契约

核心原则：给用户方向，给代理边界，给执行证据。不要让任何角色读取与自己无关的完整大文档。

按需读取：

- 多代理或需要持久计划：读 `references/work-packages.md`。
- 需要创建、领取或恢复任务：读 `references/orchestration-runtime.md`。
- 需要选择 team 形状或写 dispatch：读 `references/team-patterns.md`。
- 需要生成安全的本地启动计划、恢复包、Todo 状态、契约/缓存检查、手工工件 wave、Fast Lane request/plan，或基于 `ImplementationPacket.to_dict()` / observed `GraphQueryResult.to_dict()` 的 Atlas 证据 wave 与工作流生命周期计划：读 `references/efficiency-automation.md`。
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

执行 `team_efficiency.py` 生成的生命周期计划时，先按拓扑顺序完成全部 `workflow_register_task`，再逐 wave 调用 `workflow_ready`、`workflow_claim` 和 `workflow_endpoint_bind`；只有当前 wave 的所有任务都达到 `DONE` 才进入下一 wave。`workflow_ready` 因先前已将任务提升为 READY 而返回空集时，仍以 SQLite 中的持久任务状态为 claim 前置条件，不要求返回集合与计划 wave 完全相等。

`task_episode_graph` 只能由可信 host 转发真实 Code Atlas 输出。规范化哈希、标识符和 provenance 字段只验证内部一致性，不认证调用方提供的 JSON 来源；observed fixture 也不是端到端真实性证据，来源认证边界由 ATLAS-12C 完成。

严格索引任务在 `workflow_register_task` 时标记 `strict_index=true`：先 `project_index_sync`，再 `project_index_query` 取得 `trace_id`，创建 `worktree_checkpoint_create`，写入后以 `project_index_sync(bind_as="output")` 和再次查询确认输出；最后登记 `workflow_artifact_register(kind="verification", snapshot_id=...)`，才可 `workflow_complete`。旧任务保持 `strict_index=false`。

## Scheduler topology V1

`2718lab-devkit/scheduler-topology-v1` binds auditable opaque identity values
for the plan, lease, and G-drive worktree. A/B/C are main conversation,
scheduler, and writer; A alone reviews and integrates, and each scheduler has
at most a `1:3` writer set. Design and prewarm are read-only and do not count
as writers, but remain gated by actual host slots, host capability, lease, and
safety gates. A cross-scope `declared-child` split must strictly reduce conflict
or return `UNSPLITTABLE_SCOPE_CONFLICT`. This contract does not restore
account-usage quota, D-drive temporary roots, or a parent model ceiling, and
does not weaken host capability, lease, worktree, review, or safety gates.

如果 MCP 不可用，允许按 `references/work-packages.md` 使用文件降级，但必须标记 `DEGRADED_SKILL_ONLY`，关闭并发写入和崩溃恢复承诺，并向用户说明这不等价于完整插件。

### 3.1 工作包项目隔离（V2）

`team-efficiency/work-package-v1` 是可读、可分解的诊断载荷，不是执行授权。它可以被
`decompose`/`plan-waves` 显示，但任何会创建 assignment、worktree、claim、resume 或恢复持久
状态的 Fast Lane 编译调用，遇到 v1 必须返回 `NO_SAFE_WORK` 和
`LEGACY_PROJECT_UNBOUND`，零 assignment、零队列、零外部派发。

未来外部 Desktop bridge 如要消费的载荷必须是 `team-efficiency/work-package-v2` exact-key envelope，包含原始 canonical v1
`package`、其 `package_payload_hash`、`project_fence`（仅 `project_id`、
`binding_digest`、`binding_version`）、`workspace_id` 与 `input_snapshot_id`。V2 的
source-plan hash 必须包含该整个 binding，因此相同 task/workflow 在不同项目、workspace 或输入
snapshot 下不能共用计划、lease、receipt 或恢复状态。

manifest 中的 fence 只是可验证的结构与 hash 输入，绝不是 authority。当前仓库没有 Desktop-host
durable registry 或真正私有的跨边界 authority bridge，因此没有任何同进程 provider、module
attribute、closure、环境变量、请求 JSON、repo/task root、路径名或 caller-supplied ID 可被当作
live authority。公开 `compile_fast_lane` 与 `fast-lane` CLI 对 structurally valid V2 一律产出
`NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE`，零本地 assignment、零队列、零外部派发；V2
envelope/hash 无效、其内层 canonical v1 `package` 不能完成纯诊断解析，或 fast-lane request
壳的 schema/key/字节边界无效时，必须是 `PROJECT_BINDING_INVALID`；v1 保持
`LEGACY_PROJECT_UNBOUND`。这些结构预检不读取 host、账号用量或 index 输入，也不触及 scheduler。
公开 MCP request 若试图携带明确的 host-private 字段（如 `host_status`、账号用量或 index
evidence），则是适配器输入违规，必须在编译前以 `FASTLANE_REQUEST_INVALID` 拒绝，而不是把
该值当作可诊断的计划输入。
增加
Desktop-host durable registry、跨进程 authority 传递或公开 MCP 参数属于后续外部 host 合同，不能由
工作包 JSON 或 Python 私有命名假装已经存在。

同一限制覆盖 `bootstrap --apply` 及 import-callable `apply_bootstrap_plan`：当前公开入口在构建
caller-supplied bootstrap plan 或调用 worktree mutation 前，无条件以
`NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE` 失败关闭，因而不能到达
`git worktree add`。不带 `--apply` 的 `bootstrap` 仍只输出 dry-run 诊断计划；其中的 project、
root、worktree 和任何 JSON 都不是 sealed V2 execution context。仓库当前不存在可执行的
host-authorized worktree path：没有 module-private capability、runner、Git probe 或 adapter 可绕过
该关闭结果。Desktop host registry 与真正私有的跨边界 execution bridge 是外部前置条件；它们尚未在
本仓库实现，也不能用 Python module attribute、closure 或 caller-supplied JSON 伪装。

### 4. 接地后再写

列出不能百分之百确认的接口并逐个查证。查不到时选择可被现有证据支持的保守实现，并明确记录限制；不把编译器当 API 文档。

### 5. 调度与回传

按 `references/team-patterns.md` 选择最小 team。每个 dispatch 必须给出任务卡绝对路径、允许写入的路径、依赖、验收命令和禁止事项。代理只回传：改动文件、真实命令输出、结论和阻塞项。

#### Ultra Fast Lane

下面是未来外部 Desktop bridge 的 host 合同形状：

```text
python scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --host-status <fast-lane-host-status.json> --reasoning-effort ultra
```

`ultra` 自动激活（Ultra automatic activation）；低于 Ultra 的 effort 必须由 host 显式传入 `--enable`，否则得到 inactive plan。当前仓库的公开 `fast-lane` CLI/API 不消费 host-status、额度或 index 输入来激活该合同：在外部 Desktop authority bridge 实现并验收前，它始终输出 `NO_SAFE_WORK/PROJECT_AUTHORITY_UNAVAILABLE` 的零 assignment/队列预览。下文的 descriptor、route 与 host dispatch 约束只定义未来 bridge 的接入要求，不是本仓库存在的执行通路。`fast-lane` 本身不调用模型、不启动 agent、不创建会话或工作树、不运行 gate、不改写 Git、不领取或完成 workflow。协调器 lane 保有设计、集成、风险决策和最终验收责任；是否需要 Sol 设计/独立终审由精确的 host-attested route 决定，编译器不硬锁某个模型。

host 通过不超过 3 MiB、有 exact-key 的 `--host-status` 传入 `workflow_id`、当前 lease/binding 与
`routing_context`。后者按 `(task_id, scheduler_role)` 唯一关联完整
（每个不超过 32 KiB 的）`2718lab-devkit/fastlane-routing-request-v3` 和可信证据 hash；scheduler 只调用
`fastlane_routing.py`，不得从 `recommended_route`、profile 或 capability 猜测/重建
score、floor、fallback。每个 start receipt 绑定 `routing_context_hash`、
`routing_result_hash`、`task_fingerprint`、reason codes、safety-floor rank 以及该次有界
routing input；生命周期校验重放该历史 core input，不以较晚的 host event 改写旧 receipt。
该 envelope 最多容纳原 16 个 unit 和一个已批准 remediation unit 的 85 个 task/role entries。

缺少、重复、未知 task、task/role 不一致或 core unavailable（包括 capability 未 attested）
任一此类条目都必须让整个 dispatch plan fail closed 到 `NO_SAFE_WORK`（零 worker、零队列），绝不回落到固定 `recommended_route`。
`ultra` 只激活
lane；worker model/effort 逐任务由 core 和 host attestation 决定，worker effort 禁止 `ultra`。
prewarm 始终是独立的只读证据角色，不能变为 execution。

host 只消费 `action="start"` descriptor，绝不重新 spawn `action="retain"`；仅在终态事件后（only after a terminal event）refill，且没有安全有用的工作（no safe useful work）时必须如实保留 idle slot。不得按 commentary 更新轮询或补位（no commentary polling）。

跨会话不是 LLM 的自由选择：当
`cross_session_dispatch_projection.dispatch_policy.action="dispatch_all"` 且
`target="independent_codex_session"` 时，host 必须机械地派发该 projection 的全部
assignments；`dispatch_none` 不创建会话，`stop` 失败关闭。该 policy 的
`selection_authority="compiler"`、`llm_choice=false` 固定不变。
这仍是 inert policy descriptor：只有可信 host integration 在 worktree、lease、
context 与 predecessor fence 全部验证后才能机械创建独立会话/工作树；compiler 和
skill 不直接调用 host dispatch 工具。

每个 assignment 还必须消费 `host_dispatch`。其中的 `model` 和
`reasoning_effort` 是本次调用的显式参数，`inherit_current_session_model=false`、
`require_explicit_route=true`；host 调用 `collaboration.spawn_agent` 时必须原样传入
这两个值，缺失或改写就拒绝派发，不能让宿主从当前会话（例如 Luna）继承模型。可信
host-attested child route 是独立路由权威，不受 DevKit parent-session rank ceiling 限制；
没有该 attestation 时必须拒绝而非提升或猜测。

索引由 host 在边界一次性准备：assignment 的 `index_context` 是有界的
`team-efficiency/fast-lane-index-context-v1`，包含输入/输出 snapshot、写/读 scope、
已知节点锚点和单次查询预算。host 在 dispatch boundary 做一次 input query，在
terminal boundary 做一次 output query；worker 只消费 packet，不调用
`project_index_register/sync/status/query`，也不做 item 内轮询。缺少 packet 或 hash
不匹配时停止该 assignment。这样索引安全约束仍在，但不会把低价值的索引仪式交给
LLM 自己编排。

Fast Lane 不包含额度协调器，也不读取、缓存、推断或以额度作路由输入。任何未来额度
能力须作为外部宿主独立产品建立新合同；本发行版的 host route 与子会话派发不引用该能力。

若 `host_spawn_exact_route` 必须先取得 `host_target`，它只能是 `parked endpoint bootstrap`：claim（及条件 endpoint bind）成功前 worker 保持 inert，禁止下发任务或访问 worktree、gate、写入、checkpoint、sync/query、receipt、candidate、terminal；这不是 prewarm，也不新增 compiler operation。独立会话必须在获准任务根下创建并绑定自己的隔离 Git worktree；不得把协调器的脏集成工作树当作 worker 工作区，缺失或无法验证 worktree 时 fail-closed。

归档不是 adapter 操作：只能在协调器 lane 已完成 acceptance、最终证据已绑定之后由 host 执行。
X 轮回滚均未恢复时，系统仅产生候选清理资格；默认不自动删除任何 worktree、证据、缓存或用户数据。
Fast Lane 的宿主任务根由 `CODEX_FASTLANE_TASK_ROOT` 配置；本机默认
`G:\2718lab\_codex\.codex-task-temp`，显式根也必须位于 G:。默认 `G:\2718lab\_codex\.codex-task-temp`
是本机唯一允许的临时根基线。
配置值必须是现存的、本地绝对、非 C 盘、不得为卷根且不含 reparse-point
的目录，编译器只在其下派生受限 `project` 的任务根，并在规范化前按词法路径拒绝 project
子路径中的 reparse-point。bootstrap 和 read-context 的 worktree、scratch、普通 cache 与测试
证据都必须严格位于该派生根下；每个 read-context 必须声明 project，且有 execution
context 时必须与其 project 对齐，并携带 `task_root_hash`；配置变化时每个
read-context 与 bootstrap 都重算该 hash。路径尾随点/空格、保留设备名或根外目标均
fail-closed；Windows apply 在目录创建和 Git 调用期间持有只共享 read、拒绝 write/delete
共享的目录句柄：它会自行创建、校验并 pin 空的最终 worktree 叶目录后才调用 Git，拿不到句柄或
叶目录已存在就停止。默认 bootstrap 保持 v1；非默认根使用只携带规范根 hash 的
bootstrap-v2，在 apply 前重算该 hash。该变量是宿主配置，不是 request 或 bootstrap
plan 可自报的根字段。
禁止把 `TEMP`、`TMP`、`TMPDIR` 或临时根指向 C: 或 G: 以外的本机盘符。C-drive temporary roots are forbidden。
这仍是当前 bootstrap/read-context 边界。空项目只能生成 bootstrap-only index：不得据此创建可执行
assignment，直到可信宿主提供有界项目索引上下文。旧 schema 输入返回
`FASTLANE_SCHEMA_UPGRADE_REQUIRED`，不得降级猜测或启动工作。

### 6. 验证与交付

按 `references/verification-checklist.md` 运行受影响路径。向用户只报告方向性结果、关键证据、风险/限制和下一外部动作；代理级实现细节留在任务卡和执行记录中。

## 领域路由

| 领域 | Skill |
|---|---|
| MCP / FastMCP | `mcp-server-dev` |
| Python 工程 | `python-engineering` |
| 发布、CI、市场 | `oss-repo-ops` |

过程规则听本 skill，领域接口听对应 skill。
