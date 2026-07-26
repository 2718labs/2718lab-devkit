#!/usr/bin/env python3
"""2718lab devkit hook - Codex PostToolUse(Edit|Write).

编辑 AstrBot 插件的 metadata.yaml 时,非阻断地提醒几个高频上架陷阱
(与 oss-repo-ops 的 check_release.py 同源判据):
  - version 用 2 段号(裸 1.0 被 YAML 当浮点数 → 市场校验挂)
  - repo 带 .git 后缀
  - astrbot_version 未加引号 / 误带 v 前缀

设计:纯标准库、无第三方依赖;**永远 exit 0,绝不阻断**;只在真发现陷阱时,
经 PostToolUse 的 systemMessage 通道说一句提醒。
"""

import json
import re
import sys


def analyze(text: str):
    issues = []

    m = re.search(r"(?m)^\s*version\s*:\s*(.+?)\s*$", text)
    if m:
        raw = m.group(1).strip()
        val = raw.strip("\"'")
        quoted = raw[:1] in ("'", '"')
        if re.fullmatch(r"\d+\.\d+", val) and not quoted:
            issues.append(
                f"version: {raw} 是 2 段号,会被 YAML 当浮点数(非字符串)→ 市场校验会挂。"
                "改 3 段式 X.Y.Z(可带 v 前缀)或给值加引号。"
            )

    m = re.search(r"(?m)^\s*repo\s*:\s*(.+?)\s*$", text)
    if m and m.group(1).strip().strip("\"'").endswith(".git"):
        issues.append("repo 以 .git 结尾 —— 官方示例一律不带,建议去掉。")

    m = re.search(r"(?m)^\s*astrbot_version\s*:\s*(.+?)\s*$", text)
    if m:
        raw = m.group(1).strip()
        if raw[:1] not in ("'", '"'):
            issues.append(
                "astrbot_version 未加引号 —— 区间字符串要带引号,否则 YAML 解析异常。"
            )
        elif raw.strip("\"'").lstrip().startswith("v"):
            issues.append("astrbot_version 不应带 v 前缀(与 version 字段正好相反)。")

    return issues


def main():
    data = json.load(sys.stdin)
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ""
    if not path.replace("\\", "/").lower().endswith("metadata.yaml"):
        return

    text = ""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ti.get("content") or ""
    if not text:
        return

    issues = analyze(text)
    if not issues:
        return

    msg = (
        "【2718lab metadata 体检】"
        + " ".join(issues)
        + " 详见 oss-repo-ops / astrbot-plugin-dev skill。"
    )
    out = {"systemMessage": msg}
    print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 钩子绝不阻断正常编辑:任何异常都吞掉
        pass
    sys.exit(0)
