# 01 流程说明：创建视频项目后的端到端处理流程

> 主线为 AUTO 项目（当前代码新建项目固定 `production_mode="AUTO"`，`identity_project/service.py:628`）。
> 每步标注：触发入口 → 核心文件/类/函数/行号 → 关键行为。失败与重试细节见 `03-states-and-errors.md`，提示词原文见 `04-prompts.md`。

## 环节 0：创建项目草稿（不产生任何外部调用）

- **触发**：`POST /identity/projects`（`identity_project/router.py:559` `create_project`）。
- **处理**：`IdentityProjectService.create_project`（`identity_project/service.py:573`）
  - 校验频道默认音色门禁：CosyVoice 源要求 `channel.default_voice_version_id`，EL 源要求 `channel.default_cloned_voice_id`，缺失抛 `channel_default_voice_required`（service.py:592-608）。
  - 引用校验 `_validate_project_refs`（service.py:1138）：角色图去重与归属、音色语言兼容（CosyVoice）/克隆音色归属（EL）。
  - 持久化草稿 `Project`，`production_mode="AUTO"`（service.py:617-634）。
- **冻结前编辑**：`PATCH /identity/projects/{id}`（router.py:621 → service.py:1024 `edit_project`，乐观锁 `expected_revision`）；文案预检 `POST /narration/preflight`（`api/narration_router.py:63` → `ProjectNarrationService.preflight`，`application/narration.py:783` → `segment_pasted_script` 确定性分句 `domain/narration.py:274`，逐句估算时长仅作冻结前提示）。
- **产出**：PostgreSQL 项目草稿行；无快照、无任务、无外部调用。

## 环节 1：确认配音 / 一键提交（全链路唯一授权点）

- **触发**：`POST /narration/confirm`，HTTP 202（`api/narration_router.py:95` `confirm`）；前端入口 `frontend/api-client.js:15` `confirmNarration`。
- **处理**：`ProjectNarrationService.confirm_and_start`（`application/narration.py:792`）
  1. 写守卫 `assert_writable`（项目不在删除流程中）。
  2. AUTO 且快照未冻结 → 先经拆句 agent（环节 2），失败则整体提交失败、不落任何快照/job（narration.py:799-814）。
  3. 调 `SqlProjectSnapshotAdapter.confirm_and_enqueue`（`api/narration_runtime.py:146`）在**单个数据库事务**内完成：
     - 项目行 `SELECT ... FOR UPDATE` 加锁（narration_runtime.py:154-159）；
     - 重入幂等：已有快照+job 时直接返回既有 job（narration_runtime.py:171-187）；
     - 应用拆句计划 `apply_sentence_split_plan`（narration_runtime.py:207-209）；
     - 计算快照哈希并生成配音 job：`job_id = "narration_" + sha256(...)`（narration_runtime.py:211-246）；
     - 冻结不可变快照：`IdentityProjectService.confirm_narration`（`identity_project/service.py:1043`）→ `_build_snapshot`（service.py:1078，CosyVoice/EL 两分支）；
     - AUTO 项目同事务创建 `auto_pipeline_runs` 运行（RUNNING）：`create_active_run_in_session`（narration_runtime.py:257-262）；
     - 同事务创建**项目级前置导演** run + 全部单元：`ProjectDirectorService.prepare_run`（`application/project_director.py:626`），幂等键 `narration-confirm:{project_id}:{snapshot_hash[:32]}`，trigger=`NARRATION_CONFIRMATION`（narration_runtime.py:302-330），并写故事板 outbox 事件 `PROJECT_DIRECTOR_RUN_REQUESTED`（`adapters/persistence/storyboards/project_director_repository.py:390`）；
     - 持久化不可变分句脚本与 job 到 `NarrationProjectRecord`（narration_runtime.py:263-301）。
