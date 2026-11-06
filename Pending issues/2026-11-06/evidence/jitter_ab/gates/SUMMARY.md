# PR #14 合并前门禁汇总（2026-11-06）

**判定：通过 —— 相对 main 无新增失败；新增 31 条单测通过；新增 1 条集成用例被收集且通过。可以合并。**

- PR #14 head `ba7dbac`，base `28eda2f`，4 个提交、17 个文件（`git log --oneline 28eda2f..ba7dbac`、`git diff --name-status`）。
- 两侧工作树（都在**测试机**上）：
  - main 侧 `D:/AAA-project/wt-pr14-main` @ `28eda2f`（detached）
  - 分支侧 `D:/AAA-project/wt-pr14-branch` @ `ba7dbac`（detached）
- 运行环境：测试机 `C:\Users\15202\.workbuddy\binaries\python\envs\tubeflow-test`（pytest 9.1.1 / pytest-asyncio 1.4.0 / temporalio 1.34.0）。
  集成测试经 SSH 隧道 `127.0.0.1:55435` → 部署机 `127.0.0.1:55432`，对真实 Postgres 跑（`TUBEFLOW_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55435/tubeflowtest`）。
- 两侧一律设 `PYTHONPATH=<该侧>/backend/src`，避免导入主检出源码。

## 1. 逐项对比

| 检查 | main `28eda2f` | 分支 `ba7dbac` | 对比结论 |
| --- | --- | --- | --- |
| `ruff check` | Found 15 errors | Found 15 errors | 输出逐字节相同（`diff` 空）→ 无新增 |
| `mypy` | 12 errors in 7 files（206 源文件） | 12 errors in 7 files（206 源文件） | 行号归一化后逐条相同 → 无新增 |
| `npm run check`（前端） | 259 tests / 259 pass / 0 fail | 259 tests / 259 pass / 0 fail | 相同；`git diff --stat -- app.js styles.css index.html frontend/` = 0 行 |
| 单元测试（全量） | 2441 passed, 10 skipped | 2472 passed, 10 skipped | +31 = 新增 `test_jitter_cadence_ab.py`，0 失败 |
| 新增单测 `test_jitter_cadence_ab.py` | — | 31 passed | 满足「31 条必须通过」 |
| 集成测试 | 19 failed, 121 passed, 7 skipped | 19 failed, 122 passed, 7 skipped | 失败 nodeid 集合逐条相同（`main/integration_failed.txt` vs `branch/integration_failed.txt`，`diff` 空）；+1 passed = 新增用例 |
| 新增集成用例 `test_manual_intent_experiment_arm_is_refused_on_a_composite_group` | — | collected = 1，未出现在失败清单 | 通过 |

原始输出全部在同目录：`main/{unit,integration,ruff,mypy,npm}.log|txt`、`branch/...`。

## 2. 需要如实说明的一处过程

分支第一次全量单元测试出现 **1 条失败**：

```
FAILED tests/unit/test_storyboard_auto_retry.py::test_sql_concurrent_failover_supersede_creates_exactly_one_successor
tests\unit\test_storyboard_auto_retry.py:1210: in test_sql_concurrent_failover_supersede_creates_exactly_one_successor
    assert all(item.status is GenerationStatus.FAILED for item in results)
```

处理与定性：
1. 该用例在 **main 侧与分支侧各单独复跑 3 次，6/6 全部 passed**（4.72–5.30 s），记录见 `flaky_rerun.txt`。
2. 分支全量**复跑一次**：`2472 passed, 10 skipped, 33 warnings in 112.64s`（`branch/unit_rerun.log`），0 失败。
3. 该用例是 SQLite 并发下的既有偶发（多线程抢同一 attempt 的 supersede），PR #14 未改 `test_storyboard_auto_retry.py` 与并发 failover 路径。

结论：按「相对 main 无新增失败」判据，不构成新增失败；但**首次运行的这条抖动是真实发生过的**，此处保留原记录，不以外推替代。

## 3. 两侧既有的 19 条集成失败（与本次改动无关）

两侧同一集合，主因是测试机环境与历史数据，逐条清单见 `integration_failed.txt`：

| 数量 | 原因 |
| ---: | --- |
| 12 | `KeyError: 'TUBEFLOW_TEST_MINIO_ENDPOINT'` —— 测试机未配置 MinIO 测试端点（`test_project_purge_shared_assets`、`test_staged_upload_protocol` 等） |
| 2 | `assert '0047' == '0039'` —— 用例硬编码期望 alembic 版本 `0039`，库已在 `0047` |
| 1 | `VideoCapacityUnavailable: no compatible H3 node has capacity` |
| 1 | `test_identity_project_postgres`：期望 201，实际 409（库内已有初始化数据） |
| 3 | 其余断言（`assert 1 == 2`、`assert None is not None` 等），分布同上两类用例 |

这些在 main 侧完全一样，因此不因 PR #14 新增；也**未修复**（不在本次范围），此处只记录。

## 4. 未运行 / 未覆盖

- 未在部署机上跑测试（门禁按惯例在测试机跑）。
- 未做真实 Provider 调用（本步只跑测试替身；A/B 的 DeepSeek/H3 调用在第 4 步执行）。
- 前端只跑 `npm run check`（与历次门禁一致），未跑浏览器端到端。
