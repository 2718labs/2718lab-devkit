# 2718lab DevKit

<p align="center">
  <a href="https://github.com/2718labs/2718lab-devkit">
    <img
      src="https://socialify.git.ci/2718labs/2718lab-devkit/image?custom_description=Deterministic+project+intelligence+and+durable+engineering+orchestration&description=1&font=Inter&forks=1&issues=1&language=1&name=1&owner=1&pattern=Circuit+Board&pulls=1&stargazers=1&theme=Auto"
      alt="2718lab DevKit"
    />
  </a>
</p>

<p align="center">
  <a href="https://github.com/2718labs/2718lab-devkit/actions/workflows/ci.yml"><img src="https://github.com/2718labs/2718lab-devkit/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-0.2.0-informational.svg" alt="Version 0.2.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License: AGPL-3.0" /></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB.svg" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/hosts-Codex%20%7C%20Claude%20Code-6E56CF.svg" alt="Codex and Claude Code" />
</p>

`2718lab-devkit` 是面向 Codex 与 Claude Code 的工程基础设施插件。它把仓库理解、
任务执行和交付验证从一次性提示词，变成有项目事实、有状态、有恢复点的工程工作流。

- **确定性项目索引**：保存可复查的项目快照、来源窗口和影响关系。
- **耐久任务编排**：用 DAG、租约、写入范围和证据引用管理多步骤工作。
- **受控工程交付**：在写入前建立检查点，在完成前绑定输出和验证证据。

## 快速导航

