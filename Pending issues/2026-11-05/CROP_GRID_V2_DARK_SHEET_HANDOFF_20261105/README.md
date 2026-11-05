# 交接：crop-grid-v2 误拒暗调画风故事板 → 247462a4 AUTO 停线（2026-11-05）

状态：**只读复核，未改代码、未改业务数据、未恢复项目**。本文供接手会话直接定位数据与代码；修复方案需另行实施并经用户确认（见 §6）。
对象：项目 `247462a4-e719-4488-ae14-aab962de1a27`「You're the Explorer Who Found Machu Picchu」，频道 `c77ea4a2-0058-4fab-839b-1b92172f35bd`「Past Unknown 历史悬疑史」。
前序诊断：`Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/README.md`（另一会话，结论方向正确，机理与部分数字有误，勘误见 §5）。
本文所有路径相对仓库根目录；时间均为 +0800，除非标注 Z。

---

## 1. 结论

1. **直接原因**：故事板阶段逐格裁切演练 `crop-grid-v2` 对该项目几乎所有故事板报 `shot_panel_gutter_not_found`，每组 14 次故障转移预算耗尽后，AUTO 监督器按设计停线
   （`auto_pipeline/service.py:332-342`，reason `storyboard_failed:storyboard_panel_layout_unreadable`，2026-11-05 12:55:19）。
2. **根本原因**：分隔带判定使用**绝对亮度差阈值** `_V2_DELTA = 40`（0–255 灰度）。对黑色分隔带，它要求分隔带两侧约 65 px 以内的画面平均亮度 ≥ 约 40。
   该项目同一提示词里有两条要求让这一点无法满足：
   - v2 版式提示词要求**纯黑分隔、画面紧贴分隔、不得画白框**（`application/storyboard_prompt.py:96-99`）；
   - 频道画风为暗黑油画、**「边缘渐隐」**（记录于 `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/DIAG.md` §1），以 `Art style for every panel: {style}` 写入每格（`storyboard_prompt.py:116`）。
   结果：**严格按提示词画出的故事板**，分隔带 ±65 px 内画面（剔除分隔带）中位数只有 18.5–31.1 灰度，最理想对比度 11.9–30.1，全部低于 40；
   失败点统一在**第一条行分隔（y 轴）**，严格搜索与 ±25% 放宽搜索都找不到候选，x 轴根本没被执行。
3. **同一根因的第二条路径**：只有一行出镜的版式（2 镜 2×2）不走行分隔，而走「暗尾」规则 `_V2_DARK_MAX = 64`（同为绝对阈值）。
   暗调画面整行均值 ≤ 64，暗尾从图底一直延伸到第 0 行，同样报 `shot_panel_gutter_not_found`。
4. **能通过的样本恰恰违反了提示词**：2 张通过样本是白页边 + 白缝 + 黑描边（提示词明令禁止）。它们靠**白缝**（≈254）被标记；
   其黑描边本身的对比度（79ec5d16 为 27.6）同样达不到 40。也就是说，本项目里检查器**放行违规图、拒收合规图**。
5. **重试无法自愈**：每次重提使用同一提示词与画风，7 个模型产出的亮度分布相同，故障转移链按确定性方式打光；
   成本见 `CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/captured/db_stats.txt`（2566 次生成 / 13 成功 / 2385 次该码）。

---

## 2. 引入问题的变更（其他会话的更新）

引入问题的两笔代码提交 `a969e13`、`571e122`（以及表末与本问题无关的 `1ec07ca`）均带 `Claude-Session: https://claude.ai/code/session_01Dxiw2z96YGytDXAL1j5pqM`，来自同一会话；合并、部署与开关操作由提交者 MYSF 完成。

