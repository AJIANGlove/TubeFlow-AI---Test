# 第 2 轮执行中的报错原文（2026-11-10）

## 1. `collect_r2.py` 首次运行（未修改前）

`docker exec -e OUT=/tmp/motion_r2 tubeflowai-test-web-1 python /tmp/r2/collect_r2.py /tmp/r2/spec.json` 退出码 1，`/home/edy/r2/collect.log` 尾部：

```
HINT:  Perhaps you meant to reference the column "video_generation_attempts.updated_at".
[SQL: select attempt_id::text as attempt_id, idempotency_key, node_id, stage, archive_stage, error_code, created_at, updated_at, input_snapshot_json, request_json, probe_evidence_json, archived_asset_json, output_sha256 from video_generation_attempts where group_id = cast(%(g)s as uuid) and (attempt_id = cast(%(s)s as uuid) or (idempotency_key like 'motion-r2-%%' and input_snapshot_json -> 'experiment' ->> 'name' = 'prompt-edit-ab-v1')) order by created_at]
[parameters: {'g': '6e714ebb-8150-51e6-83b8-574581f3915a', 's': '4bb5c37b-eb46-4fb0-b4bf-fda6871b7eb5'}]
(Background on this error at: https://sqlalche.me/e/21/f405)
```

根因：`video_generation_attempts` 表没有 `created_at` 列（只有 `updated_at`）。

处理：把 `collect_r2.py` 的 select 与 order by 改用 `updated_at`，记录里的 `created_at` 置 `None`（提交/终态时刻以 `submit_v2/` 日志为准）；其余逻辑未改。修正后同一命令退出码 0，`{"records": 14, "errors": []}`。

## 2. 第 1 批只读演练（驱动脚本路径拼错，非交接包问题）

8 条报：

```
ERROR FileNotFoundError code=None message=None text=[Errno 2] No such file or directory: '/tmp/r2/prompts/116_m1.txt'
```

原因：我的 `run_dryrun.sh` 拼成 `${g}_${arm}.txt`，实际文件名是 `g${g}_${arm}.txt`。修正后 12 条全部 `DRYRUN_OK`；第 1 批日志保留在 `round2/results_v2/dryrun_v2_attempt1/`。

## 3. 部署机仓库无法 `git pull`（交接包 §2.4）

`/home/edy/tubeflowai-test` 当前分支 `codex/01a049161e167002ac505153d422f0b3`、HEAD `f206c54`、**未配置 remote**、351 个已修改文件。为避免覆盖部署机上的在途状态，未执行 `git pull`；第 2 轮文件改用 `tar` 传到 `/home/edy/r2/` 再 `docker cp` 进容器，容器内四个提示词 sha256 与表格逐一相同。
