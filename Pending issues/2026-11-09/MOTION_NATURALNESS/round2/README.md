# 第 2 轮：只改提示词的 H3 对照（edited_v1 臂，USER_DECISIONS §54）

**状态**：工具与只读演练随交接包 v1 交付；正式提交（调用 H3）等两件事——第 1 轮导出的分析结果，以及用户另行确认的 Provider 额度。本文的臂设计在第 1 轮分析后定稿，定稿前均为**计划，未验证**。

## 1. 机制

- 手动 H3 新增实验臂 `edited_v1`：请求带整段已编辑提示词 `edited_prompt`、其 `edited_prompt_sha256`、变体标签 `variant_label`，以及来源尝试 `prompt_source_attempt_id`（同组、同故事板、同参考输入的 v4 单格尝试，默认取该组当前视频）。
- 提示词按 v4 单格合同全量校验（分段、绑定行、逐镜格位映射、跨镜引用、代词、`Maintain` / `No storyboard grid` / `split screen` / `text` / `watermarks` 等必需措辞、无配乐），**不调用编译器**；策略记为 `tubeflow_storyboard_v4_single_panel_edited_v1`。
- seed 由请求显式给出，可与来源不同（同一提示词换 seed 即对照）。参考图、工作流、其余参数与来源请求相同（单元测试核对）。
- 与 §51 其他实验臂相同：不自动重提、不做停滞处置、不替换已有的当前视频、只用于 crop-grid-v2 分组。

## 2. 工具 `edit_submit.py`（web 容器内运行）

一次运行只提交一步；`prompt_compiler=None`，脚本内不可能调用 DeepSeek。

| 选项 | 含义 |
| --- | --- |
| `--project-id` + `--group-seq`（或 `--group-id`） | 目标组；故事板取 `storyboard_current`，来源尝试默认取 `group_current_videos` |
| `--seed <整数>` 或 `--seed source` | `source` = 沿用来源尝试请求里的 seed |
| `--source-text` | 提示词用来源尝试原文（R0 复跑对照） |
| `--edited-prompt-file F --expected-sha256 H` | 提示词用文件原样内容（UTF-8，不去空白、不转换换行；含 `\r` 或 sha256 不符即拒绝） |
| `--variant-label` | `[a-z0-9][a-z0-9_-]{0,47}` |
| `--dry-run` | 在回滚的会话里做与 `create()` 相同的只读预取（`for_intent=False`，面板只在内存派生）并全量校验提示词；不创建尝试、不调用 Provider；打印 `DRYRUN_OK {...}` 或拒绝原因 |

输出：来源尝试摘要、源与编辑稿的 sha256、逐行差异（`DIFF` 行）、同组未终结尝试、提交后的 `READBACK` 与当前视频指针前后值。项目 `production_state` 非 `ACTIVE` 时正式提交拒绝（演练只记录）。

## 3. v1 包里的只读演练

对第 116 组（船上）与第 119 组（湖岸、蓄水池）各跑一次：

```
python /tmp/edit_submit.py --project-id 2280f7c6-f0c7-439a-8d82-71c52b588cbf --group-seq 116 \
  --seed source --source-text --variant-label r0 \
  --idempotency-key motion-r2-g116-r0-dryrun --step-label g116-r0-dryrun --dry-run
```

用途：在正式花额度之前确认这两组能作为来源（v4 单格、参考输入未变、原文过合同校验）。若报 `video_reference_input_changed` 或 `experiment_requires_v4_single_panel`，第 2 轮改用「先跑新基线臂、再编辑基线文本」的方案（需要额外 DeepSeek 编译与 H3 次数），届时另行报额度。

## 4. 计划中的臂（第 1 轮分析后定稿，未验证）

| 臂 | 提示词 | 检验的假设 |
| --- | --- | --- |
| R0 | 来源原文不变 | 复跑对照：同提示词换 seed 时配角与环境是否仍静止 |
| M1 | 在该镜块里写明配角动作与环境运动（如船随划桨轻晃、水面起波纹） | H1「没写就不动」 |
| M2 | M1 + 去掉抑制性措辞（`stable camera`、负规则里的 `camera shake / jitter / flickering textures`），保留合同必需措辞 | H2「抑制性措辞压住了环境运动」 |

计划规模：2 组 × 3 臂 × 2 seed（来源 seed + 另 1 个）= 12 次 H3，0 次 DeepSeek。判定规则（区域位移、扣除镜头运动后的场景运动占比、用户目视）在 v2 包提交前写定，事后不改口径。
