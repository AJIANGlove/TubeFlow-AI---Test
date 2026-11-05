# 第二轮测试项目（v2 格内构图规则，2026-11-05）

项目 `484746c4-ddf3-48af-90ca-a25cf41cbf35`（复制 fa3a0b30），镜像 `sha256:30ffc3c3…`
（`v2-panel-rule-20261105`，含 §46 第 11 条）。AUTO **COMPLETED**（round 1）。

## 1. 项目与时间

| 项 | 值 |
| --- | --- |
| 项目 ID | `484746c4-ddf3-48af-90ca-a25cf41cbf35` |
| create 校验 | `comparison` 11 项全 `equal:true`、`production_mode=AUTO`、`ACTIVE` |
| job_id | `narration_484746c4-…_29ad09b7…`（含项目 ID） |
| 确认 → 旁白完成 | 05:45:39Z → 05:47:42Z，约 2 分钟（39 句） |
| AUTO 终态 | **COMPLETED** round 1，`updated_at` 07:14:37Z |
| 总时长 | 05:45:39Z → 07:14:37Z ≈ **89 分钟**（比首轮 59 分钟长，H3 与 `23c59183` 并发） |

## 2. 组与版式

- 13 组，全部 `grid-v2`（2x2）；镜头 2 镜 ×3、3 镜 ×7、4 镜 ×3。
- 首张成功故事板即 `grid-v2`。
- 故事板生成 16 条：13 SUCCEEDED + 3 FAILED，失败码全部为 `aspect_ratio_not_supported`（grsai 侧拒收，各自重试一次后成功）——本组无 `storyboard_panel_layout_unreadable`。

## 3. checks.json

| 字段 | 值 |
| --- | --- |
| `groups` | 13 |
| `all_groups_v2` | **true** |
| `every_attempt_single_panel` | **true** |
| `attempts_total` / `without_snapshot` | 13 / 0 |
| `attempts_failing_checks` | 空 |
| `strategies` | `{tubeflow_storyboard_v4_single_panel: 13}` |
| `ladder_error_count` | 0 |
| `layout_unreadable_by_group` | 每组 0 |
| 留黑格 | 13 组共 15 个空格，全部 `dark=true`，均值 0.1–0.2 |

## 4. HF 与成片

每组 HF（am4）：

| 组 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| HF | .721 | .062 | .127 | .095 | .040 | .369 | .262 | .082 | .109 | .266 | .028 | .121 | .215 |

- **全片 HF 0.274**（首轮 0.219）；最差段 97–186 帧 HF 1.213；37 镜头中 6 个超 0.25；`max_speed` 2.22。
- 首轮同位置：全片 0.219、最差 0.709、9 个抖。第二轮整体抖动变差，第 1 组从 0.188 升到 0.721。
- 成片：资产 `b9485f85-44d8-412c-86c4-7612f7a7438d`（version `39f3ca81`），SHA-256 `490cc71c…`，
  102,162,981 字节（97.4 MB），1920×1080 / 24 fps / 3905 帧 / 162.720 s。mp4 未入库（哈希见 `mp4_sha256.txt`）。

## 5. Provider 调用

| Provider | 调用 |
| --- | ---: |
| ElevenLabs TTS | 39 句 |
| DeepSeek 拆句 | 1 |
| DeepSeek 导演 | 0 条 `director_generation_attempts`（复用复制来源的计划） |
| grsai 故事板 | 16（13 成功 + 3 次 `aspect_ratio_not_supported` 后重试成功） |
| DeepSeek 视频编译 | 13（按 13 次尝试计） |
| H3 视频 | 13 |
| 合成 | 1（本地 ffmpeg） |

## 6. 与首轮的可比事实

| 项 | 首轮 72a22f0a | 第二轮 484746c4 |
| --- | --- | --- |
| 组数 / 视频 | 14 / 14 | 13 / 13 |
| 故事板失败 | 1（aspect_ratio） | 3（aspect_ratio） |
| `layout_unreadable` | 0 | 0 |
| 全片 HF | 0.219 | 0.274 |
| 抖镜头 | 9 / 39 | 6 / 37 |
| 分屏格（目检） | 1 组（第 9 组第 3 镜） | 未目检（第 5 关留给评审） |

第 5 关未勾选，分屏与身份等目视判据留待评审用 `evidence/sheet_v_*.png`、`sb_*.png`、`panels_*.png` 复核。
