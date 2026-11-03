# H3 正式入口接入操作记录（2026-09-18）

用户明确要求“请直接接入正式接口”，并已说明已有项目不再继续生成视频。本次只把已验证的三台新机器接到原 8000 入口；新视频、配音、生图和 LLM 提交额度均为零。本文记录持久配置与恢复操作，不能代替服务启动后的验收报告。

## 已准备的配置

- 原 `.env` 的所有既有键值保留，包括 Provider、数据库、对象存储、Temporal、旧节点 ID / 地址 / 实例身份 / Token。旧节点不改指新机器。
- 旧三节点列入 `TUBEFLOW_H3_RETIRED_NODE_IDS`。历史请求和 QC_FAILED 输出继续保留；退休节点不进入新任务调度，不能仅因历史远端不可达就伪造归档成功或删除记录。 启动绑定前会拒绝仍占槽、有非终态尝试或未完成归档清理的退休节点；不满足排空条件时失败退出。
- 新池节点为 `h3-new-1`、`h3-new-2`、`h3-new-3`，分别访问 Docker 内部地址 `http://h3-engine-new-1:8000`、`http://h3-engine-new-2:8000`、`http://h3-engine-new-3:8000`；对应实例 `8kdu1z040qlsjb9v`、`ob6kefgwgyqkpy33`、`2cop10ldfhkcmhwx`。
- 每台容量 2，总容量 6，与 KineStack 共用 Engine 限制。三个独立 Token 只通过服务端环境传入，不写入本文或公开报告。
- 新项目首次选择的默认路由为 candidate；显式项目 allowlist 为空。已保存的 legacy 项目工作流快照不改写，不从 8100 复制业务项目、数据库、媒体或会话。
- 故事板工作流固定 `h3.tubeflow.storyboard-to-video` / `1.0.0`，manifest `65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9`。原生输出、真实配音门禁与 TTS 混音规则不变。
- `web` / `worker` 使用 `docker-compose.yml` 中固定的不可变镜像引用并设置 `pull_policy: never`，移除这两个服务的源码 `build`。前端、Provider 与其余应用功能沿用原正式镜像；不得整包构建当前有未提交修改的源码替换它们。 当前分别固定 Web `sha256:0c35113ab719d400451dbce81bf54a9f1a60395033fe77fadd4eb819fe690d04`、Worker `sha256:7974d6013a3da9d52df53e41fa4fc1277d053ca90266244cf31b1af104090714`。
- 基础设施、网络、卷、只读公共音色包挂载、命令和依赖均保留。原实际发布为 `0.0.0.0:8000`，局域网仍通过 `http://192.168.110.4:8000` 访问；此处未收紧或扩大发布端口。

## 恢复操作

只有负责主任务的人确认完整 PostgreSQL 备份、历史摘要、业务/Temporal 排空、旧项目不续跑和最终镜像验证均通过后，才能执行以下命令。当前配置准备脚本不启动任何服务。

在本仓库根目录运行：

```powershell
docker compose --project-name tubeflowai-test --env-file .env -f docker-compose.yml up -d --no-deps --no-build --pull never web
```

确认 Web 健康、正式数据源和默认路由正确后，再单独恢复 Worker：

```powershell
docker compose --project-name tubeflowai-test --env-file .env -f docker-compose.yml up -d --no-deps --no-build --pull never worker
```

`migrate` 保留原有定义，本轮不执行它；当前数据库已是 0029。不要执行无服务限定的 `up --build`，否则仍可能从现有源码构建并运行迁移服务。上述 `--no-deps` 命令不会启动 migrate、minio-init 或重建 PostgreSQL / MinIO / Temporal。

恢复后的只读验收应核对：本机及局域网健康与匿名鉴权、已登录账号的原历史读取、三新节点可用性和故事板版本、历史业务/输出摘要、队列与执行计数；不得以新增视频作为本次接入的隐含测试。

## 回退与证据

若检查失败，先停止本次 Web / Worker，不自动切回已退休旧 GPU，也不恢复或重放历史生成任务。原 `.env`、Compose、镜像引用和完整数据库备份保存在本轮受保护的 private 目录；回退应由负责主任务的人按实际故障与数据状态执行，禁止覆盖媒体或清空基础设施。

配置差异与无密钥检查见 [正式配置核验](../../KineStack/test-results/h3-production-cutover-20260918/tubeflow8000-production-configuration.json)。完整启动、运行与历史保持证据在同一轮 `h3-production-cutover-20260918` 目录；配置核验通过不代表已启动、已生成或已完成人工质量验收。


## 本轮实际验收结果

原 8000 Web 与 Worker 已恢复并健康，三台新节点 READY、旧三节点 disabled/OFFLINE。64 项相关单元回归、3 项隔离 PostgreSQL 迁移/并发测试、修改文件 Ruff 与四源码 mypy 全部通过。原镜像仅四源码变化，原前端、依赖层和有效环境值保留。

已使用既有账号在本机及局域网地址真实登录并读取项目、第 08 步视频与旧媒体：页面/API 200、媒体 HEAD 200、Range 206、注销后 401。真实数据库事务内的新项目路由选择 candidate 故事板 1.0.0 和三新节点，随后完整回滚，没有保留测试项目或创建生成意图。13 张历史表全行哈希一致，6 个旧项目路由及 14 条历史 QC_FAILED 均保留；Temporal、可派发/恢复意图、三 Engine 执行与远端队列均为零。没有新增生成或重测音画质量。

完整结果见 [正式切流最终验收](../../KineStack/test-results/h3-production-cutover-20260918/tubeflow8000-production-cutover-final.json)。本仓库此前已有大量未提交修改，未整体暂存或提交这些修改；本轮精确补丁、原文件备份及固定镜像已保存，主线 KineStack 阶段快照记录该限制。
