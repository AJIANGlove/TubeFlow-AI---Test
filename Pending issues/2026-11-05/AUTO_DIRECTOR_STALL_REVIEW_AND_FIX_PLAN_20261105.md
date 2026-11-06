# 三个 AUTO 项目导演阶段停滞：结论复核与分层修复方案（2026-11-05）

| 项 | 内容 |
| --- | --- |
| 性质 | 只读复核：没有改代码、数据、容器或配置，没有调用任何 Provider。**2026-11-05 用户对第 5–7 节的推荐回复「全部按推荐执行」**，记入 USER_DECISIONS §50；实现见分支 `fix/director-stall-20261105`（末次合并按推荐采用配方 ②「保留前镜」，并限 AUTO 项目）。 |
| 代码基线 | `main@746d6bc`。文中行号都指这个提交。 |
| 一手材料 | `AUTO_DIRECTOR_STALL_MONITOR_20261105.md`（下称「监控文档」）；`evidence/auto_director_stall_monitor/` 下的 `incidents/E01–E03`、`resumes/E02–E03`、`timeline.csv`；`docs/DEPLOYMENT.md` §40 |
| 项目 | SOIL `7cf2e79d-…`、CELESTE `f881122d-…`、MEDIEVAL `ff67c5e9-…` |
| 时间 | UTC 原文，括号内为 CST（UTC+8）。导演表没有 `updated_at`，失败时刻只能给区间。 |

标注约定：
- **【事实】** 指仓库内有出处；
- **【推断】** 指由事实推出、未直接取证；
- **【无证据】** 指仓库内找不到出处。

---

## 0. 摘要

1. **CELESTE 不是概率性卡住，而是同一单元反复失败。**
   - 监控期内能看到失败单元的 3 轮运行，全部终止在**第 40 单元**。
   - E01 与 E02 的最终诊断逐字节相同。
   - 拒收的四轴组合，正好是 v10 提示词里写明「必然非法」的那个示例。
2. **「恢复」不是整轮重跑。** 代码已复用上一轮成功的单元，SUCCEEDED 随恢复依次为 40→44→46。
   - 但第 40 单元每轮的提示词逐字相同，温度为 0.0，所以重跑几乎必然复现同一结果。
3. **观测窗口内的时间主要花在等人恢复。** 10:47:53Z–11:43:27Z（18:47:53–19:43:27 CST）共 55 分 34 秒：
   - 运行合计 10 分 29 秒；
   - 暂停等待合计 43 分 48 秒，占 79%。
   - 三轮运行分别在 3 分 23 秒、4 分 31 秒、2 分 34 秒内就失败。
   - 所以在这个窗口里，「长尾调用」不是停滞的原因。
4. **你的第 1、2、3 条结论里的具体数字，仓库里都没有出处。** 第 4 条的时区写错了。第 5 条的「同一失败码」与证据不符：实际是同一单元，失败码不同。详见第 1 节。
5. **调度层有两处代码级缺陷属实，但它们只影响速度，不导致失败：**
   - 单元认领存在竞态；
   - 8 线程的活动线程池由多个 Worker 共用。
6. **只改一处的建议：** 第 3 次仍因相邻多样性被拒时，对违规相邻对做确定性合并（第 6 节）。这条与已确认的 §5.3、§20.2 冲突，2026-11-03 也曾提出但未获决定（§45 状态行），必须由用户决定。

---

## 1. 证据核对表（你的 5 条结论）