- **产出**：冻结快照、配音 job（PENDING）、AUTO 运行（RUNNING）、前置导演 run（INTENT_PERSISTED）+ outbox 事件。响应 `{project_id, job_id, state: "QUEUED"}`。

## 环节 2：超长句拆句 agent（仅 AUTO，冻结前）

- **位置**：`sentence_split/service.py` `SentenceSplitService.plan_splits`（:62）。
- **流程**：`segment_pasted_script`（v3 规则）→ `estimate_split_risk_sentences` 找出估算超阈值（默认 12s）的风险句 → 并发 ≤4 逐句调 LLM（`sentence_split/service.py:72-73`）→ `validate_split_parts` 无损拼接校验。
- **Provider**：DeepSeek（`adapters/deepseek_sentence_split.py`），每句最多 3 次尝试、退避 5s/15s/30s、429 尊重 Retry-After；提示词见 `04-prompts.md` §1。
- **失败语义**：任一风险句最终失败 → 整个 `confirm` 请求失败（`sentence_split_unavailable` / `sentence_split_provider_rejected` / `sentence_split_response_invalid` / `sentence_split_lossy`），不落快照。

## 环节 3：任务调度（Temporal + outbox relay + AUTO 编排器）

系统有 **5 条 outbox/relay 回路** 与 **1 个 AUTO 巡检器**，全部装配在 Worker 进程 `workflows/worker.py:build_production_worker_assembly`（:176），组件名单见 worker.py:82-99：

| 组件 | 代码 | 队列/循环 |
| --- | --- | --- |
| narration-outbox-relay | `worker.py:324` `narration_relay` → `ProjectNarrationService.dispatch_pending_jobs`（`application/narration.py:827`） | 每 2s 扫描 PENDING 配音 job → `TemporalNarrationStarter.start`（`workflows/narration.py:138`），队列 `tubeflow-narration` |
| storyboard-outbox-relay | `worker.py:306` → `StoryboardOutboxDispatcher.dispatch_one`（`application/storyboard_outbox.py`）→ `TemporalStoryboardWorkflowStarter`（`workflows/storyboards.py:176`） | 队列 `storyboards`，按事件类型分发 5 种工作流（storyboards.py:180-192） |
| h3-outbox-relay | `worker.py:492` → `dispatch_video_outbox_once`（`video_generation/worker.py:45`）+ `redrive_stale_video_outbox`（worker.py:197） | 队列 `tubeflow-h3-video`（`config.py:45`） |
| composition-outbox-relay | `worker.py:280` `relay_forever` → `CompositionOutboxRelay.relay_one`（`application/composition_outbox.py:71`）→ `TemporalCompositionDispatcher`（`workflows/composition.py:124`） | 队列 `tubeflow-project-purge`（workflows/composition.py:23） |
| purge-outbox-relay | `worker.py:413`（项目删除，超出本文范围） | 同上队列 |
| auto-pipeline-sweeper | `worker.py:569` `auto_pipeline_sweep` → `AutoPipelineService.sweep_once`（`auto_pipeline/service.py:130`） | 每 `auto_pipeline_sweep_interval_seconds`（默认 20s，`config.py:130`）推进全部 RUNNING 运行 |

- **AUTO 编排器**：`AutoPipelineService.advance`（`auto_pipeline/service.py:84`）持锁按权威事实执行**恰好一个动作**，决策树 `_advance_locked`（service.py:142-349）：配音未就绪 → 导演未就绪 → 物化 → 故事板草稿/生成 → 视频 → 合成 → 完成。推进靠「踢点」驱动：各阶段终态回调里调用 `auto_kick`（配音 `workflows/narration.py:104`，导演/故事板 `workflows/storyboards.py:49`，视频 `video_generation/temporal.py:38`，合成 `workflows/composition.py:82`），巡检器兜底。
- 所有 Temporal 工作流启动均用 `WorkflowIDReusePolicy.REJECT_DUPLICATE` 等策略保证幂等（如 `workflows/narration.py:153`）。

