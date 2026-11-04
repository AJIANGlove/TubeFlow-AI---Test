# 自动生产「恢复」点击无效 — 第 8 组导演方案结构违规（2026-11-04）

交接对象：Claude（后续分析 / 修复）。
状态：仅只读排查，未改任何代码、数据、容器或配置；未触发任何外部 AI 调用。
项目：`2c291aba-085d-499d-bd32-b3a4d75f252f`（`여름 메뉴인 줄 알았지? 빙수 장사의 진짜 수익 구조`，ko-KR / 16:9，107 组 / 496 句）。

---

## 0. 一句话结论

**「恢复自动执行」不是接口坏了，而是这次的数据让恢复必然无效**：恢复只做 `PAUSED→RUNNING` + 授权轮次 +1 + 同步踢一次 `advance`，而 `advance` 进入 `storyboard_drafts` 后执行的是同一段确定性「导演草稿播种」，第 8 组的候选方案本身违反领域不变量（组内非相邻镜头场景/景别/视角完全重复），播种必然再次抛错，于是 **3~7 毫秒内重新 PAUSED**，HTTP 仍返回 200，前端照常弹「已恢复自动执行」。

更上一层：该违规方案本来应该在**物化阶段**被拦下（`group_diversity_invalid`），但该校验被 `if ... and not blocking:` 条件跳过——第 8 组已有阻断型冲突 `source_preplan_quality_conflict`，于是校验分支整体没执行，违规镜头原样入库。

---

## 1. 现象

- 项目详情页显示：`自动生产已暂停：导演方案校验未通过，需人工修正`
  - 「导演方案校验未通过的分组：08（共 1 组）。失败原因：第 8 组：director plan repeats an event or composition across the group」
  - 「原因码 director_validation_failed · 当前阶段 导演草稿播种」
- 点击「恢复自动执行」：出现成功提示，但页面立即回到同一条暂停横幅；重复点击无任何进展。
- 进度：真实配音 496/496 完成；导演规划 54/54 前置单元；**故事板 0/107 组**；组视频 0/107；未合成。
- 截图：`evidence/autoresume_group08/ui_paused_banner.png`

---

## 2. 环境

| 项 | 值 |
| --- | --- |
| 部署机 | `edy@192.168.110.200`，项目目录 `/home/edy/tubeflowai-test` |
| 镜像 | `sha256:8278db3ed79d93f24d3e7f644884022d3926649790f7bff18f85cc2fdd4e0567`，构建于 `2026-11-04T00:17:23Z` |
| 容器 | web=`tubeflowai-test-web-1`（compose 服务 web，容器名非默认）、worker=`tubeflowai-test-worker-1`（同名镜像，两者均 healthy） |
| 迁移 | `alembic_version = 0046` |
| 代码取证位置 | 容器内 `/usr/local/lib/python3.12/site-packages/tubeflow_test/`；已核对 `auto_pipeline/service.py`、`application/project_director.py` 与 `/app/src/tubeflow_test/` **逐字节一致**（`diff -q` 无输出） |

---

## 3. 证据文件索引（同目录 `evidence/autoresume_group08/`）

| 文件 | 内容 |
| --- | --- |
| `db_export.txt` | 13 段数据库取证：项目、运行行、payload、物化、分组/草稿统计、第 8 组镜头字段、导演当前指针 |
| `candidates_summary.csv` | 107 组逐行：`group_sequence / group_id / review_state / has_final_revision / conflict_codes / draft_revisions` |
| `group08_candidate.json` | 第 8 组候选完整行（shots、links、conflicts、review_state、final_revision_id） |
| `repro_check.py` / `repro_output.txt` | 按 `domain/storyboards.py` 规则独立复算第 8 组重复项（不导入产品代码、不写数据） |
| `logs_resume_pause.txt` | web/worker 的暂停行与 resume 请求行逐字转录（人工整理，非原始抓取；复现命令见 §7） |
| `ui_paused_banner.png` | 用户提供的暂停界面截图 |
| `code/00_versions.txt` | 镜像、alembic、各引用文件行数、site-packages 与 /app/src 一致性 |
| `code/01…09_*.py/js` | 部署镜像内逐字摘录的关键代码段（含行号注释） |

> 说明：本机终端对部分长十六进制串的**显示**会被改写；上述文件是字节级重定向产物，内容正确（已用 `od` 校验）。引用 UUID 时以 `db_export.txt` 为准。

---

## 4. 事实链（时间均为 UTC，界面为 UTC+8）

### 4.1 运行记录（`auto_pipeline_runs`，单行原地更新）

```
id                  = 78a56ed9-599e-4df1-ac90-a01d68be0ecf
project_id          = 2c291aba-085d-499d-bd32-b3a4d75f252f
state               = PAUSED
reason_code         = director_validation_failed
authorization_round = 7            # 已恢复过 6 次
created_at          = 2026-11-04 03:28:43.597503+00
updated_at          = 2026-11-04 07:19:54.443654+00
```

