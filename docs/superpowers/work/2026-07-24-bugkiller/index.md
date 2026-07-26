# Bugkiller Work Index

## Shared Contracts

- `contracts/orchestrator-api.md`: 通用任务图、租约、上下文投影和 MCP 工具契约。
- `contracts/bugkiller-policy.md`: Bugkiller 状态、风险、模型路由和语言边界。
- `contracts/host-boundaries.md`: Codex sandbox、审批、Git/GitHub 和数据目录边界。

## Tasks

| Card | Status | Depends on | Write conflict |
|---|---|---|---|
| `tasks/ORCH-01.md` | done | none | none |
| `tasks/ORCH-02.md` | done | ORCH-01 | orchestrator store |
| `tasks/ORCH-03A.md` | done | ORCH-02 | orchestrator store |
| `tasks/ORCH-03.md` | done | ORCH-01, ORCH-03A | orchestrator service |
| `tasks/ORCH-04A.md` | done | ORCH-03A | mailbox store |
| `tasks/ORCH-04.md` | done | ORCH-03, ORCH-04A | messaging service |
| `tasks/BK-01.md` | done | none | none |
| `tasks/BK-02.md` | done | BK-01 | bugkiller adapters |
| `tasks/BK-03.md` | done | ORCH-04, BK-01 | skill and plugin agent assets |
| `tasks/BK-04.md` | done | ORCH-04, BK-01 | store/service integration |
| `tasks/BK-05.md` | done | BK-02, BK-04 | server.py |
| `tasks/BK-06.md` | done | BK-03, BK-05 | manifests/docs/current config refresh |

## Dispatch

- Current wave: complete.
- Main coordinator owns shared contracts and integration decisions.
- Max active agents: 3; max active writer per write scope: 1.
- Agent context: one card plus only contracts named in its Context section.
- No automatic reviewer for low-risk cards.
- Dangerous or irreversible work stops before execution and asks the user.
- Git commit, push, and PR remain separate manual gates.