## 环节 4：逐句配音（CosyVoice 或 ElevenLabs）

- **工作流**：`NarrationWorkflow`（`workflows/narration.py:31`）：按 20 句一批循环调活动 `synthesize_narration_batch`（每批最多 3 次 provider 尝试；活动级 Temporal 重试 maximum_attempts=100、`NarrationIntentConflictError` 不重试），最后调 `finish_narration_job`。
- **批次执行**：`ProjectNarrationService.synthesize_batch`（`application/narration.py:925`）→ `SubmitSentenceNarration.execute_batch`（:255）
  - 幂等：按句幂等键查重；已有 checkpoint 直接复用归档 ZIP；`SAFE_TO_RETRY` 且在退避窗口内的句子跳过（narration.py:986-995）。
  - 源路由 `provider_for`（narration.py:79）：按快照冻结的 `tts_provider` 分派。
- **CosyVoice 路径**：`CosyVoiceHttpAdapter.submit_batch`（`adapters/cosyvoice.py:54`）→ `POST {base}/batch_generate`，multipart 上传参考 ZIP + 每句一个 txt（格式见 `04-prompts.md` §8.1），响应为 ZIP 字节流。
- **ElevenLabs 路径**：`ElevenLabsNarrationProvider`（`application/elevenlabs_narration.py:56`）：逐句串行 `submit`（:86），key 池轮换（`_synthesize_with_rotation` :143），缺失克隆音色时自动补克隆（`_clone_into_account` :221），单请求字符上限拦截（:148），request stitching（`previous_request_ids`，:153-167）；适配器 `ElevenLabsHttpClient.synthesize`（`adapters/elevenlabs.py:57`）→ `POST /v1/text-to-speech/{voice_id}`；结果打包成与 CosyVoice 同构的 ZIP（`_package` :258）。
- **QC 与归档**：`_complete_raw`（narration.py:560）：原始 WAV QC（`inspect_wav`）→ 归一化（48kHz/单声道/16bit）→ 归一化 QC → 时长一致性校验 → 原始与归一化两份资产分别归档对象存储 → `save_completed` 落 `COMPLETED`。
- **提示词**：无（TTS 为文本→语音，不涉 LLM 提示词；请求文本格式见 `04-prompts.md` §8）。

## 环节 5：实测时间轴与配音分组

- **触发**：`finish_narration_job` 活动 → `ProjectNarrationService.finish_job`（`application/narration.py:1141`）。
- **全部成功时**：`finalize_timeline`（:1106）→ `rebuild_authoritative_timeline`（:682）→ `build_measured_timeline`（`domain/narration.py:418`，逐句按实测帧数/采样率排精确起止）→ `group_timeline`（`domain/narration.py:482`，顺序装组、组跨度 >15s  flush，单句 >15s 标记 `video_blocked` + `single_sentence_exceeds_15000ms`）。
- **随后钩子**：`self._director_materializer(...)`（narration.py:1156-1157）触发环节 7 的物化；AUTO 清阻断原因（:1158-1159）。
- **有失败时**：job 落 `FAILED`/`PARTIAL_FAILED`；AUTO 项目 `_schedule_auto_retries`（narration.py:1181）按预算轮次为失败句生成持久化重投 job（退避 5s/15s/30s，每句 3 次封顶），无法自动恢复的句序写入项目级阻断原因。
- **AUTO 编排器接管**：配音未就绪期间 advance 停留 `phase="narration"`，若存在阻断原因则 `resume_auto_narration`（narration.py:1275）重估入队，仍有剩余则暂停运行（`auto_pipeline/service.py:145-157`）。AUTO 下实测超长句整项目暂停（`REASON_OVERSIZED_SENTENCE`，service.py:164-181），替代 MANUAL 的跳过规则。

## 环节 6：项目级前置导演（LLM，与配音并行启动）

