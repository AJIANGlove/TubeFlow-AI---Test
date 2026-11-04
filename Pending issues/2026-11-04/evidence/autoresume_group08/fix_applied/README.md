# 临时修复执行记录：第 8 组 AUTO「恢复」无效

- 时间：2026-11-04 22:14–22:16 CST（14:14–14:16 UTC）
- 授权：用户明确指令「你直接帮我改，然后恢复」
- 环境：镜像 `8278db3ed79d`、alembic `0046`；web=`tubeflowai-test-web-1`、worker=`tubeflowai-test-worker-1`
- 范围：仅项目 `2c291aba-085d-499d-bd32-b3a4d75f252f` 的第 8 组候选一行 + 一次标准 resume
- 未做：未改代码、未重建镜像、未重启容器、未改动其他项目

## 1. 备份（部署机与本地各一份）

部署机：`/home/edy/tubeflowai-test/runtime/operations/auto-resume-fix-20261104/`

| 文件 | 内容 |
| --- | --- |
| `group08_candidate.before.json` | 修改前第 8 组候选整行（含 shots/conflicts） |
| `run.before.txt` | 修改前 `auto_pipeline_runs` 整行（PAUSED / round 8 / payload） |

本地同名副本在本目录，另含 `group08_candidate.after.json` 与 `after_state.txt`。

## 2. 变更（恰好 1 行）

```sql
update project_director_group_candidates
set shots_json = jsonb_set(shots_json::jsonb, '{5,camera_viewpoint}', '"EYE_LEVEL"'::jsonb)::json
where project_id = '2c291aba-085d-499d-bd32-b3a4d75f252f' and group_sequence = 8;
-- UPDATE 1
```

第 6 镜 `(scene, shot_size, camera_viewpoint)`：
`(빙수점 - 계산대 장부, CLOSE_UP, OVERHEAD)` → `(빙수점 - 계산대 장부, CLOSE_UP, EYE_LEVEL)`

复算结果：`composition 非相邻重复 = []`（PASS），event 键无重复。

## 3. 恢复动作与结果

在 web 容器内调用产品同一实现（等价于 `POST /identity/projects/{id}/auto-pipeline/resume`）：

```python
SqlAutoPipelineRunRepository(db.session).resume_paused(owner, project_id, now=datetime.now(UTC))
# -> resumed: RUNNING round: 9 reason: None
```

随后调度器 `advance` 自动推进（无需人工再点）：

| 时间 (UTC) | 事实 |
| --- | --- |
| 14:15:41 | 运行置 RUNNING，`authorization_round` 8 → 9 |
| 14:15:43 | 第 8 组草稿生成并落库：`d89c8794-43fb-473f-90a6-386bad1fc390`，`kind=APPLIED`、`review_state=NOT_REQUIRED` |
| 14:16:03–14:16:14 | 107 组故事板生成记录创建，107 个 `GridSheetGenerationWorkflow` 启动 |
| 14:16:14 | payload phase 由 `storyboard_drafts` 推进到 `storyboard`，无新的暂停原因 |

校验：

- 草稿总数 `106 → 107`，`REQUIRED = 0`
- 第 8 组草稿镜头第 6 个确为 `EYE_LEVEL`（已直接读 `storyboard_director_revisions.shots_json` 核对）
- 生成进度（22:32 CST）：`SUBMITTED 28 / SUBMITTING 8 / INTENT_PERSISTED 71`，持续增长，无失败码

## 4. 回滚

- 候选行：用 `group08_candidate.before.json` 的内容覆盖 `shots_json`（同一 WHERE 条件）即可还原。
- **注意**：已生成的草稿修订是不可变资产且已 `APPLIED`，回滚候选行不会撤销该草稿；若需彻底回退，应按“草稿不可变、重建修订/切换指针”的既有规则另行处理，不属于本次临时修复范围。

## 5. 已知遗留

- 代码缺陷未修复（物化阶段 `if ... and not blocking` 短路了 `DirectorPlan` 校验；`resume` 对不可自愈原因码仍是幂等空转），其他同型数据仍可能复现同一暂停。
- 本次只改候选行，未回写 `project_director_materializations` 快照，二者不再严格对应；如需重新物化需单独评估。
- 人工改动未写入 `USER_DECISIONS.md`，不构成产品决策，仅为本项目的临时解卡。
