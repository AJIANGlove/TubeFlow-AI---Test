# ElevenLabs 付费配音接入 —— 改动汇总（WP4–WP7）

> 生成时间：2026-10-13 16:36（Asia/Shanghai）。仓库：`D:\AAA-project\TubeFlow AI - Test`。
> 依据：`docs/planning/USER_DECISIONS.md` §23（2026-10-13 EL 节）、`docs/planning/FINAL_ACCEPTANCE_CRITERIA.md` §24、实施计划 `longshot-us-agent-doctor-strange.md`（WP0–WP7）、授权记录 `runtime/operations/elevenlabs_dubbing_integration_20261013.md`。
> 本文只记录**已核实事实**；未验证项在 §5 与 §7 明确标注，未做推断。

## 1. 范围与分工

| 工作包 | 内容 | 由谁完成 | 状态 |
| --- | --- | --- | --- |
| WP0 | 会话现状核对、测试基线 | 本轮 | 完成 |
| WP1 | 配置项 + `mihomo` 代理容器 + `.env.example` + DEPLOYMENT 台账 | **上一会话** | 完成 |
| WP2 | 号池（PostgreSQL + 管理页 + API） | **上一会话** | 完成 |
| WP3 | EL HTTP 适配器（TTS/克隆/音色/配额） | **上一会话**，本轮补 `proxy` 说明与 `list_account_voices` | 完成 |
| WP4 | 克隆音色实体 + 频道/项目/快照 + API + 删除保护与自动清理 | 本轮 | 完成 |
| WP5 | 配音链路接入（provider 路由 + EL runner + 配额耗尽暂停/恢复） | 本轮 | 完成 |
| WP6 | 前端（频道管理面板、创建向导、配音设置阶段） | 本轮 | 完成 |
| WP7 | 文档同步（USER_DECISIONS §23.1 实施说明、验收 §24）与全量检查 | 本轮 | 完成 |

上一会话（kimi-code `session_5d4b9056`）的 WP1–WP3 产物**当时未提交**；本轮在其工作区之上继续，并把注释中的 `§22` 引用改为 `§23`。

## 2. 改动文件清单（sha256 为 2026-10-13 16:36 快照）

### 2.1 本次新建（11，另加本文档共 12 项入提交）

| 文件 | 行数 | sha256(前16) |
| --- | ---: | --- |
| `backend/migrations/versions/0035_cloned_voices_and_tts_provider.py` | 146 | `80a3cec5fea4f3d5` |
| `backend/src/tubeflow_test/adapters/audio_transcode.py` | 61 | `5fd04cc26b0092e8` |
| `backend/src/tubeflow_test/application/elevenlabs_narration.py` | 304 | `8dd2e5288bb918ef` |
| `backend/src/tubeflow_test/identity_project/cloned_voices.py` | 168 | `19f3fdd0a6cf2401` |
| `backend/src/tubeflow_test/identity_project/cloned_voice_service.py` | 261 | `9711b56730651a38` |
| `backend/tests/integration/test_cloned_voices_postgres.py` | 252 | `42e1668bf2b27456` |
| `backend/tests/unit/test_cloned_voice_service.py` | 578 | `e74511b527c51f29` |
| `backend/tests/unit/test_cloned_voices_domain.py` | 171 | `5075c1bbeda4f484` |
| `backend/tests/unit/test_elevenlabs_narration.py` | 548 | `781a3c8393062ca1` |
| `frontend/el-voices.js` | 133 | `a2f75868aed59567` |
| `frontend/el-voices.test.js` | 127 | `e76f47b2c0cdad29` |

### 2.2 本次修改（表 26 行：入提交 24、混写未提交 2）

判定依据：文件最后修改时间 ≥ 2026-10-13 15:00（本次会话从 14:40 前后开始，此前的改动属上一会话 / 其他任务）。

