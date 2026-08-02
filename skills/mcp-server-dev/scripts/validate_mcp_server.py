#!/usr/bin/env python3
"""MCP server 交付前自检脚本(仅用标准库,可在任何环境运行)。

用法: python validate_mcp_server.py <server目录或单个 .py 文件>
退出码: 0=全部通过(可有警告), 1=存在必须修复的错误。

只做机械规则检查(语法/混包/装饰器括号/Context 获取方式/transport 字符串/
resource URI/返回类型注解/print 污染 stdio/依赖钉法)。人工审查见 SKILL.md 第 0/2/4 步。
"""

import ast
import py_compile
import re
import sys
from pathlib import Path

ERRORS: list[str] = []
WARNS: list[str] = []

# (A) 官方 SDK v1.x 允许的 transport 字符串
A_TRANSPORT_WHITELIST = {"stdio", "streamable-http", "sse"}
# (B) 独立版 3.x 允许的 transport 字符串
B_TRANSPORT_WHITELIST = {"stdio", "http", "sse"}


def err(msg):
    ERRORS.append(msg)


def warn(msg):
    WARNS.append(msg)


def collect_py_files(target: Path):
    if target.is_file():
        return [target]
    files = []
    for p in target.rglob("*.py"):
        parts = set(p.parts)
        if "__pycache__" in parts or "tests" in parts or p.name == "conftest.py":
            continue
        files.append(p)
    return files


def top_module(name: str) -> str:
    return name.split(".")[0]


def decorator_chain(dec) -> tuple[str, bool]:
    """返回 (点分装饰器名, 是否带括号调用)。"""
    is_call = isinstance(dec, ast.Call)
    node = dec.func if is_call else dec
    chain = []
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        chain.append(node.id)
    return ".".join(reversed(chain)), is_call


def check_syntax(files):
    ok_files = []
    for p in files:
        try:
            py_compile.compile(str(p), doraise=True)
            ok_files.append(p)
        except py_compile.PyCompileError as e:
            err(f"{p}: 语法错误 —— {e.msg}")
    return ok_files


def detect_package(trees: dict[Path, ast.AST]) -> str | None:
    """返回 'A' / 'B' / 'MIXED' / None(两个包都没导入)。"""
    has_a = False
    has_b = False
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                m = node.module
                if m == "mcp.server.fastmcp" or m.startswith("mcp.server.fastmcp."):
                    has_a = True
                elif m == "fastmcp" or m.startswith("fastmcp."):
                    has_b = True
                if m == "mcp.server" or m.startswith("mcp.server.") and "MCPServer" in \
                        {a.name for a in node.names}:
                    for a in node.names:
                        if a.name == "MCPServer":
                            err(f"{node.module}: import MCPServer —— 这是 SDK main 分支的 "
                                "v2 alpha 类名,禁止生产使用;稳定 API 是 v1.x 的 FastMCP")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    tm = top_module(a.name)
                    if tm == "fastmcp":
                        has_b = True
    if has_a and has_b:
        return "MIXED"
    if has_a:
        return "A"
    if has_b:
        return "B"
    return None


def check_decorators_and_transport(files, trees, package):
    whitelist = A_TRANSPORT_WHITELIST if package == "A" else B_TRANSPORT_WHITELIST
    other = "B" if package == "A" else "A"
    for p, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    name, is_call = decorator_chain(dec)
                    short = name.split(".")[-1]
                    if short in ("tool", "prompt"):
                        if package == "A" and not is_call:
                            err(f"{p}: {node.name} 用了裸 @mcp.{short}(不带括号) —— "
                                "(A) 包 v1.x 文档里没有这种写法,必须写成 "
                                f"@mcp.{short}(),不要把 (B) 包的语法搬过来")
                        if package == "B" and not is_call:
                            pass  # (B) 包允许裸装饰器
                    if short == "resource":
                        if not is_call or not (isinstance(dec, ast.Call) and dec.args):
                            err(f"{p}: {node.name} 的 @mcp.resource(...) 缺少 URI 字符串参数,"
                                "两个包都要求 resource 必须带 URI")
                    # tool/prompt 用了 (B) 已弃用的 enabled= 参数
                    if short == "tool" and is_call and isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "enabled":
                                warn(f"{p}: {node.name} 的 @mcp.tool(enabled=...) —— "
                                     "(B) 3.0.0 起已弃用,改用 mcp.enable()/mcp.disable()")
                    # tool 函数缺返回类型注解
                    if short == "tool" and node.returns is None:
                        warn(f"{p}: {node.name} 是 tool 但缺少返回类型注解,"
                             "补上有助于生成结构化输出 schema")

            # Context 获取方式检查(仅 (A) 包禁用 get_context/CurrentContext)
            if package == "A" and isinstance(node, ast.Call):
                fname = ""
                if isinstance(node.func, ast.Name):
                    fname = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    fname = node.func.attr
                if fname in ("get_context", "CurrentContext"):
                    err(f"{p}: 调用了 {fname}() —— (A) 包 v1.x 没有依赖注入辅助函数,"
                        "这是 (B) 独立版 2.x/3.x 才有的 API,不要混用")

            # transport 字符串检查:mcp.run(transport=...) 或 fastmcp run --transport
            if isinstance(node, ast.Call):
                fattr = node.func.attr if isinstance(node.func, ast.Attribute) else None
                if fattr == "run":
                    for kw in node.keywords:
                        if kw.arg == "transport" and isinstance(kw.value, ast.Constant) \
                                and isinstance(kw.value.value, str):
                            val = kw.value.value
                            if val not in whitelist:
                                hint = ""
                                if val == "streamable-http" and package == "B":
                                    hint = "(B) 现行字符串是 \"http\",不是 (A) 包的连字符版"
                                elif val == "http" and package == "A":
                                    hint = "(A) 包不用 \"http\",应为 \"streamable-http\""
                                elif "streamable_http" in val:
                                    hint = "下划线写法两个包文档都没有,应为连字符 \"streamable-http\"(A)/\"http\"(B)"
                                err(f"{p}: transport={val!r} 不在 ({package}) 包白名单 "
                                    f"{sorted(whitelist)} 内 {hint}")
    del other


