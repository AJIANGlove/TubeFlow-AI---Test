# 测试过程问题记录

状态：部分已修复（各条目保留处理状态与验证证据）
建立日期：2026-09-04

本文件记录测试过程中发现的产品与工程问题。每条包含现象、证据与处理方向；处理完成后保留修复状态与验证证据，并同步相关规划文档。

## UI-01：配音失败缺少明确的"需人工处理"提示

- 现象：配音任务以 PARTIAL_FAILED 结束后，第 06 步页面只显示笼统"失败"，没有明确指出哪些句子失败、失败码以及需要人工处置的引导。
- 证据：2026-09-04 新项目 d80b2be7 配音 307/309 成功、句 38 与 292 以 `processing_failed`（Provider 2xx 但本地输出质检拒绝）失败；页面仅显示"失败 / The attempt failed"。
- 建议方向：句子级失败状态进入公开合同（序号 + 稳定失败码 + 建议动作），页面给出明确的"需人工处理"提示。
- 状态：2026-09-08 已修复。`narration-sentence.v1` 提供全部非分隔句的公开状态、稳定失败码和允许动作；第 06 步列出需人工处理的句序、原因与建议。证据：`test_invalid_output_api_exposes_sentences_and_recovers_only_authorized_units`、`test_sentence_public_contract_only_offers_safe_actions`；`frontend/narration-view.test.js` 9 项通过；1440×1000 Chrome 模拟后端验收通过（`tmp/wp1-ui-before.png`、`tmp/wp1-ui-after.png`），未调用真实 AI。

## UI-02：失败配音句无法筛选查看，且没有手动/一键重试入口

- 现象：用户无法在页面筛选失败句子，也没有任何重试按钮；失败句实际无法通过任何界面动作恢复。
- 证据：同上两句失败后页面无处置入口。后端核查：`/narration/recover` 人工处置对 `OUTPUT_INVALID` 只允许 `CONFIRMED_FAILED`（终态，不可重试），重试类处置只对 `SAFE_TO_RETRY`/`REMOTE_STATE_UNKNOWN` 开放，因此 `OUTPUT_INVALID` 句子在产品上永远无法恢复，项目会被配音门禁永久卡住。
- 建议方向：为 `OUTPUT_INVALID` 增加明确的人工"确认重试"处置（新增 SAFE_TO_RETRY 迁移），页面提供失败句筛选与一键重试。
- 进展：2026-09-04 已补后端最小处置 `RETRY_INVALID_OUTPUT`（reconcile → SAFE_TO_RETRY → 自动续跑），并用它完成句 38、292 的真实重试；页面侧筛选与按钮仍待排期。
- 状态：2026-09-08 已完成页面失败筛选、逐句及全部失败句的确认重试。逐句 POST 使用原失败尝试键，只接受该句授权；202 如实显示已受理，批量结果分别报告。补齐持久队列、幂等重放、错误状态/越权拒绝，以及重试响应丢失和跨项目切换的 GET 轮询保护。证据：`test_multiple_recoveries_preserve_active_job_and_replay_is_idempotent`、`test_invalid_output_api_rejects_completed_unknown_and_unowned_attempts`；`test_narration_recovery_queue.py` 隔离 PostgreSQL 3 项全部通过（`tmp/be01/wp1-recovery.log`）；前端 9 项全部通过（`tmp/wp1/frontend-recovery-regression.log`），浏览器模拟两句均恢复成功且提示消失。

## BE-01：集成测试夹具与 WIP 漂移（历史记录 21 项，本次复现 22 项）

