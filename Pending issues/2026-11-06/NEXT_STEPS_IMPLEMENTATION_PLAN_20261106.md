# 下一步实施计划：端到端验证、隔帧停顿离线修补测试、集成测试欠账（2026-11-06）

| 项 | 内容 |
| --- | --- |
| 依据 | 用户 2026-11-06 08:04Z 在「TubeFlow Optimization Review」文档评论：第 4 项（终混 loudnorm、CRF 18）延后；其余三项出详细实施计划。 |
| 范围 | A. 一个小型 AUTO 项目端到端验证；B. 隔帧停顿（抖动 A）离线修补测试；C. 19 条既有集成测试失败的清理。 |
| 状态 | **D1–D3 已确认**（用户 2026-11-06 评审第 0 节时回复：“No problem. However, for the small auto project, you just need to set the title and content and then conduct the test and monitoring directly.”）。据此修订 D1：标题与文案由 Claude 定（A.2），实施方直接新建、提交并监控。A 会调用 Provider；B、C 不调用。 |

## 0. 用户决定（2026-11-06 已确认）

| # | 决定 | 内容 | 理由 |
| --- | --- | --- | --- |
| D1 | 授权一个小型 AUTO 项目跑完全流程 | **已确认，并按用户意见修订**：<br>· 标题与文案由 Claude 定（见 A.2）；<br>· 实施方按页面同样的接口新建项目并「提交」（§15 的一次授权），随后监控；<br>· 视频调用上限：H3 提交总数超过分组数 × 2 时，实施方暂停该项目并汇报。 | 重剪（§48）、末次合并（§50）、观测指标（§51.5）都还没有在真实项目上产出过；小项目成本低，又能覆盖整条链路。 |
| D2 | 在部署机新起一个**测试专用 MinIO** 容器 | **已确认**：<br>· 用 docker-compose.yml 已钉死的 MinIO 镜像（部署机已有，不下载）；<br>· 名称 `tubeflow-contract-minio`，只绑定 `127.0.0.1:59000`；<br>· 独立数据卷、独立随机账号；<br>· 不加入业务网络；<br>· 测试机经 SSH 隧道访问。 | 12 条集成测试缺 `TUBEFLOW_TEST_MINIO_ENDPOINT`，覆盖项目删除与共享资产清理、H3 归档等上线必需路径。测试库已有同样做法（独立的 `tubeflow-contract-pg`）。 |
| D3 | 在部署机用一次性容器做 B 的离线处理 | **已确认**：用现行生产镜像起临时容器：`--rm`、`--network none`、`--cpus=2`，只做 ffmpeg 与 OpenCV 计算，不连库、不连存储。 | 镜像里已有 ffmpeg、OpenCV、numpy，不需要装任何新软件；限 CPU 并隔离网络，不影响线上 web/worker。 |

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

## A. 小型 AUTO 项目端到端验证（D1 已确认）

**目的**：在真实项目上第一次看到重剪、末次合并、观测指标的产出，同时检查整条链路在上线前是否健康。

### A.1 参照项目与设置来源

以 `484746c4-ddf3-48af-90ca-a25cf41cbf35` 为参照（不在保护名单；A/B 实验所用项目；13 组；画风 S7；`v4_single_panel`）。新项目除标题与文案外，全部沿用它的设置，使结果可以与它及普查数据直接对比：

| 字段 | 取值 |
| --- | --- |
| `channel_id` | 与 484746c4 相同（预计为频道 `f28a0845`「韩国小生意经济财经」，以库中读到的为准） |
| `character_image_ids` | 与 484746c4 相同（顺序也相同） |
| `visual_style` | 与 484746c4 项目记录中的 `visual_style` 逐字相同 |
| `tts_provider` 与音色 | 与 484746c4 相同：CosyVoice 用同一 `voice_version_id`，ElevenLabs 用同一 `cloned_voice_id` |
| `locale` / `aspect_ratio` | `ko-KR` / `16:9` |
| `expected_duration_seconds` | 不传 |

