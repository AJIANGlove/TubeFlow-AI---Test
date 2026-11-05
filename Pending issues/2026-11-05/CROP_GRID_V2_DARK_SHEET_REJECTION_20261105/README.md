# crop-grid-v2 在极暗故事板上判「找不到分隔带」→ 故事板耗尽 → AUTO 暂停（2026-11-05）

状态：**只读诊断，未修改代码、未改业务数据、未恢复/重跑该项目**。
对象：`247462a4-e719-4488-ae14-aab962de1a27`「You're the Explorer Who Found Machu Picchu」（频道「Past Unknown 历史悬疑史」）。

---

## 0. 摘要

1. 项目 `247462a4` 的 AUTO 于 **2026-11-05 12:55:19 +0800** 自动暂停，原因码
   `storyboard_failed:storyboard_panel_layout_unreadable`。暂停是**设计行为**：故事板阶段只要有一组在当前授权轮内
   转为终态 FAILED，监督器就停线（`auto_pipeline/service.py:332` → `_pause`）。
2. 触发失败的是逐格裁切演练 `crop-grid-v2`：它报 `shot_panel_gutter_not_found`（找不到分隔带），
   **但分隔带客观存在**——4 张最新失败整图都是 2752×1536、格子等大、26–56 px 纯黑分隔带。
3. 真正的原因是该判定采用**绝对阈值**「列/行亮度均值与 ±65 px 邻域中位数相差 ≥ 40」（`_V2_DELTA=40`）。
   这批图是极暗夜景（整图列中位亮度只有 **34–58 / 255**），黑分隔带与黑画面之间达不到 40 级；
   实测 4 张样本每条分隔带的最大偏差只有 **3.8–28.9**，一条线都标不出来。
   同项目 13 张**通过**的故事板是白页边/白分隔（偏差 **196–227**），所以能过。
4. 代价：故事板阶段 4.4 小时内产生 **2566 次**生成（**13 成功 / 2553 失败**，其中 **2385** 条是该码），
   **53 组里 40 组零成功并把 7 个模型的 14 次预算全部打光**（`failover_total_attempt=14`，route 0→6）。
   库内可见 **4 条独立的 14 次预算链**（预算是按授权轮计的，链断开即重启一轮），
   当前容器日志另见 3 次暂停记录（1 次 quota、2 次 layout）⇒ 预算被重置 3 次（通常由「恢复」触发）；
   每次恢复重置预算，故单组累计达 55 次提交。
5. 已用**部署机容器内镜像自带代码 + PIL 12.3.0** 对同一批图目录复现：4/4 同样被拒，错误码一致；
   样本文件 sha256 与库内 `checkpoint_json->>'expected_sha256'` **逐字节一致**。

---

## 1. 环境

| 项 | 值 |
| --- | --- |
| 部署机 | `edy@192.168.110.200`，`/home/edy/tubeflowai-test` |
| 镜像 | `sha256:edee542dda35…`（`narration-jobid-20261104`） |
| 容器 | `tubeflowai-test-worker-1`、`tubeflowai-test-web-1`（web），均 **2026-11-05T02:41:39Z** 启动（10:41:39 +0800） |
| 数据库 | `tubeflowai-test-postgres-1` / `tubeflow` |
| 关键开关 | `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true`（2026-11-04 21:41:55 +0800 打开，见 `DEFERRED_EXISTING_PROJECTS_20261104.md` §3.1） |
| 项目创建 | **2026-11-04 22:23:48 +0800**（14:23:48Z），晚于开关开启 → 全部故事板钉 `grid-v2` |
| 代码基线 | 容器内 `site-packages/tubeflow_test/video_generation/panel_images.py` 679 行，与仓库同名文件 `diff` 无差异 |

---

## 2. 结论

### 2.1 直接原因：设计上的暂停闸门

