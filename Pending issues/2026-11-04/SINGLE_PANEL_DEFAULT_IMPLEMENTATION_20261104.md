# 新项目默认逐格参考：实施说明（2026-11-04，USER_DECISIONS §46）

分支 `claude/single-panel-default-v2`。用户已确认实施计划 D1–D6 全部取推荐值。未调用任何真实 Provider，未部署。

## 1. 解决的问题

fa3a0b30 第 9 组 0–3 s 分屏、第 9/11 组同一人物出现两次：整张多格故事板作为一张参考图送给 H3，H3 把多格当成一帧。14 臂实验（`docs/MULTI_PANEL_LEAK_EXPERIMENT_20261020.md`）中只有“单格参考”干净。

## 2. 实施内容

| 项 | 文件 | 内容 |
| --- | --- | --- |
| 开关 | `config.py`、`.env.example` | `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS`，默认 `false` |
| 网格 v2 与钉版 | `domain/storyboards.py`、`application/storyboards.py`、`application/storyboard_prompt.py`、两个故事板仓储 | 首张故事板时钉 `grid-v1`/`grid-v2`；v2：2–4 镜 2×2、5–6 镜 2×3；v2 提示词要求等大格与约 12 px 纯黑分隔；仅 v2 在 `input_json` 写 `grid_layout_version` |
| 裁切 v2 | `video_generation/panel_images.py` | `crop-grid-v2`：均值偏离邻域识别分隔带、先行后列、暗尾识别留黑格、内缩 3 px、格内暗色分隔判错版式 |
| 故事板阶段校验 | `application/storyboards.py`、`api/storyboard_runtime.py` | v2 故事板裁切演练失败 → `storyboard_panel_layout_unreadable`，走既有故障转移链（至多 14 次） |
| 参考策略 | `adapters/persistence/production_authority.py`、`shot_panel_assets.py`、`orchestration.py` | v2 故事板 → `single_panel_v1` + `crop-grid-v2`，无需 allowlist |
| v4 单格提示词 | `video_prompt/domain.py`、`video_prompt/templates.py`、`orchestration.py` | 新策略 `tubeflow_storyboard_v4_single_panel`（单格规则 + v4 规则 + 第 22 条），校验加性别代词禁令 |
| AUTO 梯子 | `video_generation/orchestration.py` | 生产逐格尝试可自动重提，全部级别沿用单格策略，复用已归档逐格资产 |

无数据库迁移（新值均在既有 JSON 列中）。

## 3. 本地验证

- 裁切实测（`evidence/shortproj/sb_1..13.png`，按 v1 版式）：旧 `crop-title54-v1` 2/13 通过（11 张 `shot_panel_gutter_not_found`）；`crop-grid-v2` 13/13 通过，39 格逐一目视无相邻格内容；8 个故意错版式（如 2×2 当 1×2、1×2 当 1×1）全部拒收。fa3a0b30 g7 第 1 格门框（浅色长直线）不误判。前提：这些 PNG 与库中原始字节一致（第 1 关复核）。
- 黄金值：v1 故事板提示词（1–6 镜 × 16:9/9:16）、v3 单格/v3/v4 整图系统提示词、v3 单格载荷与指纹，与 main `3af818d` 逐字一致。
- 新增单元测试 3 个文件、88 项：`test_grid_layout_v2.py`、`test_panel_images_v2.py`、`test_single_panel_v2_default.py`（含梯子），全部通过。
- 本地可运行的单元测试：改动前后失败集合相同（均为 temporalio、asyncio 插件等本地缺依赖所致）；改动文件 ruff 通过；mypy 无新增错误。
- 未运行：PostgreSQL 集成测试、全量 ruff/mypy、真实生成。

## 4. 已知限制

- 故事板格子内部画错的内容（多余的手、单格内已画两个同一人物，如 fa3a0b30 g2 第 1 格）不会因逐格参考消失，需故事板内容质检。
- 5–6 镜组仍为 2×3，格子约 550×465（比例约 1.2）。
- 回滚代码会使已钉 v2 项目的新视频意图 fail-closed（`shot_panel_derivation_unsupported`）；关闭开关只影响尚无故事板的项目。