- 开跑前只读读出上述各值并记录。若 484746c4 的 `locale` 不是 `ko-KR`，或任一引用已失效（接口以校验错误拒绝），**停下汇报，不替换为别的频道、角色或音色**。
- 频道的默认设置一律不改。

### A.2 标题与文案（Claude 定，2026-11-06）

- **标题**：`겨울 석 달 붕어빵 노점, 정말 얼마나 남을까?`（中文意思：冬季三个月的鲫鱼饼摊，到底能赚多少？）
- **文案**：`Pending issues/2026-11-06/E2E_AUTO_RUN/script_ko.txt`，26 句，UTF-8，sha256 `60c66b3279fdf4bfc1fa6800d4ba2310c76887ba81611acf3e55b22e3d73fc9f`。提交时按文件内容原样发送，不改字、不改换行。
- **在 Claude 云端工作区用仓库代码做的检查**：
  - `validate_script_language(ko-KR)` 通过；
  - `segment_pasted_script` 切为 26 句；
  - `estimate_narration_duration_ms` 估计合计约 156 秒、最长一句约 9.6 秒；
  - 按 13 秒阈值无超长风险句，拆句 agent 预计不调用模型；
  - 按每组不超过 15 秒粗算约 14 组（实际以配音实测为准）。
- **内容说明**：
  - 与该频道既有选题（小生意收益结构）一致；
  - 文中数字都在文案里声明为「便于理解的假设」，算术已核对（2000/3≈667，667−250≈417，300×25=7500，7500×2000/3=500 万，500 万−187.5 万=312.5 万，312.5 万−80 万=232.5 万，232.5 万÷250 小时=9300 元）；
  - 用「사장님」（老板）称呼主角，不出现性别代词，以免与频道角色图冲突；
  - 动作多（倒面糊、翻模具、排队、寒风），便于观察隔帧停顿与重剪。

<details>
<summary>文案中文对照（仅供用户阅读，不提交）</summary>

1. 冷风一吹，巷口就会陆续出现鲫鱼饼摊。
2. 这生意，真的赚钱吗？
3. 今天就拿一个假想的鲫鱼饼摊，算一算冬季三个月的账。
4. 以下数字都是为了便于理解的假设。
5. 先看价格。
6. 假设三个卖两千韩元。
7. 相当于一个约 667 韩元。
8. 面糊、红豆、油、纸袋加在一起，材料费按每个 250 韩元算。
9. 那么每卖一个约赚 417 韩元。
10. 一天能卖多少个？
11. 假设日均 300 个，每月营业 25 天。
12. 那么一个月 7,500 个，营业额 500 万韩元。
13. 扣掉材料费，约剩 312 万韩元。
14. 再加上燃气费、摊位费，以及整个冬天用的帐篷和取暖炉燃料。
15. 这些固定开销按每月 80 万韩元算，到手约降到 230 万韩元。
16. 而且这笔钱是老板一个人每天站 10 小时挣来的。
17. 按小时算，每小时不到 1 万韩元。
18. 所以老板最先在意的是减少扔掉的鲫鱼饼。
19. 客人少的时候提前烤一大堆，凉了最后只能扔掉。
20. 所以不会一次把模具全填满，而是看排队情况调节烤的量。
21. 第二是菜单。
22. 红豆鲫鱼饼旁边再放上奶油馅的，就会有客人多买一袋。
23. 最后的变数是天气。
24. 气温降到零下的日子队伍就长，暖和的日子客人就少。
25. 说到底，鲫鱼饼摊是冬季三个月里和天气、时间以及一勺面糊较劲的生意。
26. 下次买一袋鲫鱼饼时，不妨想想里面装着的这笔账。

</details>

### A.3 预计调用（§15.11 记录用；费用未知）

