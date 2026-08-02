# 接地纪律(不凭记忆写 API)

本文件配合 `FASTLANE_CONTRACT.md` 使用。核心原则:**训练记忆不是可信来源**,尤其是框架/库的具体签名、参数名、版本行为——记忆会过期、会和相似框架混淆、会自信地编造不存在的参数。

## 1. 查证优先级表

写任何"我不能 100% 确定"的 API 前,按此顺序查,查到即止,不要跳级:

| 优先级 | 来源 | 什么时候用 |
|---|---|---|
| 1 | 本地源码(项目里能直接读到的依赖代码、`.pyi`、已导入模块的 `__init__.py`) | 项目本身依赖了这个库时优先看它实际装的版本 |
| 2 | Sibling skill 的 `references/*.md` | 任务落在 2718lab 已有领域 skill 范围内时(见第 3 节路由表) |
| 3 | 官方文档(有联网能力时现查) | 本地查不到、也没有对应 sibling skill 时 |
| 4 | 搜索(issue/讨论区/changelog) | 官方文档没写清楚的边界行为、版本差异 |

**禁止**用"训练记忆兜底"作为以上四级都查不到时的退路——查不到就用保守的、能在文档里找到确证的写法,并在交付说明里注明"此处待用户核实:<具体是什么不确定>"。绝不能编造一个听起来合理的参数名交上去。

## 2. 触发词表:什么信号说明"我在凭记忆写"

出现下列任一信号,立刻停下查证,不要继续往下写:

- 写 `import` 语句时犹豫"这个包是不是叫这个名字"
- 参数名/参数顺序是"大概是这样"、"应该是叫 xxx"式的猜测
- 心里念叨"这个版本好像行为不一样,但记不清具体差异"
- 想抄一个印象里"很像"的相邻框架的写法(例如把 NoneBot 的写法套到 AstrBot 上)
- 写完一段后自己想"编译器/解释器会告诉我错在哪",准备先跑再说

## 3. 路由表:领域 → 该读的 sibling skill

| 领域 | 读哪个 skill | 具体文件 |
|---|---|---|
| AstrBot 插件开发 | `astrbot-plugin-dev` | `references/api-reference.md`(API 签名)、`references/guidelines.md`(团队规范) |
| MCP server 开发 | `mcp-server-dev` | `references/api-fastmcp-sdk.md`、`references/api-fastmcp-standalone.md`、`references/packaging-and-integration.md` |
| 通用 Python 工程(代码质量/测试/打包) | `python-engineering` | `references/guidelines.md`(团队代码质量守则)、`references/toolchain-commands.md`(uv/ruff/pyright/pytest 命令速查)、`references/pyproject-reference.md`(打包与 [tool.*] 配置键) |
| 开源仓库运营(许可证/版本/Release/CI/插件市场提交) | `oss-repo-ops` | `references/astrbot-market.md`(插件市场提交)、`references/release-workflow.md`(版本/tag/Release/CI)、`references/repo-hygiene.md`(README/LICENSE/CHANGELOG 卫生) |

路由表本身会过期(sibling runtime 增删文件),不确定时先读 `FASTLANE_CONTRACT.md` 顶部配套文件说明,以那里的实际列表为准。

## 4. Progressive disclosure 纪律

先读 `FASTLANE_CONTRACT.md`,按它给的"何时读"提示决定要不要打开某个 `references/*.md`,**禁止一次性把所有 references 全读一遍**——大部分任务只需要命中一两份文件,全读是浪费上下文且稀释重点。契约是索引,references 是按需展开的详情。
