# 第 3–5 关重跑（PR #6 修复后，2026-11-05）

测试项目 `72a22f0a-becd-44e7-8af5-369589781885`，AUTO **COMPLETED**（round 1）。
旁白作业 ID 修复生效：`narration_72a22f0a-…_29ad09b7…`。

## 1. 旁白作业 ID 碰撞核实（合并前，`gate3_5/job_collision.json`）

| 项目 | job_id | 与谁共用 | 工作流 |
| --- | --- | --- | --- |
| `faf27eb5` | `narration_29ad09b7…` | `fa3a0b30`（COMPLETED，且在该项目 `finished_jobs` 中） | `workflow not found`：命名空间保留期实测 86400 秒，该工作流 2026-11-04T02:07Z 关闭，查询时刚过 24 小时被清 |
| `3206fa93` | `narration_auto_retry_a0fb9d84…` | 无（`other_project` 为空） | 同样 not found；**与本次碰撞不同源**，另一类停滞，只记录 |

## 2. Gate A / 合并 / 镜像

分支 `aad06d1` 对 main `09cd418`：

| 检查 | main | 分支 | 判定 |
| --- | --- | --- | --- |
| unit | 2342 passed / 0 failed / 10 skipped | 同左 | 一致 |
| integration | 19 failed / 119 passed / 7 skipped | 19 failed / **120 passed** / 7 skipped | 多出的 1 条通过即新用例（单跑 `1 passed in 3.64s`），失败集合逐条相同 |
| ruff | 15 条 | 15 条，逐字节相同 | 无新增 |
| mypy | 12 条（7 文件 / 206 源） | 同左，逐字节相同 | 无新增 |

`branch_only_failures` 为空。合并 `01ccf01`，证据提交 `c0b8222`。
镜像 `sha256:edee542d…`（`tubeflow-test:narration-jobid-20261104`，tag 按用户给定，实际构建于 11-05）；容器内 `narration_runtime.py` LF 归一 sha256 `2450a3b9…` 与 `HEAD:backend/src/.../narration_runtime.py` 逐字一致，含 `narration_{project_id}_{snapshot_hash}`。

## 3. 部署与重启影响

T_dep **2026-11-05T02:41:26Z → 02:41:40Z**（10:41:26–10:41:40 +0800）；compose 三处改钉 `edee542d…`，`.env` 未改（开关 true），无迁移。

| 校验 | 结果 |
| --- | --- |
| `/health/live` | 200（启动约 100 秒） |
| web / worker 镜像 | 均 `sha256:edee542d…`，healthy |
| 开关 | 两容器 `Settings().storyboard_single_panel_new_projects is True` |
| alembic | `0046` |

部署后 10 分钟快照（02:41:26Z–02:49:53Z）：视频尝试被触碰 **0**；FAILED / REMOTE_STATE_UNKNOWN / QC_FAILED / local_integrity_blocked **0**；新增故事板 **37** 条全部 grid-v2，全部属 `247462a4`（36 FAILED `storyboard_panel_layout_unreadable` + 1 SUCCEEDED）。该项目是开关开启后新建的 v2 项目，其失败来自自身故事板梯子，不是重启打断；其余既有 v1 项目 0 新增。

## 4. 测试项目

| 项 | 值 |
| --- | --- |
| 项目 ID | `72a22f0a-becd-44e7-8af5-369589781885` |
| create 校验 | `comparison` 11 项全 `equal:true`、`production_mode=AUTO`、`ACTIVE` |
| job_id | `narration_72a22f0a-becd-44e7-8af5-369589781885_29ad09b7…`（**前缀含项目 ID**） |
| 旁白耗时 | 确认 02:50:32Z → 进入导演 02:52:49Z，**约 2 分 17 秒**（39 句） |
| AUTO | **COMPLETED** round 1，无 PAUSED，`updated_at` 03:49:41Z |
| 总时长 | 02:50:32Z → 03:49:41Z ≈ **59 分钟** |
| 组数 / 镜头 | 14 组；2 镜 ×5、3 镜 ×8、4 镜 ×1 |
| 版式 | 14 组全部 `2x2`（`grid-v2`），首张成功故事板即 `grid-v2` |

## 5. checks.json 要点（测试项目）

