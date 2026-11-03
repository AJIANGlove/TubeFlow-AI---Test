# 项目 765dca11 导演暂停只读诊断（2026-11-03）

范围：只读；未恢复、未重生成、未修改任何数据。结论在 §5。

## 1. AUTO 暂停原因与时间

- 运行 `f586e469-1ff1-48ca-b2ee-ca7ce2de75fe`（轮次 3），状态 `PAUSED`，`reason_code=director_preplan_failed`。
- 由 RUNNING 转 PAUSED 的时间：**2026-11-03 09:10:19.600 UTC（本地 17:10:19）**（payload `paused_at_ms` 与行 `updated_at` 一致）。
- 直接触发：第三条导演运行 `9b2920f3` 在 09:03 创建、随后失败（`director_output_adjacent_diversity_insufficient`），AUTO 随即暂停。

## 2. 导演运行明细

| run | 创建时间(UTC) | 状态 | 单元总数 | SUCCEEDED | FAILED | INTENT_PERSISTED |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `798d3ebc` | 07:52:37 | FAILED | 81 | 0 | 1 | 80 |
| `f4be7cfc` | 08:19:43 | FAILED | 81 | 21 | 1 | 59 |
| `9b2920f3` | 09:03:25 | FAILED | 81 | 25 | 1 | 55 |

单元全部是 `project-director-unit-v9`（v9 在该项目生效）+ 每轮 1 个 `project-director-global-plan-v7`。

## 3. 失败单元逐条（诊断 JSON 仅安全诊断，无模型文本）

| run | 序 | 类型 | prompt_ref | 尝试次数 | failure_code | response_diagnostics_json |
| --- | ---: | --- | --- | ---: | --- | --- |
| `798d3ebc` | 1 | GLOBAL_PLAN | project-director-global-plan-v7 | 3 | `director_output_sentence_id_uuid_invalid` | `{}` |
| `f4be7cfc` | 20 | SHOT_PLAN | project-director-unit-v9 | 3 | `director_output_adjacent_diversity_insufficient` | `{"adjacent_pair_after_order":3,"changed_axes":1,"previous_axes":"PERFORM_ACTION|PROCESS|SAME_MOMENT|SAME_LOCATION","current_axes":"PERFORM_ACTION|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION"}` |
| `9b2920f3` | 20 | SHOT_PLAN | project-director-unit-v9 | 3 | `director_output_adjacent_diversity_insufficient` | `{"adjacent_pair_after_order":2,"changed_axes":1,"previous_axes":"REVEAL_EVIDENCE|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION","current_axes":"ESTABLISH_CONTEXT|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION"}` |

全部 243 个单元的逐条状态：`Pending issues/2026-11-03/DIAG_765dca11_units.txt`。

## 4. 同失败码的横向分布（近 30 天）

`director_output_sentence_id_uuid_invalid`：
- 全库历史仅此 1 次，且落在 `project-director-global-plan-v7`（全局计划提示词，v9 未改动）。
- 没有任何 v8 项目（或任何其他项目）出现过该错误码。

`director_output_adjacent_diversity_insufficient`（近 30 天，按 prompt_ref）：
- v5：18 次；v7：11 次；v9：3 次（本项目 2 次，另一项目 `146ae08c` 1 次）。
- 该错误码在 v5/v7 上更常见，是既有内容相关失败，非 v9 引入。

## 5. 结论

**与 v9 无关。**

1. 首轮失败是全局计划层（v7 提示词）的输出把句子 ID 写成了非 UUID，属于偶发的模型输出结构错误，且该错误码历史上只此一次；v9 没有改动全局计划提示词。
2. 后两轮失败是同一单元（第 20 个分镜组）的相邻多样性不足：相邻两镜只差 1 个轴（REVEAL_EVIDENCE→ESTABLISH_CONTEXT，均为 OBJECT_OR_EVIDENCE/SAME_MOMENT/SAME_LOCATION）。该失败码在 v5/v7 上同样常见（30 天 29 次），属于内容相关，不是 v9 新增。
3. v9 单元的成功率没有异常信号（本轮 20/21 与 24/25 成功，直到卡点单元）。

建议（不执行）：该项目 AUTO 保持 PAUSED 等用户测试；若恢复后第 20 组仍连续多样性失败，值得单独给该组加导演重试预算或人工改稿，而不是归因 v9。

## 6. 后续

按用户规则：结论为“非 v9”，新项目由用户创建（脚本 30–50 句），然后执行 `VIDEO_QUALITY_AB_ROUND2_PLAN_20261103.md`（名义 54 次、上限 90，已获原则批准）。本轮诊断未产生任何真实 Provider 调用。
