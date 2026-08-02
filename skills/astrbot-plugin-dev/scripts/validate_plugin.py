#!/usr/bin/env python3
"""AstrBot 插件交付前自检脚本(仅用标准库,可在任何环境运行)。

用法: python validate_plugin.py <插件目录>
退出码: 0=全部通过(可有警告), 1=存在必须修复的错误。
"""

import ast
import json
import keyword
import re
import sys
from pathlib import Path

ERRORS: list[str] = []
WARNS: list[str] = []

BANNED_IMPORTS = {
    "requests": "禁用同步网络库 requests,改用 aiohttp/httpx",
    "nonebot": "这是 NoneBot 框架的模块,AstrBot 插件禁止混用其他 bot 框架",
    "nonebot2": "这是 NoneBot 框架的模块,禁止混用",
    "koishi": "这是 Koishi 框架的模块,禁止混用",
    "graia": "这是 Graia/Ariadne 框架的模块,禁止混用",
    "botpy": "这是 QQ botpy SDK,AstrBot 插件应使用 astrbot.api",
    "khl": "这是 khl.py(KOOK SDK),应通过 AstrBot 平台适配器使用",
    "telebot": "这是 pyTelegramBotAPI,应通过 AstrBot 平台适配器使用",
    "telegram": "python-telegram-bot 属其他框架,应通过 AstrBot 平台适配器使用",
    "discord": "discord.py 属其他框架,应通过 AstrBot 平台适配器使用",
    "quart": "AstrBot v4.26.0 起 Web 后端为 FastAPI,用 astrbot.api.web",
}
CORE_OVERLAP_PKGS = {"aiohttp", "pydantic", "openai", "quart", "pillow", "psutil"}
# 框架 DEFAULT_VALUE_MAP(astrbot/core/config/default.py)的严格白名单:9 个,无 "dict"。
VALID_SCHEMA_TYPES = {"string", "text", "int", "float", "bool", "object", "list",
                      "template_list", "file"}


def err(msg):
    ERRORS.append(msg)


def warn(msg):
    WARNS.append(msg)


def py_files(root: Path):
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if "__pycache__" in parts or "tests" in parts or p.name == "conftest.py":
            continue
        yield p


def top_module(name: str) -> str:
    return name.split(".")[0]


