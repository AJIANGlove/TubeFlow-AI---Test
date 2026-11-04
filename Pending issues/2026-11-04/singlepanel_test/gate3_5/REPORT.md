# Gate 3/5 — 第 3 关开关开启与测试项目（2026-11-04，中止）

结果：**开关已按用户决定开启并通过全部校验；测试项目的旁白作业停在 `DISPATCHED`，
AUTO 未推进，第 3–4 关证据未取得，第 5 关未开始。** 未重启、未恢复、未重试、未改代码。

## 1. T_on 与校验（全部通过）

| 项 | 值 |
| --- | --- |
| `.env` 备份 | `.env.bak-single-panel-on-20261104` |
| 改动 | 仅 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS` false → true |
| T_on | 2026-11-04T13:41:55Z 开始 / 13:42:27Z 完成（21:41:55–21:42:27 +0800） |
| 镜像 | web/worker 均 `sha256:238bd9ea…`（未重建） |
| 迁移 | `alembic 0046`（无迁移） |
| 服务 | web healthy（启动约 80 秒后 `/health/live` 200）、worker healthy |
| 开关实读 | web、worker 内 `Settings().storyboard_single_panel_new_projects is True`，`env` = `true` |

## 2. 重启影响（既有项目，只记录）

T_on 后 10 分钟（至 13:51:09Z）：

- 视频尝试被更新 **2 条**，全部属 `a0e52ea0`，`RUNNING` → `ARCHIVED` / `archive_stage=COMPLETE`。
- `FAILED` / `REMOTE_STATE_UNKNOWN` / `archive_stage=QC_FAILED` / `local_integrity_blocked`：**0 条**。
- 新增故事板 **0 条**；带 `grid_layout_version` **0 条**、`grid-v2` **0 条** → B 类未被钉 v2。
- A 类（重启时有运行或在途）：`fd3bf04d`（C+A，RUNNING r2）、`a0e52ea0`（B+A，PAUSED r6，在途视频 2）、
  `2c291aba`（C+A，PAUSED r7）、`1142cd90`（B+A，在途故事板 6）、`ca6f47d6`（B+A，在途故事板 4）。
  其余 47 个为 B 类；纯 C 类无。完整 52 行见 `pre_restart.json`，说明见
  `Pending issues/2026-11-04/DEFERRED_EXISTING_PROJECTS_20261104.md` 第 3 节。

## 3. 测试项目

| 项 | 值 |
| --- | --- |
| 项目 ID | `faf27eb5-23d4-43e7-8269-029336e38167` |
| 创建 / 确认 | 21:51:35 / 21:51:51 +0800 |
| create 校验 | `comparison` 11 项全部 `equal: true`（含 `script_sha256` `4dc63e1c…`），`production_mode=AUTO`，`production_state=ACTIVE` |
| confirm 返回 | `state=QUEUED`、`narration_sentences=39`、`auto_state=RUNNING`、`auto_round=1`、`frozen_sentences=0` |
| AUTO 终态 | **未到达**：RUNNING r1，`phase=narration`，`updated_at` 自 13:51:51Z 未变 |
| 组数 | 0（旁白未冻结，未生成组） |
| 每组镜数 / v2 版式 | 无 |
| 重复项目 | `039e36e3-bd59-423e-84ca-d53828227e18`（21:51:31），未确认、无 AUTO 运行、无 Provider 调用 |

## 4. 停摆证据

- 旁白作业 `narration_29ad09b7…`：`DISPATCHED`、`revision=1`、`sentence_sequences=39`。
- `authoritative_narration_sentences` **0**、`narration_attempt_records` **0**、本项目 MinIO 新增对象 **0**。
- `monitor.jsonl` 13:52:04 / 14:07:26 / 14:43:17 三行完全相同。
- 对照：`fa3a0b30` 同一 39 句脚本旁白约 4 分钟；`247462a4`（开关开启后 22:23 创建）旁白完成 261 句
  并进入导演阶段，说明链路可用；`23c59183`（22:30 创建）同样停在 `DISPATCHED`、0 句。
- `DISPATCHED` 无自动回收：`3206fa93` 自 2026-09-17 停留在该状态。
- 详见 `STALL.md`。

## 5. Provider 调用（实际）

| Provider | 调用 |
| --- | ---: |
| DeepSeek 拆句（确认配音时） | 1（39 句序列） |
| ElevenLabs TTS | 0 |
| DeepSeek 导演 | 0 |
| grsai 故事板 | 0 |
| DeepSeek 视频编译 | 0 |
| H3 视频 | 0 |

## 6. 未执行 / 未取得

- `gate3_5_collect.py` 未运行（无故事板、无成片）；`groups.json`、`checks.json`、`evidence/*.png` 未生成。
- HF、成片规格、`single_panel_automatic_resubmit_not_supported` 日志计数均无数据。
- FINAL_ACCEPTANCE §42「第 3–4 关」「第 5 关」均未勾选。