payload（`db_export.txt` §3）：

```json
{
  "phase": "storyboard_drafts",
  "failure_code": "director_output_adjacent_diversity_insufficient",
  "group_sequences": [8],
  "failure_messages": {"8": "director plan repeats an event or composition across the group"},
  "phase_elapsed_ms": {"narration": 1099739, "director_preplan": 4714170},
  "director_failed_run_id": "8ff8975f-2b45-45c3-8d42-796c35bb6c1f"
}
```

### 4.2 日志时序（web 容器 `docker logs -t`）

| 时间 | 事件 |
| --- | --- |
| 06:08:45.008 | `POST …/auto-pipeline/resume` 200（**这次恢复有效**，之后继续跑了约 30 分钟） |
| 06:38:41.272 | worker：`auto pipeline paused … reason=director_preplan_failed phase=director_preplan` |
| 06:53:41.919 | `POST …/resume` 200（**再次有效**，随后重跑前置导演） |
| 07:00:09.128 | 生成物化（`project_director_materializations`，status=REVIEW_REQUIRED） |
| 07:00:13.692 | worker：`auto pipeline paused … reason=director_validation_failed phase=storyboard_drafts` |
| 07:16:09.840 / .843 | web：先 `auto pipeline paused`（同一原因/阶段），**3 ms 后**才打印该 resume 请求的 200 |
| 07:17:20.752 / .755 | 同上，间隔 3.7 ms |
| 07:19:54.443 / .451 | 同上，间隔 7.2 ms |

暂停日志来自 `auto_pipeline/service.py:745`（web、worker 同一实现），且紧邻 resume 请求的访问日志，说明**暂停发生在 resume 请求线程内**，不是别的调度器。

### 4.3 分组与草稿分布

| 指标 | 值 |
| --- | --- |
| `project_director_group_candidates` | 107 行 |
| `review_state=REQUIRED` / `NOT_REQUIRED` | 41 / 66 |
| `storyboard_director_revisions` | 106 行（`DRAFT/APPROVED` 10 + `DRAFT/NOT_REQUIRED` 96） |
| 第 8 组草稿修订数 | **0** |
| `storyboard_generations` | 0 行 |
| `video_batch_items` / `composition_versions` | 0 / 0 |
| `director_generation_attempts`（全项目） | **0 行** |

即：107 组里 106 组已成功播种草稿，唯独第 8 组一条都没有；故事板图片一张都没生成。

### 4.4 第 8 组镜头（`group_sequence=8`，7 句）

| order | scene | shot_size | camera_viewpoint |
| --- | --- | --- | --- |
| 1 | 빙수점 - 계산대 장부 | CLOSE_UP | OVERHEAD |
| 2 | 빙수점 - 카드 단말기 옆 | EXTREME_CLOSE_UP | OBJECTIVE |
| 3 | 빙수점 - 계산대 앞 | MEDIUM | EYE_LEVEL |
| 4 | 빙수점 - 카드 결제대 | CLOSE_UP | OVER_SHOULDER |
| 5 | 빙수점 - 계산대 장부 위 | CLOSE_UP | OVERHEAD |
| 6 | 빙수점 - 계산대 장부 | CLOSE_UP | OVERHEAD |

- 第 1 与第 6 镜三元组**完全相同**，位置差 5（非相邻）→ 命中 `repeats_non_adjacent(composition_keys)`。
- `repro_check.py` 复算结果：`composition 非相邻重复: [(0, 5, ('빙수점-계산대장부','close_up','overhead'))]`，`event 非相邻重复: []`。
- 该组 `review_state=REQUIRED`、`final_revision_id=NULL`，冲突为 3 条 `source_preplan_quality_conflict`，**没有** `group_diversity_invalid`。

### 4.5 物化

```
id                    = a78f3daf-8964-43ce-8293-35d512591334
preplan_revision_id   = 5b7808d6-e9bb-45e0-9863-fbd700b6f271
algorithm_version     = controlled-semantic-lineage-remap-v5
status                = REVIEW_REQUIRED
created_at            = 2026-11-04 07:00:09.12843+00
```

---

## 5. 根因

### 5.1 直接原因：恢复动作对这类暂停是**幂等空转**

代码路径（镜像内实读，见 `code/`）：

1. `identity_project/router.py:672` `resume_auto_pipeline` → `pipeline.resume(...)`，返回 `{state, reason_code, authorization_round}`。
2. `auto_pipeline/service.py:152 resume()`：
   ```python
   run = self._runs.resume_paused(owner_id, project_id, now=self._clock())
   self.kick(owner_id, project_id)   # kick = advance，同步执行
   return run                         # 返回 kick 之前的 RUNNING 快照
   ```
