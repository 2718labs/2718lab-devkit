---
description: 用 astrbot-plugin-dev 规范起一个新的 AstrBot 插件骨架(复制模板→改占位符→实现)
argument-hint: [astrbot_plugin_xxx] [一句话功能]
---

# /2718lab-new-plugin

按团队 `astrbot-plugin-dev` skill 的规范,起一个新的 AstrBot 插件骨架。

目标:$ARGUMENTS

步骤:

1. 先读 `astrbot-plugin-dev` skill(SKILL.md 第 0 条「框架保真」+ 第 1 步「新建流程」)。**不要凭记忆写 AstrBot API。**
2. 从 `${CLAUDE_PLUGIN_ROOT}/skills/astrbot-plugin-dev/assets/templates/` 复制整套起步文件到新插件目录。目录名 = 插件名,须 `astrbot_plugin_` 前缀、全小写、合法模块名(无连字符/大写/空格)。
3. 改掉所有占位符:`metadata.yaml` 的 name / desc / version(3 段号 `X.Y.Z`,建议带 `v`)/ author / repo(不带 `.git`)/ astrbot_version(引号内、不带 `v`);`main.py` 的插件类与逻辑;`_conf_schema.json`;`README.md`。
4. 按上面的功能描述实现;只用 skill 的 `references/api-reference.md` 里核实过的 API,查不到就不用。
5. 交付前运行 `python "${CLAUDE_PLUGIN_ROOT}/skills/astrbot-plugin-dev/scripts/validate_plugin.py" <插件目录>`,**0 错误**才算完。

要发布/上架市场 → `/2718lab-release-check` 与 `oss-repo-ops` skill。交付前想过一遍红队 → `/2718lab-review`。
