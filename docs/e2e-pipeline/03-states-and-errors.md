# 03 状态与异常处理

> 本文汇总各环节的状态机、失败分类、重试预算与恢复入口。所有条目均可在代码中定位；标注【推断】的除外。

## 1. 通用模式

- **意图持久化优先**：所有外部调用前先落库意图（attempt/generation/run + 幂等键 + canonical 输入哈希），再提交 Provider；Worker 崩溃后经 outbox relay / 租约回收恢复。
- **远端未知态（REMOTE_STATE_UNKNOWN）**：请求可能已送达但结果未知时的显式状态。MANUAL 项目一律不自动重提（需人工对账/证据处置或限定免证据入口）；AUTO 项目按 §15.4 自动对账，无果则自动重提并计入每对象 3 次预算。
- **失败分类**（§15.3，AUTO）：临时故障（超时/连接/429/5xx/输出不确定性拒收）→ 预算内自动退避重试；既定失败（401/403、永久性输入拒绝）→ 直接阻断不自动重试；LLM 类调用的 400/404/409/410/422 按 §15.14 归入临时故障（代码见 `adapters/director_model.py:87-90`、`adapters/deepseek_sentence_split.py:78-80`、`adapters/deepseek_video_prompt.py:67-70`）。
  - 【推断】注意：`adapters/director_model.py:90`、`adapters/deepseek_sentence_split.py:81`、`adapters/deepseek_video_prompt.py:70` 中 `raise Retryable...` 之后各有一行不可达的 `raise Permanent...`，是 §15.14 改动留下的死代码；不影响行为（前者先抛出）。
- **公共状态映射**：`api/public_status.py:31` 把内部状态映射为 `public_status` ∈ {NOT_STARTED, QUEUED, RUNNING, RECOVERY_REQUIRED, SUCCEEDED, FAILED, CANCELLED} + `allowed_actions`；REMOTE_STATE_UNKNOWN → RECOVERY_REQUIRED + (RECONCILE, REFRESH)。

## 2. AUTO 编排器运行状态

- 模型：`auto_pipeline/domain.py` `AutoPipelineRun` / `AutoPipelineState`（RUNNING / PAUSED / COMPLETED 等）。
- 决策树：`auto_pipeline/service.py:142-349`，阶段 phase ∈ {narration, director_preplan, director_materialization, storyboard_drafts, storyboard, video, composition}。
- 稳定暂停原因码（`auto_pipeline/domain.py` 常量 + service.py 引用）：
  - `REASON_OVERSIZED_SENTENCE`（实测超长句，phase=video）
  - `REASON_DIRECTOR_PREPLAN_FAILED`（含 `failure_code`、失败 run 书签）
  - `REASON_DIRECTOR_VALIDATION_FAILED`（含 `group_sequences`、`failure_messages`）
  - 故事板/视频/合成：`storyboard_failed_reason(...)` / `video_failed_reason(...)` / `REASON_COMPOSITION_FAILED`（含首错码）
  - EL 号池耗尽不消耗对象预算：配音阻断原因 `narration_provider_rejected` 等（`application/narration.py:1222-1234`）
- 恢复：`POST /identity/projects/{id}/auto-pipeline/resume`（`identity_project/router.py:600`）→ `AutoPipelineService.resume`（`auto_pipeline/service.py:104`）：预算轮次 +1、置 RUNNING、立即 kick。「保存导演草稿即续跑」：`resume_if_paused_for`（service.py:110）按原因码守卫。
- 完成收口：检测到当前成片 → `close_superseded`（合成 outbox 非终态行收口 SUPERSEDED）→ run COMPLETED（service.py:333-337）；§17.2 完成前残留失败不再驱动「需要恢复」。
- 阶段用时（2026-10-17）：`auto_pipeline_runs.payload` 保存 `timing_from_phase` / `phase_started_ms` / `phase_elapsed_ms` / `phase_paused_ms` / `run_paused_ms` / `paused_at_ms` / `completed_ms`（全部 epoch 毫秒，JSON 安全，无迁移）；`workflow-summary` 仅在存在计时数据时输出 `auto_pipeline.timing`（按后端 8 阶段原始用时，页面聚合成 5 行）。暂停/人工等待不计入；历史运行与部署瞬间在途阶段不补算，显示「—」。

## 3. 配音子系统

