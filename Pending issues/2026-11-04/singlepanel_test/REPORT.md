# PR #4 重跑验证报告（2026-11-04）

项目：`AJIANGlove/TubeFlow-AI---Test`，PR #4 `Default new projects to one cropped panel per shot (§46)`。
分支：`claude/single-panel-default-v2`，head `2a0b33f35aa8c461030ad1e6bdec8bcd061cd2ac`（含修复提交 `2a0b33f`，parent `a969e13d`）。
验收依据：`FINAL_ACCEPTANCE_CRITERIA.md §42`、`USER_DECISIONS.md §46`。

## 结论

| 门禁 | 结果 | 说明 |
| --- | --- | --- |
| Gate A 代码检查 | **PASS** | ruff/mypy 无新增；集成无 branch-only 失败；单元仅 1 个 safe-delete shim 例外 |
| Gate 1 离线裁切 | **FAIL** | 515 条中 406 通过、109 失败，通过率 **78.835% < 90%** |
| Gate 2 部署观察 | **未执行** | Gate 1 失败即停止 |
| Gate 3/4 新项目 | **未执行** | 未新建项目、未产生 Provider 调用 |
| Gate 5 视觉验收 | **未执行** | 未导出新项目证据 |

PR #4 已合并到 main：`3eac2ba Merge PR #4: single-panel default v2 (grid-v2 + crop-grid-v2)`，已推送 `origin/main`。
生产未部署：仍为 `sha256:8278db3ed79d93f24d3e7f644884022d3926649790f7bff18f85cc2fdd4e0567`；`.env`、compose、服务均未改。
`d725172f` 未暂停、未恢复、未干预。

## Gate A：代码检查（PASS）

同口径在 main `f5b743c` 与 PR `2a0b33f` 上执行；外部 venv、`backend/`、fresh `--basetemp`。

| 检查 | main | PR | 差异 |
| --- | --- | --- | --- |
| 单元测试 | 5 failed / 2226 passed / 10 skipped | 6 failed / 2313 passed / 10 skipped | 仅 1 个 branch-only，见下 |
| ruff `src tests` | 15 errors | 15 errors | 错误集合完全一致（diff 为空） |
| mypy `src` | 12 errors / 7 files | 12 errors / 7 files | 错误集合完全一致（diff 为空；`panel_images.py:557` 已消失） |
| PostgreSQL 集成 | 19 failed / 119 passed / 7 skipped | 19 failed / 119 passed / 7 skipped | 失败集合完全一致（diff 为空） |

branch-only 单元失败：

```text
tests/unit/test_storyboard_batch.py::test_generate_skips_succeeded_without_force_and_regenerates_with_force
```

- 全量：失败，堆栈为 `_cleanup_temp` → `_safe_path_unlink` → `SAFE_DELETE_BULK_CONFIRM_REQUIRED` → `SystemExit: 1`。
- 单跑：`1 passed, 2 warnings in 4.65s`。
- 按本次任务口径，该 safe-delete shim 例外不阻断。

说明：ruff/mypy 在 main 本身有既有错误（15 / 12），PR 未新增；集成测试 19 个失败为缺 MinIO/Temporal/ffmpeg 的环境基线，main 与 PR 完全一致。未把任何未通过项描述为通过。

## Gate 1：离线裁切（FAIL）

### 范围与前提

- `public.storyboard_generations` 中 `status='SUCCEEDED'` 且 `input_json->>'prompt'` 不含 `Caption:`：**515 条 / 6 个项目**（T0 快照）。
- 13 张 `Pending issues/2026-11-04/evidence/shortproj/sb_1..13.png` 与项目 `fa3a0b30` 当前故事板原图：**13/13 sha256 + 字节数一致**。

### 调用

每条读取 MinIO 原始字节，校验 sha 后调用：

```python
derive_shot_panels(
    content,
    source_sha256=<sha of bytes>,
    panel_count=<number of shots in input_json.shots>,
    aspect_ratio="16:9",
    derivation={"version": "crop-grid-v2"},
    layout_version="grid-v1",
)
```

### 总体结果