| 文件 | 最后修改 | 行数 | sha256(前16) |
| --- | --- | ---: | --- |
| `backend/src/tubeflow_test/config.py` | 15:30/16:14 ※ | 321 | `9cff83787e3f552d` |
| `.env.example` | 15:30/16:14 ※ | 81 | `af8a0ce8fd828184` |
| `backend/src/tubeflow_test/application/narration_ports.py` | 15:52 | 271 | `67f7a6ad963810b0` |
| `backend/src/tubeflow_test/adapters/persistence/narration.py` | 15:52 | 1975 | `e0b844ab98d24213` |
| `backend/tests/unit/test_narration.py` | 15:51 | 1992 | `733a3f24fa1f4b1f` |
| `backend/src/tubeflow_test/application/narration.py` | 15:50 | 1548 | `19a25b043299c046` |
| `app.js` | 15:48 | 6846 | `65dd2b5c1c89be16` |
| `backend/src/tubeflow_test/domain/narration.py` | 15:45 | 567 | `c9c65f60dfc0c27b` |
| `docs/planning/USER_DECISIONS.md` | 15:30 | 335 | `a0e292936f888f17` |
| `docs/planning/FINAL_ACCEPTANCE_CRITERIA.md` | 15:30 | 408 | `4ceb820cb0cba2d7` |
| `backend/tests/unit/test_narration_voice_reference.py` | 15:30 | 226 | `99370b8669f21558` |
| `backend/src/tubeflow_test/identity_project/service.py` | 15:30 | 1227 | `8a04a910e67e24da` |
| `backend/src/tubeflow_test/identity_project/router.py` | 15:30 | 838 | `6b4b694b7e8128a5` |
| `backend/src/tubeflow_test/identity_project/domain.py` | 15:30 | 326 | `b3da05c302374753` |
| `backend/src/tubeflow_test/domain/errors.py` | 15:30 | 33 | `85cd5a7b09b9d18a` |
| `backend/src/tubeflow_test/api/narration_runtime.py` | 15:30 | 658 | `3c54f9b7eb0e51bd` |
| `backend/src/tubeflow_test/adapters/persistence/identity_project/repository.py` | 15:30 | 1400 | `66e7ab0f9f4055ed` |
| `backend/src/tubeflow_test/adapters/persistence/identity_project/models.py` | 15:30 | 400 | `bb788950bd0f72d8` |
| `scripts/build.mjs` | 15:22 | 1 | `d1a2581d89d18b3f` |
| `package.json` | 15:22 | 15 | `1bd8014d4c1a41a2` |
| `frontend/api-client.js` | 15:20 | 21 | `29535902620253f0` |
| `backend/tests/unit/test_production_composition_authority_sql.py` | 15:13 | 697 | `c9d1ff7f6c484e8c` |
| `backend/tests/unit/test_production_active_job_gates_sql.py` | 15:13 | 390 | `15868a81a7502972` |
| `backend/tests/unit/test_narration_auto_retry.py` | 15:06 | 599 | `3e2a9a32aea43fb2` |
| `backend/tests/unit/test_auto_pipeline.py` | 15:06 | 1596 | `10f67f26f7f6b0c1` |
| `backend/tests/unit/test_workflow_summary.py` | 15:04 | 1467 | `65c6cd9c15ea8d4b` |

> ※ `config.py` 与 `.env.example` 在 16:14 被**同仓并行的另一任务**（导演超时预算修复）再次改动（该任务同时新建了 `backend/tests/unit/test_director_timeout_budget.py` 等文件）。因此这两个文件当前内容**混有两轮改动**：EL 相关部分（`elevenlabs_proxy` 只接受 `http://`）属本轮，其余属那一任务。

### 2.3 更早任务写入、本轮续改的测试文件（D 类，4）

这些文件在本次会话之前（或由更早任务）写入工作区，本轮为适配 EL 契约在其上继续改动；全部随本次提交：

| 文件 | 父提交是否存在 | 本轮我的改动 |
| --- | --- | --- |
| `backend/tests/unit/test_auto_pipeline.py` | **否**（整文件 1596 行随本次提交新增） | `_NarrationVoices.get_reference` 签名、`VOICE` 常量 |
| `backend/tests/unit/test_narration_auto_retry.py` | **否**（整文件 599 行随本次提交新增） | `_VoicePort.get_reference` 签名 |
| `backend/tests/unit/test_workflow_summary.py` | 是 | `Project`/`ProjectSnapshot` 关键字参数构造 |
| `backend/tests/unit/test_narration.py` | 是 | 3 个 EL/批量回归测试 + `_VoicePort` 签名 |