| # | 结论 | 判定 | 仓库内证据 | 问题与需补采 |
| --- | --- | --- | --- | --- |
| 1 | 长尾阻塞整批：单次调用 1274/1232/1201 s；activity 超时 20 分钟、HTTP 超时 900 s；gather 等最慢者 | **机制成立，数字无证据** | 【事实】`workflows/storyboards.py:156-171`：`asyncio.gather` 等 N 个活动全部返回才进入下一轮。`DIRECTOR_CALL_ACTIVITY_TIMEOUT = 20 分钟`（`:35`）。`config.py:80` `director_provider_timeout_seconds=900`。`adapters/director_model.py:56` `httpx.Client(timeout=900)` | ① 三个数字在仓库内无出处。<br>② 三个数都**恰好略高于 1200 s**，即活动超时 20 分钟。按 Temporal 计时，单次活动尝试不会超过它；如果数字来自 worker 侧日志，超出 1200 s 的部分发生在服务端已判超时之后。所以它们更像活动超时，或包含线程池排队时间（见第 2 条），而不是模型本身的耗时。<br>③ `httpx` 的 `timeout=900` 是连接、读、写、池各自的超时，**不是总时长**。所以 `config.py:77-79` 注释里的不变式「HTTP 超时 < 租约 ≤ 活动超时」在服务端慢速吐字时并不成立。<br>④ 该活动没有设 `retry_policy`（`storyboards.py:160-164`），超时后会按 Temporal 默认策略无限次重试。<br>⑤ 观测窗口内各轮 2.5–4.5 分钟就失败，长尾不是这段停滞的原因。<br>补采：Temporal 历史 `temporal workflow show -w project-director-<owner_id>-<run_id>`（工作流 ID 规则见 `project_director_repository.py:394`），取每个 `advance_project_director` 的 Scheduled/Started/Completed/TimedOut 时刻。导演调用本身没有耗时日志：`project_director.py` 与 `director_model.py` 里都没有 logger |
| 2 | 并发退化：<2 s 空转比例 14%/46%/57%，疑为 `limit(1)` 后认领冲突即放弃 | **机制成立，比例无证据** | 【事实】`next_claimable_unit` 不加锁，`order_by(ordinal).limit(1)`（`project_director_repository.py:441-469`）。`claim_unit` 先锁运行行再复核单元（`:570-622`），失败返回 None。`advance` 遇 None 直接返回，不再找下一个（`project_director.py:821-822`）。<br>同一轮 4 个活动几乎同时启动，读到的是同一个最小序号单元：1 个认领成功，其余立即返回，再一起等成功者的模型调用。<br>另一处【事实】：`workflows/worker.py:185` 只有一个 `ThreadPoolExecutor(max_workers=8)`，由 storyboard、cover、media 三个 Worker 共用（`:186-275`）。三个项目各要 4 个并发导演活动，合计已超过 8 | ① 比例数字仓库内无出处。<br>② 线程池排队会让 `start_to_close` 在拿到线程前就开始计时。这取决于 SDK 的 `max_concurrent_activities` 默认值，需在部署机核实所用 temporalio 版本，并看 worker 日志里有没有线程池小于并发上限的告警。<br>补采：上面的 Temporal 历史，按 Completed−Started 分布统计 <2 s 的占比；worker 启动日志 |
| 3 | 校验概率问题：每个相邻对都要差 2 轴；首次通过率 77%/34%/36%；整轮 213–385 个相邻对必然违规 | **部分成立；「随机概率」的框架与 CELESTE 的数据不符** | 【事实】解析期逐对检查，`changed < 2` 即拒收（`project_director.py:1675-1689`）；单元 3 次尝试（`:90`、`:1129`）。<br>【推算】E01–E03 中第 40 单元以外的单元共 14 次尝试、8 次成功（逐次通过率约 57%），且没有一个耗尽预算。推算依赖 `unit_attempts` 是本轮各单元 `attempt_count` 之和，用 `auto_retries_used` 交叉核对一致（5=2+3、4=2+2、3=2+1）。<br>而第 40 单元在 3 轮里全部耗尽预算（第 2 节） | ① 77%/34%/36% 与 213–385 在仓库内无出处。<br>② 失败集中在固定单元，且重试高度相关（同提示词、温度 0.0），独立概率连乘的模型不适用。<br>补采：附录 SQL A、C |
| 4 | 18:10（UTC）镜像上线重建 worker → SUBMITTING 单元判 `worker_lost_after_call_started`，占 20 分钟租约 → CELESTE 累积到 `remote_state_unknown_exhausted` | **时区写错；因果属于推断** | 【事实】部署 T_dep 为 **10:09:53Z–10:10:06Z（18:09:53–18:10:06 CST）**（`docs/DEPLOYMENT.md` §40），不是 18:10 UTC。<br>运行 `8697ee51` 创建于 09:43:08Z（17:43:08 CST），失败码 `remote_state_unknown_exhausted`。下一轮 `9c2a58c3` 创建于 10:33:20Z（18:33:20 CST），所以失败落在这两个时刻之间，覆盖部署时刻。<br>租约 20 分钟（`project_director.py:97`）；过期后由 `find_stale_submitting_unit` 判为 worker_lost（`:796-807`）；只有 `attempt_count ≥ 3` 才终止为 exhausted（`:1129`） | ① 时序吻合：部署 + 20 分钟租约 ≈ 10:30Z，与 10:33Z 建新轮相符，但仍是推断。<br>② 另有一种不需要部署的机制：调用超过 20 分钟时，租约在调用途中过期，同一运行的其他活动会把仍在进行的调用判为 worker_lost（见第 1 条③），并且可能重复调用。<br>补采：附录 SQL B（`8697ee51` 各单元的失败码、尝试次数、租约） |
| 5 | 三次恢复都 accepted 并建了新 run，但都失败在同一 failure_code ⇒ 恢复有效但不解决根因 | **前半成立；「同一失败码」不成立** | 【事实】三次都 accepted，新运行均已核验（E01–E03 `action.verification`）。<br>新运行的失败码：`924ee9a8` 相邻多样性；`be6678d5` **`director_output_sentence_coverage_invalid`**（与前两次不同）；`d449aa5f` 的结果仓库内没有记录。<br>恢复后能看到失败单元的两轮（`924ee9a8`、`be6678d5`）都终止在**第 40 单元**；恢复前那轮 `baf41aa2` 也是 | 应改为「同一单元，失败码不全相同」。<br>恢复不是整轮重跑：`prepare_recovery_units` 复用上一轮成功的单元（`project_director.py:374-485`）。<br>补采：`d449aa5f` 的失败码与失败单元（附录 SQL A） |

