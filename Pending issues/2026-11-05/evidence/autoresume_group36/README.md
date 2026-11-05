# 临时修复记录：第 36 组（项目 23c59183）

- 时间：2026-11-05 13:17–13:19 CST（05:17–05:19 UTC）
- 授权：用户指令「处理」
- 环境：镜像 `8278db3ed79d`、alembic `0046`；web=`tubeflowai-test-web-1`、worker=`tubeflowai-test-worker-1`
- 范围：仅项目 `23c59183-7165-4c33-a161-7ecf28964c78` 的第 36 组候选一行 + 一次标准 resume
- 未做：未改代码、未重建镜像、未重启容器、未动其他项目

## 变更

```sql
update project_director_group_candidates
set shots_json = jsonb_set(shots_json::jsonb, '{5,camera_viewpoint}', '"HIGH_ANGLE"'::jsonb)::json
where project_id='23c59183-7165-4c33-a161-7ecf28964c78' and group_sequence=36;
-- UPDATE 1
```

第 6 镜 `EYE_LEVEL` → `HIGH_ANGLE`，与第 1 镜的构图三元组解耦；其余 5 镜未改。

## 备份

部署机 `/home/edy/tubeflowai-test/runtime/operations/auto-resume-fix-20261105/`，本地副本见本目录。

## 结果

- `authorization_round` 7 → 8；状态 RUNNING、reason 清空
- 第 36 组草稿 `d2a624f5-88a6-45c7-ac85-3ca156764659`（`APPLIED` / `APPROVED`）
- 草稿 78 → 79、`REQUIRED = 0`；阶段推进到 `storyboard`
- 79 个故事板生成任务已入队并开始提交（05:19 UTC：SUBMITTED 11 / SUBMITTING 8 / INTENT_PERSISTED 60）
- 恢复后无新的 `auto pipeline paused`

## 回滚

用 `group36_candidate.before.json` 覆盖 `shots_json` 即可还原候选；已 `APPLIED` 的草稿修订不可变，不会随之撤销。