### 2.4 上一会话改动、本轮未续改的文件（6：已提交 4 + 未提交 2）

其中 4 个随本次提交，2 个为部署接线、由本会话另做补提交（见 §7）：

| 文件 | 最后修改 | 入提交 | 说明 |
| --- | --- | --- | --- |
| `backend/src/tubeflow_test/identity_project/ports.py` | 14:59 | 是 | 上一会话基线（本轮新增的端口方法见 §3.3 说明） |
| `backend/src/tubeflow_test/identity_project/adapters.py` | 14:59 | 是 | 同上 |
| `backend/src/tubeflow_test/api/app.py` | 14:10 | 是 | 注册号池路由与 `api_roots` |
| `backend/src/tubeflow_test/adapters/persistence/schema.py` | 13:39 | 是 | 元数据含新表 |
| `docker-compose.yml` | 14:03 | 否 | `mihomo` 服务与 EL 环境变量注入（组 1 补提交） |
| `dev-server.mjs` | 13:50 | 否 | `apiPrefixes` 加 `/elevenlabs-pool`（组 1 补提交） |

（其余未跟踪文件与"无 EL 标记"的已修改文件属其它任务，未列入本文；见 §7。）

### 2.5 上一会话新建（C 类，18；全部随本次提交）

其中**表内 5 项**：

| 文件 | 行数 | sha256(前16) |
| --- | ---: | --- |
| `backend/migrations/versions/0034_elevenlabs_key_pool.py` | 53 | `ada36ee6651a361b` |
| `backend/src/tubeflow_test/adapters/persistence/elevenlabs.py` | 278 | `385149a7bfbe981a` |
| `backend/src/tubeflow_test/api/elevenlabs_pool.py` | 113 | `51c11a2b0dd53799` |
| `backend/tests/unit/test_elevenlabs_pool_domain.py` | 82 | `cc9181a68dfa0edf` |
| `frontend/el-pool.test.js` | 55 | `56354f1b3ef5a7ee` |

其余 **13 项**（正文列举）：

`backend/src/tubeflow_test/adapters/elevenlabs.py`、`backend/src/tubeflow_test/elevenlabs/{__init__,domain,ports,service}.py`、`backend/tests/integration/test_elevenlabs_pool_postgres.py`、`backend/tests/unit/test_elevenlabs_{adapter,config,pool_api,pool_service}.py`、`docker/mihomo/entrypoint.sh`、`frontend/el-pool.html`、`frontend/el-pool.js`

## 3. 分层改动明细

> 路径约定：`xxx/yyy.py` 形式的简写均相对 `backend/src/tubeflow_test/`；测试文件相对仓库根。
> WP1–WP3（号池、EL HTTP 适配器、mihomo 容器、号池页）由上一会话完成，本节只在其接口被本轮改动处标注。

### 3.1 数据库迁移 `0035`（唯一 schema 变更）

- 新表 `cloned_voices`（`channel_id` FK RESTRICT、`owner_id` FK、源音频五列、`uq_cloned_voices_channel_name`）。
- 新表 `cloned_voice_accounts`（复合主键 `(cloned_voice_id, pool_key_id)`，分别 FK CASCADE / RESTRICT）。
- `channels`：`default_tts_provider TEXT NOT NULL DEFAULT 'cosyvoice'`、`default_cloned_voice_id UUID`（**无 FK**，见下）+ 索引。
- `projects`：`tts_provider TEXT NOT NULL DEFAULT 'cosyvoice'`、`cloned_voice_id UUID` FK RESTRICT + 索引；`voice_version_id` 由 NOT NULL 放开为可空。
- `projects` 快照列：`snapshot_tts_provider`、`snapshot_cloned_voice_id`、`snapshot_cloned_voice_name`、`snapshot_cloned_voice_source_sha256`。
- `narration_attempt_records`：新增 `provider_request_id TEXT`（EL request-id 对账副本）。

