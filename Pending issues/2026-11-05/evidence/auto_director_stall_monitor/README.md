# 导演阶段停滞监控证据

采集方式：部署机 postgres 容器内只读事务（`BEGIN TRANSACTION READ ONLY`），
SQL 见 `runtime/operations/director-stall-monitor-20261105/collect.sql`。

- `incidents/` 每次中断事件的完整快照（含 auto 运行、导演 run、失败单元明细）
- `resumes/` 每次恢复的请求、结果与立即复核
- `timeline.csv` 机器可读时间线（追加写入）

字段字典与取证边界见主文档第 4 节。含密钥特征的值已在落盘前脱敏。
