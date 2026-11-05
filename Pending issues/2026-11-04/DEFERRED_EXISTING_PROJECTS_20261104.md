# 暂缓处理的既有项目记录（2026-11-04）

依据：USER_DECISIONS §46 第 10 条。用户决定先完成第 3–5 关、尽快上线可用版本；受影响的既有项目只记录，不在本轮处理。本文件只记录事实，不对这些项目做暂停、恢复、重新处理或重新生成。

## 1. 受影响的两类

| 类别 | 影响 | 本轮处理 |
| --- | --- | --- |
| A. 开关开启重启时仍在运行的项目（AUTO RUNNING、在途故事板或视频尝试） | 重启发生在生成过程中。Gate 2 的同类重启未发现失败或未知态，但本次仍需逐项记录 | 只记录重启前后快照 |
| B. 已有故事板、已钉 v1 的项目 | §46 第 2 项：继续走整图参考（v1 网格 + composite），分屏、同一人物重复出现等问题仍可能出现，本轮修复不覆盖 | 只记录；后续是否重做、如何重做另行决定 |
| C. 尚无任何故事板生成的既有项目 | §46 第 2 项：开关开启后，若它们生成首张故事板，会被钉为 `grid-v2`，走逐格参考 | 只记录名单；不恢复、不触发 |

## 2. 已知状态（截至 Gate 2 T+30，2026-11-04T10:15Z）

| 项目 | AUTO | 类别 | 备注 |
| --- | --- | --- | --- |
| `a0e52ea0-0ef7-4a95-9c76-d87c947c38a0` | RUNNING，round 5，phase video | A、B | Gate 2 窗口内 12 次尝试到达 ARCHIVED，无 FAILED / REMOTE_STATE_UNKNOWN |
| `d725172f-bffa-4795-9554-8761a197fa08` | RUNNING，round 10 | A、B | Gate 2 部署后被外部由 PAUSED(round 9，`video_failed:intent_rejected`) 恢复，非本次操作；按既定约束不触碰 |
| `2c291aba-085d-499d-bd32-b3a4d75f252f` | PAUSED，round 7，phase storyboard_drafts，`director_validation_failed` | 待查：B 或 C | 若尚无任何故事板生成即属 C 类：恢复后首张故事板会钉为 `grid-v2` |
| `765dca11`、`146ae08c`、`fa3a0b30`、`31e00d87` | 未在 RUNNING/PAUSED 列表 | B | Gate 1 口径中均已有 v1 故事板 |

## 3. 开关开启重启前后快照

由实施方按第 3 关指令在下方追加：完整的 A、B、C 三类名单，以及重启后 10 分钟内既有项目出现的 FAILED、REMOTE_STATE_UNKNOWN、QC_FAILED 或 local_integrity_blocked。

### 3.1 开关开启（T_on = 2026-11-04T13:41:55Z / 21:41:55+0800，完成 13:42:27Z）

镜像未变（`sha256:238bd9ea…`），无迁移（`alembic 0046`），仅把部署机 `.env` 的 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS` 由 `false` 改为 `true` 后 `up -d --no-build`。按用户决定未等待既有项目排空。备份 `.env.bak-single-panel-on-20261104`。

### 3.2 A/B/C 名单（重启前快照，52 个项目）

判定：A = 重启时有 RUNNING/PAUSED 运行或有在途工作；B = 已有故事板；C = 故事板行数为 0。完整表见 `singlepanel_test/gate3_5/pre_restart.json` 的 `projects`。

| 项目 | 类别 | AUTO | 故事板行/成功 | 在途 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `a0e52ea0-0ef7-4a95-9c76-d87c947c38a0` | B+A | PAUSED r6 `video_failed:intent_rejected` | 131 / 122 | 视频 2（RUNNING） | 重启时正在出片，两尝试在 10 分钟内转为 ARCHIVED/COMPLETE |
| `fd3bf04d-…`（19:51 新建） | C+A | RUNNING r2（导演阶段） | 0 / 0 | 无 | 尚无故事板；开关开启后其首张故事板将钉 `grid-v2`（§46 第 2 项预期） |
| `2c291aba-085d-499d-bd32-b3a4d75f252f` | C+A | PAUSED r7 `director_validation_failed` | 0 / 0 | 无 | 同上，若被恢复则首板钉 `grid-v2` |
| `1142cd90-f7dd-4455-8d7b-45ceae6ccf09` | B+A | 无运行行 | 822 / — | 故事板 6 | 非终态故事板为历史遗留行，本轮不处理 |
| `ca6f47d6-1824-4298-aaf8-c1d4a1011715` | B+A | 无运行行 | 98 / — | 故事板 4 | 同上 |
| 其余 47 个项目 | B | COMPLETED 或无运行行 | > 0 | 无 | 已钉 v1，继续整图参考 |

纯 C 类（仅有故事板为 0、且当前完全空闲）的项目：**无**。

### 3.3 重启后 10 分钟（T_on 起至 2026-11-04T13:51:09Z）

- 视频尝试被触碰：**2 条**，全部属 `a0e52ea0`，stage 由 RUNNING 变为 `ARCHIVED`，`archive_stage=COMPLETE`。
- 触发标记（FAILED / REMOTE_STATE_UNKNOWN / `archive_stage=QC_FAILED` / `local_integrity_blocked`）：**0 条**。
- 新增故事板：**0 条**；其中带 `grid_layout_version` 的 **0 条**，`grid-v2` **0 条**。
- 结论（事实）：B 类项目未出现 `grid_layout_version` 键；C 类项目在窗口内尚未生成故事板。`web`/`worker` 重启后 healthy，`/health/live` 200。

证据：`singlepanel_test/gate3_5/post_restart.json`（SQL 见同目录 `post_restart.sql`）。


## 4. 旁白作业停在 DISPATCHED 的项目（作业 ID 跨项目碰撞，根因见 `NARRATION_JOB_ID_COLLISION_20261104.md`）

修复（作业 ID 加 `project_id`）只作用于部署后才确认的项目。下列项目存的仍是旧 ID，修复后依旧停住。按 §46 第 10 条只记录。解卡需要逐项目改写 `payload.job`，会触发付费配音，须用户逐项目授权。

| 项目 | 状态 | 备注 |
| --- | --- | --- |
| `faf27eb5-23d4-43e7-8269-029336e38167` | AUTO RUNNING r1，phase narration，作业 DISPATCHED | 第 3 关首个测试项目（复制 `fa3a0b30`）；除确认时 1 次拆句外无 Provider 调用 |
| `039e36e3-bd59-423e-84ca-d53828227e18` | 未确认配音，无 AUTO 运行 | 第 3 关误建的重复项目；无 Provider 调用 |
| `23c59183-7165-4c33-a161-7ecf28964c78` | 作业 DISPATCHED，0 句 | 2026-11-04 22:30 +0800 由其他会话新建 |
| `3206fa93` | 作业 DISPATCHED | 2026-09-17 起 |

## 5. 开关开启后被恢复、已走 grid-v2 的项目

- `2c291aba-085d-499d-bd32-b3a4d75f252f`：重启前属 C 类（故事板 0 行）。
- 2026-11-04 22:18 +0800 由另一会话修改导演候选后恢复（`df1c5c7`，round 8 → 9），进入故事板阶段并启动 107 个组的故事板工作流。
- 此时开关已开（T_on 21:41:55 +0800），按 §46 第 2 项，这些组的首张故事板钉 `grid-v2`，视频走逐格参考。
- 本条只记录，不干预该项目。其数据可作为第 3–4 关的只读补充证据。
