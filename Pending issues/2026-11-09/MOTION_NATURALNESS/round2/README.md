# 第 2 轮：只改提示词的 H3 对照（edited_v1 臂，USER_DECISIONS §54）

**状态**：臂、seed、测量口径与判定规则已于 2026-11-10 写定（本文第 4、5 节与 `spec.json`），提交后不再改动。额度已由用户确认（USER_DECISIONS §54 第 4 项：本研究 H3 ≤ 24、DeepSeek ≤ 16）；本轮用 12 次 H3、0 次 DeepSeek，由交接包 v2 下发。依据：`../round1/ANALYSIS.md`。

## 1. 机制

- 手动 H3 实验臂 `edited_v1`：请求带整段已编辑提示词 `edited_prompt`、其 `edited_prompt_sha256`、变体标签 `variant_label`，以及来源尝试 `prompt_source_attempt_id`（同组、同故事板、同参考输入的 v4 单格尝试，默认取该组当前视频）。
- 提示词按 v4 单格合同全量校验，**不调用编译器**；策略记为 `tubeflow_storyboard_v4_single_panel_edited_v1`。
- seed 由请求显式给出，可与来源不同。参考图、工作流、其余参数与来源请求相同。
- 与 §51 其他实验臂相同：不自动重提、不做停滞处置、不替换已有的当前视频、只用于 crop-grid-v2 分组。

## 2. 工具

| 文件 | 在哪跑 | 作用 |
| --- | --- | --- |
| `edit_submit.py` | web 容器 | 一次提交一步（`prompt_compiler=None`，不可能调用 DeepSeek）；`--dry-run` 在回滚会话里只读演练 |
| `collect_r2.py` | web 容器 | 只读收集实验尝试与两条生产尝试的状态、指标与输出视频原文件 |
| `make_compare.py` | 有 ffmpeg 的任意机器 | 每组 6 格对比视频（不印文字，格位对应见 `KEY.json`）+ 每个视频的 960 宽副本 |
| `measure_motion.py` | 有 numpy、opencv-python、ffmpeg 的机器 | 按 `spec.json` 的区域测残差运动；没有 opencv 就由 Claude 在云端跑 |

`edit_submit.py` 选项：`--project-id` + `--group-seq`（或 `--group-id`）；`--seed <整数>|source`；`--source-text`（R0）或 `--edited-prompt-file F --expected-sha256 H`；`--variant-label`；`--idempotency-key`；`--step-label`；`--dry-run`。

## 3. v1 只读演练结果（2026-11-10，实施方）

第 116、119 组均 `DRYRUN_OK`：来源 `4bb5c37b-…`（seed 2253417698150749）、`137ab7a8-…`（seed 8248958064718409），策略 `..._edited_v1`，项目 `ACTIVE`，参考策略 crop-grid-v2；项目尝试数前后都是 165。日志：`dryrun/`。

## 4. 臂与次数（写定）

| 臂 | 提示词文件 | 改动 | 检验 |
| --- | --- | --- | --- |
| R0 | 来源原文（`--source-text`） | 无 | 复跑对照；同 seed 时也检验 H3 是否可复现 |
| M1 | `prompts/g116_m1.txt`、`prompts/g119_m1.txt` | 按分格图写出画面里的人和物并各给自然动作：116 第 1 镜两名队员的动作、船随拉绳轻晃、船周水面起波纹（替换原来的 `small shadowed figures … work`）；119 第 1 镜补写 (S1)、提灯火苗、衣摆、树枝、云、倒影与波纹，第 2 镜补写云、树梢、水面波纹。其余文字不变 | H6/H1：编译器若「看过分格图」，配角与环境会不会动 |
| M2 | `prompts/g116_m2.txt`、`prompts/g119_m2.txt` | M1 + 去掉全局 `stable camera`、负面规则里的 `camera shake, jitter` 与 `flickering textures`；116 另去掉 Maintain 里的 `the boat and lake geometry`。合同必需措辞全部保留 | H2：抑制性措辞是否压住了环境运动 |

