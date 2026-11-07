# 2026-11-07 计划复核与下一步

| 项 | 内容 |
| --- | --- |
| 依据 | 用户 2026-11-07 要求对照最新 `main`（`b3b244a`）检查 11-06 计划；随后确认「yes」：做导演镜头数提示修复与文档整理，并出 3a 的实施提示词（USER_DECISIONS §53）。 |
| 本次补丁 | 分支 `claude/director-shot-count-notice-20261107`，基于 `b3b244a`；Claude 云端工作区对该仓库没有推送权限，以 `git am` 补丁交付。 |

## 1. 11-06 计划完成情况（对照仓库证据）

| 计划项 | 结论 | 证据 |
| --- | --- | --- |
| A：小型 AUTO 项目端到端 | **跑通**。9/9 故事板、9/9 视频、合成一次成功；9 条归档 `motion_metrics.status=ok`。重剪（§48）与末次合并（§50）**未触发**，FINAL_ACCEPTANCE §45 末项仍未验证。途中因 8 句单元撞上镜头上限暂停 1 次，用户确认后恢复 1 次。 | `2026-11-06/E2E_AUTO_RUN/` |
| B：隔帧停顿离线插帧 | **已做，结论为否**：4 条高停顿样片仅 1 条降到 0.2 以下，6 条全部丢帧。 | `2026-11-06/HELD_FRAME_INTERPOLATION_TEST/` |
| B 之后：超标换 seed 重生成 | **未做**（§51.5 只观测）。2026-11-07 用户目视成片后决定停止隔帧停顿后续工作（§53 第 2 项）。 | `HELD_FRAME_CLIP_CHECK/` |
| C：19 条集成失败 | **已清零**。PR #19（`bf22055`）合并后带测试 MinIO 全量 **146 passed / 3 skipped / 0 failed**；合并树 `backend/` 与受测提交 `797e75d` 逐字相同。3 条跳过日志未列名，与「2 条 Temporal 用例未配 `TUBEFLOW_TEST_TEMPORAL_TARGET`、1 条 FFmpeg 用例测试机无 ffmpeg」一致（推断）。 | `2026-11-07/C1_GATE_AND_INTEGRATION/` |

## 2. 本次补丁内容

1. **导演镜头数拒收的重试提示**（产品代码只改 `backend/src/tubeflow_test/application/project_director.py`）：
   - 拒收诊断记录 `returned_shot_count` 与 `shot_count_maximum`；
   - 下一次尝试的提示写明返回几镜、上限几镜，要求合并相邻句，并说明上限优先于逐句节奏；
   - 只改发往服务商的视图，持久化提示词、哈希与 canonical 不变；
   - 新增 `tests/unit/test_project_director_shot_count_notice.py`（9 条）。
2. **文档**：
   - USER_DECISIONS §53；
   - FINAL_ACCEPTANCE §50 新增，§47 末项勾选，§45 末项补记；
   - 把 `NEXT_STEPS_IMPLEMENTATION_PLAN_20261106.md` 从未合并的 `claude/next-steps-plan-20261106` 带入 `main`（`main` 上已有文档引用它）；
   - `HELD_FRAME_CLIP_CHECK/`（成片 0:13–0:37 的复算与用户目视结论）；
   - `SCENE_VARIETY_CHECK/`（3a 脚本与说明）。

## 3. PR #19 的部署

- PR #19 改了 `adapters/persistence/database.py`，但部署机 `.env` 的数据库 URL **不含 `options=`**（C1 SUMMARY 第 0 节），线上行为不变，**不需要单独部署**。
- 随本次镜像一起上线即可，部署记录里注明。

## 4. 给实施方的提示词

