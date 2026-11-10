# 第 2 轮执行记录（交接包 v2，2026-11-10）

**结论先说**：12 条 `edited_v1` 提交全部 `ARCHIVED`，H3 实际 **12 次**、DeepSeek **0 次**；两组当前视频指针未变；无自动重提。按写定的判定规则，**H6/H1 与 H2 均不成立**——但 R0 两个 seed 之间的差异远大于臂间差异（见第 5 节），第 2 轮被 seed 噪声主导，不能据此否定 H6。最终判定仍以用户盲看为准。

## 1. 演练（12 条，全部 `DRYRUN_OK`）

- 第 1 批 8 条因我的驱动脚本少写 `g` 前缀报 `FileNotFoundError: '/tmp/r2/prompts/116_m1.txt'`，原文留在 `dryrun_v2_attempt1/`；修正文件名后 **12 条全部 `DRYRUN_OK`**，日志在 `dryrun_v2/`。
- 演练前后项目尝试总数均 **165**（未创建尝试）。
- 容器内四个提示词 sha256 与 `README.md` 第 4 节表格逐一相同。

## 2. 提交（12 条，全部归档）

| # | 标签 | attempt_id | seed | prompt_sha256（前 12） | 提交 (UTC) | 终态 (UTC) | stage |
| ---: | --- | --- | ---: | --- | --- | --- | --- |
| 1 | g116-r0-source | `54994575-9209-45b1-9936-3fa34c179ac4` | 2253417698150749 | c287c4d6b5e7 | 07:46:44 | 07:58:45 | ARCHIVED |
| 2 | g116-m1-source | `f446bca2-d017-4181-94be-a133db7775c7` | 2253417698150749 | 339183968e47 | 07:58:52 | 08:57:32* | ARCHIVED |
| 3 | g116-m2-source | `94103e89-199b-4803-a489-575da8021b8a` | 2253417698150749 | fa0632a6c191 | 08:36:58 | 10:08:36 | ARCHIVED |
| 4 | g116-r0-second | `cabee3fe-a5bd-41e1-b4fa-cc9987a0ce8d` | 20261110 | c287c4d6b5e7 | 10:08:36 | 10:19:40 | ARCHIVED |
| 5 | g116-m1-second | `ab19bf7b-5bec-4ee1-9aa1-a5da8f35be2a` | 20261110 | 339183968e47 | 10:19:40 | 10:58:46 | ARCHIVED |
| 6 | g116-m2-second | `25fb27e2-6787-48b9-9291-b90dc44cc252` | 20261110 | fa0632a6c191 | 10:58:46 | 11:05:54 | ARCHIVED |
| 7 | g119-r0-source | `7b6fbf90-febb-42cc-9fe6-d6f50bdf1e67` | 8248958064718409 | fadafbf2d85f | 07:58:56 | 10:49:42 | ARCHIVED |
| 8 | g119-m1-source | `3e81492f-6f53-46cd-ad0b-920d19cbd46c` | 8248958064718409 | 7e0efac93d04 | 10:49:42 | 10:57:47 | ARCHIVED |
| 9 | g119-m2-source | `1242af4e-37d3-4e0a-876c-eb9470d4a4cf` | 8248958064718409 | 78d2bb0f9b8a | 10:57:47 | 11:03:54 | ARCHIVED |
| 10 | g119-r0-second | `876f7567-b37c-45cc-b5f6-a44feb0fd3a8` | 20261110 | fadafbf2d85f | 11:03:55 | 11:08:59 | ARCHIVED |
| 11 | g119-m1-second | `d57ca4e8-1e38-43ed-976a-7bd1b44dcc85` | 20261110 | 7e0efac93d04 | 11:08:59 | 11:14:03 | ARCHIVED |
| 12 | g119-m2-second | `bb197cec-8089-461a-8abd-b1dbd9fcafcb` | 20261110 | 78d2bb0f9b8a | 11:14:03 | 11:19:07 | ARCHIVED |

\* m1-source 的真实归档时刻是 08:44 前后；日志里 08:57:32 是续跑器重新观察到的时刻（原轮询器在 90 分钟前被替换，未留下精确终态行）。

