# 用户创建视频项目后的端到端流程说明

> 本文是主流程文档。图形总览见 [PROJECT_VIDEO_E2E_SEQUENCE_20261014.md](./PROJECT_VIDEO_E2E_SEQUENCE_20261014.md)；状态与失败细节见 [PROJECT_VIDEO_E2E_STATE_ERRORS_20261014.md](./PROJECT_VIDEO_E2E_STATE_ERRORS_20261014.md)；P1–P11 的原文见 [PROJECT_VIDEO_E2E_PROMPTS_20261014.md](./PROJECT_VIDEO_E2E_PROMPTS_20261014.md)。除特别标注外，以下均为【已确认】的当前代码事实。

## Step 0：创建草稿项目，不启动生产

### 触发与实现

- 前端/API：`POST /identity/projects`。
- 路由：`backend/src/tubeflow_test/identity_project/router.py:559-582`，`create_project()`；`/identity` 前缀见同文件 `:244`。
- 服务：`backend/src/tubeflow_test/identity_project/service.py:573-634`，`IdentityProjectService.create_project()`。
- 前端实际 API 地址：`frontend/api-client.js:13-15`。

### 输入、输出与状态

- 输入：`channel_id`、用户粘贴的 `script`、1–8 张角色图 ID、`visual_style`、`locale`、`aspect_ratio`、配音源/音色、可选标题和期望时长。
- 创建前校验：频道默认音色门禁在 `identity_project/service.py:589-608`；项目引用校验在 `:609-616`。
- 输出：持久化草稿 `Project`，`snapshot=None`，并未创建生产任务。
- 当前创建路径显式写入 `production_mode="AUTO"`：`identity_project/service.py:617-632`，关键行 `:628`。请求模型没有让用户选择 MANUAL 的字段。
- 失败：引用、语言、音色或默认音色校验失败会直接拒绝；该路由没有自动重试。
- Prompt：**无**。

### “脚本生成”的准确边界

**【未找到相关实现】**从主题、链接或 brief 自动撰写完整项目脚本的链路。当前 `script` 是创建请求中的用户输入；后续只有确定性分句和 AUTO 风险句二级拆分，见 Step 1 与 Prompt 文档 P1。

## Step 1：提交生产；可选拆句后原子冻结与入队

### 触发入口

- 当前前端：`app.js:2539-2566`，AUTO 直接确认；MANUAL 才先走 preflight。
- API：`POST /narration/confirm`，路由为 `backend/src/tubeflow_test/api/narration_router.py:95-104`，HTTP 202。
- 应用服务：`backend/src/tubeflow_test/application/narration.py:792-818`，`ProjectNarrationService.confirm_and_start()`。

### 1A. AUTO 风险句拆句（同步、冻结前）

- `confirm_and_start()` 仅对尚未冻结的 AUTO 项目调用拆句：`application/narration.py:798-817`。
- `SentenceSplitService.plan_splits()`：`sentence_split/service.py:62-80`；仅估算风险句调用 Provider，无风险句直接返回空计划。
- 每个风险句最多 3 次尝试，退避常量 5/15/30 秒，429 可尊重 `Retry-After`：同文件 `:82-156`。
- Provider：`adapters/deepseek_sentence_split.py:38-90`，`POST {base_url}/chat/completions`。
- 任一风险句最终失败会使本次提交失败；由于调用发生在冻结事务之前，本次不会新建快照、配音 job 或 AUTO 运行。
- Prompt：**P1（拆句 system + JSON user + 两类修复原因）**。

### 1B. 同一数据库事务完成四类写入

核心：`backend/src/tubeflow_test/api/narration_runtime.py:146-336`，`SqlProjectSnapshotAdapter.confirm_and_enqueue()`。

1. **行锁与幂等**：`:153-187`；已有有效快照/job 时返回既有 job。
2. **应用拆句计划并计算快照哈希**：`:193-235`。
3. **创建配音 job**：`:236-246`，初始 `PENDING`，`job_id="narration_"+snapshot_hash`。
4. **冻结业务快照**：`:247-256` 调 `IdentityProjectService.confirm_narration()`；其领域校验在 `identity_project/service.py:1043-1076`，持久化在 `adapters/persistence/identity_project/repository.py:558-625`。
5. **AUTO 运行**：`api/narration_runtime.py:257-262` 创建 `AutoPipelineRun(RUNNING, authorization_round=1, phase=narration)`；初始领域对象见 `auto_pipeline/domain.py:149-166`。
6. **导演意图**：`api/narration_runtime.py:302-330` 调 `ProjectDirectorService.prepare_run()`。这里使用 `_IntentOnlyDirectorModel`（同文件 `:88-90`），确认事务内不会调用导演 Provider。
7. **导演 outbox**：`adapters/persistence/storyboards/project_director_repository.py:378-399` 写 `PROJECT_DIRECTOR_RUN_REQUESTED(PENDING)`。

