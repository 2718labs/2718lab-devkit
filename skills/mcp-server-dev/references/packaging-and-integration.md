# 打包、调试与接入(两包通用 + 各自差异)

本文件管:项目打包(pyproject/uv)、本地调试命令、接入 Claude Desktop / Claude Code、传输选型建议。**不管** GitHub release / CI 流程(见 `oss-repo-ops` skill),**不管**通用 Python 工程规范如测试/typing/异步项目布局(见 `python-engineering` skill)。

## 1. pyproject.toml 模板要点

### (A) SDK 内置 v1.x

```toml
[project]
name = "my-mcp-server"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]>=1,<2",
]
```

`<2` 上界必须写,防止环境解析到 v2 alpha(`MCPServer`,breaking API,详见 `api-fastmcp-sdk.md` 第 0 节)。

### (B) 独立版 3.x

```toml
[project]
name = "my-mcp-server"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "fastmcp>=3.4,<4",   # 钉在实际测试过的 3.x 线;用了 CurrentContext() 等 v2.14+ 特性时下界要体现出来
]
```

若只用最基础的 `@mcp.tool` + 类型注解 Context,没有用到 `CurrentContext()`/`get_context()`,下界可以放宽,但不要越过 3.x/4.x 边界,也不要空写不带版本号的 `fastmcp`。

### [project.scripts] 提醒

两包的 server 正常都靠 `mcp dev/run`(A)或 `fastmcp run`(B)拉起,不是靠 console-script 入口点(见下面第 2 节)。除非你自己在 server 文件里补了 `def main(): mcp.run()` 并在 `if __name__ == "__main__":` 里调用它,否则**不要**写:

```toml
[project.scripts]
my-mcp-server = "my_mcp_server.server:main"
```

——两份模板 server 文件(`server_sdk.py`/`server_standalone.py`)都没有定义 `main()`,原样抄这段会导致装出来的命令行脚本一运行就 ImportError。

来源:两包各自 API 参考文件已引用的 grounding 来源(pyproject 写法为工程惯例,非 grounding 直接给出的字符串,按“保守写法 + 标注版本门槛”处理)。

## 2. 本地调试

### (A) SDK 内置 v1.x

```bash
uv run mcp dev server.py                              # 起 MCP Inspector
uv run mcp dev server.py --with pandas --with-editable .   # 额外装依赖/以可编辑模式装本项目
uv run mcp run server.py                              # 直接跑(仅支持 FastMCP,不支持低层 Server)
```

### (B) 独立版 3.x

```bash
fastmcp run my_server.py:mcp                          # stdio,CLI 直接 import mcp 对象
fastmcp run my_server.py:mcp --transport http --port 8000
```

## 3. 接入 Claude Desktop

### (A) SDK 内置 v1.x

```bash
uv run mcp install server.py
uv run mcp install server.py --name "My Analytics Server" -v API_KEY=abc123 -v DB_URL=... -f .env
```

- `--name` 覆盖 Claude Desktop 里显示的服务器名。
- `-v KEY=VALUE` 注入环境变量,可重复传多个。
- `-f .env` 从 env 文件批量加载环境变量。

### (B) 独立版 3.x

grounding report 抓取的页面里没有给出 (B) 包对应 `mcp install` 的等价命令;fastmcp CLI 目前确认的子命令只有 `fastmcp run`(见上)。若需要把 (B) 包 server 接入 Claude Desktop,按 Claude Desktop 官方配置文件手工写 `command`/`args` 指向 `fastmcp run xxx.py:mcp` 或直接指向解释器 + 脚本,**不要**臆造一个 `fastmcp install` 命令——grounding 里没出现过,查不到就不写。

## 4. 接入 Claude Code

(A) 包 v1.x README quickstart 给出的方式,是直接注册一个跑在 HTTP transport 上的 server:

```bash
claude mcp add --transport http my-server http://localhost:8000/mcp
```

也就是说:先用 `mcp.run(transport="streamable-http")` (A) 或 `mcp.run(transport="http")` (B) 把 server 起在本地端口,再用 `claude mcp add --transport http <name> <url>` 注册。grounding 里没有 (B) 包专属的 Claude Code 接入命令,按同样的 `claude mcp add --transport http` 方式处理即可(注册命令来自 Claude Code 自身,不属于任一 FastMCP 包)。

## 5. 传输选型建议

| 场景 | 建议 transport |
|---|---|
| 本地单机、被 Claude Desktop/Code 当子进程拉起 | 默认 stdio,不传 transport 参数 |
| 需要独立进程常驻、被多个客户端通过网络连接 | (A) `"streamable-http"` / (B) `"http"` |
| 旧客户端只认 SSE,或历史项目已用 SSE | `"sse"`——两包文档都提示这是过渡/legacy 选项,新项目优先选 HTTP |

## 6. 部署提醒

- stdio 模式下,**standard output 是协议通道**,任何 `print()` 都会污染协议流导致客户端解析失败。日志一律走 Context(`ctx.info/debug/warning/error`)或标准 `logging` 模块写到 stderr,不写 stdout。
- HTTP/SSE 常驻部署时,注意进程重启策略、端口固定、以及日志落盘(不依赖 Context,因为常驻服务可能没有活跃请求时也要记录日志,这部分按 `python-engineering` 通用工程规范处理)。

## 7. 姊妹 skill 分工

- 通用 Python 工程(测试、typing、异步、项目布局)→ `python-engineering`。
- GitHub 发布、CI、版本流程 → `oss-repo-ops`。
- 给 AstrBot 写插件(哪怕插件内部调用 MCP client)→ `astrbot-plugin-dev`,本文件不管这块。