- 全部 12 条快照带 `experiment`（`name=prompt-edit-ab-v1`、`arm=edited_v1`、`variant_label`、`edited_prompt_sha256`、`prompt_source_attempt_id`），策略均为 `tubeflow_storyboard_v4_single_panel_edited_v1`。
- 提交的提示词 sha256 与文件逐一相同（`prompts/g116_m1.txt` 等），R0 等于来源提示词 sha。
- `superseded_attempt_id` 指向这 12 条的记录 **0** 条；两组 `group_current_videos` 前后不变（116 `4bb5c37b…`、119 `137ab7a8…`）；项目尝试总数 165 → 177（+12，全为实验条）。
- 每秒 60 秒只读轮询日志见 `submit_v2/`。

## 3. 调度冲突（影响时长，不影响结果）

- 07:58–10:40 之间，另一个项目 `6777b0bd`（韩语，109 组）持续占满 4 个 H3 槽位（`h3-new-1` 离线；`h3-new-2/3` 各 2 槽）。我们的 119 组 `r0` 从 07:58 等到 10:41 才拿到 STAGING，116 组 `m2` 等了 1 小时 32 分。
- 交接包 §4.3 原定 90 分钟停线；实施方把选择交给用户，用户 2026-11-10 选择「继续等对方跑完」，据此改为每条 6 小时硬上限，**不取消、不重提、不换参数**。
- 对方项目在 10:40 前后释放槽位，之后 8 条在 30 分钟内跑完。

## 4. 收集与测量（只读）

- `collect_r2.py` 首次运行报错：`column "created_at" does not exist`（脚本 select 了 `video_generation_attempts.created_at`，该表没有此列）。**已把该脚本的 `created_at` 改为 `updated_at`、`created_at` 字段置 `None`**（提交与终态时刻以 `submit_v2/` 日志为准），其余逻辑未动；`attempts.json` 14 条（12 实验 + 2 生产）、`errors` 空、每条 `video_sha256_matches_asset` 为 true。原报错原文见 `Pending issues/2026-11-09/evidence/handoff_v2/`。
- `make_compare.py`、`measure_motion.py` 在 **web 容器内**运行（容器有 ffmpeg 6/7 与 cv2 5.0.0；测试机无 ffmpeg）。产出 `compare/compare_g116.mp4`、`compare/compare_g119.mp4`、`KEY.json`、`videos_960/`，`measure.jsonl` 14 行、0 错误。

## 5. 测量结果（`env_score`，960 宽像素/秒）

| 组 | 生产 | R0 @source | R0 @second | M1 @source | M1 @second | M2 @source | M2 @second |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 116（第 1 镜） | 0.71 | 0.71 | **3.90** | 2.29 | 3.31 | 3.36 | 2.83 |
| 119（两镜平均） | 1.31 | 1.31 | **3.71** | 2.26 | 2.03 | 2.21 | 6.72 |

- 可复现性：`g116_r0_source` 与 `g116_production`、`g119_r0_source` 与 `g119_production` 的**解码帧 MD5 完全相同**（`e34633f3…`、`bad847b0…`），文件级 sha256 不同只是封装元数据；R0 复跑的画面与生产一致。
- 但同一提示词换 seed 后差异巨大：116 `env_score` 0.71 → 3.90（树线控制区 0.46 → 3.17，触发「整画面晃动」退化闸）；119 1.31 → 3.71（`control:wall` 0.51 → **15.2**）。**seed 的影响远大于臂的影响。**
- 按写定规则：H6/H1 要求 M1 ≥ 2×R0 且两个 seed 都满足——116 只在一个 seed 成立（2.29 vs 0.71，3.2×；另一个 3.31 vs 3.90，0.85×）、119 两个 seed 都不成立；H2 要求 M2 ≥ 1.5×M1 且两个 seed 都满足——116 为 1.47× / 0.85×、119 为 0.98× / 3.31×，均未同时满足。**结论：按规则都不成立，但差异主要来自 seed 噪声与 R0@second 的整画面晃动，判定的解释力有限。**
- 退化闸：116 R0@second 树线 3.17 px/s（>1.5 且 >3×R0 的 0.46）；119 R0@second `control:wall` 15.2 px/s。`held_share` 全部 ≤0.05，`hf` 0.027–0.103（参考线 0.25），无抖动抬升。

## 6. 给用户的上传包

`deliverables/R2_videos_v2.zip`（28,426,480 字节，sha256 `61f4bb35f089c7ca2a723d3d623a2b758301237086b66936dc50818d1743b166`），含 `compare_g116.mp4`、`compare_g119.mp4`、`videos_960/`，**不含 `KEY.json`**（先盲看）。
