# 抖动分层：策略 × 日期区间、策略 × 画风（只读，2026-11-06T02:00Z）

## 一句话结论

上一轮「11-03 起隔帧停顿占比从 8.4% 升到 47.1%」**主要是策略构成变了，不是时间本身**：`v3`/`v2` 全部落在 11-03 之前，`v4` 两个策略全部落在 11-03 之后，两段几乎不重叠，直接按日期比会把策略差异当成时间差异。按画风再切一层后还能看到：**同一画风下 v4 的占比确实明显更高**（S3 18.8% → 77.3%/58.2%，S4 7.8% → 63.0%/34.4%），但 **S2（暗黑油画）基本不变**（10.8% → 8.8%）。所以「v4 让抖动变多」这个说法要限定画风。

## 口径

- SQL：任务给定的 `select … from group_current_videos c join video_generation_attempts a … join authoritative_narration_groups g … join projects p … where a.updated_at >= '2026-10-01'`，`from/join/where` **逐字未改**；`select` 列表只多了 `g.project_id`、`g.sequence`（用于「DB 有 / survey 无」的归因）。全文见 `stratify_export.sql`。
- 只读：`SET default_transaction_read_only = on` + `BEGIN TRANSACTION READ ONLY` + `SET LOCAL statement_timeout='300s'`；导出用 `COPY … TO STDOUT`，`psql -X -q`，stderr 为空。
- 关联键：`attempt_id`。`survey.jsonl` 的 `held_share` 是唯一测量值来源。
- 日期区间：用 `survey.jsonl.updated_at`（UTC）切 `before 2026-11-03` / `2026-11-03 and later`（`2026-11-03T00:00:00Z` 归后者）。**这是视频尝试的更新时间，不必然等于生成时间**。
- 空值：`held_share = null`（`moving_windows = 0`，静止或过短）**计入 `n_videos`、单独计入 `n_unmeasured`，不进均值、也不当 0**。`error` 行单列（本次 0 条）。
- 样本标记：`n_videos < 10` 记「样本少」；`n_measured < 10 ≤ n_videos` 记「样本少‡」。
- 均值是**逐视频等权**，不按 `moving_windows` 加权。

## 覆盖

| 项 | 值 |
| --- | --- |
| DB 导出（含表头 4400 行） | **4399** 个当前视频 |
| `survey.jsonl` | **4182** |
| 关联成功 | **4182** |
| DB 有、survey 无 | **217**（都是 `v4_single_panel`，且都是在普查之后才成为当前视频的，所以没有测值、也没有 survey 时间戳 ⇒ 日期桶记 `unknown`） |
| survey 有、DB 无 | **0** |
| `held_share` 为空 | **285**（其中 217 是上一条的未测；其余 68 条是 `moving_windows=0`） |
| `error` 行 | **0** |
| 一致性 | 表 1 `ΣALL = 4399`、表 2 `ΣALL = 4399`，与导出相同 |

## 表 1：策略 × 日期区间（节选，完整见 `strategy_date.md`）

| 策略 | 区间 | 视频 | 测到 | 均值 | ≥0.2 | 占比 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `v3` | before | 2406 | 2354 | 0.060 | 208 | 8.8% |
| `v3` | and later | 1 | 1 | 0.000 | 0 | 0.0%（样本少） |
| `v2` | before | 791 | 783 | 0.045 | 54 | 6.9% |
| `v2` | and later | 0 | 0 | — | 0 | — |
| `v4_single_panel` | before | 0 | 0 | — | 0 | — |
| `v4_single_panel` | and later | 437 | 436 | **0.300** | 263 | **60.3%** |
| `v4_natural_motion` | before | 0 | 0 | — | 0 | — |
| `v4_natural_motion` | and later | 546 | 539 | **0.192** | 196 | **36.4%** |
| `v3_single_panel_opt_in_v1` | and later | 1 | 1 | 0.400 | 1 | 100%（样本少） |

