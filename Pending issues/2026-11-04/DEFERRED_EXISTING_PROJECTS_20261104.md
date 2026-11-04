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

（待追加）
