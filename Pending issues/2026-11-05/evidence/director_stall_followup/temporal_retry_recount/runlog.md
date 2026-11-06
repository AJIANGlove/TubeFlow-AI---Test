# 复算运行日志（只读）

- 生成时刻：`2026-11-06T01:57:11.689797Z`
- 执行位置：部署机 worker 容器 `tubeflowai-test-worker-1`（temporalio **1.35.0**），脚本经 stdin 喂入，未落地到容器
- 命名空间：`default`；服务地址：`tubeflowai-test-temporal-1:7233`
- 只读 API：`ListWorkflows` / `GetWorkflowHistory` / `DescribeWorkflowExecution`；无 signal/terminate/cancel/start

## 枚举查询原文

```
primary(W0)    : StartTime >= "2026-11-05T12:29:46Z" and StartTime < "2026-11-05T13:59:46Z"
supplement(W0) : StartTime < "2026-11-05T12:29:46Z" and CloseTime >= "2026-11-05T12:59:46Z" and CloseTime < "2026-11-05T13:59:46Z"
running(W0)    : ExecutionStatus = "Running"
primary(W1)    : StartTime >= "2026-11-05T13:29:46Z" and StartTime < "2026-11-05T14:59:46Z"
supplement(W1) : StartTime < "2026-11-05T13:29:46Z" and CloseTime >= "2026-11-05T13:59:46Z" and CloseTime < "2026-11-05T14:59:46Z"
running(W1)    : ExecutionStatus = "Running"
```

## W0
- 窗口：`2026-11-05T12:59:46Z` – `2026-11-05T13:59:46Z`（lookback start `2026-11-05T12:29:46Z`）
- listings：{"primary": 112, "supplement": 15, "running": 0}；unique=127；truncated=False；supplement_unavailable=None
- 类型直方图：{"GridSheetGenerationWorkflow": 69, "GridSheetRecoveryWorkflow": 2, "GroupVideoRecoveryWorkflow": 52, "ProjectDirectorRunWorkflow": 4}
- 抓取：attempted=127 ok=127 failure_count=0
- scan：{"histories_scanned": 127, "started_events": 8943, "attempt_gt_1_total": 0, "in_window": 0}
- totals：{"timeout_retries": 0, "application_retries": 0, "other_retries": 0}

## W1
- 窗口：`2026-11-05T13:59:46Z` – `2026-11-05T14:59:46Z`（lookback start `2026-11-05T13:29:46Z`）
- listings：{"primary": 136, "supplement": 13, "running": 0}；unique=149；truncated=False；supplement_unavailable=None
- 类型直方图：{"GridSheetGenerationWorkflow": 71, "GridSheetRecoveryWorkflow": 2, "GroupVideoRecoveryWorkflow": 75, "ProjectDirectorRunWorkflow": 1}
- 抓取：attempted=149 ok=149 failure_count=0
- scan：{"histories_scanned": 149, "started_events": 11505, "attempt_gt_1_total": 0, "in_window": 0}
- totals：{"timeout_retries": 0, "application_retries": 0, "other_retries": 0}

## 命令

```bash
docker exec -i tubeflowai-test-worker-1 python - --mode fetch --window W0 \
  --start 2026-11-05T12:59:46Z --end 2026-11-05T13:59:46Z --out /tmp/live_W0.json \
  < "Pending issues/2026-11-05/evidence/director_stall_followup/tools/recount_retries.py"
docker exec -i tubeflowai-test-worker-1 python - --mode pending --out /tmp/pending_now.json < …recount_retries.py
python recount_retries.py --mode compare --w0 w0.json --w1 w1.json \
  --t-dep-start 2026-11-05T13:59:14Z --t-dep-end 2026-11-05T13:59:46Z --out compare.json
```
