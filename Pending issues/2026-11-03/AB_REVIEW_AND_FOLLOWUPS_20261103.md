# A/B 证据复核与后续实施（2026-11-03）

范围：对 `VIDEO_QUALITY_AB_RESULTS_20261103.md` 与 `evidence/` 8 张图的只读复核，以及据此在分支 `claude/quality-followups-20261103` 中的实施。未调用任何真实 Provider，未部署。

## 1. 复核结论

### 1.1 与原报告一致（已核实）

- v3 两组均出现非参考图人物：第 11 组全程为短发围裙男性，第 16 组为穿马甲男性；v4 两组均为参考图女性（长卷发、眼镜、米色西装）。
- v4 第 16 组为三段真实速度场景（台灯书桌 → 柜台 → 书桌），无图表叠加。
- 第 11 组新故事板与旧版几乎相同：坐姿、托腮、正面构图，仍有 `SHOT 1` 角标。

### 1.2 原报告遗漏或不准确

1. **第 11 组 v4 人物消失再出现**：约 0.5–3 s 关键帧中人物半透明、消失后再出现（`evidence/AB_g11v4_contact.png` 第 1 行第 2–5 帧、第 2 行第 1 帧）。原报告“30 帧均为同一名女性”不准确。原因未查明。
2. **第 16 组 v3 存在格局泄漏**：约 1.5–6.5 s 为左右并列的两块柱状图画面（`AB_g16_v3_contact.png` 第 1–4 行），属多格同框类缺陷，原报告未列。
3. **第 16 组姿势部分改变**：旧故事板三格人物均托腮（第 1 格侧脸托腮）；新故事板仅第 2 格托腮，第 1 格为写字的手，第 3 格无人物。原报告记为“未改变”。
4. **故事板错误语言文字**：第 16 组新故事板金额使用中文“万”（如“7,491万”），项目为韩语；第 11 组为中文招牌。视频中被复制成乱码（如第 11 组 v4 招牌“人月營銷費”）。文字来源是故事板，不是 v4。
5. **竖条格子**：16:9 下 3 镜为 1×3，每格约 557×941（竖幅），H3 需把竖幅参考转成横幅镜头。

### 1.3 仍不能下结论

- 四次 attempt 的 seed 均为 `NULL`，抖动差异只是方向性证据；样本为 2 组、1 个项目、各 1 次。

## 2. 本分支实施（USER_DECISIONS §43、§44）

| 项 | 文件 | 内容 |
| --- | --- | --- |
| 默认 v4 | `backend/src/tubeflow_test/config.py`、`.env.example` | 默认 `tubeflow_storyboard_v4_natural_motion`；`.env.example` 原为已证伪的 `tubeflow_enhanced_v1`，一并更正 |
| 梯子第 4 级 | `video_generation/orchestration.py` `_ladder_strategy` | 配置为 v3/v4 时用配置策略；其他配置保持 `kinestack_v6_port` |
| 风格冲突子句 | `domain/storyboards.py`、`application/storyboard_prompt.py`、`video_prompt/templates.py` | 安全规则 v2 移除“保留原图……仅转换……风格”，故事板与视频编译载荷共用同一正则；快照不改写 |
| 故事板文字 | `application/storyboard_prompt.py` | 不再绘制 SHOT 角标；禁止图内可读文字，金额与对比用实物表达 |

部署注意：部署机若显式设置 `TUBEFLOW_VIDEO_PROMPT_STRATEGY=tubeflow_storyboard_v3`，须改为 v4（或删除该行）才生效；部署前确认无在途故事板生成。

## 3. 未实施：H3 seed 记录（交给具备 PostgreSQL 集成测试环境的实施方）

### 3.1 目标

首个 attempt 在未设 seed 时也写入确定、可复现的 seed，使后续 A/B 可单变量对照；不改变“每次重新生成得到不同结果”的语义。

### 3.2 约束（代码核实）

- seed 同时进入 `input_snapshot["seed"]`（`orchestration.py` `_input_snapshot`，取 `source.seed`）与 `request["parameters"]["seed"]`（`_request`，取 `generation.seed`）；Worker 由快照重建 generation（`_generation_from_snapshot` → `_snapshot_seed`），引擎提交读 `generation.seed`（`video_generation/service.py`）。
- `trigger()` 的幂等比较在分配 `attempt_id` 之前构建 fresh 快照并与已持久化快照比较；若 seed 由 `attempt_id` 派生，同一幂等键重试会误报 `idempotency_conflict`。
- `validate_reference_intent()` 用 `source + attempt.attempt_id` 重算 request 哈希；`prepare_manual_video_intent()`、`prepare_frozen_single_panel_replay_intent()` 也各自构建快照与 request。

### 3.3 建议实现

1. seed 由 `(group_id, idempotency_key)` 派生：`int.from_bytes(sha256(f"{group_id}:{idempotency_key}").digest()[:8], "big") % 2**53`，仅在 `source.seed is None` 时使用；与 `_ladder_seed` 同一整数域。
2. 新增 `_authorize(source, *, idempotency_key: str | None = None)`，在构建 `GroupVideoGeneration` 时代入派生 seed；`_input_snapshot` 的 `"seed"` 改取 `generation.seed`。
3. 四条意图路径（`trigger`、`ManualH3IntentWriter` / `prepare_manual_video_intent`、`validate_reference_intent`（取 `attempt.idempotency_key`）、冻结单格回放（取 `replay_id`））统一传入同一键。
4. 测试：同键重试不冲突且 seed 相同；不同键 seed 不同；Worker 重建后提交的 seed 与快照一致；`validate_reference_intent` 哈希自洽；PostgreSQL 集成用例覆盖手动意图与回放。
5. 可选：`ManualH3Input` 增加可选 `seed`（当前 `extra="forbid"`），仅用于实验对照；属于公开 API 变化，须用户另行确认。

## 4. 待查：第 11 组 v4 人物消失

只读查询 attempt `ed495263-3072-482b-98e0-f6d357c7b886` 在 `video_prompt_compilations` 中的编译全文，核对 0–3 s 描述（是否写了 enter/appear/fade 或开场无人物的空镜）。若提示词无此类描述，则归为 H3 生成伪影，由后续人物在场 QC（关键帧人脸计数 1→0→1）兜底。