输出为 `{"project_id","job_id","state":"QUEUED"}` 加公开状态字段。**该 HTTP 请求本身没有执行 `AutoPipelineService.advance()`。**

### 另一个容易混淆的入口

`POST /identity/projects/{project_id}/confirm-narration` 位于 `identity_project/router.py:633-646`，只调用快照冻结服务；它不是当前前端 AUTO 生产主链，不会在该路由内创建配音 job、AUTO 运行或导演 outbox。

## Step 2：Worker、outbox 与 Temporal 调度

### 调度骨架

- 生产装配：`backend/src/tubeflow_test/workflows/worker.py:176-584`，`build_production_worker_assembly()`。
- 配音派发：`application/narration.py:827-842`，`dispatch_pending_jobs()`；Worker 2 秒 relay 见 `workflows/worker.py:322-328`。
- 故事板/导演 outbox 派发：`application/storyboard_dispatcher.py:25-45`；事件到 workflow 的映射在 `workflows/storyboards.py:176-207`。
- 合成 relay：`workflows/worker.py:280-302`；H3 relay 与 Temporal worker 装配在 `:431-557`。
- AUTO 巡检：`AutoPipelineService.sweep_once()`，`auto_pipeline/service.py:130-136`；周期接线 `workflows/worker.py:558-583`，代码默认 20 秒（`config.py:129-130`）。

### AUTO 编排器的工作方式

- `AutoPipelineService.advance()`：`auto_pipeline/service.py:84-102`。锁定活动 run，只处理 `RUNNING`，每次最多执行一个编排动作。
- 决策树：同文件 `:142-349`，依次检查配音、超长句、导演、物化、故事板、视频与合成事实。
- 权威事实读取：`adapters/persistence/auto_pipeline.py:257-315`，不是只看 `payload.phase` 书签。
- **【未找到相关实现】**AUTO 自己独立的 Temporal workflow 或专属 auto-pipeline outbox；它通过 `kick/sweep` 调用各阶段既有服务与 outbox。
- Prompt：**无**。

## Step 3：真实配音、QC、实测时间轴与分组

### 调用链

1. `NarrationWorkflow.run()`：`workflows/narration.py:30-64`，每批最多 20 句。
2. activity `synthesize_narration_batch`：同文件 `:77-87`。
3. `ProjectNarrationService.synthesize_batch()`：`application/narration.py:925-1104`。
4. `SubmitSentenceNarration.execute_batch()`：同文件 `:255-558`。
5. 按冻结 `tts_provider` 进入 CosyVoice 或 ElevenLabs。

### Provider 输入

- **CosyVoice**：`adapters/cosyvoice.py:51-157`，`POST {base_url}/batch_generate`，multipart 含参考 ZIP 和每句文本文件。文本文件是确定性 TTS 控制文本：`情感: {emotion}\n译文: {text}\n`（`:70-78`），不是 LLM Prompt。
- **ElevenLabs**：`application/elevenlabs_narration.py:90-141` 逐句串行；HTTP 适配器 `adapters/elevenlabs.py:57-117` 调 `POST /v1/text-to-speech/{voice_id}?output_format=mp3_44100_128`。body 为 `text/model_id` 和可选 `previous_request_ids`。
- Prompt：**无**；两条 TTS 主链均未找到 system/user LLM 消息。

### 输出、QC 与存储