另：`timeline.csv` 的 CST 列与 UTC 列有 2–3 秒不一致（如 `11:11:07.460Z` 对 `19:11:10 CST`），CST 列应由 UTC 列换算而来。

---

## 2. Q2：失败单元的位置

| 项目 | 运行 | 创建（UTC / CST） | 运行失败码 | 失败单元 | 结束时 SUCCEEDED（与上轮差） | 出处 |
| --- | --- | --- | --- | --- | --- | --- |
| CELESTE | `6fae6a67` | 09:02:44Z / 17:02:44 | adjacent_diversity | 未采集；【推断】序号 ≤ 约 14 | 9 | 监控文档 §3.1 |
| CELESTE | `8697ee51` | 09:43:08Z / 17:43:08 | remote_state_unknown_exhausted | 未采集（FAILED 2 个） | 38（+29） | 同上 |
| CELESTE | `9c2a58c3` | 10:33:20Z / 18:33:20 | adjacent_diversity | 未采集（FAILED 2 个） | 38（+0） | 同上 |
| CELESTE | `baf41aa2` | 10:47:53Z / 18:47:53 | adjacent_diversity | **40**（3 次）；42 有 1 次拒收、待重试 | 40（+2） | E01 `problem_units` |
| CELESTE | `924ee9a8` | 11:11:50Z / 19:11:50 | adjacent_diversity | **40**（3 次） | 44（+4） | E02 |
| CELESTE | `be6678d5` | 11:28:47Z / 19:28:47 | sentence_coverage_invalid | **40**（3 次） | 46（+2） | E03 |
| CELESTE | `d449aa5f` | ≤11:48:33Z / ≤19:48:33 | 无记录 | 无记录 | — | E03 `verification` |
| MEDIEVAL | `29e49f83` / `964a36b4` / `c80ea8bb` | 09:03:58Z / 09:43:00Z / 10:07:12Z | coverage / adjacent / adjacent | 均未采集 | 23 / 26 / 35 | §3.1 |