两处对计划的**有意偏离**（已在验收文档 §24 记录理由）：

1. `channels.default_cloned_voice_id` **不加外键**：与 `channels` 互相引用会让 SQLAlchemy 无法解析建表环，既有 metadata 合同测试（`test_cover_migration`、`test_project_director_infrastructure_contracts`、`test_production_active_job_gates_sql`）会因 SAWarning 失败；按 `current_cover_style_profile_id` 的指针列先例处理，归属校验在服务层。
2. `cloned_voice_accounts` **不重复加 UNIQUE**：`(cloned_voice_id, pool_key_id)` 已是复合主键，再加唯一索引冗余。

### 3.2 领域层（纯规则）

- `identity_project/domain.py`：`TtsProvider`、`ClonedVoice`、`ClonedVoiceAccount`、`ClonedVoiceUsage`；`Channel`/`Project`/`ProjectSnapshot` 扩展配音源与音色事实（`ProjectSnapshot` 的 `voice_version_id`、三件媒体事实改为可空，EL 分支只填克隆音色三列）；纯函数 `cloned_voice_delete_guard`（三类引用保护）、`channel_default_voice_gate`、`cloned_voice_cleanup_candidates`、`normalized_tts_provider`、`_assert_voice_assignment`。
- `identity_project/cloned_voices.py`：上传/名称/备注校验、源音频后缀白名单、槽位可用判定 `slot_available`、自动清理选择器 `cleanup_selection`、端口 `ClonedVoiceClonePort` / `ClonedVoiceSourceStorage` / `ClonedVoiceCatalogPort`。
- `domain/narration.py`：`NarratedSentence.voice_version_id` 可空 + 新增 `provider_request_id`；`narration_provider_blocking_code`（Provider 账号级阻断码映射）；`narration_idempotency_key` 文档化音色身份口径。
- `domain/errors.py`：新增 `ProviderHttpStatusError`（携带状态码，供号池判定矩阵用真实状态码而非猜测）。

### 3.3 端口与仓储

- `identity_project/ports.py`：克隆音色的增删查、账号映射读写、三类引用计数、频道默认音色门禁所需读取（方法在本轮新增；文件因上一会话已有改动，最后一次写入早于本轮，见 §2.3）。
- `application/narration_ports.py`：`VoiceReference` 双源化；`SynthesisRequest.provider/cloned_voice_id`；`SynthesisResult.provider_request_id/provider_request_ids/failed_sequences/errors`；`NarrationBatchFailure`；`previous_request_ids`（仅取 `el:` 前缀、≤3、按句序）；`ProjectSnapshotPort`/`VoiceReferencePort` 返回类型放宽为可空 + 按项目解析音色。
- SQL 仓储 `identity_project/repository.py`：Provider 归一化（历史行回落 cosyvoice）、快照按配音源重建、克隆音色与映射 CRUD、快照三列写入、`_fact()` 处理缺失的媒体事实。
- SQL 仓储 `adapters/persistence/narration.py`：`provider_request_id` 列写入**裸 request-id**（`el:` 前缀只留在 `payload.remote_id`）；`_narrated_json/_from_json` 正确处理 `voice_version_id=None` 与 `provider_request_id` 往返。

### 3.4 应用编排与 API

- `identity_project/cloned_voice_service.py`（新）：上传音频入对象存储 → 逐 active 账号克隆 → 单账号失败如实报告；槽位满时先免费刷新配额、再按「最旧且完全无引用」清理重试；删除走三类引用守卫，远端音色 + 本地行 + 源音频一并清理。
- `identity_project/service.py`：`update_channel` 支持默认配音源/默认克隆音色（改选源时门禁成对）；`create_project` 按源分支（CosyVoice 用公共音色、EL 用本频道克隆音色，缺省用频道默认，未设置一律 `channel_default_voice_required`）；`confirm_narration` 按源冻结快照。
- `identity_project/router.py`（新端点，走 `/identity` + Bearer + 频道所有权）：
  - `GET /identity/channels/{id}/cloned-voices`
  - `POST /identity/channels/{id}/cloned-voices`（multipart：`file` + `name` + `remark`；体积护栏 100 MB）
  - `DELETE /identity/channels/{id}/cloned-voices/{voice_id}`
  - `GET|HEAD /identity/channels/{id}/cloned-voices/{voice_id}/source`（媒体 Cookie + ETag/304 + Range 206）
  - `_channel`/`_project` 序列化新增配音源字段；错误码映射新增三类删除保护（409）与同名冲突（409）。
