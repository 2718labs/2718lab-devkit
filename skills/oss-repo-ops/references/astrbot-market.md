# AstrBot 插件市场提交 —— 完整流程与排错

本文件是 SKILL.md 第 4 步的完整版。所有事实来自 `docs.astrbot.app/dev/star/plugin-publish.html`、`docs.astrbot.app/dev/star/plugin-new.html` 以及对 `AstrBotDevs/AstrBot_Plugins_Collection`、`AstrBotDevs/AstrBot` 两个仓库的实际观察。**本文件未覆盖的细节(审核 SLA、下架申诉流程等)一律以官方文档为准,不得脑补。**

## 1. 两个仓库的角色分工(不要混淆)

- `AstrBotDevs/AstrBot_Plugins_Collection` —— **只是索引/注册表**,不托管插件代码。它维护机器可读的插件目录 `plugin_cache_original.json`(仓库 `main` 分支根目录);AstrBot 客户端经 `https://api.soulter.top/astrbot/plugins` 拉取(GitHub raw 作为回退)。**注意别踩坑**:`plugins.json` 只是 AstrBot 端本地缓存的文件名,不是这个仓库对外发布的索引 URL——历史上写成 `soulter.github.io/.../plugins.json` 会 404。这个仓库**不接受插件代码的 PR**。
- `AstrBotDevs/AstrBot` —— 主仓库,插件市场提交生成的 Issue **实际落在这里**(例如 issue #2984)。

**机制迁移说明**:早期的社区实践(例如 `AstrBot_Plugins_Collection` issue #875 "[Plugin] astrbot_plugin_nte")是直接向注册表仓库提 Issue;当前官方文档与较新的真实提交(例如 `AstrBot` 主仓库 issue #2984)显示流程已经迁移到主仓库的 Issue 列表。**不要凭这段历史自己判断该往哪个仓库提 Issue** —— 永远走下面第 2 节的 UI 流程,UI 生成的预填 Issue 跳转到哪个仓库,就是当前正确的目标仓库。

## 2. 完整提交步骤

1. 插件作者把插件开发成一个正常的公开 GitHub 仓库(遵循 `astrbot-plugin-dev` 的目录规范,`astrbot_plugin_` 前缀)。
2. 打开 `plugins.astrbot.app`。
3. 点击页面右下角的 `+` 按钮。
4. 在弹出的表单里填写:
   - 插件基本信息:name、desc、tags
   - 作者信息:author、social_link
   - 仓库地址:repo(GitHub 仓库 URL,不带 `.git` 后缀)
5. 点击"提交到 GITHUB"按钮——这会以表单内容为参数生成一个**预填好的 JSON 模板 Issue body**,并把浏览器跳转到对应仓库的"创建 Issue"页面。
6. 提交者需要在 Issue 表单里勾选三个强制承诺项:
   - 插件已经过充分测试
   - 不包含恶意代码
   - 遵守 GitHub 社区行为准则
7. 确认无误后点击 **Create** 提交 Issue。
8. 仓库配置的 CI/CD 流水线会自动解析这个 Issue 的结构化内容,校验通过后把插件信息索引进市场目录(`plugin_cache_original.json`)。**这是 Issue 触发的自动索引流程,不是人工审核后由维护者手动提 PR 合并的流程**,也不是纯爬虫式的自动发现——必须走这个 Issue 提交动作触发。

## 3. 16MB 包体限制与瘦身手段

市场对插件打包体积有硬性限制:**zip ≤ 16MB**,超出会被 CI/CD 自动拒绝(不是人工审核拒绝)。

瘦身手段(按优先级):

1. 压缩体积较大的图片/音频等二进制资源。
2. 打包时剥离 `.git`、`__pycache__`、`node_modules`、本地开发配置目录等不需要随包分发的内容。
3. 用 `.gitattributes` 控制哪些文件参与打包/导出。
4. 维护一个专门用于发布的"瘦身 release 分支"——只包含运行时必需文件,开发用的测试数据、文档草稿等留在主分支不合并进去。
5. 如果插件确实有正当理由超过 16MB(例如内置了必要的大型模型/词典文件),**联系维护者手动处理**,不要试图绕过自动校验。

## 4. 仓库/目录命名与 metadata.yaml 发布相关字段

- 仓库/目录名:**全小写、不含空格与连字符、尽量简短**——它会成为插件加载的模块名,这几条是硬约束(大写/连字符/空格会导致无法作为模块导入)。官方文档**推荐**(非强制)以 `astrbot_plugin_` 为前缀,团队约定遵守。这同时是 `metadata.yaml` 里 `name` 字段的取值,市场要求与目录名相等。
- 官方起始模板:`Soulter/helloworld`,在 GitHub 上点 "Use this template" 生成新仓库后改名。
- `metadata.yaml` 与发布直接相关的字段(完整字段表见 `astrbot-plugin-dev` skill 的 api-reference,这里只列上架会踩的坑):
  - `version`:官方模板用 `v` 前缀(如 `v1.0.0`),**推荐但非强制**——市场同样接受纯 3 段号(如 `1.0.0`,本身已是字符串,有实际在架插件这么写)。真正的坑是 **2 段号**:裸 `1.0` 会被 YAML 解析成浮点数 `1.0`(非字符串),市场校验失败。规避:一律 3 段式 `X.Y.Z`(可选 `v` 前缀),或给值加引号。团队建议带 `v`,便于 git tag / Release / metadata 三处对齐。
  - `repo`:**建议不带 `.git` 后缀**(官方示例一律不带,最稳妥)。市场 URL schema 据称也接受 `.git` 形式,所以这是强约定、而非已证实的硬性失败点——拿不准就不带。
  - `astrbot_version`:必须加引号(字符串),PEP 440 风格区间(如 `">=4.16,<5"`),**不带 `v` 前缀**——这一点和 `version` 字段的要求正好相反,容易搞混。
  - `name`:必须等于目录名,合法模块名(小写字母/数字/下划线,不能有连字符/大写/空格)。

## 5. 上架后的更新机制

插件上架后,`metadata.yaml` 里的 `version` 每次变化都应该对应一次新的 GitHub Release 与 git tag(见 `release-workflow.md`)。AstrBot 客户端的插件更新检测依赖 `repo` 字段能正确解析(建议不带 `.git`),市场索引(`plugin_cache_original.json`)的刷新时机由官方 CI 决定,具体刷新频率未在本 skill 的 grounding 范围内确认——不确定时以 `docs.astrbot.app` 为准。

## 6. 常见被拒/索引失败原因(可推断的机械项)

- zip 超过 16MB(见第 3 节)。
- `metadata.yaml` 缺失,或 `version`/`astrbot_version` 类型不对(浮点数而非字符串、缺引号)。
- `repo` 带 `.git` 后缀,或指向非公开仓库。
- `name`/目录名含非法字符(连字符、大写字母、空格——无法作为模块加载)。(缺 `astrbot_plugin_` 前缀是不推荐,但未必是硬性拒因。)
- Issue 提交时未勾选三个强制承诺项。
- 仓库缺少 `LICENSE`,部分审核可能会要求明确授权信息(具体是否强制未在本 skill 的 grounding 范围内确认,建议无论如何都补齐,见 `repo-hygiene.md`)。

超出以上机械项的具体审核细节(人工复核时长、失败后如何申诉、下架流程)不在本 skill 的 grounding 范围内,一律引导用户查 `docs.astrbot.app/dev/star/plugin-publish.html`,不要编造。

## 参考来源

- 发布插件到插件市场 | AstrBot —— docs.astrbot.app/dev/star/plugin-publish.html
- AstrBot 插件开发指南 | AstrBot —— docs.astrbot.app/dev/star/plugin-new.html
- AstrBot 插件市场 —— plugins.astrbot.app
- GitHub - AstrBotDevs/AstrBot_Plugins_Collection
- GitHub - AstrBotDevs/AstrBot
- GitHub - Soulter/helloworld(插件模板)
- [Plugin] astrbot_plugin_nte · Issue #875 · AstrBotDevs/AstrBot_Plugins_Collection(旧流程实例)
- [Plugin] astrbot-plugin-tmp-bot · Issue #2984 · AstrBotDevs/AstrBot(新流程实例)
- 内部参考:`astrbot-plugin-dev` skill 的 `references/guidelines.md`(团队自己的插件发布记录,与以上一致)
