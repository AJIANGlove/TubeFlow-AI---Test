# 一键自动生产（AUTO 模式）实施与首轮真实验收 · 会话交接文档

日期：2026-09-15 至 2026-09-17。本文供新会话接手使用，全部为已核实事实；未执行或未验证的项明确标注。

## 1. 本次任务与最终状态

- 目标：新建项目「上传文案 → 选音色 → 点一次提交 → 全链路自动执行 → 直接产出最终成片」，异常尽量自动处理，处理不了的暂停并展示真实原因。
- 状态：**已完成并真实验收通过**。首个全量项目（`3206fa93-133b-4dfc-b786-cc3008ee252b`，1:1 复制模板项目 `17d2b9e9-f70c-4450-a1d6-9b13727126a7`）自动跑完全链路，产出 160,208,325 字节 MP4 + 同步 SRT，已自动/手动切换为当前成片并可直接观看、ZIP 导出。
- 验收规模：321 句配音、54 导演单元、91 组故事板、91 组 H3 视频、1 次合成。

## 2. 已确认决策（全部落于文档）

- `docs/planning/USER_DECISIONS.md` §15（§15.1–§15.15）：一键授权范围、每对象 3 次预算（无整轮总上限）、失败分类、未知态自动对账/重提、H3 停滞 GPU 判定、QC 自动重生成、超长句拆句 agent、创意关卡跳过、暂停/恢复=新授权轮次、聚合进度视图、ZIP 唯一导出、仅新建项目生效、部署来源输入变化自动整轮重跑+审计（§15.13）、LLM 偶发 4xx 有界重试（§15.14）、AUTO 编译双修复预算（§15.15）。
- `docs/planning/FINAL_ACCEPTANCE_CRITERIA.md` §18：对应验收项。
- `AGENTS.md`：触发边界条款已更新为新旧并存模型。
- 授权记录：`runtime/operations/auto_pipeline_authorization_20260915.md`（含执行记录与最终核算）。

## 3. 架构与关键代码（新会话导航）

- 模式标记：`projects.production_mode`（AUTO/MANUAL，迁移 0024）；预算轮次 `projects.auto_budget_round`（恢复 +1，迁移 0026）。
- 编排器：`backend/src/tubeflow_test/auto_pipeline/`（domain/ports/service/factory）。`advance` 幂等逐阶段推进；终态踢点 + Worker 巡检（`auto-pipeline-sweeper`，20s，`auto_pipeline_sweep_interval_seconds`）；暂停写稳定 `reason_code`；`POST /identity/projects/{id}/auto-pipeline/resume` 恢复=新授权轮次（预算按轮重置）。
- 各阶段 AUTO 自动恢复（每对象 3 次、退避 5s/15s/30s、429 尊重 Retry-After）：配音（`application/narration.py` finish_job 自动重投 + 阻断原因持久化于 narration_project_records.payload["blocking_reason"]）；前置导演（`application/project_director.py`，未知态也计预算重提、无整轮上限、AUTO_RESUME 前缀复用）；故事板（`application/storyboards.py` connect/429 重试、未知自动对账无果重提，血缘存 input_json）；H3（`video_generation/orchestration.py`，EngineRejected 既定拒绝重提、未知 3 轮无果重提、QC_FAILED 自动重生成、停滞 GPU 判定取消+重提；血缘列迁移 0025）；合成（`application/composition.py` outbox 代际重投 + `SqlAlchemyCompositionOutbox.reconcile_lost_dispatches` 失联工作流对账，`composition_stale_reconcile_seconds` 默认 2100s）。
- 拆句 agent：`sentence_split/`（领域无损校验=模型输出仅作锚点、按原句重对齐切割；服务每句 3 次修复重试带 repair_reason 且换幂等键）+ `adapters/deepseek_sentence_split.py`；配置 `sentence_split_provider_*`（默认 DeepSeek 端点 + deepseek-v4-flash）。冻结前介入，失败整体拒绝。
- GPU 探测：`video_generation/gpu_probe.py`（fail-closed）+ 节点脚本 `scripts/h3_gpu_status_agent.py`（纯标准库，代理节点本机 ComfyUI `/queue`+`/system_stats`；**尚未部署到三台 H3 机器**，未部署时停滞兜底走"3 轮对账无果重提"）。
- 前端：`frontend/auto-progress.js` 聚合进度视图（`auto_progress` 页签仅 AUTO 项目）、`app.js` 提交改造（第 05 步「提交并开始生产」、AUTO 移除预检、自动执行期间锁定手动生成入口）、`frontend/api-client.js` 加 `autoPipelineResume`。
- 迁移 0024–0028 为本次新增；0029（H3 路由 legacy/candidate 分流）来自**另一工作流**（非本次任务），已随部署应用。

## 4. 真实验收运行 1 暴露并修复的问题（每个都有测试）