- **入口**：确认事务内创建（环节 1），或用户/编排器恢复创建（`create_run`，`application/project_director.py:549`；AUTO 恢复走 `_DirectorResumeAdapter.create_recovery_run`，`auto_pipeline/factory.py:35`，幂等键 `auto-pipeline-director:{project_id}:r{round}`）。
- **单元化**：`prepare_run`（project_director.py:626）→ `_prepare_units`（:1057）：1 个 `GLOBAL_PLAN` 全局规划单元 + 每个非视觉分隔句 1 个确定性单元（不调 LLM）+ 按段打包的 `SHOT_PLAN` 单元（上下文 ±3 句、全局开头 16 句）；canonical 输入哈希含 prompt 版本 `project-director-global-plan-v5` / `project-director-unit-v5`（:58-59）。
- **执行**：`ProjectDirectorRunWorkflow`（`workflows/storyboards.py:124`）按 `director_parallelism`（默认 4，`config.py:101`）并发执行活动 `advance_project_director`（:78）→ `ProjectDirectorService.advance`（project_director.py:702）：认领租约（20 分钟，:78）→ 调 `OpenAiCompatibleDirectorModel.generate`（`adapters/director_model.py:61`，`POST {base}/chat/completions`，json_schema 严格输出，带 `Idempotency-Key` 头）→ 输出校验（`_validated_plan_document` :1272 / `_validated_output` :1348，含相邻镜头多样性拒收）。
- **提示词**：全局规划 `_plan_prompt`（:1178）、单元 `_prompt`（:1215），占位符 `__GLOBAL_PLAN_DOCUMENT__` 由 `_bind_prompt`（:492）注入全局大纲；原文见 `04-prompts.md` §2。
- **重试/失败**：每单元 3 次预算、退避 5s/15s、429 尊重 Retry-After；AUTO 未知态按预算自动重提，耗尽落 `remote_state_unknown_exhausted`（:80）。全部单元成功 → run `SUCCEEDED` → 活动内触发 `materialize_if_ready`（workflows/storyboards.py:83）并踢 AUTO 编排器。
- **API**：`POST /projects/{id}/director-preplan/runs`（`api/project_director.py:266`）、`GET .../runs/latest`（:297）、`POST .../runs/{run_id}/manual-resolution`（:319）、`GET .../director-preplan/current`（:342）、`GET .../director-materialization/current`（:353）。

## 环节 7：确定性重映射物化（无外部 AI）

- **位置**：`ProjectDirectorService.materialize_if_ready`（`application/project_director.py:904`）→ `materialize`（:834）。
- **前置条件**：存在当前前置导演修订 + 权威配音分组精确覆盖全部视觉句子（:834-862），否则 `narration_not_ready`。
- **处理**：按组把前置导演镜头确定性重映射为组级候选 `_candidate`（:1527）：镜头按句子链接筛选入组、重建 shot_id 与血缘；超过组级上限 6 个镜头时调 `converge_group_shots`（`application/plan_convergence.py`，规则版本 `adjacent-merge-v1`）做相邻合并收敛；算法版本 `controlled-semantic-lineage-remap-v4`（:64）。冲突/歧义 → `ReviewState.REQUIRED`。
- **产出**：`ProjectDirectorMaterialization`（SUCCEEDED 或 REVIEW_REQUIRED）+ 每组 `ProjectDirectorGroupCandidate`。
- **提示词**：无。

## 环节 8：分组导演草稿播种（无外部 AI）