- 句 attempt 状态（`application/narration.py` `NarrationAttemptState`）：INTENT_RECORDED / SUBMISSION_STARTED / COMPLETED / SAFE_TO_RETRY / PROVIDER_REJECTED / OUTPUT_INVALID / ARCHIVE_FAILED / REMOTE_STATE_UNKNOWN。
- 失败码到状态的映射：`SubmitSentenceNarration.execute`（narration.py:163-187）：
  - `provider_submission_unknown` → REMOTE_STATE_UNKNOWN
  - `provider_not_reached` → SAFE_TO_RETRY
  - `provider_output_invalid` / `provider_output_missing` → OUTPUT_INVALID
  - `provider_rate_limited`（429）→ AUTO 为 SAFE_TO_RETRY（带 `retry_not_before`），MANUAL 为 PROVIDER_REJECTED
  - 其余 → PROVIDER_REJECTED
- AUTO 重投：`_schedule_auto_retries`（narration.py:1181）：每句每预算轮次 3 次封顶（`narration_auto_retry_exhausted`），退避 5s/15s/30s（`narration_auto_retry_delay_seconds`），429 尊重 Retry-After；耗尽落 `narration_submission_budget_exhausted`。
- QC：原始 WAV 与归一化 WAV 双检（48kHz/单声道/16bit/时长一致性，narration.py:569-597）；EL 侧另有时长护栏 `_assert_duration`（`application/elevenlabs_narration.py:276`）。
- EL 特有（`adapters/elevenlabs.py:231-275` `_map_http_error`）：402 → `provider_quota_exhausted`；401 经免费复核区分 `provider_auth_failed`（确认失效）/ `provider_quota_exhausted`（订阅可读）；403 且响应含 `content_against_policy` → `provider_content_rejected`（内容审核确定性失败，不在号池轮换码 `_ROTATION_ERROR_CODES`（`application/elevenlabs_narration.py:44`）内，按既有映射落 PROVIDER_REJECTED 不自动重试）；404 → `provider_voice_missing`；429 → `provider_rate_limited`。号池全部不可用落 `provider_quota_exhausted`（不耗对象预算）。
- MANUAL 恢复：`POST /narration/recover`（`api/narration_router.py:142`），分辨率 CONFIRMED_NOT_ACCEPTED / CONFIRMED_FAILED / RETRY_MISSING / RETRY_INVALID_OUTPUT + 外部证据。

## 4. 前置导演子系统

- run 状态：INTENT_PERSISTED / RUNNING / SUCCEEDED / FAILED / REMOTE_STATE_UNKNOWN（FAILED 可带 `retry_blocked`）。
- unit 状态：INTENT_PERSISTED / LEASED / SUBMITTING / SUCCEEDED / FAILED（租约 20 分钟，`PROJECT_DIRECTOR_UNIT_LEASE`，`application/project_director.py:78`）。
- 自动重试：每单元 3 次尝试、退避 5s/15s、429 尊重 Retry-After；输出结构解析失败保存安全诊断（不含响应正文）。
- 相邻多样性拒收：稳定码 `director_output_adjacent_diversity_insufficient`（§20.2，解析期拒收并计入预算）。
- 未知态：MANUAL 走 `resolve_unknown`（project_director.py:799，证据哈希落失败码）或 §10.6 免证据「重新处理」；AUTO 未知态自动重提，耗尽落 `remote_state_unknown_exhausted`（:80）。
- 孤儿单元：`create_run` 恢复路径先 `reclaim_expired_units` 收尾租约过期的 LEASED/SUBMITTING 单元（:575-584）。
- 输入变化：AUTO 恢复时快照身份不变而 canonical 输入变化 → 自动整轮重跑并留审计（§15.13，:587-604）；快照身份变化 → `director_snapshot_integrity_violation` fail-closed。

## 5. 故事板子系统

- generation 状态：`domain/storyboards.py` `GenerationStatus`：INTENT_PERSISTED / SUBMITTING / SUBMITTED / RUNNING / DOWNLOADED / QC_PASSED / OBJECT_STORED / SUCCEEDED / FAILED / REMOTE_STATE_UNKNOWN。
- 失败定位字段：`failure_stage` ∈ {submit, reconcile, poll, provider, download_or_qc, object_write} + `failure_code`（`application/storyboards.py`）。
- 提交分类（storyboards.py:527-560）：
  - `ProviderSubmitRejectedError`（4xx）→ FAILED 终态
  - `ProviderSubmitThrottledError`（429）→ AUTO 预算内退避重提（`_submit_retryable_failure` :838）
  - `ProviderSubmitNotDeliveredError`（connect 未送达）→ AUTO 预算内自动重提
  - 其他异常 → REMOTE_STATE_UNKNOWN
