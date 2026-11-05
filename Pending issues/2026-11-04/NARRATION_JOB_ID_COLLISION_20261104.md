# 旁白作业停在 DISPATCHED：作业 ID 跨项目碰撞（2026-11-04）

## 1. 现象

- 第 3 关测试项目 `faf27eb5` 于 13:51:51Z 确认配音（逐项复制 `fa3a0b30`），旁白作业 `narration_29ad09b7…` 一直停在 `DISPATCHED`。
- 该项目的逐句尝试为 0，权威句子为 0，也没有任何音频对象。
- 同窗口另有两个项目：`23c59183` 现象相同；`247462a4` 正常完成 261 句。
- `3206fa93` 自 2026-09-17 起停在同一状态。

证据见 `singlepanel_test/gate3_5/STALL.md`。

## 2. 根因（代码事实）

1. 作业 ID 只由内容快照哈希构成：`api/narration_runtime.py` 中 `confirm_and_enqueue` 生成 `narration_{snapshot_hash}`。哈希输入为 `script_sha256`、`voice_version_id`、`visual_style`、`character_image_ids`、`locale`、`aspect_ratio`、`channel_id`，ElevenLabs 项目另加 `tts_provider` 和 `cloned_voice_id`。**不含 `project_id`。**
2. 作业 ID 就是 Temporal 工作流 ID：`workflows/narration.py` 的 `TemporalNarrationStarter.start` 以 `id=job_id`、`WorkflowIDReusePolicy.REJECT_DUPLICATE` 启动工作流，并用 `suppress(WorkflowAlreadyStartedError)` 吞掉重复启动。这样设计原本是为了同一作业丢失响应后能安全重派。
3. 派发器在 `start` 返回后无条件执行 `mark_job_dispatched`（`application/narration.py` 的 `dispatch_pending_jobs`）。
4. 集成测试 `test_narration_temporal.py::test_temporal_duplicate_and_starter_restart_reuse_stable_workflow` 已断言这一行为：同一 ID 的工作流关闭后再次 `start`，返回成功，但不会产生新的运行（run_id 不变）。

由此推出：第二个内容完全相同的项目（同文案、音色、风格、角色、语言、画幅、频道）会得到与第一个项目相同的作业 ID。它的启动被 Temporal 拒绝，异常被吞掉，作业被标为 `DISPATCHED`，但实际没有任何工作流在跑。`DISPATCHED` 没有超时回收，所以作业永久停住。

`faf27eb5` 是按第 3 关要求逐项复制 `fa3a0b30` 建出的项目，11 项对比全部相等，正好命中这一条件。该问题与 §46 开关、与重启都无关。

**待部署机只读核实**（见第 5 节指令第 0 步）：`fa3a0b30` 的作业 ID 也是 `narration_29ad09b7…`，且该 ID 的 Temporal 工作流属于 `fa3a0b30`、已结束。`23c59183` 与 `3206fa93` 同理，应各能找到一个共用作业 ID 的更早项目。

## 3. 影响范围

- **哪些项目会中招**：凡是用与某个既有项目完全相同的内容新建并确认的项目，都会卡住。例如用户重建一个失败项目、文案音色风格角色都不改。时间窗口受 Temporal 命名空间保留期限制：只要旧工作流仍在保留期内，新项目就会停住。
- **其他工作流不受影响**：其余 Temporal 工作流 ID 都带唯一 ID。故事板与导演用 generation/run/attempt UUID，视频用 attempt UUID，清理用 project_id。旁白的逐句自动重试作业 `narration_auto_retry_*` 与对账作业 `narration_reconcile_*` 的 ID 已包含 `project_id`。

## 4. 修复（PR #6，`fix/narration-job-id-per-project`）

- **改动**：作业 ID 改为 `narration_{project_id}_{snapshot_hash}`，只改这一行。
  - 同一项目重放确认仍得到同一 ID，因为既有 job 直接复用。
  - 导演幂等键 `narration-confirm:{project_id}:{snapshot_hash[:32]}` 不变。
- **只影响部署后才确认的项目**：已确认项目的作业 ID 早已存入 `narration_project_records.payload.job`，不受影响。
- **新增集成测试**：两个内容相同的项目确认后得到不同的作业 ID，各自以本项目 ID 为前缀，重放确认 ID 不变。
- **不在本修复范围**：`DISPATCHED` 无超时回收；`WorkflowAlreadyStartedError` 一律吞掉、不核对归属。ID 改为按项目唯一后，跨项目碰撞已不再可能，这两点只记录、不改。

## 5. 已卡住项目的处理

修复不会自动解开已经卡住的作业，因为它们存的仍是旧 ID。按 USER_DECISIONS §46 第 10 条，只记录、后续处理，名单见 `DEFERRED_EXISTING_PROJECTS_20261104.md` 第 4 节。

解卡需要逐项目改写 `payload.job`，即换成新 ID 并置回 `PENDING`。这属于数据修复，会触发付费配音，必须先取得用户逐项目授权。