- `api/narration_runtime.py`：快照哈希仅在 EL 分支加入 `tts_provider`/`cloned_voice_id`（CosyVoice 分支哈希保持不变）；`SqlVoiceReferenceAdapter.get_reference` 改按项目配音源解析（CosyVoice 走参考 ZIP，EL 只返回逻辑音色事实）；`create_narration_runtime` 装配双 Provider 映射 + EL 专用代理客户端 + `SessionScopedProjectRepository`。
- `application/elevenlabs_narration.py`（新，EL Provider）：单句合成 → ffmpeg 解码重采样为 `pcm_f32le/24kHz/mono` WAV → 按 `{seq:04d}-{text_sha256[:12]}.wav` 打包 ZIP；按账号解析/补克隆音色；402 或复核确认的 401 换 key 原地续跑；全池无 key → `provider_quota_exhausted`；字符 10000 兜底；时长护栏；批量路径返回含**批内全部句**的 ZIP 并逐句回传 request-id。
- `application/narration.py`：Provider 路由（`providers` 映射 + `provider_for`）；批量路径逐句远端 id 落库；部分失败按逐句权威码落 `PROVIDER_REJECTED` 且已成功句照常归档；`provider_quota_exhausted` → `narration_provider_quota_exhausted` 项目级阻断（不自动重投、不消耗对象预算）。
- `adapters/audio_transcode.py`（新）：`FfmpegRawWavTranscoder`，把 Provider 原生音频（EL 的 MP3）转成权威 raw WAV 合同。

### 3.5 配置 / 部署件

- `config.py`：`elevenlabs_api_base`、`elevenlabs_proxy`、`elevenlabs_tts_model`、`elevenlabs_per_key_concurrency`（1–5）、`elevenlabs_duration_guard_seconds`（10–600）；**代理只接受 `http://`**（httpx 的 `socks5://` 需要未安装的 `socksio`，允许它只会在建连时才失败）。
- `docker-compose.yml`：`mihomo` 服务（镜像按 digest 钉、仅 default 网络、无端口暴露、无 restart 策略）；web/worker 注入 `TUBEFLOW_ELEVENLABS_*` 且 `depends_on: mihomo: service_healthy`。
- `.env.example`：EL 分组 + `MIHOMO_SUBSCRIPTION_URL` / `MIHOMO_NODE_FILTER`（凭证不入库）。
- `dev-server.mjs`：`/elevenlabs-pool` 前缀；`scripts/build.mjs` 与 `package.json`：新增 `frontend/el-voices.js`。

### 3.6 前端

- `frontend/el-voices.js`（新，纯函数 + 8 个 `node:test`）：配音源定位与文案、音色分配校验（源×音色二选一）、频道默认音色就绪判定、克隆音色视图模型与删除保护展示、上传校验、逐账号克隆结果如实汇总、冻结事实摘要。
- `app.js`：频道管理页新增「配音源与克隆音色」面板（切换默认源、设为默认、试听、删除、上传克隆、逐账号结果提示）；创建向导支持配音源切换与克隆音色选择；配音设置阶段支持源切换、克隆音色选择与「将冻结的音色」展示。
- `frontend/api-client.js`：`clonedVoices` / `uploadClonedVoice` / `deleteClonedVoice` / `clonedVoiceSourceUrl`。

### 3.7 文档