- 现象：`backend/tests/integration` 下 21 个用例失败，根因是未提交 WIP 改变了产品行为而测试未同步：`projects.title` 非空约束后多个夹具仍插空标题、项目删除需标题确认后旧用例仍按 project_id 确认、迁移 0018 刻意不可逆后 downgrade 类用例仍尝试跨过它。
- 证据：2026-09-03/04 两次全量集成运行均稳定复现（与 H3 停滞修复无关）。
- 建议方向：统一更新夹具与删除确认语义，明确 downgrade 类用例的边界（0018 之后不可逆）。
- 状态：2026-09-08 已修复。隔离环境校准后基线为 22 failed / 61 passed / 0 skipped（`tmp/be01/baseline-integration-configured.log`）。补齐项目标题、按真实标题确认删除；迁移用例从实际历史版本建夹具并局部往返，避免跨越 0013/0014/0018 的不可逆点，完整最新 Schema 比对仍保留独立用例。另按已确认合同修正历史视频可访问断言、历史 Channel Schema、导演镜头字段与视频提示词编译器替身，未弱化产品约束。
- 修复证据：`test_fresh_schema_matches_canonical_metadata`、`test_0006_round_trip_then_current_head_preserves_cleanup_metadata`、`test_0007_round_trip_discards_nullable_staged_purge_manifest`、`test_empty_postgres_authority_round_trip_after_migration` 等；全量 `pytest tests/integration -q` 为 99 passed / 0 failed / 0 skipped，真实 PostgreSQL + MinIO + Temporal + FFmpeg（`tmp/be01/integration-final.log` / `.xml`）。专用隔离库 `tf_be01_test` 与独立 MinIO，不连接实际项目库。
- 联合回归：后端全量单测 1014 passed / 0 skipped（Windows + 本机已有 FFmpeg 9.0.1；`tmp/final-unit.log` / `.xml`），`ruff check src tests migrations`、`mypy src`（128 个源文件）、`npm run check`（86 项前端测试、语法检查与构建）均通过。早期使用 2019 年 FFmpeg 的 3 项失败已用原测试在现代版本验证消除（`tmp/wp1/ffmpeg-old-diagnostics.log`、`tmp/wp1/ffmpeg-modern-windows.log`）。

## UI-03：用户要求废除远端未知态的"人工证据处置"步骤

- 现象：前置导演运行因 `ReadTimeout` 落入远端未知态后，页面强制要求先点【人工证据处置】才能续跑；用户认为该步骤多余，要求废除。
- 证据：2026-09-04 新项目 d80b2be7 导演续跑运行 REMOTE_STATE_UNKNOWN（67 单元，12 成功），页面仅提供"人工证据处置 / REFRESH"。
- 注意：该步骤是已确认决策（`USER_DECISIONS.md` §10.6、验收标准 §11"未知远端状态只能人工证据处置，不得自动重提"）。废除属用户已明确表达的新决策意向；实施时必须同步修订两份规划文档与相关合同/测试，并保留防重复提交的替代约束（如处置前置条件的内部校验），不能只删步骤。
- 状态：2026-09-08 已完成，仅项目级前置导演免除强制证据。未知态页面直接提供“重新处理”；项目锁、实际在途检查、同键幂等、输入变化确认及 `manual_evidence_waived` 事务审计已实现（迁移 0023）。同输入恢复复用全部合同完整的成功单元，保留回执与血缘；分组导演、故事板、H3 保留原证据规则。未解决的非最新旧 UNKNOWN 历史继续明确拒绝，不自动改写历史运行。
- 修复证据：`test_project_director.py`、`test_project_director_api.py`、`test_workflow_summary.py` 等相关单测 207 项通过；`test_project_director_manual_recovery_postgres.py` 的真实 SQL/API 11 项全部通过（`tmp/be01/wp2-manual-recovery.log`），覆盖成功单元零 Provider 重提、审计、输入确认、并发幂等与出站写入失败回滚；`frontend/project-director.test.js` 21 项通过。两份规划及公开动作同步修订。

## BE-02：H3 远端卡死导致单机挤压（已修复，仅留档）
- 现象：2026-09-01 批量 74 组视频全部被挤压到单台引擎串行约 4.5 小时；根因为 4 个远端执行在引擎侧 ACCEPTED 停滞 17.5 小时，槽位被无限轮询续租永久占死。
- 处理：2026-09-03 已实施并部署停滞隔离（STALLED 不计容量 + 降频轮询 + 手动取消 + QC_FAILED 终态化 + 平局打散），决策见 `USER_DECISIONS.md` §7.21–25。

## BE-03：Director Provider 在 JSON 前输出 `<think>` 思维链导致解析失败（根因已定位）

