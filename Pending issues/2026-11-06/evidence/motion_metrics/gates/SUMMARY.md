# PR #15 合并前门禁汇总（2026-11-06）

**判定：通过 —— 相对 main 无新增失败；新增 10 条单测中 9 条通过、1 条因测试机无 ffmpeg 跳过（跳过不计通过）。可以合并。**

- PR #15 head `da60ef6`，base `36123e9`，2 个提交（`884761b`、`da60ef6`）、10 个文件（`git diff --name-status 36123e9 da60ef6 | wc -l` = 10）。
- 两侧工作树（都在**测试机**）：main `D:/AAA-project/wt-pr15-main` @ `36123e9`、branch `D:/AAA-project/wt-pr15-branch` @ `da60ef6`。
- 两侧一律 `PYTHONPATH=<该侧>/backend/src`，已断言导入路径分别指向各自工作树（`pkg …\wt-pr15-main\…`、`…\wt-pr15-branch\…`）。
- 集成经 SSH 隧道 `127.0.0.1:55436` → 部署机 `127.0.0.1:55432`（真实 Postgres）。

## 1. 依赖重装（分支侧，按 `backend/pyproject.toml`）

`pip install -e ".[dev]"`（Python 3.12.14）。`pip freeze` 前后差异**只有**：

```
> numpy==2.5.4
> opencv-python-headless==5.0.0.93
- -e git+…@36123e9…#egg=tubeflow_ai_test_backend&subdirectory=backend
+ -e …&subdirectory=..\..\..\wt-pr15-branch\backend
```

- 实际版本：`cv2 5.0.0`、`numpy 2.5.4`（`gates/dep-versions.txt`）。
- 其它包**一个未动**（`gates/venv-{before,after,diff}.txt`）。
- 最后一行是 editable 安装指向从 git URL 变为本次工作树路径，属预期。

## 2. 逐项对比

| 检查 | main `36123e9` | 分支 `da60ef6` | 对比结论 |
| --- | --- | --- | --- |
| `ruff check src tests` | Found 15 errors | Found 15 errors | 输出逐字节相同（`diff` 空） |
| `mypy src` | 12 errors in 7 files（206 源文件） | 12 errors in 7 files（**208** 源文件） | 行号归一后逐行相同；唯一差异是「检查源文件数 206→208」（新增两个模块） |
| 单元（全量） | 2472 passed / 10 skipped | **2481 passed / 11 skipped** | +9 passed +1 skipped = 新增 10 条 |
| `tests/unit/test_video_motion_metrics.py` | 文件不存在 | **9 passed, 1 skipped** | 跳过的是 `…:187: ffmpeg not on PATH`（适配器那条）——**按「跳过」记录，不算通过** |
| 集成 | 19 failed / 122 passed / 7 skipped（收集 148） | 19 failed / 122 passed / 7 skipped（收集 148） | 失败 nodeid **逐条相同**；且与上一轮（jitter_ab）的失败集合**完全一致** |
| `npm run check` | 259 / 259 / 0 | 259 / 259 / 0 | 相同 |

原始输出：`gates/{main,branch}/{unit,integration,ruff,mypy,npm}.log|txt`、`gates/{main,branch}/integration_failed.txt`、`gates/branch/motion_metrics_unit.log`。

## 3. 过程说明

- **本轮已知偶发未触发**：`test_sql_concurrent_failover_supersede_creates_exactly_one_successor` 在两侧全量里都通过，无需复跑（上一轮的复跑记录仍在 `../jitter_ab/gates/flaky_rerun.txt`）。
- **收集数的小出入（不影响判据）**：上一轮 main 侧集成通过数为 121、本轮为 122，两侧本轮均为 148 收集。失败集合完全相同，差异只可能来自上一轮某个用例的收集/执行状态；本轮两侧一致，故不构成新增失败。
- **19 条既有失败**与上一轮同一批：12 条缺 `TUBEFLOW_TEST_MINIO_ENDPOINT`、2 条硬编码期望 `0039`（库在 `0047`），其余为共享库状态与容量类；未修复，不在本次范围。

## 4. 未运行 / 未覆盖

- 未在部署机上跑测试（门禁按惯例在测试机跑）。
- 未做真实 Provider 调用（本任务全程不调用任何 Provider）。
- 前端未改，`npm run check` 只跑一次留证，未跑浏览器端到端。
- **适配器那条端到端测试未真正执行**（测试机无 ffmpeg）；它的等价验证放在部署机的复算步骤（`../recompute/`）。
