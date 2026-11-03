# 02 链路总览：流程图与分步输入/输出/状态/重试

> 与 `01-flow.md` 环节编号一致；状态码与重试预算的完整定义见 `03-states-and-errors.md`。

## 1. 总体流程图（Mermaid）

```mermaid
flowchart TD
    subgraph 用户与前端
        U0[POST /identity/projects<br/>创建草稿] --> U1[PATCH /identity/projects/id<br/>防抖自动保存]
        U1 --> U2[POST /narration/confirm<br/>一键提交/确认配音]
        U13[GET /projects/id/export<br/>下载 ZIP]
    end

    subgraph API 进程 FastAPI
        C0[IdentityProjectService.create_project]
        C1[ProjectNarrationService.confirm_and_start]
        C2[SentenceSplitService.plan_splits<br/>超长句拆句 agent]
        C3[confirm_and_enqueue 单事务<br/>冻结快照+配音 job+AUTO run+导演 run]
        C1 --> C2 --> C3
    end

    subgraph 数据库与 outbox
        DB[(PostgreSQL 权威状态)]
        OB1[narration job PENDING]
        OB2[storyboard outbox<br/>PROJECT_DIRECTOR_RUN_REQUESTED]
        OB3[storyboard outbox<br/>STORYBOARD_GENERATION_REQUESTED]
        OB4[video_generation_outbox]
        OB5[composition outbox]
        C3 --> OB1
        C3 --> OB2
    end

    subgraph Worker 进程
        W1[NarrationWorkflow<br/>tubeflow-narration]
        W2[ProjectDirectorRunWorkflow<br/>storyboards 队列]
        W3[GridSheetGenerationWorkflow<br/>storyboards 队列]
        W4[GroupVideoRecoveryWorkflow<br/>tubeflow-h3-video]
        W5[manual-composition-v1<br/>tubeflow-project-purge]
        SW[auto-pipeline-sweeper 20s 巡检]
    end

    subgraph 外部服务
        P1[CosyVoice /batch_generate<br/>或 ElevenLabs TTS]
        P2[Director LLM grsai chat/completions]
        P3[DeepSeek 视频提示词编译]
        P4[grsai gpt-image-2 /v1/draw/completions]
        P5[H3 节点 /executions REF2VA]
    end

    OB1 --> W1 --> P1
    P1 --> R1[QC+归一化+归档<br/>实测时长]
    R1 --> R2[build_measured_timeline<br/>group_timeline 15s 装组]
    OB2 --> W2 --> P2
    P2 --> R3[前置导演修订]
    R2 --> R4[materialize 确定性重映射<br/>controlled-semantic-lineage-remap-v4<br/>+ adjacent-merge-v1 收敛]
    R3 --> R4
    R4 --> R5[seed_drafts 播种组级草稿<br/>歧义对系统审批]
    R5 --> OB3 --> W3 --> P4
    P4 --> R6[下载+图片 QC+对象存储<br/>SUCCEEDED 切当前]
    R6 --> R7[video_batch 逐组意图<br/>同步编译视频提示词] --> P3
    P3 --> OB4 --> W4 --> P5
    P5 --> R8[下载+视频 QC+归档<br/>首版自动当前]
    R8 --> OB5 --> W5 --> R9[FFmpeg 合成<br/>成片+SRT]
    R9 --> DONE[AUTO run COMPLETED<br/>首版成片自动当前]
    SW -.兜底推进.-> DB

    U13 --> X1[StreamingExport 临时 ZIP<br/>final.mp4+subtitles.srt+封面]

    subgraph 支线：封面（手动，不入 AUTO 链路）
        V1[参考图上传/频道风格] --> V2[视觉解析 gpt-5.5]
        V2 --> V3[封面文字+两版提示词+评委]
        V3 --> V4[lingkeai images/generations 出图]
    end
```

## 2. 文本时序列表