3. `adapters/persistence/auto_pipeline.py:183 resume_paused()`：**只**校验 PAUSED、`auto_budget_round + 1`、置 RUNNING。**不清理、不重算、不重新生成任何分组方案，也不重置失败计数。**
4. `auto_pipeline/service.py:132 advance()` → `_advance_locked()`；物化已就绪、故事板未完成 → 进入 `L280` 分支：
   - `seed_drafts(auto_approve_ambiguous=True)`（`application/storyboard_batch.py:154`）对「无草稿且无已应用修订」的组逐组调用 `director.edit_draft(...)`；
   - 第 8 组用候选镜头重建 `DirectorPlan`，在 `domain/storyboards.py:358` 抛 `ValueError("director plan repeats an event or composition across the group")`；
   - 异常被 `except (DomainError, ValueError)` 收成 `StoryboardBatchSeedResult(..., "error", message=str(exc))`，第 8 组仍然没有草稿。
5. `service.py:306-329`：`invalid = [无草稿或草稿 REQUIRED 且未应用]` 仍含第 8 组 → `_pause(run, REASON_DIRECTOR_VALIDATION_FAILED, phase="storyboard_drafts", details={group_sequences:[8], failure_messages:{...}})`。

因为第 4 步完全确定性、没有任何外部调用、也没有「重生成导演方案」的动作，**恢复多少次结果都相同**：`authorization_round` 已到 7（06:00 之后日志里可见 5 次 resume 请求），而 107 组候选、106 份草稿、第 8 组 0 草稿的分布没有任何变化。

### 5.2 数据原因：违规方案来自 preplan 重映射，且从未经过组内校验

- 第 8 组的镜头不是逐组导演 AI 产出：`director_generation_attempts` 全项目 0 行；镜头由前置导演 preplan 经 `controlled-semantic-lineage-remap-v5` 重映射物化而来。
- 物化时该组被标记 `REVIEW_REQUIRED`（冲突码 `source_preplan_quality_conflict`），但 `shots` 仍原样落库，直到 AUTO 播种时才第一次被 `DirectorPlan` 构造校验发现违规。

### 5.3 代码缺陷：物化阶段的结构校验被 `not blocking` 短路

`application/project_director.py`（见 `code/05_…py`）：

```python
blocking = _blocking_conflicts(tuple(conflicts))
review_state = ReviewState.REQUIRED if blocking or ambiguous else ReviewState.NOT_REQUIRED
final_revision = None
if (new_shots and len(new_shots) <= DIRECTOR_GROUP_SHOT_LIMIT and not missing and not blocking):
    try:
        final_revision = DirectorPlanRevision(...)   # ← 唯一做「非相邻重复」等不变量校验的地方
    except ValueError:
        conflicts.append(ProjectDirectorConflict("group_diversity_invalid", ...))
        review_state = ReviewState.REQUIRED
```

- `_blocking_conflicts` 只豁免审计型冲突 `shot_timing_converged`（`_AUDIT_ONLY_CONFLICT_CODES`），`source_preplan_quality_conflict` 属于阻断型。
- 于是第 8 组因已有阻断冲突，**整个 `DirectorPlanRevision` 构造与其 `except ValueError` 兜底都被跳过**：既不落 `final_revision`，也不产生 `group_diversity_invalid`，违规镜头照样写进候选。
- 这与数据完全吻合：第 8 组 `conflicts` 只有 3 条 `source_preplan_quality_conflict`，没有 `group_diversity_invalid`；而 `final_revision_id` 为 NULL。

### 5.4 为什么界面看起来像「成功了」

- `resume()` 返回的是 `kick()` **之前**的对象（RUNNING），所以接口 200 的响应体是 `state=RUNNING`，即便数据库此刻已经又变回 PAUSED。
- 前端 `app.js:5661` 的封装对任何 2xx 都弹固定文案「已恢复自动执行；相关对象预算已按规则重置。」，随后刷新又渲染回暂停卡片。
- 截图横幅里「需人工修正」的提示与「恢复自动执行」按钮同时存在；在产品语义上，这类暂停应走第 07 步人工修正（见 §6），按钮本身无法修复数据。

---

## 6. 产品内已存在的正确解卡路径（已核对代码，未执行）

`api/storyboards.py:208-243`：第 07 步保存导演草稿（`PUT /storyboard-groups/{group_id}/director/draft`）成功后，若 `revision.may_apply` 为真，会调用

```python
pipeline.resume_if_paused_for(owner_id, project_id, reason_code=REASON_DIRECTOR_VALIDATION_FAILED)
```

