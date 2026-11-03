# 第 11 组 v4 人物消失：只读判定（2026-11-03）

范围：attempt `ed495263-3072-482b-98e0-f6d357c7b886`（项目 `31e00d87…`，第 11 组，`tubeflow_storyboard_v4_natural_motion`）。
只读分析，未改代码、未重新生成。

## 1. 提示词来源

`video_prompt_compilations` 中该 attempt 的行数为 **0**：该表只在编译尝试/失败时写入诊断，成功编译不落行。
因此以 `video_generation_attempts.input_snapshot_json->'video_prompt'->>'text'` 为准——这是提交给 H3 的同一份编译全文（快照 SHA-256 `7509926b0d5ed64f…`）。

## 2. 0–3 s 对应文本

该 attempt 只有 **1 个镜头块**（`[Shot 1]`，时长约 15 s，静态机位）：

> [Shot 1] Panel 1 (row 1, column 1) opens on a medium objective view inside the tanghuru shop, where (S1) stands behind a wooden counter with trays of skewered fruit, a small cash register, and stacked paper receipts; a laminated franchise binder and a clear acrylic price stand sit on the counter as non-readable physical props. **Static camera, 0% movement.** (S1) opens the franchise binder, traces one finger down a page, then shifts the binder next to a small stack of receipts; he lifts the acrylic price stand, sets it beside the binder, exhales through his nose, and gives a small, tired nod toward the counter layout.

收尾一致性句：`Maintain the identity of (S1) exactly as in character reference 1, one person only, the same clothing in every shot …`；负规则含 `No … on-screen text or numbers …`。

## 3. 关键词核查

对全文检索 `enter/enters/entering`、`appear/appears`、`fade/fading`、`dissolve`、`empty/vacant/no one`、`disappear`、`transparent/ghost`、`materialize`、`walk in`、`off-screen`、`out of frame`：
除负规则里“不得把故事板空格画进画面”外，**没有任何入场、出现、淡入淡出、空镜或离画描述**。Shot 1 一开场就写明人物站在柜台后，且机位静止。

## 4. 画面复核

0–4 s 每 0.5 s 抽帧（`evidence/AB_g11v4_first4s.png`）：

| 时间 | 观察 |
| --- | --- |
| 0.0 s | 人物完整，低头看册子 |
| 0.5 s | 人物半透明，柜台与册子仍在 |
| 1.0 s、1.5 s | 人物消失，只剩柜台、册子、收据 |
| 2.0 s | 人物以淡影重新出现 |
| 2.5 s、3.0 s、3.5 s | 人物恢复完整并继续动作 |

背景、道具与机位全程不变。

## 5. 结论

**H3 生成伪影，不是提示词驱动。** 证据：提示词明确写了一个静止机位、人物全程在柜台后的连续动作，没有任何“进入/淡出/空镜”指令；画面里消失与恢复只发生在人物身上，场景与道具完全稳定，符合扩散视频模型的角色保持丢失（identity dropout），而非文本要求。

尚未确定触发条件：只有 1 个样本、1 个 seed、1 个节点，无法区分是 seed 运气、镜头时长过长（15 s 单镜）、还是 v4 的“静态机位 + 主体动作驱动”写法放大了该风险。

## 6. 建议（未实施）

1. 人物在场 QC：按 0.5 s 抽帧统计人脸数量，出现 `1→0→1` 判定为人物消失，进入既有内容 QC 梯子。
2. 下一轮 A/B 把「人物消失」列为独立指标，并覆盖多 seed、多节点；本轮不作为结论。