**判定：CELESTE 在能看到失败单元的 3 轮里，全部终止在第 40 单元。** 这是固定位置的反复失败，不是随机拒收。

支持这一判定的事实：
1. **E01 与 E02 的最终诊断逐字节相同**（同一相邻对、同一四轴取值）。
2. **恢复时第 40 单元的提示词逐字相同。**
   - 恢复要求每个单元的提示词版本、句子、上下文、模型与参数都与上一轮相同（`project_director.py:401-412`），并用同一份全局规划文档重新绑定（`:466`）。所以第 40 单元每轮拿到的提示词逐字相同。
   - 温度固定为 0.0（`api/storyboard_runtime.py:285`、USER_DECISIONS §40.5）。
   - 重试提示只按失败码生成，不随尝试次数变化（`:2119-2136`）。
   - 所以每轮的第 1 次尝试完全相同；如果前两次失败码相同，第 2、3 次也完全相同。
3. **E03 的最终失败码不同**，说明输出并不完全确定，但仍卡在同一单元。

推断（未取证）：`6fae6a67` 结束时只有 9 个单元成功、49 个未开始。单元按序号认领（`project_director_repository.py:467-468`），并发为 4，所以那一轮失败的单元序号应在约 14 以内，后来在别的轮次通过了。这说明 CELESTE 至少有两个难单元：一个早期单元最终通过，第 40 单元一直卡住。

MEDIEVAL 没有单元级数据，无法判断。

---

## 3. Q3：四轴诊断有没有系统性模式

仓库里只有 3 条相邻多样性诊断，其中 2 条不同；E03 的覆盖失败诊断为空 `{}`，因为覆盖拒收不写诊断。

| 来源 | 单元 / 相邻对 | 前一镜 | 后一镜 | 变化的轴 |
| --- | --- | --- | --- | --- |
| E01、E02（逐字相同） | 40 / 1→2 | REVEAL_EVIDENCE \| OBJECT_OR_EVIDENCE \| SAME_MOMENT \| SAME_LOCATION | ESTABLISH_CONTEXT \| OBJECT_OR_EVIDENCE \| SAME_MOMENT \| SAME_LOCATION | 仅 semantic_event |
| E01 | 42 / 2→3 | PERFORM_ACTION \| PRIMARY_SUBJECT \| LATER \| SAME_LOCATION | REVEAL_EVIDENCE \| PRIMARY_SUBJECT \| LATER \| SAME_LOCATION | 仅 semantic_event |

各轴变化次数（3 条诊断）：
- semantic_event 变化 3/3；
- subject_focus 0/3；
- time_relation 0/3；
- location_relation 0/3。

跨项目对照：2026-11-03 的 `765dca11`（v9 提示词）也出现过「两镜都是 OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION，只改 semantic_event」，以及一次「只改 subject_focus」（`Pending issues/2026-11-03/ROUND2_REVIEW_AND_DIRECTOR_V10_20261103.md` 第 2.1 节）。

结论：
1. **有模式，但不是你举的那两种。**
   - 「REVEAL_EVIDENCE 与 SHOW_CONSEQUENCE 互换」0/3。
   - 「subject_focus 恒为 PRIMARY_SUBJECT」1/3。
   - 实际模式是：**时间与地点两轴始终不变，只改事件标签，焦点不跟着改。**
   - 【推断】单元通常是同一场景内的 3–6 句，时间和地点很少变化。这样「至少差 2 轴」实际上等于要求每次切镜都同时改 semantic_event 和 subject_focus，模型经常只改一个。附录 SQL C 可以验证时间、地点两轴在已通过的相邻对里改动得有多少。
