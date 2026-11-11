# 第 3 轮执行记录（交接包 v3，2026-11-11）

- 8 条 `edited_v1` 全部 `ARCHIVED`；H3 实际 **8 次**（研究累计 20/24，剩余 4）、DeepSeek **0 次**（剩余 16）。
- 两组当前视频指针未变；`superseded_attempt_id` 指向这 8 条的记录 0 条；项目尝试数 **177 → 185**。
- 本文只记录执行与数值，**不作判定**（判定由 Claude 按 `round3/README.md` 第 5 节做，用户目视为最终标准）。

## 1. 演练（8 条，全部 `DRYRUN_OK`）

容器内 `/tmp/r3/prompts/*.txt` 的 sha256 与 `round3/README.md` 第 2 节表格一致（`g116_m3.txt 4e4c094a…`、`g119_m3.txt 86ed6c60…`）。演练前后项目尝试总数均 **177**；日志在 `dryrun_v3/`。

## 2. 提交（全部归档，`error_code` 空）

| # | 标签 | attempt_id | node | seed | prompt_sha256 | held_share | hf | 提交→终态 (UTC) |
| ---: | --- | --- | --- | ---: | --- | ---: | ---: | --- |
| 1 | g116-m3-second | `a464acfa-8aff-4125-93e3-fcaf6688fe3f` | h3-new-2 | 20261110 | 4e4c094a…（表） | 0.00 | 0.056 | 02:36:23 → 02:43:26 |
| 2 | g116-m3-source | `cf895e28-ce22-4c61-9b93-be3735a410d1` | h3-new-2 | 2253417698150749 | 4e4c094a…（表） | 0.00 | 0.109 | 02:43:26 → 02:50:30 |
| 3 | g116-m3-third | `b3f12e73-64f7-4721-9867-6579b1304786` | h3-new-2 | 20261111 | 4e4c094a…（表） | 0.00 | 0.058 | 02:50:30 → 02:57:34 |
| 4 | g116-r0-third | `dbc7131e-a491-4a16-96f4-016ea7424446` | h3-new-3 | 20261111 | c287c4d6…（来源） | 0.00 | 0.074 | 02:57:34 → 03:04:38 |
| 5 | g119-m3-source | `f73a7998-4d13-48b4-a9ba-31b17f9108e9` | h3-new-3 | 8248958064718409 | 86ed6c60…（表） | 0.00 | 0.090 | 02:36:27 → 02:41:31 |
| 6 | g119-m3-second | `d380b156-b286-4f4b-8f9e-44ad7047974b` | h3-new-3 | 20261110 | 86ed6c60…（表） | 0.00 | 0.076 | 02:41:31 → 02:46:35 |
| 7 | g119-m3-third | `85c39956-1f67-44e1-9ff8-171a6d156e87` | h3-new-3 | 20261111 | 86ed6c60…（表） | 0.00 | 0.099 | 02:46:35 → 02:51:39 |
| 8 | g119-r0-third | `ef7ce9ea-e601-4c98-b859-60c395db7236` | h3-new-3 | 20261111 | fadafbf2…（来源） | 0.00 | 0.110 | 02:51:39 → 02:56:43 |

`node_id` 与完整 attempt_id 见 `attempts.json`（22 条中的 `motion-r3-*`）与 `submit_v3/`。

## 3. 收集与测量

- `collect_r2.py`（v3 版，支持 `idempotency_prefixes`）→ `attempts.json` **22 条**（第 2 轮 12 + 本轮 8 + 2 条生产）、`errors` 空、每条有视频的 `video_sha256_matches_asset` 为 true。
- `make_compare.py` + `round3/spec.json` → `compare_g116.mp4`、`compare_g119.mp4`、`KEY.json`、`videos_960/`（22 个）。格位：116 = `m2_second, m3_third, r0_third, m3_source, m2_source, m3_second`；119 = `m3_second, r0_third, m2_source, m3_third, m2_second, m3_source`。
- `measure_motion.py`（v3 版，固定时段 + 相关性保护 + `regions_dx_px_s`）→ `measure_v3.jsonl` **22 行**、0 错误。

### 3.1 报告要求的数值（只报数）

| 视频 | `env_score`（按镜头） | `window_truncated` | `segment_s` |
| --- | --- | --- | --- |
| g116_m3_source | 2.78 | true | 0.38–7.5 |
| g116_m3_second | 3.29 | true | 0.25–5.5 |
| g116_m3_third | 5.86 | true | 0.25–6.88 |
| g116_r0_third | 1.38 | false | 0.25–7.5 |
| g116_r0_source | 0.79 | false | 0.25–7.5 |
| g116_r0_second | 0.95 | false | 0.25–7.5 |
| g119_m3_source | 4.05 / 4.31 | false / false | 0.25–4.5 / 5.5–7.0 |
| g119_m3_second | 2.99 / 1.88 | false / false | 同上 |
| g119_m3_third | 5.14 / 2.28 | false / false | 同上 |
| g119_r0_third | 1.16 / 0.67 | false / false | 同上 |
| g119_r0_source | 1.15 / 1.06 | false / false | 同上 |
| g119_r0_second | 2.14 / 1.23 | false / false | 同上 |

119 各条的 `regions_dx_px_s`（正 = 向右）：

| 视频 | 镜 1 water / sky / branches | 镜 2 sky / water / trees |
| --- | --- | --- |
| m3_source | +1.08 / +5.00 / −1.26 | +3.55 / +3.79 / −1.01 |
| m3_second | +1.32 / +5.28 / −0.76 | +3.07 / +0.71 / −1.11 |
| m3_third | +0.39 / +3.72 / −1.39 | +2.83 / +0.88 / −1.00 |
| r0_third | −0.37 / +1.37 / −0.37 | +0.21 / −1.10 / +0.18 |
| r0_source | −0.43 / +1.50 / −0.45 | +0.69 / −3.50 / +0.19 |
| r0_second | −1.62 / +1.94 / −0.53 | +1.45 / −0.05 / −0.09 |

## 4. 给用户的上传包

`deliverables/R3_videos_v3.zip` — **19,472,372 字节**，sha256 `12ba43a97589de5fc9a8db37059f15aee2bf9128c45da9d1ed6abf2a0cc62ca8`
内容：`compare_g116.mp4`、`compare_g119.mp4`、本轮 8 个 960 宽视频（`*_m3_*.mp4`、`*_r0_third.mp4`），**不含 `KEY.json`**。

## 5. 偏差

- 无 Provider 报错、无 REFUSED、无超时。部署机仓库仍无 remote，沿用 v2 的 tar + `docker cp` 方式（`/tmp/r3`）。
- 容器内 `/tmp/r3`、`/tmp/motion_r3` 已删除。
