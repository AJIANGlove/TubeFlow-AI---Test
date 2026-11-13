# 第 4 轮：湖岸风向用场景参照物写（M4），3 个 seed（USER_DECISIONS §54）

**状态**：2026-11-12 写定（本文与 `spec.json`），提交后不再改动。依据：`../round3/ANALYSIS.md`。额度：本研究 H3 ≤ 24、DeepSeek ≤ 16，已用 H3 20 次；**本轮用 H3 3 次、DeepSeek 0 次**，之后剩 H3 1 次、DeepSeek 16 次。

## 1. 只改湖岸（第 119 组）第 1 镜

船上（第 116 组）M3 已解决配角不动与岩芯拉长；用户看到的「多一只手」来自分格图本身（第 3 轮分析第 2 节），改提示词去不掉，本轮不再提交 116。

| 文件 | 字符数 | sha256 |
| --- | --- | --- |
| `prompts/g119_m4.txt` | 4686 | `8237492eb94b200bdc138a90672b9063d44588735356a0ddf961be4251d98a10` |

相对 M3 只改第 1 镜的风：

- M3：`a night wind picks up from the left … the coat hem and the lantern flame lean to the right, the branches … sway to the right, … mist … drifts … from left to right, thin clouds drift slowly to the right …`
- M4：`a steady night wind blows in off the lake toward the cliff, and everything moves with it the same way, toward the shore: the coat hem trails back behind the figure and ripples steadily, the lantern flame leans back toward the cliff, the branches at the left edge bend gently away from the lake, a thin layer of mist drifts low across the water toward the shore, thin clouds drift slowly the same way past the moon, and small ripples run toward the shore …`

理由：人物面朝湖，衣摆自然拖在身后（画面左）；M3 让云按文字向右、衣摆按姿态向左，于是相反。M4 不用「左 / 右」，改用湖面、崖边、岸、人物身后这些参照物，并让衣摆「稳定地」起伏而不是乱甩。第 2、3 镜不变（与 M3 相同）。

已在 Claude 云端用生产校验函数通过；部署机 `--dry-run` 为最终校验。

## 2. 提交（3 次 H3）

| # | 臂 | seed | 幂等键 |
| --- | --- | --- | --- |
| 1 | m4 | `20261110`（second，M3 在此 seed 衣摆不一致） | `motion-r4-g119-m4-second` |
| 2 | m4 | `20261111`（third，同上） | `motion-r4-g119-m4-third` |
| 3 | m4 | `source`（M3 在此 seed 用户判自然） | `motion-r4-g119-m4-source` |

## 3. 对比视频（盲看）

6 格（`spec.json` 的 `tile_order`）：M4 × 3 个 seed、M3 × 3 个 seed（第 3 轮已有视频，用户看过，作为参照）。

## 4. 判定（写定，事后不改）

1. **用户目视（最终标准）**：逐格「自然 / 僵 / 怪」。M4 **通过** = 3 格里至少 2 格「自然」，且没有一格因**运动**而「怪」（衣摆、火苗、树、雾、云方向不一致，抖动、变形、闪烁、多出人物）。第 2 镜「切到小湖」属于导演分镜内容、所有格相同（第 3 轮分析第 5 节），**不计入本轮**。
2. **云的方向**（`regions_dx_anchor_px_s`，以人物脚下岩石为参照）：第 1 镜 `env:sky` ≤ −0.3（向崖边，即画面左，与衣摆拖向一致），三个 seed 中至少两个满足。第 3 轮 M3 三条均为 +6.9…+8.4（向右），作为对照。水面、树枝、衣摆的数值只报告（衣摆的问题是朝向，不是幅度，测不出来，以用户目视为准）。第 2 镜有推镜，左缘树木的参照位移受视差影响（R0 也有 −7.6），只报告。
3. **运动量**：第 1 镜 `env_score` ≥ 1.5 px/s（第 3 轮 M3 为 3.0–4.8，R0 为 0.8）。
4. **退化闸**：`held_share` 或 `hf` 超过参考线（0.2 / 0.25）；用户看到抖动、变形、闪烁或多出人物。

**下一步（按结果）**：M4 通过 → 第 2–4 轮的写法合起来形成编译规则（见 `../README.md` 第 4 节），进入「编译器看得到分格图」的真实编译验证；那一步需要把分格图发给能看图的模型（新的数据外发）和新的 H3 额度，都须用户另行授权。M4 不通过 → 按用户描述再改。
