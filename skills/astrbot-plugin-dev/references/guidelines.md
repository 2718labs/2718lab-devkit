# AstrBot 插件开发与维护规范守则(团队版)

> 版本:v1.0 | 制定日期:2026-07-06
> 适用范围:本团队所有 AstrBot 插件项目
> 依据:AstrBot v4.25.1 源码、官方文档 docs.astrbot.app、官方插件模板 Soulter/helloworld、插件集合仓库 AstrBotDevs/AstrBot_Plugins_Collection 的 CI 审核脚本,以及社区高星插件实践。关键条款均在文中注明来源;AstrBot 迭代较快,涉及版本门槛的条款以当时官方文档为准。
> 条款分级:**[强制]** 违反会导致插件无法加载/无法过审/产生事故;**[要求]** 团队必须遵守,例外需在 PR 中说明;**[建议]** 推荐做法。

---

## 目录

1. [总则](#1-总则)
2. [仓库与项目结构规范](#2-仓库与项目结构规范)
3. [metadata.yaml 规范](#3-metadatayaml-规范)
4. [编码规范](#4-编码规范)
5. [数据与配置规范](#5-数据与配置规范)
6. [生命周期与资源管理规范(热重载安全)](#6-生命周期与资源管理规范热重载安全)
7. [兼容性规范](#7-兼容性规范)
8. [安全规范](#8-安全规范)
9. [测试与质量保证](#9-测试与质量保证)
10. [Git 工作流与协作规范](#10-git-工作流与协作规范)
11. [版本与发布规范](#11-版本与发布规范)
12. [维护与运营规范](#12-维护与运营规范)
13. [附录](#13-附录)

---

## 1. 总则

1.1 **[强制]** 遵守官方开发原则(docs: plugin-new.html「开发原则」):功能经过测试;包含良好注释;持久化数据存 `data` 目录而非插件自身目录;良好错误处理,不让插件因一个错误崩溃;提交前用 ruff 格式化;禁用 `requests`,使用 `aiohttp`/`httpx` 等异步库;给已有插件扩功能优先向原插件提 PR,而不是另写一个插件(除非原作者停止维护)。

1.2 **[强制]** 发布到插件市场即承诺:已充分测试、不含恶意代码、遵守 GitHub 社区行为准则(插件提交 Issue 模板的三个必选项)。

1.3 **[要求]** 团队插件默认开源,许可证统一(建议 MIT 或 AGPL-3.0,注意 AstrBot 本体为 AGPL-3.0-or-later;若插件闭源商用,先做许可证合规评估——插件以进程内 import 方式运行于 AGPL 宿主中,法律边界存在争议,拿不准时咨询律师)。

1.4 **[要求]** 每个插件必须有唯一负责人(Owner)与一名后备(Backup),记录在团队插件矩阵表(见 12.6)。

---

## 2. 仓库与项目结构规范

### 2.1 命名

2.1.1 **[强制]** 插件名(= 仓库名 = `metadata.name` = 安装后目录名 = Python 模块名)必须:

- 以 `astrbot_plugin_` 开头(官方推荐,团队强制);
- 全小写、无空格、无连字符,只用下划线;
- 是合法 Python 标识符且非关键字(源码 `star_manager.py: _validate_importable_name()` 强校验,含 `/`、`\` 或非标识符直接安装失败);
- 尽量简短、能表意。

> 原因:AstrBot 安装插件时会**把目录重命名为 metadata.name**,目录名即 import 路径 `data.plugins.<目录名>.main`。名字不合法 = 装不上。

2.1.2 **[强制]** 指令名必须避免与内置指令及常见插件冲突。跨插件同名指令**不会报错**,而是按 priority 静默共存、依次执行(源码 `waking_check/stage.py` + `star_request.py`)。团队约定:对通用词(如 `help`、`status`)必须加插件特征前缀或使用指令组,如 `/meme status` 而非 `/status`。

### 2.2 标准目录结构

2.2.1 **[要求]** 团队统一采用以下结构(带 * 为按需可选):

```text
astrbot_plugin_example/
├── main.py                 # 入口,唯一的 Star 子类定义处
├── metadata.yaml           # 插件元数据(必须)
├── requirements.txt        # 第三方依赖(有依赖则必须)
├── _conf_schema.json       # * 配置 Schema(有配置项则必须)
├── logo.png                # * 1:1 比例,推荐 256x256
├── README.md               # 必须,结构见 2.3
├── CHANGELOG.md            # 必须,Keep a Changelog 格式
├── LICENSE                 # 必须
├── .gitignore              # 必须(排除 __pycache__、.venv、node_modules 等)
├── core/                   # * 业务逻辑子模块(不含 Star 子类!)
│   ├── __init__.py
│   └── service.py
├── pages/                  # * WebUI 插件页面(每个一级子目录一个 Page,须含 index.html)
├── skills/                 # * 随插件分发的 Agent Skills(子目录含 SKILL.md)
├── .astrbot-plugin/
│   └── i18n/               # * 国际化文案 zh-CN.json / en-US.json
├── tests/                  # * 单元测试
└── .github/
    └── workflows/ci.yml    # 团队统一 CI(ruff + 加载冒烟)
```

2.2.2 **[强制]** 入口文件必须是 `main.py`(或 `<插件名>.py`,团队统一用 `main.py`)。两者都没有时插件被直接跳过(源码 `_get_modules()`),市场 CI 也做此检查。

2.2.3 **[强制]** 整个插件中只允许 `main.py` 里存在**一个** `Star` 子类。原因(源码 `base.py: __init_subclass__`):

- v3.5.19+ 起 Star 子类按 `cls.__module__` 自动注册,同一模块内第二个 Star 子类会覆盖第一个;
- 加载器按 `data.plugins.<目录名>.main` 匹配注册表,Star 子类若定义在子模块中会匹配失败,落入旧版反射加载分支,极易报「插件未通过 Star 注册」。
- 业务逻辑放 `core/` 等子模块,以普通类/函数形式组织,由 main.py 组装。

2.2.4 **[要求]** 大体积静态资源(模型、字体、示例图)不入仓库,改为首次运行时下载到数据目录。市场发布 zip 硬限制 16MB,超限 CI 直接拒绝(docs: plugin-publish.html)。

### 2.3 README 规范

2.3.1 **[要求]** README 必须包含(参考高星插件 meme_manager、private_companion 的结构):

1. 插件名、一句话简介、徽章(版本/License/支持平台);
2. 明示 **AstrBot 版本要求**与支持平台;
3. 功能列表;
4. 安装方式(插件市场一键安装 / 手动 clone 到 `data/plugins/`);
5. 配置说明(逐配置项);
6. 指令表(指令、参数、权限、示例);
7. FAQ / 常见坑;
8. 反馈渠道(Issue 链接)与许可证。

2.3.2 **[建议]** 安装成功后 README 内容会返回给 WebUI 展示(源码 `install_plugin`),因此开头一屏要能独立成立,截图用仓库内相对路径或 CDN。

---

## 3. metadata.yaml 规范

3.1 **[强制]** 必填字段:`name`、`desc`、`version`、`author`。缺任一,加载时抛「插件元数据信息不完整」(源码 `_load_plugin_metadata()`);市场 CI 同样检查这四项。

3.2 **[强制]** `repo` 必须填写 GitHub 仓库地址(https、github.com 域名、恰好 `owner/repo` 两段、**不以 `.git` 结尾**)。不填 repo 的插件**用户无法通过面板更新**(源码 `updator.py`),市场 CI 也会校验 URL 可达性。

3.3 **[要求]** 团队标准模板:

```yaml
name: astrbot_plugin_example        # 见 2.1.1,决定安装目录名
display_name: 示例插件               # 展示名(AstrBot >= v4.5.0)
desc: 一句话描述插件功能。
short_desc: 更短的市场卡片描述。      # 可选,缺省回退 desc
version: v1.0.0                     # 与 git tag、CHANGELOG 三处一致
author: YourTeamName
repo: https://github.com/yourteam/astrbot_plugin_example
astrbot_version: ">=4.16,<5"        # 必须声明!PEP 440,不加 v 前缀
support_platforms:                  # 按实测填写,值必须取自官方合法列表
  - aiocqhttp
  - telegram
```

3.4 **[要求]** `astrbot_version` 为团队强制项:按实际测试过的最低版本声明下界,大版本声明上界(如 `<5`)。不满足时框架阻止加载并提示版本不兼容——这比运行时报 AttributeError 对用户友好得多。

3.5 **[注意]** `support_platforms` 合法值(即 `ADAPTER_NAME_2_TYPE` 的 key):`aiocqhttp, qq_official, qq_official_webhook, telegram, wecom, wecom_ai_bot, lark, dingtalk, discord, slack, kook, vocechat, weixin_official_account, weixin_oc, satori, misskey, line, matrix, mattermost`。

3.6 **[注意]** yaml 中 `description` 会被自动映射为 `desc`;metadata.yaml 的值**覆盖** `@register` 装饰器填的值。新代码不再使用 `@register` 装饰器(已标记 DeprecationWarning),直接继承 `Star` 即自动注册。

---

## 4. 编码规范

### 4.1 工具链与风格

4.1.1 **[强制]** 使用 ruff,配置对齐 AstrBot 上游(pyproject.toml):line-length 88;lint 规则集 `F, W, E, ASYNC, C4, Q, I, UP`;ignore `F403, F405, E501, ASYNC230, ASYNC240`。提交前必须通过 `ruff format .` 和 `ruff check .`(接入 pre-commit,见 9.3)。

4.1.2 **[要求]** 语法基线:AstrBot 运行时要求 Python >= 3.12,但上游 ruff/pyright target 为 3.10。团队插件代码以 **3.10 语法**为兼容线(不用 3.11+ 独有语法),运行环境按 3.12 测试。

4.1.3 **[要求]** 公共函数写类型注解与 docstring;handler 的 docstring 必填——它会被 `/plugin` 帮助系统解析展示给最终用户,要写人话。

### 4.2 导入规范

4.2.1 **[强制]** 只从官方 API 面导入,禁止深入 `astrbot.core.*`(官方文档明示的少数例外除外:`session_waiter`、`get_astrbot_data_path`、agent tool 相关类)。core 内部无兼容承诺,升级即碎。

```python
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp
```

4.2.2 **[强制]** `filter` 必须以 `from astrbot.api.event import filter` 方式导入(遮蔽内置 `filter` 是官方约定写法);禁止 `from astrbot.api.all import *`(上游自己都把它排除在 ruff 检查外)。

4.2.3 **[强制]** 日志统一 `from astrbot.api import logger`,禁止自建 `logging.getLogger` / `print`。

### 4.3 插件类结构

4.3.1 **[强制]** 标准骨架:

```python
class ExamplePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        # 只做轻量赋值;耗时初始化放 initialize()

    async def initialize(self):
        """插件激活时由框架 await 调用,做真正的初始化。"""

    async def terminate(self):
        """禁用/卸载/重载/更新时调用,清理一切自建资源(见第 6 章)。"""
```

4.3.2 **[强制]** **禁止定义 `__del__`**。源码 `_terminate_plugin()` 是 `if __del__ ... elif terminate ...` 的关系:类里只要有 `__del__`,`terminate()` 就永远不会被调用。

4.3.3 **[强制]** `terminate` 必须**直接定义在插件主类上**。框架只查 `star_cls_type.__dict__`,不查 MRO——定义在中间基类里的 `terminate` 不会被调用(源码 `star_manager.py`)。

4.3.4 **[注意]** 有 `_conf_schema.json` 时框架优先按 `cls(context=..., config=...)` 实例化,TypeError 时回退 `cls(context=...)`;`__init__` 内可直接使用 `self.name` / `self.author` / `self.plugin_id`(框架在实例化前注入的类属性)。

### 4.4 Handler 与装饰器规范

4.4.1 **[强制]** 所有 handler 写在插件类内,前两个参数固定为 `self, event`;handler 与钩子必须是 `async def`(钩子在源码中有 `iscoroutinefunction` 断言)。

4.4.2 **[强制]** 多 filter 叠加是 **AND** 关系。禁止在同一函数上叠两个 `@filter.command`——两个指令名不可能同时匹配,结果是**永远不触发**。一个指令多个名字用 `alias`:

```python
@filter.command("meme_help", alias={"表情帮助", "memehelp"})
```

4.4.3 **[要求]** 指令组用 `@filter.command_group("xxx")`,子指令 `@xxx.command(...)`,嵌套子组用 `@xxx.group(...)`(不是 command_group)。组函数体保持 `pass`。

4.4.4 **[要求]** 指令参数利用框架自动解析(int/float/bool/str、默认值、Optional);吞尾参数用 `GreedyStr` 且必须是最后一个参数。参数缺失时框架会把 ValueError 文本原样发给用户,因此参数名要可读。

4.4.5 **[要求]** `priority` 默认 0,数值大者先执行。团队约定:普通功能不设 priority;确需拦截类逻辑(风控、鉴权)统一用 100 以上并在代码注释说明;`event.stop_event()` 会终止后续所有 handler 与 LLM 流程,使用必须有注释说明理由。

4.4.6 **[强制]** 监听全量消息必须显式 `@filter.event_message_type(filter.EventMessageType.ALL)`——没有任何 filter 的监听函数会被框架直接跳过(源码 `waking_check/stage.py`)。注意 `filter.regex` 不受唤醒前缀约束,能在未唤醒的群聊消息上触发,写正则要防误伤。

4.4.7 **[强制]** 管理类/危险指令必须加 `@filter.permission_type(filter.PermissionType.ADMIN)`,不得自行用 QQ 号硬编码判断。

4.4.8 **[强制]** 回复消息:异步生成器 handler 用 `yield event.plain_result(...)` 等;**协程 handler `return "字符串"` 不会发出任何消息**(框架只识别 MessageEventResult/CommandResult 实例)。事件钩子(on_llm_request 等)内**不能 yield**,要 `await event.send(...)`。

4.4.9 **[强制]** `@filter.llm_tool` 的参数 Schema **完全由 docstring 的 `Args:` 段生成**,不读函数签名注解。格式必须是 Google 风格 `参数名(类型): 描述`,类型限 `string / number / object / boolean / array`。缺失或格式错误会导致参数被静默丢弃甚至插件加载失败:

```python
@filter.llm_tool(name="get_weather")
async def get_weather(self, event: AstrMessageEvent, location: str):
    """获取指定地点的天气。

    Args:
        location(string): 地点名称,如"杭州"
    """
```

### 4.5 异步与网络

4.5.1 **[强制]** 禁止 `requests` 及任何同步阻塞 IO(time.sleep、同步 DB 驱动、大文件同步读写)直接出现在事件循环中。网络用 `aiohttp`/`httpx` 异步客户端;确需阻塞调用,包 `asyncio.to_thread()`。

4.5.2 **[要求]** `aiohttp.ClientSession` 在 `initialize()` 创建一次、全插件复用、在 `terminate()` 关闭;禁止在 handler 里每次新建 session。

4.5.3 **[要求]** 所有外呼必须设超时(aiohttp `ClientTimeout(total=...)`),并对失败做重试上限与降级文案。

4.5.4 **[要求]** 后台任务统一 `self._tasks: set[asyncio.Task]` 管理:`create_task` 后加入集合、`add_done_callback` 移除、terminate 时逐个 cancel 并 await(见 6.2)。禁止裸 `asyncio.create_task()` 不留引用(会被 GC 静默吞掉,且热重载后成僵尸任务)。

### 4.6 错误处理

4.6.1 **[强制]** handler 内部必须自行 try/except 业务异常并回复友好文案。未捕获异常框架虽不会崩(会记 traceback 并触发 on_plugin_error),但会把 `":(\n\n在调用插件 xxx 的处理函数 xxx 时出现异常:..."` 这种原始报错直接发给终端用户——这是体验事故(源码 `star_request.py`)。

4.6.2 **[要求]** except 分支必须 `logger.error(..., exc_info=True)` 或 `logger.exception(...)` 留全栈;面向用户的文案不包含堆栈、路径、密钥等内部信息。

4.6.3 **[建议]** 需要全局兜底(如上报监控)时用 `@filter.on_plugin_error()` 钩子;在钩子里 `event.stop_event()` 可屏蔽框架默认的报错回显。

---

## 5. 数据与配置规范

### 5.1 持久化数据

5.1.1 **[强制]** 一切持久化数据写入数据目录,**禁止写插件安装目录**——插件更新时旧目录被整体删除重建(源码 `updator.py`),写在里面的数据必丢:

```python
data_dir = StarTools.get_data_dir()          # => data/plugin_data/<插件名>,自动创建
```

注意 `get_data_dir()` 不传参时通过调用栈反查插件模块,**必须在插件自身模块内直接调用**;在外部工具库/lambda 中调用请显式传插件名。

5.1.2 **[要求]** 轻量状态(计数、开关、游标)用框架 KV 存储(AstrBot >= 4.9.2,按 plugin_id 隔离):`await self.put_kv_data(k, v)` / `await self.get_kv_data(k, default)` / `await self.delete_kv_data(k)`。结构化大数据用 sqlite/json 文件存 5.1.1 的目录。

5.1.3 **[要求]** 写文件用「临时文件 + `os.replace`」原子替换(AstrBotConfig 自身即如此实现),防止半写状态。

5.1.4 **[注意]** 用户在面板「卸载并删除数据」清理的是 `data/plugin_data/<目录名>`;手动安装且目录名 ≠ metadata.name 时会与 `get_data_dir()`(按插件名)错位——再次强调 2.1.1 的命名一致性。

5.1.5 **[注意]** AstrBot >= v4.26.2(#8291)起,**卸载插件会自动清空该插件的 KV 存储**(5.1.2 的 `put_kv_data`/`get_kv_data`/`delete_kv_data`)。凡是跨"卸载→重装"仍需保留的数据(用户长期配置、历史统计等),一律写 5.1.1 的 `get_data_dir()` 文件,不要只依赖 KV;KV 仅适合"卸载即应清空"的轻量状态。

### 5.2 插件配置

5.2.1 **[强制]** 一切用户可调参数走 `_conf_schema.json`,禁止让用户改代码常量或手动编辑 json。框架自动生成 `data/config/<目录名>_config.json` 并在 WebUI 提供可视化编辑;新版本新增配置项会自动补默认值合并(源码 `AstrBotConfig`),因此**只增不删改**:废弃字段标记 `invisible: true` 保留一个大版本,再删除。

5.2.2 **[要求]** Schema 字段规范:每项必须有 `type`(合法值共 9 个,严格白名单:`string / text / int / float / bool / object / list / template_list / file`,**无 `dict`**——框架 `DEFAULT_VALUE_MAP` 只认这 9 个,写 `type: dict` 加载时 raise TypeError;嵌套映射用 `object`+`items`)、`description`(一句话)、`default`;复杂含义补 `hint`;敏感或内部字段 `invisible: true`;枚举用 `options`(展示文本用 `labels`);选择器 `_special` 只用官方开放值(`select_provider / select_provider_tts / select_provider_stt / select_persona / select_knowledgebase`),保留值禁用。

5.2.3 **[强制]** 类型非法(不在合法列表)会抛 TypeError 导致**插件加载失败**——Schema 改动必须过一次真实加载测试(见 9.2)。

5.2.4 **[要求]** 配置读写:`self.config` 是 dict 子类且支持点号访问,但点号访问未命中返回 **None** 而非 KeyError——判断存在性用 `in` 或 `.get()`;修改后 `self.config.save_config()` 落盘。

5.2.5 **[强制]** API Key 等敏感信息只能来自配置项,禁止硬编码进代码或提交进仓库;README 与日志中不得出现真实 key。

---

## 6. 生命周期与资源管理规范(热重载安全)

> 背景:AstrBot 的禁用/卸载/更新/热重载都会调用 `terminate()`,随后把 `data.plugins.<目录名>` 前缀的模块从 `sys.modules` 移除、清掉框架侧注册(handler、llm_tools、平台适配器)。**框架不会替你清理自建资源**;清不掉的旧对象会让"新旧两套类并存",导致 isinstance 失败、端口占用、任务泄漏(源码 `_unbind_plugin` / `_purge_modules`)。

6.1 **[强制]** `terminate()` 清理清单——凡在插件中创建过以下资源,必须在 terminate 中对应释放:

| 资源 | 释放动作 |
|---|---|
| asyncio 任务/定时循环 | `task.cancel()` 后 `await asyncio.gather(*tasks, return_exceptions=True)` |
| aiohttp ClientSession / websocket | `await session.close()` |
| 数据库连接/文件句柄 | close/commit |
| 自监听的第三方回调、全局单例注册 | 显式反注册、置空 |
| `context.register_web_api()` 注册的路由 | 源码未见框架自动清理,自行记录并规避重复注册 |
| 子进程/线程池 | terminate/shutdown |

6.2 **[要求]** 参考实现:

```python
async def initialize(self):
    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    self._tasks: set[asyncio.Task] = set()
    t = asyncio.create_task(self._poll_loop())
    self._tasks.add(t)
    t.add_done_callback(self._tasks.discard)

async def terminate(self):
    for t in list(self._tasks):
        t.cancel()
    await asyncio.gather(*self._tasks, return_exceptions=True)
    if self._session and not self._session.closed:
        await self._session.close()
```

6.3 **[要求]** `initialize()` 与 `terminate()` 必须**幂等**(重复调用不报错):禁用状态下插件不会被实例化,启用走整体 reload,更新走「删目录→解压→reload」,同一进程内可能多次经历完整生命周期。

6.4 **[注意]** terminate 抛异常只记 warning、不阻断卸载,但意味着资源已泄露——CI 与 code review 把 terminate 完整性作为必查项。

---

## 7. 兼容性规范

7.1 **[强制]** `requirements.txt` 规范:

- 有第三方依赖必须写全(否则用户安装即 ModuleNotFound);
- **不钉死与 AstrBot 核心重叠的包版本**(aiohttp、pydantic、openai 等,见上游 pyproject dependencies):框架用核心依赖 constraints 保护安装,冲突版本会抛 `DependencyConflictError` 直接装不上(源码 `pip_installer.py`)。写宽松下界如 `aiofiles>=23`,不写 `==`;
- 依赖尽量少、体积尽量小(参与 16MB 限制,且用户网络环境差)。

7.2 **[要求]** 版本适配策略:

- 使用带版本门槛的 API(如 KV 存储 4.9.2+、`llm_generate` 4.5.7+、`display_name` 4.5.0+)时,`astrbot_version` 下界必须同步抬高,或做 hasattr 降级;
- AstrBot 每次 minor 发布后,插件矩阵内全部插件在新版本上跑一遍冒烟(见 12.3)。

7.3 **[要求]** 平台差异必须显式处理,不得假设所有平台能力一致。已知差异(docs: plugin.html 适配矩阵):主动消息在 qq_official、企微、钉钉不可用;`Node/Nodes` 合并转发仅 OneBot v11;`Record` 语音当前仅接受 wav;`File` 组件部分平台不支持;aiocqhttp 发送纯文本会 strip 首尾空白(可用零宽空格 `​` 规避)。只支持部分平台的功能,用 `@filter.platform_adapter_type(...)` 限定并在 metadata `support_platforms` 中如实声明。

7.4 **[建议]** 插件间联动一律做成**软依赖**:`self.context.get_registered_star(name)` 探测,不存在则降级,禁止直接 import 其他插件模块(热重载后模块对象会失效)。

7.5 **[要求]** AstrBot >= v4.26.0(PR #8688)起,插件 Pages 的 Web 后端由 Quart 迁移到 FastAPI/Starlette。新写的 Web API handler 一律使用官方新增模块 `astrbot.api.web`(`request`/`json_response`/`error_response`/`file_response`/`stream_response` 等,详见 api-reference.md 第 15 节),**禁止**新代码出现 `from quart import ...` 或 `jsonify(...)`;`register_web_api()` 的注册签名本身未变,不受影响。用了 `astrbot.api.web` 就按 7.2 的原则把 `astrbot_version` 下界同步抬到 `>=4.26`。

## 8. 安全规范

8.1 **[强制]** 禁止编写或集成任何恶意/灰产功能(刷量、爬虫对抗、账号自动化违规操作等);这是市场收录的承诺项,违反将连累团队全部插件的信誉。

8.2 **[强制]** 输入不可信:凡把用户输入拼进 shell 命令、SQL、文件路径、URL 的,必须白名单/参数化/`shlex.quote`;文件下载与解压必须校验路径穿越(`..`)与解压体积上限。

8.3 **[强制]** 危险操作(删数据、发广播、改配置)必须 ADMIN 权限 + 二次确认(用 `session_waiter` 实现 30 秒确认,参考 meme_manager 实践)。

8.4 **[要求]** 插件 Pages 遵守框架安全模型:后端路由必须以插件名为前缀注册(`register_web_api(f"/{PLUGIN_NAME}/...")`),前端 endpoint 只用相对路径;不要试图突破 iframe sandbox 或访问 Dashboard cookie。

8.5 **[要求]** 遥测/统计类功能必须默认关闭、在 README 声明收集内容,并提供配置开关。

8.6 **[要求]** 日志脱敏:不打印完整 API key、token、用户隐私消息全文(必要时截断/掩码)。

---

## 9. 测试与质量保证

9.1 **[要求]** 本地开发环境统一:clone AstrBot 本体,插件仓库 clone 到 `AstrBot/data/plugins/` 下开发;开启 `ASTRBOT_RELOAD=1` 环境变量启用 watchfiles 热重载(需安装 watchfiles),或在 WebUI 手动「重载插件」。

9.2 **[强制]** 每次发版前过一遍「真实加载测试」——这正是插件市场 CI 的做法(集合仓库 `scripts/validate_plugins/run.py`:隔离环境安装 requirements 后用 `PluginManager.load()` 实际加载,失败即 fail):

- 干净环境(新虚拟环境或 Docker)安装目标 AstrBot 版本;
- 安装插件 → 启动 → 确认无加载报错;
- 核心指令逐条手测;
- 禁用→启用→重载→更新 四个生命周期动作各来一遍,观察日志无资源泄漏警告。

9.3 **[要求]** 仓库统一接入 pre-commit(`ruff format` + `ruff check`)与 GitHub Actions CI。团队标准 CI 至少包含:ruff 检查;metadata.yaml 必填字段与 `astrbot_version` 存在性校验;requirements 可安装;(可选)拉起 AstrBot 做加载冒烟。

9.4 **[要求]** 纯逻辑模块(`core/`)写 pytest 单测,目标:核心业务路径有测试即可,不追求覆盖率数字。与框架强耦合的 handler 以 9.2 的集成冒烟为主。

9.5 **[建议]** 发布前自查用附录 E 的检查清单,PR 模板中内嵌该清单。

---

## 10. Git 工作流与协作规范

10.1 **[要求]** 分支模型:`main` 保持随时可发布;日常开发用 `feat/xxx`、`fix/xxx` 短分支(命名对齐 AstrBot 上游 CONTRIBUTING),PR 合入 main;禁止直接 push main。

10.2 **[要求]** Commit 与 PR 标题使用 Conventional Commits 前缀:`feat:` `fix:` `docs:` `refactor:` `perf:` `chore:` `ci:`。这同时是上游要求,团队成员给 AstrBot 或其他插件提 PR 时同样适用。

10.3 **[要求]** 代码评审:每个 PR 至少 1 名非作者成员 review。评审必查项:terminate 资源清理完整性(6.1);异常处理与用户文案(4.6);数据写入位置(5.1);metadata/`astrbot_version` 是否随 API 使用同步(7.2);配置 Schema 变更是否只增不删(5.2.1)。

10.4 **[要求]** Issue 驱动:非 trivial 改动先开 Issue 描述方案再动手;用户反馈的 bug 复现步骤记录在 Issue 中,commit message 关联 `fixes #n`。

10.5 **[建议]** 团队维护一个**插件模板仓库**(基于官方 helloworld 扩充:标准目录 + CI + pre-commit + PR 模板 + 本规范链接),新插件一律从模板创建,规范落地成本最低。

---

## 11. 版本与发布规范

11.1 **[要求]** 语义化版本 `vMAJOR.MINOR.PATCH`:破坏性变更(配置结构变化、指令改名、最低 AstrBot 版本抬高)升 MAJOR;新功能升 MINOR;修复升 PATCH。以下三处必须一致:`metadata.yaml: version`、git tag、CHANGELOG 条目。

11.2 **[要求]** 每次发版更新 `CHANGELOG.md`(Keep a Changelog 格式:Added/Changed/Fixed/Removed),并创建 GitHub Release。用户通过面板「更新插件」拉的是 repo 默认分支最新代码——**默认分支上的每个 commit 都应是可用状态**,半成品留在 feature 分支。

11.3 **[强制]** 首次发布到插件市场流程(docs: plugin-publish.html):

1. 代码推送至 GitHub 公开仓库;
2. 打开 https://plugins.astrbot.app → 右下角 `+` → 填写表单(name/display_name/desc/author/repo/tags/social_link);
3. 点「提交到 GITHUB」跳转创建 Issue,目标仓库是 **`AstrBotDevs/AstrBot_Plugins_Collection`**(标题形如 `[Plugin] <name>`,JSON body 含 name/display_name/desc/author/repo,可选 tags/social_link,CI 摄入),确认 JSON 无误、勾选三个承诺项后 Create。**注意:向 AstrBot 主仓库提 Issue 的旧方式已废弃**;目标也不是 `Astrbot_Plugins_Market`(那只托管市场站点 index.html)。

3.6 注意 repo 填写规范(不带 `.git` 后缀)。收录后无需为普通版本更新重新提交——市场每日 CI 自动从仓库刷新 version/stars 等信息。

11.4 **[强制]** 过审硬指标自查(对应市场 CI 检查项):zip ≤ 16MB;repo 为 github.com 且可公开访问;metadata.yaml 存在、可解析、四必填字段齐全且为字符串;name 合法(不含 `/` `\` `..`);入口 `main.py` 或 `<name>.py` 存在;干净环境可完整加载(依赖可装、导入不报错)。

11.5 **[要求]** 破坏性变更发布时:README 顶部加迁移说明;`astrbot_version` 与配置迁移逻辑同步;必要时在旧版本发一个仅含升级提示的 PATCH。

11.6 **[注意]** AstrBot >= v4.26.3(#8448)起,WebUI 支持「本地安装插件」入口,不必依赖 GitHub 仓库即可在本机 AstrBot 上直接安装调试中的插件目录。本地调试/内网环境可用该路径替代「clone 到 data/plugins/」的手动方式,但仍不改变 11.3 的市场提交流程(市场收录仍走 Issue 制 + 公开仓库)。

---

## 12. 维护与运营规范

12.1 **[要求]** 响应 SLA(团队对外承诺):Issue 首次响应 ≤ 3 个工作日;致命 bug(插件导致宿主异常、数据丢失)修复 ≤ 3 天,一般 bug 下个 PATCH 版本;安全问题走私下渠道(SECURITY.md 留邮箱),修复后再披露。

12.2 **[要求]** Issue 分级标签统一:`bug / feature / question / upstream(AstrBot 本体问题) / wontfix`。判定为上游问题的,引导用户到 AstrBot 主仓库并附插件侧分析。

12.3 **[要求]** 跟进上游:订阅 AstrBot Releases;每个 minor 版本发布后 7 天内完成全矩阵冒烟(9.2 精简版),不兼容的插件要么适配、要么用 `astrbot_version` 上界挡住新版本并发公告。加入官方开发者 QQ 群(975206796)保持信息同步。

12.4 **[要求]** 弃用流程:决定停止维护的插件,依次执行 README 顶部标注「不再维护」+ 归档仓库 + (若有替代品)指引迁移。**不要删仓库**——市场 CI 会将失联仓库标记进 unreachable-plugins,直接删除对已安装用户不负责任。移交他人维护优于弃用(呼应 1.1 的官方协作原则)。

12.5 **[要求]** 用户数据兼容:涉及存储结构变更时必须写自动迁移逻辑(参考 meme_manager 的旧目录安全迁移实践),禁止让用户手动删数据重来。

12.6 **[要求]** 团队插件矩阵表(建一个内部看板/表格)字段:插件名、Owner/Backup、当前版本、支持的 AstrBot 范围、支持平台、上次冒烟日期、开源状态、SLA 达标情况。每周例会过一遍红黄灯。

12.7 **[建议]** 运营动作:插件市场卡片质量(logo、short_desc、tags)直接影响安装量;重要更新在官方社区/群适度公告;关注 stars 与 Issue 舆情作为质量信号。留意官方插件奖励类活动(如「桐谷霁屿 x AstrBot 插件奖励活动」)。

---

## 13. 附录

### 附录 A:十大坑位速查(全部源自源码分析)

| # | 坑 | 后果 | 规避 |
|---|---|---|---|
| 1 | 目录名/metadata.name 非法标识符 | 装不上/import 失败 | 2.1.1 |
| 2 | 插件类定义 `__del__` | `terminate()` 永不执行 | 4.3.2 |
| 3 | `terminate` 写在基类而非主类 | 不会被框架调用 | 4.3.3 |
| 4 | 同一函数叠两个 `@filter.command` | 永不触发(AND 语义) | 4.4.2 用 alias |
| 5 | 跨插件同名指令 | 静默共存按 priority 竞争 | 2.1.2 前缀化 |
| 6 | 协程 handler `return "str"` | 消息不发出 | 4.4.8 用 yield |
| 7 | handler 未捕获异常 | 原始报错文本发给用户 | 4.6.1 |
| 8 | requirements 钉死核心重叠依赖 | DependencyConflictError 装不上 | 7.1 |
| 9 | 数据写插件目录 | 更新即丢失 | 5.1.1 |
| 10 | 后台任务/session 不在 terminate 清理 | 热重载后泄漏、新旧类并存 | 第 6 章 |

### 附录 B:main.py 骨架模板

```python
"""astrbot_plugin_example - 示例插件。"""

import asyncio

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


class ExamplePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self):
        self.data_dir = StarTools.get_data_dir()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("example 插件初始化完成")

    @filter.command("example", alias={"示例"})
    async def example_cmd(self, event: AstrMessageEvent):
        """示例指令,回显发送者昵称。"""
        try:
            yield event.plain_result(f"Hello, {event.get_sender_name()}!")
        except Exception:
            logger.exception("example 指令执行失败")
            yield event.plain_result("出了点问题,请稍后再试~")

    async def terminate(self):
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("example 插件已终止")
```

### 附录 C:_conf_schema.json 示例

```json
{
  "api_key": {
    "type": "string",
    "description": "服务 API Key",
    "hint": "在 xxx 平台的控制台获取",
    "obvious_hint": true,
    "default": ""
  },
  "timeout": {
    "type": "int",
    "description": "请求超时秒数",
    "default": 30
  },
  "mode": {
    "type": "string",
    "description": "工作模式",
    "options": ["simple", "advanced"],
    "default": "simple"
  },
  "advanced": {
    "type": "object",
    "description": "高级设置",
    "items": {
      "retry": {"type": "int", "description": "重试次数", "default": 3}
    }
  }
}
```

### 附录 D:发版检查清单(嵌入 PR 模板)

```text
[ ] ruff format / ruff check 通过
[ ] metadata.yaml: version 已更新,与 git tag、CHANGELOG 一致
[ ] astrbot_version 范围与本次使用的 API 匹配
[ ] 配置 Schema 变更为"只增不删",废弃项 invisible
[ ] terminate() 覆盖本次新增的所有资源
[ ] 干净环境真实加载测试通过(安装→加载→指令→禁用→启用→重载)
[ ] 新增依赖已写入 requirements.txt,未钉死核心重叠包
[ ] README / CHANGELOG 已更新;zip 体积 < 16MB
[ ] 无硬编码密钥;日志无敏感信息
```

### 附录 E:主要参考来源

- 官方文档:插件开发指南 https://docs.astrbot.app/dev/star/plugin-new.html 、最小实例/事件/发消息/配置/存储/会话控制/调用 AI/国际化/Pages/文转图等子页(docs.astrbot.app/dev/star/guides/*)、发布插件 https://docs.astrbot.app/dev/star/plugin-publish.html 、旧版完整指南 https://docs.astrbot.app/dev/star/plugin.html
- 源码(AstrBotDevs/AstrBot,v4.25.1):`astrbot/core/star/`(base.py、star.py、star_manager.py、star_handler.py、star_tools.py、context.py、updator.py、register/、filter/)、`astrbot/core/pipeline/`(context_utils.py、star_request.py、waking_check/stage.py)、`astrbot/api/*`、pyproject.toml、CONTRIBUTING.md
- 插件模板:https://github.com/Soulter/helloworld
- 插件市场与审核:https://plugins.astrbot.app 、AstrBotDevs/AstrBot_Plugins_Collection(plugins.json、scripts/validate_plugins/run.py、.github/workflows/validate*.yml、ISSUE_TEMPLATE/PLUGIN_PUBLISH.yml)
- 社区实践样本:anka-afk/astrbot_plugin_meme_manager、menglimi/astrbot_plugin_private_companion

> 维护说明:本规范由团队集中维护,随 AstrBot 大版本更新复审;条款与官方文档冲突时,以官方最新文档为准并回改本规范。