def check_python(root: Path) -> bool:
    """返回 True 表示插件源码中出现了 astrbot.api.web 的导入(供门槛校验使用)。"""
    uses_web_api = False
    main_py = root / "main.py"
    if not main_py.exists():
        err("缺少入口文件 main.py")
        return uses_web_api

    star_classes = []  # (file, ClassDef)
    for p in py_files(root):
        src = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(root)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            err(f"{rel}: 语法错误 {e.lineno} 行: {e.msg}")
            continue

        for node in ast.walk(tree):
            # 禁用 import 检查(其他框架/requests/quart)
            if isinstance(node, ast.Import):
                for a in node.names:
                    m = top_module(a.name)
                    if m in BANNED_IMPORTS:
                        err(f"{rel}: import {a.name} —— {BANNED_IMPORTS[m]}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                m = top_module(node.module)
                if m in BANNED_IMPORTS:
                    err(f"{rel}: from {node.module} import ... —— {BANNED_IMPORTS[m]}")
                if node.module == "astrbot.api.web":
                    uses_web_api = True
            # jsonify( 检查(v4.26.0 起后端 FastAPI,不再是 Quart 的 jsonify idiom)
            elif isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "jsonify")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "jsonify")
            ):
                warn(f"{rel}: 出现 jsonify(...) —— AstrBot v4.26.0 起 Web 后端为 FastAPI,"
                     "插件 Web API handler 请改用 astrbot.api.web 的 json_response() 等")
            # print( 检查
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "print":
                warn(f"{rel}: 使用 print(),应改用 from astrbot.api import logger")
            # Star 子类收集
            elif isinstance(node, ast.ClassDef):
                bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                         for b in node.bases}
                if "Star" in bases:
                    star_classes.append((str(rel), node))

    if not star_classes:
        err("未找到继承 Star 的插件主类")
        return uses_web_api
    if len(star_classes) > 1:
        err(f"存在多个 Star 子类 {[(f, c.name) for f, c in star_classes]},"
            "整个插件只允许 main.py 中定义一个(框架按模块注册,多个会互相覆盖)")
    rel, cls = star_classes[0]
    if rel != "main.py":
        err(f"Star 子类 {cls.name} 定义在 {rel},必须定义在 main.py(否则框架匹配不到)")

    methods = {m.name: m for m in cls.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "__del__" in methods:
        err(f"插件类 {cls.name} 定义了 __del__ —— 它会让框架永不调用 terminate(),必须删除")
    if "terminate" not in methods:
        warn(f"插件类 {cls.name} 未定义 terminate();若创建过任务/会话/句柄则必须定义并清理")

    # 指令 handler 必须是异步生成器(yield);llm_tool 需要 Args: docstring
    for m in methods.values():
        decs = []
        for d in m.decorator_list:
            node = d.func if isinstance(d, ast.Call) else d
            chain = []
            while isinstance(node, ast.Attribute):
                chain.append(node.attr)
                node = node.value
            decs.append(".".join(reversed(chain)) or getattr(node, "id", ""))
        dec_str = " ".join(decs)
        # 叠加多个命令名过滤器 = 死锁:多个 filter 是 AND 关系,两个不同命令名
        # 不可能同时匹配,该 handler 永不触发(框架按 module_func 名注册,两个
        # 装饰器挂在同一 handler 的 event_filters 上做 AND)。多命令名用 alias={...}。
        cmd_count = sum(1 for x in decs if x in ("command", "command_group"))
        if cmd_count >= 2:
            err(f"main.py: handler {m.name} 叠加了 {cmd_count} 个 "
                "@filter.command/command_group —— 多 filter 为 AND 关系,不同命令名"
                "不可能同时匹配,该 handler 永不触发;多命令名请用 alias={...}")
        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(m))
        if ("command" in dec_str or "regex" in dec_str or "event_message_type" in dec_str):
            if not isinstance(m, ast.AsyncFunctionDef):
                err(f"main.py: handler {m.name} 必须是 async def")
            if not has_yield:
                ret_str = any(isinstance(n, ast.Return) and n.value is not None
                              for n in ast.walk(m))
                if ret_str:
                    err(f"main.py: handler {m.name} 用 return 返回值——协程 return 字符串"
                        "不会发出任何消息,必须改为 yield event.plain_result(...)")
                else:
                    warn(f"main.py: handler {m.name} 没有 yield,确认是否需要回复消息")
        if "llm_tool" in dec_str:
            ds = ast.get_docstring(m) or ""
            if "Args:" not in ds:
                err(f"main.py: llm_tool {m.name} 的 docstring 缺少 'Args:' 段——"
                    "参数 Schema 完全由 docstring 生成,缺失则参数被静默丢弃")
            elif not re.search(r"\w+\s*\(\s*(string|number|boolean|object|array)", ds):
                err(f"main.py: llm_tool {m.name} 的 Args: 段参数缺少类型标注,"
                    "格式必须是 '参数名(string): 描述'")

    # 数据写插件目录检查(启发式)
    all_src = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in py_files(root))
    if re.search(r"os\.path\.dirname\(\s*__file__\s*\)", all_src) and \
            re.search(r"(json\.dump|open\([^)]*[\"']w|write)", all_src):
        warn("疑似把数据写在插件自身目录(dirname(__file__)),插件更新会整目录删除,"
             "持久化必须用 StarTools.get_data_dir()")

    return uses_web_api


