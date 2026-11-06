# 下一步实施计划：端到端验证、隔帧停顿离线修补测试、集成测试欠账（2026-11-06）

| 项 | 内容 |
| --- | --- |
| 依据 | 用户 2026-11-06 08:04Z 在「TubeFlow Optimization Review」文档评论：第 4 项（终混 loudnorm、CRF 18）延后；其余三项出详细实施计划。 |
| 范围 | A. 一个小型 AUTO 项目端到端验证；B. 隔帧停顿（抖动 A）离线修补测试；C. 19 条既有集成测试失败的清理。 |
| 性质 | 计划，待用户确认第 0 节的 3 个决定后执行。A 会调用 Provider；B、C 不调用。 |

## 0. 需要用户先确认的 3 个决定

| # | 决定 | 推荐 | 理由 |
| --- | --- | --- | --- |
| D1 | 授权一个小型 AUTO 项目跑完全流程 | **由用户本人在页面新建并「提交」**（§15 的一次授权）。配置如下：<br>· 文案 2–3 分钟，约 10–14 个配音分组；<br>· 沿用现有频道画风与一个角色；<br>· 视频调用上限：H3 提交总数超过分组数 × 2 时，实施方暂停该项目并汇报。 | 重剪（§48）、末次合并（§50）、观测指标（§51.5）都还没有在真实项目上产出过；小项目成本低，又能覆盖整条链路。实施方不代建项目、不代点提交。 |
| D2 | 在部署机新起一个**测试专用 MinIO** 容器 | 用 docker-compose.yml 已钉死的 MinIO 镜像（本机已有，不下载）。<br>· 名称 `tubeflow-contract-minio`，只绑定 `127.0.0.1:59000`；<br>· 独立数据卷、独立随机账号；<br>· 不加入业务网络；<br>· 测试机经 SSH 隧道访问。 | 12 条集成测试缺 `TUBEFLOW_TEST_MINIO_ENDPOINT`，覆盖项目删除与共享资产清理、H3 归档等上线必需路径。测试库已有同样做法（独立的 `tubeflow-contract-pg`）。 |
| D3 | 在部署机用一次性容器做 B 的离线处理 | 用现行生产镜像起临时容器：`--rm`、`--network none`、`--cpus=2`，只做 ffmpeg 与 OpenCV 计算，不连库、不连存储。 | 镜像里已有 ffmpeg、OpenCV、numpy，不需要装任何新软件；限 CPU 并隔离网络，不影响线上 web/worker。 |

## 1. 通用约束（三个工作流都适用）

- **术语**：部署机 = 192.168.110.200；测试机 = 实施方机器；Claude 云端工作区 = Claude 写代码与跑单元测试的地方。
- **禁止**：
  - 禁止虚构；
  - 未运行、跳过或失败的检查如实写明，并注明在哪台机器上做的；
  - 除 A 之外不调用任何 Provider；
  - 不改 `.env`；
  - 不触碰保护名单项目（d725172f、a0e52ea0、2c291aba、23c59183、247462a4、faf27eb5、039e36e3、3206fa93、fd3bf04d、7cf2e79d、f881122d、ff67c5e9）。
- **部署**：任何会重启 worker 的部署按 §52 等待在途导演活动。本计划本身不需要部署。
- **证据与文档**：
  - 证据统一放 `Pending issues/2026-11-06/` 下各子目录；
  - 大于 40 MB 的媒体不入库，只记 sha256 与存放位置；
  - 数据库与对象存储的写操作只限本计划明确列出的项。

## A. 小型 AUTO 项目端到端验证（D1 确认后）

**目的**：在真实项目上第一次看到重剪、末次合并、观测指标的产出，同时检查整条链路在上线前是否健康。

**步骤：**
1. **开跑前只读核查**（部署机）：
   - H3 节点在线、`/group-videos/contract` 可用；
   - 没有其他 AUTO 运行在跑；
   - 记录现行配置：重剪开关与阈值 0.10、`director_parallelism`、视频提示词默认策略、逐格参考开关。
2. **用户新建并提交项目**（用户操作）。实施方记录 project_id 与提交时刻。
3. **运行中只读监控**（部署机），每 10 分钟一次快照，阶段变化时加一次：
   - 前置导演：运行轮次、单元状态分布、拒收原因计数；`response_diagnostics_json` 里出现 `adjacent-diversity-merge-v1` 的单元（合并前后的镜头数与被并的镜头对）。
   - 故事板：每组生成次数、`composition_recut_approvals_json` 非空的组（重剪距离与阈值）、`needs_review` 的组。
   - 视频：每组提交次数、QC 结果、梯子级数、节点；`probe_evidence_json->'motion_metrics'` 的 `status`、`held_share`、`hf`、`jitter_shots`。
   - 合成：是否成功、耗时、成片时长与分组数是否一致。
   - 全程：H3 提交总数；超过分组数 × 2 就暂停项目并汇报（只对这一个新项目操作）。