```
用户                API 进程                       Worker 进程                    外部 Provider
 | POST /projects      |                                |                             |
 |-------------------->| create_project（草稿）          |                             |
 | POST /narration/confirm                            |                             |
 |-------------------->| confirm_and_start              |                             |
 |                     | ├─ SentenceSplitService ------|--------------------------->| DeepSeek 拆句
 |                     | └─ confirm_and_enqueue（单事务：快照/job/AUTO run/导演 run/outbox）
 | 202 QUEUED          |                                |                             |
 |                     |  narration relay 2s ---------->| NarrationWorkflow           |
 |                     |                                | synthesize_batch ---------->| CosyVoice / EL
 |                     |                                | finish_job → 时间轴/分组     |
 |                     |  storyboard relay ------------>| ProjectDirectorRunWorkflow  |
 |                     |                                | advance（并发 4 单元） ----->| Director LLM
 |                     |                                | SUCCEEDED → materialize     |
 |                     |  AUTO 编排器 kick/sweep        | seed_drafts（确定性）        |
 |                     |  storyboard relay ------------>| GridSheetGenerationWorkflow |
 |                     |                                | submit/poll/download/QC --->| grsai gpt-image-2
 |                     |  video_batch.process_all       | 编译提示词 ---------------->| DeepSeek
 |                     |  h3 relay -------------------->| GroupVideoRecoveryWorkflow  |
 |                     |                                | stage/submit/inspect ------>| H3 节点
 |                     |                                | SUCCEEDED → archive         |
 |                     |  composition relay ----------->| manual-composition-v1       |
 |                     |                                | FFmpeg 合成                  |
 | GET 轮询 workflow-summary / 各状态端点（5s/2.5s 退避）                             |
 | GET /export -------->| StreamingExport ZIP           |                             |
```

## 3. 分步输入 / 输出 / 状态流转 / 失败处理表

