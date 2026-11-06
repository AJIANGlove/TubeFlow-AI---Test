# 抖动：离线普查与提示词 A/B 方案（2026-11-05）

依据：用户 2026-11-05「全部按推荐执行」，方向为「先用固定 seed 对提示词做 A/B，同时离线统计抖动有多普遍」（USER_DECISIONS §51）。根因见 `../JITTER_484746C4_20261105/README.md`。

## 1. 离线普查（只读，无 Provider 调用）

| 文件 | 内容 |
| --- | --- |
| `held_frames.py` | 隔帧停顿占比，只用 ffmpeg（`tblend` 差分 + `signalstats`）。算法与 `../JITTER_484746C4_20261105/scripts/cadence.py` 相同 |
| `jitter_survey.py` | 在 web 容器内对当前分组视频逐条测量；用法写在文件头 |

**本地复核：** 用 `held_frames.py` 重算 `cadence.txt` 中的 7 段视频，结果与原记录逐项一致：
- 484746c4 第 1 组 0.10、第 9 组 0.19；
- fa3a0b30 第 1 组 0.05、第 2 组 0.43、第 4 组 0.24、第 8 组 0.35、第 9 组 0.00。

**口径：**
- 隔帧停顿占比：在有运动、且不跨镜头切点的 12 帧窗口里，「运动只发生在每隔一帧」的窗口所占比例。
- 只覆盖抖动 A（主体隔帧停顿）。机位突跳 B 和静止微颤 C 仍看既有 HF 指标；HF 测不到 A。
- 「明显」暂定为占比 ≥ 0.2，只用于计数，不是验收阈值。

## 2. 提示词 A/B（2026-11-06 修订：已授权，USER_DECISIONS §51 第 3 项）

**修订说明：** 原方案（2026-11-05）是第 1、9 组、两臂各自编译。2026-11-06 用户确认的改动：
- 改用第 7、12 组：普查时当前视频的隔帧停顿占比为 0.72、0.49。第 1、9 组只有 0.08、0.20，基线太低，看不出改善。
- 不加第三组（v3 提示词），原因见 USER_DECISIONS §51 第 3 项。
- 按镜头分开报告，并增加抖镜头一条判定。

另有一处实现改动：变体臂不再单独编译，而是复用同组、同 seed 基线臂的已编译提示词，再插入这一句。这样两臂只差这一句，DeepSeek 的随机性（如每镜选哪种运镜）不再混进对比，DeepSeek 调用也从名义 8 次降到 4 次。额度上限不变。

**对象：** `484746c4` 第 7 组、第 12 组，沿用当前故事板，不重新生成故事板。

**两臂：**
- 基线（`experiment_arm: "baseline"`）：现行 `tubeflow_storyboard_v4_single_panel`，照常编译。
- 变体（`experiment_arm: "cadence_v1"`）：取同组、同 seed 基线臂的已编译提示词，在 `detailed_description` 的 `[Shot 1]` 之前插入下面这一句，作为独立一行，不调用编译器。策略记为 `tubeflow_storyboard_v4_single_panel_cadence_v1`。英文原文逐字如下：
  > Animate every frame at the full frame rate with smooth, continuous motion; never hold a pose for two frames or use limited animation ("animating on twos"); people, hands and objects move a little on every frame.

现行 v4 的规则 8、9 已经有「stable camera」「Static camera, 0% movement」，负面清单里也已有「camera shake, jitter」（`video_prompt/templates.py`）。所以变体只针对 A（主体隔帧停顿），不重复已有的机位规则。

**seed：** S1 = 798160553724158，S2 = 798160553724159。两臂通过 `ManualH3Input.seed` 显式给定同一对 seed；变体的 seed 必须等于其基线的 seed，否则拒绝。

**额度：**

| Provider | 名义 | 硬上限 | 说明 |
| --- | ---: | ---: | --- |
| DeepSeek 编译 | 4 | 16 | 只编译 4 个基线臂；每次允许 1 次修复；同一幂等键重发也会再编译一次 |
| H3 视频 | 8 | 8 | 2 组 × 2 臂 × 2 seed |

- 失败、UNKNOWN、取消都计入额度，不重提。
- 实验尝试按 MANUAL 处理，系统不会自动重提。

**执行顺序（每组串行；两组可以并行）：**
- 每组依次为：S1 基线 → S1 变体 → S2 基线 → S2 变体。
- 同组同一时刻只允许一个未终结的尝试，所以必须等上一条终结后再提交下一条。
- 基线 H3 失败的，不再提交它的变体（省下这次调用），这一对记为「不可比」。

| 步 | 请求体（`POST /group-videos/groups/{group_id}/generations`） | `Idempotency-Key` |
| --- | --- | --- |
| 1 | `{"expected_storyboard_generation_id": "<当前故事板>", "seed": 798160553724158, "experiment_arm": "baseline"}` | `jitter-ab-v1-g7-s1-baseline` |
| 2 | `{"expected_storyboard_generation_id": "<同上>", "seed": 798160553724158, "experiment_arm": "cadence_v1", "prompt_source_attempt_id": "<第 1 步的 attempt_id>"}` | `jitter-ab-v1-g7-s1-cadence` |
| 3–4 | 同上，seed 换成 798160553724159 | `jitter-ab-v1-g7-s2-baseline` / `-cadence` |

第 12 组相同，键里的 `g7` 换成 `g12`。

- POST 报错或超时时，不要换新键重发。先按键查 `video_generation_attempts`，确认是否已经落库。
- 节点无法指定；按实际节点记录，作为混淆项。

**测量（`ab/`，只读）：**
1. `ab_export.py`（在 web 容器内运行）：导出 8 条尝试的元数据，以及已归档的 MP4。
2. `ab_measure.py`（需要 ffmpeg、numpy、opencv-python）。对每条视频计算：
   - 整条的隔帧停顿占比（`held_frames.py`，与普查同一算法）；
   - 按画面实际切点分镜：每镜的隔帧停顿占比与 HF，并标出提示词为该镜要求的运镜；
   - 抖镜头数（HF ≥ 0.25）；
   - 每 0.5 秒一格的接触表。

   它还会逐对核对「变体提示词去掉这一句后，是否与基线逐字相同」。
3. 目视：逐张看接触表，按对比较，看四项——身份（是否参考图人物）、多余人物、可读文字、分屏或网格泄漏。结果写入 `visual.json`（无缺陷写空列表），然后重跑 `ab_measure.py` 得出结论。

**判定（预先写定）：** 4 对比较中，三条同时成立，才把「变体设为默认」提交用户决定；否则如实报告为「无效或不确定」。
1. 至少 3 对的变体整条隔帧停顿占比低于基线。「不可比」的对算作「不低于」。
2. 变体的抖镜头总数不多于基线。
3. 任何一对中，变体都没有其基线没有的画面缺陷。

**代码：** PR「jitter A/B experiment arms」。
- `ManualH3Input.experiment_arm` / `prompt_source_attempt_id`，缺省为空，行为不变。
- 实验臂要求显式 seed，并且只用于 crop-grid-v2 分组。
- 实验身份写入快照的 `experiment` 键，并参与幂等比较。