| 时间 (+0800) | 提交 / 事件 | 引入的内容（与本问题相关部分） | 记录位置 |
| --- | --- | --- | --- |
| 11-04 13:29 | `a969e13`「Default new projects to one cropped panel per shot (§46)」 | ① `_V2_DELTA = 40.0` 绝对阈值、`_v2_extreme`（均值 vs 邻域中位数）；② `_v2_dark_tail` 与 64 暗尾阈值（当时名 `_V2_INTERIOR_DARK_MAX`）；③ v2 提示词「straight solid black gutters about 12 pixels wide… Do not draw white frames…」；④ 故事板阶段裁切演练，失败进入故障转移链（`storyboards.py:_check_panel_layout`） | `git show a969e13`；`Pending issues/2026-11-04/SINGLE_PANEL_DEFAULT_IMPLEMENTATION_20261104.md` |
| 11-04 15:25 | `3eac2ba` 合并 PR #4 | — | — |
| 11-04 16:27 | `571e122`「Revise crop-grid-v2 after Gate 1 (§47)」 | ① 放宽搜索 `_v2_wide_separator`：**只过滤 `_v2_extreme` 已标记的行**（`panel_images.py:580`），不能产生新候选；② 两侧对照判定 `_v2_is_gutter_band` 同样用 Δ≥40；③ 像素常量按图幅放大（邻域 40→65）；④ 常量改名 `_V2_DARK_MAX` | `git show 571e122`；`Pending issues/2026-11-04/CROP_GRID_V2_GATE1_REVIEW_20261104.md` |
| 11-04 17:27 | `1707d85` 合并 PR #5 | — | — |
| 11-04 17:37 | `588cb2f` Gate 1 重跑 98.36%（539/548） | 标定样本为 6 个**其他项目**的既有 **v1 提示词**故事板（以 `layout_version="grid-v1"` 跑 v2 裁切），不含 v2 提示词产物、不含 247462a4；这 6 个项目所属频道与画风本文未核对 | `Pending issues/2026-11-04/singlepanel_test/gate1_rerun/REPORT.md` §1–§3 |
| 11-04 17:45 | 部署镜像 `sha256:238bd9ea…`（由 `1707d85` 构建），开关 false | — | `docs/DEPLOYMENT.md` §36；`29a2fa8` |
| **11-04 21:41:55** | **开关开启** `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true`（镜像不变） | 此后新建项目首张故事板钉 `grid-v2` | `docs/DEPLOYMENT.md` §37；`Pending issues/2026-11-04/DEFERRED_EXISTING_PROJECTS_20261104.md` §3.1 |
| 11-04 22:23:48 | 247462a4 创建 | 全部故事板为 `grid-v2` | 前序诊断 README §1、§4 |
| 11-05 08:30 | 进入故事板阶段，首批拒收 | — | `captured/db_stats.txt` §2–§3 |
| 11-05 10:41 | 部署 `sha256:edee542d…`（由 `01ccf01` 构建，PR #6 旁白修复） | 裁切与提示词代码与 `238bd9ea` **逐字相同**（`git diff 1707d85 01ccf01` 对 `panel_images.py`、`storyboard_prompt.py`、`storyboards.py` 为空） | `docs/DEPLOYMENT.md` §38；`Pending issues/2026-11-04/singlepanel_test/gate3_5/BUILD_PROVENANCE.md` |
| 11-05 12:55:19 | AUTO 暂停 | — | `captured/db_stats.txt` §1 |
| 11-05 12:20 / 13:34 | `1ec07ca` 提交 / `c7baf5b` 合并 PR #7（v2 每格单一镜头规则） | **与本问题无关**：晚于失败起点，且仓库内无部署记录（`DEPLOYMENT.md` 最后一节为 §38） | `git show 1ec07ca` |

为什么没在上线前发现：
- 单元测试夹具画面为中间调（`backend/tests/unit/test_panel_images_v2.py:29-50` `_panel`，各通道 90–200），没有「暗画面紧贴黑分隔」的用例；
- Gate 1 复核已把「分隔存在但未识别（浅色细线、**紧邻深色画面**、无缝拼接）」5 张列为一类，并判断「部分可由放宽搜索覆盖」
  （`CROP_GRID_V2_GATE1_REVIEW_20261104.md` §2 表最后第二行）。按 `panel_images.py:580` 的实现，放宽搜索不可能覆盖这类图；
- 对照：同批上线后的测试项目 `72a22f0a`（14 组）该码 0 次、`2c291aba`（107 组）该码 2 次（`Pending issues/2026-11-04/singlepanel_test/gate3_5/REPORT_2.md` 第 56、67 行），说明问题集中在暗调画风，不是 v2 全面失效。

注：Δ≥40、邻域、「按暗尾起点定边界」与「约 12 px 纯黑分隔、无白框」写在用户已确认的 `docs/planning/USER_DECISIONS.md` §46 第 3–4 项与 §47 第 2 项；暗尾阈值 64 本身未写入决定，是 `a969e13` 的实现常量。代码忠实实现了这些决定，缺陷是该规则对暗调画风的盲区未被测试覆盖。

---

## 3. 机理与实测