- raw 检查、归一化、跨媒体时长校验、归档：`application/narration.py:560-647`。
- 生产 raw inspector 与音频 QC：`adapters/audio_qc.py:38-221`；FFmpeg 归一化在同文件 `:267-647`。规范化目标为 48kHz、单声道、16-bit PCM。
- 每句保存原始与规范化音频资产、QC/实测事实和 `NarratedSentence`；媒体字节进入对象存储，权威业务状态进入 PostgreSQL。
- 实测时间轴：`domain/narration.py:418-479`，以真实帧数/采样率推进，不用估算时长。
- 分组：`domain/narration.py:482-522`，顺序贪心装组，普通组不超过 15000ms；单句严格大于 15000ms 会形成 `video_blocked` 组。
- AUTO 遇到实测超长组会整项目暂停 `oversized_sentence`：`auto_pipeline/service.py:163-181`，不是自动跳过后继续出片。

### 状态、重试与回调

- attempt 状态定义：`domain/narration.py:80-90`；job 状态为 `PENDING/DISPATCHED/COMPLETED/PARTIAL_FAILED/FAILED`。
- AUTO 对 `SAFE_TO_RETRY/REMOTE_STATE_UNKNOWN/OUTPUT_INVALID` 按每句预算轮次派生重试 job：`application/narration.py:1181-1273`。
- `finish_job()` 在全部成功时构建时间轴并调用导演物化钩子：`application/narration.py:1141-1179`。
- `finish_narration_job` activity 后调用内部 `auto_kick`：`workflows/narration.py:89-105`。

## Step 4：项目级前置导演，与配音并行

### 调度与模型调用

- `ProjectDirectorRunWorkflow`：`workflows/storyboards.py:124-144`，并行执行 `advance_project_director`。
- activity：同文件 `:78-91`；调用 `ProjectDirectorService.advance()`。
- 主执行：`application/project_director.py:702-797`，认领 unit、调用模型、校验输出并持久化。
- 单元准备：同文件 `:1057-1122`；先有 1 个 `GLOBAL_PLAN`，再有多个 `SHOT_PLAN`。目标每单元 3–6 句，最多 8 句；1200 字符是达到目标最少句数后的切包阈值，并非所有单元的绝对字符上限（`_pack_section()`，`:1683-1699`）；常量见 `:58-82`。
- 默认并行度：`workflows/storyboards.py` 使用 `settings.director_parallelism`；代码默认 4、范围 1–16（`config.py:101`）。【运行值未确认】环境可覆盖。
- HTTP 模型适配器：`adapters/director_model.py:61-98`，仅发送一条 `system` 消息，`response_format=json_schema(strict)`。
- 生产参数：`api/storyboard_runtime.py:121-133,202-224`，`temperature=0.2`；代码默认模型 `gpt-5.6-sol`（`config.py:65-71`）。

### 输入与输出

- 输入来自冻结项目快照、冻结句子、角色序号与资产哈希、风格、语言、画幅；装配和完整性复核见 `adapters/persistence/storyboards/project_director_repository.py:91-185`。
- `GLOBAL_PLAN` 输出章节大纲；验证后的 canonical plan JSON 再绑定到各 `SHOT_PLAN` 的 `__GLOBAL_PLAN_DOCUMENT__`。
- `SHOT_PLAN` 输出无毫秒时间码的项目级镜头及句子链接。
- 非视觉分隔单元在模型调用前由本地确定性完成：`application/project_director.py:738-755`，不会把对应 Prompt 发给模型。
- Prompt：**P2（全局规划）与 P3（单元规划）**。

### 状态与异常

- run/unit 状态：`domain/project_director.py:18-44`。
- 每单元最多 3 次、退避 5/15/30 秒：`application/project_director.py:71-80,1022-1055`。
- run 成功后 `_finalize()` 形成不可变导演修订：同文件 `:1487-1525`；失败会使 AUTO 暂停 `director_preplan_failed`。
- 终态后 activity 先尝试物化，再 `auto_kick`：`workflows/storyboards.py:78-91`。

## Step 5：确定性重映射、6 镜收敛与草稿播种

### 物化触发

共有三个生产触发点：配音全部完成（`application/narration.py:1141-1160`）、导演 run 成功（`workflows/storyboards.py:78-91`）、AUTO 幂等补踢（`auto_pipeline/service.py:188-193`）。它们进入同一 canonical 物化路径，不调用外部 AI。

### 重映射与收敛

