# 回切相似度阈值离线标定（2026-11-05）

只读标定，用于给 USER_DECISIONS §48 第 4 项的阈值提供基线。**本目录不含阈值选择**：阈值由用户看完样图后决定，决定前开关保持关闭、`TUBEFLOW_DIRECTOR_COMPOSITION_RECUT_MIN_DISTANCE` 不写入。

## 怎么跑的

镜像 `sha256:fa167f1b6432075500a97ace31678ac60d67dfbddcfc14e2ba54cd714e4ae347`（`tubeflow-test:recut-resume-20261105`，含 PR #10），脚本 `../recut_calibration.py`（PR #10 自带，导入产品代码里的 `recut-similarity-v1`）：

```bash
docker cp recut_calibration.py tubeflowai-test-worker-1:/tmp/recut_calibration.py
cd /home/edy/tubeflowai-test
docker compose exec -T -e OUT=/tmp/recut_cal -e LIMIT=600 worker python /tmp/recut_calibration.py
docker compose cp worker:/tmp/recut_cal /home/edy/pr10-regression/recut_cal
```

2026-11-05T10:12:12Z–10:12:48Z，36 秒。只做 SELECT 与 MinIO GET；不写库、不写对象、不调 Provider。

## 口径

- 集合：`storyboard_generations.status='SUCCEEDED'` 且 `input_json.grid_layout_version='grid-v2'` 且有媒资的故事板，`limit 600`（实际 343 张，未截断）。
- 每张用 `crop-grid-v2` 裁格（失败计入 `crop_failures`，本次为 0），再对同组任意两格算 `recut-similarity-v1 = 1 − Pearson(64×36 灰度缩略图)`：0 表示两格灰度图完全一致，1 表示不相关，2 表示完全反相。（2026-11-05 勘误：原文把 0 和 1 的含义写反了。）
- 分类：`different`（构图键不同）、`adjacent_same`（构图键相同且相邻，即连续观察）、`nonadjacent_same`（构图键相同且不相邻，即旧规则拦截、本次要放行的那类）。

## 结果（`summary.json`）

- 选中 343 张，量到 **342** 张（1 张不足 2 镜），裁切失败 **0**。
- 项目：`2c291aba` 107、`23c59183` 79、`fd3bf04d` 77、`247462a4` 53、`72a22f0a` 14、`484746c4` 13。
- `different` **2877 对**：p0 **0.1240**、p0.5 0.3090、p1 0.3536、p2 0.4248、p5 0.5647、p10 0.6606、p25 0.8138、p50 0.9400，均值 0.9164。
- `adjacent_same` **2 对**：0.6947、0.9411，均值 0.8179。
- `nonadjacent_same` **0 对**（旧规则下这类根本没被放行过）。

原始 2877 对距离的分桶（距离 = 1 − Pearson，理论上界 2，实测最大 **1.4452**）：0–0.1 **0**、0.1–0.2 **4**、0.2–0.3 8、0.3–0.4 34、0.4–0.5 46、0.5–0.6 92、0.6–0.7 192、0.7–0.8 287、0.8–0.9 521、0.9–1.0 668、≥1.0 1025。

## 看图的几个事实

- `closest_different_01..06.jpg`：最近 6 对「构图不同」的镜头并排。最近一对 0.1240（`2c291aba` 第 1 与第 6 镜）目视是同一书桌同一本账册的**不同瞬间**（一个在写、一个把手放上去），属于正常连续镜头。
- 也就是说现有语料里，**没有任何一对构图不同的镜头低于 0.1240**；阈值取在这条线以下（例如 0.10）时，今天所有已成功的 v2 故事板都不会命中检查。
- `same_composition_01.jpg`：全部同构图对，只有 2 对，且都是 0.6947 / 0.9411——**不相似**。所以本语料给不出「回切长什么样」的正样本，阈值只能从「不同镜头」这一侧定上限，不能从同构图侧反推。
- 没有 `nonadjacent_same` 样本，说明这类构图在 6 个 v2 项目里没有成功产出过（这正是旧规则拦截的结果），本次放行会第一次让它们出现。

## 需要用户决定

阈值数值（写入 `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT_MIN_DISTANCE` 并记进 USER_DECISIONS §48 第 4 项），以及是否同时打开 `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT`。本轮到此为止：开关仍为 `false`、阈值仍为空。

## 文件

| 文件 | 内容 |
| --- | --- |
| `summary.json` | 脚本原样输出：算法、张数、项目、裁切失败、各分类分位数 |
| `pairs.jsonl` | 每对一行：项目/组/故事板/两格序号/间隔/分类/距离 |
| `closest_different_01..06.jpg` | 最近 36 对「构图不同」并排图（每页 6 对，标注距离） |
| `same_composition_01.jpg` | 全部同构图对（2 对）并排图 |
