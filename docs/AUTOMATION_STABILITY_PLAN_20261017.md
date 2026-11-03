# 自动化流程稳定运行方案

日期：2026-10-17
适用对象：本项目 AUTO 一键生产链路（真实配音 → 前置导演 → 导演草稿 → 故事板 → 组视频 → 合成）
证据边界：本文所有"现状"结论均来自本仓库代码/配置与部署机（192.168.110.200）实测；标注"建议"的部分尚未实施。

---

## 0. 结论先行

1. 自动化能不能稳定跑，取决于四件事，而不是"重试次数够不够"：
   - 每个外部动作都能被判成 **成功 / 明确失败 / 未知** 三种之一；
   - 每种失败都有 **唯一出口**（自动重试或停下来等人，不允许"既没成功也没人管"）；
   - 每个在途状态都有 **主人 + 租约 + 回收者**；
   - 每个暂停都能在 **1 分钟内被看见**，且带可执行动作。
2. 本系统当前最贵的缺口不是重试策略，而是三处：**失败证据不留存**、**驱动环单点（sweep 只在 worker 进程里，且 compose 无 restart）**、**无日志/无指标/无告警**。
3. 最危险的故障形态是"假成功"和"静默停滞"，显式报错反而是最便宜的一类。
4. 重试只能吸收瞬时故障；对永久故障重试只是浪费预算与时间，必须 fail-fast 到人工。
5. 任何稳定性改造都必须带可执行验收（能复现、能度量），否则只是把问题藏起来。

---

## 1. 常见问题及成因（附本系统真实证据）

| # | 问题 | 本系统证据 | 成因 |
|---|---|---|---|
| 1 | **上游"假成功/空响应"**：HTTP 200 但没有可用正文 | 2026-10-16 项目 `2333d455` 前置导演单元 23，`director_response_content_missing`：`http_status=200`、`raw_size_bytes=616`、`usage completion=10810`、`finish_reason=stop`、`parse_stage=content`（`choices[0].message` 里没有 `content` 字段） | 聚合网关把上游异常归一化成 200；推理型模型把 completion 预算耗在思维链上；字段"缺失"而非"空串"。本地解析器只能判"缺字段"，拿不到原因 |
| 2 | **虚假 4xx** | `USER_DECISIONS` §15.14：实测 grsai 对同一合法请求偶发返回 400，原样重放即 200；代码 `adapters/director_model.py:87-90` 已将 400/404/409/410/422 归入可重试 | 网关侧限流/路由抖动被包装成业务错误。若按"4xx=永久失败"处理，会误杀可恢复任务 |
| 3 | **远端未知态与重复提交风险** | 历史 `remote_state_unknown_exhausted` 多次（项目 `a5367080` 单元 1/117，2026-10-08 至 10-13）；规则见 `USER_DECISIONS` §15.4 | 超时/连接中断/5xx 时无法证明对方是否已受理；本地幂等键是否被 Provider 真正执行不受我们控制。重提可能重复计费 |
| 4 | **静默停滞/孤儿在途** | 2026-10-13 生产实锤：项目 `a5367080` / unit 120 长期停在 SUBMITTING，恢复循环报 `director_run_unresolved`（C1）；另有 `worker_lost_after_call_started` 失败码 | 在途状态只存在于 DB，持有它的是进程；进程死掉后没有回调、没有超时回收，状态机就停在那里。`kick()` 是 best-effort（`auto_pipeline/service.py:123`），踢失败只记日志 |
| 5 | **驱动环单点** | `auto_pipeline` 的推进靠 worker 内的 `auto_pipeline_sweep` 循环（`workflows/worker.py:569`，间隔 20s，`config.py:135`）；实测 `docker-compose.yml` 全文 **没有 `restart:` 与 `stop_grace_period`**；`run_worker()` 用 `asyncio.wait(FIRST_COMPLETED)`，任一被监督组件退出即整体退出（fail-fast） | 保活责任放在容器编排层，而编排层没配重启策略。worker 一退，全链路停摆且页面仍显示"已连接后端"（web 是另一个容器） |
| 6 | **容量/合同口径不一致** | 2026-10-14 第 85 组卡点 `group_shot_limit_exceeded`（候选 6 镜 > 组上限 5，全库唯一）；实测 **78/133 组（58.6%）横跨 ≥2 个导演单元**；生成端 ≤min(2×句数,8) 与消费端 ≤5 两处口径（`project_director.py` vs `storyboards.py:277`） | 配音分组按实测时长冻结、导演单元按字符打包，两套划分目标不同、天然互相切割；边界只在运行时才暴露 |
| 7 | **失败证据不可回溯** | 单元诊断字段 `response_diagnostics_json` 每次重试覆盖，只留最后一次（本次 3 次尝试只能看到第 3 次）；原始响应体不落库，只有 `raw_response_sha256`；全仓库无 `logging.basicConfig/dictConfig`，INFO 被丢弃，本次暂停在 worker 日志里**只有 1 行** | 设计上把数据库当唯一权威、把日志当附属品；排障时缺"请求→响应原文→判定"的证据链 |
| 8 | **观测/告警缺失** | 只有 `/health/live`（`api/app.py:389`，仅证明进程活着）；无 `/metrics`；无任何告警通道（2026-10-14 已确认） | 健康检查回答的是"进程在不在"，不是"流程有没有在动"；暂停只能靠人刷页面发现 |
| 9 | **重试策略过于均质** | 导演/配音/故事板/合成都用固定退避 `(5,15,30)` 且**无抖动**（`project_director.py:77`、`narration.py:23`、`storyboards.py:67`、`composition.py:24`）；只有封面通道 `cover_llm_transport.py:197-210` 加了 jitter | 固定间隔在上游故障时会形成同步重试波，放大拥塞；对长尾慢故障又不够长 |
| 10 | **环境/发布漂移** | mihomo 曾因容器内缺 geo 文件去 GitHub 下载、境内超时后 fatal 退出（已由 `docker/mihomo/entrypoint.sh` 固化）；镜像按 `sha256:` 钉死；2026-10-14 实锤并行会话构建把"半截修复"扫进镜像 `7edaa916` | 启动期依赖外网、共享工作区、多人并行构建同一上下文，都会让"同一份代码跑出不同结果" |
| 11 | **对象存储误用风险** | MinIO 为 xl 存储，直接读 `/data/<bucket>/<key>` 的字节 ≠ 对象字节（含块校验层），必须走 S3 API | 排障时图省事读磁盘文件，会得到错误的哈希/长度结论，从而误判 |

