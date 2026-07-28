---
name: astrbot-plugin-dev
description: 团队 AstrBot 插件开发规范与 API 参考(基线 AstrBot v4.26.5)。凡是涉及 AstrBot 的任务都必须使用本 skill——新建/编写/修改/评审/调试/发布 AstrBot 插件,提到 astrbot_plugin、Star 插件类、metadata.yaml、_conf_schema.json、插件 WebUI Pages/Web API(astrbot.api.web / FastAPI)、LLM 函数工具、插件市场提交,或基于 AstrBot 开发 QQ/微信/Telegram 等聊天机器人功能时。新建插件直接复制 assets/templates/ 起步套件(以官方 DBJD-CR/astrbot_plugin_helloworld 模板为底、经团队加固)。即使只是写一小段 AstrBot 插件代码片段也要先查本 skill,框架有大量反直觉的坑(如 __del__ 会屏蔽 terminate、协程 return 字符串不发消息、双 @filter.command 永不触发、v4.26 起 Web API 不再是 Quart)。Use this skill whenever the user mentions AstrBot, astrbot plugins, or IM bot plugins built on the AstrBot framework.
---

# AstrBot 插件开发(团队规范版)

为团队开发 AstrBot 插件时,严格按本文件执行。**不要凭记忆写 AstrBot API**——AstrBot 的接口与 NoneBot/Koishi 等框架完全不同且迭代快,记忆极易出错;本文件和参考文件里的写法才是准确的。

配套文件:

- `references/api-reference.md` — 全部 API 的确切用法(装饰器/事件钩子/消息组件/LLM 工具/配置/存储/会话控制/主动消息)与版本门槛。**写任何本文件未覆盖的 API 前,必须先读它对应章节**。
- `references/guidelines.md` — 完整团队规范守则(条款编号)。评审、发布、维护时对照。
- `assets/templates/` — 可直接复制的起步全套(以官方 DBJD-CR/astrbot_plugin_helloworld 模板为底、团队加固;含 .github 工作流/issue 模板/LICENSE/CONTRIBUTING)。
- `scripts/validate_plugin.py` — 交付前自检脚本,**必须运行**(见第 5 步)。

## 第 0 条:框架保真(最容易被弱模型违反)

1. 只允许从 `astrbot.api.*` 导入框架能力。标准导入块(直接复制,不要自创):

```python
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp
```

2. **严禁**出现其他 bot 框架的任何 API:`nonebot`、`koishi`、`graia`、`botpy`、`khl`、`telebot`、`discord.py`、`on_command`、`CommandSession`、`Matcher` 等统统不属于 AstrBot。见到这些词就是写错了。
3. `requirements.txt` 只写插件真实 import 的第三方库;**禁止**写 `astrbot` 本身(插件运行在 AstrBot 进程内);禁止用 `==` 钉死 aiohttp/pydantic/openai 等与 AstrBot 核心重叠的包(会触发依赖保护直接装不上),用 `>=` 或不写版本。
4. 不确定某个 API 是否存在、参数怎么传:查 `references/api-reference.md`。查不到就不要用,改用文档里确认存在的写法。

## 工作流程(按顺序执行)

### 第 1 步:判断任务类型

| 任务 | 做法 |
|---|---|
| 新建插件 | 复制 `assets/templates/` 全套文件 → 改占位符 → 实现功能 |
| 写/改功能 | 读 api-reference 对应章节 → 照抄其代码样式改写 |
| 代码评审 | 对照下方「硬性规则」+ guidelines.md 逐条检查,输出带条款号的问题清单 |
| 发布/过审 | guidelines.md 第 11 章 + 附录 D 清单 |

### 第 2 步:文件结构(每个文件长这样,不要偏离)

```text
astrbot_plugin_<名字>/          # 目录名=metadata.name,全小写下划线
├── main.py                    # 唯一的 Star 子类在这里
├── metadata.yaml              # 必须
├── requirements.txt           # 有第三方依赖才要
├── _conf_schema.json          # 有用户配置项才要
├── README.md
├── LICENSE                    # * 开源发布用,团队默认 AGPL-3.0(见 assets/templates/LICENSE)
├── .github/…                  # * workflows/ISSUE_TEMPLATE/PR 模板,起步套件已含(见 assets/templates/.github)
└── core/…                     # 复杂业务逻辑放子模块(普通类/函数,不放 Star 子类)
```