| 阶段 | 预计 | 上限（现行合同） |
| --- | --- | --- |
| 拆句 agent（DeepSeek） | 0 次（无风险句） | 每句 3 次 |
| 配音 | 26 句各 1 次 | 每句 3 次 |
| 前置导演（grsai） | 单元数由系统划分 | 每单元 3 次 |
| 故事板出图 | 每组至少 1 次；触发重剪的组多 1 次 | 每组 14 次（§25、§27） |
| 视频提示词编译（DeepSeek） | 每组至少 1 次 | 每组 3 次（含 2 次修复） |
| H3 视频 | 每组至少 1 次 | 每组 6 次（§26）；**本次另设总量闸门：超过分组数 × 2 即暂停** |
| 合成 | 1 次，部署机本地 ffmpeg | 3 次 |

耗时参照：开跑前只读查 484746c4（或同频道最近一个已完成的小项目）各阶段起止时间，记入运行记录。

### A.4 步骤

1. **记录授权**（`runtime/operations/`，按 AGENT_AUTHORIZATION 第 5 节）：
   - 用户原话与日期见本文开头的状态行；
   - 对象：本次新建的这一个项目；
   - 允许动作：新建 + 一次「提交」+ 下面第 5 步所列的至多 1 次恢复；
   - 上限：H3 总数不超过分组数 × 2；
   - 记录里不写文案全文。
2. **开跑前只读核查**（部署机）：
   - H3 节点在线、`/group-videos/contract` 可用；
   - 列出正在运行的其他 AUTO 运行（只记录，不操作；有的话照常开跑，耗时对比时注明）；
   - 记录现行配置：重剪开关与阈值 0.10、`director_parallelism`、视频提示词默认策略、逐格参考开关；
   - 读出 A.1 各字段。
3. **新建并提交**（部署机，走页面使用的同一组接口）：
   - 以 484746c4 所属账号登录（`POST /identity/login`）。没有该账号的登录凭据时停下汇报；不得重置密码、新建账号或伪造会话。
   - `POST /identity/projects`：`title` 用 A.2 的标题，`script` 用文件原文，其余字段按 A.1。
   - 读回项目：`script` 与文件逐字相同，`production_mode=AUTO`。
   - `POST /narration/confirm`，请求体 `{"project_id": "<新项目 id>"}`，这一步就是「提交」。
   - 记录 project_id、两次请求与响应（去掉 Cookie/Token）、提交时刻。
4. **运行中只读监控**（部署机），每 10 分钟一次快照，阶段变化时加一次：
   - 前置导演：运行轮次、单元状态分布、拒收原因计数；`response_diagnostics_json` 里出现 `adjacent-diversity-merge-v1` 的单元（合并前后的镜头数与被并的镜头对）。
   - 故事板：每组生成次数、`composition_recut_approvals_json` 非空的组（重剪距离与阈值）、`needs_review` 的组。
   - 视频：每组提交次数、QC 结果、梯子级数、节点；`probe_evidence_json->'motion_metrics'` 的 `status`、`held_share`、`hf`、`jitter_shots`。
   - 合成：是否成功、耗时、成片时长与分组数是否一致。
   - 全程：H3 提交总数。
5. **暂停与恢复**（只对这一个新项目）：
   - H3 提交总数超过分组数 × 2：按 DEPLOYMENT §35 暂停 `d725172f` 时的同一做法暂停（`manual_deploy_pause`），然后汇报。
   - 系统自行暂停时：
     - 原因是服务商侧临时故障（如 grsai `get image url failed`、5xx、超时）：可通过正式接口 `POST /identity/projects/{id}/auto-pipeline/resume` 恢复 **至多 1 次**，H3 总量闸门照旧，跨轮累计；
     - 其他原因（校验失败、实测超长句、`h3_prompt_shot_blocks_invalid` 等），或已恢复过 1 次：不再恢复，汇报原因码与证据。
6. **结束后汇报**：
   - 各阶段耗时；
   - 每组「隔帧占比 / HF / 抖镜头」表，与普查中 v4 单格的 60% 和 484746c4 对照；
   - 重剪与合并出现了几次、在哪些组（没触发就写「未触发」）；
   - 所有失败码与重试次数；
   - 各阶段实际调用次数，与 A.3 对照。
