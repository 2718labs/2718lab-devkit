# AstrBot 插件 API 与工具调用参考

> 依据:AstrBot v4.26.5(2026-07-07)源码与官方文档(docs.astrbot.app)。标注「vX.Y.Z+」的 API 有最低版本要求,使用时同步抬高 metadata 的 `astrbot_version` 下界。

## 目录

1. [官方导入面(import 全景)](#1-官方导入面import-全景)
2. [事件监听装饰器](#2-事件监听装饰器)
3. [事件钩子全表](#3-事件钩子全表)
4. [AstrMessageEvent 常用成员](#4-astrmessageevent-常用成员)
5. [消息发送与消息组件](#5-消息发送与消息组件)
6. [插件配置 _conf_schema.json](#6-插件配置-_conf_schemajson)
7. [数据存储](#7-数据存储)
8. [调用 LLM 与 Agent](#8-调用-llm-与-agent)
9. [LLM 函数工具(Tools)](#9-llm-函数工具tools)
10. [对话与人格管理](#10-对话与人格管理)
11. [会话控制 session_waiter](#11-会话控制-session_waiter)
12. [文转图](#12-文转图)
13. [平台实例与协议端 API](#13-平台实例与协议端-api)
14. [Context 与 StarTools 方法总表](#14-context-与-startools-方法总表)
15. [插件 Pages 与 Web API](#15-插件-pages-与-web-api)
16. [国际化 i18n](#16-国际化-i18n)
17. [版本门槛速查](#17-版本门槛速查)

---

## 1. 官方导入面(import 全景)

只从 `astrbot.api.*` 导入(少数官方文档明示的 core 路径例外,已在下文标出):

```python
from astrbot.api import logger, AstrBotConfig, sp, html_renderer
from astrbot.api import llm_tool, FunctionTool, ToolSet          # LLM 工具
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.provider import Provider, ProviderRequest, LLMResponse, Personality
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
import astrbot.api.message_components as Comp
from astrbot.api.util import session_waiter, SessionController   # 会话控制
```

`filter` 必须整体导入(`from astrbot.api.event import filter`),遮蔽内置 filter 是官方约定。禁止 `from astrbot.api.all import *`。

---

## 2. 事件监听装饰器

全部位于 `filter.*`,handler 必须是插件类内的 `async def`,前两个参数固定 `self, event`。多个装饰器叠加为 **AND** 关系。

| 装饰器 | 签名/参数 | 说明 |
|---|---|---|
| `@filter.command(name, alias: set = None, priority=0, desc=None)` | 指令 | 受唤醒前缀约束;名字不能带空格;alias 提供别名 |
| `@filter.command_group(name)` | 指令组 | 组函数体 `pass`;子指令 `@组名.command()`,子组 `@组名.group()`,可无限嵌套 |
| `@filter.event_message_type(filter.EventMessageType.ALL)` | 枚举:`PRIVATE_MESSAGE / GROUP_MESSAGE / OTHER_MESSAGE / ALL` | 监听全量消息的唯一正规方式(无 filter 的监听函数会被框架跳过) |
| `@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP \| ...)` | Flag 枚举,支持按位或 | 枚举含 20 个平台 + ALL |
| `@filter.permission_type(filter.PermissionType.ADMIN, raise_error=True)` | `ADMIN / MEMBER` | raise_error=True 权限不足回复提示并终止;False 静默跳过 |
| `@filter.regex(pattern)` | str 或 re.Pattern | **不受唤醒前缀约束**,对 `message_str.strip()` 做 search,谨防误伤 |
| `@filter.custom_filter(MyFilter())` | CustomFilter 子类 | 支持 `&`、`\|` 组合 |
| `@filter.llm_tool(name=None)` | 见第 9 节 | LLM 函数工具 |

**指令参数自动解析**:签名剩余参数按位置从消息切分并转换,支持 `int/float/bool/str`、默认值、`Optional[T]`;`GreedyStr` 吞掉剩余全部文本且必须是最后一个参数。参数缺失时框架把 `ValueError("必要参数缺失。该指令完整参数: ...")` 文本直接发给用户。

```python
@filter.command("add")
async def add(self, event: AstrMessageEvent, a: int, b: int = 0):
    """加法。/add 1 2"""
    yield event.plain_result(f"{a + b}")
```

**priority**:默认 0,**数值大者先执行**;`event.stop_event()` 终止后续一切 handler 与 LLM 流程。

**返回约定**:
- 异步生成器 handler(推荐):`yield event.plain_result(...)` 发消息;`yield` 裸交还控制权不发消息。
- 协程 handler:`return MessageEventResult` 有效;**`return "字符串" 不会发出任何消息**。
- handler 抛未捕获异常:框架记 traceback → 触发 on_plugin_error 钩子 → 把原始报错文本发给用户 → stop_event。

---

## 3. 事件钩子全表

钩子不可与 command 等消息过滤器混用;钩子内**不能 yield**,发消息用 `await event.send(...)`;钩子内异常被框架吞掉(仅日志)。

| 钩子 | Handler 签名(self 后) | 时机 |
|---|---|---|
| `@filter.on_astrbot_loaded()` | `()` | Bot 初始化完成(v3.4.34+) |
| `@filter.on_platform_loaded()` | `()` | 平台适配器加载完成 |
| `@filter.on_waiting_llm_request()` | `(event)` | 进入 LLM 请求等待(可发"思考中…") |
| `@filter.on_llm_request()` | `(event, req: ProviderRequest)` | LLM 请求前;改 `req.system_prompt` 只放稳定内容(每轮变化会打破提示词缓存,成本 7-20 倍);动态上下文用 `req.extra_user_content_parts.append(TextPart(text=...))`,`.mark_as_temp()` 仅本轮生效(v4.24.2+) |
| `@filter.on_llm_response()` | `(event, resp: LLMResponse)` | LLM 返回后 |
| `@filter.on_agent_begin()` / `on_agent_done()` | `(event, run_context[, response])` | Agent 开始/结束(v4.23.2+) |
| `@filter.on_using_llm_tool()` | `(event, tool: FunctionTool, tool_args: dict\|None)` | 工具调用前(v4.12.2+) |
| `@filter.on_llm_tool_respond()` | `(event, tool, tool_args, tool_result)` | 工具返回后(v4.12.2+) |
| `@filter.on_decorating_result()` | `(event)` | 发送前装饰,操作 `event.get_result().chain` |
| `@filter.after_message_sent()` | `(event)` | 发送后 |
| `@filter.on_plugin_error()` | `(event, plugin_name, handler_name, error, traceback_text)` | 任意插件 handler 异常;钩子内 `event.stop_event()` 可屏蔽默认报错回显 |
| `@filter.on_plugin_loaded()` / `on_plugin_unloaded()` | `(metadata)` | 插件装载/卸载 |

---

## 4. AstrMessageEvent 常用成员

| 成员 | 说明 |
|---|---|
| `event.message_str` | 消息纯文本 |
| `event.message_obj` | `AstrBotMessage`:`type, self_id, session_id, message_id, group_id, sender, message(组件链), message_str, raw_message(平台原始对象), timestamp` |
| `event.get_sender_id()` / `get_sender_name()` | 发送者 |
| `event.get_group_id()` | 群 ID(私聊为空) |
| `event.unified_msg_origin` | 会话唯一 ID,格式 `platform_name:message_type:session_id`,可持久化,用于主动消息 |
| `event.get_platform_id()` / `get_platform_name()` | 平台标识 |
| `event.is_admin()` | 是否管理员 |
| `event.is_at_or_wake_command` | 是否处于唤醒态 |
| `event.plain_result(text)` / `image_result(path_or_url)` / `chain_result([...])` / `make_result()` | 构造回复 |
| `event.request_llm(...)` | 转交 LLM 流程 |
| `await event.send(result)` | 钩子/waiter 内直接发送 |
| `event.stop_event()` / `continue_event()` / `is_stopped()` | 传播控制 |
| `event.set_extra(k, v)` / `get_extra(k)` | 事件级临时数据 |
| `event.bot` | aiocqhttp 平台的协议端 client(见第 13 节) |

---

## 5. 消息发送与消息组件

### 被动回复(handler 内)

```python
yield event.plain_result("文本")
yield event.image_result("/path/or/https-url")
yield event.chain_result([Comp.At(qq=event.get_sender_id()), Comp.Plain("你好")])
```

`MessageEventResult` 链式:`.message() .at() .at_all() .url_image() .file_image() .base64_image() .use_t2i() .use_markdown() .stop_event()`。

### 主动消息(定时/延迟场景)

```python
from astrbot.api.event import MessageChain

umo = event.unified_msg_origin          # 先在事件里存下来
chain = MessageChain().message("到点啦!").file_image("path/to.jpg")
await self.context.send_message(umo, chain)   # 或 StarTools.send_message(umo, chain)
```

平台差异:qq_official(QQ 官方接口)、企微、钉钉**不支持主动消息**;aiocqhttp、Telegram、飞书支持。

### 消息组件(`import astrbot.api.message_components as Comp`)

| 组件 | 用法 | 限制 |
|---|---|---|
| `Comp.Plain(text)` | 文本 | aiocqhttp 会 strip 首尾空白,可用零宽空格 `​` 规避 |
| `Comp.At(qq=...)` / `AtAll()` | @某人/@全体 | |
| `Comp.Image.fromURL(url)` / `.fromFileSystem(path)` | 图片 | |
| `Comp.Record(file=path)` | 语音 | 当前仅接受 wav |
| `Comp.Video.fromFileSystem(path=...)` / `.fromURL(url=...)` | 视频 | |
| `Comp.File(file=..., name=...)` | 文件 | 部分平台不支持 |
| `Comp.Reply(id=...)` | 引用回复 | |
| `Comp.Node(uin=..., name=..., content=[...])` / `Nodes` | 合并转发 | 仅 OneBot v11(aiocqhttp) |
| `Comp.Face(id=...)` / `Poke` / `Music` | QQ 表情/戳一戳/音乐卡 | 仅 OneBot v11 |

---

## 6. 插件配置 _conf_schema.json

插件根目录放 `_conf_schema.json`,框架自动生成 `data/config/<目录名>_config.json` 并注入 `__init__(self, context, config: AstrBotConfig)`。WebUI 可视化编辑;新版本 Schema 变更自动补默认值合并。

**type 合法值(共 9 个,严格白名单)**:`string / text(大文本框) / int / float / bool / object(配 items 子 Schema) / list / template_list(v4.10.4+) / file(v4.13.0+)`。**没有 `dict` 类型**——框架 `DEFAULT_VALUE_MAP`(astrbot/core/config/default.py)只认这 9 个,写 `type: dict` 会在加载时 `raise TypeError` 直接崩(嵌套映射用 `object` + `items`)。type 非法 → 插件加载失败。

**通用属性**:`description`(一句话)、`hint`(悬浮提示)、`obvious_hint`(bool)、`default`、`invisible`(bool,面板隐藏)、`options`(下拉枚举)+`labels`(展示文本)、`editor_mode`/`editor_language`/`editor_theme`(代码编辑器,v3.5.10+)、`slider`(`{"min":0,"max":2,"step":0.1}`)。

**`_special` 可视化选择器**(v4.0.0+)可用值:`select_provider / select_provider_tts / select_provider_stt / select_persona`(结果 str)、`select_knowledgebase`(结果 list)。其余保留值禁用。

**AstrBotConfig 用法**:dict 子类,支持点号访问但**未命中返回 None 而非 KeyError**;修改后 `self.config.save_config()` 原子落盘。

```json
{
  "api_key": {"type": "string", "description": "API Key", "obvious_hint": true, "default": ""},
  "timeout": {"type": "int", "description": "超时秒数", "default": 30},
  "mode": {"type": "string", "description": "模式", "options": ["simple", "advanced"], "default": "simple"},
  "advanced": {"type": "object", "description": "高级", "items": {
    "retry": {"type": "int", "description": "重试次数", "default": 3}
  }}
}
```

---

## 7. 数据存储

| 场景 | API |
|---|---|
| 数据目录(文件/sqlite) | `StarTools.get_data_dir()` → `data/plugin_data/<插件名>`,自动创建。只在插件自身模块内直接调用;外部工具函数中调用需显式传插件名 |
| 轻量 KV(v4.9.2+,按 plugin_id 隔离) | `await self.put_kv_data(k, v)` / `await self.get_kv_data(k, default)` / `await self.delete_kv_data(k)`。**注意(v4.26.2+,#8291)**:插件卸载时框架会自动清空该插件的 KV 存储;跨"卸载→重装"必须保留的数据不要放 KV,改写 `get_data_dir()` 下的文件 |
| 读全局配置 | `self.context.get_config()`(只读);`self.context.get_config(umo=...)` 会话粒度(v4.0.0+) |
| 数据根路径 | `from astrbot.core.utils.astrbot_path import get_astrbot_data_path`(官方文档示例用法) |

禁止写插件安装目录——更新时整目录删除重建。

---

## 8. 调用 LLM 与 Agent

### 新版 API(v4.5.7+,推荐)

```python
provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt="你好")
text = resp.completion_text
# 也支持 contexts=[...消息段...] 传多轮
```

### Agent 循环(自动处理工具调用,v4.5.7+)

```python
await self.context.tool_loop_agent(
    event=event, chat_provider_id=provider_id,
    prompt="帮我查天气", system_prompt="...",
    tools=ToolSet([MyTool()]), max_steps=30, tool_call_timeout=60,
)
```

### 旧版 API(仍可用)

```python
prov = self.context.get_using_provider(umo=event.unified_msg_origin)
resp = await prov.text_chat(prompt="...", context=[{"role": "user", "content": "..."}],
                            system_prompt="...", image_urls=[], func_tool=None)
```

`LLMResponse` 字段:`completion_text / result_chain / tools_call_args / tools_call_name / raw_completion`。

也可以不自己调用而转交主流程:`yield event.request_llm(prompt=...)`。

---

## 9. LLM 函数工具(Tools)

### 方式一:装饰器(简单场景)

```python
@filter.llm_tool(name="get_weather")   # name 缺省用函数名
async def get_weather(self, event: AstrMessageEvent, location: str):
    """获取指定地点天气。

    Args:
        location(string): 地点名称,如"杭州"
    """
    yield event.plain_result(f"{location} 晴 25℃")
```

**硬规则**:参数 Schema **完全由 docstring 的 `Args:` 段生成**(不读函数签名注解);格式必须 `参数名(类型): 描述`;类型限 `string / number / object / boolean / array`(v4.5.7+ 支持 `array[string]` 等子类型)。缺 Args 段 → 参数被静默丢弃;格式错误 → 可能导致插件加载失败。装饰器不支持 `parameters=` 显式 Schema(会被忽略)。

### 方式二:FunctionTool 类(需精确控制 Schema)

```python
from dataclasses import dataclass, field
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

@dataclass
class WeatherTool(FunctionTool[AstrAgentContext]):
    name: str = "get_weather"
    description: str = "获取城市天气"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return ToolExecResult(content=f"{kwargs['city']} 晴")
```

注册:`self.context.add_llm_tools(WeatherTool())`(v4.5.1+)。`context.register_llm_tool()` 已弃用。启停:`StarTools.activate_llm_tool(name)` / `deactivate_llm_tool(name)`。

**版本注意**:v4.26.0 起 LLM 工具有独立于插件启用状态的按工具权限/启用管理;v4.26.2(#9048)正式把"插件启用状态"和"工具启用状态"分离,`activate_llm_tool`/`deactivate_llm_tool` 的语义随之变化(工具可以在插件保持启用的情况下单独被禁用,反之亦然)。v4.26.1(#9001)修复过一次回归——插件注册的工具在受保护权限处理场景下可能无法正常调用。**调试"工具不触发"时,先查 WebUI 里该工具自己的启用开关,不要只查插件级开关。**

---

## 10. 对话与人格管理

`self.context.conversation_manager`:
`get_curr_conversation_id(umo)`、`get_conversation(umo, cid)`、`new_conversation(...)`、`switch_conversation`、`delete_conversation`、`get_conversations`、`update_conversation(history/title/persona_id)`、`add_message_pair(cid, user_message=UserMessageSegment(...), assistant_message=AssistantMessageSegment(...))`(消息段类来自 `astrbot.core.agent.message`)。

`self.context.persona_manager`:
`get_persona(id)`、`get_all_personas()`、`create_persona(persona_id, system_prompt, begin_dialogs, tools)`(tools=None 允许全部工具,[] 禁用全部)、`update_persona`、`delete_persona`、`get_default_persona_v3(umo)`。

---

## 11. 会话控制 session_waiter

多轮交互(等待用户下一条消息),v3.4.36+:

```python
from astrbot.core.utils.session_waiter import session_waiter, SessionController

@filter.command("接龙")
async def start(self, event: AstrMessageEvent):
    yield event.plain_result("请发送一个成语~")

    @session_waiter(timeout=60, record_history_chains=False)
    async def waiter(controller: SessionController, event: AstrMessageEvent):
        if event.message_str == "退出":
            await event.send(event.plain_result("已退出"))   # waiter 内不能 yield
            controller.stop()
            return
        await event.send(event.plain_result("先见之明"))
        controller.keep(timeout=60, reset_timeout=True)      # 续期等待下一条

    try:
        await waiter(event)
    except TimeoutError:
        yield event.plain_result("超时了!")
    finally:
        event.stop_event()
```

`SessionController`:`keep(timeout, reset_timeout)`、`stop()`、`get_history_chains()`。默认按 sender_id 区分会话;自定义粒度实现 `SessionFilter.filter(event) -> str` 并 `await waiter(event, session_filter=MyFilter())`(如返回 group_id 让全群共享)。

---

## 12. 文转图

```python
url = await self.text_to_image("# 标题\n内容")            # Star 方法,return_url=False 存本地
url = await self.html_render(TMPL, {"items": [...]},       # HTML + Jinja2 模板
                             options={"full_page": True, "type": "jpeg", "quality": 90})
yield event.image_result(url)
```

options 对应 Playwright screenshot:`timeout / type("jpeg"|"png") / quality(仅jpeg) / omit_background(仅png) / full_page / clip / animations / caret / scale`。在线调试:https://t2i-playground.astrbot.app/

---

## 13. 平台实例与协议端 API

```python
platform = self.context.get_platform_inst(event.get_platform_id())   # v4.0.0+

# aiocqhttp(OneBot v11)直接调协议端 API:
if event.get_platform_name() == "aiocqhttp":
    client = event.bot
    await client.api.call_action("delete_msg", message_id=int(event.message_obj.message_id))
```

Napcat/Lagrange 的 action 文档:napcat.apifox.cn、lagrange-onebot.apifox.cn。

---

## 14. Context 与 StarTools 方法总表

**Context**(`self.context`):

| 方法 | 用途 |
|---|---|
| `get_registered_star(name)` / `get_all_stars()` | 插件间软依赖探测 |
| `get_config(umo=None)` | 全局/会话配置 |
| `get_db()` | 框架数据库 |
| `send_message(umo, chain)` | 主动消息 |
| `llm_generate(...)` / `tool_loop_agent(...)` | LLM/Agent(v4.5.7+) |
| `get_using_provider(umo)` / `get_provider_by_id(id)` / `get_all_providers()` | Provider 管理(另有 TTS/STT 变体) |
| `get_llm_tool_manager()` / `add_llm_tools(*tools)` | 工具管理 |
| `register_web_api(route, handler, methods, desc)` | 注册 WebUI 后端 API(路由须以插件名为前缀) |
| `get_platform_inst(platform_id)` | 平台实例 |
| `conversation_manager` / `persona_manager` / `platform_manager` | 管理器 |
| 已废弃 | `get_platform`、`register_llm_tool`、`unregister_llm_tool`、`register_commands`、`register_task` |

**StarTools**(类方法,无需实例):`get_data_dir(plugin_name=None)`、`send_message(session, chain)`、`create_message(...)`、`create_event(...)`、`activate_llm_tool / deactivate_llm_tool / register_llm_tool / unregister_llm_tool`(v4.26.2+ 工具启用状态与插件启用状态分离,语义变化见第 9 节末尾)。

**Star 自带**:`text_to_image()`、`html_render()`、KV 三件套(`put_kv_data/get_kv_data/delete_kv_data`)、`self.name / self.author / self.plugin_id`(框架注入)。

---

## 15. 插件 Pages 与 Web API

- 目录:`插件根/pages/<page_name>/index.html`,WebUI 插件详情页入口。简单配置优先用 `_conf_schema.json`,Pages 适合 Dashboard/复杂表单/日志流。
- 后端:`self.context.register_web_api(f"/{PLUGIN_NAME}/ping", self.ping_handler, ["GET"], "描述")`;**路由必须以插件名为前缀**。`register_web_api` 本身的签名(route, view_handler, methods, desc)**未变**(源码 `core/star/context.py` 已核实)。
- **v4.26.0+(PR #8688)后端已从 Quart 迁移到 FastAPI/Starlette**(源码 `dashboard/server.py` 用 `FastAPIAppAdapter`)。新代码写 handler 请改用官方新增的公开模块 `astrbot.api.web`,**禁止**再写 `from quart import ...` / 返回 `jsonify(...)`:

  ```python
  from astrbot.api.web import request, json_response, error_response, file_response, stream_response

  async def ping_handler(self):
      body = await request.json()          # request 是 async-context 代理
      # 还可用 request.method / .path / .headers / .cookies / .query /
      # .path_params / .plugin_name / .username / .body() / .form() / .files()
      return json_response({"ok": True, "echo": body})
  ```

  该模块闭集导出(仅这些,不要凭记忆加别的):`request`、`json_response()`、`error_response()`、`file_response()`、`stream_response()`,以及类型 `PluginRequest`、`PluginUploadFile`、`PluginMultiDict`。旧式 Quart 写法通过 `quart_compat_path` 兼容 shim 仍能跑,但新插件一律用 `astrbot.api.web`,并把 `astrbot_version` 下界抬到 `>=4.26`(用了这套 API 才需要抬,模板默认门槛不动)。
- 前端桥 `window.AstrBotPluginPage`(自动注入):`ready()`(返回 {pluginName, pageName, locale, i18n, isDark, ...})、`t(key, fallback)`、`apiGet/apiPost(endpoint, ...)`(endpoint 用相对路径,自动转发到 `/api/plug/<plugin_name>/...`)、`upload(endpoint, file)`、`download(...)`、`subscribeSSE(endpoint, {onMessage,...})`、`onContext(handler)`(响应暗色切换)。
- endpoint 禁止:空串、`/` 开头、`..`、`\`、scheme、query(用 params 参数)、hash。
- iframe sandbox 限制:`allow-scripts allow-forms allow-downloads`,不可访问 Dashboard cookie/DOM;SPA 用 hash routing;静态资源相对路径。

---

## 16. 国际化 i18n

- 目录:`插件根/.astrbot-plugin/i18n/<locale>.json`(zh-CN.json、en-US.json,locale 同 WebUI;文件 ≤1MB)。
- 必须嵌套 JSON(不支持点号扁平 key)。顶层键:`metadata`(覆盖 display_name/short_desc/desc)、`config`(按配置项名嵌套,覆盖 description/hint/labels;options 值不翻译,展示用 labels)、`pages`(`pages.<page_name>.title/...`)。
- 回退:当前语言缺失 → metadata.yaml / _conf_schema.json 默认文案。

---

## 17. 版本门槛速查

| API / 特性 | 最低 AstrBot 版本 |
|---|---|
| `session_waiter` | v3.4.36 |
| 指令 `alias` | v3.4.28 |
| 免 `@register` 自动注册 | v3.5.19 |
| Schema `editor_mode` | v3.5.10 |
| `_special` 选择器 / `get_config(umo=)` / `get_platform_inst` | v4.0.0 |
| `display_name` / `logo.png` | v4.5.0 |
| `add_llm_tools` | v4.5.1 |
| `llm_generate` / `tool_loop_agent` / llm_tool array 子类型 | v4.5.7 |
| KV 存储 / `self.name` 注入 | v4.9.2 |
| Schema `template_list` | v4.10.4 |
| Schema `file` 类型 | v4.13.0 |
| 工具钩子 `on_using_llm_tool` / `on_llm_tool_respond` | v4.12.2 |
| Agent 钩子 `on_agent_begin` / `on_agent_done` | v4.23.2 |
| `mark_as_temp()` | v4.24.2 |
| Star Context 类型提示修复(若类型提示异常,升级到该版本以上) | v4.25.5(#8659) |
| `astrbot.api.web` 模块 / 插件 Web API 后端由 Quart 迁移到 FastAPI | v4.26.0(PR #8688) |
| 工具启用状态与插件启用状态分离(`activate_llm_tool`/`deactivate_llm_tool` 语义变化) | v4.26.2(#9048) |
| KV 存储卸载插件时自动清空 | v4.26.2(#8291) |