4. **结束后汇报**（部署机）：
   - 各阶段耗时；
   - 每组「隔帧占比 / HF / 抖镜头」表，与普查中 v4 单格的 60% 对照；
   - 重剪与合并出现了几次、在哪些组（没触发就写「未触发」）；
   - 所有失败码与重试次数。
5. **交用户目视**：
   - 重剪前后的故事板（§48.4、FINAL_ACCEPTANCE §45 第 7 项）；
   - 被合并单元的镜头计划；
   - 成片。

**验收**：
- 成片生成；
- 观测指标在每条通过 QC 的视频上都是 `status=ok`（FINAL_ACCEPTANCE §47 最后一项）；
- 重剪与合并如果触发，产出交给用户看过，结论由用户定。

**证据**：`Pending issues/2026-11-06/E2E_AUTO_RUN/`（快照、SQL、汇总表）。

## B. 隔帧停顿离线修补测试（D3 确认后；不调用 Provider）

**假设**：一拍二的片段里，每个姿态停留 2 帧。如果把第二帧换成运动补偿插出来的中间帧，每秒就恢复 24 个不同的姿态，隔帧停顿占比会明显下降。是否会带来变形、重影，要靠目视判断。

**方法**：只用 ffmpeg 自带滤镜，不引入 RIFE 等外部模型；那样需要用户另行决定。

| 臂 | 滤镜链 | 说明 |
| --- | --- | --- |
| B0 基线 | 原片，按同参数重编码 | 让三臂经过同样的编码损失，公平对照 |
| B1 运动补偿 | `mpdecimate` 去掉近重复帧（保留原时间戳）→ `minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1` 补回到 24 fps | 主候选 |
| B2 混合 | `mpdecimate` → `minterpolate=fps=24:mi_mode=blend` | 计算便宜，但会有重影，作对照 |

- **编码参数**：三臂一律 `libx264 -crf 18 -preset veryfast -pix_fmt yuv420p`，帧率固定 24。
- **帧数**：输出帧数必须与原片相同；不同则该臂判失败，不得截断或补帧凑数。
- **`mpdecimate` 阈值**：先用默认值；在 1 条样片上调到「一拍二窗口里每对第二帧被去掉、全帧率片段不被去掉」后，固定下来用于全部样片。调参过程记录在案。

**样片**（只读：从对象存储取已归档视频，读法同 `ab_export.py`）：

| 类别 | 选法 |
| --- | --- |
| 高停顿 | A/B 的 `3ca4653d`（第 12 组 S2，0.97）、`7460885c`（第 7 组 S1，0.49）；`484746c4` 第 7、3 组的当前视频（普查时 0.72、0.58） |
| 低停顿对照 | `0cc7c89c`（0.11）；普查中 v3 策略、隔帧占比为 0、运动窗口最多的 1 条（检查修补会不会损坏本来流畅的片子） |

**测量**（在一次性容器内，用镜像里的 `FfmpegOpenCvMotionExtractor` 与 `motion_metrics_evidence`）：
- 每条每臂：整条与逐镜的隔帧停顿占比、HF、抖镜头数；
- 帧数与时长；
- 清晰度：逐帧拉普拉斯方差均值，与 B0 比；
- 单条处理耗时。

**交用户目视**：每条做一个三臂并排的 MP4（B0 | B1 | B2，各缩到 704 宽），高停顿样片另做 0.5 倍慢放版。

**预先写定的判定**：B1 同时满足以下各条，才把「在归档或合成阶段加可选修补步骤」作为方案提交用户决定；否则如实报告。
- 4 条高停顿样片修补后隔帧占比都低于 0.2；
- 帧数与时长与原片完全一致；
- 低停顿对照的隔帧占比不升高，清晰度相对 B0 下降不超过 5%；
- 用户看并排视频，认为人物面部、手部和画面里的文字没有明显变形或重影。

**不在本次范围**：「隔帧占比超标就换 seed 重生成」要等 A 拿到真实分布后再议。产品代码本次不改。

**证据**：`Pending issues/2026-11-06/HELD_FRAME_INTERPOLATION_TEST/`（脚本、命令、结果表；并排视频合计 ≤ 40 MB 才入库，否则只记 sha256 与存放位置）。

## C. 19 条既有集成测试失败的清理（不调用 Provider）

当前状态（测试机，main 与分支两侧相同）：19 failed / 122 passed / 7 skipped。证据见 `evidence/integration_guard/gates/main/integration.log`。