7. **交用户目视**：
   - 重剪前后的故事板（§48.4、FINAL_ACCEPTANCE §45 第 7 项）；
   - 被合并单元的镜头计划；
   - 成片。

**验收**：
- 成片生成；
- 观测指标在每条通过 QC 的视频上都是 `status=ok`（FINAL_ACCEPTANCE §47 最后一项）；
- 重剪与合并如果触发，产出交给用户看过，结论由用户定。

**证据**：`Pending issues/2026-11-06/E2E_AUTO_RUN/`（文案文件已在；快照、SQL、请求记录、汇总表）。

## B. 隔帧停顿离线修补测试（D3 已确认；不调用 Provider）

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

**与 A 的关系**：容器限 2 个 CPU，并避开 A 的合成阶段，以免拖慢线上合成。

**证据**：`Pending issues/2026-11-06/HELD_FRAME_INTERPOLATION_TEST/`（脚本、命令、结果表；并排视频合计 ≤ 40 MB 才入库，否则只记 sha256 与存放位置）。

## C. 19 条既有集成测试失败的清理（不调用 Provider）

当前状态（测试机，main 与分支两侧相同）：19 failed / 122 passed / 7 skipped。证据见 `evidence/integration_guard/gates/main/integration.log`。

| 子项 | 条数 | 原因 | 谁做 | 做法 |
| --- | ---: | --- | --- | --- |
| C1 期望值过时 | 2 | 写死迁移版本 `0039`（`test_cloned_voices_postgres.py:246`、`test_elevenlabs_pool_postgres.py:56`） | Claude 出 PR | 改为从 alembic `ScriptDirectory` 读当前 head |
| C1 对象存储守卫 | — | 测试会建桶、删对象，必须确保连的不是线上 MinIO | Claude 出 PR | 新增 `refuse_application_object_store`：目标 MinIO 存在线上桶 `tubeflow-private` 时直接判测试失败；所有读 `TUBEFLOW_TEST_MINIO_ENDPOINT` 的测试在建桶前调用它。附单元测试与源码扫描，做法同库守卫 |
| C2 缺测试 MinIO | 12 | 未配置 `TUBEFLOW_TEST_MINIO_*` | 实施方（D2 已确认） | 见下 |
| C3 待查 | 5 | 见下 | 实施方只读排查，Claude 据结果出修复 | 逐条用 `-vv --tb=long` 单独运行，并附下述核查 |

**C2 做法**（部署机）：
1. 确认 docker-compose.yml 中钉死的镜像 `minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` 已在部署机（`docker image inspect`）；不在则停下汇报，不拉取。
2. 起容器：
   - 名称 `tubeflow-contract-minio`，`--restart unless-stopped`；
   - 只发布 `127.0.0.1:59000:9000`，不发布控制台端口；
   - 独立数据卷 `tubeflow-contract-minio-data`；
   - 不加入 compose 的业务网络；
   - 随机生成 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`，写入 `runtime/operations/`（不入 git）。
3. 核对：
   - 新实例中没有任何桶；
   - 线上 MinIO 容器与数据卷未改动（前后对比 `docker inspect` 的启动时刻与挂载）。
4. 测试机经 SSH 隧道访问，集成运行时设置 `TUBEFLOW_TEST_MINIO_ENDPOINT`、`TUBEFLOW_TEST_MINIO_ACCESS_KEY`、`TUBEFLOW_TEST_MINIO_SECRET_KEY`。
5. **完整集成重跑等 C1 合并后再做**（C1 带对象存储守卫）。在此之前只做第 1–3 步与一次连通性检查。
6. 在 DEPLOYMENT 记一节运维操作（容器、端口、数据卷、账号存放位置、回滚方法：`docker rm -f` 加删除该数据卷，只限这两个对象）。

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

- **现在就可以做，彼此并行**：
  - 实施方：A（新建、提交、监控）；
  - 实施方：B（一次性容器，避开 A 的合成阶段）；
  - 实施方：C2 第 1–3 步与连通性检查；C3 只读排查；
  - Claude：C1 的 PR。
- **C1 合并后**：实施方带测试 MinIO 重跑完整集成测试。
- 每一步完成后实施方各自汇报，Claude 复核后给出下一步。

## 3. 给实施方的提示词（A、B、C2、C3）

```
任务：执行 Pending issues/2026-11-06/NEXT_STEPS_IMPLEMENTATION_PLAN_20261106.md（PR #18 分支 claude/next-steps-plan-20261106 的最新版）中的 A、B、C2、C3。
用户已于 2026-11-06 确认 D1–D3（原话见计划开头的状态行）。C1 由 Claude 出 PR；C1 合并前不要带 MinIO 跑完整集成测试。