---

## 2. 可落地的解决思路与最佳实践

优先级定义：**P0 = 不做就会再次静默停摆/无法定因**；P1 = 降低复发率与定位成本；P2 = 长期工程化。

### P0-1 失败响应证据留存（解决 #1 #7）

- 做法：`_structured_response_error()` 已算出 `raw_response_sha256` 与 `raw`（`adapters/director_model.py:224-256`），在其中把**失败响应体**写入对象存储，键为 `director-raw/{sha256}.json`，元数据记 `project_id/run_id/unit_id/ordinal/attempt_no/parse_stage`，TTL 7 天。成功响应不存，避免存储膨胀。
- 落点：`adapters/director_model.py` + 一个对象存储端口（Web/Worker 共用）。
- 验收：任一 `director_response_*` 失败，能用 1 条命令取回原文并在本地复现出同一个 `parse_stage`。
- 注意：MinIO 读取必须走 S3 API（见 #11）；写入失败不得影响主流程（降级为只记哈希）。

### P0-2 每次尝试一行历史（解决 #7）

- 做法：新增 `director_unit_attempts`（或复用事件表），字段：`unit_id, attempt_no, failure_code, diagnostics_json, provider_receipt, raw_sha256, occurred_at`。`project_director_units` 保留"最后一次"用于快照，但不再承担历史。
- 验收：3 次尝试的失败码/诊断可分别读出；能判断"三次同因"还是"逐步变化"。
- 现状对照：`schedule_unit_retry()` 目前只更新单元行（`project_director_repository.py:747-795`），历史靠覆盖。

### P0-3 编排层保活（解决 #5）

- 做法：`docker-compose.yml` 给 `web/worker/postgres/minio/temporal/mihomo` 全部加 `restart: unless-stopped`；给 `worker` 加 `stop_grace_period`（≥ 单次最长外部调用租约，导演调用租约见 `project_director.py:79-82` 与 `TUBEFLOW_DIRECTOR_PROVIDER_TIMEOUT_SECONDS=900`）。
- 验收：`docker kill -s KILL tubeflowai-test-worker-1` 后 60 秒内容器自动回到 healthy，`sweep_once` 在 2 个周期（40s）内接管所有 RUNNING 运行。
- 注意：加重启策略前必须确认优雅停机语义——否则在途调用会被判 `worker_lost_after_call_started`，产生新的未知态（见 §3）。