1. 拆句 agent token 未配置 → `.env` 补 `TUBEFLOW_SENTENCE_SPLIT_PROVIDER_TOKEN`（复用视频提示词编译的 DeepSeek 密钥）。
2. 拆句有损（em-dash 切分点丢空格）→ 重对齐重切方案（生产 5 个风险句实测全部无损通过）。
3. 导演全局规划章节超限（提示词未告知上限 24）→ 提示词补齐上限，版本 `project-director-global-plan-v5`。
4. 配音恢复热循环：批路径 attempt 未落预算轮次 + 同键续投簿记漂移误报冲突 → 键派生轮次落库、内容身份守卫放宽、既定冲突用不可重试错误 `NarrationIntentConflictError`。
5. 物化孤儿行（0 候选，疑为部署重启窗口写入竞态）→ 读取容忍 + 重写前精确删除 + 就绪判定候选感知 + 编排器幂等补踢。
6. 故事板审核拦截 8 组（output_moderation/通用错误）→ 用户授权恢复重试后全部成功。
7. 视频提示词编译 12 组失败（输出不确定性）→ AUTO 双修复预算 + 网络临时故障入共享预算。
8. Docker 宿主三次崩溃 → 合成失联工作流对账（自动标记失败+重排）、FFmpeg 取消（media_command_cancelled）纳入自动重试（purge 门禁保护）、合成工作流 ID 轮次命名空间（修复实施中引入的跨轮 `g2` 唯一约束冲突）、对账调用加异常护栏防循环崩溃。
9. AUTO 首版合成未自动设为当前 → 成功后自动切换（§15.10）；本次运行该版本（`56ff975e`）为手动切换（既有端点）。

## 5. 最终核算（已写入授权记录）

- CosyVoice 644 次尝试；前置导演 grsai 63 次单元调用；故事板 99 次；DeepSeek 拆句 5 次；视频提示词编译 91+ 次（未逐条持久化）；H3 视频 93 次尝试；合成 4 次任务；诊断直连约 4 次。
- 耗时约 24 小时（2026-09-16 16:20 提交 → 2026-09-17 约 16:10 完成），授权轮次 11。
- 卡点主因：H3 GPU 生成时长、上述异常分支修复、Docker Desktop 三次崩溃。

## 6. 环境/部署现状

- 共享 PostgreSQL 迁移已到 0029；web/worker 为最新代码且 healthy；MinIO/Temporal 未动。
- `.env` 新增：`TUBEFLOW_SENTENCE_SPLIT_PROVIDER_TOKEN`（值复制自 `TUBEFLOW_VIDEO_PROMPT_PROVIDER_TOKEN`）。
- 可配新项：`h3_node_gpu_status_urls`、`h3_gpu_agent_token`、`sentence_split_provider_*`、`sentence_split_estimated_seconds_threshold`（默认 12s）、`auto_pipeline_sweep_interval_seconds`（默认 20s）、`composition_stale_reconcile_seconds`（默认 2100s）。
- Docker Desktop 宿主机不稳（三次崩溃），建议提高内存上限；容器重启由 Temporal 耐久 + outbox + 编排器巡检自愈。

## 7. 已知遗留/待办（未做或未验证，勿当作已完成）

- **GPU 采集脚本未部署**：三台 H3 机器需运行 `scripts/h3_gpu_status_agent.py`（设 `H3_GPU_AGENT_TOKEN`），并把探测 URL 配入 `.env`；不配则停滞判定无 GPU 感知。
- **集成测试（真实 Postgres）只在早期门禁跑过一次**（96 通过）；后续的配音续投、物化孤儿行、合成对账、编译修复等改动仅经单元/sqlite 级验证，PG 级集成回归未重跑（`TUBEFLOW_TEST_POSTGRES_URL` 指向隔离库后跑 `backend/tests/integration`）。
- **视频历史尝试行的 budget_round 为 1**（过渡期旧镜像产生）；当前代码已单测证明按轮标记正确，存量行不回改。
- **超长句兜底路径未真实触发**：本次 321 句全部实测 ≤15 秒（拆句 agent 生效），"实测仍超 15 秒整项目暂停"的分支只有自动化测试覆盖。
- 真实异常分支实测只覆盖了：CosyVoice 停机 4xx、grsai 虚假 400、审核拦截、编译不确定性、Worker 崩溃失联。H3 停滞/QC_FAILED/429 限流等未做真实触发验证。
- 前端聚合视图的后端联调仅经本次运行验证；`reason_code`/phase 映射已对齐真实码值。
- 项目存在历史遗留在途旧项目（MANUAL）：行为不变。

## 8. 新会话操作要点（本机环境陷阱）

- 后端 Python：`./.venv/Scripts/python.exe`（项目根）；pytest 需 `-p no:cacheprovider --basetemp=../tmp/<name>`（Windows 权限问题）。
- 后端文件 CRLF 行尾：Edit 工具可能失败，用 `open(..., newline="")` 的 Python 脚本精确替换（先断言唯一匹配）。
- **Bash 工具参数里的 `\uXXXX`/`\r` 等转义会被解码成真实字符**，脚本内用 `chr(0x2014)` 等构造。
- git-bash 路径转换坑：docker 命令含绝对路径时 `export MSYS_NO_PATHCONV=1`。
- Docker 构建慢（常超 10 分钟）：用后台任务 + 长超时；`docker compose up` 后 web 健康检查需等约 45 秒。
- 业务规则与授权：动手前必读 `docs/planning/USER_DECISIONS.md`（§15 为最新）、`docs/planning/AGENT_AUTHORIZATION.md`、`runtime/operations/AUTHORIZATIONS.md`；真实付费调用逐次报批并记录次数与耗时。
- 登录验收账号：用户名 `admin-74d1abc03713`（密码向用户索取；本文件不存凭据）。
