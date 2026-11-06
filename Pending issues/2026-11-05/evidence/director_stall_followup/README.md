# 导演停滞补采（PR #12 合并前只读）

采集时刻：2026-11-05T13:38Z–13:46Z（UTC）。全部只读：`BEGIN TRANSACTION READ ONLY` + `SELECT` + `temporal workflow show`；无写库、无写对象、无 Provider 调用、未操作任何项目。

对应 `AUTO_DIRECTOR_STALL_REVIEW_AND_FIX_PLAN_20261105.md`（PR #12 分支 `1baebef`）第 7 节第 4 项的授权请求，以及 `FINAL_ACCEPTANCE` §45「部署机补采」一项。

## 1. 附录 SQL A–D（+E）

脚本 `sql/{A,B,C,D}.sql` 从分支文档附录逐字节抽取（`git show pr12:…` + 正则切分），未改动一字；`sql/E.sql` 为本次新增。每份都在只读事务中执行，原始输出 `sql/out/{A..E}.txt`。

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| A | 254 | 三个项目所有「未成功或有过拒收」的单元，含 `response_diagnostics_json` |
| B | 2 | `8697ee51` 的失败单元：33（`remote_state_unknown_exhausted`，attempt 3）、39（`worker_lost_after_call_started`，attempt 2） |
| C | 11 | 已通过（非复用）单元内相邻对的实际轴向变化分布 |
| D | 1 | `be6678d5` 第 40 单元：`prompt_ref=project-director-unit-v10`，6 句 |
| E | 16 | 三个项目的全部导演运行 + `owner_id`（附录没有，工作流 ID 需要它） |

**E 的必要性**：工作流 ID 是 `project-director-<owner_id>-<run_id>`，而附录 A–D 都不返回 `owner_id`。本项目 `owner_id` 实际是 `fb294c02-5f32-4597-b1e9-f7348ac9734a`（以文本存储，非 UUID 形式），已用 `temporal workflow list --query 'WorkflowId STARTS_WITH "project-director-"'` 逐条核对：34 条导演工作流中 16 条属于这三个项目，ID 拼接方式成立。

**C 的结果**（`event/focus/time/location` 是否变化 → 计数）：

| 组合 | 计数 |
| --- | --- |
| 事件+焦点+时间变，地点不变 | 194 |
| 事件+焦点变 | 183 |
| 四轴全变 | 165 |
| 事件+时间变 | 75 |
| 事件+焦点+地点变 | 21 |
| 焦点+时间变 | 19 |
| 事件+地点变 | 16 |
| 焦点+时间+地点变 | 13 |
| 焦点+地点变 | 9 |
| 时间+地点变 | 1 |

合计 731 对。**只变 1 轴的行数为 0**（`t|f|f|f`、`f|t|f|f`、`f|f|t|f`、`f|f|f|t` 均无），与「相邻至少差 2 维」的规则一致：这类对根本没通过。

## 2. Temporal：16 个导演运行的 `advance_project_director`

历史原文 `temporal/history/<run_id>.json`（16 份，`temporal workflow show --output json`，rc=0）；解析脚本 `tools/temporal_audit.py`，汇总 `temporal/summary.json` 与 `temporal/summary.md`。

**总量**：572 次活动尝试，572 次 Completed，**TimedOut 0 次，重试 0 次**。

**Started→Completed**（模型调用真实耗时）：全 16 轮里最大 **806.07 s**（`8f63bd28`），其次 646.62 / 537.02 / 532.08 s；多数轮的 p50 在 0.1–0.7 s（认领失败立即返回的那批），有实质调用的轮 p50 为 62–232 s。

**Scheduled→Started**（排队等 worker 线程）：p50 普遍 0.01–0.03 s，但出现 **5 次约 1201 s 的排队**：

| run | Scheduled→Started |
| --- | --- |
| `5d25a3a4` | 1201.017 s、1201.024 s |
| `8697ee51` | 1201.011 s、1201.685 s |
| `c80ea8bb` | 1201.029 s |

**1201 / 1232 / 1274 的结论**：

- **1201 s 出现 5 次，全部属于「排队等线程」，不是模型调用**，且都恰好压在 1200 s = `DIRECTOR_CALL_ACTIVITY_TIMEOUT`（20 分钟）这条线上，±2 s 内。
- **1232 s 未出现**；**1274 s 未出现**。仓库内也查不到这两个数的出处（`grep -rn` 全库仅 `.workbuddy/memory/2026-11-05.md:48` 的区间写法 "1201–1274 秒"）。
- 因此「长尾调用拖住整批」在本窗口内**不成立**：没有任何一次调用被记为超时，也没有任何一次重试；真正的 20 分钟级别等待发生在活动开始之前（线程池排队）。

## 3. 版本与线程池告警

| 数据 | 值 | 来源 |
| --- | --- | --- |
| worker 内 `temporalio` 版本 | **1.35.0** | `logs/temporalio_version.txt` |
| worker 日志窗口 | 2026-11-05T10:10:22Z – 13:45:28Z，1702 行 | `logs/worker_before_deploy_ts.log` |
| 「线程池 max_workers 小于 max_concurrent_activities」类告警 | **无** | 同上，`grep -ciE "max_workers|thread pool|max_concurrent_activities"` = **0**；`grep -i warn` = 0 |
| 导演日志三类标记 | **无（0 条）** | 该镜像尚未含 PR #12 的日志；且窗口内无新导演调用 |

日志尾部为 `no compatible H3 node has capacity`（h3-new-1 OFFLINE，h3-new-2/3 各 2/2 占满），与导演无关。

## 4. 未采集 / 未验证

- 附录 SQL 里没有 `owner_id`，本次新增 E 才拿到；这一点已在上面说明，未改动 A–D 原文。
- `1232`、`1274` 两个数值在仓库内**无出处**，本次历史里也**未出现**；不推测其来源。
- 线程池告警：temporalio 1.35.0 在该日志级别下**不输出**此类告警，故「无」只能表述为「日志中没有」，不能据此断言不存在线程池不足；正面的证据是上面 5 次 1201 s 排队。
- 未采集：`advance_project_director` 的服务端 `timeoutType` 分布（本窗口 TimedOut=0，无样本）、导演提示词全文与响应正文（库里只存 sha256，且本次按要求不读取正文）。

## 5. 勘误（2026-11-05 复核，详见 `Pending issues/2026-11-05/DIRECTOR_STALL_FOLLOWUP_REVIEW_20261105.md`）

- 第 2 节「TimedOut 0、重试 0；1201 s 是排队」不成立。`temporal/history` 原文中共有 **6** 个活动的 `ActivityTaskStarted.attempt = 2`，`lastFailure` 为 StartToClose 超时：第 1 次尝试跑满 1200 s 后超时，1 s 后重试。Temporal 不为中间重试写事件，本目录的审计脚本因此看不到。
- 第 1 节 C 表：`t|t|f|t`（75）应为「事件+焦点+地点」，`t|f|t|t`（21）应为「事件+时间+地点」，漏列 `t|f|t|f`「事件+时间」35。原始输出 `sql/out/C.txt` 无误。