### P0-4 结构化日志（解决 #7 #8）

- 做法：启动处 `logging.config.dictConfig`，JSON formatter 固定字段：`ts, level, logger, project_id, run_id, unit_id, group_id, phase, reason_code, provider_receipt, attempt, duration_ms`；INFO 起落 stdout，交由 Docker `json-file` 轮转（配 `max-size/max-file`）。禁止落 prompt 全文与密钥（只落 `prompt_sha256`、`length`）。
- 验收：一次失败能按 `run_id` grep 出"请求→响应判定→重试排期→终态→暂停"完整链，不依赖读代码猜。
- 现状：全仓库无日志配置，只有 WARNING+ 经 lastResort 落 stderr；本次暂停日志仅 1 行。

### P0-5 最小指标 + 暂停告警（解决 #8）

- 做法：`/metrics`（Prometheus 文本格式即可）暴露：各阶段在途/成功/失败计数、每对象预算消耗分布、暂停运行数（按 reason_code 分组）、外部调用 p50/p95 时延、`sweep` 最后心跳时间、租约回收次数。告警规则最小集：① 任一 run PAUSED 立即告警；② `sweep` 心跳 > 60s 未更新告警；③ 单阶段失败率 10 分钟 > 20% 告警。
- 验收：制造一次暂停，1 分钟内告警到达，且消息里带 `project_id + reason_code + phase`。

### P1-1 把"失败分类学"固化成代码常量（解决 #1 #2 #3）

- 四类，各有唯一出口：
  1. **确定性失败**（鉴权 401/403、内容策略拒绝）→ 不重试，直接暂停并给人工动作；
  2. **瞬时失败**（429/5xx/虚假 4xx/网关 200 空响应）→ 有界退避重试；
  3. **未知态**（超时、连接中断、无回执）→ 只对账/恢复查询，AUTO 按 §15.4 有限重提；
  4. **本地拒收**（结构/多样性/容量校验不通过）→ 修复重试或人工修稿，不当作 Provider 故障。
- 现状已有前缀约定（`director_response_*`/`provider_http_*`/`remote_state_unknown_*`），建议收敛为一个显式枚举 + 单一映射函数，新增失败码必须登记，禁止散落字符串。

### P1-2 退避加抖动 + 尊重 Retry-After（解决 #9）

- 做法：在 `(5,15,30)` 基础上加 full jitter（`uniform(0, base)`，参考已有实现 `cover_llm_transport.py:210`），429 仍优先 `Retry-After`。分母是"同一 Provider 的并发调用数"，不是全局限流。
- 验收：模拟上游 500 持续 60s，重试时间点在时间轴上分散，不出现同秒并发尖峰。

### P1-3 停滞检测器独立于 worker（解决 #4 #5）

- 做法：把 `sweep_once()` 与 `reclaim_expired_units()/find_stale_submitting_unit()`（`project_director_repository.py:471/500`）的"定期调用者"从 worker 内循环提升为**独立进程或宿主机 cron/systemd timer**，通过 HTTP 调管理端点执行；worker 内保留原循环作为加速路径。
- 验收：worker 完全停止时，停滞检测器仍能发现"在途 > 阈值无推进"的对象并告警/回收；恢复后自动续跑。

### P1-4 启动自检门禁（fail-closed，解决 #5 #10）

- 做法：`/health/ready` 增加真实依赖探测：DB 迁移版本 = head、MinIO 可写（PUT+DELETE 探针）、Temporal 可达、`mihomo:7890` 监听（`[::]:7890` 而非 127.0.0.1）、geo 文件存在、Provider 鉴权 1 次轻量探针（失败只降级为"未就绪告警"，不阻断对外读路径）。
- 验收：任一依赖缺失时 worker 不发布 ready 文件（现有机制 `/tmp/tubeflow-worker-ready`），compose 不把流量导过去，且日志明确写出缺哪个依赖。

### P1-5 合同一致性门禁（解决 #6）

- 做法：启动时断言三处容量关系：单元 `≤min(2×句数,8)`、组 `≤5`、故事板格数 `=5`；并在播种前对"组候选镜数 > 5"做**显式拒绝 + 可执行提示**（当前已是 `group_shot_limit_exceeded`，缺的是"提前到规划期暴露"）。
- 验收：构造 6 镜用例，在导演规划阶段即报出，而不是跑到第 85 组才停。
- 注意：**不允许**自动截断/静默合并（`USER_DECISIONS` §5.7、§20.3）——只能"近上限监控 + 用户确认式确定性合并提案"。