### 3.1 判定链路（`backend/src/tubeflow_test/` 下，HEAD = 部署内容）

```
application/storyboards.py:1180   StoryboardGenerationService._check_panel_layout
└ video_generation/panel_images.py:667   check_storyboard_panel_layout
  └ :392  grid_v2_panel_boxes      先定行：:416 row_spans = _v2_spans(column_strip, "y", …)
    └ :430  _v2_spans
      ├ :545  _v2_separator        严格容差；候选来自 :558 _v2_runs(_v2_extreme(…))
      ├ :565  _v2_wide_separator   ±25% 单格；候选同样来自 :580 _v2_runs(_v2_extreme(…))，再经 :620 _v2_is_gutter_band 过滤
      └ :602  _v2_dark_tail        未用尽格子时的暗尾；:613 means ≤ _V2_DARK_MAX(64)
          └ :477  _v2_extreme      :485 |行均值 − median(±65 行)| ≥ _V2_DELTA(40)
常量：panel_images.py:27 _V2_DELTA=40.0、:41 _V2_DARK_MAX=64.0、:26 _V2_REFERENCE_SIZE=(1672,941)
暂停：auto_pipeline/service.py:332 facts.storyboard_terminal_failures() → :334 _pause
预算：domain/storyboards.py:76 STORYBOARD_AUTO_MAX_SUBMISSIONS=14；按 failover_chain_id 计（application/storyboards.py:1252 _auto_resubmit、:1281）
```

### 3.2 12 张失败原图实测（本文脚本 `verify_row_gutter_contrast.py`，业务同款条带与常量）

「最理想对比度」= 邻域 ±65 行中**剔除分隔带后**的中位数 − 分隔带均值，已经排除了「分隔带把中位数拉低」的影响。

| 原图 | 镜数 | 分隔带行（宽） | 带内均值 | 剔除后中位数 | 最理想对比度 | 上侧 / 下侧 7–26 px | 被标记行数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| e459d954_6panels_25931.jpg | 6 | 755–780（26） | 0.6 | 18.5 | **17.9** | 13.4 / 31.7 | 0 |
| ce321800_4panels_30023.jpg | 4 | 756–779（24） | 1.2 | 19.8 | **18.7** | 15.2 / 28.9 | 0 |
| 1a04e778_4panels_25915.jpg | 4 | 748–788（41） | **17.4**（深灰） | 29.3 | **11.9** | 27.6 / 29.2 | 0 |
| 313b41c6_5panels_25923.jpg | 5 | 749–786（38） | 0.7 | 26.5 | **25.8** | 26.6 / 22.4 | 0 |
| 0a476862-….png | 6 | 756–779（24） | 1.0 | 22.6 | **21.5** | 21.3 / 32.2 | 0 |
| 1b9389dc-….png | 6 | 755–779（25） | 1.0 | 31.1 | **30.1** | 24.1 / 48.4 | 0 |
| 42fb7318-….png | 3 | 749–786（38） | 0.6 | 22.6 | **22.0** | 14.9 / 49.0 | 0 |
| 900f552a-….png | 5 | 752–783（32） | 0.5 | 20.7 | **20.2** | 17.8 / 28.6 | 0 |
| a51485e7-….png | 6 | 756–779（24） | 0.7 | 22.7 | **22.0** | 18.8 / 58.5 | 0 |
| c2dba5e1-….png | 5 | 749–785（37） | 0.6 | 23.0 | **22.4** | 20.4 / 47.7 | 0 |
| fe2a6e19-….png | 4 | 758–777（20） | 0.9 | 30.6 | **29.7** | 23.2 / 53.9 | 0 |
| 49625d3e-….png | 2 | 无行分隔（只有一行出镜） | — | — | 暗尾从第 0 行开始，偏离期望 −768（允许 ±83） | — | — |
| *79ec5d16_4panels.png（通过）* | 4 | 黑描边 788–796（9） | 5.5 | 33.1 | 27.6 | **254.3** / 33.1 | 43（白缝） |
| *cc1ced3d_4panels.png（通过）* | 4 | 黑描边 789–801（13） | 7.9 | 64.7 | 56.8 | **254.4** / 65.0 | 69（白缝） |