`metadata.yaml` 逐字段照抄这个格式(注意注释里的格式陷阱):

```yaml
name: astrbot_plugin_example        # 必须是合法 Python 标识符:只用小写字母/数字/下划线,禁止连字符和空格
display_name: 示例插件
desc: 一句话描述插件功能。           # 必填
version: v1.0.0                     # 必须带 v 前缀!写裸 1.0 会被 YAML 解析成数字导致过不了市场审核
author: YourTeamName                # 必填
repo: https://github.com/yourteam/astrbot_plugin_example   # 必填(不带 .git 后缀),否则用户无法更新
astrbot_version: ">=4.16,<5"        # 必填,带引号,不带 v 前缀;用了新 API 要抬高下界(门槛表见 api-reference 第17节)
```

`_conf_schema.json` 是**严格 JSON**:双引号、无注释、无尾逗号。type 只能是这 9 个之一
`string / text / int / float / bool / object / list / template_list / file`(**无 `dict`**,嵌套映射用 `object`+`items`),
每项都要有 `description` 和 `default`。写错 type = 插件加载失败(框架直接 raise TypeError)。

### 第 3 步:main.py 标准骨架(直接以此为底稿)

```python
"""astrbot_plugin_example - 一句话描述。"""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


class ExamplePlugin(Star):                       # 全插件唯一的 Star 子类,只能在 main.py
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config                     # 有 _conf_schema.json 时框架自动注入
        self._session = None                     # aiohttp 会话等资源先置 None
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self):                  # 耗时初始化放这里,不放 __init__
        self.data_dir = StarTools.get_data_dir() # 持久化数据只能写这个目录
        # 需要网络时: import aiohttp; self._session = aiohttp.ClientSession(
        #     timeout=aiohttp.ClientTimeout(total=30))

    @filter.command("example", alias={"示例"})    # 多个名字用 alias,禁止叠两个 @filter.command
    async def example_cmd(self, event: AstrMessageEvent):
        """指令说明(会展示给用户,必须写)。"""
        try:
            yield event.plain_result("Hello!")   # 发消息只能 yield;return "字符串" 发不出去
        except Exception:
            logger.exception("example 执行失败")  # 留栈
            yield event.plain_result("出了点问题,请稍后再试~")  # 用户看到友好文案

    async def terminate(self):                   # 必须直接定义在本类;绝对不要定义 __del__
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
```

### 第 4 步:实现功能时的硬性规则

**结构**
1. 目录名 = `metadata.name`,合法标识符,`astrbot_plugin_` 前缀。
2. 入口 `main.py`,唯一 Star 子类;业务逻辑放 `core/` 子模块。
3. 指令名避免通用词冲突(跨插件同名指令会静默共存),通用词加前缀或用指令组。

**生命周期**
4. 禁止 `__del__`(框架逻辑是 `if __del__ ... elif terminate`,有它 terminate 永不执行)。
5. `terminate` 直接定义在主类(框架不查继承链),清理所有自建资源:任务、session、句柄、回调。
6. `initialize`/`terminate` 幂等(禁用→启用→重载→更新会反复经历生命周期)。

**Handler**
7. handler 是类内 `async def`,前两参数 `self, event`;发消息用 `yield event.plain_result(...)`;钩子(on_llm_request 等)内不能 yield,用 `await event.send(...)`。
8. 多 filter 叠加是 AND;别名用 `alias={...}`;监听全量消息必须 `@filter.event_message_type(filter.EventMessageType.ALL)`(无 filter 的函数被框架跳过)。
9. handler 内 try/except + `logger.exception` + 友好回复(未捕获异常的原始报错会直接发给用户)。
10. `@filter.llm_tool` 的参数 Schema 完全由 docstring `Args:` 段生成,格式 `参数名(string): 描述`,类型限 string/number/object/boolean/array;不写 = 参数静默丢失。
11. 危险/管理操作加 `@filter.permission_type(filter.PermissionType.ADMIN)`,必要时用 session_waiter 二次确认。

