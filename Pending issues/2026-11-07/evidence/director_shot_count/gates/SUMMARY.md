# 导演镜头数拒收重试提示（§53）门禁汇总 — 2026-11-07，测试机

结论：**门禁全绿，无新增失败，可合并。**

- 分支 `claude/director-shot-count-notice-20261107`，基于 main `b3b244a`，`git am` 应用补丁 2 个提交（重写为 `3483573`、`7529913`）。
- `git diff --stat main` = 14 个文件、1424 插入 / 3 删除；**产品代码只有 `backend/src/tubeflow_test/application/project_director.py`**（+51/−2），其余为新测试、`Pending issues/` 文档与 2 份规划文档。

## 0. 前置只读

| 项 | 结果 |
| --- | --- |
| `git apply --check`（应用前） | 通过，14 个文件无冲突 |
| `git am` | 退出 0，2 个提交；工作区未跟踪项仍只有 2 条既有 P0 文件 + 本次证据目录 |
| 导入来源（关键） | venv 的 `__editable__…pth` 指向 `D:\AAA-project\wt-pr15-branch\backend\src`；**两侧均显式 `PYTHONPATH=<被测树>/backend/src`**，`import-origin.txt` 记录实测来源（main 侧 `wt-dsc-main-20261107`、分支侧本仓库） |

## 1. 单元（两侧同机同命令：`python -m pytest tests/unit -q -rf --basetemp=<全新目录>`）

| 侧 | 结果 | 用时 |
| --- | --- | --- |
| main `b3b244a` | **2500 passed / 11 skipped / 0 failed** | 94.65 s |
| 分支 | **2509 passed / 11 skipped / 0 failed** | 92.33 s |

+9 = 新增文件的 9 条，两侧 skip 均为 11 条。

- 新文件 `tests/unit/test_project_director_shot_count_notice.py` 单跑：**9 passed, 0 skipped**（0.68 s）。
- 4 条 SQL 用例（Claude 云端因缺 SQLAlchemy 未能运行）：**4 passed**（2.71 s），逐条 PASSED：
  - `test_project_director.py::test_sql_reclaim_expired_units_scopes_terminal_runs_only`
  - `test_project_director.py::test_sql_find_materialization_tolerates_orphan_row`
  - `test_project_director.py::test_materialization_ready_requires_candidate_rows`
  - `test_project_director_stall_fixes.py::test_sql_unit_mapping_round_trips_the_diagnostics`
- 首次跑曾出现 325 个 `tmp_path` fixture ERROR：`--basetemp` 的父目录 `…/envs/tubeflow-test/Temp` 不存在（`FileNotFoundError WinError 3`）。建父目录后重跑得上述 2500P/11S；该失败属环境问题，已覆盖日志。

## 2. 集成（带测试 MinIO，两侧各一次完整跑）

- 隧道：本机 `55441 → 部署机 127.0.0.1:55432`（`tubeflowtest`）、`59100 → 127.0.0.1:59000`（`tubeflow-contract-minio`）。
- 环境：`TUBEFLOW_TEST_POSTGRES_URL` + `TUBEFLOW_TEST_MINIO_ENDPOINT=127.0.0.1:59100` + 凭据取自 `runtime/operations/contract-minio-20261106/`；**未设** `TUBEFLOW_TEST_TEMPORAL_TARGET`（与基线一致）。
- 开跑前：59100 的 `list_buckets()` 为空、无 `tubeflow-private`。

| 侧 | 结果 | 用时 |
| --- | --- | --- |
| main `b3b244a` | **146 passed / 3 skipped / 0 failed** | 283.50 s |
| 分支 | **146 passed / 3 skipped / 0 failed** | 273.31 s |

3 条跳过（两侧相同，`-rs` 原文）：

| # | 用例位置 | 原因 |
| ---: | --- | --- |
| 1 | `tests/integration/test_narration_temporal.py:63` | isolated Temporal target is not configured |
| 2 | `tests/integration/test_narration_temporal.py:109` | isolated Temporal and PostgreSQL are not configured |
| 3 | `tests/integration/test_real_ffmpeg_composition.py:31` | FFmpeg integration fixture requires local ffmpeg and ffprobe |

## 3. 静态检查（两侧同机同命令）

| 检查 | main | 分支 | 判定 |
| --- | --- | --- | --- |
| `ruff check . --output-format=concise` | 18 errors | 18 errors | 逐条 diff **为空** |
| `mypy src` | 12 errors / 7 files / 208 源 | 12 errors / 7 files / 208 源 | 排序归一后 **逐条 diff 为空**（原始输出顺序不同，属 mypy 报错顺序） |
| `npm` | **未跑**（前端零改动） | 同 | — |

说明：ruff 默认输出为带源码片段的 full 格式，为便于逐条 diff 两侧统一加 `--output-format=concise`；full 输出另存 `gate-ruff-main-full.txt`。18 条与 C1（2026-11-07）记录的集合逐条相同。

## 4. 跑后只读复核

| 检查 | 结果 |
| --- | --- |
| 测试 MinIO 桶 | **`[]`（空）**，无遗留 |
| 测试库临时 schema | `contract\_%` **0**、`narration\_authority\_%` **0**、`voice\_race\_%` **0**、`identity\_test\_%` **0** |
| 对象存储守卫误报 | 两侧日志 `holds the application bucket` **各 0 命中** |

## 5. 判据

- 无新增失败、无新增跳过、跳过集合与基线一致 ⇒ **合并（--no-ff）**。
- 全程未调用任何 Provider、未改 `.env`、未触发生成。