def check_print_in_stdio(files, trees):
    for p, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "print":
                warn(f"{p}: 出现 print() —— stdio 模式下 stdout 是协议通道,"
                     "日志请用 ctx.info/debug/warning/error 或标准 logging 写 stderr")
                break


def check_pyproject(target: Path, package: str | None):
    root = target if target.is_dir() else target.parent
    pp = root / "pyproject.toml"
    if not pp.exists():
        # 向上找一层(templates 场景下 pyproject 常在项目根)
        pp = root.parent / "pyproject.toml"
    if not pp.exists():
        warn("未找到 pyproject.toml,跳过依赖钉法检查")
        return
    raw = pp.read_text(encoding="utf-8", errors="replace")
    # 逐行去掉整行注释(以 # 开头,允许前导空白),避免把模板里注释掉的备选依赖段落
    # 误判为"真的声明了"。行内 # 之后的内容同样按 TOML 注释处理。
    lines = []
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split(" #")[0])
    text = "\n".join(lines)
    has_mcp_dep = re.search(r'"mcp(\[[^\]]*\])?\s*[><=!~]', text) is not None
    has_fastmcp_dep = re.search(r'"fastmcp\s*[><=!~]', text) is not None
    if has_mcp_dep and has_fastmcp_dep:
        err("pyproject.toml 同时声明了 mcp[cli] 和 fastmcp 依赖 —— 一个项目只能选一个包")
    if package == "A":
        if not re.search(r'"mcp(\[[^\]]*\])?\s*>=\s*1\s*,\s*<\s*2"', text):
            warn('pyproject.toml 未按 "mcp[cli]>=1,<2" 钉版本上界,'
                 "存在解析到 v2 alpha(MCPServer,breaking API)的风险")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    target = Path(sys.argv[1]).resolve()
    if not target.exists():
        print(f"路径不存在: {target}")
        sys.exit(1)

    files = collect_py_files(target)
    if not files:
        print(f"未找到任何 .py 文件: {target}")
        sys.exit(1)

    ok_files = check_syntax(files)

    trees: dict[Path, ast.AST] = {}
    for p in ok_files:
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            trees[p] = ast.parse(src)
        except SyntaxError:
            continue  # 已在 check_syntax 报过错

    package = detect_package(trees)
    if package == "MIXED":
        err("项目同时 import 了 mcp.server.fastmcp 与 fastmcp —— 混包,禁止同一项目混用")
    elif package is None:
        warn("未检测到 mcp.server.fastmcp 或 fastmcp 的 import,无法做包相关规则检查")
    else:
        check_decorators_and_transport(ok_files, trees, package)

    check_print_in_stdio(ok_files, trees)
    check_pyproject(target, package if package not in (None, "MIXED") else None)

    print(f"== MCP server 自检: {target} ==")
    if package in ("A", "B"):
        print(f"检测到包: ({package}) {'SDK 内置 v1.x mcp.server.fastmcp' if package == 'A' else '独立版 fastmcp 3.x'}")
    for e in ERRORS:
        print(f"[错误] {e}")
    for w in WARNS:
        print(f"[警告] {w}")
    print(f"结果: {len(ERRORS)} 个错误, {len(WARNS)} 个警告 -> "
          + ("不可交付,必须修复错误" if ERRORS else "通过"))
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