- `auto_pipeline_runs`：`state=PAUSED`，`reason_code=storyboard_failed:storyboard_panel_layout_unreadable`，
  `authorization_round=7`，`payload.phase=storyboard`，`payload.group_sequences=[5]`，
  `payload.paused_at_ms → 2026-11-05 04:55:19Z`（12:55:19 +0800）。
- 该 reason 只由 `AutoPipelineService._pause()`（`auto_pipeline/service.py:332-342`）写入，
  判定条件是 `facts.storyboard_terminal_failures()` 非空——**自动触发，不是人工暂停，也与重启无关**
  （容器 10:41:39 +0800 启动，暂停在 12:55:19，之前无重启）。

### 2.2 根本原因：绝对对比度阈值在极暗画面上失效

判定链路（仓库代码，与容器内一致）：

```
StoryboardGenerationService._check_panel_layout        application/storyboards.py:1180
  └─ check_storyboard_panel_layout                     video_generation/panel_images.py:667
      └─ derive_shot_panels → grid_v2_panel_boxes      panel_images.py:392
          └─ _v2_spans → _v2_separator / _v2_wide_separator   panel_images.py:430 / 545 / 565
              └─ _v2_extreme: |列/行均值 − ±neighbourhood 中位数| ≥ _V2_DELTA   panel_images.py:477
```

关键常量（`panel_images.py:27-44`，按 1672×941 标定、随图幅放大）：

| 常量 | 基准值 | 2752×1536 实际值（factor≈1.632） |
| --- | --- | --- |
| `_V2_DELTA` | **40.0（绝对，不缩放）** | **40.0** |
| `_V2_NEIGHBOURHOOD` | 40 | **65**（邻域窗口 ±65 px） |
| `_V2_BAND_MAX` | 32 | 52 |
| `_V2_FRAMED_MAX` | 48 | 78 |
| `_V2_RADIUS_MIN` | 16 | 26（另有 4% 单格，实际 26–56） |

失效机理：分隔带是**纯黑**（带内最暗 0.2–11），但两侧格子本身也很暗（列均值 20–55）。
`_v2_extreme` 取 ±65 px 邻域**中位数**作基准，该窗口同时覆盖黑分隔带与较暗的一侧画面，
中位数被拉到 21–46，于是「黑线 − 中位数」只有 15–35，**永远达不到 40** → 没有任何一条线被标记 →
`_v2_runs` 返回空 → `_v2_separator` 与 `_v2_wide_separator` 都返回 `None` →
`shot_panel_gutter_not_found`。

换句话说：**算法把「黑线相对于局部中位数的绝对落差」当作分隔证据，而这批图的整幅动态范围本身就低于该阈值。**

---

## 3. 证据

### 3.1 规模与失败码（`captured/db_stats.txt` §2/§3/§4）

| 指标 | 值 |
| --- | --- |
| 故事板生成总数 | **2566**（全部 `GRID_SHEET`，全部 `grid-v2`） |
| SUCCEEDED | **13**（2026-11-05 09:06–12:43 +0800） |
| FAILED | **2553** |
| 其中 `storyboard_panel_layout_unreadable` | **2385**（占失败 93.4%） |
| `aspect_ratio_not_supported` | 68 |
| `provider_quota_exhausted` | 44 |
| `generate image failed` | 24 |
| `ProviderReconcileUnsupportedError` | 10 |
| 内容审核（中英文两串） | 9 + 7 |
| `get image url failed` | 5 |
| `remote_state_unknown_exhausted` | 1 |

| 分组维度 | 值 |
| --- | --- |
| 有生成记录的分组 | **53** |
| 至少成功 1 张 | 13 |
| 零成功 | **40** |
| 零成功且 `failover_total_attempt>=14` | **40** |

### 3.2 取样与哈希（不可否认的溯源）

4 张失败样本取自 12:57–12:58 +0800 的末轮尝试（`pix / banana-2.1`），文件 sha256 与库内
`checkpoint_json->>'expected_sha256'` **完全一致**；2 张通过样本的 sha256 与 MinIO `object_key` 内嵌值一致。

