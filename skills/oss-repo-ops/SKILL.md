---
name: oss-repo-ops
description: 开源仓库运营与 AstrBot 插件市场上架规范。凡是涉及把仓库"发布出去"的任务都必须使用本 skill——为插件/工具仓库补齐或评审 README/LICENSE/CHANGELOG,选许可证(MIT vs AGPL-3.0),做语义化版本、打 tag、发 GitHub Release,配最小 CI(GitHub Actions lint+test),写 issue/PR 模板,或把 astrbot_plugin_* 仓库提交到 AstrBot 插件市场(plugins.astrbot.app 提交流程、16MB 包体限制、metadata.yaml 版本同步)时。上架机制有反直觉的坑(Issue 制自动索引而非 PR 制、version 忌用 2 段号否则被 YAML 当浮点数、repo 建议不带 .git 后缀),即使只是"顺手发个版"也要先查本 skill。只管发布与仓库卫生;写插件代码查 astrbot-plugin-dev,Python 工程化查 python-engineering。Use this skill whenever the user mentions publishing a repo, releasing/tagging versions, licensing, changelogs, GitHub repo hygiene, or submitting a plugin to the AstrBot plugin market.
---

# 开源仓库运营(团队规范版)

本 skill 管**仓库发布与卫生**:README/LICENSE/CHANGELOG 补齐与评审、语义化版本与 GitHub Release、最小 CI、issue/PR 模板、AstrBot 插件市场提交。不管这些:

- 插件业务代码怎么写、`metadata.yaml`/`_conf_schema.json` 完整字段、AstrBot API 用法 → `astrbot-plugin-dev`。
- lint/test 工具链本身怎么选型配置(ruff 规则、pytest 结构、pyproject.toml)→ `python-engineering`。
- MCP 服务器仓库同样适用本 skill 的发布与卫生规则,代码归 `mcp-server-dev`。
- 评审节奏、交付方法论 → `work-methodology`。

配套文件:

- `references/astrbot-market.md` — 完整市场提交流程、字段陷阱、排错。
- `references/repo-hygiene.md` — README/LICENSE(MIT vs AGPL 完整论证)/CHANGELOG/模板设计理由。
- `references/release-workflow.md` — semver 判级、tag→Release→CHANGELOG 操作序列、CI YAML 全文注释、版本 drift 修复。
- `assets/templates/` — README/CHANGELOG/两份 LICENSE 全文/ci.yml/issue 与 PR 模板,可直接复制。
- `scripts/check_release.py` — 交付前发布自检脚本,**必须运行**(见第 5 步)。

## 第 0 条:流程保真(反幻觉纪律,最容易被弱模型违反)

1. AstrBot 市场上架是**Issue 制自动索引**,不是向注册表仓库提 PR。**禁止**编造"fork `AstrBot_Plugins_Collection` 然后提 PR 加一行 JSON"这类流程——不存在。人类入口只有一个:`plugins.astrbot.app` 右下角 `+` → 填表 → 点"提交到 GITHUB"生成预填 Issue → Create。
2. 提交目标仓库随时间变过(旧例子指向 `AstrBot_Plugins_Collection` 的 issue,新文档走主仓库 `AstrBot` 的 issue 流程)。**不要手写 Issue**,一律走 UI 生成 —— UI 把你导向哪个仓库就是哪个,不要自己猜。
3. 注册表 `AstrBotDevs/AstrBot_Plugins_Collection` 只维护机器可读索引(`plugin_cache_original.json`,经 `api.soulter.top/astrbot/plugins` 对外),**不托管代码**;禁止指导用户往这个仓库推代码或提插件本体的 PR。(`plugins.json` 是客户端本地缓存名,不是对外索引 URL,别混。)
4. 不确定的市场规则(人工审核时长、下架申诉流程、tag 分类词表等)**不要编**——写"以 `docs.astrbot.app/dev/star/plugin-publish.html` 为准",不得脑补细节。
5. LICENSE 条款不要凭记忆复述或手打;全文只从 `assets/templates/LICENSE-MIT` / `LICENSE-AGPL-3.0` 复制。
6. GitHub Actions 语法不确定时照抄 `assets/templates/ci.yml`,action 版本号(`actions/checkout@v4`、`actions/setup-python@v5`)不要自由发挥。

## 工作流程

### 第 1 步:判断任务类型

| 任务 | 做法 |
|---|---|
| 新开源一个仓库 | 复制 `assets/templates/` 全套 → 改占位符 → 按第 2 步清单补齐 |
| 发一个新版本 | 第 3 步 release 流程 + 运行 `scripts/check_release.py` |
| 上架/更新 AstrBot 市场 | 第 4 步 + `references/astrbot-market.md` |
| 仓库卫生评审 | 对照第 2 步清单 + `references/repo-hygiene.md`,输出带条款号的问题清单 |

### 第 2 步:仓库卫生基线(硬性规则)