按原因码守卫自动续跑（授权轮次 +1）。也就是说：**必须先让第 8 组存在一份可应用的合法草稿**（把第 6 镜的 scene/shot_size/camera_viewpoint 改成与第 1 镜不同，或调整其他镜头顺序/构图），保存时才可能真正继续。

注意：第 8 组当前**没有任何草稿**，批量「保存导演草稿」走的是同一个 `seed_drafts`，会复现同一错误；只有提交一份改过的镜头列表（PUT）才有机会通过。

---

## 7. 复现与只读验证命令

在部署机 `/home/edy/tubeflowai-test` 下（`sudo` 不需要；`docker compose exec -T` 会吞掉后续 stdin，务必加 `< /dev/null`）：

```bash
P=2c291aba-085d-499d-bd32-b3a4d75f252f

# 运行状态
docker compose exec -T postgres psql -x -U tubeflow -d tubeflow \
  -c "select state, reason_code, authorization_round, updated_at from auto_pipeline_runs where project_id='$P';" < /dev/null

# 第 8 组镜头
docker compose exec -T postgres psql --csv -U tubeflow -d tubeflow -c "
select s->>'order', s->>'scene', s->>'shot_size', s->>'camera_viewpoint'
from project_director_group_candidates c, json_array_elements(c.shots_json::json) s
where c.project_id='$P' and c.group_sequence=8 order by (s->>'order')::int;" < /dev/null

# 恢复与暂停的时序（关键：暂停行早于同一请求的 200 行 3~7 ms）
docker logs -t --since 2026-11-04T06:00:00Z tubeflowai-test-web-1 2>&1 \
  | grep -E "auto pipeline paused: project=2c291aba|/auto-pipeline/resume"
```

本地复算（不连数据库）：

```bash
python "Pending issues/2026-11-04/evidence/autoresume_group08/repro_check.py"
```

---

## 8. 观察到但尚未解释的点（供 Claude 判断，勿当结论）

1. `payload.failure_code = director_output_adjacent_diversity_insufficient` 与 `reason_code = director_validation_failed` 不一致；`director_failed_run_id` 也像是前置导演 run 的残留。疑似 `run.paused(...)` 合并了旧 payload 字段（未验证）。
2. 41 个 `REQUIRED` 候选里，只有第 8 组在播种时报错；其余 40 组虽然物化阶段同样跳过校验，但重构 `DirectorPlan` 时未触发不变量（属于「恰好没重复」，不代表安全）。
3. 同型风险未评估：任何「已有阻断型 preplan 冲突 + 重映射后组内非相邻重复」的项目都会卡在同一处；由于 `seed_drafts` 对每组独立 try/except，只有真正违规的组会阻塞整个 AUTO。

---

## 9. 建议排查/修复方向（未实施，供决策）

> 以下为分析建议，不是已确认的产品决策；按 `AGENTS.md`，任何语义变化需用户确认并同步 `USER_DECISIONS.md` / 验收文档。

- **校验前置**：物化时无条件构造一次 `DirectorPlanRevision`（或单独调用等价的不变量检查），把 `group_diversity_invalid` 如实写入冲突；不要让 `not blocking` 短路掉唯一的结构校验。
- **恢复语义**：`director_validation_failed` 这类「方案结构违规」在 AUTO 下没有自愈路径，恢复按钮要么触发一次该组的导演重生成（受 §25/§27 次数上限约束），要么在接口/界面上明确「需人工修正，恢复不会生效」，不要返回 RUNNING 状态 + 成功提示。
- **响应一致性**：`resume` 返回 `kick` 之后的真实状态，或让前端按返回的 `state` 决定提示文案。
- **回归测试**：至少覆盖「候选存在阻断冲突且镜头非相邻重复 → 物化应产出 `group_diversity_invalid`」「resume 对不可自愈原因码的行为」「第 07 步改稿保存后按原因码续跑」。

---

## 10. 证据文件清单

```
Pending issues/2026-11-04/
├── AUTO_RESUME_NOOP_GROUP08_20261104.md      # 本文档
└── evidence/autoresume_group08/
    ├── db_export.txt
    ├── candidates_summary.csv
    ├── group08_candidate.json
    ├── repro_check.py
    ├── repro_output.txt
    ├── logs_resume_pause.txt
    ├── ui_paused_banner.png
    └── code/
        ├── 00_versions.txt
        ├── 01_auto_pipeline_service_advance_resume.py
        ├── 02_auto_pipeline_service_storyboard_branch.py
        ├── 03_storyboard_batch_seed_drafts.py
        ├── 04_domain_storyboards_nonadjacent_repeat.py
        ├── 05_project_director_materialization_validation.py
        ├── 06_persistence_resume_paused.py
        ├── 07_router_resume_endpoint.py
        ├── 08_api_storyboards_auto_resume_on_save.py
        └── 09_frontend_app_resume_handler.js
```