| 文件（`evidence/`） | 组 | 镜数 | 生成时间 (+0800) | sha256（前 16 位，完整见 `evidence/manifest.sha256`） | 库内 expected_sha256 |
| --- | --- | --- | --- | --- | --- |
| `failed/e459d954_6panels_25931.jpg` | `e459d954` | 6 | 12:58:06 | `3818ea6baddb19b0…` | **一致** |
| `failed/ce321800_4panels_30023.jpg` | `ce321800` | 4 | 12:58:00 | `9ba03ab153fe2fee…` | **一致** |
| `failed/1a04e778_4panels_25915.jpg` | `1a04e778` | 4 | 12:57:53 | `520bce426be57996…` | **一致** |
| `failed/313b41c6_5panels_25923.jpg` | `313b41c6` | 5 | 12:57:52 | `700a49cebe84af81…` | **一致** |
| `passed/79ec5d16_4panels.png` | `88e0c7a8` | 4 | 12:43:26 | `a377d13367de698b…` | MinIO key 内嵌一致 |
| `passed/cc1ced3d_4panels.png` | `6aa58bcf` | 4 | 12:31:37 | `b5f89acee14dd2a2…` | MinIO key 内嵌一致 |

尺寸：6 张全部 **2752×1536**（16:9）。预览见 `evidence/previews/`（900 px 缩图，仅用于快速目视）。

### 3.3 实测偏差（`captured/profile.txt`）

判定口径与业务代码一致：`|均值 − median(±65 px)|`，阈值 40；表内为**期望网格线 ±70 px 内的最大偏差**。

| 样本 | 方向/分隔 | 期望位置 | 整图中位亮度 | 带内最暗 | 最大偏差 | 是否达阈值 |
| --- | --- | --- | --- | --- | --- | --- |
| `e459d954`(6 格) | y split1 | 768 | 47.6 | **0.2** | 15.3 | BELOW |
| | x split1 | 917 | 44.9 | 0.2 | 24.6 | BELOW |
| | x split2 | 1835 | 44.9 | 0.2 | 22.9 | BELOW |
| `ce321800`(4 格) | y split1 | 768 | 53.1 | 0.4 | 16.8 | BELOW |
| | x split1 | 1376 | 51.5 | 0.2 | 12.3 | BELOW |
| `1a04e778`(4 格) | y split1 | 768 | 50.4 | 16.7 | 11.9 | BELOW |
| | x split1 | 1376 | 58.2 | 11.4 | 3.8 | BELOW |
| `313b41c6`(5 格) | y split1 | 768 | 33.8 | 0.4 | 16.1 | BELOW |
| | x split1 | 917 | 34.0 | 0.3 | 28.9 | BELOW |
| | x split2 | 1835 | 34.0 | 0.4 | 11.9 | BELOW |
| `79ec5d16`(通过) | y / x | — | 50.8 / 51.0 | 15.2 / 17.7 | **227.5 / 220.6** | MEETS |
| `cc1ced3d`(通过) | y / x | — | 67.4 / 63.6 | 18.4 / 18.7 | **196.8 / 218.7** | MEETS |

失败样本「带内最暗」几乎都是 0.2–0.4（纯黑分隔带确实画出来了），但最大偏差仍远低于 40；
通过样本的分隔是**亮的**（白页边/白缝），偏差 196–227。

### 3.4 邻域扫描：不是「调一个常量就好」（`captured/sweep.txt`、`captured/profile_neighbourhood.txt`）

固定 `Δ=40`，只改邻域半径，对 `e459d954`（6 格）：

| 邻域半径 | 结果 |
| --- | --- |
| 65（现值） | REJECT |
| 120 | REJECT（三条分隔最大偏差 24.1 / 27.2 / 26.2） |
| 160 / 220 | REJECT |
| 300 | REJECT（y=47.7、x1=45.4 达标，**x2 仍只有 34.2**） |