- `ProjectDirectorService.materialize()`：`application/project_director.py:834-902`。canonical 输入包括算法版本、前置修订、快照哈希和实测分组。
- 当前算法版本：`controlled-semantic-lineage-remap-v4`，常量在同文件 `:61-64`。
- 每组候选构造：同文件 `:1527-1565`。按稳定句子身份截取前置镜头并保留来源血缘。
- 最终组上限：`DIRECTOR_GROUP_SHOT_LIMIT=6`，`domain/storyboards.py:23-26`。
- 超过 6 镜时调用 `converge_group_shots()`：`application/project_director.py:1553-1565`；纯函数实现位于 `application/plan_convergence.py:30-113`。
- 合并规则：选择“动作结果+画面描述”总字符最短的相邻对，平局取前镜序号较大者；保留前镜结构字段，拼接两段文本，重定向句子与来源血缘，最后重排为 1..n。
- 仍有覆盖缺失、冲突或领域校验失败时，不以自动合并伪装成功；候选会带冲突/审阅状态。

### 草稿播种

- AUTO：`auto_pipeline/service.py:195-245`。
- 服务：`application/storyboard_batch.py:154-204`，`StoryboardBatchService.seed_drafts()`；既有歧义草稿修复在 `:206-252`。
- AUTO 传入 `auto_approve_ambiguous=True`，为允许的连续观察歧义附加系统审批；其他结构冲突仍暂停 `director_validation_failed`。
- Prompt：**无**。

## Step 6：逐组生成整张多格故事板

### 创建生成意图

- AUTO：`auto_pipeline/service.py:247-283`，对缺失且可生成的组调用批量服务。
- 批量：`application/storyboard_batch.py:254-316`，先应用可应用草稿，再逐组创建 generation。
- 单组手动入口仍保留：`POST /storyboard-groups/{group_id}/grid-sheet-generations`，`api/storyboards.py:261-291`。
- 生成服务：`application/storyboards.py:422-493`，`StoryboardGenerationService.create()`。
- Prompt 构建与持久化顺序：`:438-490`——实测时间区间 → `build_grid_sheet_prompt()` → `apply_prompt_safety()` → canonical 哈希 → `INTENT_PERSISTED`。
- outbox：`adapters/persistence/storyboards/repository.py:1026-1041`，同事务写 `STORYBOARD_GENERATION_REQUESTED`。
- Prompt：**P4**。它不是另一次 LLM 编译，而是代码确定性拼装后交给图像模型。

### 执行、QC 与版本

- `GridSheetGenerationWorkflow`：`workflows/storyboards.py:109-121`；终态 kick 在 `:57-67`。
- 推进：`application/storyboards.py:495-818`，提交、轮询、下载、QC、对象归档、成功切换。
- 状态主链：`INTENT_PERSISTED→SUBMITTING→SUBMITTED/RUNNING→DOWNLOADED→QC_PASSED→OBJECT_STORED→SUCCEEDED`。
- 图片技术 QC：`adapters/storyboard_image_qc.py:9-39`。
- 成功后 current 切换：`adapters/persistence/storyboards/repository.py:1445-1559`；历史版本不覆盖。

### Provider 分支

- 代码默认 `storyboard_provider_kind="contract"`：`config.py:58-64`；生产 runtime 可按配置选择 contract 或 grsai。【运行值未确认】
- grsai 分支：`adapters/grsai_grid_sheet.py:61-129`；参考图上传换 URL，提交 `/v1/draw/completions`，轮询 `/v1/draw/result`。请求中的 `prompt` 是安全替换后的完整 P4，`webHook="-1"`。
- AUTO 的明确拒绝、connect/429、发送后未知、下载/QC/存储异常走不同路径；详见状态文档。

## Step 7：视频提示词编译与 H3 组级视频

### AUTO 批量入口与提示词编译

- AUTO：`auto_pipeline/service.py:285-331,353-382`，续跑或创建 `GroupVideoBatchService` job，再 `process_all()`。
- 批量服务：`application/video_batch.py:167-229`。
- 当前生产 writer：`video_generation/persistence.py:335-385,396-570`，`ManualH3IntentWriter.create()`。
- 编译入口：`video_generation/orchestration.py:2157-2170`，`compile_video_prompt()`；MANUAL `max_repairs=1`，AUTO `max_repairs=2`。
- 编译器：`video_prompt/compiler.py:62-167`；HTTP 适配器 `adapters/deepseek_video_prompt.py:35-126`。
- 代码默认策略 `tubeflow_storyboard_v2`、默认模型 `deepseek-v4-flash`：`config.py:72-76`。【运行值未确认】
- 编译产物包括最终文本、策略、模型、回执、修复标记与输入指纹，写入视频输入快照：`video_generation/orchestration.py:2334-2342`。
- Prompt：**P5**，含 system 策略、user JSON、few-shot 与代码后拼 `visual_asset_bindings` 行。