- 现象：前置导演全局规划单元连续三轮运行以 `director_response_content_json_invalid` 失败（Provider HTTP 200、finish_reason=stop、JSONDecodeError，parse_stage=content_json）。
- 证据：2026-09-04 诊断重放确认响应 content 以 `<think>` 思维链开头、JSON 本体完整位于其后（grsai 的 gpt-5.6-sol 未按 strict json_schema 约束 content）。失败诊断见 unit.response_diagnostics_json。
- 处理：2026-09-04 解析器剥离 `<think>…</think>` 包装后再做 JSON 解析（其余校验与失败分类不变），已部署并续跑通过。
- 后续观察：同类 Provider 行为（推理型模型输出思维链）若在别处复现，按同一模式处理。

## BE-04：故事板 Provider `error` 失败改为有界自动重试（2026-10-14 已扩展为双渠道故障转移）

- 现象：批量生成故事板时部分组以 provider 阶段 generic `error` 失败（非输入/输出违规拦截），原规则只能落终态 FAILED 后由用户手动重试。
- 证据：2026-09-04 项目 d80b2be7 首轮 91 组中 6 组 `failure_stage=provider, failure_code=error`；2026-10-14 韩国项目另实测 `get image url failed` 与 `generate image failed`。
- 最终决策：按 `USER_DECISIONS.md` §25 执行 GRSAI 四模型 + Pix 三模型顺序故障转移（`gpt-image-2-vip` 不上线），生产每组每轮 14 次、每模型 2 次；`input_moderation`/`output_moderation` 等审核与明确参数错误不转移，`REMOTE_STATE_UNKNOWN` 不得自动替代提交。
- 实施记录：2026-09-04 的 6 组已按当时手动指令重试；当前代码、迁移、自动化验证、当前启用七模型验收与韩国项目恢复进度见 `runtime/operations/storyboard_provider_failover_20261014.md`，未完成项不得按本标题提前宣称通过。

## BE-04：合成活动缺少心跳导致大项目必被取消（根因已定位）

- 现象：91 组项目点击「开始合成」后任务以 `composition_failed` 失败；UI 显示处理中后失败。
- 证据：`manual-composition-v1` 的 compose 活动 `heartbeat_timeout=30s`，但 `compose_revision` 全链路（`application/composition.py`、`adapters/ffmpeg.py`）没有任何心跳调用——91 个视频的探测+合并+转码远超 30 秒，Temporal 必然中途取消活动（worker 日志见 `temporalio.exceptions.CancelledError` 于 ffmpeg.probe）；活动重试时又因首轮遗留的在途状态被 `composition_in_progress` 以 non_retryable 终结，任务直接 FAILED。
- 建议方向：合成活动改为在工作线程中执行并周期性心跳（或拉长心跳超时并与租约对齐）；重试路径需先清理首轮在途状态。修复后需重新部署并重试合成。

## BE-05：内存与 SQL 节点认领结果不一致

- 现象：完全平局时内存仓储取最大节点 ID，SQL 取最小；另发现内存停滞槽位的容量计算与 SQL 不一致。
- 状态（2026-09-08）：已修复，用户已明确授权将额外 STALLED 容量公式对齐及相关测试、文档纳入工作包 4 并提交。内存仓储的容量公式与最终节点 ID 平局规则对齐现有 SQL；生产 SQL 调度实现不变。两份规划已同步，临时审批阻塞已解除。
- 验证证据：本次重新运行 `backend/tests/unit/test_video_stall.py` 为 22 passed / 0 failed / 0 skipped（`tmp/wp4-final.log` / `.xml`）；完整 Ruff 与 mypy（128 个源文件）通过。此前相邻视频/H3 测试 104 项、联合单测 1014 项及隔离集成 99 项已通过（`tmp/final-unit.log`、`tmp/be01/integration-final.log`）。`test_node_claim_tiebreak_and_rotation_match_sql` 与 `test_stalled_capacity_boundaries_match_sql` 对照两仓储验证完全平局、九次分布、停滞正/零/负容量和预绑定认领，未调用真实视频引擎。
