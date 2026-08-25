# 打包、调试与接入(两包通用 + 各自差异)

本文件管:项目打包(pyproject/uv)、本地调试命令、接入 Codex 插件的 `.mcp.json`、传输选型建议。**不管** GitHub release / CI 流程(见 `oss-repo-ops` skill),**不管**通用 Python 工程规范如测试/typing/异步项目布局(见 `python-engineering` skill)。

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

## 3. 接入 Codex 插件

Codex 插件只通过插件根目录的 `.mcp.json` 声明 MCP server，不执行第三方客户端安装命令。
配置保持可审计的 stdio 入口，例如:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": ["run", "--locked", "python", "server.py"],
      "cwd": "mcp-tools"
    }
  }
}
```

## 4. 传输选型建议

| 场景 | 建议 transport |
|---|---|
| 本地单机、被 Codex 插件当子进程拉起 | 默认 stdio,不传 transport 参数 |
| 需要独立进程常驻、被多个客户端通过网络连接 | (A) `"streamable-http"` / (B) `"http"` |
| 旧客户端只认 SSE,或历史项目已用 SSE | `"sse"`——两包文档都提示这是过渡/legacy 选项,新项目优先选 HTTP |

## 5. 部署提醒

- stdio 模式下,**standard output 是协议通道**,任何 `print()` 都会污染协议流导致客户端解析失败。日志一律走 Context(`ctx.info/debug/warning/error`)或标准 `logging` 模块写到 stderr,不写 stdout。
- HTTP/SSE 常驻部署时,注意进程重启策略、端口固定、以及日志落盘(不依赖 Context,因为常驻服务可能没有活跃请求时也要记录日志,这部分按 `python-engineering` 通用工程规范处理)。

## 6. 姊妹 skill 分工

- 通用 Python 工程(测试、typing、异步、项目布局)→ `python-engineering`。
- GitHub 发布、CI、版本流程 → `oss-repo-ops`。