### H3 提交与归档

1. attempt 初始为 `CREATED`，同事务写 `VIDEO_GENERATION_REQUESTED`：`video_generation/persistence.py:537-569`。
2. H3 outbox 启动 `GroupVideoRecoveryWorkflow`：`video_generation/worker.py:45-117`。
3. workflow/activity：`video_generation/temporal.py:28-78,120-148`，推进 `DurableGroupVideoOrchestrator`。
4. 参考资产先 staging，再 `POST /executions`；Engine HTTP 端点见 `video_generation/engine.py:159-217`。
5. 请求构造：`video_generation/orchestration.py:2214-2242`；实际 prompt 使用编译文本，而非失败时回退旧故事板 Prompt。
6. 下载、SHA-256/大小复核、不可变归档、ffprobe/QC、archive confirm、暂存清理：同文件 `:1344-1586`。
7. 首个成功归档且该组没有 current 时自动设为当前视频：`:1581-1585`；后续候选需显式切换。

### 异常与重试

- H3 自动重提使用已编译 Prompt，不重新调用 DeepSeek。
- AUTO 的明确拒绝、未知态、QC_FAILED、STALLED/GPU 探测分别由 `orchestration.py` 的对应分支处理；candidate/durable 工作流对未知与 404 另有更严格证据门禁。
- 终态失败使 AUTO 暂停 `video_failed:<error_code>`；批量意图创建阶段的错误可暂停为 `video_failed:intent_rejected`。

## Step 8：FFmpeg 渲染合成与 SRT

### 触发与调度

- AUTO：`auto_pipeline/service.py:333-349`；经 `auto_pipeline/factory.py:48-64` 调 `enqueue_composition()`。
- 手动入口：`POST /projects/{project_id}/composition`，`api/media_router.py:195-205`。
- 权威输入 revision 与 outbox：`adapters/persistence/media_repository.py:556-692`。
- relay：`application/composition_outbox.py:64-81`；Temporal dispatcher 在 `workflows/composition.py:124-143`。
- workflow/activity：`workflows/composition.py:34-88`，activity 30 分钟、30 秒心跳、最多 3 次活动尝试。

### 合成实现

- 服务：`application/composition.py:146-239`，`ManualCompositionService` 读取不可变 revision，先生成 SRT，再认领 job、调用 composer、激活候选。
- FFmpeg：`adapters/ffmpeg.py:149-284`，`FFmpegComposer.compose()`。
- 每组视频按精确帧数裁剪并转 24fps；TTS 按实测采样数裁剪；H3 原生环境声 `volume=0.25`，与 TTS 混音后限幅。
- 最终参数：H.264/libx264、yuv420p、AAC、48kHz、双声道、24fps、`+faststart`；关键命令在同文件 `:223-250`。
- 输出校验：`:251-284,316-332`，帧率、采样率、分辨率、帧数/时长不满足即失败并删除候选。
- SRT：`domain/composition.py:199-226`，按保留句子的连续实测时间轴独立生成，不烧入 MP4。
- Prompt：**无**。

### 结果存储与 current

- `activate_candidate()`：`adapters/persistence/media_repository.py:995-1172`。MP4 与 SRT 上传到 MinIO，PostgreSQL 保存媒体资产、manifest、job 和 `composition_versions(current=false)`。
- AUTO 首版成功且仍无 current 时自动选中：`application/composition.py:188-194`。
- 本地候选文件最终在应用层删除：`application/composition.py:195-212`；长期媒体字节以 MinIO 为准。
- 合成临时故障可按 AUTO 预算重排；activity 重试、outbox 派发重试和应用层合成重试是三层不同机制，详见状态文档。

## Step 9：完成判定、状态更新与回调通知

### 完成判定

- `facts.composition_current_exists` 为真且前置门禁已满足时，`AutoPipelineService` 收口非终态合成 outbox 并执行 `run.completed()`：`auto_pipeline/service.py:333-337`。
- AUTO 状态只允许 `RUNNING→PAUSED`、`RUNNING→COMPLETED`、`PAUSED→RUNNING`：`auto_pipeline/domain.py:47-57`。
- `COMPLETED` 只要求当前成片存在，不要求 ZIP 已导出或封面已生成。