要点：
- 11/11 张可量的失败图对比度 11.9–30.1，阈值 40；整图中位亮度 34–58（前序 `captured/profile.txt`），暗的是分隔带两侧。
- 中位数被分隔带拉低的幅度只有 0.5–7.6 级，去掉它也到不了 40。
- 上侧（上排格子的下缘）均值 13.4–27.6，11 张全部低于放宽搜索「两侧都比分隔亮 ≥40」所需的约 41；其中 10 张上侧比下侧更暗，与「边缘渐隐」画风一致。直接对真实分隔带调用 `_v2_is_gutter_band`，11 张全部返回 False。
- 分隔带宽 20–41 px（提示词要求约 12 px），在 `framed_max`=78 之内，**不是**失败原因。
- 「边缘渐隐」是因果解释：实测与之吻合，但未做去掉该子句的对照生成（§7）。

---

## 4. 数据位置清单

### 4.1 仓库内（已提交）

| 内容 | 路径 |
| --- | --- |
| 4 张失败整图原字节（2752×1536 JPEG，sha256 = 库内 `expected_sha256`） | `Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/evidence/failed/` |
| 2 张通过整图（PNG）、6 张缩图、校验和 | 同上 `evidence/passed/`、`evidence/previews/`、`evidence/manifest.sha256` |
| 8 张失败原图（文件名即 generation_id） | `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/originals/` |
| 24 条裁切追踪（全部 `y wide` 失败） | `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/trace.jsonl` |
| 53 组逐条故事板记录（含 `failover_total_attempt`、`failure_code`、`provider`、`created_at`，可直接看预算链重启） | `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/groups.json` |
| 每组拒收次数 | 同目录 `checks.json` → `layout_unreadable_by_group` |
| 项目画风、频道、组数 | 同目录 `DIAG.md` §1 |
| 库内统计原文（10 段） | `Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/captured/db_stats.txt`；SQL 在同目录 `sql/collect.sql` |
| 容器内复现、worker 日志汇总 | 同目录 `captured/container_repro.txt`、`captured/worker_log_reason.txt` |
| Gate 1 标定样本与结果（548 条、MinIO `object_key`、sha256） | `Pending issues/2026-11-04/singlepanel_test/gate1_rerun/baseline.json`、`gate1_results.jsonl`、`REPORT.md`、`diag/` |
| Gate 1 首轮失败分类 | `Pending issues/2026-11-04/CROP_GRID_V2_GATE1_REVIEW_20261104.md` §2 |
| 已确认决策 | `docs/planning/USER_DECISIONS.md` §46（第 592 行起）、§47（第 612 行起） |
| 部署记录 | `docs/DEPLOYMENT.md` §36（408 行）、§37（420 行，含回滚方法）、§38（429 行） |
| 开关开启前后快照 | `Pending issues/2026-11-04/DEFERRED_EXISTING_PROJECTS_20261104.md` §3.1、§3.4 |
| 镜像构建溯源 | `Pending issues/2026-11-04/singlepanel_test/gate3_5/BUILD_PROVENANCE.md` |
| 对照项目（72a22f0a、2c291aba） | `Pending issues/2026-11-04/singlepanel_test/gate3_5/REPORT_2.md` |
| 单元测试 | `backend/tests/unit/test_panel_images_v2.py`（夹具 29–50 行）、`backend/tests/unit/test_grid_layout_v2.py` |
| 本文核验脚本 | `Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_HANDOFF_20261105/verify_row_gutter_contrast.py` |

### 4.2 部署机

| 内容 | 位置 |
| --- | --- |
| 主机与目录 | `edy@192.168.110.200`，`/home/edy/tubeflowai-test` |
| 开关 | 该目录 `.env` → `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true`；开启前备份 `.env.bak-single-panel-on-20261104` |
| 容器 | `tubeflowai-test-worker-1`（裁切演练在此执行）、`tubeflowai-test-web-1`、`tubeflowai-test-postgres-1`（库 `tubeflow`，用户 `tubeflow`）、`tubeflowai-test-minio-1` |
| 部署的业务代码 | 容器内 `/usr/local/lib/python3.12/site-packages/tubeflow_test/video_generation/panel_images.py` |
| worker 日志 | `docker logs tubeflowai-test-worker-1`，关键字 `storyboard_panel_layout_rejected`、`auto pipeline paused`；`json-file` 无持久化，11-05 10:41 之前的日志随容器重建丢失 |

### 4.3 数据库（`tubeflowai-test-postgres-1` / `tubeflow`）