2. **问题不是提示词没有说。**
   - 第 40 单元的输出与 v10 提示词里写明「is invalid」的示例逐个 token 相同：`REVEAL_EVIDENCE|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION followed by ESTABLISH_CONTEXT|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION is invalid`（`project_director.py:2176-2180`）。
   - 重试提示也重复了这条规则（`:2127-2129`）。
   - v10 还允许「一时一地找不到相差两轴的第二镜时返回单镜」（`:2188-2192`），但模型在第 40 单元仍给出两个物件镜头。
   - 【推断】这一段文案很可能只描述同一物件、同一时刻，模型倾向于凑出两镜而不用单镜；把非法组合原样写进提示词，也可能起反作用。这两点都需要 A/B 才能确认。
3. **样本太小，不足以说「所有单元都是这样」。** 附录 SQL C 可以从已通过的单元里统计相邻对实际改了哪些轴，给出全量分布。

---

## 4. 观测窗口内时间花在哪里（CELESTE）

| 段 | 起（UTC / CST） | 止（UTC / CST） | 时长 |
| --- | --- | --- | --- |
| 运行 `baf41aa2` | 10:47:53.601Z / 18:47:53 | 暂停 10:51:16.654Z / 18:51:16 | 3 分 23 秒 |
| 暂停等待（E01） | 10:51:16.654Z | 恢复 11:11:07.460Z / 19:11:07 | 19 分 51 秒 |
| 恢复 → 新运行建立 | 11:11:07.460Z | 11:11:50.056Z / 19:11:50 | 43 秒 |
| 运行 `924ee9a8` | 11:11:50.056Z | 暂停 11:16:21.427Z / 19:16:21 | 4 分 31 秒 |
| 暂停等待（E02） | 11:16:21.427Z | 恢复 11:28:13.394Z / 19:28:13 | 11 分 52 秒 |
| 恢复 → 新运行建立 | 11:28:13.394Z | 11:28:47.368Z / 19:28:47 | 34 秒 |
| 运行 `be6678d5` | 11:28:47.368Z | 暂停 11:31:21.730Z / 19:31:21 | 2 分 34 秒 |
| 暂停等待（E03） | 11:31:21.730Z | 恢复 11:43:27.154Z / 19:43:27 | 12 分 05 秒 |

- 合计：运行 10 分 29 秒（19%），暂停等待 43 分 48 秒（79%），恢复到建新运行 1 分 17 秒（2%）。
- 「运行」段以暂停时刻为止，含 AUTO 巡检最多约 20 秒的发现延迟，所以真实失败不晚于暂停时刻。
- 这个 AUTO 运行从 09:02:44Z（17:02:44 CST）开始，到 11:53:37Z（19:53:37 CST）仍在导演阶段，共 2 小时 50 分 53 秒。更早几段的暂停与运行时长已无法还原（监控文档 §4.2）。

---

## 5. 分层修复建议（按改动小、风险低排序）

「需决定」一列标出与已确认决策冲突、必须由用户确认的项。其余项也都要等你确认后才实施。

