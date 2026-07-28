# astrbot_plugin_example

一句话简介。

> **模板来源**:起步套件以官方 [DBJD-CR/astrbot_plugin_helloworld](https://github.com/DBJD-CR/astrbot_plugin_helloworld) 模板为底,`main.py` 为团队加固版——相对官方骨架增加了 config 注入、任务集管理(`self._tasks`)、aiohttp 生命周期、terminate 清理。刻意排除了两项:`.github/workflows/shit-mountain.yml`(DBJD-CR 仓库自用的代码质量评分/徽章生成流水线,与插件本身无关)与 `assets/*.svg`(该流水线配套的徽章图,同理排除)。其余 `.github/`、`LICENSE`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`run_ruff.bat` 均忠实复制,仅将仓库特定名称/链接换成占位符。

- AstrBot 版本要求:`>=4.16,<5`
- 支持平台:aiocqhttp

## 功能

- 功能 1
- 功能 2

## 安装

- 插件市场搜索 `astrbot_plugin_example` 一键安装
- 或手动:`git clone <repo>` 到 `AstrBot/data/plugins/` 后在 WebUI 重载

## 配置

| 配置项 | 说明 | 默认 |
|---|---|---|
| api_key | 服务 API Key | 空 |
| timeout | 请求超时秒数 | 30 |

## 指令

| 指令 | 参数 | 权限 | 说明 |
|---|---|---|---|
| /example | 无 | 所有人 | 示例指令 |

## FAQ

## 反馈

Issue: <repo>/issues

## 许可证

AGPL-3.0,见 [LICENSE](./LICENSE)