- **AUTO 路径**：编排器在 `phase="storyboard_drafts"`（`auto_pipeline/service.py:195-245`）：`StoryboardBatchService.seed_drafts`（`application/storyboard_batch.py:154`，`auto_approve_ambiguous=True`，为歧义相邻对附加系统审批 `_system_approvals_for` :76，理由常量 :70）→ 仍有 `REQUIRED` 草稿则 `repair_ambiguous_drafts`（:206）→ 仍不合法则暂停 `director_validation_failed`。
- **MANUAL 路径**：`POST /projects/{id}/storyboard-batch/director-drafts`（`api/storyboard_batch.py:95`）手动播种；单组编辑 `PUT /storyboard-groups/{group_id}/director/draft`（`api/storyboards.py:184`），应用 `POST .../director/apply`（:242）。
- **保存即续跑**：AUTO 暂停期间保存导演草稿会按原因码守卫恢复运行（`AutoPipelineService.resume_if_paused_for`，`auto_pipeline/service.py:110`）。
- **提示词**：无。

## 环节 9：故事板整图生成（grsai gpt-image-2）

- **AUTO 触发**：编排器 `phase="storyboard"`（service.py:247-283）→ `StoryboardBatchService.generate`（`application/storyboard_batch.py:254`）逐组 `_generate_one`（:278）：在途/未知态跳过，草稿先 `apply_draft`，然后 `StoryboardGenerationService.create`（`application/storyboards.py:422`）。
- **MANUAL 触发**：`POST /projects/{id}/storyboard-batch/grid-sheet-generations`（`api/storyboard_batch.py:102`）或单组 `POST /storyboard-groups/{group_id}/grid-sheet-generations`（`api/storyboards.py:261`）。
- **意图创建**：`create` 校验配音门禁（`narration_ready`）与导演修订一致 → `_shot_time_ranges` 计算角标时间 → `build_grid_sheet_prompt`（`application/storyboard_prompt.py:94`，确定性网格布局声明+逐格镜头字段+角标绘字要求）→ `apply_prompt_safety`（storyboard_prompt.py:57，违禁词最小替换留档）→ canonical 哈希 + 幂等键 → 落 `INTENT_PERSISTED`。
- **执行**：outbox 事件 `STORYBOARD_GENERATION_REQUESTED` → `GridSheetGenerationWorkflow`（`workflows/storyboards.py:109`）循环调 `advance_storyboard_generation`（:57）→ `StoryboardGenerationService.resume`（storyboards.py:495）：
  1. `claim_submit` → 角色参考图经 grsai 文件服务上传换公网 URL（`adapters/grsai_grid_sheet.py:168`）→ `submit_grid_sheet`（:61）`POST /v1/draw/completions`（`webHook:"-1"` 关闭回调、`shutProgress:true`、`aspectRatio` 官方 1K 像素档、`quality` 默认 high）；
  2. `SUBMITTED/RUNNING` → `poll`（:97）`POST /v1/draw/result`；
  3. 成功 → 下载（`_download_atomic`）→ Pillow 技术 QC（哈希/尺寸/格式）→ `put_verified` 归档对象存储 → `complete_and_switch_current`（storyboards.py:813）落 `SUCCEEDED` 并切换当前指针；
  4. 终态后活动 finally 中踢 AUTO 编排器（workflows/storyboards.py:63-67）。
- **失败分类**：connect 未送达/429 → AUTO 预算内自动重提；4xx 拒绝 → 直接 FAILED；发送后不确定 → `REMOTE_STATE_UNKNOWN`，AUTO 对账无果自动重提（`_auto_resubmit` :863）。
- **提示词**：`04-prompts.md` §3。

## 环节 10：视频提示词编译 + H3 组视频（KineStack REF2VA）