| 序 | 层 | 改动 | 影响面 | 回归风险 | 验证方式 | 需决定 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 观测 | 每次导演调用结束时写一条结构化日志：运行、序号、第几次、失败码、诊断、调用耗时。覆盖拒收补记缺失或多余句子的**序号与数量**（不记正文，沿用 §40.1 口径） | 只增加日志和诊断字段 | 极低 | 单元测试检查日志字段；部署后日志能还原每次尝试 | 否 |
| 1 | 调度 c | **修认领竞态**：`advance` 在 `claim_unit` 返回 None 时，重新取下一个可认领单元，最多重试并发度次；或把「取下一个 + 认领」合并成一个事务 | 只影响 `advance` 与仓储查询，不改提示词、哈希和预算 | 低。并发真正到 4 后，同时在途的调用变多，429 走既有退避 | 单元测试：4 个并发 `advance` 对 4 个可认领单元，应认领 4 个不同单元；集成测试 PG 锁 | 否 |
| 2 | 提示词 a | **重试提示按违规对具体化，第 3 次升级指令**。<br>第 2 次提示写明哪两镜、四轴各是什么、只有哪一轴变了；第 3 次如果仍是同码，改为「把这两镜合成一镜，覆盖它们的全部句子」。<br>只改发给服务商的视图，持久化提示词与 canonical 不变（与 §45.3 同一机制） | 所有单元的本地拒收重试；不影响成功单元与前缀复用 | 低到中：模型是否照做未知，没有数据 | 单元测试（提示词随诊断与次数变化、持久化不变）；上线后统计第 40 类单元第 2、3 次的通过率 | 是：改动 §45.3 的提示文本 |
| 3 | 调度 c | 线程池与并发上限对齐：把 `max_workers=8` 调到不小于「导演并发 × 同时运行的项目数 + 故事板等活动」，并给 Worker 设显式 `max_concurrent_activities` | worker 资源；SQLAlchemy 连接池也要按线程数核算 | 中：线程多了可能耗尽数据库连接池 | 先按第 1 节补采确认存在排队，再压测 | 否（但要先取证） |
| 4 | 校验 b | **末次失败后确定性合并**：第 3 次仍**只**因相邻多样性被拒时，把违规相邻对合并成一镜，合并后再查新的相邻对，直到全部合格或只剩 1 镜；记一条审计型冲突码，不阻断（与 `shot_timing_converged` 同类，`project_director.py:110-118`）。<br>合并配方二选一：<br>① 沿用 §24 的 `adjacent-merge-v1`（`application/plan_convergence.py`），文本拼接，可能在一格里写两个时刻，与 §46.11「每格单一时刻」冲突；<br>② 保留前镜文本，只合并句子链接，后镜画面内容会丢失 | 只影响被拒 3 次的单元；输出镜头变少，后续物化、故事板、视频照常 | 中：镜头数减少；配方 ① 可能出现一格两时刻，配方 ② 会丢后镜内容。<br>相邻检查是 `_validated_output` 的最后一步（`:1675-1689`），只因它被拒的输出已通过覆盖与链接校验，合并不会破坏覆盖 | 纯函数单元测试（覆盖不丢、链接并集、序号重排、最少 1 镜、新相邻对复检）；端到端：3 次被拒 → 合并 → 运行成功；上线后统计合并次数与每单元镜头数 | **是**：与 §5.3「不自动降级」、§20.2「耗尽后运行 FAILED」冲突；2026-11-03 已提出未获决定（§45 状态行） |
| 5 | 提示词 a | 提示词 v11：去掉逐 token 的非法示例；改写为一句正面规则：「同一时间、同一地点内的相邻两镜，必须同时改 semantic_event 和 subject_focus」 | 新钉版项目；按 §45.2 的口径钉版，已在 v10 的运行不变 | 中：canonical 变化，前缀复用只在同版本内成立 | 黄金值测试（v8–v10 不变）；离线 A/B（需报批真实调用） | 是：新提示词版本 |
| 6 | 调度 c | `gather` 改为滑动窗口（任一活动返回就补一个） | 工作流代码 | 中：在途工作流重放会出现不确定性，必须用 `workflow.patched` 做版本分支 | Temporal 重放测试；在途运行不受影响 | 否 |
| 7 | 调度 c | 超时对齐：给 HTTP 调用加总时长上限（小于租约），给 `advance_project_director` 设显式重试策略 | 适配器与工作流 | 中：总时长上限需要流式读取或独立线程实现 | 先按第 1 节补采确认存在超过 20 分钟的调用 | 否 |
| 8 | 恢复 d | 单元失败后不立即终止运行，让其余单元继续，全部结束后再判 FAILED | 运行状态语义、暂停时机、进度页 | 中到高 | 单元与集成测试 | **是**：§5.3 已确认「预算耗尽……停止尚未提交的批次」（USER_DECISIONS 第 73 行） |

不建议的做法：
- **按相邻对数量自适应降低阈值**：会削弱已确认的「至少 2 维」规则（§5.12、§20.2），而且第 40 单元失败在第 1 对，与对数无关。
- **加大重试次数，或无人值守自动恢复**：已被三次恢复的结果否定。在提示词逐字相同、温度 0.0 的情况下，重跑高度相关。
- **问题 d 的前提需要更正**：AUTO 恢复已经是局部重试，成功单元逐个复用（`project_director.py:463-484`），不是整轮重跑。真正的缺口是失败单元每轮拿到的提示词完全相同。

