# 超时重试复算（只读，2026-11-06T01:55Z–01:57Z）

## 结论

1. **改判据后，W0 与 W1 仍然都是 0 次超时重试。** 不是「旧判据恰好没错」，而是这次真的按 `ActivityTaskStarted.attempt > 1` 逐条数过：W0 扫 127 个工作流 / **8943** 条 Started 事件、W1 扫 149 个 / **11505** 条，`attempt > 1` 都是 **0**，`fetch_failures` 都是 0。
2. **6 次超时重试全部发生在 16 个导演运行里，且都在 09:59–10:29Z，早于这两个窗口**（W0 起点 12:59:46Z）。它们不可能与 **13:59:14Z 的 PR #12 重启**有关——时间上在重启前 3.5 小时以上。
3. 这 6 次的 `lastFailure` 全是 `{"message":"activity StartToClose timeout","source":"Server","timeoutFailureInfo":{"timeoutType":"TIMEOUT_TYPE_START_TO_CLOSE"}}`，`attempt = 2`，**且历史里没有 attempt=1 的 Started 事件**（`has_prior_started = false` ×6）。所以 1201 s 只能表述为「**调度 → 第 2 次尝试开始**」，机制上服务端按 StartToClose 计了首次尝试的超时；**不能再写成「排队等线程」**——那是上一轮的错误读法。
4. 4 次在 **PR #10 重启（10:09:53Z）之前**调度（`5d25a3a4` 10:07:25Z、`8697ee51` 10:09:08Z ×2、`c80ea8bb` 10:09:20Z），2 次紧邻 **PR #8 合并时刻 09:38:41Z**（`5d25a3a4` 09:39:04Z ×2；PR #8 的部署时刻未采集，无法证实到分钟）。
5. 采集时点（01:56:43Z）**没有运行中的工作流**（0 个），因此 `pending_activities` 检查为空。这一项只能记「无可检查对象」，**不能记「无 attempt>1 的待处理活动」**。

## 口径

- 判据：`ActivityTaskStarted.attempt > 1`；归因看该事件的 `lastFailure`：
  - 有 `timeoutFailureInfo` → `timeout`，按 `timeoutType` 分组；
  - 有 `applicationFailureInfo` → **单列为 application，绝不并入超时**；
  - 都没有 → `other`，标「归因不明」。
- 窗口：W0 `[2026-11-05T12:59:46Z, 13:59:46Z)`、W1 `[2026-11-05T13:59:46Z, 14:59:46Z)`；窗口归属按 **`started_time`** 的 epoch 判定（半开区间）。
- 枚举三条查询分开计数（原始查询原文见 `runlog.md`）：
  - `primary`：`StartTime ∈ [窗口起点−30m, 窗口终点)` —— 复现旧口径；
  - `supplement`：`StartTime < 起点−30m` 且 `CloseTime ∈ 窗口` —— 补「启动很早、窗口内才结束」的长跑工作流，本次两窗分别补到 **15** 与 **13** 个；
  - `running`：`ExecutionStatus="Running"` —— 本次两窗均为 0。
  - 按 `(workflow_id, run_id)` 去重：W0 **127**、W1 **149**，未触 5000 上限（`truncated=false`）。
- 活动类型不在 Started 事件上，用 `scheduledEventId` 回查 Scheduled；`unresolved_scheduled_event` 两窗均为 0。
- 时间解析：`eventTime` 带 9 位纳秒，先截到 6 位再 `fromisoformat`，比较用 epoch，不做字符串比较。

## 与旧审计的差异（必须保留的解释）

上一轮（`docs/DEPLOYMENT.md` §41）写的是「两窗 `ActivityTaskTimedOut` 均为 0」→「无新增超时」。这个判据有盲区：**Temporal 只在重试耗尽时才写 `EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT`**，一次超时后重试成功的尝试不会留下该事件。所以「0 TimedOut」不等于「0 重试」。

本次用 `attempt > 1` 重数，结论方向不变（W0/W1 都是 0），但依据换成了可直接观察的 Started 事件——**这正是 PR #13 第 1 节要求的补采**。

## 覆盖与失败

| 项 | W0 | W1 |
| --- | --- | --- |
| primary / supplement / running | 112 / 15 / 0 | 136 / 13 / 0 |
| 去重后 workflow-run | 127 | 149 |
| 抓取成功 / 失败 | 127 / **0** | 149 / **0** |
| 扫描 Started 事件 | 8943 | 11505 |
| `attempt > 1`（窗口内） | **0** | **0** |
| timeout / application / other | 0 / 0 / 0 | 0 / 0 / 0 |

- `supplement` 查询在本次服务端**可用**，两窗都返回了数据（15 / 13），没有 `supplement_unavailable`。
- 历史抓取用 `fetch_history()`（非 `fetch_history_and_close`），并发 6、单条 120 s、失败重试 1 次后计入 `fetch.failures`；本次 0 失败。

## 文件

| 文件 | 内容 |
| --- | --- |
| `director16.json` | 16 份本地导演历史的复算（全历史，不按窗口裁剪），6 条明细 |
| `w0.json` / `w1.json` | 两个窗口的枚举、抓取、扫描、分组与逐条重试 |
| `pending_now.json` | 采集时点运行中工作流的 `pending_activities`（本次 0 个运行） |
| `compare.json` | W0 vs W1 判定 + 与 13:59:14Z 重启的时序相容性 |
| `tables.md` | 上述数据的表格渲染 |
| `runlog.md` | 查询原文、计数、版本、时间戳 |

脚本：`../tools/recount_retries.py`（`json-dir` / `fetch` / `pending` / `compare` 四种模式，只读）。
