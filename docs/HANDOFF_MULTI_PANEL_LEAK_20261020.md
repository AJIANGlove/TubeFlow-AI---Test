# 交接文档：「多格故事板同框」缺陷

- 日期：2026-10-20
- 状态：**未开始处理**（用户明确要求与“清晰度/HD”方案分开，单独立项）
- 交接对象：新会话
- 生产现状（不要回退）：2026-10-20 13:20 已上线 HD 工作流 + 1920×1080 合成（`docs/DEPLOYMENT.md §24`），与本缺陷无关，处理本缺陷时不得改动这条链路。

---

## 1. 一句话描述

模型生成的组视频里，**没有生成连续的单画面镜头，而是把多格故事板整张“复现”成了一帧**：画面中出现并排的 2–3 个格子、格线、以及故事板里烧进去的 `SHOT n · 时间` 说明文字，有时还带着故事板里的价签/字幕。

## 2. 现象与判定标准

正常：一段 10–15 秒视频，全帧只有一个连续场景，镜头内人物/场景随提示词推进。

异常（同框）满足任一即可判定：
1. 画面被 1–2 条明显的竖直或水平分隔线切成 2–3 块，各块内容/机位不同；
2. 画面里出现故事板式的 `SHOT 1` / `SHOT 2` / `SHOT 3` 文字（或带秒数区间的说明）；
3. 同一帧同时出现“远景+中景+特写”三种构图并列，且互不衔接。

注意：本项目允许“价签数字”等剧情内文字，所以**不能把任何文字都判为泄漏**；判定重点是格线、`SHOT n` 说明、并列多机位。

## 3. 最新可复现样本（2026-10-20 实测）

| 项 | 值 |
| --- | --- |
| 项目 | `5c6fce56-78ea-41b9-825c-f278698b5ca1`（야채 가게 장사 뒤에 숨은 진짜 비밀，121 组） |
| 组 | 第 2 组（sequence 2，294 帧 / 12 秒） |
| seed | `123456789` |
| 故事板 | 1671×941，**3 格并排**，格内已烧入 `SHOT 1/2/3` 与时间说明 |
| 基线 1056×608 | **复现**（`ab_base_g2_00001_.mp4`） |
| HD 1600×896 | **同 seed 同样复现**（`ab_hd_g2_00001_.mp4`） |

结论：这不是分辨率或 HD 工作流引入的问题；**同一 seed 下基线与 HD 都会复现**，说明是“故事板参考图 → 模型行为”层面的问题。

提示词侧已经包含两道防线，但仍然失效：
- `Treat each panel as a separate chronological shot beat, not as one composite image.`
- 末尾负规则：`No storyboard grid, panel borders, split screen, text overlays beyond the intended price-comparison numbers, ...`

证据文件（已归档，可直接看）：
```
runtime/operations/multi_panel_leak_20261020/
  ab_storyboard_2.png        3 格故事板原图（含 SHOT 说明）
  g2_7.png                   基线第 7 秒整帧（同框）
  hd_g2_7.png                HD 第 7 秒整帧（同框）
  g2_base_crop.png           基线 2K 1:1 裁剪
  g2_hd_crop.png             HD 2K 1:1 裁剪
  ab_base_g2_00001_.mp4      基线失败视频（1056×608）
  ab_hd_g2_00001_.mp4        HD 失败视频（1600×896）
  prompt_g2_pix.txt          该组实际提示词（4203 字符）
```

## 4. 历史结论（避免重复走弯路）

- `USER_DECISIONS §17`：`tubeflow_enhanced_v1` 采用“指令侧锚定网格标签”的方案，**实机复现多格同框，已证伪**；默认模板改为 `tubeflow_storyboard_v2`，现为 `v3`。**不要再用“加强提示词/锚定格子标签”作为唯一方案。**
- `USER_DECISIONS §20`：提示词编译器只影响新建尝试，不回填存量项目。
- `USER_DECISIONS §26`：画面级质检兜底已立项，但**范围仅“均匀黑区/空 Panel 检测”**，不识别格线、`SHOT` 文字或并列多机位。
- `docs/H3_PROMPT_AB_PLAN_20261015.md §40`：候选臂出现多格同框/网格泄漏即判该变量证伪。

## 5. 链路与代码入口

1. **故事板图生成**：`backend/src/tubeflow_test/application/storyboard_prompt.py`
   - L79：1 格时要求 “single full-frame panel with no grid lines”
   - L86–90：多格时明确行列、阅读顺序、空 Panel 涂黑
   - L104–114：**每格内必须逐字打印 caption 文本**（caption 含 `SHOT n · 起止秒`，见 `domain/storyboards.py` L556）
   - → 这是“烧进图里的文字”的来源，也是 H1 假设的核心。
2. **网格布局**：`backend/src/tubeflow_test/domain/storyboards.py`
   - L30–57：`GRID` 表、`grid_layout_for()`、`panel_row_column()`；5 格用 2×3 + 第 6 格纯黑。
3. **视频提示词编译/校验**：`backend/src/tubeflow_test/video_prompt/domain.py`
   - L149：`_validate_shot_labels`
   - L178–186：`Multi-panel storyboard shot plan = <Picture 2>` 绑定句
   - L245–265：强制包含 “Treat each panel…”、`No storyboard grid`、`split screen`
   - 注意：校验只保证“文本里有这几句”，**不保证模型遵守**。