| 文件 | 字符数 | sha256 |
| --- | --- | --- |
| R0 g116（来源原文） | 3723 | `c287c4d6b5e7c2c96aed65fc6e1504bdcd397fca96ec93d3184ad552697d918a` |
| `prompts/g116_m1.txt` | 3987 | `339183968e47fa764c3611d96c18ec4d4fe09c5f35d182a8b61dab1359b53826` |
| `prompts/g116_m2.txt` | 3901 | `fa0632a6c191e071f282e8ad1b73a8f2e3e3598de2a9aa3250f850d20bf13080` |
| R0 g119（来源原文） | 4087 | `fadafbf2d85f7337aa62caf36d37211661db5ce2744733d7c6bec80f563ef339` |
| `prompts/g119_m1.txt` | 4556 | `7e0efac93d045d6109f01eef76e61cf6455d7f1d3422bcc68e5b67d8a25da729` |
| `prompts/g119_m2.txt` | 4498 | `78d2bb0f9b8ab6be54e1a88682c0b1c0e4186dc8531c8e4bc8e6e6841aa8b7fd` |

4 个编辑稿在 Claude 云端已用生产校验函数 `validate_single_panel_compiled_prompt`（按导出的编译输入重建期望值）全部通过；两份来源原文在同样重建下也通过，作为重建正确的对照；故意删掉 `No storyboard grid` 的稿被拒（`h3_prompt_negative_rules_missing`）。部署机上的 `--dry-run` 是最终校验。

seed：来源 seed（`source`）与 `20261110`。共 2 组 × 3 臂 × 2 seed = **12 次 H3、0 次 DeepSeek**。每次提交只算一次 H3：失败、超时、状态不明都计入，不重提、不补跑。

幂等键：`motion-r2-g<组>-<臂>-<source|second>`，例如 `motion-r2-g116-m1-second`。

## 5. 测量与判定规则（写定，事后不改）

**测量**（`measure_motion.py` + `spec.json`）：在每个输出视频里检测切点，取含计划中点的段作为目标镜头（116 第 1 镜；119 第 1、2 镜），两端各去 6 帧；每隔 6 帧取一对，480 宽 Farneback 光流，用 RANSAC 仿射扣除镜头运动；每个区域给残差速度（960 宽画面上的像素/秒）。`env_score` = 该镜「环境区域」残差速度的中位数（116：左配角、右配角、船舷、水面；119 第 1 镜：水面、天空、左侧树枝；第 2 镜：天空、水面、左侧树）。119 的组得分 = 两个目标镜头 `env_score` 的平均。

生产基线（成片测得，不是第 2 轮输出）：116 第 1 镜 `env_score` 0.67（左配角 0.68、右配角 0.64、船舷 0.66、水面 0.71；主角 6.62；静止树线 0.43）；119 第 1 镜 1.42、第 2 镜 1.40。

**有效性**：尝试须 `ARCHIVED` 且有输出视频，目标镜头检测段 ≥ 1.5 s。某臂在某组少于 2 个有效 seed，该组该臂的比较记「无法判定」。

**判定**（同组、同 seed 比较，两个 seed 都满足才算）：

1. **H6/H1 成立**：M1 的组得分 ≥ 2 × R0，且 M1 的组得分 ≥ 1.5 px/s（高于噪声底）。两组都成立 → 成立；只一组成立 → 部分成立；否则不成立。116 另单列「配角动了」：左右配角各自 ≥ 2 × R0。
2. **H2 成立**：M2 的组得分 ≥ 1.5 × M1。
3. **退化闸**（任一条触发，该臂不能作为改模板的方向，即使运动变多）：自动指标 `held_share` 或 `hf` 比同 seed 的 R0 高 50% 以上且超过参考线（`held_notable` 0.2、`jitter_shot_hf` 0.25）；116 的静止树线残差速度 > 3 × R0 且 > 1.5 px/s（整画面晃动或纹理游动）；用户看到抖动、变形、闪烁或多出人物。
4. **可复现性**：R0 在来源 seed 下的输出字节与生产输出相同 → H3 可复现；不同 → 用 R0 两个 seed 之间的差异作为同臂噪声参考，写进结果。
5. **最终标准是用户目视**：用户先盲看两段对比视频（格位 1–6，不看 `KEY.json`），说出哪几格「自然」、哪几格「僵」或「怪」；数值判定说明「为什么」，用户判定说明「好不好」。两者不一致时如实记录，由用户决定方向。

**下一步（按结果）**：H6/H1 成立 → 第 3 轮做「看得到分格图」的编译（给编译器每格画面内容，并要求为画面里每个人和物写自然动作），新编译需要 DeepSeek 次数；只有 H2 成立 → 评估去掉抑制性措辞的风险；都不成立 → 转向 H3（参考图锁定）与 H4（画风）。
