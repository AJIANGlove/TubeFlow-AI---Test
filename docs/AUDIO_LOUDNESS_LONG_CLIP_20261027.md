# 长音频响度归一化回落（2026-10-27）

## 1. 现象

项目 `22dbcf09`（Pompeii）配音第 229 句在 `budget_round` 2→6 之间反复失败，每轮 3 次用尽后
自动流水线以 `narration_submission_budget_exhausted` 暂停；用户每点一次「恢复自动执行」，
约 50 秒后再次暂停（日志 `2026-10-27T04:14:18Z`、`04:15:08Z`、`04:23:37Z`）。
该句尝试记录的 `safe_error_code` 一律是 `processing_failed`，看不到真实原因。

## 2. 根因（部署机实测复现）

取该句当前检查点 `narration/ff03d2f0-…`（691,432 B，内含 `0229-6fa4f06b9b97.wav`）：

```
pcm_f32le / 24000 Hz / mono / 7.200 s / 691,292 B
mean_volume -22.5 dB, max_volume -3.5 dB
句内三段停顿：1.86–2.47s、3.49–3.88s、5.96–6.40s（合计约 1.4s）
```

在容器内用同一份 `FfmpegAudioNormalizer` 复跑，稳定抛出：

```
ValueError: FFmpeg normalized loudness is outside tolerance   (audio_qc.py:606)
```

数值原因：

| 项 | 实测 | 门限 |
|---|---|---|
| 源 integrated loudness | -23.11 LUFS | 目标 -16 |
| 源 true peak | -3.49 dBTP | 上限 -0.8 |
| 需要增益 | +7.11 dB | — |
| 峰值剩余余量 | ≈2.5 dB | — |
| 两遍 `loudnorm(linear=true)` 输出 | -17.43 LUFS / TP -1.00 | -16 ± 1 LU |
| 偏差 | 1.43 LU | 超 0.43 LU → 抛错 |

`linear=true` 只允许线性增益，峰值余量不足时无法补到 -16；该异常是无码 `ValueError`，
`_complete_raw` 归为 `processing_failed` / `OUTPUT_INVALID`，于是每轮同因失败。

对比：≤3.0s（`_SHORT_AUDIO_MAX_FRAMES_24KHZ = 72_000`）的短音频走 `_normalize_short`，
带增益窗口 ±12 dB、限幅器与最多两次闭环校正；**>3s 的长音频此前没有任何回落**。
（短音频那条修复见 `runtime/operations/stuckpoints_fix_20261014.md` 问题 3，只覆盖短句。）

## 3. 修复

`backend/src/tubeflow_test/adapters/audio_qc.py`：

1. 主路径（两遍 `loudnorm(linear=true)`）**完全不变**；仅在过不了响度门时调用
   `_normalize_short(..., long_audio=True)`，复用既有「增益 + 限幅 + 闭环校正」配方，
   输出仍受同一响度门（±1 LU / TP ≤ -0.8 dBTP）与同一时长守恒校验约束。
2. 新配方标识 `long-f32le-24k-gain-limiter-v1`，写入 `normalization.fallback.recipe_id`；
   `max_source_frames_24khz` 在长音频回落时置空并新增 `source_frame_count`。
3. 新增 `AudioProcessingError(ValueError)`，携带稳定失败码；响度不可达时抛
   `audio_loudness_unreachable`，经既有 `getattr(error, "code", …)` 直接落到
   `safe_error_code`（界面可见），不再是无码 `processing_failed`。
4. 资格窗口沿用短音频常数（初始 ±12 dB、最终 15 dB）；超出窗口仍 fail-closed。

`backend/src/tubeflow_test/application/narration.py`：

- `_complete_raw` 的失败分支补结构化 warning 日志（project / sequence / phase / code / 异常摘要），
  下次同类问题无需上机逐字节复现。

## 4. 验证

- 三个真实失败产物（seq 229 的 r5/r6 检查点）在新代码下全部过门：

| 资产 | 路径 | 输出响度 | True Peak | 增益 | 输出帧数 |
|---|---|---|---|---|---|
| `ff03d2f0` | long-f32le-24k-gain-limiter-v1 | -16.68 LUFS | -1.04 dBTP | +7.11 dB | 345,600 |
| `f8b699b3` | 同上 | -16.78 LUFS | -1.04 dBTP | +6.63 dB | 349,440 |
| `92e7ced1` | 同上 | -16.62 LUFS | -1.03 dBTP | +6.90 dB | 349,440 |

  帧数与源严格 2:1（24k→48k），时长不变。
- 新增单元回归（`tests/unit/test_audio_normalizer_contract.py`）：
  - 长音频低响度 → 回落通过、帧数守恒、配方与 `source_frame_count` 正确；
  - 长音频主路径达标 → 不触发回落（`two-pass-loudnorm`，无 `fallback` 键）；
  - 需求增益 ±12 dB 之外 → `audio_loudness_unreachable` fail-closed；
  - `_long_fallback_eligible` 边界（≤3s、非 24k、NaN）。
- 既有用例 `test_non_short_source_fails_closed_when_gate_is_missed` 语义更新：
  回落适用时先尝试回落，仍不达标照样 fail-closed（渲染 4 次）；另补
  `test_non_short_source_outside_fallback_window_renders_once_then_fails`（1 次渲染后失败）。
- 全量后端单测：**2099 passed / 8 skipped / 1 failed**。唯一失败
  `test_production_container_contract.py::test_dockerfile_uses_migration_gated_api_console_and_ffmpeg_smoke`
  属既存漂移（工作区 `backend/Dockerfile` 早前已改为阿里云 apt 源，用例仍断言旧 apt 行），
  与本次改动无关，未修。

## 5. 回滚

- 容器内覆盖前备份：部署机 `/home/edy/tubeflow-migrate/overlay-longaudio-20261027/pre.tar.gz`
  （含 `audio_qc.py` 与 `narration.py` 原文件）。
- 回滚 = 解包覆盖两文件后 `docker compose restart web worker`；镜像固化版本见
  `docs/DEPLOYMENT.md`。

## 6. 遗留与边界

- 长音频回落只在源为 `pcm_f32le / 24kHz / mono` 时启用（当前 EL provider 产物形态）；
  其它格式仍 fail-closed，不会放大噪声。
- 需求增益超过 ±12 dB 的合法句仍会失败（与短音频同口径），但会带
  `audio_loudness_unreachable` 码，可被界面与日志直接识别。
- 工作区仍有 300+ 个未提交文件（上次提交为 2026-09-08 基线 `d3ef87d`），本次仅提交
  与本缺陷相关的 3 个文件 + 本文档；完整快照入库仍需单独立项。