**没有一组策略同时跨两个区间**，所以这张表不能用来判断"时间趋势"——它只能说明两段由不同策略构成。

## 表 2：策略 × 画风（节选，完整见 `strategy_style.md`）

画风标签见 `visual_style_legend.csv`（**共 8 种**，我上一轮按前 60 字聚合误看成 6 种）：

| 标签 | 视频 | 画风开头 |
| --- | ---: | --- |
| S1 | 1418 | 欧美二维手绘动画、半写实卡通人物、复古历史绘本背景… |
| S2 | 649 | 暗黑油画插画、明暗对照布光… |
| S3 | 632 | 刻意简单、低细节、粗糙的卡通讲解动画… |
| S4 | 614 | 欧美二维手绘动画…朝鲜王朝历史绘本风格… |
| S5 | 497 | 欧美二维手绘动画…低饱和赭石配色、轻微胶片颗粒… |
| S6 | 364 | 带轻微纸纹理的扁平 2D 卡通讲解插画… |
| S7 | 224 | 欧美二维…复古历史绘本背景（长尾与 S1 不同） |
| S8 | 1 | 暗黑油画插画…（与 S2 的细微差异版） |

同一画风跨策略的对比（只列两边都有 ≥10 条测值的格子）：

| 画风 | `v3`（before） | `v4_single_panel` | `v4_natural_motion` |
| --- | --- | --- | --- |
| S2 | 10.8%（499 测到） | 8.8%（68 测到，样本偏小） | — |
| S3 | 18.8%（330） | **77.3%**（97） | **58.2%**（146） |
| S4 | 7.8%（231） | **63.0%**（27，样本偏小） | **34.4%**（122） |
| S6 | — | 33.3%（33） | 25.2%（258） |
| S7 | — | 73.0%（211） | 30.8%（13，样本少） |

读法：**S3、S4 上 v4 明显更高**（S3 差 3–4 倍）；**S2 上几乎不动**（10.8% → 8.8%，且后者只有 68 条）。S7 只在 v4 出现，无法跨策略比。

## 必须写明的限制

1. `visual_style` 是**项目级**属性，"策略 × 画风"的每个格子实际是**若干项目的聚合**，不是独立同分布样本；不要用它做显著性判断。
2. 表 1 里 `v4_single_panel` 的 217 条 `unknown` 是普查之后新增的当前视频（无测值、无时间戳），它们只影响 `n_videos`，不影响均值与占比。
3. `v3_single_panel_opt_in_v1` 只有 1 条；S8 只有 1 条；S4×v4_single 只有 27 条测值；S2×v4_single 只有 68 条 —— 这些格子的百分比只能当线索。
4. `updated_at` 不一定等于生成时间；跨区间比较时这一点无法消除。
5. 本轮只做分层统计，**没有跑 A/B、没有触发任何重生成**（§51 的 A/B 需要另行授权 8 次 H3 调用）。

## 文件

| 文件 | 内容 |
| --- | --- |
| `stratify_export.sql` | 只读导出 SQL（含唯一一处偏离的说明） |
| `db_export.tsv.gz` | 原始导出（gzip 压缩的 tab 分隔、UTF-8、含表头，4399 行；`stratify.py` 直接支持 `.gz`） |
| `stratify.py` | 本地关联与分层脚本（纯离线，不连库；`joined.csv` 只保留 `style_label`，长原文见 legend） |
| `joined.csv` | 逐 attempt 的关联结果（含 `in_survey` / `date_bucket` / `style_label` / `held_share`） |
| `strategy_date.{csv,md}` | 表 1（含 `unknown` 与 `ALL` 行） |
| `strategy_style.{csv,md}` | 表 2 + 均值透视 |
| `visual_style_legend.csv` | S1–S8 的完整原文与 sha1 前 12 位 |
| `coverage.json` | 行数、匹配数、空值数、一致性自检 |
| `export.err` | psql stderr（空） |
