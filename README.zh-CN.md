[English](README.md)

# 2718lab DevKit —— Codex + MCP v1.1.1

[![版本](https://img.shields.io/badge/version-v1.1.1-blue)](./.codex-plugin/plugin.json)
[![许可证](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

2718lab DevKit 是一个 Codex-first 工程工具包：它包含一个本地、仅 stdio
传输的 MCP 运行时，用于有边界的项目索引、Atlas 证据、Relay 生命周期协调和
确定性的 Fast Lane 规划；同时还包含一组精简的 Skill 说明书。本仓库承载版本化的
v1.1.1 包；已提交的 manifest 和 allowlist 定义可执行运行时范围，说明书导航、
安装、构建和验证章节共同给出支持的工作流。

当前版本保留刻意 fail-closed 的 Fast Lane 预览。公共编译器和 CLI 固定返回
`NO_SAFE_WORK` 与零 assignments：不会消费 host-status 或实时账号输入，也没有
worktree 执行路径。宿主执行属于未来外部 Desktop-host bridge 合同的要求。

> [!IMPORTANT]
> **工作流提醒：** 先用有界证据路由。A1/A2/A3 并行只允许发生在互不重叠、独占
> 的写入范围，并各自使用 G: 盘隔离任务根。执行前必须 claim 并 bind；prewarm
> 只读，`action="retain"` 不是新 spawn。主对话须经 PR-style 独立审查后再集成，
> 最后验收和归档。Fast Lane 不使用额度协调器或额度输入。不要把运行时状态或凭据提交到仓库。

> **调度拓扑 V1：** `2718lab-devkit/scheduler-topology-v1` 以 opaque identity
> 绑定计划、lease 和 G 盘 worktree。A/B/C 分别是主对话审查合并、scheduler 和
> writer；每个 scheduler 最多 1:3 writers。design/prewarm 只读且仍受实际 host
> slot、host capability 和 safety gates 约束。跨 scope 只能 declared-child split，
> 且必须严格降低冲突，否则为 UNSPLITTABLE。

## 已交付内容

- Project Index 提供不透明工作区注册、受限快照、状态和图查询。
- Checkpoint 服务创建、检查和恢复证据绑定的快照。
- Atlas 提供有边界的图查询、实现包准备、惰性渲染和持久化验收投影。
- Relay 验证显式工作包并暴露生命周期状态。Python 只返回结构化宿主
  动作；工作树准备和 agent 调度仍由 Codex 宿主负责。
- 可执行 MCP 运行时精确暴露 17 个工具，不暴露 MCP prompts、MCP resources、
  静态 prompt agent 或模型运行器。
- 可选的 Codex Skill bundle 是 DevKit 的说明书表面，提供简短的模块化手册；
  它不构成第二个运行时，也不是可执行的 prompt/agent 表面。
- Fast Lane 是 MCP runtime 中的纯本地编译器。其公共面当前没有调度权限并且
  fail-closed：不会产生 assignment，也不会 spawn agent、修改 Git、运行命令或
  执行 worktree。宿主执行预留给未来的外部 Desktop-host bridge 合同。
  RuntimeRoot 的 host-private V2/V3 bootstrap 仅由注入的测试替身覆盖；
  没有 external host embedding 实证，也不声称 operational/host-integrated GO。

## 核心模块速览

| 模块 | 作用 | 从哪里开始 |
| --- | --- | --- |
| [`mcp-tools/server.py`](mcp-tools/server.py) | stdio MCP 入口和公共 17 工具面 | [精确 MCP 面](#精确-mcp-面) |
| [`mcp-tools/project_index/`](mcp-tools/project_index/) | 工作区注册、受限快照、状态和图查询 | [Project Index 工具](#精确-mcp-面) |
| [`mcp-tools/devkit_atlas/`](mcp-tools/devkit_atlas/) | 证据图查询、实现包、渲染和验收投影 | [Atlas 工具](#精确-mcp-面) |
| [`mcp-tools/devkit_relay/`](mcp-tools/devkit_relay/) | 显式工作包编译和生命周期宿主动作 | [Relay 工具](#精确-mcp-面) |
| [`mcp-tools/devkit_runtime/`](mcp-tools/devkit_runtime/) | 运行时路径、checkpoint、持久边界和宿主私有 bridge | [运行时数据与恢复](#运行时数据与崩溃恢复) |
| [`mcp-tools/orchestrator/`](mcp-tools/orchestrator/) | 持久化 workflow、task、lease 和生命周期状态 | [workflow 生命周期](mcp-tools/devkit_fastlane/references/efficiency-automation.md#workflow-lifecycle-plan) |
| [`mcp-tools/devkit_fastlane/`](mcp-tools/devkit_fastlane/) | 确定性路由/Fast Lane 编译器、契约和测试 | [Fast Lane 契约](mcp-tools/devkit_fastlane/FASTLANE_CONTRACT.md) |
| [`.codex-plugin/`](.codex-plugin/) | 插件 manifest、产物 allowlist 和可复现构建器 | [构建主产物](#构建主产物) |

## 整体工作流

仓库级默认工作流是 Fast Lane。`workflow-design` 准备有界输入；
`fastlane_compile` 或 `team_efficiency.py` 随后只返回无权限、fail-closed 的计划。
`fast-lane-routing` 记录的是预期的未来宿主消费边界；skill 和当前编译器均不会
启动 agent，也不会创建或执行跨会话工作树。当前路径只用于检查被阻断的计划，
权限仍保留在本仓库之外。

```mermaid
flowchart TD
    A["安装并配置 .mcp.json"] --> B{"选择入口"}
    subgraph MCP["MCP 运行时"]
        C["mcp-tools/server.py<br/>stdio 入口"] --> D["Project Index / Checkpoint<br/>Atlas / Relay"] --> E["有界结果<br/>宿主动作"]
    end
    subgraph FAST["Fast Lane"]
        F["fast-lane request"] --> G["team_efficiency.py<br/>公共编译器"]
        G --> X["失败关闭<br/>NO_SAFE_WORK，零 assignments"]
        H["未来外部 Desktop-host bridge<br/>仅合同"] -. "未交付或调用" .-> G
    end
    B -->|MCP 工具| C
    B -->|Fast Lane| F
```

## Codex 说明书导航

DevKit 刻意分为两个表面：MCP 运行时执行有界工具工作，本地插件则包含可选的、
仅供查阅的 Codex 说明书 bundle。该 bundle 有一个短总览
（`skills/devkit-overview`）、一个工作流设计说明书
（`skills/workflow-design`，默认 Fast Lane 策略）和六份独立模块说明书。按需加载：

`fast-lane-routing` · `bugkiller` · `code-atlas` ·
`mcp-server-dev` · `oss-repo-ops` · `python-engineering`。

Skills 是说明书，不是 MCP 工具或可执行的 prompt surface。它们不含 slash command、
agent profile、脚手架模板、校验器或调度代码。精简的运行时 ZIP 刻意不带入这些
说明书，但这个打包边界不代表 DevKit 只包含 MCP 运行时。

## 文档导航

把本页作为入口，按契约跳转到需要的细节，不必重复阅读整个仓库：

- [Fast Lane 契约](mcp-tools/devkit_fastlane/FASTLANE_CONTRACT.md)
- [效率自动化参考与 CLI 细节](mcp-tools/devkit_fastlane/references/efficiency-automation.md)
- [验证清单](mcp-tools/devkit_fastlane/references/verification-checklist.md)
- [工作包与任务卡规则](mcp-tools/devkit_fastlane/references/work-packages.md)
- [编排运行时契约](mcp-tools/devkit_fastlane/references/orchestration-runtime.md)
- [团队与 lane 模式](mcp-tools/devkit_fastlane/references/team-patterns.md)
- [仓库自动化与审查](docs/governance/repository-automation.md)
- [参与贡献](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [行为准则](CODE_OF_CONDUCT.md)
- [历史设计记录](docs/superpowers/README.md)：仅供追溯，可能提及已退出的组件，
  不是当前实现契约。
- [发布历史](CHANGELOG.md)

实现入口见
[Fast Lane 编译器](mcp-tools/devkit_fastlane/scripts/team_efficiency.py)。
Fast Lane 不含额度协调器合同；公共编译器和 CLI 不读取、协调或推断账号额度。

## 精确 MCP 面

公共服务器名为 2718lab-devkit。每个结果都使用有界的
2718lab-devkit/tool-result-v1 包络。公共面精确包含：

| 区域 | 工具 |
| --- | --- |
| Project Index | project_index_register、project_index_sync、project_index_status、project_index_query |
| Checkpoint | worktree_checkpoint_create、worktree_checkpoint_status、worktree_checkpoint_restore |
| Atlas | atlas_query、atlas_prepare、atlas_render、atlas_accept |
| Relay | relay_compile、relay_start、relay_status、relay_handoff、relay_integrate |
| Fast Lane | fastlane_compile |

服务器没有 prompt 或 resource 面。工具输入是结构化且有界的；绝对工作
者路径、Shell 片段、原始源码、凭据、无界命令输出和调用方伪造的验收证据
都会被拒绝。

## 本地安装与运行

要求：Python 3.11 或更高版本，以及 uv。

在仓库根目录执行：

    cd mcp-tools
    uv sync --locked --no-dev
    uv run --locked --no-dev python server.py

标准宿主配置是 .mcp.json。它以 mcp-tools 为工作目录运行上面的锁定命令，
转发两个宿主私有 bridge selector 名称，以及可选的项目/线程范围标识：

- CODEX_DEVKIT_HOST_BRIDGE_FD
- CODEX_DEVKIT_HOST_BRIDGE_HANDLE
- CODEX_PROJECT_ROOT、CODEX_WORKSPACE_ROOT
- CODEX_PROJECT_ID、CODEX_WORKSPACE_ID、CODEX_THREAD_ID

这些是 selector 或 identity 名称，不是应该自行编造或塞进任务消息的值。后五项
会把持久化状态限定在单个项目或线程，避免投影到另一个工作区。需要私有
宿主 capability broker 或 proof registry 的 Relay 生命周期变更，在宿主
没有提供可证明能力时会失败关闭，并返回
RELAY_CAPABILITY_BROKER_UNAVAILABLE。服务器不会暴露原始 handle，也不会
退回到无关的本地 start。

## 构建主产物

allowlist builder 会在插件源码树之外生成确定性的 ZIP。请选择源码树之外的输出目录：

    python .codex-plugin/build_main_artifact.py --plugin-root . --output <artifact-output-dir>/2718lab-devkit-v1.1.1.zip

产物包含 manifest、.mcp.json、LICENSE、锁定的 Python 项目，以及
.codex-plugin/main-artifact-allowlist.json 选中的运行时文件。它的可执行运行时
表面是 MCP 服务器；ZIP 同时携带 Fast Lane 契约、必需参考资料和策略 assets、
`team_efficiency.py` 兼容入口及其路由模块。它明确不包含可选的 Skill 说明书 bundle、
命令辅助文件、hooks、CI
文件、宿主私有状态、prompts、静态 agent 或任意仓库文件。

Fast Lane 可通过以下可执行入口检查其 fail-closed 结果：

    python mcp-tools/devkit_fastlane/scripts/team_efficiency.py fast-lane --input <fast-lane-request.json> --reasoning-effort ultra

遗留的 host-only 开关仅保持解析兼容。当前公共 CLI 不会读取或消费它们，
也不能激活工作；本发行版不含账号用量协调器或缓存合同。

`CODEX_FASTLANE_TASK_ROOT` 以及 worktree/cache 位置同样预留给未来由宿主拥有的
执行 bridge。当前公共编译器既不会创建也不会执行 worktree，不能把它的输出当作
已接受 worktree 配置的证据。

## 运行时数据与崩溃恢复

持久化数据留在本地。RuntimeConfig 按以下顺序解析数据根：

1. 宿主为本安装显式提供的绝对目录 `CODEX_DEVKIT_DATA_ROOT`。
2. 宿主显式提供的绝对目录 `PLUGIN_DATA`。
3. `CODEX_HOME/data/2718lab-devkit`。
4. 默认 Codex 数据目录：
   %USERPROFILE%\.codex\data\2718lab-devkit。

便携式本地安装应把 `CODEX_DEVKIT_DATA_ROOT` 设为持久的 G: 盘路径，
例如 `G:\CodexData\.codex\data\2718lab-devkit`。这个覆盖项与旧版
`PLUGIN_DATA` 分开，避免旧检出意外打开正式运行时的数据库。

当宿主提供 CODEX_PROJECT_ROOT 或 CODEX_WORKSPACE_ROOT 时，持久化根会在
`scoped-v1` 下按该项目根的 SHA-256 身份继续分域。没有项目根时，
CODEX_PROJECT_ID、CODEX_WORKSPACE_ID 或 CODEX_THREAD_ID 会提供非路径的
回退范围。原始项目路径或 identity 不会写入范围目录名。这能避免长生命周期
插件进程把一个项目的 workflow、index 或 receipt 投影到另一个项目。没有范围
的命令行调用为了兼容性仍使用未加后缀的根；宿主集成应始终提供项目或线程范围。

本机 DevKit 工作必须把 CODEX_TASK_TEMP 以及 TMPDIR/TEMP/TMP/
PYTHONPYCACHEPREFIX 子路径放在隔离的 G: 盘任务根。已配置的临时根会获得与持久化
数据相同的范围后缀。Hosted Windows CI 是明确例外：工作流必须先要求
RUNNER_TEMP，再把 CODEX_TASK_TEMP 及所有任务临时/缓存子路径派生到其下。
该宿主提供的临时根例外不构成 external host embedding 实证。运行时会拒绝
不安全、重叠、缺失或 reparse-point 根目录，不会把回退状态写入仓库。

宿主中断后，应从持久化工作流租约、端点、artifact 引用、快照和有界
receipt 恢复。继续前先重新绑定有效的当前上下文。不得从聊天记录、原始
日志或无关的新 start 重建权限。独立任务只有在证据、提交、集成和验收
全部完成后才可归档。

### 留存与清理边界

留存必须是宿主明确声明的策略，不能从成功结果或未经验证的结果推断。
合并之后，只有在策略规定的连续 `x` 轮后续 integration 均由协调器明确
接受、这些轮次没有发生 rollback、完成一次新的且仍然有效的 host 重检查，
并且持久化证据同时具备 candidate、base、integration commit 以及所需的
review、verification、integration receipt 时，宿主才可记录
`cleanup_candidate`。pending、仅观察到、推断得到或其他未经验证的结果，
都不算已接受的轮次。

`cleanup_candidate` 只是可清理候选资格证据，不是删除授权。任何组件都绝不
自动删除 worktree、cache、receipt、evidence 或用户数据。没有留存策略，或
任一门槛/证据缺失时，相关材料必须永久保留，直到另有明确且独立授权的清理
策略。

派生的项目索引快照使用独立的两阶段维护命令。先运行
`uv run python -m devkit_runtime.index_maintenance preview`，检查完整候选集与
保护集；确认后再把未变化的 identity 传给
`uv run python -m devkit_runtime.index_maintenance apply --preview-id <id>`。
Apply 会持有 Orchestrator 写围栏、重新核验跨数据库快照引用、为每个工作区保留
最新两代，并且每批最多删除 32 个候选。preview 过期、schema 缺失、锁失败或
引用无效都会零删除并关闭失败。Atlas 数据永远不属于索引清理目标。

本地 Windows 的 `C:/` 和所有非 `G:/` 临时根、worktree、cache、evidence
路径都禁止使用；可信 hosted Windows CI 仅可把宿主提供的 `RUNNER_TEMP`
作为明确例外，并在其下派生所有任务临时/缓存路径。该例外不构成外部宿主
embedding 或本地路径放宽的证据。

## 确定性 Fast Lane

Fast Lane 编译器位于
mcp-tools/devkit_fastlane/scripts/fastlane_routing.py 和
mcp-tools/devkit_fastlane/scripts/team_efficiency.py。公共 MCP 入口为
`fastlane_compile`；当前每一次调用都会刻意以 `NO_SAFE_WORK` 和零 assignments
被阻断。

- `ultra` 和 `--enable` 只选择被阻断结果的形状，不会激活调度。
- 公共编译器/CLI 不消费 host-status、账号用量、index evidence 或 worktree root。
- 它不会派发会话、创建 worktree、补位或运行命令；仓库内不存在这些动作的执行路径。
- 外部 Desktop-host bridge 未来可以提供经证明的项目权限和执行能力。
  这只是未来合同，不是已交付实现，也不是任何 Desktop host 源码已经存在的声明。

### 账号用量边界

账号用量不是 Fast Lane 的输入或路由机制。本发行版不含账号用量协调器、缓存或
外部采集器合同。

## 安全与范围边界

- Atlas 是本地确定性服务，暂时无法接入第三方源；不调用 LLM、向量库、网络
  服务、Shell 或补丁应用器。
- Relay 编译显式工作包并返回宿主动作，不伪造成功 spawn；真正的 Codex
  调度由宿主负责。
- 工作树、分支、租约、任务、快照、receipt 和证据身份均有绑定；过期、
  伪造、跨工作流或冲突输入会失败关闭。
- stdio stdout 只承载协议；诊断写入 stderr。
- 运行时数据、任务临时目录、工作树、缓存和验证证据均保持本地且有界。

## 验证

请从将要构建的 revision 运行以下发布验证：

    cd mcp-tools
    uv run --locked pytest -q
    uv lock --check
    uv run --locked ruff check devkit_atlas/service.py devkit_continuity devkit_runtime/atlas_acceptance.py orchestrator/service.py project_index/checkpoints.py project_index/service.py project_index/store.py
    uv run --locked python -m compileall -q devkit_atlas devkit_continuity devkit_runtime orchestrator project_index

CI 和全新产物检查才是当前测试计数的唯一来源。它们验证精确 17 工具清单、
空 prompt/resource 列表、协议干净的 stdout、正常和拒绝调用、缺失宿主能力时的
失败关闭，以及不依赖源 checkout 的独立启动。本 README 刻意不冻结会过期的回归计数。

## 版本

本仓库代表版本化的 v1.1.1 包。发布说明见
[CHANGELOG.md](CHANGELOG.md)；构建和安装请以已提交的 manifest、产物 allowlist
和锁定依赖为准。维护者从 current `main` 手动 dispatch Release；它通过全部 gates
后才创建注释 tag 并发布匹配的 GitHub Release。单独 push tag 不会触发发布。

## 许可证

[AGPL-3.0](LICENSE)。
