# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)
与[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.2.0] - 2026-07-26

### Added

- 确定性统一项目索引，覆盖 Python、Markdown、JSON、TOML 与 YAML 的
  可验证节点、边、快照和查询回执。
- SQLite 任务编排、租约 fencing、耐久点对点邮箱和 artifact 引用。
- 任务自有 checkpoint/CAS，以及与严格索引工作流绑定的安全恢复。
- 供所有领域 Skill 复用的 `2718lab-*` Agent 角色与危险操作审批日志。
- 共享底座之上的 Bugkiller 简单状态机与复杂缺陷任务 DAG。

### Changed

- 工程基础设施中的共享项目索引、耐久编排、审批适配器与 `2718lab-*` Agent
  角色提升为所有领域 Skill 可直接使用的执行底座；Bugkiller 仅保留缺陷状态、
  风险路由和证据规则。
- 旧 `bugkiller_*` MCP 工具与 `bugkiller-*` Agent 名称保留为兼容别名。
- MCP 对外名称统一为 `2718lab-tools`。
- 代码写入只路由到显式的 `gpt-5.6-sol` `ultra` 角色；
  Luna/Terra 只承担分诊、调查、文档或只读验证。
- 实现型请求在首个 Patch 解锁后优先开工，减少过度设计与重复收口。
- 开源仓库入口增加安全报告策略，CI 使用只读 `contents` 权限，并清除历史
  设计投影中的本机路径与私人 marketplace 名。
- 公开仓库坐标统一为 `2718labs/2718lab-devkit`，双宿主与 Python 项目元数据
  指向同一入口；README 增加 CI、版本与 AGPL-3.0 徽章。
- 仓库增加 Claude Code marketplace 清单，可直接从
  `2718labs/2718lab-devkit` 添加并安装。
- 首发仓库补齐行为准则与功能建议，并重整 Pull Request 模板；Issue 与 PR 入口采用
  “共享底座优先”的能力分类，Bugkiller 明确为共享底座之上的专门工作流。

### Fixed

- 严格任务可以按精确路径登记尚不存在的计划新文件，不再需要把写入范围扩大到
  整个目录；已删除文件和注册前意外出现的路径仍触发 `INDEX_STALE`。
- 过期租约可按任务当前阶段的已绑定快照原地重领，无需搬走合法输出。
- 发布检查器可区分 AstrBot 插件仓库与 Codex 插件仓库。

## 0.1.0 - 2026-07-12

### Added

- 首个内部版本，包含 `astrbot-plugin-dev`、
  `mcp-server-dev`、`python-engineering`、`oss-repo-ops` 与
  `work-methodology`。
- 五条开发命令、元数据 guard、MCP 校验工具和 AGPL-3.0 许可证。

[Unreleased]: https://github.com/2718labs/2718lab-devkit/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2718labs/2718lab-devkit/releases/tag/v0.2.0
