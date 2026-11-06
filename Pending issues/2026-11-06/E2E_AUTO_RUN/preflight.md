# A 开跑前只读核查（2026-11-06T08:50:56Z，部署机）

原始输出：同目录 `preflight.raw.txt`。全部只读，无 Provider 调用。

## 1. H3 与合同

- `GET /group-videos/contract` → `{"available":true,"status":"AVAILABLE"}`。
- `h3_engine_nodes`（6 台）：

| node_id | enabled | health | capacity | claimed | workflow_id | workflow_version | manifest 前 12 |
| --- | --- | --- | ---: | ---: | --- | --- | --- |
| 5baad724… | f | OFFLINE | 2 | 0 | h3.community.reference-images-to-video | 1.1.0-mvp | a8b0461398da |
| cb883e07… | f | OFFLINE | 2 | 0 | 同上 | 同上 | a8b0461398da |
| fee2aa26… | f | OFFLINE | 2 | 0 | 同上 | 同上 | a8b0461398da |
| h3-new-1 | t | OFFLINE | 2 | 0 | h3.tubeflow.storyboard-to-video | 1.0.0 | 65bf74530537 |
| h3-new-2 | t | **READY** | 2 | 0 | 同上 | 1.0.0 | 65bf74530537 |
| h3-new-3 | t | **READY** | 2 | 0 | 同上 | 1.0.0 | 65bf74530537 |

可用总槽位 4（h3-new-2/3 各 2）。

## 2. 同时在跑的 AUTO

`auto_pipeline_runs where state in ('RUNNING','PAUSED')` 只有一条：

- `faf27eb5-23d4-43e7-8269-029336e38167`，RUNNING，`authorization_round=1`，`updated_at=2026-11-04 13:51:51Z`。
- 该项目的 `project_director_units` 最新一条是 2026-11-04 13:51:51Z，LEASED/SUBMITTING 计数 0，无任何 `video_generation_attempts`。
- **判定：陈旧 RUNNING（两天无推进），不是真实在跑。只记录、不处置**（且在保护名单内）。做耗时对比时注明。

## 3. 现行配置（容器 printenv 实读）

| 开关 | web | worker |
| --- | --- | --- |
| `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT` | `true` | `true` |
| `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT_MIN_DISTANCE` | `0.10` | （未注入，仅派发侧用） |
| `TUBEFLOW_H3_SINGLE_PANEL_OPT_IN_ENABLED` | `true` | — |
| `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS` | `true` | — |
| `TUBEFLOW_VIDEO_PROMPT_STRATEGY` | `tubeflow_storyboard_v4_natural_motion` | 同 |

`TUBEFLOW_DIRECTOR_PARALLELISM` 未显式设置 → 代码默认 4（`config.py:126`）。

## 4. 参照项目 484746c4（逐字读出）

| 字段 | 值 |
| --- | --- |
| title | 사장님 없는 무인 아이스크림 가게, 정말 돈이 될까? |
| channel_id | `f28a0845-6ddf-4111-ae2d-f5de595a5fd6`（韩国小生意经济财经） |
| character_image_ids | `{500c0519-94fe-448b-8410-15152629a6cd}` |
| locale / aspect_ratio | **ko-KR** / 16:9 ✅ |
| expected_duration_seconds | 空 |
| tts_provider / cloned_voice_id / voice_version_id | `elevenlabs` / `8c529fdd-cd89-4430-a76d-43c986547d89` / 空 |
| production_mode / state | AUTO / ACTIVE（`auto_budget_round=1`） |
| script | 1274 字符，md5 `4c4cc468bb2da3ab8de44d6a01c4d38f` |
| visual_style | 见 `preflight.raw.txt` 第 33 行，逐字同（欧美二维手绘动画…文字和水印。） |
| owner | `admin-74d1abc03713` |

引用有效性：`character_images` 1 行、`channels` 1 行、`cloned_voices` 1 行；频道已设默认音色（`elevenlabs` + 同一 cloned voice，默认 CosyVoice 版本 `5c1d77e6…` 仅作兼容音色存在）。

**闸门结论：locale = ko-KR，三项引用全部有效 → 可以开跑。**

## 5. 耗时参照（484746c4）

| 阶段 | 条数 | 首条 | 末条 |
| --- | ---: | --- | --- |
| 前置导演 `project_director_runs` | 1 | 2026-11-05 05:45:39Z | 同 |
| 故事板 `storyboard_generations` | 16 | 05:54:08Z | 05:56:09Z |
| 视频 `video_generation_attempts` | 21 | 06:06:33Z | 2026-11-06 03:45:43Z |

- 13 组、21 次视频尝试（第 7、12 组各 5 次，含 §51 抖动 A/B 的变体臂，不是纯 AUTO 重试）。
- `composition_jobs` 无时间戳列，合成耗时改用运行记录里的时间差。
- 该项目的 `probe_evidence_json->'motion_metrics'` 全为空（归档早于 2026-11-06 04:42 指标上线），所以**本次新项目将是第一条带 motion_metrics 的自然归档**。

## 6. 结论

可以按 A.2 建项目。同时段唯一活动 AUTO 是两天未推进的陈旧 RUNNING（`faf27eb5`），不构成资源竞争，但耗时对比时注明。