- 对账：grsai 不支持按幂等键查询，`reconcile()` 恒返 None（`adapters/grsai_grid_sheet.py:93-95`）→ AUTO 对账无果自动重提（`_auto_resubmit` :863，耗尽落 `STORYBOARD_UNKNOWN_BUDGET_EXHAUSTED_FAILURE`）。
- 版本切换：成功归档后 `complete_and_switch_current`（storyboards.py:813）；历史版本不可变，候选需显式切换（`select_current` :950）。
- 提示词安全替换：`storyboard-prompt-safety-v1` 规则命中记录随生成输入留档（`application/storyboard_prompt.py:36-69`）。

## 6. 视频子系统（H3）

- attempt 阶段（`video_generation/orchestration.py:60` `AttemptStage`）：CREATED / STAGING / NOT_SUBMITTED / SUBMITTING / SUBMITTED / RUNNING / SUCCEEDED / FAILED / CANCELLED / ARCHIVED / REMOTE_STATE_UNKNOWN；归档子阶段 `ArchiveStage`（:74）含 QC_FAILED / CLEANUP_PENDING 等。
- 节点调度：`claim_compatible_node`（兼容性+容量，STALLED 槽位不计容量；平局取最久未完成节点再最小 node_id，§7.25）；槽位租约轮换 `rotate_slot_lease`。
- 提交异常（`_stage_and_submit`，orchestration.py:1738-1802）：
  - `EngineUncertain` → REMOTE_STATE_UNKNOWN
  - `EngineRejected` → AUTO 预算内 supersede 重提；MANUAL 落未知态
  - 其他 EngineError → CLEANUP_PENDING → FAILED
- 停滞：远端零变化超阈值（ACCEPTED 30min / RUNNING 60min）→ STALLED，降频 300s 轮询（`video_generation/temporal.py:142-145`）；AUTO 经 GPU 探测自动取消重提（`_stalled_gpu_decision` orchestration.py:963）。
- QC_FAILED：`SUCCEEDED + QC_FAILED` 为等待决策的自动化终态，不参与 outbox redrive（`video_generation/worker.py:224-228`）；AUTO 下自动重生成新版本（计入 3 次预算，§15.6）。
- 批量 job：`GroupVideoBatchService`（`application/video_batch.py:127`）逐项跳过原因：video_blocked / no_current_storyboard / has_current_video / remote_acceptance_unknown / already_running / not_found / error；单组失败不中断批次。
- 取消：`request_action(CANCEL)`（orchestration.py:637）→ `cancel`（:1037）；候选 Engine 异步取消按 §16.1 对账。

## 7. 合成子系统

- outbox 行状态：PENDING / DISPATCHED / SUPERSEDED / 终态（FAILED/SUCCEEDED）。
- 临时错误码集合 `media_command_failed/timeout/cancelled`（`application/composition.py:27-29`）→ AUTO `schedule_auto_retry`（3 次，退避 5/15/30s，composition.py:23-24）。
- 失联对账：relay 每 60s 调 `reconcile_lost_dispatches`（`workflows/worker.py:288-301`），超时 DISPATCHED 行落 FAILED（`composition_workflow_lost`），AUTO 自动重排新代际。
- `begin_composition` 冲突 → `composition_in_progress`（composition.py:166-167）；候选已存在幂等返回。
- AUTO 首版成功自动切当前（composition.py:188-193）；`RETRY_COMPOSITION` 可把 SUPERSEDED 行重新武装为 PENDING（§17.3）。

## 8. 删除（附带说明，非生产链路）

- `DELETE /projects/{id}` 需确认头与项目标题精确匹配（`api/media_router.py:257`）：浏览器只允许 ISO-8859-1 头值，因此前端把标题按 UTF-8 后 base64 放在 `X-Confirm-Project-Title-B64`（支持中文/韩文/emoji）；明文 `X-Confirm-Project-Title` 仍保留给旧客户端与脚本，两者同时出现时 B64 优先，解码失败按 `deletion_confirmation_required` fail-closed。异步 `ProjectPurgeWorkflow`（`workflows/project_purge.py`）按数据库记录的对象键逐个物理删除；完成后签发 10 分钟有效的认证加密回执查 `DELETED`（`api/deletion_receipt.py`）。

## 9. 通知与可观测性

- 无出站 webhook（grsai 提交显式 `webHook:"-1"`，`adapters/grsai_grid_sheet.py:83`）。
- 前端轮询合同：项目列表 5s；运行页 2.5s/5s/10s 退避、单请求在途、终态停轮询（§10.7-10.9）；`workflow_summary` 提供阶段、阻塞原因与 `allowed_actions`。
- Provider 侧诊断留痕：导演/拆句/编译保存请求回执与响应 SHA-256；H3 每次查询持久化最近远端状态与变更时间（§7.21，节点身份仅服务端可见）。