对照：`79ec5d16`（白分隔）在 65/120/300 下都能稳定切出 4 格（`1285×704` 等）。
⇒ 单一窗口半径或单一 Δ 常量的线性调整无法覆盖全部画面；**判据形态本身**（对单侧中位数取绝对差）是关键。

### 3.5 容器内复现（`captured/container_repro.txt`）

```
worker image: sha256:edee542dda35…
deployed module: /usr/local/lib/python3.12/site-packages/tubeflow_test/video_generation/panel_images.py (679 lines)
PIL 12.3.0
e459d954_6panels_25931.jpg panels=6 sha256=3818ea6b… REJECT VideoRuleViolation: shot_panel_gutter_not_found
ce321800_4panels_30023.jpg panels=4 sha256=9ba03ab1… REJECT VideoRuleViolation: shot_panel_gutter_not_found
1a04e778_4panels_25915.jpg panels=4 sha256=520bce42… REJECT VideoRuleViolation: shot_panel_gutter_not_found
313b41c6_5panels_25923.jpg panels=5 sha256=700a49ce… REJECT VideoRuleViolation: shot_panel_gutter_not_found
```

调用的是生产同款 `check_storyboard_panel_layout(path, panel_count=N, aspect_ratio="16:9", layout_version="grid-v2")`，
不是自写实现；本地仓库与容器内该文件逐字一致。

### 3.6 worker 日志（`captured/worker_log_reason.txt`）

- 当前 worker 容器（10:41:39 +0800 启动）内 `storyboard_panel_layout_rejected` 共 **1111** 条，
  **100% 属本项目**，`reason` **100% 是 `shot_panel_gutter_not_found`**，无第二种原因。
- 库内同码 2385 条；差额 1274 条产生于 08:30–10:41，属**已被重建的旧容器**（`json-file` 无持久化配置，
  重建即丢），不是漏记或采样。
- web 容器（`tubeflowai-test-web-1`）内同标记 **0** 条。
- 本项目暂停记录（当前容器可见）：`storyboard_failed:storyboard_panel_layout_unreadable` ×2、
  `storyboard_failed:provider_quota_exhausted` ×1。

### 3.7 重试预算与恢复次数（`captured/db_stats.txt` §6）

单组 `9b6a530a` 的 4 条预算链（+0800）：

| 链 | 提交次数 | 起 | 止 |
| --- | --- | --- | --- |
| `e759f5b7` | 14 | 08:30 | 09:16 |
| `fa581b58` | 14 | 09:47 | 10:42 |
| `6002137b` | 13 | 10:54 | 12:02 |
| `567e3044` | 14 | 12:05 | 12:56 |

- 每条链都是一次授权轮（`failover_route_index` 0→6，7 个模型依次尝试；同模型 2 次）。
- 链一断即换新链，说明预算被重置；共 **4 条链** ⇒ 预算至少被重置 3 次（通常由「恢复」触发）。
  当前容器日志（10:41 起）可见本项目 3 次暂停：1 次 `provider_quota_exhausted`、2 次
  `storyboard_panel_layout_unreadable`；10:41 之前的暂停记录随旧容器重建丢失。
- 每次恢复重置 14 次预算，单组累计 **55 次**提交（14+14+13+14）。
- `authorization_round=7` 与「多次恢复」一致（含此前阶段的恢复）。

### 3.8 按模型分布（`captured/db_stats.txt` §5）

| 模型 | 尝试 | 其中该码 | 成功 |
| --- | --- | --- | --- |
| grsai / gpt-image-2 | 390 | 334 | 0 |
| grsai / gpt-image-2.5 | 390 | 340 | 0 |
| grsai / nano-banana-2 | 387 | 384 | 3 |
| grsai / nano-banana-pro | 373 | 366 | 8 |
| pix / tt-image-2.5 | 366 | 361 | 0 |
| pix / tt-image-2 | 357 | 323 | 0 |
| pix / banana-2.1 | 290 | 277 | 2 |