| # | 环节 | 输入 | 输出 | 状态流转（权威存储） | 失败重试与异常处理 |
| --- | --- | --- | --- | --- | --- |
| 0 | 创建草稿 | 频道、文案、角色图、风格、语言、音色、画幅 | 项目草稿行 | draft（无快照） | 校验失败 422/409（`channel_default_voice_required` 等），无重试 |
| 1 | 确认配音/提交 | 草稿 + 预期 revision | 冻结快照、配音 job、AUTO run、导演 run、outbox 事件 | job PENDING；run RUNNING；导演 run INTENT_PERSISTED | 单事务，失败整体回滚；重入幂等返回既有 job |
| 2 | 拆句 agent | 风险句文本、语言 | SentenceSplitPlan | （不落独立状态；计划随记录持久化） | 每句 3 次、退避 5/15/30s；失败 → 整个 confirm 失败 |
| 4 | 逐句配音 | 冻结句、音色参考 | 原始+归一化 WAV、QC、实测时长 | 句 attempt：INTENT_RECORDED→SUBMISSION_STARTED→COMPLETED；异常：SAFE_TO_RETRY / PROVIDER_REJECTED / OUTPUT_INVALID / ARCHIVE_FAILED / REMOTE_STATE_UNKNOWN | AUTO 每句 3 次预算退避重投（`_schedule_auto_retries`）；MANUAL 仅 OUTPUT_INVALID 可手动重试 |
| 5 | 时间轴/分组 | 全部归档句 | 时间轴 + 分组（≤15s/组） | job COMPLETED / FAILED / PARTIAL_FAILED | 分组为纯确定性计算；单句>15s 标记 video_blocked（MANUAL 跳过该组 / AUTO 整项目暂停） |
| 6 | 前置导演 | 冻结句快照、风格、角色引用 | 全局大纲 + 单元镜头修订 | run：INTENT_PERSISTED→RUNNING→SUCCEEDED/FAILED/REMOTE_STATE_UNKNOWN；unit：INTENT_PERSISTED→LEASED→SUBMITTING→SUCCEEDED | 每单元 3 次、5/15s 退避；AUTO 未知态自动重提（耗尽 `remote_state_unknown_exhausted`）；MANUAL 未知态需人工/免证据恢复入口 |
| 7 | 重映射物化 | 前置修订 + 权威分组 | 组级候选（含血缘） | materialization：SUCCEEDED / REVIEW_REQUIRED | 纯本地；canonical 幂等；冲突 → REVIEW_REQUIRED 等人工/系统审批 |
| 8 | 导演草稿播种 | 组级候选 | 组级导演草稿修订 | revision：DRAFT（review_state）→ APPLIED | 校验不过 → AUTO 暂停 `director_validation_failed`；保存草稿可按原因码自动续跑 |
| 9 | 故事板生成 | 应用修订、角色图、风格 | 多格故事板整图 | INTENT_PERSISTED→SUBMITTING→SUBMITTED/RUNNING→DOWNLOADED→QC_PASSED→OBJECT_STORED→SUCCEEDED；FAILED / REMOTE_STATE_UNKNOWN | connect 未送达与 429：AUTO 预算内自动重提；4xx 拒绝：直接 FAILED；未知态：对账（grsai 不支持按键查询，恒返 None）→ AUTO 自动重提（3 次封顶） |
| 10 | 视频编译+H3 | 当前故事板、组实测时长、角色图 | 组视频 MP4 | CREATED→STAGING→SUBMITTING→SUBMITTED/RUNNING→SUCCEEDED→ARCHIVED；FAILED / CANCELLED / REMOTE_STATE_UNKNOWN / STALLED 标记 | 编译：修复重试+3 次预算；H3：节点槽位调度、停滞降频轮询、AUTO GPU 探测自动取消重提（3 次封顶）；QC_FAILED 为待决策终态 |
| 11 | 合成 | 各组当前视频+逐句音频 | 成片 MP4 + SRT | outbox PENDING→DISPATCHED→（SUPERSEDED 收口）；attempt FAILED/SUCCEEDED；版本 current 指针 | 临时错误（media_command_*）AUTO 3 次退避；失联工作流对账 `composition_workflow_lost` 后 AUTO 重排 |
| 12 | 导出 | 当前成片、SRT、当前封面 | 流式 ZIP | 无持久状态 | 临时目录即用即清；孤儿清理兜底 |
| 13 | 封面（手动支线） | 参考图/频道风格、标题、冻结文案 | 封面提示词版本、封面图版本 | 各生成：INTENT_PERSISTED→SUCCEEDED/FAILED | 文字失败回退标题；单版失败跳过；全失败 FAILED；评委失败本地择优 |
| 14 | 状态通知 | — | public_status / workflow_summary | — | 页面 GET 轮询（列表 5s；运行页 2.5/5/10s 退避）；无出站回调 |

## 4. 关键并发与幂等不变式（代码事实）

- 配音：句级幂等键由（项目+句子文本哈希+音色身份）派生（`domain/narration.py:405` `narration_idempotency_key`，调用点 `application/narration.py:869`）；Temporal 工作流 id = job_id，`REJECT_DUPLICATE`（`workflows/narration.py:148-154`）。
- 导演：同项目同时最多一个未终结 run（`create_run` 阻断检查，`application/project_director.py:613-620`）；单元 20 分钟租约（`PROJECT_DIRECTOR_UNIT_LEASE`，:78）。
- 故事板：同输入未解决的生成拒绝二次提交（`remote_state_unresolved`，`application/storyboards.py:461-465`）；grsai 提交并发上限 20（`adapters/grsai_grid_sheet.py:54`）。
- 视频：每组同时最多一个在途尝试（`find_unresolved_group` → `remote_acceptance_unknown`，`video_generation/orchestration.py:487-489`）；节点槽位租约+横扫（`sweep_expired_slots`）。
- 合成：幂等键 = `compose:{owner}:{project}:{inputs_hash}` + AUTO 预算轮次命名空间（`application/composition.py:157-160`）。
- AUTO 编排：`advance` 持项目锁、每次最多一个外部动作（`auto_pipeline/service.py:84-102` 与文件头注释）。
