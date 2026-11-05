# 自动生产「恢复」无效（第 36 组）— 同型第二例（2026-11-05）

项目：`23c59183-7165-4c33-a161-7ecf28964c78`（`A Day in the Life of a Roman Plebeian — Nothing Like the Movies`，en-US / 16:9，79 组 / 475 句）
状态：已完成临时解卡（只改 1 行候选 + 一次标准 resume），未改代码、未重建镜像、未重启容器。
同型首例：`Pending issues/2026-11-04/AUTO_RESUME_NOOP_GROUP08_20261104.md`（机制、代码位置、缺陷分析见该文档 §5）。

---

## 1. 现象

- 项目页横幅：`自动生产已暂停：导演方案校验未通过，需人工修正`；`导演方案校验未通过的分组：36（共 1 组）`；`失败原因：第 36 组：director plan repeats an event or composition across the group`；`原因码 director_validation_failed · 当前阶段 导演草稿播种`。
- 进度：真实配音 475/475、导演规划 60/60，故事板 0/79、组视频 0/79。
- 点击「恢复自动执行」无进展（与 8 号项目一致）。

## 2. 处理前现场（部署机只读取证）

| 项 | 值 |
| --- | --- |
| 运行行 | `863939ce-fc16-4bd6-88e6-32157461fbfa`，`PAUSED / director_validation_failed / authorization_round=7` |
| payload | `phase=storyboard_drafts`、`group_sequences=[36]`、`failure_messages={"36":"director plan repeats an event or composition across the group"}` |
| 物化 | `REVIEW_REQUIRED`，`controlled-semantic-lineage-remap-v5`，`2026-11-05 03:58:13Z` |
| 分组 | 候选 79、草稿 78、故事板生成 0；**唯一缺草稿的是第 36 组**（`review_state=REQUIRED`、`final_revision_id=NULL`） |

## 3. 违规点（与 8 号项目同一不变量）

第 36 组 6 个镜头中，第 1 与第 6 镜的 `(scene, shot_size, camera_viewpoint)` 完全相同，位置差 5：

| # | scene | shot_size | camera_viewpoint |
| --- | --- | --- | --- |
| 1 | popina - shared table | MEDIUM | EYE_LEVEL |
| 6 | popina - shared table | MEDIUM | EYE_LEVEL |

两镜的 `subject_focus/semantic_event` 不同，所以是**构图重复**而非事件重复，命中 `domain/storyboards.py:358` 的 `repeats_non_adjacent(composition_keys)`。

## 4. 处理（2026-11-05 13:17–13:19 CST / 05:17–05:19 UTC）

### 4.1 变更（恰好 1 行，已备份）

```sql
update project_director_group_candidates
set shots_json = jsonb_set(shots_json::jsonb, '{5,camera_viewpoint}', '"HIGH_ANGLE"'::jsonb)::json
where project_id='23c59183-7165-4c33-a161-7ecf28964c78' and group_sequence=36;
-- UPDATE 1
```

第 6 镜 `EYE_LEVEL → HIGH_ANGLE`（`HIGH_ANGLE` 是既有合法枚举，第 38 组第 1 镜即在用；改后三元组 `(popina - shared table, MEDIUM, HIGH_ANGLE)` 与组内其余 5 镜均不同）。

复算：`composition 非相邻重复 = []`、`event 非相邻重复 = []`（见 `repro_output.txt`）。

### 4.2 恢复

容器内调用产品同一实现（等价 `POST /identity/projects/{id}/auto-pipeline/resume`）：

```python
SqlAutoPipelineRunRepository(db.session).resume_paused(owner, project_id, now=datetime.now(UTC))
# resumed: RUNNING round: 8 reason: None
```

### 4.3 结果（实测）

| 时间 (UTC) | 事实 |
| --- | --- |
| 05:18:09 | 运行置 RUNNING，`authorization_round` 7 → 8 |
| 05:18:11 | 第 36 组草稿生成：`d2a624f5-88a6-45c7-ac85-3ca156764659` |
| 05:18:41 | 阶段 `storyboard_drafts → storyboard`；79 份修订全部 `APPLIED`（22 APPROVED + 57 NOT_REQUIRED） |
| 05:19:02 | 79 个故事板生成任务：`INTENT_PERSISTED 60 / SUBMITTED 11 / SUBMITTING 8`，持续增长 |

- 草稿 78 → **79**，`REQUIRED = 0`
- 05:17 之后 web/worker 均无该项目的新 `auto pipeline paused`

## 5. 备份与回滚

- 部署机：`/home/edy/tubeflowai-test/runtime/operations/auto-resume-fix-20261105/`（`group36_candidate.before.json`、`after.json`、`run.before.txt`）
- 回滚：用 before.json 覆盖 `shots_json`（同一 WHERE）；但已生成且 `APPLIED` 的草稿是不可变资产，回滚候选不会撤销它。

## 6. 遗留（与首例相同）

代码层缺陷未修：物化阶段 `if ... and not blocking` 短路了 `DirectorPlan` 校验（未产出 `group_diversity_invalid`），`resume` 对不可自愈原因码仍是幂等空转。同一形态在 2026-11-04 与 11-05 已连续出现两例，建议按 11-04 文档 §9 修复并补回归测试。

## 7. 证据清单

`evidence/autoresume_group36/`

| 文件 | 内容 |
| --- | --- |
| `db_export.txt` | 项目、运行行、payload、物化、缺草稿分组、第 36 组镜头、恢复后统计 |
| `group36_candidate.before.json` / `.after.json` | 改前/改后候选整行 |
| `run.before.txt` | 改前运行行 |
| `after_state.txt` | 恢复后运行行、草稿、生成进度、暂停日志检查 |
| `repro_output.txt` | 按 `domain/storyboards.py` 规则复算的重复项与结论 |
