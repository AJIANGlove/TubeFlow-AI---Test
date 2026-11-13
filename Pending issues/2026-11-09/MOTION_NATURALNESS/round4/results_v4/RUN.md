# 第 4 轮执行记录（交接包 v4，2026-11-13）

- 3 条 `edited_v1`（M4 × second/third/source）全部 `ARCHIVED`；H3 实际 **3 次**（研究累计 23/24，**剩余 1**）、DeepSeek **0 次**。
- 第 119 组当前视频指针未变（`137ab7a8…`）；`superseded_attempt_id` 指向本轮 **0** 条；项目尝试数 **185 → 188**。
- 本文只记录执行与数值，**不作判定**。

## 1. 演练（3 条，全部 `DRYRUN_OK`）

容器内 `/tmp/r4/prompts/g119_m4.txt` 的 sha256 = `8237492eb94b200bdc138a90672b9063d44588735356a0ddf961be4251d98a10`，与 `round4/README.md` 一致。演练前后项目尝试总数均 **185**；日志在 `dryrun_v4/`。

## 2. 提交（全部归档，`error_code` 空）

| # | 标签 | attempt_id | node | seed | prompt_sha256 | held_share | hf | 提交→终态 (UTC) |
| ---: | --- | --- | --- | ---: | --- | ---: | ---: | --- |
| 1 | g119-m4-second | `41dc72d9-2861-4289-8647-27511f150881` | h3-new-2 | 20261110 | 8237492e…（表） | 0.00 | 0.065 | 03:06:06 → 03:11:10 |
| 2 | g119-m4-third | `219c7095-dcc4-4b45-82c0-c6e397160a13` | h3-new-2 | 20261111 | 8237492e…（表） | 0.00 | 0.092 | 03:11:10 → 03:16:14 |
| 3 | g119-m4-source | `5e5a599d-bbab-4025-87bf-f1bfcb1b4c50` | h3-new-2 | 8248958064718409 | 8237492e…（表） | 0.00 | 0.104 | 03:16:14 → 03:21:18 |

## 3. 收集与测量

- `collect_r2.py`（v4 版）→ `attempts.json` **8 条**（交接包写「应有 7 条」，实际为 m3×3 + r0@third + m4×3 + production = **8**）、`errors` 空、`video_sha256_matches_asset` 全 true。
- `make_compare.py` → `compare_g119.mp4`、`KEY.json`、`videos_960/`（8 个）。格位：1 `g119_m3_third`、2 `g119_m4_source`、3 `g119_m3_source`、4 `g119_m4_third`、5 `g119_m3_second`、6 `g119_m4_second`。
- `measure_motion.py`（v4 版，带 anchor）→ `measure_v4.jsonl` **8 行**、0 错误。

### 3.1 报告要求的数值（第 1 镜，只报数）

| 视频 | `env_score` | `window_truncated` | `regions_dx_anchor_px_s` |
| --- | ---: | --- | --- |
| g119_m4_second | 2.76 | false | water +0.44 / sky **+5.66** / branches_left +0.16 / coat_hem −0.04 / figure −0.03 |
| g119_m4_third | 3.32 | false | water +0.19 / sky **+4.72** / branches_left −0.13 / coat_hem −0.51 / figure −0.06 |
| g119_m4_source | 3.03 | false | water +0.01 / sky **+3.54** / branches_left −0.01 / coat_hem −0.52 / figure −0.02 |

对照（同一 `round4/spec.json` + anchor 口径重跑的 8 条）：

| 视频 | `env_score`（第 1 镜） | `env:sky` anchor dx | `env:coat_hem` anchor dx |
| --- | ---: | ---: | ---: |
| g119_m3_second | 2.98 | +8.14 | +0.07 |
| g119_m3_source | 3.70 | +8.36 | −0.02 |
| g119_m3_third | 4.79 | +6.94 | +0.08 |
| g119_r0_third | 0.81 | +2.33 | 0.00 |
| g119_production | 0.83 | +2.55 | +0.04 |

## 4. 给用户的上传包

`deliverables/R4_videos_v4.zip` — **7,407,370 字节**，sha256 `c7c96e438dea53c6d991d58d61ade06fb6ee18b8f54228d9e3db1fb99ab72241`
内容：`compare_g119.mp4` + 本轮 3 个 960 宽视频（`g119_m4_second/third/source.mp4`），**不含 `KEY.json`**。

## 5. 偏差

- 无 Provider 报错、无 REFUSED、无超时；部署机仓库仍无 remote，沿用 tar + `docker cp`（`/tmp/r4`）；容器内 `/tmp/r4`、`/tmp/motion_r4` 已删除。
- 交接包 §5 写 `attempts.json 应有 7 条`，实际 8 条（见第 3 节）；按实际记录。
