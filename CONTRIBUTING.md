# Contributing

感谢你愿意改进 `2718lab-devkit`。

## 开发环境

```powershell
uv sync --frozen
uv run pre-commit install
```

运行质量门禁：

```powershell
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

测试会使用 `CODEX_TASK_TEMP` 创建临时仓库。在本地运行时，请把它指向
仓库外的可写临时目录；不要把临时数据库、checkpoint CAS 或证据目录提交。

## Pull Request

- 一个 PR 只解决一个明确问题。
- 行为修改先写会失败的测试，再做最小实现。
- 更新公开行为或安装方式时同步更新 README 与 CHANGELOG。
- 不要提交 token、私钥、真实聊天内容或本机绝对路径。
- 未公开的安全问题按 [`SECURITY.md`](SECURITY.md) 私下报告，不开公开 Issue。
- commit、push、PR 和 Release 始终是独立授权门。
