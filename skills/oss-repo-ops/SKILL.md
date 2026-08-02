---
name: oss-repo-ops
description: Prepare, review, release, tag, CI-enable, or submit a 2718lab/AstrBot open-source repository. Use for README, LICENSE, CHANGELOG, GitHub Actions, releases, PRs, and AstrBot market submission.
---

# 开源仓库运营

这是发布与仓库卫生的短说明书；工程实现转给对应技能：

- `references/repo-hygiene.md`：README、许可证、模板和体积基线。
- `references/release-workflow.md`：版本、tag、Release 和 CI。
- `references/astrbot-market.md`：AstrBot 市场的当前提交字段与 Issue 流程。
- `assets/templates/`：LICENSE、CI、issue/PR 模板；`scripts/check_release.py`：检查。

## 硬约束

1. README 先说明功能、安装、配置、兼容版本和跳转文档；CHANGELOG 使用
   Keep-a-Changelog；`.gitignore` 排除虚拟环境、缓存和 `.git` 垃圾。
2. 许可证按仓库边界判断：团队默认 AGPL-3.0；纯独立调用作品才在明确批准时用 MIT。
   不凭记忆手写许可证全文，复制模板。
3. tag、GitHub Release 和项目版本必须是同一三段版本；不要声称不存在的远程/tag。
4. CI 至少运行 lint、语法/类型检查和测试；优先复制模板中的 action 版本。
5. AstrBot 市场通常走提交 Issue，包体控制在 16MB 内；不编造不确定的审核规则。

## 交付

核对隐私和 allowlist，运行 `python scripts/check_release.py <repo>`，再按
`python-engineering` 或 `astrbot-plugin-dev` 做代码验证。用户未明确授权时不 push、打 tag、
发 Release 或创建 PR；需要外部发布时报告目标远程、分支和真实结果。
