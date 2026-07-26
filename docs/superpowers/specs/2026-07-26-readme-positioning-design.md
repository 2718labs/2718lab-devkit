# 2718lab DevKit README 与产品定位设计

## 目标与受众

README 面向第一次接触 `2718lab-devkit` 的技术用户。读者应在首屏回答四个问题：

1. 这是什么；
2. 它解决什么工程问题；
3. 它由哪些可验证能力组成；
4. 安装后先做什么。

产品定位是 **Codex 工程基础设施插件**：把确定性项目理解、耐久任务编排、
受控写入与恢复、工程规范和交付验证组合成可复现的开发工作流。
Bugkiller 是使用这套基础设施处理复杂缺陷的一种专门工作流，不是插件本体。

## 结构方案

### 方案 A：平台能力优先（采用）

先解释统一工程闭环，再按项目智能、编排、安全执行、工程技能四组展示能力，
最后把 Bugkiller 放入“专门工作流”。

优点是定位准确，新用户能先理解整体价值；缺点是需要把 31 个 MCP 工具按能力组
归纳，不能逐个堆在首屏。

### 方案 B：工作流旅程优先（不采用）

按“理解仓库 → 规划 → 执行 → 验证 → 交付”写完整旅程。

优点是容易顺着读；缺点是会弱化 Skill、MCP、Agent 三种交付形态，
也容易让人误以为插件只有一种固定流程。

### 方案 C：组件目录优先（不采用）

先列 Skills、Commands、Agents 和 MCP tools，再逐项解释。

优点是查阅方便；缺点是与当前 README 的问题相同——组件很多，但用户看不出
为什么需要它们、它们怎样共同工作。

## 首屏设计

首屏按以下顺序组织：

1. `2718lab DevKit` 标题；
2. 一句产品定义，不以 Bugkiller 或“修 bug”开头；
3. 三条价值摘要：确定性项目理解、耐久工程编排、可验证安全交付；
4. 仅使用可验证徽章。没有公开 CI、Release 或 marketplace 页面时不放假链接。

建议产品定义：

> 面向 Codex 的工程基础设施插件，用确定性项目索引、耐久任务编排和可复用工程
> 规范，把仓库理解、受控执行与验证交付连成一条可恢复的工作流。

## 信息架构

### 1. 为什么需要它

用一小段说明普通提示词无法持久保存项目事实、任务依赖、租约、检查点和验证证据。
不使用“万能”“自动解决一切”等不可验证宣传。

### 2. 核心能力

| 能力组 | README 要表达的事实 | 实际依据 |
| --- | --- | --- |
| 项目智能 | 确定性快照、覆盖状态、词法/图/影响查询、来源窗口 | `project_index_sync/status/query` |
| 耐久编排 | 线性或 DAG 工作流、任务依赖、租约 fencing、角色上下文、耐久邮箱 | `workflow_*` 工具 |
| 安全执行 | 任务写入范围、checkpoint/CAS、当前阶段快照恢复、单次审批记录 | `worktree_checkpoint_*`、`bugkiller_approval_*` |
| 工程技能 | AstrBot、MCP、Python、开源仓库和工作方法的规范、模板与校验器 | 六个 `skills/*` 目录与五条命令 |

README 不逐个解释 31 个工具。工具参考按上述四组列出名称，
详细约束链接到对应 Skill 或现有文档。

### 3. 工作方式

用一条短流程说明共享底座：

`同步项目快照 → 查询有界事实 → 注册任务与写入范围 → 建立检查点 → 执行 →
绑定输出快照 → 登记验证证据 → 完成`

同时说明：

- 非严格旧任务仍可使用 `strict_index=false`；
- MCP 不启动模型，也不替宿主执行 commit、push、PR 或网络发布；它会运行
  捆绑校验器，并在租约和写入范围约束下维护本地索引、检查点与任务状态；
- commit、push、PR 和 Release 始终是独立授权门。

### 4. 专门工作流

Bugkiller 只在这里出现。说明它为复杂缺陷处理提供分诊、调查、代码写入、
验证和危险升级角色，并复用项目索引、编排、检查点和审批底座。
不在标题、一句话定义或安装段落中把插件称为“修 bug 插件”。

### 5. 内置能力索引

保留简洁的 Skill 表，并补充 Commands、Agents 与 MCP 工具分组。
每一项必须能映射到仓库中的真实文件或运行时列出的工具名。

### 6. 安装与第一次使用

分成两种明确场景：

- 团队 marketplace 安装：展示
  `codex plugin add 2718lab-devkit@<marketplace-name>`，并在同一段明确
  `<marketplace-name>` 由使用者替换；不再把任何私人 marketplace 名
  写成公共 marketplace；
- 仓库开发：`uv sync --frozen`，再运行 README 中列出的质量门禁。

安装后给三个从底座能力出发的示例：

1. 为当前仓库建立项目索引并查询受影响范围；
2. 用耐久 DAG 拆分一个多步骤工程任务；
3. 按 2718lab 规范创建或评审 AstrBot、MCP、Python 项目。

Bugkiller 示例放在专门工作流段落，不作为默认首个示例。

### 7. 安全模型、开发与许可证

保留现有审批边界和降级模式，但压缩宿主实现细节。开发段链接
`CONTRIBUTING.md`，列出 `uv lock --check`、Ruff、Pyright、pytest。
许可证继续使用 AGPL-3.0。

## 元数据同步范围

README 改写时同步校正以下定位文案，避免安装界面仍把插件描述成 Bugkiller：

- `.codex-plugin/plugin.json` 的 description、shortDescription、
  longDescription 和 defaultPrompt；
- `.claude-plugin/plugin.json` 的 description；
- `pyproject.toml` 的 description；
- `CHANGELOG.md` 的 Unreleased 说明；
- 对应元数据测试。

不修改插件版本、MCP 启动命令、工具 schema、角色权限、运行时逻辑或发布状态。

## 事实与写作规则

- 中文为主，命令、工具名和协议名保留英文；
- 先讲用户价值，再给组件目录；
- 只写本地源码、运行时 schema 和测试能证明的能力；
- 不声称已公开发布，不添加不存在的截图、Release、CI 或 marketplace 链接；
- 不写本机绝对路径、私人 marketplace 名或带时间戳的缓存路径；
- 不复述完整内部调度规则，README 只保留理解和安全使用所需边界；
- 链接必须指向仓库中真实存在的相对路径。

## 验收标准

1. README 前十五行把产品定义为 Codex 工程基础设施，而非 bug 修复插件；
2. 项目索引、编排和安全执行出现在 Bugkiller 之前；
3. 六个 Skill、五条命令和四组 MCP 能力均可追溯到真实实现；
4. 安装说明不含私人 marketplace 名或虚构公共发布状态；
5. README、Codex/Claude 清单和 pyproject 定位一致；
6. Bugkiller 仍被准确介绍，但明确是共享底座上的专门工作流；
7. JSON、TOML、YAML 解析通过，MCP schema 保持 31 个工具及三种查询模式；
8. Ruff、Pyright、完整 pytest 与四项仓库自检通过；
9. 本轮只创建本地提交，不配置 remote，不 tag、不 push。

## 回滚边界

设计文档独立提交。README 与元数据实现放在后续独立提交中；
如定位不符合预期，可回退实现提交而不影响已经恢复的核心运行时基线。
