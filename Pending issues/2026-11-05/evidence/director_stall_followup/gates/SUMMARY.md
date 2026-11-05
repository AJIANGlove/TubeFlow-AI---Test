# PR #12 门禁（main `746d6bc` vs 分支 `1baebef`）

执行环境：venv `C:\Users\15202\.workbuddy\binaries\python\envs\tubeflow-test`（pytest 9.1.1 / pytest-asyncio 1.4.0 / temporalio 1.34.0）；两个仓库外 worktree，均以 `PYTHONPATH=<wt>/backend/src` 覆盖 editable `.pth`，并已打印 `tubeflow_test.__file__` 确认指向各自 worktree。

| 检查 | main `746d6bc` | 分支 `1baebef` | 判定 |
| --- | --- | --- | --- |
| 后端单元全量 | **2426 passed / 0 failed / 10 skipped** | **2441 passed / 0 failed / 10 skipped** | 分支 +15（PR 新增 `test_project_director_stall_fixes.py`），两侧 0 失败 |
| `test_project_director_workflow.py` | 11 passed | 11 passed | 异步用例正常执行（PR 声明其本机缺 pytest-asyncio 未跑，本环境已补跑） |
| PostgreSQL 集成全量 | **19 failed / 121 passed / 7 skipped** | **19 failed / 121 passed / 7 skipped** | 失败集合逐条相同（19 条 diff 为空） |
| ruff `src tests` | 15 errors | 15 errors | 输出逐字节相同（`RUFF_IDENTICAL`） |
| mypy `src` | 12 errors / 7 files | 12 errors / 7 files | 去行号归一化后逐行相同 |
| `npm run check` | 259 passed / 0 failed | 259 passed / 0 failed | 两侧一致；PR 未改前端（`git diff --stat main pr12 -- app.js styles.css index.html frontend/` 为空） |
| 迁移 | `0047` | 无新增文件 | `git diff --name-status main pr12 -- backend/migrations` 为空 |
| `config.py` | — | 未改动 | 与「无新增配置项」一致 |

日志：`main/{unit.log,workflow_test.log,integration.log,ruff.txt,mypy.txt,npm_check.log}`、`branch/` 同名。

**结论**：无新增失败、无新增 ruff/mypy 条目，可以合并。

**未执行/不适用**：抖动 A/B（需另行授权 8 次 H3 调用，本次不跑）；真实 Provider 调用（本次不建项目、不重生成）。
