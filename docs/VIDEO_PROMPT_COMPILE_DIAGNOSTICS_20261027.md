# 视频提示词编译失败可观测性（2026-10-27）

## 1. 背景

项目 `22dbcf09` 组 17 在自动链路第 07 步失败，暂停原因 `video_failed:intent_rejected`，
批次项信息：

```
Video prompt stayed invalid after 2 repairs: h3_prompt_storyboard_retention_invalid
```

该组**没有任何 `video_generation_attempts` 记录**——失败发生在「提示词编译」阶段，
尚未提交模型，因此没有视频生成费用；也正因为没有 attempt，编译输出、修复链、
模型原始文本**全都没有落库**，导致只能靠重跑猜原因。

## 2. 本次改动（只加证据，不改任何规则）

- 迁移 `0045`：新增只增不改的诊断表 `video_prompt_compilations`
  （owner/project/group/attempt、strategy、model、payload 指纹、attempt_index、reason、
  output_sha256、output_text 前 20k 字符、created_at）。
- `adapters/persistence/video_prompt_diagnostics.py`：`SqlVideoPromptDiagnostics.record`，
  写入失败只记日志，**绝不影响生成主流程**。
- `video_prompt/compiler.py`：新增可选 `diagnostics` 汇与 `compile(context=…)`；
  每次尝试（provider 响应错误 / 合同校验失败）各记一行，含原因码与模型原始输出。
- `video_generation/orchestration.py`：在 `compile_video_prompt` 绑定
  owner/project/group/attempt 上下文。
- `api/app.py`、`workflows/worker.py`：装配诊断写入器。
- `video_prompt/fake_compiler.py`：替身接受 `context`，保持测试与真实签名一致。

校验规则、修复次数（AUTO 2 次）、暂停语义（一组失败即整批暂停）**均未改动**。

## 3. 验证

- 新增单测：三次校验失败各落一行（含输出原文与 sha256）；provider 响应错误落一行且
  `output_text` 为空。
- 迁移：在部署机临时库 `tubeflow_migcheck_0045` 上跑通 `upgrade head`（0045）与
  `downgrade 0044`（表与索引均被删除），随后删除临时库。
- 后端 `tests/unit`：**2101 passed / 8 skipped / 1 failed**；唯一失败仍是
  `test_production_container_contract.py::test_dockerfile_uses_migration_gated_api_console_and_ffmpeg_smoke`
  （`backend/Dockerfile` 早前改阿里云源的既存漂移，与本次无关）。
- `ruff check` 本次改动文件全部通过。

## 4. 取证操作（待执行）

组 17 需重跑才能产生诊断行：

```
POST /projects/{id}/group-video-batch/generations
{"group_ids": ["<组17 group_id>"], "force": true}
```

该接口与「恢复自动执行」一样走 Bearer 会话鉴权。重跑后查询：

```sql
select attempt_index, reason, output_sha256, output_text
from video_prompt_compilations
where project_id='22dbcf09-4c18-4fd3-8fb7-d60732c78431'
order by created_at;
```

## 5. 回滚

- 代码：镜像回退到 `sha256:63f559257d6cc64ee44117c0cd80d603a5d326b19dab65dccf0fba4b7ec315a7`
  （上一版，无诊断）。
- 迁移：`alembic downgrade 0044`（仅删诊断表，不影响业务表）。

## 6. 取证结论与对症修复（2026-10-27 15:2x）

重跑组 17 后 `video_prompt_compilations` 记录到首轮被拒的原文：模型把故事板写成了
**主体编号 `(S2)`**：

```
subject_definitions:
- (S1): … Lumen …
- (S2): The complete two-panel dark oil-painting storyboard, …
retention_analysis:
- (S1) — fully_preserved: …
- (S2) — shot_plan_reference_only: retain its shot order, …
```

校验要求逐字 `- Picture {N} — shot_plan_reference_only:`，`(S2)` 不匹配 → 拒收；
修复一次后改对，编译通过（attempt `bda8e5a0`，最终 prompt 3,695 字符且含该行）。

**对症修复（仅 v3 提示词，不动校验）**：`templates.py` 规则 5 要求故事板条目以
`- Picture {N}:` 开头并明确「故事板不是出场主体，严禁分配 (Sx) 编号」；规则 7 补
「不得把主语写成 (Sx)」。v2 规则保持不变。新增两条模板回归测试（v3 含约束、v2 不含）。