- **AUTO 触发**：编排器 `phase="video"`（service.py:285-331）：`_resume_or_start_video_job`（:353）续跑/新建 `GroupVideoBatchService` 批量 job 并 `process_all` 推进（`application/video_batch.py:194/227`），逐组 `ManualH3IntentWriter.create`（`video_generation/persistence.py:335`）。
- **MANUAL 触发**：`POST /group-videos/groups/{group_id}/generations`（`video_generation/api.py`，`create_video_router` :155）；批量 `api/video_batch.py`。
- **意图创建（同步含 LLM 编译）**：写事务外先编译提示词（persistence.py:344-356）：`compile_video_prompt` → `VideoPromptCompiler.compile`（`video_prompt/compiler.py:62`）→ DeepSeek（`adapters/deepseek_video_prompt.py:35`）；策略默认 `tubeflow_storyboard_v2`；输出校验失败修复重试（AUTO 2 次，与临时故障共享 3 次预算，compiler.py:81-167）；绑定行由代码确定性插入 `insert_binding_line`（`video_prompt/domain.py:164`）。事务内持久化不可变 attempt + outbox + 故事板绑定。
- **执行**：h3-outbox-relay → `GroupVideoRecoveryWorkflow`（`video_generation/temporal.py:120`）循环调 `recover_group_video_attempt` → `DurableGroupVideoOrchestrator.advance`（`video_generation/orchestration.py:519`）→ `_stage_and_submit`（:1655）：校验配音字节 → `claim_compatible_node` 认领兼容节点槽位 → 逐张 `engine.stage_asset` 上传参考图（`POST /staged-assets`，`video_generation/engine.py:159`）→ `engine.submit`（`POST /executions`，engine.py:199）→ 之后每轮 `resume` 用 `engine.inspect`（`GET /executions/{id}`）轮询远端 → `SUCCEEDED` 后 `archive`（orchestration.py:1344）：`outputs`/`download` 下载 → 视频 QC → 不可变归档 → 首版自动设为当前（`mark_current_if_absent`）→ `confirm_archive` 通知节点（engine.py:269）。
- **停滞处理**：远端状态零变化超阈值判 STALLED，降频继续查询（`stalled_poll_seconds` 默认 300s）；AUTO 结合 GPU 探测自动取消重提（`_stalled_gpu_decision` orchestration.py:963，`_auto_resubmit_after_cancel` :1011）。
- **提示词**：编译器系统模板与用户载荷见 `04-prompts.md` §4；编译产物即为提交给 H3 的视频提示词。

## 环节 11：合成（FFmpeg，无外部 AI）

- **AUTO 触发**：视频全部就绪后编排器 `_compositions.start`（service.py:347-348）→ `SqlAlchemyCompositionRepository.enqueue_composition`（与手动同一仓储路径，`auto_pipeline/factory.py:59`）。
- **MANUAL 触发**：`POST /projects/{id}/composition`（`api/media_router.py:195`）；重试 `:207`。
- **执行**：composition outbox → `manual-composition-v1` 工作流（`workflows/composition.py:34`）→ 活动 `compose-project-media-v1`（:60）→ `ManualCompositionService.compose_revision`（`application/composition.py:231`）→ `_compose_request`（:146）：`render_srt` 生成字幕（排除被阻止句、连续重排）→ `begin_composition` 认领 → `FFmpegComposer.compose`（`adapters/ffmpeg.py:149`）：逐组视频 trim 到实测帧、TTS 拼接（volume 1.0）、H3 环境声降音量 0.25 混音（amix + alimiter 0.95）、组间 concat、输出 libx264/yuv420p/24fps + AAC 48kHz 立体声 → `activate_candidate` 归档 → AUTO 首版自动切当前（composition.py:188-193）。
- **完成收口**：编排器检测到当前成片 → `close_superseded` 收口非终态 outbox 行 → run `COMPLETED`（service.py:333-337）。
- **提示词**：无。

## 环节 12：导出 ZIP（用户手动）

- **触发**：`GET /projects/{id}/export`（`api/media_router.py:219`）。
- **处理**：`ObjectExportFactory`（`api/media_runtime.py:401`）→ `StreamingExport`（`application/export.py:12`）：临时目录组卷 `final.mp4` + `subtitles.srt` + 当前封面原图（存在时），64KB 分块流式响应，结束或中断即清理临时目录（export.py:53-60）；孤儿导出由 `cleanup_orphan_exports`（:67）兜底。
- **提示词**：无。