| 子项 | 条数 | 原因 | 谁做 | 做法 |
| --- | ---: | --- | --- | --- |
| C1 期望值过时 | 2 | 写死迁移版本 `0039`（`test_cloned_voices_postgres.py:246`、`test_elevenlabs_pool_postgres.py:56`） | Claude 出 PR | 改为从 alembic `ScriptDirectory` 读当前 head |
| C1 对象存储守卫 | — | 测试会建桶、删对象，必须确保连的不是线上 MinIO | Claude 出 PR | 新增 `refuse_application_object_store`：目标 MinIO 存在线上桶 `tubeflow-private` 时直接判测试失败；所有读 `TUBEFLOW_TEST_MINIO_ENDPOINT` 的测试在建桶前调用它。附单元测试与源码扫描，做法同库守卫 |
| C2 缺测试 MinIO | 12 | 未配置 `TUBEFLOW_TEST_MINIO_*` | 实施方（D2 确认后） | 按 D2 起容器，随机账号写入 `runtime/operations`（不入 git）；集成运行时经隧道设置三个环境变量；在 DEPLOYMENT 记一节运维操作 |
| C3 待查 | 5 | 见下 | 实施方只读排查，Claude 据结果出修复 | 逐条用 `-vv --tb=long` 单独运行，并附下述核查 |

**C3 各条的线索与核查**（只读，不改测试库数据）：
1. `test_identity_project_postgres`：两次并发初始化都返回「管理员已初始化」。
   - 线索：`IdentityProjectRepository.initialize_admin`（`adapters/persistence/identity_project/repository.py:79`）把**任何** `IntegrityError` 都当成「已初始化」。如果后续迁移给 `accounts` 等表加了非空列或外键，而插入没有赋值，真实的首次部署也会被误报成「已初始化」。
   - 核查：临时在测试机的工作树里捕获并打印原始 `IntegrityError`（不提交），确认是哪个约束触发的。
2. `test_staged_upload_protocol` 3 条：32 路并发认领期望代号 2、实得 1；两条 `None is not None`。
   - 核查：用 `git log -L` 查 `RequestStagedStorage._record_pending` 与认领锁在测试写成之后的改动，判断是期望过时还是并发回归。
3. `test_video_postgres::test_postgres_round_trip_revision_cas_and_atomic_capacity`：「没有兼容的 H3 节点有空位」。
   - 核查：夹具节点是 `capability=video.reference_images_to_video`、`workflow_id=h3-r2v-images`、`workflow_version=1`，对照现行 `claim_compatible_node` 的兼容条件，判断是夹具过时还是兼容判定回归。
4. **测试库 `public` 里的数据来源**：
   - 只读查询 `tubeflowtest` 中 `public.accounts`、`public.projects`（含 `mig-probe`）的 `created_at` 与 `alembic_version`；
   - 与测试机上历次集成运行的时间比对；
   - 判断是否有测试或手工操作写进了 `public`。如果是测试写的，就会造成上面这类串扰。

**产出**：一张逐条结论表（期望过时 / 产品缺陷 / 环境问题，各附证据）。属于产品缺陷的，Claude 出最小修复 PR 并补测试；属于期望过时的，改测试。

**验收**：集成测试全部通过；或者每条剩余失败都有证据说明原因，并经用户认可暂留。

## 2. 顺序与并行

- **立即可做，三条可并行：**
  - Claude：C1 的 PR；
  - 实施方：C3 只读排查；
  - D3 确认后：B 的一次性容器处理。
- **D1 确认后**：用户新建并提交项目，A 开始。B 的容器限制在 2 个 CPU，并避开 A 的合成阶段，以免拖慢线上合成。
- **D2 确认后**：C2 起测试 MinIO，与 C1 一起重跑集成测试。
- 每一步完成后实施方各自汇报，Claude 复核后给出下一步。

## 3. 给实施方的提示词（B 与 C3，可立即执行；D2、D3 未确认的步骤不要做）

```
任务：执行 Pending issues/2026-11-06/NEXT_STEPS_IMPLEMENTATION_PLAN_20261106.md 中的 C3（只读排查）；
若用户已确认 D3，再执行 B（离线修补测试）。A、C2 等用户确认后另行通知，不要做。
不调用任何 Provider；不改 .env；不改任何数据库或对象存储内容；保护名单项目不触碰。
禁止虚构；未运行、跳过或失败的检查如实写明，并注明在哪台机器上做的。

C3：按计划第 C 节「C3 各条的线索与核查」逐条排查 5 条失败，以及测试库 public 的数据来源。
- 临时加的调试打印只留在测试机工作树，不提交；
- 结论表写入 Pending issues/2026-11-06/INTEGRATION_FAILURES_TRIAGE/README.md（逐条：现象、原始异常、根因、分类、证据路径）。

B（仅当 D3 已确认）：按计划第 B 节执行。
- 一次性容器参数：--rm、--network none、--cpus=2；
- 样片按表选取，从对象存储只读取片；
- 三臂参数逐字照计划；mpdecimate 阈值的调参过程写进记录；
- 输出结果表、并排视频、慢放视频，以及按预先写定判定得出的结论；
- 证据放 Pending issues/2026-11-06/HELD_FRAME_INTERPOLATION_TEST/。

汇报：C3 结论表；B 的结果表与判定（若执行）；未完成项与异常。
```