- `docs/planning/USER_DECISIONS.md`：EL 节由 `## 22` 改为 `## 23`（与 10-12 门禁修复节解冲突），并在 §23.1 补一条**实施说明**（`voice_version_id` 可空、快照列、无 FK 指针列的理由）。
- `docs/planning/FINAL_ACCEPTANCE_CRITERIA.md`：新增 §24（17 条验收项）；§3 Provider Token 条款补「号池页限定例外（§23.6）」。
- 全仓注释中的 EL 引用统一为 `§23.x`；指向 10-12 门禁修复节的 `§22` 引用保持不动。

## 4. 审查阶段发现并修复的 6 个真实缺陷

均先写复现/诊断，再修复，并补了会失败的回归测试。

| # | 缺陷与后果 | 证据 | 修复 |
| --- | --- | --- | --- |
| 1 | 批量路径只返回最后一句 ZIP → 批内其余句被判 `provider_output_missing` 重投，**重复扣字符费** | `execute_batch` 用 `extract_batch_audio(output_zip, 批内全部句名)` | 改为一个 ZIP 含批内全部句；测试 `test_submit_batch_returns_one_zip_with_every_unit_and_per_unit_remote_ids` |
| 2 | 批量路径的逐句 request-id 从不落库 → stitching 链恒为空 | 诊断实测修复前 `remote_id=None`、修复后 `el:req-1/2` | `provider_request_ids` 回传 + `_complete_raw` 随完成态落库；测试 `test_batch_result_persists_per_unit_provider_remote_ids` |
| 3 | `_narrated_json` 把 `voice_version_id=None` 写成 `"None"` → 重放该句 `UUID("None")` 抛错，续跑整批 500 | `_narrated_json/_from_json` 原文对可空字段做 `str()`/`UUID()` | 两处按 None 分支 + `provider_request_id` 往返；测试 `test_elevenlabs_narrated_sentence_round_trips_without_public_voice` |
| 4 | 批内单句确定性失败直接抛错 → 同批**已成功句被丢弃并重投**（重复付费） | `narration.py` 批量失败分支把全部活跃句标失败 | `NarrationBatchFailure` + `failed_sequences/errors`：成功句归档、失败句用逐句权威码；测试 `test_partial_batch_failure_keeps_successful_units_and_marks_authoritative_code` |
| 5 | `socks5://` 代理被 validator 接受，但 httpx 需要未安装的 `socksio` → 建连时才炸 | 实测 `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed` | 只接受 `http://`；同步改 `test_elevenlabs_proxy_scheme_validation` |
| 6 | `test_elevenlabs_pool_postgres.py` 硬断言 `alembic_version == "0034"`，head 已到 `0035` → 集成测试必失败 | `alembic heads` = `0035 (head)` | 断言改为 `0035` 并注明只断言到 head |

另修一处协同问题：频道切换面板固定携带 `null` 字段被后端 422 拒绝 → 改为只提交非空字段并给出明确提示（实测后端**不会**静默改状态）。

## 5. 验证结果（本轮实测）

**后端单元测试基线口径（定稿）**：基线 **1437**（同机、同 venv、同沙箱：`1431 passed / 6 failed / 204 errors`，来源 `tmp/run-unit3.txt`）→ 提交前 **1684**（`tmp/gate-unit.xml`）；两次运行的 **204 个 error 身份一致**（全部为 `PermissionError`：沙箱禁止 Python `tempfile` / pytest 临时目录写入与 `os.scandir`，`WinError 5`）、**6 个 failed 身份一致**（固定为 `tests/unit/test_audio_normalizer_contract.py` 的 6 项）；**新增 247 个用例未逐项与基线对照**；**前端在提交之后才暴露 1 处新失败**（`frontend/voice-library.test.js`，因 `handleCreateProject` 改三参导致断言失配，已修复、见 §9）；**集成测试、docker 构建、真实 provider 路径均未覆盖**。

