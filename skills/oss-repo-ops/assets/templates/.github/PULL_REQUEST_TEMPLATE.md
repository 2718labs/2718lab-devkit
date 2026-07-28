<!-- 复制到插件/工具仓库的 .github/PULL_REQUEST_TEMPLATE.md -->

## 改动说明

<!-- 这个 PR 做了什么、为什么。关联 issue 用 Closes #123 -->

## 自检清单(插件类改动必须逐项确认)

- [ ] 已在本地 `AstrBot/data/plugins/` 下**实测**通过(不是只看代码)
- [ ] 若改了配置:`_conf_schema.json` 已用 AstrBot 加载校验过,无非法 `type`
- [ ] **未混入其他框架 API**(nonebot / koishi / graia / botpy / on_command 等一律不属于 AstrBot)
- [ ] 若用了较新的 AstrBot API:已相应抬高 `metadata.yaml` 的 `astrbot_version` 下界
- [ ] 若面向发布:`CHANGELOG.md` 已加当前改动条目

## 影响范围

<!-- 涉及哪些平台适配器 / 是否破坏性变更 / 是否需要用户改配置 -->
