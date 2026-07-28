# 仓库卫生详版:README / LICENSE / CHANGELOG / 模板设计理由

本文件是 SKILL.md 第 2 步的完整论证版。

## 1. LICENSE:默认 AGPL-3.0(MIT 为破例)完整论证

**免责声明放最前面:以下是工程判断,不是法律意见。真正拿不准某个具体插件是否构成 AstrBot 的"派生作品"、或某个许可证选择的法律后果时,去找真正的法律顾问,不要依赖本文件下结论。**

### 1.1 背景事实

AstrBot 本体(`AstrBotDevs/AstrBot`)采用 **AGPL-3.0** 许可证。AGPL 相比普通 GPL 多了一条关键条款:它的强 copyleft 义务由"**通过网络提供服务**"触发,而不只是"分发二进制/源码"触发——也就是说,即使你只是把修改过的 AGPL 代码跑在服务器上给别人用(用户从来没有拿到过你的代码副本),你也有义务公开你的修改。这正是 AstrBot 这类"作为服务运行的机器人框架"选择 AGPL 而非普通 GPL 的典型动机:防止有人魔改了核心却只私下运营、不回馈代码。

### 1.2 团队默认:AGPL-3.0

团队默认许可证是 **AGPL-3.0**,与 AstrBot 本体保持一致。理由:①与上游生态同调,避免许可证碎片化;②工作室希望自己的修改在被网络服务化使用时也能回馈社区,而 AGPL 的网络 copyleft 正是为此设计;③省去逐个插件判定"是否构成派生作品"的心智负担——统一 AGPL-3.0 永远落在安全的一侧。

需要说清的法律事实(避免把工程立场当成法律强制):通过 `astrbot.api.*` 这层公开 API 调用宿主的普通插件,并没有直接复制、修改 AstrBot 核心源码,在"插件只是调用宿主提供的 API、不包含宿主自身代码"的情形下,插件本身通常被视为独立作品,作者**在法律上本可自由选证**,社区里确有插件用宽松许可证。但团队把 AGPL-3.0 定为**默认立场**,而不是把 MIT 定为默认——这是工作室的主动政策选择,不是法律强制。

### 1.3 什么时候 AGPL-3.0 是强制(连破例都不允许)

如果某个插件的实现方式是:**vendor(内嵌拷贝)了 AstrBot 核心的非平凡代码**,或者本质上是 AstrBot 核心某模块的派生/移植(而不是通过公开 API 调用),那么该插件可能被认定为 AstrBot 的派生作品,此时 AGPL-3.0 不只是默认、而是**必须**,不能破例改宽松证。真实案例:社区插件 `NickCharlie/astrbot_plugin_self_learning` 在 README 中给出的原话式理由是——该插件"incorporates and derives from AstrBot(also AGPL-3.0)... modifications or derivative works must be distributed under the same license"。这说明确实存在插件因为足够深度地嵌入/派生了核心代码,而必须跟随 AGPL 的先例,不是本 skill 编造的规则。

### 1.4 什么时候可以破例用 MIT

默认 AGPL-3.0;**仅当同时满足**下列条件、且工作室明确想让某个作品被最大化嵌入复用时,才考虑破例改用 MIT:

1. 该插件是直接复制/内嵌了 AstrBot 核心仓库非平凡源码吗?——必须为**否**(纯 `import astrbot.api.xxx` 调用)。
2. 该插件是把 AstrBot 某核心模块整体移植/重写后包装成插件吗?——必须为**否**。
3. 把它抽离出 AstrBot 进程后还有独立意义吗?——必须为**是**(是独立作品,不是宿主内部逻辑的延伸)。

只要 1/2 任一为"是"、或 3 为"否" → 留在 **AGPL-3.0**(见 1.3,此时可能是强制)。三条全部满足、且工作室有意让它宽松复用 → 才可破例选 **MIT**。**结论仍然只是工程建议,具体许可证选择及其法律后果由仓库所有者自行决定并对此负责。**

### 1.5 LICENSE 全文来源

LICENSE 文件的完整法律文本**只能从 `assets/templates/LICENSE-AGPL-3.0`(默认)或 `assets/templates/LICENSE-MIT`(破例时)复制**,禁止凭记忆手打或改写措辞——许可证条款的准确性直接影响法律效力,任何"大概意思对"的复述都不可接受。

## 2. README 逐段写作指引

按以下顺序组织,每段的目的写在括号里:

1. **标题 + 一句话描述**(让读者 3 秒内知道这是什么)。
2. **徽章行**(badges):至少包含版本号徽章、许可证徽章、AstrBot 兼容版本徽章。徽章示例(Shields.io 风格,占位符按实际仓库替换;注意标签里的字面连字符要写成 `--`):
   ```markdown
   ![version](https://img.shields.io/badge/version-v1.0.0-blue)
   ![license](https://img.shields.io/badge/license-AGPL--3.0-blue)
   ![astrbot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange)
   ```
3. **功能/指令列表**(每个 `@filter.command` 对应一行:指令名 + 一句话说明,方便用户直接照着用)。
4. **配置表**:必须与插件的 `_conf_schema.json` 完全对应——每一个配置项一行,列出字段名、类型、默认值、说明。这张表和 schema 文件不同步是最常见的文档腐化来源,评审时要逐项对照。
5. **安装方式**:说明把插件目录放进 `AstrBot/data/plugins/` 下(团队规范里 AstrBot 数据目录的标准位置),然后在 WebUI 里重载插件。
6. **截图/GIF**(如果插件有可见的交互效果或 UI 反馈,强烈建议放,能显著降低用户的使用门槛)。
7. **许可证脚注**:文末一行注明协议类型并链接到 `LICENSE` 文件。

## 3. 徽章示例补充

除版本/许可证/兼容性徽章外,视情况可加:

- CI 状态徽章(指向 `.github/workflows/` 里的 CI 跑测结果)。
- GitHub Release 徽章(自动显示最新 tag)。

不要滥用徽章墙——3~5 个能提供实际信息的徽章即可,堆砌纯装饰性徽章不利于阅读。

## 4. Issue/PR 模板设计理由

### 4.1 为什么 Issue 模板必须要求填 AstrBot 版本 + 平台适配器

AstrBot 支持多个 IM 平台适配器(QQ、Telegram、微信、Discord 等),很多 bug 的根因是**某个特定适配器的消息格式/API 行为差异**,而不是插件逻辑本身的问题。如果 Issue 里不强制要求填这两项,维护者经常需要来回追问才能定位问题所在的平台,浪费双方时间。所以 `bug_report` 模板必须把这两个字段设为必填项。

### 4.2 为什么 PR 模板要有三项勾选

- **本地 `data/plugins/` 下实测过**:插件是运行时动态加载的,静态类型检查/语法通过不代表在真实 AstrBot 进程里能正常初始化、响应指令、正确 terminate。
- **`_conf_schema.json` 校验过**:schema 写错会导致插件直接加载失败(框架层面抛异常),必须在提交前用 JSON 解析器验证过一次。
- **无其他框架 API 混入**:这是 `astrbot-plugin-dev` skill 反复强调的高频错误(误用 `nonebot`/`koishi`/`telebot` 等其他框架的 API),PR 阶段的 checklist 是最后一道防线。

具体模板文件见 `assets/templates/ISSUE_TEMPLATE/` 与 `assets/templates/PULL_REQUEST_TEMPLATE.md`。
