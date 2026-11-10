# 交接包 v1 门禁汇总 — 测试机，2026-11-10

结论：**两侧无新增失败；两个单跑文件全过（57 / 31）。门禁通过，可 `--no-ff` 合并。**

- 分支 `claude/motion-naturalness-v1-20261109`，基于 main `528bc29`；`git am` 应用两个补丁，提交 `f0992e1`（文档 + 第 1 轮脚本）、`28a2e34`（`edited_v1` 臂 + 第 2 轮工具）。两侧均快进干净，无手工改动。
- `git diff --stat main` = **14 个文件、+1612 / −16**，与 `PROMPT.md` 第 2.3 条列出的文件逐条一致（5 个 `MOTION_NATURALNESS` 文件、5 个后端源文件、1 个新测试、3 份文档）。
- 两侧同机同命令；均显式 `PYTHONPATH=<被测树>/backend/src`（venv 的 editable 指向别的工作树，不靠它）。

## 1. 单元（`python -m pytest tests/unit -q -rf --basetemp=<全新目录>`）

| 侧 | 结果 | 用时 |
| --- | --- | --- |
| main `528bc29` | **2509 passed / 11 skipped / 0 failed** | 96.46 s |
| 分支 `28a2e34` | **2566 passed / 11 skipped / 0 failed** | 88.12 s |

+57 = 新增 `test_prompt_edit_ab.py` 的 57 条；skipped 两侧同为 11 条。

## 2. 两个指定文件单跑

| 文件 | 侧 | 结果 |
| --- | --- | --- |
| `tests/unit/test_prompt_edit_ab.py` | 分支 | **57 passed / 0 failed**（含 2 条 HTTP 用例；Claude 云端缺 FastAPI 未跑的两条） |
| `tests/unit/test_jitter_cadence_ab.py` | 分支 | **31 passed / 0 failed** |
| `tests/unit/test_jitter_cadence_ab.py` | main | **31 passed / 0 failed**（旧臂行为不变） |

## 3. 集成（真实 Postgres 迁移 + 测试 MinIO）

- 环境：`TUBEFLOW_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:55441/tubeflowtest`（SSH 隧道 → 部署机 `tubeflow-contract-pg` 的 55432）、`TUBEFLOW_TEST_MINIO_ENDPOINT=127.0.0.1:59100`（隧道 → 部署机 `tubeflow-contract-minio` 的 59000）、凭据取自 `runtime/operations/contract-minio-20261106/credentials.txt`；未设 `TUBEFLOW_TEST_TEMPORAL_TARGET`（与基线一致）。
- 开跑前测试 MinIO `list_buckets()` = `[]`。

| 侧 | 结果 | 用时 |
| --- | --- | --- |
| main `528bc29` | **146 passed / 3 skipped / 0 failed** | 257.85 s |
| 分支 `28a2e34` | **146 passed / 3 skipped / 0 failed** | 257.39 s |

3 条跳过（两侧相同，`-rs` 原文）：

| # | 用例位置 | 原因 |
| ---: | --- | --- |
| 1 | `tests/integration/test_narration_temporal.py:63` | isolated Temporal target is not configured |
| 2 | `tests/integration/test_narration_temporal.py:109` | isolated Temporal and PostgreSQL are not configured |
| 3 | `tests/integration/test_real_ffmpeg_composition.py:31` | FFmpeg integration fixture requires local ffmpeg and ffprobe |

## 4. 静态检查

| 检查 | main | 分支 | 判定 |
| --- | --- | --- | --- |
| `ruff check . --output-format=concise` | 18 errors | 18 errors | 逐条 diff **为空**（`ruff_diff.txt` 0 行） |
| `mypy src` | 12 errors / 7 files / 208 源 | 12 errors / 7 files / 208 源 | 排序归一后 1 处差异，**同一条错误的行号位移**（`persistence.py` 914 → 957，因新代码插在其上方），无新增错误 |
| `npm` | **未跑** | **未跑** | 前端零改动（`git diff --stat main` 无前端文件） |

## 5. 跑后只读复核

| 检查 | 结果 |
| --- | --- |
| 测试 MinIO 桶 | **`[]`（空）**，无遗留 |
| 测试库临时 schema | `contract\_%` **0**、`narration\_authority\_%` **0**、`voice\_race\_%` **0**、`identity\_test\_%` **0**、`alembic\_url\_%` **0** |
| 对象存储守卫误报 | 两侧集成日志 `holds the application bucket` **各 0 命中** |

## 6. 判据

- 分支相对 main 无新增失败、无新增跳过，两个单跑文件全过 ⇒ **合并（`--no-ff`）**。
- 全程未调用任何 Provider、未改 `.env`、未跑 `npm`、未碰 12 个保护项目。

## 7. 原始日志

`main/{unit,integration,jitter_cadence_ab,ruff,mypy}.log`、`branch/{unit,integration,jitter_cadence_ab,prompt_edit_ab,ruff,mypy}.log`、`ruff_diff.txt`、`mypy_{main,branch}_sorted.txt`、`mypy_diff.txt`。
