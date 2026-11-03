# 视频提示词 v3 与 v4 自然运动 A/B 方案（2026-11-03）

状态：已获用户批准（2026-11-03「允许合并；允许部署；允许发送真实请求」「全部按推荐按执行」）。
本方案在任何真实提交前冻结；执行结果另写 `VIDEO_QUALITY_AB_RESULTS_20261103.md`。

## 1. 目的

回答两个问题，不做超出范围的推断：

1. 故事板提示词「参考图只作身份参考」是否能在**冻结风格仍含冲突句**的情况下，让人物姿势随分镜变化、同时保持身份？
2. 在同一故事板、同一节点、同一固定 seed 下，`tubeflow_storyboard_v4_natural_motion` 相对 `tubeflow_storyboard_v3` 是否改善人物一致性与抖动？

## 2. 测试对象

| 项 | 值 |
| --- | --- |
| 项目 | `31e00d87-5332-4b60-88e9-8b6dd8d475e7`（冻结快照，标题见根因文档） |
| 分组 | 第 11 组 `c42821e5-397b-512d-b2f0-efee9383473e`（1 格）；第 16 组 `2e94c2c6-3a4b-5d49-a8ed-770ed3162aeb`（3 格） |
| 原故事板 | 11 组 `68b32c5b-946d-498a-b025-3451a4d0ce66` v1；16 组 `585707ad-28be-4516-9b92-269800882e3a` v2 |
| 新故事板 | 11 组 `3c58e366-a2f0-4602-9bb1-60059f101346` v2；16 组 `028be1fe-5730-4273-8db4-2d7321e08f2b` v3 |
| 固定 seed | `798160553724158`（两组、两策略共用） |
| H3 节点 | 项目当前默认 profile `candidate`；调度器按 `max(available_capacity, node_id)` 选点，空闲时稳定为 `h3-new-3`，实际节点以 attempt 记录为准 |
| 全局策略 | 全程保持 `tubeflow_storyboard_v3`，**不切换** |

## 3. 方法

### 3.1 故事板（2 次真实生图）

用生产 `StoryboardGenerationService.create()`（与 UI 同一装配）为每组各创建 1 个新版本：同一冻结快照、同一导演修订、同一风格（仍含「保留原图姿势…仅转换艺术风格」冲突句）。
不修改项目快照、不改频道风格、不重做导演。新版本成功后成为该组当前故事板（旧版保留）。

### 3.2 视频提示词编译（4 次 DeepSeek）

用生产 `VideoPromptCompiler`（真实 canonical loader + `_compiler_input`）分别以
`tubeflow_storyboard_v3` 与 `tubeflow_storyboard_v4_natural_motion` 编译同一故事板，
每次 `max_repairs=0`，写入真实 `video_prompt_compilations` 表。
因为策略是编译入参而非读全局 env，本步骤**不需要**改动 `TUBEFLOW_VIDEO_PROMPT_STRATEGY`。

### 3.3 H3 视频（4 次）

按 `runtime/operations/h3_visual_quality_experiments_20261027/` 的提交脚本方法：把编译好的提示词、
角色参考图与新故事板图上传到 H3 节点 ComfyUI 输入目录，用同一图结构、同一 `noise_seed`、
同一 15 秒 / 16:9 / 8 步参数提交，输出前缀区分 `v3` / `v4`。
该路径不创建产品 attempt、不切换当前视频、不写 outbox；因此这 4 条视频只是实验产物。

## 4. 调用预算

| Provider | 名义次数 | 硬上限 | 说明 |
| --- | ---: | ---: | --- |
| grsai 故事板 | 2 | 6 | 每组 1 次；失败时按现有 7 模型故障转移链，上限 3 个模型/组 |
| DeepSeek 编译 | 4 | 8 | 每次 1 次，`max_repairs=0`；若出口合同失败不重试，直接记失败 |
| H3 视频 | 4 | 4 | 每组 v3/v4 各 1 次，固定 seed |
| 合计 | 10 | 18 | 失败与 UNKNOWN 计入，不自动重提 |

## 5. 门槛与停止条件

1. 新故事板 `status=SUCCEEDED` 才进入视频步骤；FAILED / REMOTE_STATE_UNKNOWN 就地停止并报告。
2. 任一编译失败（出口合同）即停止该组后续提交，不换措辞重试。
3. H3 提交后只轮询原任务；UNKNOWN / 失败如实记录，不创建替代提交。
4. 不修改：项目快照、频道默认风格、当前视频指针、默认策略、`.env`。

## 6. 测量

- 每条视频保存编译提示词全文与 SHA-256、compilation id、seed、节点、输出 SHA-256。
- 每 0.5 s 抽帧（15 s → 30 帧），检查：与角色参考图同一人、画面仅一个角色实例、无格线 / SHOT 角标 / 画面数字。
- 抖动按 `docs/VIDEO_JITTER_DIAGNOSIS_20261018.md` 的 HF 方法（位移减 5 帧滑动均值后的残差 RMS），目标 ≤ 0.25。
- 每个新故事板逐格核对：姿势是否跟随该格 Action / Visual、是否区别于参考图姿势、身份是否保持。

## 7. 交付

`Pending issues/2026-11-03/VIDEO_QUALITY_AB_RESULTS_20261103.md`：输入（ID、SHA-256、seed、节点、策略）、
调用计数、v3 vs v4 对照表、关键帧路径、结论与未验证范围。