### 状态读取

- API：`GET /identity/projects/{project_id}/workflow-summary`，`identity_project/router.py:592-598`。
- 聚合：`identity_project/service.py:645-727`，每次 GET 读取项目、计数、attempt 与 AUTO run 后组装响应。
- **【未找到相关实现】**独立的 `workflow_summary` 状态表或向该实体推送的 writer；权威状态先写各业务表，summary 是请求时投影。
- 前端 AUTO 轮询：`app.js:5050-5098`，2.5/5/10 秒退避、单在途与过期响应保护；项目列表每 5 秒轮询见 `app.js:910-940`。

### 内部回调与外部通知

- 内部 `auto_kick` 是进程内函数：`workflows/worker.py:224-230` → `AutoPipelineService.kick()`（`auto_pipeline/service.py:123-128`）。
- 配音、导演/故事板、视频、合成终态均有对应 kick；`kick` 异常仅记录日志，默认 20 秒 sweeper 只扫描 `RUNNING` 运行。
- **【未找到相关实现】**对外业务 webhook、SSE、WebSocket、邮件或第三方 IM 完成通知。grsai 的 `webHook:"-1"` 是关闭其回调的参数，不是本项目向外通知。
- Prompt：**无**。

## Step 10：手动导出 ZIP

- API：`GET /projects/{project_id}/export`，`api/media_router.py:219-229`。
- 数据读取与复核：`api/media_runtime.py:197-351`，读取当前 MP4/SRT，校验哈希；存在当前封面时一并加入。
- ZIP：`application/export.py:12-64`，`StreamingExport` 生成临时 `download.zip`，条目为 `final.mp4`、`subtitles.srt` 和可选 `cover.png|cover.jpg|cover.webp`。
- 响应结束、异常或中断后清理；**【未找到相关实现】**把 ZIP 持久化到 MinIO、建立 export outbox 或 AUTO 完成自动导出的代码。
- Prompt：**无**。

## Step 11：独立手动封面链，不属于 AUTO 主链

- 封面动作在冻结后开放，但不参与 AUTO 完成判定：`identity_project/service.py:287-317`；AUTO 阶段枚举也没有 cover（`auto_pipeline/domain.py:17-26`）。
- 参考图/风格解析：`application/cover_style.py`，消息构造 `domain/cover.py:467-505`。
- 封面文字与画面 Prompt：`application/cover_prompts.py`；同一画面模板以 0.8/1.0 两温度各生成一版，每版最多一次定向整改，两版有效时才调用评委。
- 角色卡解析：`application/cover_characters.py`；消息构造 `domain/cover.py:1208-1263`。
- 创建出图意图时读取当前 `prompt_current`，校验后冻结到 `cover_image_generations.prompt`；出图活动发送该冻结文本，不把参考图直接作为垫图：`application/cover_images.py:101-163`；图像请求适配器 `adapters/cover_image.py:48-60`。
- Prompt：**P6–P11**，其中 P11 是确定性拼装后发给图像模型的最终封面文本，不是新增 LLM 环节。

## AUTO 与保留 MANUAL 分支

| 环节 | 当前新建 AUTO | 保留的 MANUAL 代码路径 |
| --- | --- | --- |
| 新建入口 | 固定 AUTO | 当前 POST 无 MANUAL 选择入口 |
| 提交 | 风险句拆句 + 创建 AUTO 运行 | 不执行 AUTO 拆句分支，不创建 AUTO 运行 |
| 配音/导演 | 会执行，且可并行 | 也会执行对应工作流 |
| 后续阶段 | kick + sweeper 自动接力 | 由阶段级用户动作推进 |
| 自动重试 | 按 AUTO 分支与预算轮次 | 使用人工恢复/手动重试合同 |
| 首版合成 current | 无 current 时自动选中 | 无同样的自动选中分支 |
| 封面/导出 | 均需手动 | 均需手动 |

> 旧 `DirectorGenerationService` 的 1–5 镜 Prompt 和 workflow 仍由生产 Worker 保留用于旧 outbox 排水，但当前 AUTO 创建链使用 `ProjectDirectorService`，不是该服务；详见 Prompt 文档附录 L1。