**7 个模型全部为主要失败来源**，不是单一模型抖动；失败与「是否画出格子」无关，与「画面是否够亮」有关。

---

## 4. 时间线（+0800）

| 时间 | 事件 |
| --- | --- |
| 11-04 21:41:55 | 部署机 `.env` 打开 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true` |
| 11-04 22:23:48 | 本项目创建（此后首张故事板钉 `grid-v2`） |
| 11-04 22:23:52 | AUTO run 启动（round 1） |
| 11-05 08:30:51 | 进入 `storyboard` 阶段（旁白 11.9 min、导演预规划 37.0 min 已完成） |
| 08:30–09:16 | 第 1 条预算链（14 次） |
| 09:47–10:42 | 第 2 条链（恢复后） |
| 10:41:39 | 部署 `edee542d`，worker/web 重建（旧容器日志丢失） |
| 10:54–12:02 | 第 3 条链（13 次） |
| 12:05–12:56 | 第 4 条链（14 次） |
| **12:55:19** | AUTO 自动暂停：`storyboard_failed:storyboard_panel_layout_unreadable` |
| 12:58:06 | 最后一条在途尝试收尾（仍为该码） |
| 13:19 | 用户截图时状态：`PAUSED`，故事板 13 组完成 |

---

## 5. 影响面

`captured/db_stats.txt` §9：近 7 天该错误码共 2388 条，其中 **2385 条（99.9%）来自本项目**；
`2c291aba` 2 条、`fd3bf04d` 1 条（均为一次性，未导致停线）。**这不是全局回归**，
而是「v2 钉版 + 极暗画风」这一组合下的系统性误拒。

另：`aspect_ratio_not_supported` 68 条属另一类问题（模型返回非 16:9），不是本次暂停原因，未展开。

---

## 6. 复现步骤

### 6.1 本地（有 Python + Pillow 即可，不改代码）

```bash
# 仓库根目录
python "Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/scripts/repro_panel_layout.py" \
  check --image "Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/evidence/failed/e459d954_6panels_25931.jpg" \
  --panels 6
# 预期：REJECT VideoRuleViolation: shot_panel_gutter_not_found

# 量出每条分隔带的最大偏差（关键证据）
python ".../scripts/repro_panel_layout.py" profile \
  --image ".../evidence/failed/e459d954_6panels_25931.jpg" --cols 3 --rows 2
```

脚本默认从 `<repo>/backend/src` 导入业务代码（可用 `--src` 覆盖），只读、不写文件。

### 6.2 部署机容器内（生产同款代码）

```bash
docker cp evidence/failed/e459d954_6panels_25931.jpg tubeflowai-test-worker-1:/tmp/
docker exec -i tubeflowai-test-worker-1 python - <<'PY'
from pathlib import Path
from tubeflow_test.video_generation.panel_images import check_storyboard_panel_layout
check_storyboard_panel_layout(Path("/tmp/e459d954_6panels_25931.jpg"),
                              panel_count=6, aspect_ratio="16:9", layout_version="grid-v2")
PY
```

### 6.3 数据取证

```bash
docker exec -i tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow -A -F'|' -f - < sql/collect.sql
```

---

## 7. 数据清单

```
CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/
├── README.md                      # 本文
├── evidence/
│   ├── failed/                    # 4 张被拒整图原字节（2752×1536 JPEG）
│   ├── passed/                    # 2 张通过整图原字节（2752×1536 PNG）
│   ├── previews/                  # 6 张 900 px 缩图（仅目视）
│   └── manifest.sha256            # 上述文件校验和
├── scripts/repro_panel_layout.py  # check / profile / sweep 三种模式
├── sql/collect.sql                # 10 段取证 SQL（只读）
└── captured/
    ├── db_stats.txt               # 库内统计（本次采集原文）
    ├── check.txt                  # 6 张样本 pass/reject
    ├── profile.txt                # 每条分隔带最大偏差
    ├── profile_neighbourhood.txt   # 邻域 120 / 300 的对照
    ├── sweep.txt                  # 邻域扫描
    ├── container_repro.txt        # 容器内复现（含镜像 sha、PIL 版本、文件 sha256）
    └── worker_log_reason.txt      # worker 日志汇总、容器起始时间、暂停记录