```
任务：应用并验收 Claude 2026-11-07 的补丁（导演镜头数拒收重试提示 + 文档），合并、部署；然后执行 3a 只读核查。
先读 AGENTS.md、USER_DECISIONS（新增 §53）、FINAL_ACCEPTANCE（新增 §50）、AGENT_AUTHORIZATION、Pending issues/2026-11-07/NEXT_STEPS_20261107.md。

通用：
- 全程不调用任何 Provider；不改 .env；保护名单项目不触碰。
- 禁止虚构；未运行、跳过或失败的检查如实写明，并注明在哪台机器上做的。
- 部署机 = 192.168.110.200；测试机 = 你的机器。

一、补丁（测试机）
1. 从 main b3b244a 新建分支 claude/director-shot-count-notice-20261107，用 git am 应用补丁文件 director-shot-count-notice-20261107.patch（2 个提交）。git am 失败就停下汇报，不手工修改。
2. 核对 git diff --stat main：产品代码只有 backend/src/tubeflow_test/application/project_director.py；其余为测试与文档。
3. 门禁（main 与分支，同机同命令）：
   - 单元全量：基线 2500 passed / 11 skipped。
     - 新文件 tests/unit/test_project_director_shot_count_notice.py 须 9 passed、0 skipped。
     - 下面 4 条在 Claude 云端工作区因缺 SQLAlchemy 未能运行，须在测试机实际通过：
       test_project_director.py::test_sql_reclaim_expired_units_scopes_terminal_runs_only
       test_project_director.py::test_sql_find_materialization_tolerates_orphan_row
       test_project_director.py::test_materialization_ready_requires_candidate_rows
       test_project_director_stall_fixes.py::test_sql_unit_mapping_round_trips_the_diagnostics
   - 集成：带测试 MinIO，环境与 C1 第 2 节相同；基线 146 passed / 3 skipped / 0 failed。日志加 -rs，列出 3 条跳过的用例名与原因。
   - ruff check .、mypy src：与 main 逐条相同。
   - npm 不跑（前端未改）。
4. 全部无新增失败才合并（--no-ff，提交信息写门禁数字）。有任何失败：逐条列原始异常，不合并，等 Claude 复核。
5. 证据放 Pending issues/2026-11-07/evidence/director_shot_count/gates/。

二、部署（部署机，合并后）
1. 按 DEPLOYMENT 标准流程：git archive → 独立 build 目录 → docker build → 改 compose 三处 image → up -d --no-build。
2. 重启 worker 前按 USER_DECISIONS §52 查两项（LEASED/SUBMITTING 单元数、pending advance_project_director 数），记录每次复查的时刻与计数。
3. 部署后：/health/live 200，/group-videos/contract available:true。
4. 在 docs/DEPLOYMENT.md 写下一节：镜像 digest、§52 计数与等待时长、本镜像同时带上 PR #19 的 database.py 修复（部署机 .env 无 options=，行为不变）。
5. 不为验证本改动触发任何生成。之后首次自然出现 director_output_shot_count_invalid 时，记录重试提示是否含镜头数与上限、下一次是否通过（FINAL_ACCEPTANCE §50 末项）。

三、3a 只读核查（部署机，可与第一、二步并行）
1. 按 Pending issues/2026-11-07/SCENE_VARIETY_CHECK/README.md 执行：先 MODE=candidates 选项目，再 MODE=export 导出并计算。只读，不写库、不调 Provider。
2. 选项目规则：最近完成、配音句数 ≥150、不在保护名单、单元提示词为 v9 或 v10 的第一个 AUTO 项目；没有符合的就停下汇报，不放宽。
3. 首次运行脚本报错时，如实记录原始异常后停下，不改库、不自行改写查询口径。
4. 结果放 Pending issues/2026-11-07/SCENE_VARIETY_CHECK/results/。

汇报（各项完成即报）：
- 一：门禁数字、新文件与 4 条 SQL 用例的结果、3 条跳过用例名、是否合并。
- 二：镜像 digest、§52 记录、健康检查。
- 三：项目 ID 与标题、配音句数、组数、成片时长、导演提示词版本；章节数及是否到 24；每章句数/秒数/组数（最小/中位/最大）；最长 3 段的章节、visual_beat、setting、组号、时段、秒数、镜头数、不同场景文本数；最长一段在成片中的起止时刻（交用户观看）。
```