1. README 必备段落:标题+一句话描述、徽章(版本/许可证/astrbot_version 兼容性)、功能/指令列表、配置表(须与 `_conf_schema.json` 一致)、安装方式(放入 `AstrBot/data/plugins/`)、截图/GIF(UI 相关时)、许可证脚注。逐段写作指引见 `references/repo-hygiene.md`。
2. LICENSE 决策(压缩版,**不是法律意见**,拿不准找真人):团队**默认 AGPL-3.0**,与 AstrBot 本体(AGPL-3.0)一致、统一始终落在安全一侧。插件 vendor/派生了 AstrBot 核心代码时 AGPL-3.0 是**强制**(不能破例)。仅当插件是纯 `astrbot.api.*` 调用的独立作品、且工作室明确想让它被最大化嵌入复用时,才**破例**改选 MIT。完整论证与判定问题清单见 `references/repo-hygiene.md`。
3. CHANGELOG 用 Keep-a-Changelog 格式(`Added`/`Changed`/`Fixed`/`Removed`,按版本分段)——插件用户在 WebUI 更新,看不到 diff,只能靠它了解变化。
4. `.gitignore` 必须存在:`__pycache__/`、`.git` 垃圾、虚拟环境等不清理会直接吃掉市场 16MB 包体限额。
5. Issue 模板必须要求填写 AstrBot 版本 + 平台适配器(bug 常常是 adapter-specific);PR 模板需勾选三项:本地 `data/plugins/` 下实测过、`_conf_schema.json` 校验过、无其他框架 API 混入。

### 第 3 步:版本与发布(硬性规则)

1. 遵循语义化版本 `MAJOR.MINOR.PATCH`;git tag 形如 `vX.Y.Z`。
2. **三处版本保持一致**:git tag = GitHub Release tag = `metadata.yaml` 的 `version`。`version` 用 3 段式 `X.Y.Z`(团队建议带 `v` 前缀与 tag 对齐;`v` 非市场强制,纯 `1.0.0` 也接受,但裸 2 段号 `1.0` 会被 YAML 当浮点数、校验挂,务必 3 段或加引号)。这是本生态最容易漂移的点。
3. 用了新 AstrBot API 就要抬高 `astrbot_version` 下界(引号内 PEP 440 格式、不带 `v`);具体门槛表**交叉引用** `astrbot-plugin-dev` 的 api-reference 第 17 节,不在本 skill 复制。
4. 每个 tag 都要发一次 GitHub Release,body 摘录 CHANGELOG 对应段落。
5. 最小 CI(lint+test)照抄 `assets/templates/ci.yml`(核心 job 见下),工具链选型细节(ruff 规则、pytest 结构)让位给 `python-engineering`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install ruff pytest
      - run: ruff check .
      - run: python -m py_compile main.py
      - run: pytest -q || true
```

操作命令序列(`git tag -a` / `gh release create`)与判级细节见 `references/release-workflow.md`。

### 第 4 步:AstrBot 市场提交(压缩版)

1. 插件仓库公开在 GitHub 上。
2. 打开 `plugins.astrbot.app`,点右下角 `+`。
3. 填表:name / desc / author / repo / tags / social_link。
4. 点"提交到 GITHUB"——生成预填 Issue 并跳转。
5. 勾选三个强制承诺项(已充分测试 / 不含恶意代码 / 遵守 GitHub 社区行为准则),点 **Create**。
6. CI/CD 自动处理该 Issue,通过后索引进市场目录(`plugin_cache_original.json`)——全程**不需要**人工提 PR。

硬性约束:

- 打包体积 **≤ 16MB**,超限 CI 自动拒。瘦身手段:压缩图片/音频、剥离 `.git`/`__pycache__`/`node_modules`、加 `.gitattributes`、开一个瘦身的 release 分支。确实合法超限,联系维护者手动处理。
- 仓库名/目录名:全小写、无连字符/空格(会成为插件模块名,硬约束);官方**推荐** `astrbot_plugin_` 前缀(非强制,团队约定遵守)。
- `metadata.yaml` 的 `repo` 字段**建议不带 `.git` 后缀**(官方示例皆不带;schema 据称也接受,但不带更稳)。
- `metadata.yaml` 发布相关字段陷阱(完整字段表交叉引用 `astrbot-plugin-dev`):`version` 用 3 段 `X.Y.Z`(建议带 `v`,非市场强制;忌 2 段号被当浮点数)、`astrbot_version` 引号内且不带 `v`、`name` 等于目录名。

完整提交流程 JSON 字段、两个注册表仓库的历史变迁、体积瘦身完整手段、排错见 `references/astrbot-market.md`。

### 第 5 步:交付前自检

运行本 skill 自带的自检脚本:

```bash
python3 <skill目录>/scripts/check_release.py <仓库目录>
```

**0 个错误才能交付**;警告逐条人工判断。无法运行脚本时手工核对:`metadata.yaml` 存在且 `version` 为 3 段 `X.Y.Z`(建议带 `v`)、与最新 git tag 一致;`repo` 建议不以 `.git` 结尾;`astrbot_version` 带引号不带 `v`;目录名=`name`=全小写模块名(推荐 `astrbot_plugin_` 前缀);`LICENSE`/`README.md`/`CHANGELOG.md`/`.gitignore` 都存在;`CHANGELOG.md` 含当前版本条目;剔除 `.git`/`__pycache__` 后打包体积预估 ≤16MB;`.github/workflows/` 下至少有一个 CI 文件。

## 评审输出格式

```
【强制-违规】3.2 metadata.yaml 的 version 写成 1.0(2 段号被 YAML 解析成浮点数),与 git tag v1.0.0 不一致,市场校验会挂。改成 3 段号 version: v1.0.0(或 1.0.0)。
【要求-建议改】2.1 README 缺少配置表,与 _conf_schema.json 对不上,建议补全。
【通过】LICENSE、.gitignore、CHANGELOG 格式无问题。
```

评审时同样运行 `scripts/check_release.py` 辅助,但人工检查(尤其 LICENSE 选择是否合理、README 是否准确)不能省。