- [为什么需要它](#为什么需要它)
- [工作闭环](#工作闭环)
- [核心能力](#核心能力)
- [内置命令](#内置命令)
- [MCP 工具分组](#mcp-工具分组)
- [共享 Agent 角色](#共享-agent-角色)
- [安装](#安装)
- [本地开发](#本地开发)

## 为什么需要它

复杂开发任务不只缺一段代码。它还缺少稳定的项目事实、明确的任务依赖、
可恢复的写入边界，以及能证明结果的验证记录。

2718lab DevKit 把这些约束放进同一套本地工具链：Skill 提供领域规范，
Agent 承担受限角色，`2718lab-tools` MCP 保存和校验项目索引、工作流状态、
检查点、审批记录与工程自检结果。

## 工作闭环

```text
同步项目快照
  → 查询有来源的项目事实
  → 注册任务、依赖与写入范围
  → 创建任务检查点
  → 执行受控修改
  → 绑定输出快照
  → 登记验证证据
  → 完成任务
```

严格任务使用 `strict_index=true` 固化输入、查询回执、检查点、输出和验证快照；
兼容任务可以保留 `strict_index=false`。这让项目理解与最终交付使用同一条
可追溯事实链，而不是各自依赖一段临时上下文。

任务卡可以在精确写入范围内声明尚不存在、准备新建的文件；只要路径仍安全地位于
工作区且注册时确实不存在，输入快照不会把它误判为 stale。已删除的索引文件或
注册前意外出现的计划路径仍返回 `INDEX_STALE`，不能靠扩大到整个目录绕过门禁。

## 核心能力

### 确定性项目智能

- `project_index_sync` 生成内容寻址的项目快照，并可绑定任务输出。
- `project_index_status` 返回新鲜度、覆盖范围和缺口，不把猜测伪装成索引事实。
- `project_index_query` 提供三种有界查询：
  - `lexical`：查找文本与声明；
  - `graph`：沿项目关系展开；
  - `impact`：反向查找受影响的依赖方。

查询结果带来源窗口、覆盖信息和 `trace_id`，可进入后续任务与验证记录。

### 耐久工作流编排

`workflow_create` 和 `workflow_register_task` 支持线性流程与 DAG。只有依赖已完成的
任务才能进入 ready wave；`workflow_claim` 通过 lease epoch、任务版本和写入范围
阻止过期执行者继续写入。

角色上下文、依赖关系和当前状态保存在 SQLite 中。点对点协作使用
`workflow_message_send`、`workflow_inbox` 与 `workflow_message_ack` 传递已登记的
artifact 引用；消息正文不经过协调器转发，宿主 target 也不会扩大任务权限。

### 检查点、恢复与审批

- `worktree_checkpoint_create/status/restore` 只处理任务拥有的写入范围；
- 已绑定输出的运行中任务按当前阶段快照恢复租约，不要求搬走合法输出；
- `workflow_approval_prepare/grant/deny/claim` 保存不可变、单次使用的审批记录；
- commit、push、PR 和 Release 是彼此独立的外部动作门。

MCP 不启动模型，也不替宿主执行 commit、push、PR 或网络发布。它会运行捆绑的
本地校验器，并在租约和写入范围约束下维护项目索引、检查点与任务状态。

### 可复用工程技能

| Skill | 用途 |
| --- | --- |
| [`astrbot-plugin-dev`](skills/astrbot-plugin-dev/SKILL.md) | AstrBot 插件结构、API、配置、验证与生态约束。 |
| [`mcp-server-dev`](skills/mcp-server-dev/SKILL.md) | Python MCP SDK、FastMCP 边界、模板与协议校验。 |
| [`python-engineering`](skills/python-engineering/SKILL.md) | uv、Ruff、Pyright、pytest 和 Python 工程骨架。 |
| [`oss-repo-ops`](skills/oss-repo-ops/SKILL.md) | README、许可证、CI、Release 与发布自检。 |
| [`work-methodology`](skills/work-methodology/SKILL.md) | 项目接地、任务拆分、受控编排与验证纪律。 |
| [`bugkiller`](skills/bugkiller/SKILL.md) | 建立在共享底座上的复杂缺陷处理工作流。 |

每个领域 Skill 都可以直接使用 `2718lab-tools` 和下方通用 Agent。领域 Skill
负责框架事实与验收标准，`work-methodology` 负责索引、编排、角色权限和交付证据。
Bugkiller 不再拥有一套私有 Agent 或审批底座。

## 内置命令

| 命令 | 用途 |
| --- | --- |
| [`2718lab-new-plugin`](commands/2718lab-new-plugin.md) | 创建或检查 AstrBot 插件。 |
| [`2718lab-new-mcp`](commands/2718lab-new-mcp.md) | 创建或检查 Python MCP 服务。 |
| [`2718lab-new-python`](commands/2718lab-new-python.md) | 建立 Python 工程骨架。 |
| [`2718lab-release-check`](commands/2718lab-release-check.md) | 执行发布前机械自检。 |
| [`2718lab-review`](commands/2718lab-review.md) | 按 2718lab 规范评审仓库。 |

## MCP 工具分组

当前 `2718lab-tools` 暴露 36 个工具，按职责分为：

| 组 | 工具 |
| --- | --- |
| 项目智能 | `project_index_sync`、`project_index_status`、`project_index_query` |
| 检查点 | `worktree_checkpoint_create`、`worktree_checkpoint_status`、`worktree_checkpoint_restore` |
| 工作流 | `workflow_create`、`workflow_register_task`、`workflow_ready`、`workflow_claim`、`workflow_complete`、`workflow_cancel`、`workflow_status`、`workflow_context`、`workflow_peers`、`workflow_endpoint_bind` |
| Artifact 与邮箱 | `workflow_artifact_register`、`workflow_artifact_resolve`、`workflow_message_send`、`workflow_inbox`、`workflow_message_ack` |
| 共享适配与审批 | `workflow_detect_adapters`、`workflow_approval_prepare`、`workflow_approval_grant`、`workflow_approval_deny`、`workflow_approval_claim` |
| Bugkiller 专用 | `bugkiller_route` |
| 兼容别名 | `bugkiller_detect_adapters`、`bugkiller_approval_prepare`、`bugkiller_approval_grant`、`bugkiller_approval_deny`、`bugkiller_approval_claim` |
| 工程校验 | `validate_astrbot_plugin`、`validate_mcp_server`、`check_python_project`、`check_release` |

## 共享 Agent 角色

| 角色 | 边界 |
| --- | --- |
| [`2718lab-triage`](agents/2718lab-triage.md) | 低成本只读分诊与证据整理。 |
| [`2718lab-investigator`](agents/2718lab-investigator.md) | 只读调查、接地与定位。 |
| [`2718lab-doc-writer`](agents/2718lab-doc-writer.md) | 仅写任务授权的文档范围。 |
| [`2718lab-verifier`](agents/2718lab-verifier.md) | 独立只读验证并登记证据。 |
| [`2718lab-code-writer`](agents/2718lab-code-writer.md) | 在明确写入范围内实现代码。 |
| [`2718lab-risk-reviewer`](agents/2718lab-risk-reviewer.md) | 危险用户审批后的只读分析。 |
| [`2718lab-redteam`](agents/2718lab-redteam.md) | 面向高风险交付的独立红队。 |

旧的 `bugkiller-*` Agent 名称只作为已有任务卡的短兼容别名保留；新任务统一使用
`2718lab-*` 角色。角色本身不绑定某个领域，任务卡同时指定所需的领域 Skill。

## 专门工作流：Bugkiller

Bugkiller 不是插件本体，而是共享底座上的复杂缺陷工作流。它调用同一组
`2718lab-*` 角色，并只追加缺陷状态机、风险触发器和缺陷证据规则。

简单缺陷可以走线性状态机；复杂任务按依赖进入 ready wave。严格卡的关键门禁是：

```text
project_index_sync
  → project_index_query
  → worktree_checkpoint_create
  → project_index_sync(bind_as="output")
  → project_index_query
  → workflow_artifact_register(kind="verification", snapshot_id=...)
  → workflow_complete
```

Bugkiller 无 WebUI，也不会自己启动模型。具体角色由 Codex 宿主创建和路由，
MCP 只维护可恢复的任务状态与受控本地能力。

## 安装

### Codex

在已经配置 `2718lab-devkit` 的团队 marketplace 后安装：

```powershell
codex plugin add 2718lab-devkit@<marketplace-name>
```

把 `<marketplace-name>` 替换为实际 marketplace 名称。本仓库尚不把任何私人
marketplace 声明成公共安装源。

安装后建议开启一个新 Codex 任务，确认 Skill、Agent 与 `2718lab-tools` 可发现。

### Claude Code

仓库根目录同时提供最小 marketplace，可从 GitHub 直接添加并安装：

```powershell
claude plugin marketplace add 2718labs/2718lab-devkit
claude plugin install 2718lab-devkit@2718lab-devkit
```

在活动会话中执行 `/reload-plugins` 使插件生效。插件更新后也应刷新或重新加载；
不要假定当前会话会自动热加载。

## 第一次使用

可以直接从工程底座能力开始：

```text
为当前仓库同步项目索引，查询这次改动的影响范围，并给出带来源的结论。

把这个多步骤工程需求注册为耐久 DAG，按任务写入范围和验证门禁执行。

按 2718lab 规范创建或评审一个 AstrBot 插件、Python 项目或 MCP 服务。
```

需要处理已经稳定复现的复杂缺陷时，再明确调用 Bugkiller 工作流。

## 运行与降级

`.mcp.json` 使用 PATH 中的 `python`，从插件根目录启动
`mcp-tools/server.py`。运行数据目录依次取：

1. `DEVKIT_HOME`；
2. `BUGKILLER_HOME`（旧版兼容）；
3. `PLUGIN_DATA`；
4. `CODEX_HOME/2718lab-devkit`；
5. `~/.codex/data/2718lab-devkit`。

开发源和插件缓存都不应作为运行数据目录。MCP 或宿主 peer 消息不可用时，
Skill 可以进入 `DEGRADED_SKILL_ONLY` 串行模式，但此时不承诺耐久编排、
并发写入或崩溃恢复。

## 本地开发

```powershell
uv sync --frozen
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

贡献边界和提交要求见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，安全问题请按
[`SECURITY.md`](SECURITY.md) 私下报告，版本变化见
[`CHANGELOG.md`](CHANGELOG.md)。参与社区协作即表示同意遵守
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。

## 致谢

仓库首页的展示结构参考了
[`DBJD-CR/astrbot_plugin_helloworld`](https://github.com/DBJD-CR/astrbot_plugin_helloworld)；
DevKit 保留自己的工程边界、质量门禁和发布流程，没有复制该模板的 AstrBot
业务文件与自动发布工作流。

## 许可证

[AGPL-3.0](LICENSE)。