### P1-6 发布可重复（解决 #10）

- 做法：镜像固定 `sha256:`（已做到）；部署前比对两机 `docker-compose.yml` md5 与实际镜像 digest；构建前加互斥锁（文件锁即可），禁止并行会话同时构建同一上下文；构建后做"容器内代码指纹"探测（关键文件 SHA256）确认镜像内容 = 预期提交。
- 验收：任意两次部署可用 digest 列表完全复现；容器内关键文件哈希与仓库一致。

### P2-1 故障注入测试（把上述机制锁住）

用 mock provider 覆盖：200 无 `content` 字段、200 空串、200 半截 JSON、429 + Retry-After、502、连接超时、成功后回执缺失、租约过期。断言：错误码正确、重试次数 = 预算、终态 reason_code 正确、可恢复路径可用。

### P2-2 恢复路径测试

前缀复用（本次 25 个成功单元 + 重跑 30 个）、预算重置（恢复 = 新一轮授权）、`resume_if_paused_for` 原因码守卫（`auto_pipeline/service.py:110`）、输入变化时整轮重跑（§15.13）。这些一旦回归，会造成"重跑全部失败单元"或"重复使用过期结果"的昂贵错误。

### P2-3 暂停即复盘

每次 PAUSED 产出 5 行事故卡：时间、阶段、reason_code、直接原因（含 provider_receipt）、处置动作。积累 20 条后可量化"哪一类失败最贵"，再决定预算与告警阈值，而不是凭感觉调参。

---

## 3. 实施时需要注意的关键点

1. **不要用"自动收敛/自动截断"换稳定**：语义冲突（>5 镜、结构不合法）必须停下来等人，`USER_DECISIONS` §5.7/§20.3 明确禁止静默合并与自动放行。稳定性的目标是"不静默"，不是"永不暂停"。
2. **重试边界不能碰**：未知态默认不自动重提；AUTO 项目的自动重提仅在 §15.4 授权范围内、每对象 3 次封顶。把"未知态当失败重试"会带来重复计费，风险高于收益。
3. **加 restart 必须先做优雅停机**：worker 退出时应先把在途单元置回可认领状态（或等租约到期），否则 `stop_grace_period` 不足会批量制造 `worker_lost_after_call_started`。
4. **日志与原始响应都要脱敏**：prompt 含用户文案与角色设定，只落 `sha256` 与长度；原始响应体存对象存储并设 TTL，访问走 S3 API，不读 `/data` 裸文件。
5. **观测不能只加"进程存活"**：`/health/live` 通过 ≠ 流程在动。必须补"阶段级在途数"与"sweep 心跳"，否则本次这种"页面显示已连接、流程已停 11 小时"的情况会重演。
6. **预算语义改动要同步三处**：`USER_DECISIONS`、代码常量、测试断言。只改代码会造成文档与行为漂移（AGENTS.md 明令禁止）。
7. **门禁 fail-closed 只挡"写路径"**：依赖不健康时禁止新提交外部调用，但页面查询/读接口应保持可用，否则排障时看不到现场。
8. **指标/告警只暴露计数与时延**，不暴露文案、音色、角色等业务内容；告警消息只带 `project_id + reason_code + phase`。
9. **验收必须可复现**：每条建议都要有"怎么触发、看什么、判据是什么"。未实际运行的检查不得写成通过（AGENTS.md）。

---

## 4. 验收清单（建议作为上线门槛）

| 项 | 判据 |
|---|---|
| 故障注入 8 场景 | 全绿，错误码与重试次数与设计一致 |
| worker 强杀自愈 | `kill -9` 后 60s 内容器 healthy，40s 内 sweep 接管，无孤立 SUBMITTING |
| 失败定位耗时 | 任一失败可用"日志 grep + DB 1 行 + 原始响应"三步定位，≤ 10 分钟 |
| 暂停可见性 | PAUSED 后 1 分钟内告警到达，含 `project_id/reason_code/phase` |
| 证据留存 | 任一 `director_response_*` 可取回原始响应体，且能复现同一 `parse_stage` |
| 合同门禁 | 6 镜用例在规划期被拒，且提示可执行动作 |
| 发布可重复 | 两机 digest 一致、容器内关键文件哈希与仓库一致 |
