# 第二轮 A/B 复核与导演 v10（2026-11-03）

范围：对 `VIDEO_QUALITY_AB_ROUND2_RESULTS_20261103.md` 与 `evidence/r2_*.png`、`sb11_r2.png`、`sb16_r2.png` 的只读复核，以及分支 `claude/director-v10-retry-feedback` 的实施说明。未调用任何真实 Provider，未部署。

## 1. 第二轮复核

### 1.1 与原报告一致（已核实）

- 两张新故事板无 SHOT 角标、无可读文字；第 11 组人物摊手（不再托腮），第 16 组三格分别为书写、托举两个店模型、无人物的图示，身份与参考图一致。
- 11 组 v4-S1 全程为参考图女性，柜台整理票据，动作连续。
- 16 组 v3-S1 全程无人物、含大块黑区与乱码文字（如 “NEX4 CANT”），与报告一致。

### 1.2 原报告遗漏

1. **16 组 v4-S2 出现整张故事板复现**：约 1.5–6 s 关键帧为三格故事板原样并列（左：书写；中：托举店模型；右：图示），随后以“翻页”方式切走（`evidence/r2_g16v4s2.png` 第 1 行第 4 帧起至第 4 行）。原报告记为“格线/角标：无（有 4–5 帧翻页过渡）”，不准确；该条 HF 0.273 超标也与此有关。即 v4 在身份上 4/4 正确，但格局泄漏 1/4。
2. **人物头部出画**：11 组 v4-S1 前段与 16 组 v4-S2 后段多帧人物面部在画面上沿之外（只见下巴以下），属构图缺陷。

### 1.3 含义

- 格局泄漏发生在 16:9 三格（1×3 竖条格）的故事板上，支持“逐镜单独生成 16:9 参考图／单格参考默认化”（`TubeFlow Optimization Review` #4、证据复核 #7）作为下一步。
- 人物身份问题在受控 seed 下由 v4 解决（v3 0/4、v4 4/4）。

## 2. 导演 v10 与重试带原因（USER_DECISIONS §45）

### 2.1 根因（代码核实）

- `765dca11` 第 2、3 次导演运行均在 v9 单元（第 20 单元）因 `director_output_adjacent_diversity_insufficient` 失败：一次只改了 subject_focus（PROCESS→OBJECT_OR_EVIDENCE），一次两镜均为 OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION、只改 semantic_event。
- v9 的“3 句以上单元至少两镜”与“切到物件镜头会改变 subject_focus、通常也改变 semantic_event”的措辞，正好诱发上述两种只改一维的组合。
- 重试以完全相同的提示词、`temperature 0.0` 与同一 `Idempotency-Key` 重发（`adapters/director_model.py` `generate`；`application/project_director.py` `_retry_or_terminal`），3 次尝试实际等于 1 次；近 30 天 32 次相邻多样性失败全部耗尽预算与此一致。

### 2.2 实施

| 项 | 位置 | 内容 |
| --- | --- | --- |
| v10 提示词 | `application/project_director.py` `_v10_unit_prompt` | v9 文本只替换三处：换物件镜头须同时换事件；同主体焦点且同时刻同地点必然非法；一时一地单元允许单镜 |
| 钉版 | `_pinned_unit_prompt_ref` | 新项目与 v9 未成功项目 → v10；v9 成功项目 → v9；v8 → v8 |
| 重试带原因 | `_provider_call_unit`、`_repair_notice` | 本地拒收后的重试在发送给服务商的提示词末尾附加拒收原因并换用逐次幂等键；持久化单元不变 |

### 2.3 验证

- 黄金值：v8、v9 对固定输入的运行 canonical 与全部单元提示词哈希逐字等于改动前（v9 黄金值由 `e6d073b` 生成）。
- 新增 14 项单元测试，含端到端：首次两镜零维差异被拒 → 第 2 次带原因与新键 → 运行成功，持久化单元不含附加文本。
- 未运行 PostgreSQL 集成测试、ruff、mypy；未做真实生成。

### 2.4 未实施（待用户决定）

- 末次失败后对违规相邻镜头做确定性合并、不使整轮失败（与 §5.3“不自动降级”、§20.2 冲突，须用户决定）。