---

## 6. 只能改一处：第 5 节第 4 项（末次失败后确定性合并）

**为什么选它：** 它是唯一能**在构造上**保证 `director_output_adjacent_diversity_insufficient` 不再让运行失败的改动，而这个失败码是已知失败运行的主因：

| 已知失败码的运行 | 次数 | 占比 |
| --- | --- | --- |
| director_output_adjacent_diversity_insufficient | 6（`964a36b4`、`c80ea8bb`、`6fae6a67`、`9c2a58c3`、`baf41aa2`、`924ee9a8`） | 67% |
| director_output_sentence_coverage_invalid | 2（`29e49f83`、`be6678d5`） | 22% |
| remote_state_unknown_exhausted | 1（`8697ee51`） | 11% |

出处：监控文档 §3.1、§3.2。`d449aa5f` 结果未知，未计入。

**预期收益（只给能从数据推出的部分）：**
1. 上表 9 次已知失败中，有 6 次在该单元第 3 次被拒后会改为合并通过，运行不在该点终止。之后这些运行是否会在别的单元失败，数据不足以判断。
2. CELESTE 观测窗口内：`baf41aa2`、`924ee9a8` 不会在第 40 单元终止；`be6678d5` 第 3 次是覆盖失败，仍会终止。
   - 按第 4 节，这两次失败之后各有一段等待恢复（19 分 51 秒、11 分 52 秒，合计 31 分 43 秒），在合并通过的情况下不会发生；运行会继续处理后面的单元，那部分运行时间仍要花。
3. 不增加 Provider 调用：只在 3 次都已花掉之后才合并。
4. **整轮成功率从 x% 到 y% 无法从仓库数据计算**：缺逐单元、逐次的尝试历史，成功运行也未采集。
   - 计算方法：整轮成功率 = ∏(1 − q_u)，q_u 为单元 u 用尽 3 次的概率。合并之后，q_u 只剩「第 3 次失败码不是相邻多样性」的那部分。
   - 第 40 单元在 3 轮中 2 轮第 3 次是相邻多样性，样本只有 3。
   - 用附录 SQL A、C 补齐后可以按单元算出 q_u。

**如何验证：**
1. 纯函数单元测试：
   - 违规对合并后覆盖不丢、链接取并集；
   - 合并后新出现的相邻对复检；
   - 极端情况只剩 1 镜；
   - 两种合并配方分别测试。
2. 端到端单元测试：假模型 3 次都返回第 40 单元那对四轴 → 运行成功，并记有审计码。
3. 无法用真实输出回放：库里只存了响应的 sha256，不存正文。
4. 上线后统计：
   - 每轮的合并次数；
   - 每单元镜头数分布的前后对比；
   - 受影响组的故事板按 §46.11 目视抽查。

**不能接受与 §5.3 的冲突时的替代：** 第 5 节第 2 项（重试提示具体化 + 第 3 次升级指令）。它在 §45.3 的框架内，不改校验规则，但收益无法预估，要上线后才能测。

---

## 7. 需要用户决定

1. 是否允许「第 3 次仍因相邻多样性被拒时确定性合并」（第 5 节第 4 项）。如果允许，选配方 ①（文本拼接）还是 ②（保留前镜）。
2. 是否把重试提示改成按违规对具体化，并在第 3 次升级为「合成一镜」（第 5 节第 2 项）。
3. 第 0、1 项（观测、认领竞态）不改产品语义，是否先做。
4. 是否授权在部署机执行附录 SQL 与 Temporal 历史查询（只读），补齐第 1 节的缺口。

---

## 附录：补采 SQL（只读；不假设本会话能访问生产库）

