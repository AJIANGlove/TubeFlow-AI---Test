## 表 1 · 16 个导演运行里的全部 attempt > 1（全历史，不按窗口裁剪）

| # | run | activity | attempt | Scheduled (UTC) | Started (UTC) | sched→start s | prior started | timeoutType | message |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `5d25a3a4` | `advance_project_director` | 2 | 2026-11-05T09:39:04.485930Z | 2026-11-05T09:59:05.502513Z | 1201.017 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |
| 2 | `5d25a3a4` | `advance_project_director` | 2 | 2026-11-05T09:39:04.485888Z | 2026-11-05T09:59:06.499640Z | 1202.014 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |
| 3 | `5d25a3a4` | `advance_project_director` | 2 | 2026-11-05T10:07:25.487884Z | 2026-11-05T10:27:26.511409Z | 1201.024 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |
| 4 | `8697ee51` | `advance_project_director` | 2 | 2026-11-05T10:09:08.316992Z | 2026-11-05T10:29:09.328225Z | 1201.011 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |
| 5 | `8697ee51` | `advance_project_director` | 2 | 2026-11-05T10:09:08.317023Z | 2026-11-05T10:29:10.002111Z | 1201.685 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |
| 6 | `c80ea8bb` | `advance_project_director` | 2 | 2026-11-05T10:09:20.083710Z | 2026-11-05T10:29:21.112933Z | 1201.029 | false | `TIMEOUT_TYPE_START_TO_CLOSE` | activity StartToClose timeout |

合计 **6 次超时重试**（application 0、other 0）；扫描 16 份历史。

## 表 · W0 窗口（`2026-11-05T12:59:46Z` – `2026-11-05T13:59:46Z`）

- 枚举：primary **112**、supplement **15**、running **0** ⇒ 去重 **127** 个 workflow-run；截断 False，supplement 不可用：None
- 抓取：成功 **127** / 尝试 127，失败 **0**
- 扫描：Started 事件 **8943** 条，其中 attempt>1 **0** 条
- 结果：timeout **0**、application **0**、other **0**

分组表为空（窗口内没有任何 attempt>1 事件）。

## 表 · W1 窗口（`2026-11-05T13:59:46Z` – `2026-11-05T14:59:46Z`）

- 枚举：primary **136**、supplement **13**、running **0** ⇒ 去重 **149** 个 workflow-run；截断 False，supplement 不可用：None
- 抓取：成功 **149** / 尝试 149，失败 **0**
- 扫描：Started 事件 **11505** 条，其中 attempt>1 **0** 条
- 结果：timeout **0**、application **0**、other **0**

分组表为空（窗口内没有任何 attempt>1 事件）。

## 表 · 当前运行中工作流的 pending activities

- 采集时刻 `2026-11-06T01:56:43.144624Z`：运行中工作流 **0** 个，pending 总数 **0**，其中 attempt>1 **0**
- 采集时点没有运行中的工作流，pending 检查为空 —— **不代表健康**，只代表当时没有可检查对象。

## 表 · W0 / W1 判定

- verdict：**两窗均无**
- W0 timeout 0 / W1 timeout 0；application 0 / 0；抓取失败 0 / 0
- 重启窗口 `2026-11-05T13:59:14Z` – `2026-11-05T13:59:46Z`：W1 没有可评估的重试条目，因此「与重启相关」在本窗口内无对象可判。