```

采集时间：2026-11-05 13:30–13:35 +0800。所有命令只读；仅向部署机 `/tmp` 与 worker 容器 `/tmp`
复制了样本图片用于复现，未改数据库、未改业务文件、未重启服务。

---

## 8. 已知边界与待确认

1. **谁来恢复的这几次**：库内没有事件表，无法判定是页面「恢复自动执行」还是其他会话；
   只能确认库内存在 4 条独立预算链与 3 次暂停日志（1 次 quota、2 次 layout）。
2. **1274 条日志缺口**：08:30–10:41 的拒收日志随旧容器重建丢失，无法直接比对；
   该时段在库内有完整记录（`storyboard_generations`），不影响结论。
3. **页面「13/51 组」与库内 53 组**：差 2 组的统计口径未查证。
4. **本诊断未验证修复**：本文只描述失效机理，未提出已验证的补丁，也未改任何代码。
5. 既有记录 `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/DIAG.md`（同日、另一会话）
   已指出「24/24 失败在 `y wide`、码为 `shot_panel_gutter_not_found`、尺寸 2752×1536」；
   本文在此基础上给出**为什么**找不到（亮度阈值口径）与**代价**（预算与恢复次数），结论不冲突。

---

## 9. 候选修复方向（**均未实施、未验证**，供接手方评估）

| 方向 | 思路 | 风险 / 需验证 |
| --- | --- | --- |
| A. 判据改为「同时暗于两侧」 | 分隔线需比**左右（上下）各自**的对照带都暗（或都亮）≥ 某阈值，而不是与单侧中位数比 | 画面内横向暗带（桌沿、地平线）可能被误判；需用 Gate 1 的 109 张失败图 + §47 引用的 426 张样本回归 |
| B. 相对对比替代绝对 40 | 用 `(side − gutter) / max(side, 1)` 的比例（或在 gamma 校正后的亮度上计算） | 暗部噪声被放大；比例阈值需标定 |
| C. 生成侧要亮分隔 | 提示词要求白色/浅灰分隔（当前 §46 第 3 项明确要求「约 12 px 纯黑分隔」） | 属产品约定变更，需用户决定；且会改变 v2 版式与既有 13 张成功样本的口径 |
| D. 失败熔断 | 同项目连续 N 组同码即提前停手，不再让每组打满 14×7 | 与 §46 第 5 项「进入既有故障转移链、至多 14 次」冲突，需用户决定 |
| E. 仅检测阶段提亮 | 裁切前对用于**检测**的副本做 gamma/直方图拉伸，裁切坐标仍用原图 | 提亮后画面内暗线更容易被当作分隔；需保证 §47「取消格内分隔核验」不被破坏 |

**回归基线建议**：本次 6 张样本必须满足「4 拒 / 2 过」；
另外须复核 `Pending issues/2026-11-04/singlepanel_test/gate1/`（109 张失败图）与
`USER_DECISIONS` §47 引用的 426 张样本口径，确保 `shot_panel_gutter_ambiguous` / `shot_panel_crop_unsafe`
等既有判据不回归。

---

## 10. 相关文档

- `docs/planning/USER_DECISIONS.md` §46（网格 v2、钉版、14 次预算）、§47（crop-grid-v2 修订）
- `Pending issues/2026-11-04/DEFERRED_EXISTING_PROJECTS_20261104.md` §3.4（本项目的 36 条早期失败已记录）
- `Pending issues/2026-11-04/singlepanel_test/gate3_5/247462a4/DIAG.md`（同日另一会话的只读诊断）
- `Pending issues/2026-11-04/singlepanel_test/gate1/`（裁切算法的 Gate 1 基线数据）