| 表 | 关键字段 |
| --- | --- |
| `auto_pipeline_runs` | `state`、`reason_code`、`authorization_round`、`payload->>'phase'`、`payload->>'paused_at_ms'`、`payload->>'failure_code'`、`payload->'group_sequences'` |
| `storyboard_generations` | `status`、`kind`、`provider`、`asset_id`、`failover_chain_id`、`failover_route_index`、`failover_total_attempt`、`input_json->>'prompt'`（实际提示词）、`input_json->>'model'`、`input_json->'shots'`、`input_json->>'grid_layout_version'`、`checkpoint_json->>'failure_code'` / `'failure_stage'` / `'expected_sha256'` / `'download_token'` / `'qc_width'` / `'qc_height'` / `'qc_mime'` |
| `projects` | `visual_style`、`snapshot_visual_style`（模型字段见 `adapters/persistence/identity_project/models.py:272`、`:311`） |
| `channels` | `default_visual_style`（同文件 `:150`） |

补充查询（**本次未在部署机执行**，字段名按仓库模型与 `collect.sql` 已用写法核对）：

```sql
\set pid '247462a4-e719-4488-ae14-aab962de1a27'
-- A. 画风原文（确认是否含「边缘渐隐」）
select visual_style, snapshot_visual_style from projects where id = :'pid';
select default_visual_style from channels where id = 'c77ea4a2-0058-4fab-839b-1b92172f35bd';
-- B. 失败故事板实际发出的提示词（e459d954、ce321800 两条，generation_id 取自 worker 日志）
select id, input_json->>'grid_layout_version' as layout, input_json->>'prompt' as prompt
from storyboard_generations
where id in ('82f7a53d-8b59-4d76-b6bf-6f7ffd898564', '37562a3e-db55-4ef8-a36b-b4a1cc8a72af');
-- C. 13 张成功故事板（核实是否全部白缝；asset_id 可在 MinIO 取原图）
select group_id, asset_id, provider, input_json->>'model' as model, created_at
from storyboard_generations where project_id = :'pid' and status = 'SUCCEEDED' order by created_at;
-- D. 全部分组的预算链
select group_id, failover_chain_id, count(*), min(created_at), max(created_at), max(failover_total_attempt)
from storyboard_generations where project_id = :'pid' group by 1, 2 order by 1, 4;
```

---

## 5. 对前序诊断 README 的勘误

| 位置 | 原文 | 实际 |
| --- | --- | --- |
| §2.2 机理 | 「±65 px 邻域中位数被分隔带拉到 21–46，黑线 − 中位数只有 15–35」 | 剔除分隔带后中位数仍只有 18.5–31.1，对比度 11.9–30.1。中位数被拉低 0.5–7.6 级，**不是主因**；主因是分隔带两侧画面本身暗。21–46、20–55、15–35 三组数在任何 `captured/` 输出里都没有出处 |
| §2.2 | 「这批图的整幅动态范围本身就低于该阈值」 | 不成立：整图中位亮度 34–58，灯光接近纯白；暗的是分隔带两侧 |
| §0 第 2 条、§2.2 | 「26–56 px 纯黑分隔带」 | 纯黑核心 20–38 px；1a04e778 为深灰（均值 17.4），其自身 §3.3 表也写了带内最暗 16.7 |
| §3.3 表 | 列出 x 轴分隔偏差 | 失败点全部在第一条**行分隔**，x 轴从未执行；`profile` 模式用整图均值，不是业务条带（313b41c6 的 y 值因此偏低：16.1，业务条带为 24.2） |
| §9 方向 A | 「改为同时暗于两侧」 | 放宽搜索 `_v2_is_gutter_band` 已经是「同时暗于两侧 ≥40」，照样失败；单做 A 无效 |
| §0 第 3 条、§3.3 | 「同项目 13 张通过的故事板是白页边/白分隔」 | 只有 2 张样本可证；且这 2 张违反了 v2 提示词，前序文档未指出 |
| §3.2 表 | `cc1ced3d` 属组 `6aa58bcf`、12:31:37 | `collect.sql` §8 是 `limit 3`，只覆盖最新 3 张（12:32:30 及之后），该条溯源**未被采集输出覆盖**（不是矛盾，是缺证） |
| §3.8 表 | 列名「尝试」 | 实为 FAILED 计数（合计 2553），不含成功 |
| §2.2 | 未提及 | 遗漏 2 镜版式的第二条失败路径（`_V2_DARK_MAX=64` 暗尾） |

---

## 6. 修复约束（给接手会话）