```sql
-- A. 三个项目所有导演运行中「未成功或有过拒收」的单元（Q2 的完整位置分布；补 d449aa5f）
select r.project_id, r.id as run_id, r.created_at, r.status as run_status,
       r.failure_code as run_failure, r.recovery_source_run_id,
       u.ordinal, u.kind, u.prompt_ref, u.status, u.attempt_count,
       u.last_failure_code, u.failure_code, u.response_diagnostics_json,
       (u.reused_from_unit_id is not null) as reused
from project_director_runs r
join project_director_units u on u.run_id = r.id
where r.project_id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                       'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                       'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
  and (u.status <> 'SUCCEEDED' or u.last_failure_code is not null)
order by r.project_id, r.created_at, u.ordinal;

-- B. 结论 4：8697ee51 的失败单元与租约
select ordinal, status, attempt_count, last_failure_code, failure_code,
       lease_until, next_attempt_at, response_diagnostics_json
from project_director_units
where run_id = (select id from project_director_runs where id::text like '8697ee51%')
  and status in ('FAILED', 'REMOTE_STATE_UNKNOWN', 'SUBMITTING', 'LEASED');

-- C. Q3 全量：已通过（非复用）单元内相邻对实际改了哪些轴
with s as (
  select u.run_id, u.ordinal, e.idx, e.shot
  from project_director_units u
  cross join lateral jsonb_array_elements(u.shots_json::jsonb) with ordinality as e(shot, idx)
  where u.project_id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                         'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                         'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
    and u.kind = 'SHOT_PLAN' and u.status = 'SUCCEEDED' and u.reused_from_unit_id is null
), p as (
  select run_id, ordinal, idx, shot as cur,
         lag(shot) over (partition by run_id, ordinal order by idx) as prev
  from s
)
select (prev->>'semantic_event'    <> cur->>'semantic_event')    as event_changed,
       (prev->>'subject_focus'     <> cur->>'subject_focus')     as focus_changed,
       (prev->>'time_relation'     <> cur->>'time_relation')     as time_changed,
       (prev->>'location_relation' <> cur->>'location_relation') as location_changed,
       count(*)
from p where prev is not null
group by 1, 2, 3, 4 order by count(*) desc;

-- D. 第 40 单元的文案（判断是否「一时一地只描述同一物件」）与提示词版本
select u.ordinal, u.prompt_ref, jsonb_array_length(u.sentences_json::jsonb) as sentence_count,
       u.sentences_json
from project_director_units u
where u.run_id = (select id from project_director_runs where id::text like 'be6678d5%')
  and u.ordinal = 40;
```

Temporal 历史（只读）：对上面各运行执行 `temporal workflow show -w project-director-<owner_id>-<run_id> --output json`，统计 `advance_project_director` 每次的 Started→Completed 耗时、TimedOut 次数和重试次数。

---

## 8. 补采后的更正（2026-11-05，详见 `Pending issues/2026-11-05/DIRECTOR_STALL_FOLLOWUP_REVIEW_20261105.md`）

1. 第 3 节结论 1 中的【推断】「单元内时间、地点很少变化」被 SQL C 否定：731 个已通过的相邻对里，时间轴变化 448 对（61%），地点轴变化 300 对（41%），两者都不变的只有 183 对（25%）。
2. 第 2 节「固定位置反复失败」需要补充：CELESTE 全部 11 轮的失败单元依次为 9 → 33/39 → 40（连续 5 轮）→ 54 → 57。难单元会连续卡多轮，但最终会通过。第 2 节推断首轮失败单元序号在 14 以内，实际是第 9 单元。
3. 第 1 节第 4 条（重启导致未知态耗尽）现在有直接证据：`8697ee51` 的两个活动在 PR #10 重启前 45 秒调度，第 1 次尝试跑满 20 分钟超时后重试；同一运行的第 33 单元 unknown_exhausted、第 39 单元 worker_lost。
4. 第 6 节的失败码统计更新为 14 次已知失败运行：相邻多样性 10 次（71%）、句子覆盖 2 次、响应 JSON 无效 1 次、未知态耗尽 1 次。