| 检查 | 结果 |
| --- | --- |
| 后端单元测试 | 提交前 **1684 项：6 failed / 204 errors**，身份与基线（1437 项）一致；EL 侧实跑并通过：`test_elevenlabs_*` 83/83、`test_cloned_voice*` 22/22、`test_narration_voice_reference` 5/5 |
| `ruff check src tests migrations/...0035...` | All checks passed |
| `mypy --strict src` | Success: no issues found in 175 source files |
| 前端 `node --check app.js` | 通过 |
| 前端测试（提交前） | 仅 3 组：`el-voices` 8 / `el-pool` 6 / `step07-view` 16 通过（`node --test` 在本沙箱不可用，改为单进程逐文件执行） |
| 前端测试（提交后补跑全量） | 16 个文件 178 项：**177 通过 / 1 失败**（`voice-library.test.js`）→ 修复后 **178/178** |
| `node scripts/build.mjs` | 成功，`dist/frontend/el-voices.js` 已产出 |
| `alembic upgrade 0034:0035 --sql` | 渲染成功，表/列/约束与模型逐项一致 |
| `alembic heads` | `0035 (head)` |

## 6. 部署注意（未验证项，部署前必须确认）

1. **`MIHOMO_SUBSCRIPTION_URL` 必须存在于部署机 `.env`**：本机 `.env` **没有**该键（已核实）；订阅链接已由用户提供给部署负责会话，**部署时写入部署机 `.env`（不入库、不写记忆文件）**。缺失时 `entrypoint.sh` 直接 `exit 1`，而 compose 让 web/worker `depends_on: mihomo: service_healthy` → **整个 API 不会启动**（不是只暂停 EL）。在订阅链接写入前，部署该版本会阻塞整栈启动。
2. mihomo 健康检查用 `wget`（`compose` 第 159 行），**未验证该镜像是否自带 wget**；缺失会导致同样后果。（本轮无法 pull 验证：沙箱禁止 docker 命名管道。）
3. 容器内 mihomo 二进制路径由入口脚本探测，**首次部署才验证**。
4. **Postgres 集成测试一次都没跑过**，`alembic upgrade` 也从未在真实库执行（本机无 DB、Docker 被禁）；`0035` 仅做离线 SQL 渲染与模型一致性核对。**该验证由部署负责会话在部署机执行**（2026-10-13 分工确认，结果回填 `runtime/operations/elevenlabs_deploy_20261013.md`）。
5. `0035.downgrade()` 会把 `projects.voice_version_id` 恢复为 NOT NULL，**若已有 EL 项目会失败**；回滚建议仅在无 EL 项目时执行。
6. 生产镜像仍是 `sha256:53fab7b2…ca070f`（不含本轮代码，`pull_policy: never`）；部署需按 `docs/DEPLOYMENT.md` §5 构建/钉新镜像。
7. 真实 EL 调用额度仍为 0（授权记录）；克隆与 TTS 属付费操作，需另行逐次授权。

## 7. 提交范围说明（2026-10-13 用户决策；2026-10-13 终稿口径）

用户逐项确认后的提交边界与最终计数口径：

1. **提交 58 项** = **A 类 11**（本轮新建代码/测试/前端）+ **B 类 21**（本轮修改，含两份规划文档）+ **C 类 18**（上一会话 WP1–WP3 新建）+ **D 类 4**（更早任务写入、本轮续改的测试文件）+ **E 类 4**（上一会话改动、本轮未续改：`identity_project/ports.py`、`identity_project/adapters.py`、`api/app.py`、`adapters/persistence/schema.py`）+ **本文档 1**（§2.1 表末项）。
2. **未提交 5 项** = **F 类 2**（`docker-compose.yml`、`dev-server.mjs`，由本会话组 1 补提交）+ **G 类 1**（`frontend/voice-library.test.js` 断言修复，随组 1）+ **H 类 2**（`backend/src/tubeflow_test/config.py`、`.env.example`，两层混写，由部署负责会话在冻结窗口提交）。
3. **H 类影响（如实表述）**：本提交包含的代码引用 `settings.elevenlabs_*`（`api/narration_runtime.py` 5 处），但本提交内的 `config.py` **不含这些字段声明**（提交内 `elevenlabs` 出现 0 次，工作区 12 次）。因此**单独检出本提交不是可运行态**：走到 EL 客户端装配处会抛 `AttributeError`（pydantic 未声明字段，不存在"取默认值"）；当前工作区（含未提交的 `config.py`）功能不受影响。
4. **不提交**：其它任务的已修改文件与未跟踪文件（含 `.codex-tmp/`、`tmp/`、`.workbuddy/`、并行任务新建件）保持原样留在工作区。
5. 提交信息（用户确认）：
   `feat(elevenlabs): integrate paid EL dubbing with key pool, cloned voices and mihomo proxy`