- `groups` 14，`all_groups_v2` **true**，`strategies` 仅 `tubeflow_storyboard_v4_single_panel`（14），`ladder_error_count` **0**，`attempts_without_snapshot` 0。
- 故事板尝试 15 次（14 组各 1 次 + 第 7 组重试 1 次）；唯一失败为第 7 组 `aspect_ratio_not_supported`（grsai 侧拒收，重试成功）。`storyboard_panel_layout_unreadable` **每组 0 次**。
- 留黑格：14 组共 17 个空格，全部 `dark=true`，均值 0.1–0.2（0–255 灰度），最深 0.1。
- 视频：14 次尝试全部 `ARCHIVED`，策略全部 `tubeflow_storyboard_v4_single_panel`，无梯子替代（`ladder_error_count` 与 worker 日志 grep 均为 0）。
- **`every_attempt_single_panel` = false，`attempts_failing_checks` = 全部 14 条**，子项失败固定为 `each_panel_sent_once` 与 `reference_count_ok`，因为二者读 `request_json.references`，而该字段在 staging 后被替换为 `staged_assets`（实测 `references` 长度 0、`staged_assets` 4）。
  用 `input_snapshot_json.logical_request.references` 直接核对 14/14：`refs = panels + characters`（2+1、3+1、4+1）、每个 panel sha 恰好出现 1 次、character 出现 1 次、**storyboard sha 从不出现**。即产品行为符合第 3–4 关要求，脚本判据为假阴性。

## 6. 2c291aba 补充证据（只读，`2c291aba/`）

快照时间 2026-11-05T03:56Z 左右；该项目 AUTO 已 **COMPLETED**（round 9，05:29 +0800），非指令所写"仍在运行"。

- 107 组，`all_groups_v2` **true**，`strategies` 仅 `tubeflow_storyboard_v4_single_panel`（107），`ladder_error_count` 0。
- `storyboard_panel_layout_unreadable`：第 54 组 1 次、第 100 组 1 次，其余 0。
- `every_attempt_single_panel` = false（同一 `request_json.references` 假阴性）。
- 留黑格 49 组有记录，全部 `dark=true`，均值 0.1–0.8。

## 7. HF 与成片

每组 HF（am4，528×304 灰度残差 RMS）：

| 组 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| HF | .188 | .071 | .173 | .282 | .164 | .039 | .110 | .107 | .392 | .294 | .197 | .048 | .304 | .525 |

- **全片 HF 0.219**（目标 ≤0.25）；最差段 2271–2360 帧，HF 0.709；39 镜头中 9 个超 0.25。
- 成片：资产 `243c3708-17c8-4be7-b31c-434a501db2da`（version `92f13a11`），SHA-256 `0ca9f6fab23b18b49748659b684b7c6364634c1d26715ed6bf6d2fd77191f506`，99,374,032 字节（94.8 MB），1920×1080 / 24 fps / 3907 帧 / 162.800 s。mp4 未入库，留在部署机（容器 `/tmp/film.mp4` 与 MinIO 对象）。
- 每组成片另有 `v_<seq>.mp4` 在收集目录（47 MB），同样未入库。

## 8. Provider 调用（实际）

| Provider | 调用 |
| --- | ---: |
| ElevenLabs TTS | 39 句（重试次数无独立记录） |
| DeepSeek 拆句 | 1（39 句序列） |
| DeepSeek 导演 | 0 条 `director_generation_attempts`（本项目未见新导演调用） |
| grsai 故事板 | 15（14 成功 + 1 次 `aspect_ratio_not_supported` 后重试成功） |
| DeepSeek 视频编译 | 14（按 14 次视频尝试计） |
| H3 视频 | 14 |
| 合成 | 1（本地 ffmpeg，无 Provider） |

## 9. 结论

- 修复生效：同内容新项目的作业 ID 带项目 ID，旁白 2 分 17 秒完成，AUTO 59 分钟 COMPLETED。
- 第 3–4 关：`all_groups_v2`、`strategies`、`ladder_error_count` 满足；`every_attempt_single_panel` 因收集脚本读取被替换的字段判假 → **§42 第 3–4 关不勾**，等收集脚本口径确认。
- 第 5 关未勾，留给目检。
