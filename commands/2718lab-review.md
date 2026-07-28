---
description: 交付前红队自检:按 work-methodology 对当前改动做对抗性审查(调 2718lab-redteam 子代理)
---

# /2718lab-review

按 `work-methodology` 的「红队 / 交付前验证」纪律,对当前改动或方案做一次对抗性审查。

做法:

1. 用 `2718lab-redteam` 子代理(Task/Agent 工具,`subagent_type: 2718lab-redteam`)审查本次改动。给它足够上下文:改了哪些文件、涉及哪个框架(AstrBot / MCP / Python)。
2. 审查重点由子代理执行:框架 API 幻觉、未核实的接口、跨框架 API 混入、该跑的自检脚本是否跑过且 0 错误、版本一致性。
3. 收到缺陷清单后,**正面回应每一条**,不略过;确认为真的当场修,存疑的去对应 skill 的 references 或官方文档核实。
4. 全部 critical/major 清零、自检脚本 0 错误,才可说"搞定了"。

这是交付前的最后一道关,别急着收工。
