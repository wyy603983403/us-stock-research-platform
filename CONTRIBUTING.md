# 贡献约定

- 每项研究先写 `research/<name>/study.yml`（问题、假设、数据、参数、基准、验证方式、局限），再取数、再回测。
- 看过结果后再改参数 = 新版本研究；失败的研究写进 `docs/rejected.md`，不删除。
- 提交前本地跑：`pytest && ruff check . && ruff format --check . && mypy src`。
- 数据、快照、回测产物不进 Git（`data/`、`artifacts/`）。
