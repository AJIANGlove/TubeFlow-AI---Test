# 247462a4 故事板大量拒收诊断（2026-11-05，只读）

对象：`247462a4-e719-4488-ae14-aab962de1a27`「You're the Explorer Who Found Machu Picchu」。
未对该项目做任何写操作、未暂停/恢复、未重跑。

## 1. 项目

| 项 | 值 |
| --- | --- |
| 画幅 | `16:9` |
| 语言 / 模式 | `en-US` / AUTO |
| 频道 | `c77ea4a2-0058-4fab-839b-1b92172f35bd`「Past Unknown 历史悬疑史」 |
| visual_style | 暗黑油画插画、明暗对照布光，深海军蓝+琥珀金双色基调，单一暖光源，厚涂油画笔触、大气雾感、边缘渐隐；POV 历史人物多用剪影或半明半暗，常驻调查员 Lumen |
| 组数 | 53 |

## 2. 故事板生成与拒收

| 指标 | 值 |
| --- | --- |
| 故事板生成总数 | **2566**（全部 `grid-v2`） |
| `storyboard_panel_layout_unreadable` | **2385**（92.9%） |
| 按镜数分布（拒收） | 6 镜 803、5 镜 415、4 镜 630、3 镜 339、2 镜 198 |
| 有拒收的组 | 53 / 53 |
| 单组最多 / 最少 | 54 次（`2c28a146-…`）/ 7 次（`25950c2f-…`） |
| 累计拒收 ≥14 次的组 | **51 / 53** |
| 视频尝试 | **0**（未进入视频阶段） |

AUTO 当前状态：**PAUSED**，round 7，`reason_code = storyboard_failed:storyboard_panel_layout_unreadable`，
phase `storyboard`，更新于 2026-11-05 12:55 +0800。即该项目在故事板阶段耗尽后停住（本次诊断只记录，未恢复）。

## 3. 裁切追踪（最近 24 张被拒故事板）

| 指标 | 值 |
| --- | --- |
| 追踪条数 | 24（`LIMIT=24`） |
| `path_exists` 为 false | **0**（24 张原始图仍在 worker 工作目录，可追踪） |
| trace 结论 | 24/24 `failed` |
| 失败步骤 | **`y wide` 24/24**（行分隔在严格容差与放宽 ±25% 单格范围内都找不到） |
| 失败码 | `shot_panel_gutter_not_found` 24/24 |
| 图片尺寸 | **2752×1536** 24/24（基准 1672×941 的 1.65 倍，像素常量已按比例放大） |
| 版式 | `2x2` 12 张、`2x3` 12 张 |

## 4. 事实结论

- 该项目的 v2 故事板几乎全部被裁切拒绝，且拒绝点统一在**行方向（y）找不到贯穿全长的分隔带**，
  与列方向无关；放宽搜索也无效。
- 图片是 2752×1536 的大图；`crop-grid-v2` 的像素常量已按图幅等比放大（`_V2_REFERENCE_SIZE`），
  因此不是常量未缩放的问题。
- 该项目自 2026-11-05 12:55 起停在 `storyboard_failed:storyboard_panel_layout_unreadable`，
  没有任何视频尝试；按 §46 只记录，不处置。

证据：`summary.json`、`trace.jsonl`（24 条）、`previews/`（24 张带标注半尺寸）、`sheets/rejected_01..02.jpg`、
`originals/`（8 张原图）、`groups.json`、`checks.json`。
