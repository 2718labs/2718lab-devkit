---
description: 交付前独立审查：按当前 Fast Lane 契约对当前改动做对抗性验证
---

# /2718lab-review

按当前 Fast Lane 契约和受影响模块的验证清单，对当前改动或方案做一次对抗性审查。
只在任务已明确属于 2718lab DevKit 时使用；不要把本命令的任务、缓存或工作流状态带入其他项目。

做法:

1. 先用 `workflow-design` 划定本次 diff、写入边界和验收命令；有可用 Fast Lane 输入时，由 host 编译 inert plan。不要虚构 `subagent_type` 或假定 compiler 会自行启动 agent。
2. 如 host 提供了被验证的独立 reviewer route，就按该 route 派发一个只读审查；否则由协调器本地执行同一清单。审查上下文只包含改动文件、涉及模块和可复现命令。
3. 审查重点：框架 API 幻觉、未经证实的接口、跨框架 API 混入、遗漏的失败路径、已跑验证的真实结果与版本一致性。
4. 对每个发现逐条回应；确认的问题先修复，存疑的问题回到对应模块说明书的 references 或官方文档核实。
5. 所有 critical/major 清零，且受影响自检通过，才可声称交付完成。

这是交付前的最后一道关,别急着收工。