通用：
- 先读 AGENTS.md、USER_DECISIONS、FINAL_ACCEPTANCE_CRITERIA、AGENT_AUTHORIZATION。
- 只有 A 调用 Provider，且只限 A 新建的这一个项目；B、C 不调用。
- 不改 .env；不改频道默认设置；保护名单项目不触碰。
- 禁止虚构；未运行、跳过或失败的检查如实写明，并注明在哪台机器上做的。
- 部署机 = 192.168.110.200；测试机 = 你的机器。

A（计划 A.1–A.4，逐字照做）：
- 先在 runtime/operations/ 记录本次授权（A.4 第 1 步）。
- 开跑前只读核查，并读出 484746c4 的频道、角色图、画风、配音源与音色、locale。locale 不是 ko-KR，或有引用失效时，停下汇报。
- 用 484746c4 所属账号登录，调用 POST /identity/projects 新建项目：
  - 标题：겨울 석 달 붕어빵 노점, 정말 얼마나 남을까?
  - 文案：E2E_AUTO_RUN/script_ko.txt 原样（sha256 60c66b32…3fc9f）
  - 其余字段同 484746c4
- 读回核对后，调用 POST /narration/confirm 提交。没有该账号的登录凭据就停下汇报，不得重置密码、新建账号或伪造会话。
- 每 10 分钟只读快照，阶段变化时加一次。
- H3 提交总数超过分组数 × 2 时，按 DEPLOYMENT §35 的做法暂停，然后汇报。
- 系统自行暂停时，仅当原因是服务商侧临时故障，才可用正式接口恢复，至多 1 次；其他原因停下汇报。
- 证据放 Pending issues/2026-11-06/E2E_AUTO_RUN/。
- 结束后按 A.4 第 6、7 步汇报，并把重剪前后故事板、被合并单元的镜头计划、成片交用户目视。

B（计划第 B 节）：
- 一次性容器：--rm、--network none、--cpus=2；避开 A 的合成阶段。
- 样片按表选取，从对象存储只读取片。
- 三臂参数逐字照计划；mpdecimate 阈值的调参过程写进记录。
- 输出结果表、并排视频、慢放视频，以及按预先写定判定得出的结论。
- 证据放 Pending issues/2026-11-06/HELD_FRAME_INTERPOLATION_TEST/。

C2（计划第 C 节「C2 做法」第 1–3、6 步，以及一次连通性检查）：
- 镜像必须是 compose 钉死的那个 digest，部署机上没有就停下，不拉取。
- 只发布 127.0.0.1:59000；不加入业务网络；账号写入 runtime/operations/。
- 前后对比线上 MinIO 容器与数据卷，证明未改动。

C3（计划第 C 节「C3 各条的线索与核查」）：
- 逐条排查 5 条失败，以及测试库 public 的数据来源。
- 临时加的调试打印只留在测试机工作树，不提交。
- 结论表写入 Pending issues/2026-11-06/INTEGRATION_FAILURES_TRIAGE/README.md，逐条写明：现象、原始异常、根因、分类、证据路径。

汇报（各项完成即报，不必等齐）：
- A：project_id、各阶段进度与耗时、每组观测指标表、重剪与合并情况、实际调用次数、暂停或恢复记录；
- B：结果表与判定；
- C2：容器信息与核对结果；
- C3：结论表；
- 未完成项与异常。
```