## 环节 13：封面（第十阶段，独立手动功能，不在 AUTO 链路）

- **入口**（`api/cover.py`）：参考图上传 `:374`/`:538`；视觉解析 `POST .../cover-style/analyze`（`:442`/`:605`）；频道风格保存 `:468`、项目内 promote `:631`；角色卡 `:490`、角色卡解析 `:520`；提示词生成 `POST /projects/{id}/cover/prompts`（`:638`）；封面图生成 `POST /projects/{id}/cover/images`（`:712`）。
- **编排**：各动作写 cover outbox，经 `CoverOutboxDispatcher` → 4 种 Temporal 工作流（`workflows/cover.py:93-134`）。
- **提示词生成编排**：`CoverPromptService.generate`（`application/cover_prompts.py:281`）：封面文字（失败回退标题）→ 两版并行画面提示词（温度 0.8/1.0）+ 各一次定向整改 → 评委择优（温度 0.2，<112 分记低分警告）→ 装配最终提示词并切当前指针。
- **出图**：`LingkeaiCoverImageProvider.generate`（`adapters/cover_image.py:48`）`POST {base}/images/generations`。
- **提示词**：`04-prompts.md` §5（视觉解析/封面文字/画面提示词/评委/角色卡/角色绑定全套原文）。

## 环节 14：状态更新与「回调通知」（实为轮询合同）

代码中无出站 webhook/回调（见 README 口径说明）。状态更新与通知的实际形态：

- **状态写入**：每一环节的状态都先落 PostgreSQL（配音 attempts/job、导演 run/unit、故事板 generation、视频 attempt、合成 revision/attempt、AUTO run），Temporal 只是编排。
- **页面读取**：
  - 项目列表：`GET /identity/projects`（router.py:281），每 5s 轮询（§10.9）；
  - 权威摘要：`GET /identity/projects/{id}/workflow-summary`（router.py:592 → `IdentityProjectService.workflow_summary`，`identity_project/service.py:645`；AUTO 附加 `auto_pipeline` 状态与暂停细节白名单 `_with_auto_pipeline` :663）；
  - 配音：`GET /narration/projects/{id}/status`（`api/narration_router.py:106`）；
  - 故事板/视频/合成/封面各自的状态 GET（`api/storyboards.py:293`、`video_generation/api.py:52`、`api/media_router.py:147-164`、`api/cover.py:666/729`）。
- **公共状态映射**：`api/public_status.py:31` `public_state` 把内部状态映射为 `public_status`/`message`/`allowed_actions` 三元组（NOT_STARTED/QUEUED/RUNNING/RECOVERY_REQUIRED/SUCCEEDED/FAILED/CANCELLED）。
- **AUTO 完成通知**：运行 `COMPLETED` 后页面经轮询看到「视频生产完成」；新成片已自动切当前（composition.py:188-193）。

## 附：代码中存在但当前链路无 API 入口的部分

- `DirectorGenerationService`（`application/director_generation.py:144`，旧「分组导演」LLM 路径，提示词见其 `_prompt` :500）：仅在 Worker 侧装配为活动（`api/storyboard_runtime.py:207`），其 outbox 事件 `DIRECTOR_GENERATION_REQUESTED` 由仓储在创建该尝试时写入（`adapters/persistence/storyboards/repository.py:503`）；**未找到当前 API 触发入口**。【推断】它是前置导演+确定性重映射方案（§5.6）落地前的旧路径，现 AUTO 链路不经过它；依据：当前 AUTO 决策树（`auto_pipeline/service.py:142-349`）无对应阶段，草稿播种走 `StoryboardBatchService.seed_drafts` 的确定性候选。
- `api/app.py:410` 的 SPA 兜底路由与 `storyboard-preview-first-3.html` 等根目录文件属于前端/预览资产，不参与本链路。
