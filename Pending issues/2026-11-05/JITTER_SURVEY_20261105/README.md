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

## 2. 提示词 A/B（待用户授权调用次数后执行）

**对象：** 484746c4 第 1 组、第 9 组（出现抖动的两组），沿用当前故事板，不重新生成故事板。

**两臂：**
- 基线：现行 `tubeflow_storyboard_v4_single_panel`。
- 变体：在基线基础上只追加一句运动节奏要求。英文原文如下，实施时逐字使用：
  > Animate every frame at the full frame rate with smooth, continuous motion; never hold a pose for two frames or use limited animation ("animating on twos"); people, hands and objects move a little on every frame.

现行 v4 的规则 8、9 已经有「stable camera」「Static camera, 0% movement」，负面清单里也已有「camera shake, jitter」（`video_prompt/templates.py` 第 228–229 行；单格 v4 沿用这两条，只替换了格位前缀，见第 292–312 行）。所以变体只针对 A，不重复已有的机位规则。

**seed：** 两臂共用 `ManualH3Input.seed` 显式给定的同一对 seed。
- S1 = 798160553724158，S2 = 798160553724159（沿用 2026-11-03 A/B 的取值）。
- 同组的提交放在同一节点上串行执行。

**调用预算：**

| Provider | 名义 | 硬上限 | 说明 |
| --- | ---: | ---: | --- |
| DeepSeek 编译 | 8 | 16 | 2 组 × 2 臂 × 2 seed；每次允许 1 次修复 |
| H3 视频 | 8 | 8 | 2 组 × 2 臂 × 2 seed |

失败和 UNKNOWN 都计入额度，不自动重提。

**测量（每条视频）：**
- 隔帧停顿占比（`held_frames.py`）；
- HF；
- 每 0.5 秒抽帧目视：身份、多余人物、文字、分屏；
- 编译后提示词的 SHA-256、seed、节点。

**判定（预先写定）：** 两组、两个 seed 共 4 对比较里，至少 3 对变体的隔帧停顿占比低于基线，并且没有新增画面缺陷，才把「变体设为默认」提交用户决定。否则如实报告为「无效或不确定」。

**代码前提：** 变体策略需要一个新的、非默认的提示词策略值，供手动 H3 输入选用。这部分在用户授权调用次数之后再实现，避免向生产代码加入暂时用不到的分支。