## 8. 提交记录与分支事实（2026-10-13 16:47）

- 提交：`69b1808f8d388767038ee49e43db2c1192531f17`（2026-10-13 16:47:28 +0800），
  `feat(elevenlabs): integrate paid EL dubbing with key pool, cloned voices and mihomo proxy`。
- 分支：`codex/01a049161e167002ac505153d422f0b3`，父提交 `f206c54`（2026-09-08 快照线），
  本次 58 个文件、`13955 insertions(+), 473 deletions(-)`。
- 分支关系（已核实）：`main`（`29a078f`，2026-08-29）与本分支的共同祖先为 `96f7171`；
  `main` 相对该祖先仅多 1 个提交（9 文件，语音目录并发导入修复），本分支相对该祖先多 161 个提交。
  **本分支不是从 `main` HEAD 分叉，而是与 `main` 平行且更靠后的开发线**；`main` 的提交时间早于本线基线，
  因此本提交不是"落后于 main"，合并方向应为"把 `main` 并入本线"（如需）。
- 提交后仍留在工作区（未提交）：`backend/src/tubeflow_test/config.py`、`.env.example`（并行任务混改，见 §7）、
  其它任务的 174 项脏条目，以及并行任务新建的 `backend/tests/unit/test_director_timeout_budget.py`。
- 提交前门禁（本次实测）：`pytest tests/unit` 1684 项 / 6 failed / 204 errors（全部环境性）；
  `ruff`（白名单 45 个 py 文件）All checks passed；`node --check`（白名单 6 个 js）通过；
  `node scripts/build.mjs` 成功；`mypy --strict` no issues in 175 files。

## 9. 变更历史

- 2026-10-13 16:36 首版：WP4–WP7 改动汇总、审查修复记录、验证结果与部署注意。
- 2026-10-13 16:50 补充：提交范围说明（§7）与提交记录、分支事实（§8）；登记"部署机无订阅链接"与"集成测试留待另一会话"两项用户决定。
- 2026-10-13 第 3 版（勘误与口径定稿）：按第三轮复核结论修正 §2.1/§2.2/§2.3/§2.4/§2.5 的标题与归属划分。关键修正：① `test_auto_pipeline.py`、`test_narration_auto_retry.py`、`test_workflow_summary.py`、`test_narration.py` 由"上一会话未续改"更正为 **D 类（更早任务写入、本轮续改）**，与 §2.2 不再重复计数（`test_narration.py` 归 D 类）；② §2.2 标题由 22 更正为"表 26 行：入提交 24、混写未提交 2"；③ §2.4 更正为"上一会话改动、本轮未续改 6 项（已提交 4 + 未提交 2）"；④ §2.5 恢复被误删的 5 行表格（表内 5 + 正文 13 = 18）；⑤ §5 测试口径改为定稿基线句（1437→1684、204E/6F 身份一致、新增 247 项未逐项对照、前端 1 处提交后修复、集成/构建/真机路径未覆盖），并补齐 EL 侧实跑通过数（83/22/5）与前端全量 178 结果；⑥ §7 计数改为 **提交 58 = A11+B21+C18+D4+E4+本文档 1**、**未提交 5 = F2+G1+H2**；⑦ 更正 §7.3 的"Settings 走默认值"错误表述为"单独检出该提交不可运行（`AttributeError`）"。
- 2026-10-13 补提交（同批）：组 1（`docker-compose.yml`、`dev-server.mjs`、`frontend/voice-library.test.js`、本文档）与 g 修复（`production_authority.py` 的 `_frozen_voice_missing` 抽取 + 真值表单测）各自独立提交。