**数据与网络**
12. 持久化只写 `StarTools.get_data_dir()`;禁止写插件目录(更新即全删)。
13. 配置走 `_conf_schema.json`;密钥只能来自配置,禁止硬编码。
14. 网络用 aiohttp/httpx + 超时;禁 requests 与事件循环内阻塞调用(阻塞的包 `asyncio.to_thread`);后台任务全部收进 `self._tasks` 并在 terminate 取消。
15. 日志只用 `from astrbot.api import logger`,禁 print。
16. **Web API**:插件 Web API handler 用 `astrbot.api.web` 的 `request`/`json_response` 等,禁止 `quart`/`jsonify`(v4.26.0 起后端为 FastAPI);用了就把 `astrbot_version` 下界抬到 `>=4.26`。
17. **KV 生命周期**:跨"卸载→重装"要保留的数据写 `data_dir` 文件,KV 在插件卸载时会被清空(v4.26.2+)。

### 第 5 步:交付前自检(必须执行,不可跳过)

运行本 skill 自带的自检脚本(路径:本 skill 目录下 `scripts/validate_plugin.py`):

```bash
python3 <skill目录>/scripts/validate_plugin.py <插件目录>
```

它会机械化检查:语法、唯一 Star 子类、__del__/terminate、return-字符串陷阱、llm_tool docstring、其他框架混入、metadata 格式陷阱(连字符/版本浮点化/astrbot_version 带 v)、_conf_schema.json 合法性、requirements 违规。**输出"0 个错误"才能交付**;警告逐条人工判断。

若环境无法运行脚本,则逐条人工核对上面第 2-4 步的每一条,并额外用 `python3 -m py_compile main.py` 验证语法、`python3 -c "import json;json.load(open('_conf_schema.json'))"` 验证 JSON。

### 第 6 步:交付物说明

交付时向用户说明:本地调试方式(插件目录放 `AstrBot/data/plugins/` 下,WebUI 重载或 `ASTRBOT_RELOAD=1`);metadata 中占位的 repo/author 需替换;发布市场流程见 guidelines.md 第 11 章(zip ≤16MB、GitHub 公开仓库、plugins.astrbot.app 右下角 `+` 提交)。上架是 **Issue 制**,目标仓库 `AstrBotDevs/AstrBot_Plugins_Collection`(向主仓库提 Issue 的旧方式已废弃),提交 Issue 需勾选三个强制承诺项:已充分测试 / 不含恶意代码 / 遵守 GitHub 社区行为准则。另外,v4.26.3 起 WebUI 支持从本地安装插件(本地安装入口),本地调试多了一条不经 GitHub 的路径,可用于内网/未推送代码的快速验证。

## 代码评审输出格式

```
【强制-违规】4.3.2 定义了 __del__ → terminate 永不执行。删除 __del__,清理逻辑移入 terminate。
【要求-建议改】5.1.2 计数器建议改用 self.put_kv_data 而非自建 json。
【通过】生命周期、异常处理、数据目录检查无问题。
```

评审时同样运行 `scripts/validate_plugin.py` 辅助,但人工检查不能省(脚本只覆盖机械规则)。

## 2718lab 团队约定

- author 统一填 `2718lab`;仓库放 2718lab org 下,命名 `astrbot_plugin_<name>`。
- 发布走 `assets/templates/.github/workflows/release.yml`(检测 `metadata.yaml` 的 version 变更自动打包发 Release)。
- `CHANGELOG.md`、git tag、`metadata.yaml: version` 三处必须一致(呼应 guidelines.md 11.1)。

## 相关 skill

- `python-engineering` — 通用 Python 工程质量/异步/打包规范;本 skill 只管 AstrBot 特有约束,通用工程基线查它。
- `oss-repo-ops` — GitHub 仓库治理、issue 模板与 release 流程的通用部分;`assets/templates/.github` 的深度定制找它。
- `mcp-server-dev` — 插件要桥接/暴露 MCP 能力时查它。
- `work-methodology` — 评审与红队流程的通用方法论。
