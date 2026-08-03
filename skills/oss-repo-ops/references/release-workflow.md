# 版本与发布详版:semver 判级、操作序列、CI YAML 全文、drift 修复

本文件是 SKILL.md 第 3 步的完整版。

## 1. semver 判级规则(团队约定)

语义化版本 `MAJOR.MINOR.PATCH`,以下情形对应 MINOR 还是 MAJOR 是团队约定,不是 semver 规范强制,但为了统一按此执行:

- **PATCH**:纯 bug 修复,不改变任何对外行为(指令名、参数、配置项、返回格式)。
- **MINOR**:新增功能、新增指令、新增可选配置项(向后兼容,老配置不填也能跑);**抬高 `astrbot_version` 下界**(采用了新版本 AstrBot 才有的 API)也算 MINOR,除非同时伴随下面的 breaking 情形。
- **MAJOR(breaking,团队约定的判定清单)**:
  1. 指令改名或删除(用户已经在用的 `/xxx` 突然不能用了)。
  2. `_conf_schema.json` 配置项的 schema 不兼容变化(字段改名、类型变化、必填项新增且无默认值,导致老配置文件加载失败或语义变化)。
  3. 依赖的 `astrbot_version` 下界抬升到一个"老版本用户完全无法运行"的程度,且没有兼容层——这类"事实上的强制升级"按团队约定算 MAJOR,即使代码改动量很小。

判不准时,宁可判高一级(MAJOR 而非 MINOR),让用户在更新时有心理预期。

## 2. tag → Release → CHANGELOG 操作命令序列

假设已经在 `metadata.yaml` 里把 `version` 改成了 `v1.2.0`,且 `CHANGELOG.md` 已经补上对应的 `## [v1.2.0] - YYYY-MM-DD` 段落,操作顺序:

```bash
# 1. 确认工作区干净,version/CHANGELOG 改动已提交
git add metadata.yaml CHANGELOG.md
git commit -m "chore(release): v1.2.0"

# 2. 打带注释的 tag(-a 而不是轻量 tag,便于附带说明)
git tag -a v1.2.0 -m "v1.2.0"

# 3. 推送提交与 tag
git push origin main
git push origin v1.2.0

# 4. 用 gh CLI 创建 GitHub Release,body 摘录 CHANGELOG 对应段落
gh release create v1.2.0 \
  --title "v1.2.0" \
  --notes "$(sed -n '/## \[v1.2.0\]/,/## \[/p' CHANGELOG.md | sed '$d')"
```

第 4 步的 `sed` 提取逻辑仅作为示例(从 CHANGELOG 里截取当前版本段落到下一个 `## [` 之前);实际是否需要这么精确取决于 CHANGELOG 格式是否规整,更简单的做法是手动把对应段落粘贴进 `--notes`。

## 3. CI YAML 示例 + 逐行注释

以下是可按目标仓库调整的最小 CI 示例；它不是可直接复制的捆绑模板：

```yaml
name: CI
on: [push, pull_request]        # push 和 PR 都跑,尽早发现问题
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4          # 检出代码,固定用 v4(已验证可用的版本)
      - uses: actions/setup-python@v5      # 装 Python,固定用 v5
        with: { python-version: "3.11" }   # AstrBot 生态常见的 Python 版本,按实际插件要求调整
      - run: pip install ruff pytest        # 最小工具链:lint 用 ruff,测试用 pytest
      - run: ruff check .                   # 静态检查,ruff 具体规则配置让位给 python-engineering skill
      - run: python -m py_compile main.py   # 语法编译检查,最低成本的"能不能跑起来"验证
      - run: pytest -q || true              # 跑测试;插件仓库往往测试很薄,先不让测试失败卡死整个 CI
```

**版本号说明**:`actions/checkout@v4`、`actions/setup-python@v5` 是本 skill grounding 中确认可用的版本号,不要因为"更新"的直觉自行改成更高版本号——如果确实需要升级,先去 GitHub Actions 市场核实新版本号存在且兼容,而不是凭印象编。

`pytest -q || true` 这一行的 `|| true` 是有意为之:多数 AstrBot 插件仓库体量小、测试覆盖薄,团队约定阶段性放宽"测试失败即 CI 红"的严格度,重点先卡住 lint 和语法。如果某个仓库的测试已经比较完善,评审时可以建议去掉 `|| true` 让测试真正生效卡门槛。

## 4. 版本同步 drift 的修复流程

当发现 git tag、GitHub Release、`metadata.yaml` 的 `version` 三者不一致时(评审中最常见的发布问题),按以下顺序修复,而不是随便改一个了事:

1. **先确定"事实上的最新版本"应该是哪个**——通常以 CHANGELOG 里最新的条目为准,因为它记录了真实发布的内容。
2. 如果 `metadata.yaml` 的 `version` 落后于已经打出的 tag:更新 `metadata.yaml`,提交一个新 commit(不要改历史 tag 指向的内容,tag 应该不可变)。
3. 如果 tag 落后于 `metadata.yaml`(改了 version 但忘记打 tag/发 Release):按第 2 节的命令序列补打 tag、发 Release。
4. 如果三者都不一致且历史比较混乱:以 `metadata.yaml` 当前值为基准,重新走一遍完整发布流程(改 CHANGELOG → commit → tag → push → Release),把这次作为新的"正确起点",不要试图去修正历史上已经错误发布的旧 tag。
5. 修复后使用目标仓库自己的发布检查（CI 或明确的本地命令）核验 tag 与
   `metadata.yaml` version 一致；若没有自动检查，记录人工核验的依据。

## 5. `astrbot_version` 下界抬升的判断

抬高 `astrbot_version` 下界之前,先确认插件确实用到了旧版本不存在的 API——具体 AstrBot 各版本 API 可用性门槛表**不在本 skill 维护**,交叉引用 `astrbot-plugin-dev` skill 的 `references/api-reference.md` 第 17 节。本 skill 只负责提醒"抬高下界后要不要连带升 MINOR/MAJOR"的判级问题(见第 1 节)。