4. **H3 请求与工作流选择**：`backend/src/tubeflow_test/video_generation/domain.py`、`engine.py`
   - 参考图绑定：角色图 = `<Picture 1>`，故事板 = `<Picture 2>`。
5. **画面 QC**：`backend/src/tubeflow_test/adapters/video_content_qc.py`
   - L119–141：把帧切成 `grid_rows × grid_columns` 小格，统计亮度/方差，用于黑区/空 Panel 判定。
   - `video_generation/orchestration.py` L130、L1782：`video_content_qc_sampler_error` 等错误码。
   - → 现有 QC **没有**“格线/文字/多机位”检测能力。

## 6. 待验证假设（按优先级）

| ID | 假设 | 最小验证 |
| --- | --- | --- |
| H1 | 故事板格内的 `SHOT n` 说明文字被模型当成“要渲染的内容” | 把同一张故事板的 caption 区域涂掉/裁掉，同 seed 重跑 |
| H2 | 多格版式本身被模型当作构图（与文字无关） | 同内容做成 1 格 vs 3 格，同 seed 对照 |
| H3 | 提示词里反复出现 “panel” 反而强化了“多格”概念 | 去掉 `panel` 词，改写为 “single continuous frame”，同 seed 对照 |
| H4 | 负规则放在末尾、被长提示词稀释 | 把负规则前置到 `detailed_description` 开头，同 seed 对照 |
| H5 | 与 seed 相关，换 seed 可能消失 | 同配置换 3 个 seed |
| H6 | H3 对“多图参考/多格参考”的固有偏好 | 在 ComfyUI 直接跑最小工作流（固定图 + 极短 prompt），排除业务提示词干扰 |

## 7. 建议实验矩阵（成本可控）

固定：项目第 2 组、294 帧、seed 123456789、同一角色图、同一提示词（除非该臂要改）。

| 臂 | 故事板 | 提示词 | 目的 |
| --- | --- | --- | --- |
| A | 原 3 格（含 caption） | 原样 | 已知失败基线 |
| B | 3 格，caption 涂黑 | 原样 | 验证 H1 |
| C | 只裁第 1 格 | 原样 | 验证 H2 |
| D | 3 格无边框拼接 | 原样 | 验证格线影响 |
| E | 原 3 格 | 去 panel 词 + 负规则前置 | 验证 H3/H4 |
| F | 原 3 格 | 原样，换 3 个 seed | 验证 H5 |

每臂只跑 1 组（约 4 分钟基线 / 13–19 分钟 HD），先全部用基线 1056×608 做筛查，命中结论后再用 HD 复验 1 次。

判定：抽 5 帧（1/3/6/9/12 秒）人工看；同时记录是否出现格线、`SHOT` 文字、并列机位。

## 8. 如果模型侧无法根治：建议的工程防线

新增一个与黑区 QC 并列的 **multi-panel leak 检测**：

1. 抽 6–8 帧，做两件事：
   - **格线检测**：列/行方向的梯度能量在固定位置出现周期峰（2–3 等分），且持续 ≥3 帧；
   - **文字检测**：OCR（轻量模型）命中 `SHOT\s*\d` 或带时间区间的说明。
2. 命中后：
   - 自动换 seed 重试 1 次（成本上限 1 次）；
   - 仍命中 → 标记 `multi_panel_leak`，该组暂停并暴露到页面（不要静默通过）。
3. 审计：记录命中帧、检测器版本、重试 seed、最终结果，便于统计发生率。
4. 不要用“自动裁掉一格”或“静默合并”处理（违反 `USER_DECISIONS §5.7`）。

## 9. 验收标准

- [ ] 至少 20 组抽样（含第 2 组）重跑，无同框；
- [ ] 注入 `runtime/operations/multi_panel_leak_20261020/` 的失败样本，检测器 100% 命中；
- [ ] 正常样本误报率 < 2%；
- [ ] 命中后的重试/暂停不阻塞整条 AUTO，状态在页面可见；
- [ ] 不改变 HD 工作流与 1080p 合成链路的行为。

## 10. 不要做

- 不要回退或修改已上线的 HD/1080p 方案（`DEPLOYMENT §24`）；
- 不要回填存量项目（`USER_DECISIONS §20`）；
- 不要把“加强提示词”当唯一解（§17 已证伪）；
- 不要自动截断/静默合并（§5.7）；
- 不要在没有基线对照的情况下直接改故事板生成模板。

## 11. 复现方式（节点 2，`ob6kefgwgyqkpy33`）

素材与脚本在节点的 `/tmp` 与 `/root/ComfyUI/output/tubeflow-test/`：

```bash
# 基线（1056×608）
python3 /tmp/h3_ab.py --mode base \
  --prompt-file /tmp/prompt_g2_pix.txt \
  --character tubeflow-test/ab_character.png \
  --storyboard tubeflow-test/ab_g2_storyboard.png \
  --length 294 --seed 123456789 \
  --prefix tubeflow-test/repro_g2

# HD（1600×896）
python3 /tmp/h3_ab.py --mode hd ...（同上，--mode hd）
```

注意：该节点是生产集群节点之一，跑实验前先确认三台引擎无在途任务，避免抢占。
