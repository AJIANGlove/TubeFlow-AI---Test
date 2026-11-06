# PR #16 合并前门禁汇总（2026-11-06）

**判定：通过 —— 相对 main 无新增失败；新增单测 `test_integration_fixture_cleanup.py` 实际运行并通过（1 passed，0 skipped）；`backend/src` 与 `frontend` 零改动。**

- PR #16 head `1051eb6`，base `8d96e41`，**1 个提交**、**9 个文件**（5 改 4 增）。
- `git diff --stat 8d96e41 1051eb6 -- backend/src frontend` → **空**（只动 `backend/tests/**`、`docs/**`、`Pending issues/**`）。
- 两侧工作树（都在**测试机**）：`D:/AAA-project/wt-pr16-main` @ `8d96e41`、`D:/AAA-project/wt-pr16-branch` @ `1051eb6`；均设 `PYTHONPATH=<该侧>/backend/src`。

## 1. 逐项对比（测试机）

| 检查 | main `8d96e41` | 分支 `1051eb6` | 对比结论 |
| --- | --- | --- | --- |
| `ruff check src tests` | Found 15 errors | Found 15 errors | 输出**逐字节相同** |
| `mypy src` | 12 errors in 7 files（208 源） | 12 errors in 7 files（208 源） | 输出**逐字节相同**（PR 只动 tests，mypy 只看 src） |
| 单元（全量） | 2481 passed / 11 skipped | **2482 passed / 11 skipped** | +1 passed = 新增那一条，0 失败 |
| `tests/unit/test_integration_fixture_cleanup.py` | 文件不存在 | **1 passed, 0 skipped**（3.29 s） | **实跑通过**（测试机有 alembic，未触发 `importorskip`） |
| 集成 | 19 failed / 122 passed / 7 skipped | 19 failed / 122 passed / 7 skipped | 失败 nodeid **逐条相同**，且与上一轮完全一致 |
| `npm run check` | — | — | **未跑**（前端未改；按任务要求可省） |

原始输出：`gates/{main,branch}/{unit,integration,ruff,mypy}.log|txt`、`gates/branch/fixture_cleanup_unit.log`、`gates/{main,branch}/integration_failed.txt`。

## 2. 集成跑完后两个库的 `contract\_%`（部署机，2026-11-06T06:25:31Z）

| 库 | 位置 | 结果 |
| --- | --- | --- |
| `tubeflowtest` | 容器 `tubeflow-contract-pg`（角色 `postgres`，宿主 127.0.0.1:55432） | **0 行** |
| `tubeflow` | 容器 `tubeflowai-test-postgres-1`（角色 `tubeflow`，业务库） | **1 行**：`contract_be30c85aac3347ffbf3bc45bc6ea12de` |

- **测试库没有新增泄漏**：main 侧本轮仍用旧夹具，但没有用例在 `alembic upgrade` 阶段失败，所以没有新 schema。
- 任务给的预期是「测试库只有 `contract_be30c85a…` 一个」；实际是**测试库 0 个、业务库 1 个**。这个差异是本任务第 3 步停删的直接原因，见 `../README.md`。

## 3. 已知偶发

`test_sql_concurrent_failover_supersede_creates_exactly_one_successor` 两侧全量里都通过，**未触发复跑**。

## 4. 未运行 / 未覆盖

- `npm run check` **未跑**（前端未改）。
- 新单测只在**分支侧**存在；main 侧无此文件，故不做对侧对照。
- 未在部署机上跑测试（门禁按惯例在测试机）。
- 未验证「新单测在旧夹具上会失败」——那需要把测试文件复制进 main 工作树，属额外写入，本次未做；只记录了它在分支上实跑通过。
