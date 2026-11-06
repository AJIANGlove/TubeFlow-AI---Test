# 抖动 A/B 执行报告的复核与后续决定（2026-11-06）

| 项 | 内容 |
| --- | --- |
| 对象 | 实施方报告「PR #14 合并 → §52 部署 → A/B → 测量」。合并提交 `f47a5c5`，证据与文档提交 `36123e9`。 |
| 性质 | 只读复核：在 Claude 云端工作区读取仓库证据并重算；另用用户此前提供的两段 484746c4 原片检查帧类型。没有访问部署机，没有调用 Provider。 |
| 结论 | 报告数据与证据一致，判定「无效或不确定」成立。容器名一项与实施方自己的部署记录矛盾，需更正。用户 2026-11-06 确认后续决定，记入 USER_DECISIONS §51 第 4、5 项。 |

## 1. 核对结果

- **合并**：`f47a5c5` 的代码树与 PR head `ba7dbac` 相同（`1d835a0d…`）。PR #14 已由 GitHub 在 03:16:36Z 标为 merged。
- **A/B 数据**（`Pending issues/2026-11-06/evidence/jitter_ab/ab/results/measure.json`）：
  - 8 条全部 `ARCHIVED`；
  - 4 对变体提示词去掉该句后与基线逐字相同；
  - 额度：H3 8/8，DeepSeek 4/16；
  - 无自动重提；第 7、12 组的当前视频不变。
- **FINAL_ACCEPTANCE §46** 的勾选与证据相符。
- **集成测试**：经 SSH 隧道连到部署机的线上 Postgres 运行，夹具会新建临时 `contract_*` schema 并在结束时删除（`tests/integration/conftest.py`）。11-03 已有此先例；需确认没有遗留的 schema。

## 2. 需要更正

**web 容器名。**
- 实施方聊天报告称线上 web 容器名为 `tubeflowai-cgpq-dor-2`；但 `deploy/README.md` 与 `docs/DEPLOYMENT.md` §42 写的实际名是 `tubeflowai-test-web-1`。
- `tubeflowai-test-web-1` 正是项目 `tubeflowai-test` 的 compose 默认名，记录中却称其「不是默认名」，前后矛盾。
- `docker rename` 被拒的报错是「与当前名字相同」，说明容器本来就是标准名。
- 结论：除非能拿出原始 `docker ps -a` 输出，否则这条「现场偏差」应从 §42 删去。

## 3. 补充分析

1. **同节点的两对几乎不变；变化大的两对都跨节点，且方向相反。**

   | 组 | seed | 节点（基线 / 变体） | 基线 → 变体 |
   | --- | --- | --- | --- |
   | 7 | …159 | h3-new-3 / h3-new-3 | 0.53 → 0.50 |
   | 12 | …159 | h3-new-2 / h3-new-2 | 0.97 → 1.00 |
   | 7 | …158 | h3-new-2 / h3-new-3 | 0.49 → 0.30 |
   | 12 | …158 | h3-new-3 / h3-new-2 | 0.11 → 0.32 |

   - 这句话确实改变了画面：第 12 组 S2 变体出现人脸，两臂接触表的平均灰度差为 20.8。但隔帧停顿占比没有变。
   - 用户确认各节点用同一镜像部署。所以跨节点的差异更可能来自 GPU 运行的不确定性，而不是配置不同（推断）。
   - 只有 2 + 2 对，无法定量。
2. **同组之间差距极大。** 第 12 组两条基线为 0.11 和 0.97：seed 不同，编译出的提示词也不同（两镜的运镜顺序互换）。
3. **静止机位不是决定因素。**
   - 第 7 组静止镜四次为 0.67–1.0；
   - 第 12 组 S1 的静止镜两次都是 0.0；
   - 第 12 组 S2 的推镜达 0.95。

   此前「镜头运动会掩盖、静止机位会暴露隔帧停顿」的推断只得到部分支持。
4. **第 7 组第 1 镜（推镜 6%）四次 HF 都在 1.15–1.58**（2026-11-06 后续核实：整帧有 1–3 px 的不规则微颤，没有大突跳；见 `MOTION_METRICS_ROLLOUT_REVIEW_20261106.md` 第 2 节），跨 seed、跨节点都能重现；第 12 组的推镜只有 0.13–0.20。是否为真实的机位突跳，需要按原帧率目视确认（相位相关遇到缩放可能失真）。
5. **指标不是编码造成的假象。** 用户提供的两段 484746c4 原片全为 P 帧，没有 B 帧，「隔一帧才动」不是帧类型交替造成的。只检查了这两段。

## 4. 用户决定（2026-11-06）

| 建议 | 决定 | 去向 |
| --- | --- | --- |
| 结束「提示词加一句」这条路，变体不采用 | 同意 | USER_DECISIONS §51 第 4 项 |
| 归档时记录隔帧停顿占比与 HF，只观测、不拦截 | 同意；HF 依赖选择「镜像加库」 | §51 第 5 项；FINAL_ACCEPTANCE §47；PR「video motion metrics」 |
| 只读核对各 H3 节点配置 | 用户确认：同一镜像部署，无需核对 | §51 第 4 项 |
| 实施方补三件事：更正 §42 容器名；确认无遗留 `contract_%` schema；目视第 7 组第 1 镜 | 同意 | 下一轮提示词 |

## 5. 观测数据的查看方式（只读）

```sql
select a.project_id,
       count(*) filter (where a.probe_evidence_json->'motion_metrics'->>'status' = 'ok') as measured,
       count(*) filter (where a.probe_evidence_json->'motion_metrics'->>'status' <> 'ok') as not_measured,
       count(*) filter (where (a.probe_evidence_json->'motion_metrics'->>'held_share')::numeric >= 0.2) as held_ge_02,
       count(*) filter (where (a.probe_evidence_json->'motion_metrics'->>'jitter_shots')::int > 0) as with_jitter_shots
from video_generation_attempts a
where a.probe_evidence_json ? 'motion_metrics'
group by a.project_id
order by measured desc;
```

各字段的含义见 `backend/src/tubeflow_test/video_generation/motion_metrics.py`。0.2 和 0.25 只是参考线，不是门槛。
