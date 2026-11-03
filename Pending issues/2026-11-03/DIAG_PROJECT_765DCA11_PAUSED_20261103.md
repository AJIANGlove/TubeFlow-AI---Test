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

## 5. 结论（2026-11-03 用户复核后修订）

**部分修正原结论：**

1. 首轮失败（全局计划 v7 提示词、句子 ID 非 UUID）仍判定为非 v9 相关——v9 没有改动全局计划提示词，该错误码历史上仅此 1 次。
2. **修正**：第 2、3 轮失败落在 `project-director-unit-v9` 单元上；v9 的「3 句以上单元至少 2 镜」使单元内镜数更多，**客观上提高了相邻多样性校验的失败概率**，不能再用「v5/v7 也有」简单抵消——原文的近 30 天计数未按项目归一化，不能作版本间比例证据。
3. 导演失败单元 `attempt_count=3` 但为同一幂等键、同一提示词、temperature 0.0 的重发，**3 次等效 1 次**。
4. 行动项：先做一个导演修复（再开始第二轮 A/B 的新项目部分）；765dca11 保持 PAUSED，未动。

建议（不执行）：该项目 AUTO 保持 PAUSED 等用户测试；若恢复后第 20 组仍连续多样性失败，值得单独给该组加导演重试预算或人工改稿，而不是归因 v9。

## 6. 后续

按用户规则：结论为“非 v9”，新项目由用户创建（脚本 30–50 句），然后执行 `VIDEO_QUALITY_AB_ROUND2_PLAN_20261103.md`（名义 54 次、上限 90，已获原则批准）。本轮诊断未产生任何真实 Provider 调用。
