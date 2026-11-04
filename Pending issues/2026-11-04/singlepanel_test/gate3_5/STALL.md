# Gate 3/5 — 测试项目旁白作业未派发（2026-11-04）

只读诊断记录。未重启、未恢复、未重试、未改任何代码或配置。测试项目 AUTO 仍为 RUNNING，
未被人为干预。

## 1. 事实

| 项 | 值 |
| --- | --- |
| 测试项目 | `faf27eb5-23d4-43e7-8269-029336e38167`（复制 fa3a0b30，AUTO/ACTIVE，39 句） |
| 创建 / 确认 | 2026-11-04 21:51:35 / 21:51:51 +0800（13:51:51Z） |
| 确认返回 | `state=QUEUED`，`job_id=narration_29ad09b7…`，`auto_state=RUNNING`，`auto_round=1`，`narration_sentences=39` |
| 旁白作业状态 | `DISPATCHED`，`revision=1`，`sentence_sequences=39`（自 13:51 起未变化） |
| `authoritative_narration_sentences` | 0 |
| `narration_attempt_records` | 0 |
| MinIO 新增对象 | 本项目无（13:52–14:43 期间仅出现他项目的 mp4 与 srt） |
| AUTO run payload | `phase=narration`，`phase_started_ms` = 13:51:51Z，此后 `updated_at` 未变 |
| 观察记录 | `monitor.jsonl` 13:52:04 / 14:07:26 / 14:43:17 三行完全相同 |
| 服务状态 | web healthy、`/health/live` 200；worker healthy、`/tmp/tubeflow-worker-ready` = `ready`；镜像 `238bd9ea…`；`alembic 0046` |

## 2. 对照

| 项目 | 旁白结果 | 说明 |
| --- | --- | --- |
| `fa3a0b30`（同一 39 句脚本） | COMPLETED，39 句 | AUTO 运行 02:02:55Z 起，首张故事板 02:07:14Z → 旁白约 4 分钟 |
| `247462a4-e719-4488-ae14-aab962de1a27` | COMPLETED，261 句 | 22:23:48 创建（开关开启后），已完成旁白并进入导演阶段后 PAUSED（`director_preplan_failed`）→ 说明重启后旁白链路可用 |
| `23c59183-7165-4c33-a161-7ecf28964c78` | **DISPATCHED**，0 句 | 22:30:20 创建，与测试项目同一现象 |
| `3206fa93` | DISPATCHED | 2026-09-17 起长期停留该状态，说明 `DISPATCHED` 无自动回收 |

## 3. 结论（事实层面）

- 测试项目的旁白作业在 13:51:51Z 被标记 `DISPATCHED` 后没有任何进展：0 句、0 次逐句尝试、
  无音频对象，AUTO 的 `narration` 阶段自 13:51:51Z 起未更新。
- 同期另一新建项目 `23c59183` 处于同样状态；而 `247462a4`（同窗口创建）旁白完成 261 句，
  因此不是全局旁白链路不可用，而是该两个作业停在 `DISPATCHED`。
- `DISPATCHED` 状态没有自动回收/重派机制（`3206fa93` 自 9 月停留至今可证）。

## 4. 未做的事

未重启容器、未再次确认配音、未恢复 AUTO、未修改代码/配置、未对既有项目做任何操作。
按授权（恢复 0 次、除第 6 步外无提交动作）止于此。