| 指标 | 值 |
| --- | ---: |
| total | 515 |
| passed | 406 |
| failed | 109 |
| pass rate | **78.835%** |
| 门槛 | ≥90%（≥464） |
| 结论 | **FAIL** |

### 失败码

| failure_code | 数量 |
| --- | ---: |
| `shot_panel_gutter_not_found` | 66 |
| `shot_panel_layout_mismatch` | 42 |
| `shot_panel_gutter_ambiguous` | 1 |
| 合计 | 109 |

### 按项目

| 项目 | total | passed | failed | 通过率 |
| --- | ---: | ---: | ---: | ---: |
| The Wind That Decides Whether India Eats (`d725172f`) | 152 | 141 | 11 | 92.76% |
| How Did Sailors Survive Scurvy and Storms at Sea? (`765dca11`) | 147 | 121 | 26 | 82.31% |
| Egypt Is Just a River With a Flag (`146ae08c`) | 112 | 93 | 19 | 83.04% |
| When Did Ancient Humans Start Hiding in Caves… (`17d2b9e9`) | 89 | 36 | 53 | 40.45% |
| 사장님 없는 무인 아이스크림 가게… (`fa3a0b30`) | 13 | 13 | 0 | 100% |
| “과일에 설탕만 입히면 된다더니…” (`31e00d87`) | 2 | 2 | 0 | 100% |

### 按镜数

| shots | total | passed | failed | 通过率 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 59 | 59 | 0 | 100% |
| 2 | 157 | 114 | 43 | 72.61% |
| 3 | 161 | 114 | 47 | 70.81% |
| 4 | 86 | 77 | 9 | 89.53% |
| 5 | 34 | 28 | 6 | 82.35% |
| 6 | 18 | 14 | 4 | 77.78% |

Gate 1 失败，按任务规则停止：未进入 Gate 2，未部署，未创建新项目，未产生任何图片/视频/LLM Provider 调用。

## 部署与生产状态

- 未改部署机 `.env`；`TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS` 未写入。
- 未改部署机 `docker-compose.yml`；三处镜像钉仍为 `8278db3e…`。
- web/worker 未重建；生产代码仍为旧镜像，PR #4 的 v2 逻辑未在生产运行。
- 未执行任何数据库迁移；`alembic` 未变（部署前为 `0046`）。
- 未新建项目；Gate 3/4/5 全部未执行。

## 证据

```
Pending issues/2026-11-04/singlepanel_test/
├── REPORT.md
├── gateA/
│   ├── gateA_summary.json
│   ├── rerun-main-unit.log / rerun-pr4-unit.log / rerun-pr4-one.log / rerun-merge-unit.log
│   ├── rerun-main-int.log / rerun-pr4-int.log
│   ├── rerun-main-ruff.log / rerun-pr4-ruff.log
│   └── rerun-main-mypy.log / rerun-pr4-mypy.log
├── gate1/
│   ├── baseline.json                 # T0 515 条固定分母
│   ├── gate1_results.jsonl           # 515 条逐条结果
│   ├── gate1_results.jsonl.summary.json
│   ├── analysis.json                 # 按项目/失败码/镜数汇总
│   ├── sb13_sha_compare.json         # 13/13 字节一致性
│   └── gate1_run.log
└── scripts/
    ├── gate1_crop_dryrun.py
    ├── create_v2_project.py          # 未执行
    └── confirm_v2_project.py         # 未执行
```

关键提交：`3eac2ba`（PR #4 合并）；失败报告提交见本次推送。

## 偏差

1. Gate 1 未达到 90%：78.835%，主要失败为 `shot_panel_gutter_not_found` / `layout_mismatch`，集中在 2/3 镜历史 v1 版式（2 镜 72.61%、3 镜 70.81%）。
2. ruff/mypy 在 main 上本身不干净（15 / 12 条既有错误），但 PR 未新增；未修改基线。
3. 集成测试 19 个失败为环境缺失（MinIO/Temporal/ffmpeg），main 与 PR 相同；未把环境失败算作 PR 回归。
4. 单元测试 5 个共有失败为 WorkBuddy safe-delete shim，branch-only 的 1 个同源且单跑通过；未修改测试或 shim。
