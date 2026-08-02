---
description: 发布前自检:对目标仓库跑 oss-repo-ops 的 check_release.py 并解读
argument-hint: [仓库目录,默认当前工作目录]
---

# /2718lab-release-check

按 `oss-repo-ops` skill 做发布前机械自检。

目标仓库:$ARGUMENTS(未给则用当前工作目录)。

步骤:

1. 运行 `python "${CLAUDE_PLUGIN_ROOT}/skills/oss-repo-ops/scripts/check_release.py" <仓库目录>`。
2. 逐条解读输出,重点盯这些坑:`version` 用 2 段号被 YAML 当浮点数、`repo` 带 `.git`、`astrbot_version` 缺引号或带 `v`、剔除 `.git`/`__pycache__` 后打包体积 > 16MB、缺 LICENSE/README/CHANGELOG/CI。
3. **0 个 ERROR 才可发**;WARN 逐条人工判断(尤其 LICENSE 选择、README 与实现是否一致)。
4. 需要完整发版流程(semver 判级、tag → GitHub Release → CHANGELOG 摘录)→ 读 `oss-repo-ops` 的 `references/release-workflow.md`;上架 AstrBot 市场 → `references/astrbot-market.md`。

许可证默认 AGPL-3.0(团队政策,与 AstrBot 一致)。
