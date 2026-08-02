#!/usr/bin/env python3
"""2718lab · oss-repo-ops 发布前自检。

用法:
    python3 check_release.py <仓库目录>

只有 0 个错误(ERROR)才允许交付/上架;警告(WARN)逐条人工判断。
本脚本核对的是 AstrBot 插件仓库「上架/发版」会踩的机械项,与 astrbot-plugin-dev
的 validate_plugin.py(校验插件代码本身)互补,不重叠。

设计要点:优先用 PyYAML 解析 metadata.yaml —— 这样能自然暴露最阴的坑:
`version: 1.0` 会被 YAML 解析成浮点数而非字符串。没装 PyYAML 时退回正则解析,
校验能力略降但仍可用。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

IDENT_RE = re.compile(r"^astrbot_plugin_[a-z0-9_]+$")
SEMVER_V_RE = re.compile(r"^v\d+\.\d+\.\d+([-+.].+)?$")
MAX_ZIP_BYTES = 16 * 1024 * 1024  # 市场硬上限 16MB
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

errors: list[str] = []
warnings: list[str] = []


def err(code: str, msg: str) -> None:
    errors.append(f"[{code}] {msg}")


def warn(code: str, msg: str) -> None:
    warnings.append(f"[{code}] {msg}")


def raw_scalar(text: str, key: str) -> str | None:
    """从原始 YAML 文本抓一个顶层标量字段的原样值(含引号)。"""
    m = re.search(rf"(?m)^{re.escape(key)}\s*:\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else None


def load_metadata(repo: Path):
    p = repo / "metadata.yaml"
    if not p.exists():
        err("META", "metadata.yaml 缺失 —— 市场提交必需")
        return None, None
    text = p.read_text(encoding="utf-8")
    data = None
    try:
        import yaml  # type: ignore

        try:
            data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            err("META", f"metadata.yaml 不是合法 YAML: {e}")
    except ImportError:
        warn("META", "未安装 PyYAML,退回正则解析(pip install pyyaml 可获得完整校验,尤其是 version 浮点数陷阱)")
    return data, text


def check_version(data, text: str) -> str | None:
    """返回规整后的 version 字符串(如 v1.2.0),用于后续 tag/CHANGELOG 比对。"""
    if data is not None and isinstance(data, dict) and "version" in data:
        v = data["version"]
        if not isinstance(v, str):
            err("VER", f"metadata.yaml 的 version 被解析成 {type(v).__name__}({v!r}) —— 非字符串会让市场校验挂。"
                       "真陷阱是 2 段号(裸 1.0 被 YAML 当浮点数):改用 3 段式 X.Y.Z,或给值加引号。")
            return None
        version = v
    else:
        raw = raw_scalar(text, "version") if text else None
        if raw is None:
            err("VER", "metadata.yaml 缺 version 字段")
            return None
        version = raw.strip("\"'")

    # v 前缀是官方模板约定(helloworld 用 v1.3.0),推荐但非强制;市场同样接受纯 3 段号。
    bare = version[1:] if version.startswith("v") else version
    if not version.startswith("v"):
        warn("VER", f"version={version!r} 未带 v 前缀 —— 市场接受纯 3 段号,团队建议 vX.Y.Z 以便与 git tag / Release 三处一致")
    if not re.match(r"^\d+\.\d+\.\d+([-+.].+)?$", bare):
        warn("VER", f"version={version!r} 不是标准 MAJOR.MINOR.PATCH(2 段号会被 YAML 当浮点数),确认是否有意为之")
    return version


def check_astrbot_version(data, text: str) -> None:
    if data is not None and isinstance(data, dict) and "astrbot_version" in data:
        av = data["astrbot_version"]
        if not isinstance(av, str):
            err("AVER", f"astrbot_version 被解析成 {type(av).__name__}({av!r}) —— 必须加引号成字符串")
            return
        value = av
    else:
        raw = raw_scalar(text, "astrbot_version") if text else None
        if raw is None:
            warn("AVER", "metadata.yaml 未声明 astrbot_version(建议按用到的 API 明确下界)")
            return
        if not (raw.startswith('"') or raw.startswith("'")):
            err("AVER", f"astrbot_version 值 {raw!r} 未加引号 —— 区间字符串必须带引号,否则 YAML 解析异常")
        value = raw.strip("\"'")

    if value.lstrip().startswith("v"):
        err("AVER", f"astrbot_version={value!r} 不应带 v 前缀(与 version 字段正好相反,PEP 440 区间如 \">=4.16,<5\")")


def check_repo_field(data, text: str) -> None:
    r = None
    if data is not None and isinstance(data, dict):
        r = data.get("repo")
    if r is None and text:
        raw = raw_scalar(text, "repo")
        r = raw.strip("\"'") if raw else None
    if r is None:
        warn("REPO", "metadata.yaml 未声明 repo 字段")
        return
    if str(r).endswith(".git"):
        warn("REPO", f"repo={r!r} 以 .git 结尾 —— 官方示例一律不带,建议去掉(市场 schema 据称也接受 .git,但不带更稳妥)")


def check_name(data, text: str, repo: Path) -> None:
    n = None
    if data is not None and isinstance(data, dict):
        n = data.get("name")
    if n is None and text:
        raw = raw_scalar(text, "name")
        n = raw.strip("\"'") if raw else None
    if n is None:
        err("NAME", "metadata.yaml 缺 name 字段")
        return
    ns = str(n)
    if not re.match(r"^[a-z0-9_]+$", ns):
        err("NAME", f"name={n!r} 只能是小写字母/数字/下划线(不能有连字符/大写/空格)—— 它会成为插件加载的模块名")
    elif not ns.startswith("astrbot_plugin_"):
        warn("NAME", f"name={n!r} 缺 astrbot_plugin_ 前缀 —— 官方推荐使用,团队约定建议遵守")
    dirname = repo.resolve().name
    if str(n) != dirname:
        warn("NAME", f"name={n!r} 与目录名 {dirname!r} 不一致(市场要求相等;若目录只是本地检出名可忽略)")


def check_required_files(repo: Path) -> None:
    required = ["README.md", "CHANGELOG.md", ".gitignore"]
    for f in required:
        if not (repo / f).exists():
            err("FILE", f"缺少 {f}")
    # LICENSE 可能是 LICENSE / LICENSE.md / LICENSE.txt
    if not any((repo / c).exists() for c in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
        err("FILE", "缺少 LICENSE")


def check_changelog_has_version(repo: Path, version: str | None) -> None:
    p = repo / "CHANGELOG.md"
    if not p.exists() or not version:
        return
    body = p.read_text(encoding="utf-8", errors="replace")
    bare = version[1:] if version.startswith("v") else version
    if version not in body and bare not in body:
        warn("CHLOG", f"CHANGELOG.md 未见当前版本条目({version} 或 {bare}) —— 用户在 WebUI 更新只能靠它")


def check_ci(repo: Path) -> None:
    wf = repo / ".github" / "workflows"
    if not wf.is_dir() or not any(wf.glob("*.y*ml")):
        warn("CI", ".github/workflows/ 下没有 CI 文件(建议照抄 assets/templates/ci.yml)")


def check_size(repo: Path) -> None:
    total = 0
    for p in repo.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    mb = total / (1024 * 1024)
    if total > MAX_ZIP_BYTES:
        err("SIZE", f"剔除 .git/__pycache__ 等后约 {mb:.1f}MB,超市场 16MB 上限,CI 会自动拒。先瘦身(见 references/astrbot-market.md 第3节)")
    elif total > MAX_ZIP_BYTES * 0.8:
        warn("SIZE", f"体积约 {mb:.1f}MB,逼近 16MB 上限,留意")


def check_tag_consistency(repo: Path, version: str | None) -> None:
    if not version:
        return
    if not (repo / ".git").exists():
        return
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "tag", "--points-at", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        warn("TAG", "无法调用 git 校验 tag 一致性(git 不可用?)")
        return
    if out.returncode != 0:
        return
    tags = [t.strip() for t in out.stdout.splitlines() if t.strip()]
    if not tags:
        warn("TAG", f"当前 HEAD 没有 tag —— 发版时记得 git tag {version} 并保持与 metadata.version 一致")
    elif version not in tags:
        warn("TAG", f"metadata.version={version} 不在 HEAD 的 tag {tags} 里 —— 三处版本(tag/Release/metadata)必须一致")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1])
    if not repo.is_dir():
        print(f"目录不存在: {repo}")
        return 2

    data, text = load_metadata(repo)
    version = check_version(data, text) if text is not None else None
    if text is not None:
        check_astrbot_version(data, text)
        check_repo_field(data, text)
        check_name(data, text, repo)
    check_required_files(repo)
    check_changelog_has_version(repo, version)
    check_ci(repo)
    check_size(repo)
    check_tag_consistency(repo, version)

    print(f"== check_release: {repo} ==")
    for e in errors:
        print(f"ERROR  {e}")
    for w in warnings:
        print(f"WARN   {w}")
    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        print("发布被阻断:先清零 ERROR。")
        return 1
    print("机械项通过。仍需人工确认:LICENSE 选择是否合理、README 是否与实际一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