1. **需要用户决定**：Δ≥40、邻域、暗尾定边界与「纯黑分隔、无白框」是 `USER_DECISIONS.md` §46 第 3–4 项、§47 第 2 项的已确认内容；按 `AGENTS.md`「规则优先级」，改判据、改提示词或改画风处理前须先向用户集中询问。暗尾阈值 64 未写入决定，但改它同样改变 §46 第 4 项的暗尾行为，建议在同一次询问中说明。
2. **两处绝对阈值要一起看**：`_V2_DELTA`（`_v2_extreme`、`_v2_is_gutter_band`）与 `_V2_DARK_MAX`（`_v2_dark_tail`）。只改前者，2 镜版式仍会失败。
3. **候选方向（均未实施、未验证）**：
   - 相对对比或仅对检测副本做亮度拉伸，裁切坐标仍用原图；
   - 提示词改为浅色分隔；
   - 网格故事板不传「边缘渐隐」类子句。
   前序文档的方向 A 单独无效（见 §5）。
4. **回归基线（建议）**：
   - 本文 12 张失败图须转为可裁切，且目视切在真实分隔上；2 张通过图须保持通过；
   - `gate1_rerun/baseline.json` 548 条（经 MinIO `object_key` 取原图）不得回归；
   - fa3a0b30 与 `test_gate1_originals` 用例不变；
   - `shot_panel_gutter_ambiguous` / `shot_panel_crop_unsafe` 判据不得放松；
   - 新增「暗画面紧贴黑分隔」单元测试夹具。
5. **项目本身**：247462a4 仍 PAUSED。未修复前恢复会再次按每组 14 次打光预算，恢复与否请用户决定。

---

## 7. 未核实 / 待确认

1. 库内画风原文、实际发出的提示词：用 §4.3 查询 A、B 确认。本文对「边缘渐隐」的引用来自 `DIAG.md` §1，未看到库内原文。
2. 「边缘渐隐」与暗边缘的因果：数据吻合，但未做去掉该子句的对照生成。对照生成需调用 Provider，须按 `docs/planning/AGENT_AUTHORIZATION.md` 取得授权。
3. 13 张成功故事板是否都是白缝：只验证了 2 张，用查询 C 加 MinIO 原图核实。
4. 恢复是否重置 14 次预算：代码按 `failover_chain_id` 计数；`groups.json` 中第 1 组可见 3 条链：
   - 00:30Z–01:16Z，14 次；
   - 01:47Z–02:41Z，14 次；
   - 02:54Z 起，第 8 次成功。
   新链由谁、在哪条代码路径开启，本文未追。
5. 库内计数（2566 / 2385 / 53 组）、容器内复现、worker 日志：引用自前序诊断的采集输出，本次无部署机访问权限，未重新执行。

---

## 8. 复现

```bash
# 仓库根目录；需 Python 3.12+ 与 Pillow（部署镜像为 12.3.0，本文用同版本）
H="Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_HANDOFF_20261105"
E="Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/evidence"
O="Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/originals"
python -I "$H/verify_row_gutter_contrast.py" \
  "$E/failed/e459d954_6panels_25931.jpg:6" "$E/failed/ce321800_4panels_30023.jpg:4" \
  "$E/failed/1a04e778_4panels_25915.jpg:4" "$E/failed/313b41c6_5panels_25923.jpg:5" \
  "$O/0a476862-93e4-422a-a52a-bd29296a2a99.png:6" "$O/1b9389dc-c305-4b61-922a-7e3f049ddd8e.png:6" \
  "$O/42fb7318-6a65-4b11-8c11-fffe7b23529e.png:3" "$O/49625d3e-0e5b-49fb-9361-3f85e9a5d594.png:2" \
  "$O/900f552a-f75d-4643-b92d-3bbfa443dbd3.png:5" "$O/a51485e7-4426-4ce2-8dc6-d6512c7a1bf5.png:6" \
  "$O/c2dba5e1-df66-49dc-a6bf-6b8f54e44930.png:5" "$O/fe2a6e19-6ffe-4395-9081-fb76603350c8.png:4" \
  "$E/passed/79ec5d16_4panels.png:4" "$E/passed/cc1ced3d_4panels.png:4"
# 预期：12 张 REJECT shot_panel_gutter_not_found、2 张 PASS；数字与 §3.2 表一致
```

镜数取自 `groups.json` 中对应 generation 的 `shots`。脚本只读，不写文件。