def check_metadata(root: Path):
    p = root / "metadata.yaml"
    if not p.exists():
        err("缺少 metadata.yaml")
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    # 无 yaml 库也能做的行级解析(避免引入依赖)
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*?)\s*(#.*)?$", line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip("'\"")

    for k in ("name", "desc", "version", "author"):
        if not fields.get(k):
            err(f"metadata.yaml 缺少必填字段 {k}(缺失则插件加载直接失败)")

    name = fields.get("name", "")
    if name:
        if not name.isidentifier() or keyword.iskeyword(name):
            err(f"metadata.yaml: name={name!r} 不是合法 Python 标识符"
                "(安装时目录会重命名为 name,连字符/空格会直接装不上)")
        if not name.startswith("astrbot_plugin_"):
            warn(f"metadata.yaml: name={name!r} 建议以 astrbot_plugin_ 开头")

    version = fields.get("version", "")
    if version and re.fullmatch(r"\d+(\.\d+)?", version):
        raw = re.search(r"^version\s*:\s*(\S+)", text, re.M)
        if raw and not raw.group(1).startswith(("'", '"')):
            err(f"metadata.yaml: version: {version} 会被 YAML 解析为数字而非字符串,"
                "请写成 v 前缀(如 v1.0.0)或加引号")

    if not fields.get("repo"):
        warn("metadata.yaml 未填 repo —— 用户将无法通过面板更新插件;发布市场必填")
    av = fields.get("astrbot_version", "")
    if not av:
        warn("metadata.yaml 未声明 astrbot_version(团队规范强制;按实测最低版本填,如 \">=4.16,<5\")")
    elif av.startswith("v"):
        err(f"metadata.yaml: astrbot_version={av!r} 不能带 v 前缀(PEP 440 约束格式)")


def check_web_api_version(root: Path, uses_web_api: bool):
    """astrbot.api.web(v4.26.0+)与 metadata astrbot_version 下界的一致性检查。"""
    if not uses_web_api:
        return
    p = root / "metadata.yaml"
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^astrbot_version\s*:\s*(.+?)\s*(#.*)?$", text, re.M)
    if not m:
        return
    av = m.group(1).strip().strip("'\"")
    lb = re.search(r">=\s*(\d+)(?:\.(\d+))?", av)
    if not lb:
        return
    lower_bound = (int(lb.group(1)), int(lb.group(2) or 0))
    if lower_bound < (4, 26):
        warn(f"source 使用了 astrbot.api.web,但 metadata.yaml: astrbot_version={av!r} "
             "下界低于 4.26(该模块 AstrBot v4.26.0+ 才存在,v4.26.0 前后端是 Quart),"
             "请把下界抬到 >=4.26")


def check_schema(root: Path):
    p = root / "_conf_schema.json"
    if not p.exists():
        return
    try:
        schema = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err(f"_conf_schema.json 不是合法 JSON({e})——注意 JSON 不允许注释、"
            "尾逗号、单引号;该文件非法会导致插件加载失败")
        return
    if not isinstance(schema, dict):
        err("_conf_schema.json 顶层必须是对象")
        return
    for key, item in schema.items():
        if not isinstance(item, dict):
            err(f"_conf_schema.json: {key} 的值必须是对象")
            continue
        t = item.get("type")
        if t not in VALID_SCHEMA_TYPES:
            err(f"_conf_schema.json: {key}.type={t!r} 非法(合法值: "
                f"{sorted(VALID_SCHEMA_TYPES)}),会导致插件加载失败")
        if "default" not in item:
            warn(f"_conf_schema.json: {key} 建议显式给 default")
        if "description" not in item:
            warn(f"_conf_schema.json: {key} 缺少 description")
        if t == "object" and "items" not in item:
            err(f"_conf_schema.json: {key} type=object 必须配 items 子 Schema")


def check_requirements(root: Path):
    p = root / "requirements.txt"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        pkg = re.split(r"[<>=!~\[;]", line)[0].strip().lower()
        if pkg in ("astrbot", "astrbot-api"):
            err("requirements.txt 不得包含 astrbot 本身(插件运行在 AstrBot 进程内)")
        if pkg in CORE_OVERLAP_PKGS and "==" in line:
            err(f"requirements.txt: {line!r} 钉死了与 AstrBot 核心重叠的包版本,"
                "会触发核心依赖保护 DependencyConflictError 装不上;改为宽松下界(>=)或不写版本")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}")
        sys.exit(1)

    uses_web_api = check_python(root)
    check_metadata(root)
    check_schema(root)
    check_requirements(root)
    check_web_api_version(root, uses_web_api)

    print(f"== AstrBot 插件自检: {root.name} ==")
    for e in ERRORS:
        print(f"[错误] {e}")
    for w in WARNS:
        print(f"[警告] {w}")
    print(f"结果: {len(ERRORS)} 个错误, {len(WARNS)} 个警告 -> "
          + ("不可交付,必须修复错误" if ERRORS else "通过"))
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
