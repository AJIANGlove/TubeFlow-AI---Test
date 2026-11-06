# TubeFlow AI 部署与运维台账（开发机 / 部署机分离）

> 2026-09-23 建立。适用范围：境内局域网测试阶段。代码更新在开发机进行，实际运行部署在局域网另一台 Linux 物理机。新会话接手运维/部署任务前必读本文。

## 1. 拓扑总览

| 角色 | 机器 | 说明 |
|---|---|---|
| 开发机（本机） | Windows，`D:\AAA-project\TubeFlow AI - Test` | 代码修改、测试、镜像构建、运维操作的发起端（通过 SSH 控制部署机） |
| 部署机 | `edy-ThinkCentre-M760e-N000`，**192.168.110.200**，Ubuntu 24.04 LTS 桌面版，i7-14700（28 线程）/ 32G 内存 + 8G swap / 954G NVMe | 实际运行 TubeFlow 栈与 KineStack 栈（两栈均已于 2026-09-23 迁入；旧 Windows 开发机上的对应栈已停止封存） |
| 外部依赖 | 云端 CosyVoice（TTS）、DeepSeek/grsai（LLM）、3 台远程 H3 GPU 机器 | 地址与密钥在部署机 `.env` 内 |

访问入口（局域网）：TubeFlow `http://192.168.110.200:8000`；KineStack `http://192.168.110.200:3000`。

术语（2026-11-06 用户确认）：
- **部署机**：只指 192.168.110.200。
- **测试机**：实施方的机器，即上表的「开发机（本机）」。
- 另有 **Claude 云端工作区**：Claude 写代码、跑单元测试的地方，既不是测试机，也不是部署机。

文档、提示词和汇报中不再用「本机」「本地」指代机器。

部署机上的 ffmpeg 在 TubeFlow 镜像内：`backend/Dockerfile` 用 apt 安装，构建时执行 `ffmpeg -version` 校验，合成用的就是它。宿主系统的 PATH 里不一定有 ffmpeg。所以「部署机有没有 ffmpeg」要区分是容器内还是宿主系统。

## 2. 连接方式（开发机 → 部署机）

- SSH：`edy@192.168.110.200`（主机名 `edy-ThinkCentre-M760e-N000` 也可解析），密码 `123456`（用户确认内网环境明文记录无影响；公网暴露前必须改掉并换密钥登录）。
- 开发机 Git Bash 无 sshpass，用 askpass 实现非交互（OpenSSH 10.3 支持）：

```bash
mkdir -p tmp/ssh-edy
printf '#!/bin/sh\necho "123456"\n' > tmp/ssh-edy/askpass.sh && chmod +x tmp/ssh-edy/askpass.sh
export SSH_ASKPASS="$PWD/tmp/ssh-edy/askpass.sh" SSH_ASKPASS_REQUIRE=force
SSHOPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=tmp/ssh-edy/known_hosts"
ssh $SSHOPTS edy@192.168.110.200 'hostname'      # 执行命令
scp $SSHOPTS localfile edy@192.168.110.200:/path # 传文件
```

- sudo：远程命令里用 `echo 123456 | sudo -S <cmd>`。注意 `<cmd>` 若需要 stdin，先 `echo 123456 | sudo -S true` 缓存凭据，再单独执行需要 stdin 的命令（否则 sudo 会吞掉 stdin 内容导致认证失败）。
- 用户 edy 已加入 docker 组（新 SSH 会话生效）；若 docker 命令报权限错，用 `sudo docker ...` 兜底。

## 3. 部署机上的关键路径与配置

- 项目目录：`/home/edy/tubeflowai-test`（目录名即 compose 项目名，**不要改名**，否则卷名/容器名全变）
- `.env`：`/home/edy/tubeflowai-test/.env`（权限 600，含全部密钥；修改前先备份）
- 迁移暂存：`/home/edy/tubeflow-migrate/`（tar 包，验证完毕可清理）
- Docker 卷：`tubeflowai-test_postgres_data`（全部权威数据 + Temporal 历史）、`tubeflowai-test_minio_data`（全部媒体资产）
- Docker 镜像加速：`/etc/docker/daemon.json` 已配 `docker.m.daocloud.io`、`docker.1ms.run`
- 防火墙：ufw 已启用，默认拒入站，仅放行 `192.168.110.0/24` 的 22/3000/8000
- 网络：有线 enp2s0 静态 IP（NetworkManager 连接「有线连接 1」：`192.168.110.200/24`，网关 `192.168.110.1`，DNS `223.5.5.5/119.29.29.29`）
- 已禁用睡眠/挂起/合盖休眠；NTP 已同步（Asia/Shanghai）
- Docker：`Docker Engine 29.8.1` + `Compose v5.5.1`；网络 `kinestack_control`（TubeFlow web 与 KineStack 主栈/引擎共用）与 `kinestack-candidate-test-control`（引擎组附加）

## 4. 常用运维命令（在开发机 Git Bash 里执行，先按 §2 设好 SSH 变量）

```bash
# 容器状态
ssh $SSHOPTS edy@192.168.110.200 'sudo docker compose -p tubeflowai-test ps'   # 需先 cd 到项目目录或用 -p；推荐：
ssh $SSHOPTS edy@192.168.110.200 'cd /home/edy/tubeflowai-test && sudo docker compose ps'

# 日志（worker 排障最常用）
ssh $SSHOPTS edy@192.168.110.200 'sudo docker logs tubeflowai-test-worker-1 --since 15m 2>&1 | tail -80'

# 数据库只读查询（推荐用 SQL 文件方式，避免多层引号）
#   1) 本地写好 xxx.sql  2) scp 到 /tmp  3) 执行：
ssh $SSHOPTS edy@192.168.110.200 'echo 123456 | sudo -S true; sudo docker exec -i tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow -At < /tmp/xxx.sql'

# 重启 / 停止 / 启动栈
ssh $SSHOPTS edy@192.168.110.200 'cd /home/edy/tubeflowai-test && sudo docker compose restart'   # up -d / stop 同理

# 卷备份（示例：postgres）
ssh $SSHOPTS edy@192.168.110.200 'sudo docker run --rm -v tubeflowai-test_postgres_data:/data:ro -v /home/edy/tubeflow-migrate:/backup postgres:17-alpine tar czf /backup/pg-$(date +%Y%m%d).tar.gz -C /data .'
```

## 5. 代码更新 → 部署流程（标准动作）

前提：开发机改代码并跑通项目测试门禁（backend pytest / frontend 测试，见 AGENTS.md）。

1. **前端**：前端静态资源打包在镜像内（Dockerfile 将 `index.html`、`app.js`、`styles.css`、`frontend/` COPY 进 `/app/frontend`，web 容器对宿主项目目录**没有**前端挂载，2026-10-13 实测核实）。纯前端改动也必须走镜像更新流程（overlay 增量镜像 → save/load → 改钉 → `up -d web`）；`node scripts/build.mjs` 仅生成本地 `dist/` 构建产物，与部署机实际服务内容无关。
2. **后端**：开发机构建镜像（Docker Desktop）：`docker build -f backend/Dockerfile -t tubeflow-test:<change-tag> .`
3. 导出并传输：`docker save <镜像ID> | gzip > tmp/migrate-out/img.tar.gz` → `scp` 到 `/home/edy/tubeflow-migrate/` → 部署机 `gunzip -c img.tar.gz | sudo docker load`
4. **更新 compose 镜像钉**：`docker-compose.yml` 中 web / worker / migrate 三处 `image:` 钉为同一镜像摘要（2026-10-14 起为 `sha256:7edaa916c6f8561f1257b9beb767e95669a0fcdf50578efe6c9174603efc6da9`＝ voice-panel-p0-20261014；历史钉见 §12 与 §14，旧生产镜像 `51bd6e16`/`3c6d0340`/`6476ff`/`e312aa` 保留在部署机作回滚）；compose 的 web/worker 保留 `build:` 段（合同测试要求可从源码构建），部署钉与 build 段并存。部署新代码时把这三处改成新镜像 ID（`docker inspect <tag> --format '{{.Id}}'` 查完整摘要），**开发机与部署机的 compose 文件必须保持一致**；migrate 与 web/worker 共用同一镜像，无需单独传输。注意：`docker compose up -d` 不会因 `build:` 段自动重建已有镜像；如需从源码重建 migrate 镜像，先 `sudo docker compose build migrate` 再 `up -d`（2026-10-09 实测踩坑：未 rebuild 导致迁移未执行）
5. 部署机：`cd /home/edy/tubeflowai-test && sudo docker compose up -d`（`migrate` 服务自动执行 alembic 迁移；web 健康检查约 20–45 秒）
6. 验证：`compose ps` 全 healthy → `curl http://192.168.110.200:8000/health/live` 返回 200 → 抽查关键页面/计数
7. 回滚：compose 钉改回旧镜像 ID（旧镜像不要急着删），`compose up -d` 即可

## 6. 2026-09-23 迁移记录（事实存档）

- 源：开发机 Docker Desktop（`tubeflowai-test` 项目）；目标：部署机原生 Docker。冷拷贝（源栈停止）tar 包传输：
  - `postgres_data` 1.3G → 408M gz；`minio_data` 5.0G → 4.8G gz；镜像 7 个 + 生产镜像 `8fb499`（compose 钉住项，**漏传会导致 compose 报 No such image**，务必包含）；仓库 539M gz（排除 node_modules/.venv/tmp/缓存）；`.env` scp 直传（用户授权）；音色库 `runtime/public-voice-catalog/public_voice_library.zip`（299M）随仓库包
- 装载验证：两卷解包后 `du -sh` 与源端一致（1.3G/5.0G）；数据库计数一致（维京人项目 `aec5d588-b070-4a52-b006-9f0415ed6d5f`：配音 290/290、故事板 84/84、视频 84/84、管线 PAUSED/composition_failed）；5 容器约 20–48 秒全 healthy
- 旧机（开发机 Docker Desktop）栈迁移后 `docker compose stop` 封存一周作回退（卷未动）；**严禁两机同时运行同一栈**（编排器巡检会重复驱动项目，造成外部付费调用重复提交）
- 部署机曾踩坑：新机 apt 默认源 DNS 故障 → 已换阿里云镜像（`/etc/apt/sources.list.d/ubuntu.sources`）；Docker 从阿里云 docker-ce 源安装

## 7. KineStack 迁入记录（2026-09-23 完成）

- 部署位置：仓库 `/home/edy/KineStack`（compose 项目名 `kinestack`），数据 `/home/edy/KineStackData`（19G 整体迁入，含 postgres 库目录、outputs、uploads、config）
- 启动命令（两个项目，均需 --env-file 指定，**没有默认 .env**）：
  - 引擎组（先启）：`cd /home/edy/KineStackData/config && sudo docker compose -f h3-clones-compose.yaml --env-file h3-clones.env up -d`
  - 主栈：`cd /home/edy/KineStack && sudo docker compose -f compose.yaml -f /home/edy/KineStackData/config/production-compose.yaml --env-file /home/edy/KineStackData/config/runtime.env up -d`
- 机器相关改写（**仅部署机副本，开发机仓库保持原样；以后重同步代码时需保留这两处**）：
  - `runtime.env` / `h3-clones.env` 的 `KINESTACK_DATA_DIR=/home/edy/KineStackData`
  - `compose.yaml` 第 148 行 `APPLICATION_BASE_URL` 的 IP 为 `192.168.110.200`
- 权限修正：Docker Desktop bind mount 的文件迁入后属主为 edy（uid 1000），而引擎容器以 uid 10001(kinestack) 运行——已对 `KineStackData/{logs,temporary,outputs,uploads}` 执行 `chmod -R a+rwX`（**database 目录不要动**，属 uid 999=postgres）
- 镜像：server/worker `5858544a`、web `e4213a7f`、fake-engine `9bf93a5f`、h3 引擎 `88c0df22`、postgres `9b18b783`，全部 sha256 钉在 overlay 里（`pull_policy: never`），换新代码需按 §5 流程重新导出/装载/改钉
- 网络：主栈自建/复用 `kinestack_control`；引擎组额外加入外部网络 `kinestack-candidate-test-control`（已在新机创建）
- 验证结果：8 容器全 healthy，web:3000 返回 200，TubeFlow web 容器到 3 个引擎的 8000/health/live 全部 200
- 旧机上的 KineStack 生产容器已 `docker stop` 封存（数据未动）；测试残留件（`kinestack-h3-engine-candidate-1`、`kinestack-candidate-test-*`、`tubeflow-local-*`、退役的 h3-engine-1/2/3）**未迁移**仍在旧机
- **已知未决**：迁移时 3 台远程 H3 GPU 机器的 ComfyUI `/system_stats` 均返回 HTTP 503（网络可达、服务降级），需用户检查 H3 机器上的 ComfyUI 服务；恢复前 TubeFlow 新项目的视频生成不可用（维京人项目合成不受影响）

## 8. 红线与注意事项

- 任何写操作（resume/重试/删除）都要按 `docs/planning/AGENT_AUTHORIZATION.md` 走授权；监控/排查只读
- `.env` 不进 git、不进聊天明文（本文例外仅记录 SSH 口令且限内网）；改 `.env` 前备份
- 项目资产无 TTL：监控磁盘水位（`df -h /`），按用户决策执行「导出 ZIP → 删除项目」回收
- 部署机是桌面版系统：系统更新/大版本升级前先快照或确认可回退；不要顺手"优化"无关配置
- 本文与 `docs/AUTO_PIPELINE_HANDOVER_20260917.md` 互补：那份记录 AUTO 模式实现与验收，本文记录部署拓扑与运维入口；两边信息漂移时以实际机器状态为准并更新文档

## 9. 事故记录：2026-09-23 断电与网络恢复

- 事件：部署机异常断电。恢复后出现「ICMP 通、全部 TCP 不通」→ 后恶化为「ARP 全失效」。
- 根因（实测确认）：`192.168.110.37` 与局域网内其他设备/路由器状态冲突（静态 IP 未在路由器登记，位于 DHCP 池内）。**修复 = 静态 IP 改为 `192.168.110.200`**（DHCP 池外）。教训：以后加新机器，静态 IP 一律取 200+ 段，或在路由器做 MAC 绑定。
- 次生坑 1：恢复供电重启后 Docker 桥接网络 NAT 状态损坏（docker-proxy 在监听但本机 curl 都 000）→ `sudo systemctl restart docker` 重建。
- 次生坑 2：**TubeFlow 栈没有 `restart: unless-stopped`，断电/重启后必须手工 `cd /home/edy/tubeflowai-test && sudo docker compose up -d`**；KineStack 各服务有重启策略会自动回来（h3-clones 组也有）。建议后续给 TubeFlow compose 补重启策略（待用户决定）。
- 恢复后验证：两栈全 healthy，维京人项目数据完好（COMPLETED、成片版本在、计数一致）。
- 待办建议：为两台电脑 + 路由器配小型 UPS，防再次断电事故。

## 10. 运维手册：H3 僵尸任务（远端 GPU 强关导致）

症状：KineStack 节点卡片容量占满（2/2），任务长期停在「RUNNING · 远端已接单」，无实际执行，后续任务全部 QUEUED。

根因：远端 ComfyUI 被强制关机/重启后丢失在跑 prompt；引擎轮询 `/history/{prompt_id}` 收到 `200 + {}`（ComfyUI 对未知 prompt 不返回 404），误判为"仍在运行"，无限等待（已确认的引擎探活盲区，根治需改 KineStack 引擎代码：空 history + 不在 queue + 超输出超时 → 判远端丢失并报可重试错误）。

诊断（只读）：
1. 服务端卡任务：`psql -U kinestack -d kinestack -c "SELECT id,status,progress_phase,started_at FROM generation_jobs WHERE status='RUNNING';"`（容器 kinestack-postgres-1）
2. 引擎侧对应记录：`/home/edy/KineStackData/temporary/community-h3-new-{1,2,3}/community-h3.sqlite3` 的 `executions` 表（attempt_id → prompt_id）
3. 远端 ComfyUI：`/queue` 空 + `/history/{prompt_id}` 返回 `{}` → 实锤远端丢失

处置（需用户授权）：对每个僵尸 attempt，在对应引擎容器内执行：
`POST http://127.0.0.1:8000/executions/{attempt_id}/cancel`（带 `Authorization: Bearer $ENGINE_TOKEN`，token 从容器 env 取，不外显）
服务端约 30 秒内同步为 CANCELLED，容量自动释放，队列自动顶上。

记录：2026-10-08 处置 4 个僵尸任务（节点2：86454cde/410311e5；节点3：9bb8cedb/39498792），验证队列恢复。

## 11. CosyVoice 配音服务商（2026-10-08 切换）

- 老机器已下架；新机器为 Seetacloud 按时计费实例（AutoDL 容器，vGPU-32G，使用中不会被回收）
- 服务接口（公网，TLS 有效）：`https://u997192-9b33-8f193e7e.westb.seetacloud.com:8443`，已写入部署机 `/home/edy/tubeflowai-test/.env` 的 `TUBEFLOW_COSYVOICE_URL`（修改前备份为 `.env.bak-20261008-cosyvoice`）
- SSH 管理入口：`ssh -p 34937 root@connect.westb.seetacloud.com`（密码向用户索取，本文不存）
- 服务布局：`/root/voice/`（FastAPI `inference:app`，路由 `/`、`/generate`、`/batch_generate`），gunicorn 绑 `0.0.0.0:6006`，supervisord 配置 `/root/sv/supervisord.conf`（含 vram_watchdog）
- **服务无 Token 鉴权**（代码内无校验），`TUBEFLOW_COSYVOICE_TOKEN` 保持原值即可（多余头会被忽略）
- **实例重启后服务不自启**：需手工执行 `/root/miniconda3/bin/supervisord -c /root/sv/supervisord.conf`（autostart 会拉起 inference，模型加载约 30 秒）。建议后续把该启动挂进 AutoDL 的 /init/supervisor 体系或 systemd
- 验证路径：本机 `curl http://127.0.0.1:6006/` 应返回 `{"message":"Inference Service","status":"running"}`；部署机 web 容器已验证可达（200）
- 注意：该服务同时被其他业务使用（日志中见非 TubeFlow 的合成记录），排障时勿误判为异常流量

## 12. 2026-10-12 部署记录与默认部署目标确认

- 本次部署内容：AUTO 导演多样性闸口治理（USER_DECISIONS §20 / 验收 §22）。镜像 `tubeflow-test:auto-director-gate-20261012`（ID `6476ff46c60a`），compose 钉两机一致；部署前已确认无 RUNNING 管线与在途外部任务（仅 3 个 PAUSED 项目与 4 条 §13 历史未知记录）。
- 执行结果：web/worker 约 1 分钟恢复 healthy，`/health/live` 200；migrate 正常退出（本轮无 schema 迁移）；新代码已抽查在容器内生效。前端 `dist/`、`frontend/`、`app.js`、`index.html`、`styles.css` 已同步（覆盖前旧文件备份于 `/home/edy/tubeflow-migrate/frontend-backup-20261012-*.tar.gz`）。
- 用户确认（2026-10-12）：**部署机固定为 `192.168.110.200`，以后「部署」默认按本文 §5 流程直接发往该机，不再逐项询问目标**；每次部署的代码内容仍以测试门禁通过为前提，写操作授权边界不变（§8 红线）。

- 2026-10-12 第二轮部署：镜像 `3c6d0340`（hash-gate-fix-20261012）；预演（102/102 组新基线哈希一致）通过后一次构建（约 2 分钟，层缓存命中）一次到位；web/worker 恢复 healthy，`/health/live` 200，`/app.js` 已带 `Cache-Control: no-cache`；H3 当前 1/3 节点 READY（h3-new-3），总体可用。compose 两机一致（含补回的 web/worker `build:` 段）。
- 遗留观察：本项目 Dockerfile 的 `pip install --no-cache-dir` 使任何代码变更都击穿依赖层缓存（本次构建约 55 分钟）；提速改造（pip 缓存挂载 / 依赖层拆分 / 部署机直建 / 局域网 registry）已向用户提出，待决策。

## 13. mihomo 代理容器（ElevenLabs 线路，2026-10-13 接入）

- 用途：ElevenLabs 是境外服务，付费配音（克隆 + TTS）流量经 mihomo 代理容器出访；**仅 EL 流量走代理**（由 `TUBEFLOW_ELEVENLABS_PROXY=http://mihomo:7890` 注入 web/worker 的 EL 客户端），CosyVoice/DeepSeek/grsai/H3 等其余线路不受影响。
- 镜像：`metacubex/mihomo` 已按 manifest digest 钉死（compose 内 `mihomo` 服务，2026-10-13 的 `latest`）。首次部署或换钉时需在部署机 `sudo docker compose pull mihomo`（该机已配镜像加速）；容器只接 default 网络、不暴露宿主机端口、不加入 `kinestack_control`。
- 订阅链接：机场订阅 URL（clash/mihomo YAML）属凭证，只写部署机 `.env` 的 `MIHOMO_SUBSCRIPTION_URL`（模板见 `.env.example`），不入库不进 git；容器启动时由 `docker/mihomo/entrypoint.sh` 拉取（1 次 + 3 次重试，失败容器退出）。**订阅更新 = 改 `.env` 后 `sudo docker compose up -d mihomo`**（重建容器重新拉取）。
- 节点选择：启动后经 external-controller（127.0.0.1:9090，仅容器内）把全部 Selector 组锁定为组内第一个匹配 `MIHOMO_NODE_FILTER`（默认 `新加坡|Singapore|🇸🇬|SG`）的节点；无匹配则打日志保持订阅默认节点。换节点策略 = 改 `.env` 的 `MIHOMO_NODE_FILTER` 后同上重建容器。
- 故障表现：mihomo 容器挂了或节点失联时，**仅 EL 配音暂停**（句级按既有未知态/配额语义处理），其余业务线路不受影响；web/worker 对 mihomo 是 `depends_on: service_healthy`，mihomo 未就绪时 web/worker 不会启动。诊断：`sudo docker logs tubeflowai-test-mihomo-1`（`[mihomo-init]` 前缀为本入口脚本日志）。
- 并发叠加提醒：`TUBEFLOW_ELEVENLABS_PER_KEY_CONCURRENCY` 按进程生效，web 与 worker 各自实例化 EL 客户端（默认各 4/key）；多 worker 叠加部署时须相应调低该值，避免触碰 EL 账号级并发上限（Creator 档为 5）。
- restart 策略：mihomo 与栈内其他服务一致，**均无 restart 策略**（§9 次生坑 2 的已知待办不变）——部署机重启后须手工 `sudo docker compose up -d`。
- **2026-10-13 21:20–21:40 修复：号池页"添加"点击无反应（根因 + 两道加固）**。现象=EL 号池页点"添加"无反应。根因=订阅配置 `allow-lan: false` 使 mixed 代理端口只绑容器内 `127.0.0.1:7890`，web/worker 连 `mihomo:7890` 被拒 → EL key 校验出访失败 → 后端 422；前端又把错误提示在一毫秒内清掉，视觉上"无反应"。修复：
  - ① `entrypoint.sh` 拉取订阅后**强制 `allow-lan: true`**（校验失败即退出；本容器不发布宿主机端口，仅同一 docker 网络内的栈内容器可见，安全边界不变）。
  - ② `entrypoint.sh` 新增地理数据**本地补齐**：mihomo 只在 `-d` 工作目录（`/config`）查找 geo 文件，缺失时从 GitHub 下载；境内直连不稳定，**超时会 fatal 退出导致容器起不来**（本次重建实测复现）。现在启动前把镜像内置的 `/root/.config/mihomo/{geoip.dat,geoip.metadb,geosite.dat}` 补齐到 `/config`，启动不再依赖外网（初始化从 ~136s 降至 0.2s，离线可启动）。geo 数据为镜像构建时点版本；如需更新地理库需另行评估（镜像更新时处理）。
  - ③ 前端 `el-pool.js` 修复错误显示时序（refresh 不再清掉操作错误）、提交按钮加载态/防重复提交、补充 `provider_not_reached`/`provider_auth_failed` 中文提示（镜像 `e690c507`）。
  - 验证（实测）：`netstat` 见 `[::]:7890`；web 容器 TCP 连通 `mihomo:7890`；经代理访问 `api.elevenlabs.io` 返回真实 HTTP 响应（探测用无效占位 key 得 401，未产生计费、未使用有效凭证）；全栈 healthy；实际服务的 `frontend/el-pool.js` md5=新文件。重建 mihomo 后建议复查：`docker exec tubeflowai-test-mihomo-1 netstat -tln | grep 7890` 应非 127.0.0.1 单绑。
- **2026-10-13 22:xx 号池装配修复（同一晚第二轮，镜像 `89ad8e69`）**：上轮修复后「添加」仍失败，根因=**号池管理 API 的配额端口在 `api/app.py` 装配为占位实现**（业务主流程早用真实适配器，管理页路径漏切）→ 请求零外发必 422。修复=池路由复用 runtime 暴露的 `elevenlabs_quota`（共享同一适配器/信号量；替身兜底亦为真实适配器、无来源快速失败）、删除占位类、新增装配回归测试；上线验证：无效 key 探针 422 `provider_auth_failed`（2.57s 真实外呼经 mihomo 到达 EL）。细节见 `runtime/operations/elevenlabs_deploy_20261013.md`"追加 2"。
- **开发机测试环境注记（2026-10-13）**：`D:\AAA-project\TubeFlow AI - Test` 目录存在文件系统级异常——该目录内**任何 venv 都无法启动**（python 报 `Cannot read '...\pyvenv.cfg'`，移出该目录即恢复；旧仓库内 `.venv` 因此不可用），且 `%TEMP%\pytest-of-15202` 被拒访问。后端测试改用外部 venv：`C:\Users\15202\.workbuddy\binaries\python\envs\tubeflow-test`，运行：`...\binaries\python\envs\tubeflow-test\Scripts\python.exe -m pytest tests/unit -q -p no:cacheprovider --basetemp=<该环境内每次新建的目录>`。

## 14. 2026-10-14 音色面板 P0 前端上线（voice-panel-p0-20261014）

- 内容：「配音源与克隆音色」面板 UX 方案A + P0 六项落地（两级 IA；去除源切换拦截回滚、改为"先进入、后就绪引导"；「设为频道默认」即生效并跨源自动切换；删除二次确认与保护原因可见；克隆结果卡常驻/失败重试/新音色高亮；公共音色库并入 CosyVoice 源内视图）。纯前端改动，无后端/数据/迁移变更；依据 `docs/VOICE_SOURCE_CLONE_UX_PROPOSAL_20261014.md`、`docs/VOICE_PANEL_P0_IMPLEMENTATION_20261014.md`。
- 镜像 `tubeflow-test:voice-panel-p0-20261014`，完整摘要 `sha256:7edaa916c6f8561f1257b9beb767e95669a0fcdf50578efe6c9174603efc6da9`（开发机构建约 2 分钟）；tar 备份 `/home/edy/tubeflow-migrate/tubeflow-voice-panel-p0-20261014.tar.gz`（265M，md5 `8705ab5dd7058a7168181730d89960f7`，两机一致）。
- 部署前置检查：无 RUNNING AUTO 管道（6 COMPLETED + 1 PAUSED）、Temporal 无运行中工作流、全部容器 healthy、磁盘可用 797G；compose 改钉后两机 md5 一致（`9a146a616205607bdb97f4b5aa78b4f3`），部署机旧 compose 备份为 `docker-compose.yml.bak-20261014-voice-panel`。
- 执行结果：`docker load` 成功且摘要一致；migrate 重建后退出码 0（无新迁移）；web/worker 以新镜像重建，约 53 秒后全栈 healthy；`/health/live` 与首页均 200；容器内文件与 HTTP 实际服务的 `app.js` / `frontend/el-voices.js` 均含新代码标记（`voice-source-card` / `channelDefaultPatch`）。
- 回滚：compose 三处 `image:` 钉改回 `sha256:51bd6e16bc7e70e7f1f0a64b22db55e1d23aab6803685c75d183d753308e91e1`（audio-qc-fix-20261014，仍在部署机）后 `docker compose up -d`；或恢复 `docker-compose.yml.bak-20261014-voice-panel`。
- 待办：用户浏览器页面验收（可与 UAT 残留清理一并核对：项目 `7dc6c461`、克隆音色 `EL-UAT-1`/`EL-UAT-BAD`、频道默认设置）。

## 15. 2026-10-14 三问题修复上线（stuckpoints-fix-20261014：故事板违禁词规避 / 导演恢复死锁 / 短句配音）

- 内容：① 故事板提示词提交前安全替换（`apply_prompt_safety`，规则 `storyboard-prompt-safety-v1`，命中留档 `input_json.prompt_safety`）；② 导演恢复创建前回收终态运行的过期孤儿单元（`reclaim_expired_units`，修韩国项目 `a5367080` 点恢复必 `director_run_unresolved` 的永久死锁）；③ 短音频归一化增益窗口 ±6→±12 dB / 最终 9→15 dB、配方 v3（修 CosyVoice 超短句"发音太短"类必然失败）。依据与证据：`runtime/operations/stuckpoints_fix_20261014.md`。
- 镜像 `tubeflow-test:stuckpoints-fix-20261014`，摘要 `sha256:8ced423d8dc07c861cf64bd90cf859a167c401efb0698ac35a90f0e34f756c3b`（开发机构建 10m51s）；tar 备份 `/home/edy/tubeflow-migrate/tubeflow-stuckpoints-fix-20261014.tar.gz`（md5 `124d19c9add87ab3b580ec717938eec8`，两机一致）。
- 门禁：后端单测 1708 passed/exit 0；前端 182 passed + 构建通过；ruff/mypy 本次改动文件无新增问题。
- 部署前置检查：无 RUNNING 管道、Temporal 无运行中业务工作流、全栈 healthy；compose 三处改钉后两机 md5 一致（`1febc52b477a9222c30b028e006d113b`）；开发机备份 `docker-compose.yml.bak-stuckpoints-20261014`，部署机备份 `docker-compose.yml.bak-20261014-stuckpoints`。
- 注意（背景）：本镜像同时**修正了 11:01 部署的 `voice-panel-p0`（`7edaa916`）镜像携带的"半接线快照"**——该快照含修复②的服务层调用但缺适配器实现，点恢复会抛 AttributeError 并静默卡住；本镜像上线后该隐患消除。`7edaa916` 的前端内容（音色面板 P0）完整包含于本镜像。
- 执行结果：migrate 退出 0（无新迁移）；web/worker 重建约 1 分钟全栈 healthy；`/health/live` 200；上线后在线验证：真实短句失败产物归一化 OK（gain 9.75/v3）、安全替换在线生效、②三方接线齐备。
- 回滚：compose 三处钉改回 `sha256:7edaa916…`（voice-panel-p0）或 `sha256:51bd6e16…`（audio-qc-fix）→ `docker compose up -d`。
- 待办：① 用户点击韩国项目「恢复自动执行」做真实路径验证（恢复后全链路将自动继续，注意费用窗口）；② 观察新故事板生成的 `input_json.prompt_safety` 与 `output_moderation` 拒绝率；③ 音频 v3 配方开始产生新 lineage（`short-f32le-24k-gain-limiter-v3`）后抽查 normalization manifest。

- **更正（11:50–11:58）**：首版 v1 上线后修正两处注释文档引用，重建 v2 并替换上线；**最终现役钉 = `sha256:34d08d48b88be51fdcce2fc1079fd55dfabb69322ffd3a7df5ff3c94fafd4221`**（v2，tar `tubeflow-stuckpoints-fix-20261014b.tar.gz`，md5 `6fc28a0b3a4117abbb53134cd33d9acd`）；两机 compose 最终 md5 `1db7a3048e93341e60ec6f6b11668011`；web/worker healthy、`/health/live` 200、三修复在线复验通过。回滚链：v2→v1（`8ced423d…`）→voice-panel-p0（`7edaa916…`）→audio-qc-fix（`51bd6e16…`）。

## 16. 2026-10-14 终态遗留暂存清理出口上线（leftover-cleanup-20261014）

- 内容：修复「CANCELLED/FAILED 终态尝试仍持有暂存资产时被 outbox 永久重驱动（每 ~5 分钟空转、10-13 起累计 365 次短生命周期恢复工作流）」的不收敛问题。`video_generation/orchestration.py::_advance_once` 新增兜底清理出口：终态 + `staged_ids` 非空 → 审计事件 `TERMINAL_LEFTOVER_CLEANUP` + 补写 `CLEANUP_PENDING`/`cleanup_terminal_stage` → 复用既有幂等 `_cleanup_staged`（`DELETED/NOT_FOUND` 均视为成功、可断点续删），清理后 `staged_ids` 清空、自然退出 redrive 排除集（谓词语义未动）；`provider_audit.SAFE_RECEIPT_FIELDS` 白名单 +`stage`/`staged_count`（审计证据可见性）。依据与证据：`runtime/operations/stuckpoints_fix_20261014.md`「附带修复」。
- 镜像 `tubeflow-test:leftover-cleanup-20261014`，摘要 `sha256:a87a129679895c6c968c3bf1944a156fc2d9588412d1734387308deeb6a56ae4`（开发机构建 27m51s，pip 依赖下载慢）；tar 备份 `/home/edy/tubeflow-migrate/tubeflow-leftover-cleanup-20261014.tar.gz`（md5 `7069c9452e7a7866390ffcfc81a9467a`，两机一致）。
- 门禁：相关模块 104 passed；全量后端单测 1711 passed / 5 skipped（ffmpeg 环境跳过）。
- 部署前置检查：无 RUNNING 管道（韩国项目此时 PAUSED）、无在途导演单元/配音；compose 三处改钉后两机 md5 一致（`08c5be9c8045e8694967b49ef759ac2c`）；开发机备份 `docker-compose.yml.bak-leftover-20261014`，部署机备份 `docker-compose.yml.bak-20261014-leftover`。
- 执行结果：migrate 退出 0（无新迁移）；web/worker 重建约 1 分钟 healthy、`/health/live` 200；镜像内代码探测（清理出口 + 白名单 + 既往修复齐全，无半接线）。
- 在线验证（均为新代码执行）：13:02 CST 两个孤儿 attempt 各执行一次清理出口——`TERMINAL_LEFTOVER_CLEANUP` + 2× `STAGED_DELETE_RECEIPT`（4 个删除全部 `DELETED`）、两行 `staged_ids` 清零；此后 `dispatched_at` 冻结于 13:02（此前每 ~5 分钟滚动）、13:02:40 后新恢复工作流 run = 0（此前 30 分钟 12 次）、全库 redrive 可驱动计数 = 0。
- 回滚：compose 三处钉改回 `sha256:34d08d48…`（stuckpoints-fix v2，两机仍在）→ `docker compose up -d`；或恢复两机 compose 备份文件。
- 同期观察（未处理）：韩国项目 `a5367080` 已于 12:14 CST 完成「恢复自动执行」真实路径验证——新导演 run `375c7ee9` 121/121 单元 SUCCEEDED（含 #117/#120/#121 重规划，问题 2 修复生效）；管道续跑进入 `storyboard_drafts` 阶段后因组 85 导演草稿校验失败（"a director plan must contain 1 to 5 shots and a positive revision"）于 12:16:39 再次暂停，出口为页面修正草稿保存即自动续跑。

## 17. 2026-10-14 组级镜头上限 6＋确定性收敛上线（cap6-converge-20261014）

- 内容（USER_DECISIONS §24）：组级镜头上限 5→6（域/API/前端/生图网格同版）；确定性重映射升 `controlled-semantic-lineage-remap-v4`，>6 镜在 final_revision 判定前经 `adjacent-merge-v1` 确定性相邻合并收敛（保前镜身份、后镜来源重定向=多父血缘、链接并集去重、序号重排）；`group_shot_limit_exceeded` 转防御分支。完整记录：`runtime/operations/cap6_converge_deploy_20261014.md`。
- 镜像 `tubeflow-test:cap6-converge-20261014`＝`sha256:551ebbf06d779f4696292d6a95972a4e5bb0df9adb00c489ad96cdc2deb90f63`（1.11GB，构建 22m48s）；tar `/home/edy/tubeflow-migrate/img-cap6.tar.gz`（md5 `185f8e1c9da4f7ff83a37e09c3ed70a1`，两机一致）；compose 两机 md5 `706eaf5c77798041715a56f0535d75e6`（部署机旧版备份 `docker-compose.yml.pre-cap6-20261014`＝`08c5be9c…`）。
- 门禁：后端全量 1726 passed / 5 skipped（+15 新用例：收敛纯函数 6 / SQL 往返 3 / 导演 6·7 镜 2 / 6 格提示词 4）；前端 182 passed + build；ruff 全包通过；mypy 改动文件零新增。部署前 D7 真实 6 格生图验证通过（2×3 全用、无留黑、角标 SHOT 1–6 齐全；证据图 `runtime/operations/cap6_six_panel_proof_20261014.png`）。
- 前置检查：无 RUNNING 管道（PAUSED 1 / COMPLETED 6）。
- 执行结果：load 摘要一致；web/worker 重建 healthy、`/health/live` 200；容器内探测 conv=adjacent-merge-v1 / limit=6 / remap=v4 / grid6=2×3 无留空 / grid7 拒绝；`app.js` 含 `MAX_DIRECTOR_SHOTS = 6`。
- 在线验证（新代码真实路径，17:24–17:37 CST）：用户「恢复自动执行」→ 组 85 六镜草稿自动播种＋应用（revision `693eac87`，血缘 candidate `999080ae` / materialization `0fa6481f`）；管线对全部缺失组批量提交故事板生成（133 组：128 组 SUCCEEDED；5 组 FAILED——provider 阶段 grsai 服务端错误 `get image url failed` / `generate image failed`，属服务商侧偶发）；**组 85 六格故事板 SUCCEEDED（记录 `5f96593f`；提示词 Panel 6 在位、无留黑句）**。17:36:22 因当前轮终态失败暂停 `storyboard_failed:error`（载荷按暂停时点列出组 50；批次内实际失败组 50/55/60/64/103，再次「恢复」会重新评估并刷新列表）。
- 回滚：compose 三处钉改回 `sha256:a87a129679…`。注意：组 85 六镜草稿已落库并应用，旧镜像读取 6 镜修订将 fail-closed——回滚不可行时按前向修复处理（§24 / S10）。

## 18. 故事板双渠道故障转移：构建验证完成，尚未部署（2026-10-15）

- 实现（USER_DECISIONS §25）：GRSAI 四模型 + Pix 三模型固定顺序故障转移（2026-10-15 用户决定不上线 `gpt-image-2-vip`）；生产每组 14 次/每模型 2 次；AUTO 与手动故事板共用；0036 链字段与原子限额；UNKNOWN 禁止替代提交；固定横屏与最终 QC。详见 `runtime/operations/storyboard_provider_failover_20261014.md`。
- 自动化门禁：后端全量 **1779 passed / 5 skipped**；前端 **182 passed** 与构建通过；Ruff 生产源码及相关测试通过；mypy 无本任务新增错误（仍有 7 个既有错误）。PostgreSQL 空库升级、0035→0036→0035 隔离往返、真实行锁并发共 3 passed。VIP 下线后的 14 次链路需重新全量验证，不能沿用此前计数作为最终证据。
- 七模型 v2 镜像已构建并探测：`sha256:0034f26cdb9f0d7de0c60b095311a3067e236797ecac50691550b18c97c547d7`（无 VIP，`max_attempts=14`，0036 文件存在）。八模型旧镜像 `fa6f897a…` 不再作为部署候选。
- 真实模型验收：当前启用七模型全部通过。GRSAI 四项各一次成功；Pix `tt-image-2.5`/`tt-image-2`/`banana-2.1` 各一次成功（68.8s/69.1s/63.2s，画幅、哈希、解码与 QC 通过）。`gpt-image-2-vip` 三次均失败且随后由用户决定下线。Pix Token 已从本地 `.env` 经加密 SSH 写入部署机私有配置，未回显或写入代码/日志。
- 部署结果（2026-10-15 07:44）：镜像包两机 md5 `3a69d9c61f6c8c829d8019ca16b906ef`；compose 两机 md5 `f0d020ca5252191071dabfe71de8b7d0`；0036 已生效；web/worker healthy、`/health/live` 200。备份 compose `.pre-storyboard-failover-20261015` 与数据库 `/home/edy/backups/tubeflow-pre-storyboard-failover-20261015.dump`。
- 韩国项目恢复：实时预检仍恰为组 50/55/60/64/103（FAILED、无当前成功、无在途/UNKNOWN），已通过正式 API 恢复完整 AUTO，`authorization_round=17`。gpt-image-2.5 与 gpt-image-2 均各两次因「模型正在修复中」失败，系统实际转入 nano-banana-2；组 50/55/60/64 第 5 次成功、组 103 第 6 次成功，133/133 current 故事板齐全。既有 128 组零替代提交。08:27 AUTO 在视频阶段暂停：组 70/93 均为 `h3_prompt_shot_blocks_invalid`（2 次修复后仍无效），当前成功视频 9/133，合成未开始。Worker 曾因数据库连接池耗尽退出，已恢复 running/healthy；AUTO 仍 PAUSED，未擅自增加提示词调用预算或修改 H3 编译合同。

## 19. 2026-10-17 SRT 预览卡便捷下载上线（srt-download-20261017）

- 需求与决策：第 09 步「SRT 预览」卡标题栏右侧新增「下载 SRT」按钮，点击直接下载当前预览字幕；按 USER_DECISIONS §28 修订 §8.8，ZIP 仍是唯一正式交付入口。前端复用预览已完成的受保护读取结果，点击下载不新增媒体请求，也未新增后端路由。
- 镜像：`tubeflow-test:srt-download-20261017` = `sha256:6be9249d32e4cd4c2079f7675f69e59dd775c9e52bf75539102b2fe900fce7fd`，由部署机以 `tubeflow-test:storyboard-unknown-retry-20261017`（`a16063f2…`，已在本机、尚未部署）为基线的 overlay 增量镜像构建，仅替换 `/app/frontend`（`index.html` / `app.js` / `styles.css` / `frontend/`）。
- 连带上线：因基线镜像即 `a16063f2…`，本次同时上线 §27 AUTO 故事板 UNKNOWN 有界自动重提的后端变更；`migrate` 退出码 0。
- 自动化门禁：`npm run check` 通过（前端 **202 passed**，新增 `frontend/composition-srt.test.js` 4 项；lint / typecheck / build 全绿）。
- compose：两机 md5 `49f839c2cfeb8cd111cc434a3bdd9159`，web/worker/migrate 三处同钉新摘要；备份 `docker-compose.yml.bak-srt-download-20261017`（部署机同名备份，原钉 `381a09a6…`＝cover-quality-20261015）。
- 验证（部署后实测）：web/worker 均 healthy、`/health/live` 200；`http://192.168.110.200:8000/app.js` md5 `0fb3c460517c04c872643400c1eb83be` 与本地一致，`downloadSrtPreview` 命中 2 处；容器内 `/app/frontend/app.js` 同标记命中 2 处；`styles.css` md5 `731f1feec3c8057c902adbaa8ac65414` 与本地一致；web 日志无启动异常。
- 回滚：三处钉改回 `sha256:381a09a67557741ce3f9c77c55521957e63a81082ddc8540adff548c5ea48c02` → `docker compose up -d`；旧镜像 `a16063f2…`、`381a09a6…` 均保留在部署机。
- 未验证：浏览器端真实点击下载（需登录态账号）留给用户 Ctrl+F5 后验收；按钮在字幕加载中/读取失败时保持禁用的行为由源码断言覆盖。

## 20. 2026-10-17 AUTO 故事板 UNKNOWN 恢复收口上线（storyboard-recovery-supervisor-20261017b）

- 内容：在 §27 有界自动重提基础上，补 Temporal `dict` 返回兼容、恢复工作流每 100 次迭代 `continue_as_new`、ACKED 后终止工作流的 AUTO 补登记；补登记仅处理功能上线后的恢复事件，不回溯其他项目。依据与完整记录：`runtime/operations/storyboard_unknown_bounded_retry_20261017.md`。
- 镜像：以 `tubeflow-test:cover-anchor-v6-20261017` 为底的 overlay 增量镜像 `tubeflow-test:storyboard-recovery-supervisor-20261017b`＝`sha256:2d26a4a88fb0508bf904ee8902e8f809d53f3b96515e83495964cd17c8b39822`；compose 两机 md5 均为 `59aa5387bff42f28593ebf388826b211`，部署机旧 compose 备份为 `docker-compose.yml.bak-recovery-supervisor-20261017`。
- 门禁：最终叠加源码核心回归 96 passed；相关 PostgreSQL 4 passed；后端全量 1976 passed / 8 skipped；前端 202 passed 与构建通过；Ruff 全量通过；本次 7 个生产文件 mypy 零错误（全量 mypy 既有 7 个错误未消除）。
- 部署与金丝雀：web/worker healthy、`/health/live` 200；先恢复第 13 组，新 recovery/generation 工作流均 Completed、当前指针切换成功，日志无 `unhashable type: 'dict'`，随后恢复其余 11 组。
- 最终结果：12 条新恢复工作流全部 Completed；第 20 组过时旧恢复工作流在成功资产确认后精确终止；`storyboard_current` **67/67 SUCCEEDED**，最新生成记录全部 SUCCEEDED，UNKNOWN=0，未确认故事板 outbox=0。实际新增提交 16 次，未超过授权上限；该项目 AUTO 已继续进入视频阶段。

## 21. 2026-10-18 UNKNOWN 恢复 fail-closed 补口上线（storyboard-recovery-supervisor-20261017c）

- 内容：补齐 Temporal 状态不明或无法补登记时的 AUTO 暂停出口 `storyboard_recovery_unavailable`，避免监督器继续静默 RUNNING；目标 104 项、叠加源码 97 项测试通过，后端全量 1977 passed / 8 skipped，前端 202 passed，相关 PostgreSQL 4 passed。
- 镜像：`tubeflow-test:storyboard-recovery-supervisor-20261017c`＝`sha256:e60792a0a82ab910e8dafc253049e8ce0f2b6c7c273a9140465d75a4a9fd5c8a`；compose 两机 md5 均为 `04cf15531fa6b01486b479ef0d1782b8`，部署机旧 compose 备份为 `docker-compose.yml.bak-pause-fallback-20261018`。
- 执行结果：web/worker healthy、暂停出口容器内探测通过；项目 AUTO run `7b412efc…` 已为 **COMPLETED**，故事板 67/67、UNKNOWN=0，新增故事板提交仍为 16 次，费用未知。

## 22. 2026-10-19 阶段页标题与第 02 步文案复制上线（copy-buttons-20261019）

- 需求：所有阶段页项目标题支持点击复制完整标题；第 02 步「文案内容」面板头状态药丸右侧新增「复制文案」，复制完整原文（保留空行与 `---` 等分隔行）；冻结读 `snapshot.script`，草稿读 `#scriptEditor` 当前值；空文案呈禁用样式并提示，不覆盖剪贴板。
- 改动（仅前端）：`app.js`、`styles.css`、新增 `frontend/clipboard-copy.js`；`scripts/build.mjs` 与 `package.json` 的 lint/typecheck 清单同步加入新模块；新增 `frontend/clipboard-copy.test.js`。
- 门禁：`npm run check` 全绿（前端 **208 passed**，含新增 6 项；lint / typecheck / build 通过）。
- 基线更正：部署机实际运行镜像为 `sha256:ff4e5d435814e8163e0d1121d124bc4723af827ece8845bb33ce70d6ac89a431`（`video-timing-20261019`）；仓库 compose 当时的 `e60792a0…` 已过期，本次以部署机实际镜像为 overlay 基线。
- 镜像：`tubeflow-test:copy-buttons-20261019`＝`sha256:1ba8a1b659e9ed2ece96ed007bed4b11d8a8569d9357b400b309330cea18100d`，部署机构建，仅 COPY `app.js` / `styles.css` / `frontend/clipboard-copy.js`。
- compose：三处钉改为新镜像；部署机备份 `docker-compose.yml.bak-copy-buttons-20261019`；本地已同步，两机 md5 `28a3dcdd576785da706b28b617e0d7d8`。
- 部署前置：无 RUNNING AUTO（PAUSED 1：`a5367080…` `composition_failed`）。
- 执行结果：migrate / minio-init 退出 0；web/worker healthy；`/health/live` 200；线上 `app.js` md5 `49fd2b18a1d011b9666e0884c6f0fe2e`、`styles.css` `805f68fa40eb6da2cb34690585151231`、`frontend/clipboard-copy.js` `e79feab8234cf3329a1b887e5d15b86a`，均与本地一致；新模块 200 `text/javascript; charset=utf-8`、`Cache-Control: no-cache`。
- 浏览器验收（Chrome 154，`http://192.168.110.200:8000`，`isSecureContext=false`）：点击标题复制完整标题（长标题 107 字，auto / script / STEP 01 三个页面均通过）；冻结文案复制 12,629 字（含 11 个 `---` 分隔行）与文本框完全一致；草稿项目复制「测试」；空文案点击提示「文案为空，暂无可复制内容」且剪贴板保持原值；禁用态 opacity 0.55 / cursor not-allowed；拖选标题不触发复制。`execCommand('copy')` 兜底在真实鼠标手势下工作；无手势的合成点击只提示失败，不伪造成功。现场截图：`runtime/operations/copy_buttons_step02_20261019.png`。
- 回滚：恢复 `docker-compose.yml.bak-copy-buttons-20261019`，或三处钉改回 `sha256:ff4e5d43…` → `docker compose up -d --no-build web worker`；旧镜像保留。
- 观察（未处理）：媒体图片加载时出现既有 `Exception ignored ... media stream read failed`（对应请求 200，非本次改动引入）。

## 23. 2026-10-20 语言展示中文化上线（locale-zh-20261020）

- 需求：页面所有涉及的语言代码（`en-US` 等）改为中文展示，覆盖下拉框、项目卡片、提示文案与音色卡「支持语言」共 9 处；用户确认口径为「下拉/卡片/提示用长名（英语（美国））、音色卡用短名（英语 · 日语）、仅改前端展示层」。
- 改动（仅前端）：新增 `frontend/locale-labels.js`（1964 B，md5 `685f94de1dfb3e171fc71d2aec58c064`，含有序 `LOCALES`、`localeLabel`/`localeShortLabel`/`localeShortListText`，未知码回退原码）；`app.js`（383446 B，md5 `fee7e1b23a9d8d6991082d12e4cd7771`）9 处改用中文标签，`option value`、项目补丁与创建请求的 `locale` 仍为 BCP-47 代码；新增 `frontend/locale-labels.test.js`；`scripts/build.mjs` 与 `package.json` 的 lint/typecheck 清单同步加入新模块；`styles.css`（md5 `fdf4d1ded2ee6bbe03094494d9c32c05`）与 `index.html`（md5 `ca646784c504bfe5346147acab86b0a5`）未改。
- 门禁：`npm run check` 全绿（前端 **223 passed**，含新增 7 项；lint / typecheck / build 通过）。
- 基线更正：部署机实际运行镜像为 `sha256:c8e3184b280c0c03c96d38a0b45fdceef51eefdd0db8ff2fb170cddaf87773fb`（`publication-20261020`，2026-10-20 01:43 部署，此前未单独记录）；仓库 compose 三处钉与之一致，md5 `55aad77fb267eb9a9f2959bcbae6e9d7`。本小节同时作为 §22 之后镜像钉的更正记录。
- 镜像：`tubeflow-test:locale-zh-20261020`＝`sha256:143dadc4d829ab5d6f8d76591c9575832d2ad9071736ba1f3793b1825fb3ecde`（1103466140 B）；部署机独立目录 `/home/edy/tubeflow-migrate/build-locale-zh-20261020` 构建，`FROM tubeflow-test:publication-20261020`（必须用 tag，`FROM sha256:` 会被 BuildKit 当远程镜像拉取），仅两条 COPY：`app.js → /app/frontend/app.js`、`frontend/locale-labels.js → /app/frontend/frontend/locale-labels.js`。
- compose：部署机备份 `docker-compose.yml.bak-locale-zh-20261020`（=旧钉 c8e3184b，md5 `55aad77f…`）；三处钉替换后两机 md5 均为 `126f290f2e6a775e372cd4bbe2f1842d`，钉计数 3。
- 部署前置：无 RUNNING AUTO（COMPLETED 12 / PAUSED 1）；`alembic_version=0039`；`projects=19`；无并行构建；根分区可用 779G。
- 执行结果：`docker compose up -d --no-build` 后 migrate / minio-init 退出 0，web/worker healthy 且镜像为新摘要；`/health/live` 200；线上 `app.js` 200 `text/javascript; charset=utf-8`、383446 B、md5 `fee7e1b2…`；`/frontend/locale-labels.js` 200 `text/javascript; charset=utf-8`、1964 B、md5 `685f94de…`；`styles.css`/`index.html` md5 未回归；容器内 web 与 worker 的 `app.js`/新模块 md5 与 served 一致；`alembic_version` 仍 `0039`；worker ready、近 10 分钟无 error；`projects` 仍 19。
- 注意：该服务对未知路径回落 `index.html` 且返回 **200**，因此校验新模块必须看 `Content-Type: text/javascript` 与字节数，不能只看状态码（本次以 `/frontend/not-exist.js` → 200 `text/html` 做反例对照）。
- 浏览器验收（agent-browser / Chromium，`http://192.168.110.200:8000`，账号 `admin-74d1abc03713`，口令由用户提供、未入库）：项目卡片显示「英语（美国）」且页面无 `en-US` 残留；新建页语言下拉六项中文长名、`option value` 仍为代码、`FormData.locale` 仍为 `en-US`；切到日语时提示「频道默认音色不支持 日语（日本），请从下方 213 个兼容音色中手动选择。」；STEP 01 冻结项目显示「英语（美国）」且 `select.value=en-US`；STEP 05 说明「仅显示支持 英语（美国） 的系统固定不可变版本」、音色卡为短名（如「英语 · 法语 · 西班牙语 · 葡萄牙语」，并带 `title` 悬停全文）、语言筛选项中文；控制台无模块加载 / MIME / 404 报错。截图 `runtime/operations/locale_zh_20261020/`。
- 回滚：恢复 `docker-compose.yml.bak-locale-zh-20261020`（或三处钉改回 `sha256:c8e3184b…`）→ `docker compose up -d --no-build`；本次未改迁移、`alembic_version` 未变，**无需 alembic downgrade**（回滚后 `/frontend/locale-labels.js` 会落回 SPA 兜底的 200 `text/html`，旧 `app.js` 不引用该模块，属正常）。

## 24. 2026-10-20 成片清晰度方案上线（HD 工作流 + 1080p 容器，hd-storyboard-20261020）

- 决策：`USER_DECISIONS §32`；实测依据：`docs/VIDEO_SHARPNESS_DIAGNOSIS_20261020.md` 与 20 组参数矩阵 `tmp/h3check/matrix.html`。
- 引擎（KineStack）改动：新增 `app/workflows/h3-tubeflow-storyboard-hd-1.0.1.json` + manifest（复制自 `h3-kinestack-r2v-images-hd-1.0.1`，只改 title 与 `filename_prefix`）；`app/h3_workflows.py` 注册 `TUBEFLOW_STORYBOARD_HD_WORKFLOW_ID`、加入 `candidate-hd` profile、`namespace_for` 归入 `tubeflow-test`。
- 引擎镜像：`kinestack-h3-clones:hd-storyboard-20261020` = `sha256:8f396a271b1c62aa04835ef62c951384742f6b13e63cfb890b625abbb50276a1`；构建目录 `/home/edy/KineStackData/build/hd-storyboard-20261020`（`FROM kinestack-h3-clones-base:20260919` + `COPY app /app/app`；基础镜像原为无 tag 的 `sha256:88c0df22…`，先 `docker tag` 再 FROM，避免 BuildKit 把 `FROM sha256:` 当远程镜像拉取）。
- 引擎部署：`/home/edy/KineStackData/config/h3-clones-compose.yaml` 三处钉替换（备份 `.bak-hdstoryboard-20261020`），`docker compose -p kinestack-h3-clones --env-file h3-clones.env -f h3-clones-compose.yaml up -d`；三节点 `healthy`，`/capabilities` 返回 `h3.tubeflow.storyboard-to-video-hd@1.0.1`、manifest `dc96c84e0ceeb328a0601909c995e304071ba9913f1df4dfaf77f33e016edd31`、`AVAILABLE`。
- TubeFlow 改动：`domain/composition.py` 新增 `group_width/group_height/crf/sharpen`；`application/media_authority.py` 固定输出 `1920×1080`、`COMPOSITION_CRF=20`、`COMPOSITION_SHARPEN=0.4`；`adapters/ffmpeg.py` 组校验改用组尺寸，concat 后按需 `scale/crop/unsharp`，argv 增加 `-crf`；`adapters/persistence/production_authority.py` 快照输出尺寸改为固定 1080p；`application/composition.py` 载荷解析兼容旧记录；`config.py` 允许 HD 工作流与 `1.0.1`；`video_generation/engine.py` 持久执行合同加 HD id。
- TubeFlow 镜像：`tubeflow-test:hd-storyboard-20261020` = `sha256:0121a5b80762e601578cb33a65f0f83e2897fdd79ef3ef3242223727e10e115b`；构建目录 `/home/edy/tubeflow-migrate/build-hd-storyboard-20261020`（`FROM tubeflow-test:baseline-hdstoryboard-20261020`，即旧镜像 `sha256:6bd3ae50…`/tag `delete-confirm-20261020`，仅 COPY 7 个 py 文件到 site-packages）。
- `.env`：`TUBEFLOW_H3_CANDIDATE_WORKFLOW_ID=h3.tubeflow.storyboard-to-video-hd`、`VERSION=1.0.1`、`MANIFEST_SHA256=dc96c84e…`（备份 `.env.bak-hdstoryboard-20261020`）；compose 备份 `docker-compose.yml.bak-hdstoryboard-20261020`。
- 部署结果：`docker compose up -d --no-build`；web/worker healthy（新镜像）；`/health/live` 200；容器内 `Settings` 读到的 HD 工作流身份与 manifest 一致；`/group-videos/contract` 返回 `{"available":true}`；`alembic_version=0039` 未变；`projects=18`（此前用户删除过项目，`deletion_tombstones=2`，非本次改动）。
- 合成链路实测（新镜像 + 真实 HD 组视频 1600×896）：输出 1920×1080 / 24fps / 362 帧，`encoding={crf:20, sharpen:0.4, group:1600×896, output:1920×1080}`，滤镜尾部 `scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,crop=1920:1080,unsharp=5:5:0.4:5:5:0.0`。
- 测试：TubeFlow unit `1986 passed, 8 skipped`（本地无 ffmpeg）；H3 路由/合同/合成定向 `63 passed`；KineStack engine `264 passed`（pytest 在镜像内临时安装）。
- 回滚：引擎恢复 `h3-clones-compose.yaml.bak-hdstoryboard-20261020` → `docker compose -p kinestack-h3-clones ... up -d`；TubeFlow 恢复 `docker-compose.yml.bak-hdstoryboard-20261020` 与 `.env.bak-hdstoryboard-20261020` → `docker compose up -d --no-build`。无迁移，无需 alembic downgrade；旧镜像均保留。
- 未做：未用新项目做真实生成验收（需用户触发一次新项目生成）；存量项目不重做。

## 25. 2026-10-26 STEP 11 简介上线（description-step11-20261026）

- 内容：`USER_DECISIONS.md` §36 / 验收 §34；新增第 11 步「简介」、频道级提示词模板（初始 V1、版本不重用、自动保存、删除确认）、最终 SRT 门禁、结构化生成、真实时间注入、历史/当前/人工编辑/TXT、幂等生成；迁移 `0043`。
- 镜像：`tubeflow-test:description-step11-20261026` = `sha256:958ec52fc5dd06b299c2cb753b96790c58beabce05a36482a369ab654de5b0d5`（1,110,020,823 B）。构建方式：部署机独立目录 `/home/edy/tubeflow-migrate/build-description-step11-20261026`，以现役 `tubeflow-test:cover-uploads-20261023`（`a117a6be…`）为底，将当前源码同时写入 `/app/src/tubeflow_test` 与 site-packages 包路径，并覆盖迁移与前端静态目录。构建上下文 md5 `8ea6943c579ddee19737976e14725934`。
- 镜像包：`/home/edy/tubeflow-migrate/tubeflow-description-step11-20261026.tar.gz`（264M，md5 `313a579330b20b3df6522c7c03886db8`）。
- 前置检查：`projects=30`；AUTO 22 COMPLETED / 2 PAUSED / 2 条 2026-10-21 起未更新的历史 RUNNING 标记（用户明确说明其余均为历史冻结项目，不阻塞本次）；活动合成 0、staging 0、Temporal 无运行中工作流；磁盘可用 756G。
- 数据库备份：`/home/edy/backups/tubeflow-pre-description-20261026.dump`（924M，sha256 `6187c94058f54902cc6a59cb5f609865f52029f987c6f14369d8085828fbbbaf`）。
- compose：部署机备份 `docker-compose.yml.bak-description-20261026`（旧钉 `a117a6be…`），`.env` 备份 `.env.bak-description-20261026`（主部署未改 `.env`；v4 变量为同日追加，另见下条与 `.env.bak-v4-1.0-20261026`）。三处镜像钉替换后两机 sha256 均为 `297bfd96d09b595fe30912b0e6aa35f983f5e89266dc8963ad21289e16d9f16b`，md5 `36af9a152771bf410f3f5b4171088d2f`；本地旧 compose 备份同名文件已改为部署前真实版本。
- 执行结果：`docker compose up -d --no-build`；migrate 退出 0，`alembic_version=0043` 且四张简介表存在；web/worker 均以新镜像 healthy；`/health/live` 200。
- 线上验证：容器内导入 `tubeflow_test.video_descriptions.api` 与 `deepseek_description` 成功；OpenAPI 含 6 条简介路由；`/frontend/description-view.js` 200 `text/javascript`（11,174 B，md5 `070b6c24254dd40f20d5da0b720d45ff`）；`app.js` md5 `0872d10b6350410e890b4837a3097165`、`styles.css` `478b98dcfe3fc4213682d657525b4492`、`api-client.js` `7d097ea14725a49c2d2bea9b5f3129f4`，与本地逐一一致。实际浏览器已访问简介模板与项目简介 GET，均 200；首个频道懒初始化 V1 模板 1 条，简介版本/尝试均为 0。worker 启动无错误。
- 重建后复核（v4 `--force-recreate` 之后，19:30）：`/app.js` 0872d10b…、`/styles.css` 478b98dc…、`/frontend/api-client.js` 7d097ea1…、`/frontend/description-view.js` 070b6c24…，与本地同名文件逐一相同；HTTP 探测均为 200 且 `Content-Type` 正确。**注意**：`/app/frontend/api-client.js` 与 `/app/frontend/frontend/app.js` 是镜像内陈旧副本、无任何入口引用（入口 `index.html` 用绝对路径 `/app.js`，`app.js` 只 import `./frontend/*.js`），核对指纹时认准 `/app.js` 与 `/frontend/*.js` 四个文件，不要误判为版本回退。
- ElevenLabs v4 启用（同日追加，`USER_DECISIONS.md` §35）：部署机 `.env` 追加 4 项 `TUBEFLOW_ELEVENLABS_TTS_MODEL=eleven_v4`、`TUBEFLOW_ELEVENLABS_STABILITY=1`、`TUBEFLOW_ELEVENLABS_SIMILARITY_BOOST=1`、`TUBEFLOW_ELEVENLABS_USE_SPEAKER_BOOST=true`；`.env` 备份 `.env.bak-v4-1.0-20261026`（改前），改后 `.env` sha256 `52a4254812d44c993237f7daead96f7d1793142650bcf6bfde16db7b8c2534bc`。执行 `docker compose up -d --no-build --force-recreate web worker` 使 env 生效（web/worker 仍在同一镜像 `958ec52f…`，healthy，`/health/live` 返回 `{"status":"ok"}`）。容器内实读取证（web 与 worker 一致）：`eleven_v4 1.0 1.0 True {'stability': 1.0, 'similarity_boost': 1.0, 'use_speaker_boost': True}`。注意 web 容器重建会把 2026-10-21 的 `app.js` overlay 回退为镜像内版本（该 overlay 已于本次镜像烘焙进镜像，无影响）；`.env` 变更后需 `--force-recreate`，仅 `restart` 不会重读 env。
- 说明：本次镜像烘焙了 ElevenLabs v4 代码与配置读取，`.env` 已在同日启用 v4；此前“未增加 v4 变量、仍走 multilingual_v2”的表述以本条为准作废。真实 TTS 请求尚未发生（未产生计费）。
- 回滚：恢复部署机 `docker-compose.yml.bak-description-20261026`（或三处钉改回 `sha256:a117a6be…`）→ `docker compose up -d --no-build`；本次迁移为附加表，回滚应用无需删除 0043 表（旧镜像不使用它们）。旧镜像仍在部署机。仅回滚配音参数则恢复 `.env.bak-v4-1.0-20261026` 后 `--force-recreate web worker`。

## 26. 2026-10-27 项目列表「未发布」状态热覆盖（publish-unpublished-state-20261027）

- 内容：`USER_DECISIONS.md` §30.11 / 验收 §29；有当前合成成片时卡片同时显示灰色「● 未发布」与「标记已发布」，无当前成片与删除中项目只显示状态；详情页两处入口不变；不改后端、不加迁移。
- 变更面：仅 `/app/frontend/app.js` 的 `projectPublicationMarkup`（+4 行）；`styles.css` 未改（容器内 1868 行 = 仓库行数）。
- 方式：热覆盖（`docker cp /home/edy/app.js.new → 容器:/app/frontend/app.js`），未重建容器、未改镜像 / compose / `.env`，worker 未重启；现役镜像仍 `sha256:f6e7703e391255ad4f8dce422b64e9b4a7e4626b9dd90b100d0ca69f8097fa83`，`alembic_version=0044`。
- 安全依据：覆盖前用 `docker cp` 把容器版取到 `/home/edy/app.js.pre` 并与本次本地版 `diff`，只有 1 个 hunk（即本次改动），因此整文件替换不会回退 11:44 的封面模板 V2 与折叠容器修复。
- 校验：`GET /health/live` 200；`GET /app.js` 200 `text/javascript`、438876 B、8720 行（覆盖前 8716）、`cache-control: no-cache`；取回服务端文件与本地 `diff` 零差异；web 日志近 6 分钟无 error / traceback；容器 `Up (healthy)`。
- 备份与回滚：覆盖前文件在 `/home/edy/tubeflowai-test/runtime/operations/publish-unpublished-state-20261027/app.js.pre`（开发机 `tmp/deploy-check/app.js.pre`）；回滚 `sudo docker cp app.js.pre tubeflowai-test-web-1:/app/frontend/app.js`，随后 `/app.js` 应回到 8716 行。
- 注意：热覆盖不持久，任何 `docker compose up`（含 `--force-recreate`、删容器、migrate 重建）都会打回镜像内 8716 行旧版，需重新覆盖；仓库源码已含本次改动，下次从本仓库重建镜像即自然包含。本环境 md5 / sha256 输出损坏（出现非十六进制字符、位数不对），核对请用 `diff` + 行数，不要用指纹。

## 27. 2026-10-27 长音频响度回落上线（long-audio-loudness-20261027）

- 内容：`>3s` 音频在两遍 `loudnorm(linear=true)` 过不了响度门时回落「增益+限幅+闭环校正」（配方 `long-f32le-24k-gain-limiter-v1`），仍受同一响度门（±1 LU / TP ≤ -0.8 dBTP）与时长守恒约束；新增稳定码 `audio_loudness_unreachable` 落 `safe_error_code`，`_complete_raw` 增加结构化失败日志。依据与实测数据：`docs/AUDIO_LOUDNESS_LONG_CLIP_20261027.md`。
- 门禁：后端 `tests/unit` **2099 passed / 8 skipped / 1 failed**（唯一失败为 `test_production_container_contract.py::test_dockerfile_uses_migration_gated_api_console_and_ffmpeg_smoke`，属 `backend/Dockerfile` 早前改阿里云源造成的既存漂移，与本次无关，未修）。
- 构建：`docker build -f backend/Dockerfile -t tubeflow-test:long-audio-loudness-20261027 .`（9m58s）；镜像 `sha256:63f559257d6cc64ee44117c0cd80d603a5d326b19dab65dccf0fba4b7ec315a7`；镜像内自检含新配方与日志语句。
- 传输/载入：`docker save | gzip` → `/home/edy/tubeflow-migrate/longaudio-20261027.tar.gz`（277,998,780 B，`gzip -t` 通过）→ `sudo docker load`（Loaded image 摘要与本地一致）。
- 上线：先 overlay 两文件（`/home/edy/tubeflow-migrate/overlay-longaudio-20261027/`，含 `pre.tar.gz` 原文件备份）并重启验证三个真实产物；随后改钉 compose 三处为 `sha256:63f55925…`（部署机备份 `docker-compose.yml.bak-longaudio-20261027`），`docker compose up -d --no-build`；migrate 退出码 0，web/worker healthy，`alembic` 仍为 `0044`。
- 真实产物复验（新镜像 worker 容器内，seq 229 三个检查点）：全部 `long-f32le-24k-gain-limiter-v1`，输出 -16.68 / -16.78 / -16.62 LUFS，TP -1.04 / -1.04 / -1.03，帧数 345,600 / 349,440 / 349,440（源 2:1，时长不变）。
- 回滚：compose 三处改回 `sha256:f6e7703e391255ad4f8dce422b64e9b4a7e4626b9dd90b100d0ca69f8097fa83` 后 `docker compose up -d --no-build`；或解包 overlay 目录 `pre.tar.gz` 覆盖两文件并 `restart web worker`。
- 本地库：改动已提交 `f0e0180`（含 2026-10-14 未提交的短音频 ±12/15 dB、v3 与 `WAVE_FORMAT_EXTENSIBLE` 支持）；本地 `docker-compose.yml` 三处镜像钉同步为 `sha256:63f55925…`（此前本地落后于部署机，已对齐）。

## 28. 2026-10-27 视频提示词编译诊断落库（video-prompt-diag-20261027）

- 内容：新增迁移 `0045` 与 `video_prompt_compilations` 诊断表；`VideoPromptCompiler` 每次尝试（provider 响应错误 / 合同校验失败）记录策略、原因码与模型原始输出；`compile_video_prompt` 绑定 owner/project/group/attempt 上下文；写入失败只记日志，不影响生成。校验规则、修复次数（AUTO 2 次）与暂停语义均未改动。依据：`docs/VIDEO_PROMPT_COMPILE_DIAGNOSTICS_20261027.md`。
- 门禁：后端 `tests/unit` **2101 passed / 8 skipped / 1 failed**（唯一失败仍是 `test_dockerfile_uses_migration_gated_api_console_and_ffmpeg_smoke` 的既存 Dockerfile 漂移）；`ruff check` 改动文件通过。
- 迁移预检：部署机临时库 `tubeflow_migcheck_0045` 上 `upgrade head`（→0045）与 `downgrade 0044`（表/索引均删除）均通过，随后 drop 临时库。
- 构建：`docker build -f backend/Dockerfile -t tubeflow-test:video-prompt-diag-20261027 .`（4m30s）；镜像 `sha256:f05d58c1eda02a983858c22f85c6faeef007ee2c85074ec37150c08db1ae0ca4`。
- 传输/上线：`docker save | gzip` → `/home/edy/tubeflow-migrate/videodiag-20261027.tar.gz`（278,008,854 B，`gzip -t` 通过）→ `docker load` → compose 三处改钉（备份 `docker-compose.yml.bak-videodiag-20261027`）→ `up -d --no-build`；migrate 退出码 0、`alembic_version=0045`、`video_prompt_compilations` 存在、web/worker healthy、`/health/live` 200。
- 回滚：compose 三处改回 `sha256:63f559257d6cc64ee44117c0cd80d603a5d326b19dab65dccf0fba4b7ec315a7` 后 `up -d --no-build`；如需删表 `alembic downgrade 0044`（仅删诊断表）。
- 待办取证：项目 `22dbcf09` 组 17（group_id `c94b5808-c15b-585a-8de1-76ded95bbde6`）需 `POST /projects/{id}/group-video-batch/generations {"group_ids":[...],"force":true}` 重跑，才能产生诊断行；该接口需 Bearer 会话。

## 29. 2026-10-27 v3 提示词禁止故事板主体编号（prompt-v3-subjectfix-20261027）

- 内容：按诊断证据（模型把故事板写成 `(S2)`，retention 行随之写 `- (S2) — shot_plan_reference_only:`）收紧 v3 规则：故事板条目必须以 `- Picture {N}:` 出现、严禁 `(Sx)` 编号；retention 条目不得把主语写成 `(Sx)`。v2 对照不变，校验规则与修复次数未动。
- 门禁：后端 `tests/unit` **2103 passed / 8 skipped / 1 failed**（唯一失败仍是既存 Dockerfile 契约漂移）；`ruff` 通过；新增两条模板回归测试。
- 构建：`docker build -f backend/Dockerfile -t tubeflow-test:prompt-v3-subjectfix-20261027 .`（2m12s）；镜像 `sha256:7f94b8a5945161197d70c22b44b23ec5dee7c976c1588bf73b81f68d565bb009`；镜像内已含新规则文本。
- 上线：`docker save | gzip` → `/home/edy/tubeflow-migrate/promptv3fix-20261027.tar.gz`（278,008,816 B）→ load → compose 三处改钉（备份 `docker-compose.yml.bak-promptv3fix-20261027`）→ `up -d --no-build`；migrate 退出码 0、web/worker healthy、`/health/live` 200。
- 影响：提示词文本参与单元 canonical 哈希，新规则只对之后新编译的单元生效；已在途/已编译单元不受影响。
- 回滚：compose 改回 `sha256:f05d58c1eda02a983858c22f85c6faeef007ee2c85074ec37150c08db1ae0ca4` 后 `up -d --no-build`。

## 30. 2026-10-28 大项目合成 execve E2BIG 修复上线（composition-script-20261028）

- 根因：`adapters/ffmpeg.py` 把整片压成一次 ffmpeg 调用，170 组/711 句的 `-filter_complex` 约 150 KB，超过 Linux 单参数上限 `MAX_ARG_STRLEN=131072`（容器实测 131071 通过、131073 → `OSError: [Errno 7]`），每次合成 attempt 1 必失败；attempt 2 再抛 `composition_retry_required`/`composition_in_progress` 掩盖真因，AUTO 以 `composition_failed` 暂停。
- 改动：`adapters/ffmpeg.py` 图落盘改 `-filter_complex_script`（`manifest.filter_graph` 仍存明文），`OSError` 转 `DomainError("composition_exec_failed")`；`workflows/composition.py` 把 OSError 记为不可重试并上调 `start_to_close_timeout` 30 → 60 分钟；`frontend/auto-progress.js` 为 `composition_failed` 回显 `details.error_code`；compose 为 web/worker 增加 `ulimits.nofile=65536`（原 1024，本项目需 885 个 fd）。
- 门禁：后端 `tests/unit` **2104 passed / 10 skipped / 6 failed**（1 例既存 Dockerfile 契约漂移；其余 5 例单独复跑全通过，属全量顺序污染）；新增 `test_composition_argv_budget.py` 3 例；前端 255 通过。
- 构建：`docker build -f backend/Dockerfile -t tubeflow-test:composition-script-20261028 .`（构建目录 `/home/edy/build-comp-20261028`，本地打包 789 KB 上传；**构建目录内**把 pip 源换成阿里云，仓库 Dockerfile 未改）。
- 上线：镜像 `sha256:880b86002eb66c9610cd90c7886de387a3fa947cb5dbedb0cd45dc142e9f28f5`；compose 三处改钉 + ulimits（备份 `runtime/operations/composition-script-20261028/docker-compose.yml.bak`）→ `up -d --no-build`；migrate 0、`alembic_version=0045`、web/worker healthy、容器内 `ulimit -n=65536`。
- 复验：容器内 40 组子集真实合成 **191 s / 12,008 帧 / 500,333 ms**（目标 500,320，差 13 ms）；正式 r12 合成 **52,118 帧 / 2,171,600 ms / 1920×1080 / 24fps / 48kHz**，ffmpeg 峰值 RSS 13.3 GB、885 fd，成片 1,235,585,199 B + SRT 55,421 B，`auto_pipeline_runs` 转 `COMPLETED`。
- 回滚：compose 改回 `sha256:5ad75bb9b9cdaaf0b22522ee47f8646274223030b315660531032dd5900afe16` 并去掉 ulimits 后 `up -d --no-build`。

## 31. 2026-10-29 配音批量失败与故事板画幅失败「自动恢复」修复（narration-aspect-fix-20261029）

- 根因一（配音）：`application/narration.py` 批量部分失败分支把 `provider_not_reached` 硬编码落成 `PROVIDER_REJECTED`（与单句路径 `SAFE_TO_RETRY` 相反），并把整批 ZIP 的 `provider_checkpoint` 挂到被拒句；Temporal 重试先走检查点分支 → 试图把终态改写为 `SAFE_TO_RETRY` → `PROVIDER_REJECTED` 无出边 → 每次 `stale_attempt_transition`，70 次空转到 `schedule_to_close=35min`，AUTO 以 `narration_provider_rejected` 暂停。
- 根因二（故事板）：`application/storyboards.py` 把画幅 QC 失败（`|w/h−16/9|>0.03`）直接写成不可重试终态，14 次换路预算只用 1 次；本次 provider 单次返回 `2131×738`（16:9 参数，SHA256 与期望一致）即触发 `storyboard_failed:aspect_ratio_not_supported` 停线。
- 改动：新增 `_submission_failure_state()` 统一单句/批量映射；拒绝句不挂批次检查点；终态判定前移到检查点分支之前；画幅失败改走 `_auto_resubmit()`（同路由 2 次 → 逐路由 → 至多 14 次，终态码不变）；画幅异常日志带实际像素。
- 门禁：后端 `tests/unit` **2109 passed / 10 skipped / 6 failed**（6 例均既存：Dockerfile 契约漂移 1 例 + 全量顺序污染 5 例，单独复跑通过）；目标四文件 176 passed。
- 上线（最终）：先 overlay 热覆盖验证（04:33Z），随后**重建镜像持久化**——新镜像 `sha256:60fb7005c60c12ba52188aa94b72b5e5d365f7fbd8975bbe57bcf52651d94dd4`（tag `tubeflow-test:narration-aspect-fix-20261029`），compose 三处 `image:` 改钉（备份 `runtime/operations/narration-aspect-fix-20261029/docker-compose.yml.bak`）→ `up -d --no-build`；migrate 退出码 0、`alembic 0045`、web/worker healthy、`/healthz` 200；`docker diff` 已无代码改动（overlay 归零）。
- 构建：上下文 `/home/edy/build-fix-20261029`（本地打包 3.9 MB，含 backend/src、migrations、frontend 与根三件套，另补传 `backend/Dockerfile`）；1m16s。**仅构建目录**把 pip 源由 tsinghua 改为 aliyun（tsinghua 对 `setuptools-84.2.1` 返回 403），仓库 Dockerfile 未改。镜像内核验：包内 250 文件 + 45 迁移与工作树逐一 sha256 相同。
- 备份/回滚：`docker-compose.yml.bak` 还原后 `up -d --no-build` 回 `sha256:880b86002eb6…`；该旧镜像仍在本机。
- 附：上线前已用 `docker diff` 确认全仓只有这 3 个文件存在热覆盖，无其它 overlay 遗留；本次未触发任何 provider 调用。

## 32. 2026-11-03 故事板身份参考 + 视频 v4 自然运动上线（quality-v4-20261103）

- 来源：PR #1 `4bde800`（`claude/video-quality-v4-v9`），fast-forward 合入 main 并推送。改动：故事板提示词追加「参考图只作身份参考」；新增可选策略 `tubeflow_storyboard_v4_natural_motion`（默认仍 v3）；导演单元提示词 v9（仅无历史运行的新项目，既有项目钉 v8）。
- 门禁：改动 5 文件 ruff check 通过、mypy --strict 通过；`ruff format --check` 仍有 4 文件待格式化（基线本就有 5 个，非本次引入）。后端全量单元 2195 passed / 5 failed / 10 skipped（无 ffmpeg）；5 例在合并前基线同样失败、单独复跑全部通过，属既有全量顺序污染。新增测试 23 项。
- 构建：本地打包 814 KB（排除 pyc/egg-info）→ 部署机 `/home/edy/build-quality-v4-20261103` → `docker build -f backend/Dockerfile`。镜像 `sha256:424f4ae9ce1a25fe22203c27f7f526425cc2f82bef59871e06a1e089d9aabb98`（`tubeflow-test:quality-v4-20261103`），镜像内 5 个文件 SHA-256 与 main 工作树逐字一致，迁移到 `0046`。
- 部署：compose 备份 `runtime/operations/quality-v4-20261103/docker-compose.yml.bak`，三处 `image:` 改钉后 `up -d --no-build`；migrate 退出 0，`alembic 0046` 不变，web/worker healthy，`/health/live` 200。
- 部署前：AUTO RUNNING 0、非终态故事板 0、非终态视频 attempt 0。历史 10 个故事板 `REMOTE_STATE_UNKNOWN`（9-07/10-15）与 1 个导演 UNKNOWN 无恢复工作流，按不在途处理。
- 部署后：`910fa912…` 最新导演运行仍为 `project-director-unit-v8`（91 单元），无新增运行、无 `auto_confirm_new_input` 审计。`TUBEFLOW_VIDEO_PROMPT_STRATEGY` 保持 `tubeflow_storyboard_v3`（A/B 期间临时切 v4 两次提交后已恢复，见 `Pending issues/2026-11-03/VIDEO_QUALITY_AB_RESULTS_20261103.md`）。
- 回滚：compose 还原备份后 `up -d --no-build` 回 `sha256:705f9ddf3f76…`（`tubeflow-test:single-panel-lock-fix-20261103`），该镜像仍在部署机。

## 33. 2026-11-03 默认 v4 + 故事板无文字 + 风格冲突子句移除（quality-followups-20261103）

- 来源：PR #2 `3a421a2`（`claude/quality-followups-20261103`）+ ruff 修复 `3a421a2`，合并到 main（`bc90cad` 仅文档）。
- 门禁：改动文件 ruff check、mypy --strict 通过；干净工作树单元 2208 passed / 0 failed / 10 skipped；集成 19 failed / 117 passed / 7 skipped（与 main 基线一致，缺 MinIO/Temporal/ffmpeg，非本次引入）。
- 构建：部署机 `/home/edy/build-quality-followups2-20261103`（git archive of `bc90cad`，仅 LF/CRLF 差异已核对）。镜像 `sha256:35a2f3f2949cab3234da34d5aec0b5210384a0780ce3ef88f0a64b131363fc84`（`tubeflow-test:quality-followups-20261103`）。
- 前置检查：AUTO RUNNING 0、在途故事板 0、非终态视频 attempt 0（项目 `765dca11` AUTO 处于 PAUSED，未触碰）。
- 部署：compose 备份 `runtime/operations/quality-followups-20261103/docker-compose.yml.bak`、`.env` 备份 `env.bak`；三处 image 改钉；`TUBEFLOW_VIDEO_PROMPT_STRATEGY` 改为 `tubeflow_storyboard_v4_natural_motion`；`up -d --no-build --force-recreate web worker`。
- 验证：web/worker healthy，`/health/live` 200，迁移保持 `0046`，两个容器内 `Settings().video_prompt_strategy` 均为 v4；`910fa912` 导演仍为 `project-director-unit-v8`（91 单元），无新增运行与审计。
- 注意：项目 `765dca11`（AUTO PAUSED）恢复后生成的故事板将使用新提示词（无 SHOT 角标、禁文字、风格冲突句被移除），其 canonical 请求哈希与暂停时不同，属预期。
- 回滚：还原 compose 与 `.env` 备份，`up -d --no-build --force-recreate web worker` 回 `sha256:424f4ae9ce1a…`。

## 34. 2026-11-03 seed 记录 + v4 代词规则上线（seed-pronoun-20261103）

- 来源：main `7263fe4`（合并 `b321bb1` seed 记录 + `979b996` v4 代词规则）。seed 分支门禁：单元 2214/0/10、ruff/mypy 通过、PostgreSQL 集成 `test_h3_storyboard_binding.py` 13/13 + 回放/媒体锁 7/7；合并树单元 2217/0/10、ruff 通过。
- 构建：部署机 `/home/edy/build-seed-20261103`（git archive of 7263fe4）。镜像 `sha256:c031714fe190df1713a07023e13c88e6ae07e79e5ccc299e1e1156e5bfa7b19b`（`tubeflow-test:seed-pronoun-20261103`），镜像内含 `_derived_seed` 与代词校验。
- 前置检查：AUTO RUNNING 0、在途故事板 0、非终态视频 0。compose 备份 `runtime/operations/seed-pronoun-20261103/docker-compose.yml.bak`，三处 image 改钉，`up -d --no-build`。
- 验证：web/worker healthy，`/health/live` 200，迁移 `0046` 不变，策略仍为 v4（web+worker 实读），`_derived_seed` 与 `_GENDERED_PRONOUN` 在容器内可用，`910fa912` 导演 v8 不变、无新审计。
- 同日：频道 `f28a0845`（韩国小生意经济财经）的 `default_visual_style` 已删除「保留原图人物特征、服装、姿势、表情、主体数量、场景布局和物品位置，仅转换画面艺术风格。」；原文备份 `runtime/operations/channel-style-20261103/default_visual_style.before.txt`。只影响新建项目快照。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:35a2f3f2949c…`；频道风格还原用备份文件 UPDATE。

## 35. 2026-11-04 导演 v10 + 重试带原因上线（director-v10-20261104）

- 来源：main `0e832f6`（合并 PR #3 `1fe137e`）。门禁：干净工作树单元 2231 passed / 0 failed / 10 skipped；集成 19 failed / 119 passed / 7 skipped（19 条与 main 基线同源）；ruff/mypy 通过。
- 前置检查一度被新项目 `d725172f`（director_preplan）挡住；按用户决策先暂停该项目 AUTO（`manual_deploy_pause`），部署后恢复（`resume_paused`）。
- 构建：部署机 `/home/edy/build-v10-20261104`（git archive of 0e832f6）。镜像 `sha256:8278db3ed79d93f24d3e7f644884022d3926649790f7bff18f85cc2fdd4e0567`（`tubeflow-test:director-v10-20261104`），镜像内含 `project-director-unit-v10`。
- 部署：compose 备份 `runtime/operations/director-v10-20261104/docker-compose.yml.bak`，三处 image 改钉，`up -d --no-build`。
- 验证：web/worker healthy，`/health/live` 200，迁移 `0046` 不变，策略仍为 v4（web+worker 实读），`PROJECT_DIRECTOR_PROMPT_REF = project-director-unit-v10`；`910fa912` 导演仍为 v8（91 单元）、`146ae08c` 最新成功运行仍为 v9（56 单元）且 canonical 不变；`765dca11` AUTO 已为 COMPLETED（此前由用户自行处理）。
- 恢复后实测：`d725172f` 的恢复运行 `74cb474b`（00:25:38 UTC）77 个单元全部为 v10，符合 §45.2/§45.4；`765dca11` 若恢复同样将用 v10。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:c031714fe190…`。

## 36. 2026-11-04 crop-grid-v2 修订上线（开关关闭，crop-grid-v2-rev-20261104）

- 来源：main `1707d85`（合并 PR #5 `571e122`，USER_DECISIONS §47）。PR #5 只改 `video_generation/panel_images.py` 与其单元测试、Gate 1 诊断脚本与两份规划文档；无前端、无迁移、无 settings/compose 变更。
- 门禁：分支 `571e122` 与 main `c5e369a` 四项逐项比对——单元 2342 passed / 2319 passed，两侧 0 失败（分支多 23 条新用例）；集成两侧均 19 failed / 119 passed / 7 skipped 且失败集合逐条相同（既有 0039-vs-0046 期望差异）；ruff 15 条、mypy 12 条两侧逐字节相同。无分支独有失败。
- Gate 1 重跑（只读）：口径改为提示词含 “Do not draw any readable text anywhere in the image” 的故事板，冻结点 548 张 / 6 项目；通过 539、失败 9（全部 `shot_panel_gutter_not_found`）= 98.3577%，≥90%。证据 `Pending issues/2026-11-04/singlepanel_test/gate1_rerun/`（commit `588cb2f`）。
- 构建：部署机 `/home/edy/build-pr5-20261104`（`git archive` of `1707d85`，只含 backend/frontend/根三件套，1.4 MB）。镜像 `sha256:238bd9ead5f33a2f186c838210da1ec13da93f59ef857b05e11fdd3d8b468b57`（`tubeflow-test:crop-grid-v2-rev-20261104`）；容器内 `panel_images.py` LF 归一 sha256 `5b6cfa16…` 与 `HEAD:backend/src/.../panel_images.py` 逐字一致，`_V2_WIDE_FRACTION=0.25`、`_V2_MIN_CELL_FRACTION=0.6`、`_V2_FRAMED_MAX=48`、`_V2_WHITE_MIN=240.0`，`_v2_wide_separator` 在、`_assert_no_interior_gutter` 已移除；`/app/frontend/frontend` 40 个模块、`/app/migrations/versions` 46 个。
- 部署：compose 备份 `runtime/operations/crop-grid-v2-rev-20261104/docker-compose.yml.bak`，`.env` 备份 `.env.bak-single-panel-off-20261104`；三处 image 改钉，`.env` 追加 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=false`（对所有 backend 服务生效），`up -d --no-build`，17:45:08–17:45:40。
- 验证：web/worker healthy（web 启动约 2.5 分钟，启动期 `/health/live` 000，之后 200），迁移 `0046` 不变，web 与 worker 容器内 `Settings().storyboard_single_panel_new_projects is False` 且 `env` 实读为 `false`，两容器镜像均为 `sha256:238bd9ea…`。`migrate` 一次性容器 Exited(0)。`.env` 变更使 `mihomo` 一并重建（healthy）。
- Gate 2（观察 30 分钟，T0 `2026-11-04T09:45:08Z`，T+30 `10:15:24Z`）：`grid_layout_version` 键 0 条（部署前后均 0），窗口内新增故事板 0 条；4348 组快照对比，4129 个已有当前成片的组 `sb_rows/sb_version_max/attempt_rows/intent_max` 全部不变；变化的 15 组全部为部署前未完成的组（`a0e52ea0` 13 组各 +1 次尝试，`d725172f` 2 组各 +1 次尝试），期间 `d725172f` AUTO 由 PAUSED(round 9) 被外部恢复为 RUNNING(round 10)，非本次部署动作。证据 `Pending issues/2026-11-04/singlepanel_test/gate2_rerun/`。
- 开关**未**开启，未创建测试项目；第 3–5 关未执行。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:8278db3ed79d…`；`.env` 还原备份（或删除该行）。

## 37. 2026-11-04 第 3 关开关开启（crop-grid-v2，镜像不变）

- 决定依据：USER_DECISIONS §46 第 10 条（用户原话「Gate 3 now…」）。不捆绑其他改动，用已部署镜像 `sha256:238bd9ea…` 开开关；按用户决定**未等待既有项目排空**（不再适用 AUTO RUNNING=0 等前置）。
- 操作：`.env` 备份 `.env.bak-single-panel-on-20261104`，仅把 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=false` 改为 `true`，`docker compose up -d --no-build`。T_on 2026-11-04T13:41:55Z（21:41:55+0800），13:42:27Z 完成。镜像未变、无迁移（`0046`）。
- 校验：web/worker healthy（web 启动约 80 秒），`/health/live` 200，两容器 `Settings().storyboard_single_panel_new_projects is True` 且 `env` 实读 `true`，镜像仍 `sha256:238bd9ea…`，`alembic 0046`。`migrate` 一次性容器 Exited(0)，`mihomo` 随 `.env` 变更重建（healthy）。
- 重启影响（事后 10 分钟快照）：视频尝试更新 2 条（`a0e52ea0`，RUNNING→ARCHIVED/COMPLETE），无 FAILED / REMOTE_STATE_UNKNOWN / QC_FAILED / local_integrity_blocked；新增故事板 0 条，带 `grid_layout_version` 0 条。既有项目只记录，见 `Pending issues/2026-11-04/DEFERRED_EXISTING_PROJECTS_20261104.md` 第 3 节。
- 第 3–4 关：新建测试项目 `faf27eb5-23d4-43e7-8269-029336e38167`（21:51 创建并确认配音）后，旁白作业停在 `DISPATCHED`（0 句、50 分钟无进展），AUTO 仍 RUNNING，未取得第 3–4 关证据；未重启、未恢复。记录 `Pending issues/2026-11-04/singlepanel_test/gate3_5/STALL.md`。
- 回滚：`.env` 还原 `.env.bak-single-panel-on-20261104` 后 `up -d --no-build`（镜像无需变更）。

## 38. 2026-11-05 PR #6 旁白作业 ID 修复上线（narration-jobid-20261104）

- 来源：main `01ccf01`（合并 PR #6 `aad06d1`）。修复一行：`api/narration_runtime.py` 的作业 ID 由 `narration_{snapshot_hash}` 改为 `narration_{project_id}_{snapshot_hash}`；无迁移、无前端、无 settings 变更。
- 门禁：分支 `aad06d1` 对 main `09cd418`——单元两侧 2342 passed；集成两侧 19 failed，通过数 119（main）对 120（分支，新增用例 `test_identical_snapshots_in_two_projects_get_distinct_narration_job_ids` 通过，单跑 1 passed in 3.64s），失败集合逐条相同；ruff 15 条、mypy 12 条输出逐字节相同。证据 `Pending issues/2026-11-04/singlepanel_test/gateA_pr6/`。
- 构建：部署机 `/home/edy/build-pr6-20261104`（`git archive` of `01ccf01`，只含 backend/frontend/根三件套）。镜像 `sha256:edee542dda35ceadd8b3688ee690838419f620d3b32197d99237406f81db6336`（`tubeflow-test:narration-jobid-20261104`）；容器内 `narration_runtime.py` LF 归一 sha256 `2450a3b9…` 与 git blob 一致。
- 部署：compose 备份 `runtime/operations/narration-jobid-20261104/docker-compose.yml.bak`，web/worker/migrate 三处改钉，`.env` 未改（`TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS` 保持 true），`up -d --no-build`，T_dep **2026-11-05T02:41:26Z–02:41:40Z**。按 §46.10 未等待既有项目排空。
- 校验：`/health/live` 200（启动约 100 秒），web/worker healthy 且均在新镜像，两容器 `Settings().storyboard_single_panel_new_projects is True`，`alembic 0046` 不变。
- 重启影响（10 分钟快照）：视频尝试被触碰 0；FAILED / REMOTE_STATE_UNKNOWN / QC_FAILED / local_integrity_blocked 0；新增故事板 37 条全部 grid-v2、全部属开关开启后新建的 `247462a4`（其自身故事板梯子重试），既有 v1 项目无新增。
- 效果：同内容新项目 `72a22f0a` 的作业 ID 为 `narration_72a22f0a-…_29ad09b7…`，旁白约 2 分 17 秒完成，AUTO 59 分钟 COMPLETED。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:238bd9ea…`。

## 39. 2026-11-05 v2 格内构图规则上线（v2-panel-rule-20261105）

- 来源：main `c7baf5b`（合并 PR #7 `1ec07ca`，USER_DECISIONS §46 第 11 条）。仅 `application/storyboard_prompt.py` 在 `layout_version == GRID_LAYOUT_V2` 时追加一段「每格为单一连续镜头、禁格内分屏/左右对比/画中画/示意图/箭头/漂浮图标」；v1 提示词不变。无迁移、无前端、无 settings 变更。
- 门禁：分支 `1ec07ca` 对 main `72fbe78`——单元 2348 vs 2342 passed（新增 6 条参数化用例），两侧 0 失败；集成两侧 19 failed / 121 passed / 7 skipped，失败集合逐条相同；ruff 15、mypy 12 逐字节相同。证据 `Pending issues/2026-11-04/singlepanel_test/gateA_pr7/`。
- 构建（按 1A 结论改用干净 worktree）：`git worktree add --detach` 到仓库外临时目录，`git archive` 取 `backend index.html app.js styles.css frontend` → 部署机 `/home/edy/build-pr7-20261105` → 镜像 `sha256:30ffc3c39729397df3d00607dc4dea6240ccc2ca5d0235f0a0de5cdb27040166`（`tubeflow-test:v2-panel-rule-20261105`）。容器内 `storyboard_prompt.py` LF 归一 sha256 `f95dc1e6…` 与 `c7baf5b` blob 一致。
- 部署：compose 备份 `runtime/operations/v2-panel-rule-20261105/docker-compose.yml.bak`，三处改钉，`.env` 未改（开关 true），`up -d --no-build`，T_dep **2026-11-05T05:36:46Z–05:37:00Z**。按 §46.10 未等待排空。
- 校验：`/health/live` 200（启动约 80 秒），web/worker healthy 且均在新镜像，两容器 `Settings().storyboard_single_panel_new_projects is True`，`alembic 0046` 不变。
- 重启影响（10 分钟快照 05:45:25Z）：视频尝试被触碰 12 条，全部属 `23c59183`（4 RUNNING / 4 CREATED / 4 ARCHIVED-COMPLETE，其自身恢复推进）；FAILED / REMOTE_STATE_UNKNOWN / QC_FAILED / local_integrity_blocked 0 条；新增故事板 0 条。
- 第二轮测试项目 `484746c4`：旁白约 2 分钟，AUTO **COMPLETED**（05:45:39Z → 07:14:37Z，89 分钟），13 组全部 grid-v2，checks 全绿，全片 HF 0.274（首轮 0.219）。证据 `Pending issues/2026-11-04/singlepanel_test/gate3_5_r2/`。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:edee542d…`。

## 40. 2026-11-05 组内非相邻构图回切与「恢复」如实反馈上线（开关关闭，recut-resume-20261105）

- 来源：main `0675f3b`（合并 PR #10 `c40b196`，USER_DECISIONS §48；合并预演树 `17dbd686` 与实际合并树逐位相同）。含 §48.1–2（恢复返回真实状态、暂停详情不跨次残留）、§48.3–5（开关关闭时逐字不变；开启且阈值为正、项目为 grid-v2 的 AUTO 组允许 1 条非相邻构图回切放行）、`recut-similarity-v1`、迁移 `0047`、开关 `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT` 默认 `false`。无 `.env` 变更。
- 门禁（分支对 main `66f7609`）：单元 **2426 passed / 0 failed**（main 2384 / 0）；集成两侧 **19 failed 逐条相同**、跳过 7 条原因相同，通过数 121（main）对 120（分支，差 1 条因分支里 `tests/integration/test_cover_image_upload_postgres.py` 是旧副本，少 `test_two_distinct_uploads_increment_version_and_switch_current`；合并后该文件取 main 版本 `e8eb749e…`，4 个用例）；ruff 15 条、mypy **12 errors / 7 files** 两侧相同，按行号归一化后逐行相同（只有因为新代码插入造成的行号位移）；`npm run check` 258 passed / 0 failed。
- 门禁（迁移）：隔离 PG 上 `0046 → 0047 → 0046 → 0047` 往返通过；`storyboard_director_revisions.composition_recut_approvals_json` 为 `json`、`NOT NULL`、默认 `'[]'::json`；迁移前写入的 2 行降级后数据保留、升级后读出 `[]`；不指定该列插入也得到 `[]`；`alembic heads` 单头 `0047`。
- 门禁（第 8 组回放，附录 B）：三行输出与文档逐字一致（`shot6 vs shot1` 差 3 维 / 事件键 `False`；原样播种 `ValueError(...) review=NOT_REQUIRED`；第 07 步保存 `OK review=NOT_REQUIRED may_apply=True`）。
- 构建：干净 worktree `D:/AAA-project/wt-build-pr10` @ `0675f3b` → `git archive` 取 `backend index.html app.js styles.css frontend`（1.4 MB gz，515 文件）→ 部署机 `/home/edy/build-pr10-20261105` → 镜像 `sha256:fa167f1b6432075500a97ace31678ac60d67dfbddcfc14e2ba54cd714e4ae347`（`tubeflow-test:recut-resume-20261105`，构建 2 分 36 秒）。镜像内 16 个文件（6 个前端 + 9 个后端模块 + 迁移 `0047`）LF 归一 sha256 与 `0675f3b` blob 逐条相同。
- 门禁（部署机，548 张 Gate 1 原图，新旧都从 MinIO 取）：旧侧 = 部署前 worker（`3175dc0b`），新侧 = 新镜像一次性容器；两侧各 548 行输出 **逐字节相同**（544 通过、4 张仍 `shot_panel_gutter_not_found`，`boxes_changed_among_passed_before` 空、`newly_failing` 空、`newly_passing` 空）。
- 部署：compose 备份 `runtime/operations/recut-resume-20261105/docker-compose.yml.bak`，web/worker/migrate 三处改钉，`up -d --no-build`，T_dep **2026-11-05T10:09:53Z–10:10:06Z**。按 §46.10 未等待排空。
- 校验：`migrate` 退出码 0，`alembic 0046 → 0047`；`/health/live` 200（启动约 50 秒），web/worker healthy 且均在新镜像；两容器 `recut=False`、`min_distance=None`、`single_panel=True`；`/app.js` 200 `text/javascript` 440242 字节、`/frontend/auto-progress.js` 200 12220 字节，均含新代码标记。
- 重启影响（10 分钟快照 10:20:10Z）：视频尝试被触碰 **17 条**，全部属 `247462a4`（7 CREATED / 4 ARCHIVED / 3 RUNNING / 2 SUCCEEDED / 1 SUBMITTED，其自身梯子重试并在推进；1 条 `video_content_black_region`）；新增故事板 **11 条**全部 grid-v2、全部属 `247462a4`，且全部产生于 10:11 之前（部署后 10 分钟内无新增）；`storyboard_director_revisions` 新增 0 条；`composition_recut_approvals_json <> '[]'` **计数 0**；`247462a4` AUTO 仍 RUNNING round 9、`23c59183` 仍 COMPLETED round 8（两者 `updated_at` 早于本次部署，未被打断）；`/health/live` 200、web/worker healthy。
- 回滚：还原 compose 备份后 `up -d --no-build` 回 `sha256:3175dc0b…`（迁移 `0047` 无破坏性，无需降级）。
- 同批补记（PR #8 暗色分隔后备，镜像 `3175dc0b` 已含）：部署机上用新镜像重裁 `Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/evidence/` 的 6 张原图——4 张曾拒收中 3 张通过（`313b41c6` 5 格、`ce321800` 4 格、`e459d954` 6 格），`1a04e778`（灰分隔约 17）仍拒收；2 张曾通过的原图裁切框不变。`247462a4` 现存成功 v2 故事板 53 张全部仍可裁（历史 3218 次 `storyboard_panel_layout_unreadable` 的原图未入库，只能按留存证据复核）。
- 离线标定（`recut_calibration.py`，只读）：342/343 张成功 v2 故事板（6 项目，`247462a4` 53、`2c291aba` 107、`23c59183` 79、`fd3bf04d` 77、`72a22f0a` 14、`484746c4` 13）裁切失败 0；同组不同镜头 2877 对距离 p0 **0.1240**、p1 0.3536、p5 0.5647、p25 0.8138、p50 0.94（均值 0.9164）；同构图相邻 2 对为 0.6947 / 0.9411；**同构图非相邻 0 对**（旧规则下不存在）。阈值由用户看图后决定，本次未选、未写入 `..._MIN_DISTANCE`、未开开关。结果与样图 `Pending issues/2026-11-05/RECUT_CALIBRATION/results/`。

## 41. 2026-11-05 导演停滞治理上线 + 回切开关开启（阈值 0.10，director-stall-20261105）

- 来源：main `aa76022`（合并 PR #12 `1baebef`，含 PR #11 的提交；合并预演树 `2cfac3a6` 与实际合并树逐位相同）。含 USER_DECISIONS §50（末次确定性合并 `adjacent-diversity-merge-v1`、按违规对的重试提示、导演调用日志、认领重试上限 16）、§48 第 4 项阈值 **0.10** 并开启开关、§51 抖动方向；验收 §45。**无迁移**（head 仍 `0047`）、`config.py` 未改、前端未改。
- 门禁（分支 `1baebef` 对 main `746d6bc`）：单元 **2441 passed / 0 failed**（main 2426 / 0，+15 为 PR 新增用例）；集成两侧 **19 failed / 121 passed / 7 skipped**，失败集合逐条相同；ruff 15 条逐字节相同；mypy **12 errors / 7 files** 归一化后逐行相同；`npm run check` 两侧均 **259 passed / 0 failed**（较 §40 记录的 258 多 1，main 侧既有差异，两侧一致）；`tests/unit/test_project_director_workflow.py` 两侧各 **11 passed**（本机 pytest-asyncio 1.4.0，PR 声明其本机缺该依赖未跑，本次已补）。证据 `Pending issues/2026-11-05/evidence/director_stall_followup/gates/`。
- 部署前只读补采（§45 第 6 项，`Pending issues/2026-11-05/evidence/director_stall_followup/`）：附录 SQL A–D 逐字执行（另补 E 取 `owner_id`，工作流 ID 才能拼出）；16 个导演 run 的 Temporal 历史共 **572 次 `advance_project_director`，TimedOut 0、重试 0**，Started→Completed 最大 806.07 s；**1201 s 出现 5 次，全部是 Scheduled→Started（排队等线程）**（`5d25a3a4`×2、`8697ee51`×2、`c80ea8bb`×1，均 1200 s 活动超时边界内），**1232 / 1274 s 未出现**；worker 内 temporalio **1.35.0**，1702 行日志中「线程池 / max_workers / max_concurrent_activities」告警 **0 命中**（写「无」，不能据此断言不存在线程池不足）。
- 构建：干净 worktree `D:/AAA-project/wt-build-pr12` @ `aa76022` → `git archive` 取 `backend index.html app.js styles.css frontend`（1.42 MB gz、516 文件）→ 部署机 `/home/edy/build-pr12-20261105` → 镜像 `sha256:aa73e69215a10edd767d0742eefe196f9867dea7a88fcbe9f4f9bdd5f172810a`（`tubeflow-test:director-stall-20261105`，35 秒，pip 层缓存命中）。镜像内 `application/project_director.py` `287daad5…`、`application/plan_convergence.py` `946171a7…`、`domain/project_director.py` `ae5a0691…`、`adapters/persistence/storyboards/project_director_repository.py` `7c6962fd…`，LF 归一 sha256 与 `aa76022` blob 逐条相同。
- 部署：compose 备份 `runtime/operations/director-stall-20261105/docker-compose.yml.bak`、`.env` 备份 `.env.bak-director-stall-20261105`；`.env` 追加 `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT=true`（第 87 行）与 `..._MIN_DISTANCE=0.10`（第 88 行），一次写两行；`docker compose run --rm --no-deps -T web` 预校验得 `True / 0.1 / True` 后才上线；web/worker/migrate 三处改钉，`up -d --no-build`，T_dep **2026-11-05T13:59:14Z–13:59:46Z**。按 §46.10 未等待既有项目排空。
- 校验：`/health/live` 200（启动约 70 秒），web/worker healthy 且均在新镜像；两容器 `Settings().director_composition_recut is True`、`director_composition_recut_min_distance == 0.1`、`storyboard_single_panel_new_projects is True`；`alembic 0047` 不变；上述 4 个文件哈希在两个运行容器内各核一遍，全部与 blob 相同。
- 重启影响（10 分钟快照 14:10:07Z）：视频尝试被触碰 **25 条**（`f881122d` 10 CREATED + 2 RUNNING、`247462a4` 3 ARCHIVED、`7bc8ddd7` 2 ARCHIVED + 2 CREATED + 1 RUNNING + 1 SUCCEEDED、`ff67c5e9` 1 RUNNING、`7cf2e79d` 1 RUNNING）；FAILED / REMOTE_STATE_UNKNOWN / QC_FAILED / local_integrity_blocked **各 0**；新增故事板 **2 条**（均 `f881122d`、grid-v2：1 条 `aspect_ratio_not_supported` 失败 + 1 条成功）；`storyboard_director_revisions` 新增 0 条；`composition_recut_approvals_json <> '[]'` **0**（全表 4920 行）；`storyboard_recut_panels_too_similar` **0**；窗口内 `project_director_runs` 新增 **0**。
- 60 分钟窗口（同批采集，两窗都在 T+65 分钟一次性读取）：W0 `[12:59:46Z, 13:59:46Z)` 112 个工作流、W1 `[13:59:46Z, 14:59:46Z)` 136 个（枚举口径 `StartTime ∈ [窗口起点−30m, 窗口终点)`，覆盖跨窗开始的工作流）。故事板/合成/封面活动的 `ActivityTaskTimedOut` **两窗均为 0**，`fetch_failures` 0；判据「W1 出现 W0 没有的超时」未触发，**未回滚**。导演日志 `project director call finished` / `unit rejected` / `unit merged` 自 T_dep 起 **各 0 条**，且窗口内导演运行数为 0 ⇒ 本节三项写**「未验证（窗口内无导演运行）」**，不写「无异常」。回切放行数与 `too_similar` 同为 0，同样按**「未验证（窗口内无 grid-v2 AUTO 组进入回切检查）」**记录。
- 回滚方法：还原上述两个备份后 `up -d --no-build`，回到 `sha256:fa167f1b…`；本次无迁移，不需要降级。**本次未触发回滚。**
- 抖动普查（§51，只读，全量 4182 条当前分组视频，T+60 窗口结束后运行）：见 §41 末尾「抖动普查结果」。
- 补记（PR #8，镜像 `sha256:3175dc0b…`）：**部署时刻未采集**；**部署后 10 分钟快照未采集**——当时未记录，且该容器已被本次与 §40 的两次上线替换，无法回溯。仅能确认：该镜像在 §40 中被用作「旧侧」跑完 548 张回归，输出与新镜像逐字节相同。
- PR 状态：本机未安装 `gh`，PR #12 与 #11 需人工在网页关闭（#12 已以 `aa76022` 合入 main）。
- 抖动普查结果（`jitter_survey.py`，只读；`SINCE=2026-10-01`、`LIMIT=4500` 覆盖全量；2026-11-05T15:05:51Z–15:29:47Z，24 分钟）：**4182 条**当前分组视频（53 个项目），测到 **4114** 条，`errors` **0**；其余 68 条 `held_share` 为 `null`，原因是 `moving_windows=0`（静止或过短，没有可测的运动窗口），非测量失败。整体 `held_share` 均值 **0.10**、最大 **1.0**、**≥0.2 共 722 条**（占测到数的 17.6%）。分布：恰为 0 的 2541 条、0–0.2 851、0.2–0.4 325、0.4–0.6 217、0.6–0.8 94、0.8–1.0 86（其中恰为 1.0 的 47 条）。项目均值 ≥0.15 的：`ff67c5e9` 0.455（16/18 条 ≥0.2）、`2c291aba` 0.406（83/107）、`23c59183` 0.367（59/79）、`fd3bf04d` 0.336（53/77）、`72a22f0a` 0.333（10/14）、`765dca11` 0.323（85/147）、`7bc8ddd7` 0.320（17/27）、`484746c4` 0.245（8/13）、`e6e60acd` 0.211（27/78）、`a0e52ea0` 0.154（42/122）。结果 `Pending issues/2026-11-05/JITTER_SURVEY_20261105/results/{survey.jsonl,summary.json}`；口径与限制见该目录 `README.md`。**本节只统计、不设阈值、不触发任何重生成**（A/B 需另行授权 8 次 H3 调用，本次未做）。
- 勘误（2026-11-05 复核，`Pending issues/2026-11-05/DIRECTOR_STALL_FOLLOWUP_REVIEW_20261105.md`）：
  - 「部署前只读补采」一条中，「TimedOut 0、重试 0；1201 s 是排队」不成立：6 个活动的第 1 次尝试因 StartToClose 超时被自动重试（`attempt = 2`），其中 4 个在 PR #10 重启（10:09:53Z）前调度。
  - 「60 分钟窗口」一条的超时判据只计重试耗尽后的 `ActivityTaskTimedOut` 事件，看不到被自动重试吸收的超时，「未回滚」的依据需按 `attempt > 1` 复核。
  - PR #11、#12 已由 GitHub 于 13:58:06Z 自动标记为 merged，不需要人工关闭。

## 42. 2026-11-06 抖动 A/B 实验臂上线并执行首轮 A/B（jitter-ab-20261106）

- 来源：main `f47a5c5`（合并 PR #14 `ba7dbac`，USER_DECISIONS §51 第 3 项、§52）。含 `ManualH3Input.experiment_arm` / `prompt_source_attempt_id`、`derive_cadence_variant_prompt`、`CADENCE_SENTENCE_V1`、策略 `tubeflow_storyboard_v4_single_panel_cadence_v1`、A/B 测量脚本拆为 frames/hf 两段。**无迁移**（head 仍 `0047`）、**不改 `.env`**、**不改前端**。
- 门禁（分支 `ba7dbac` 对 main `28eda2f`，均在测试机执行）：单元 **main 2441 passed / 分支 2472 passed**（+31 = 新增 `test_jitter_cadence_ab.py`，该文件 31 条全过）；集成 **两侧 19 failed / 7 skipped**，失败 nodeid 集合逐条相同（主因 12 条 `KeyError: 'TUBEFLOW_TEST_MINIO_ENDPOINT'`、2 条 `assert '0047' == '0039'`），通过数 121 对 122（+1 = 新增 `test_manual_intent_experiment_arm_is_refused_on_a_composite_group`，收集数 1 且未出现在失败清单）；ruff **15 条逐字节相同**；mypy **12 errors / 7 files** 归一化后逐行相同；`npm run check` 两侧均 **259 passed / 0 failed**。证据 `Pending issues/2026-11-06/evidence/jitter_ab/gates/`。
- 过程说明：分支首次全量单元跑出 1 条 `test_sql_concurrent_failover_supersede_creates_exactly_one_successor` 失败（SQLite 并发既有偶发）；该用例两侧各单跑 3 次全过，分支全量复跑 **2472 passed / 0 failed**。原记录保留在 `gates/branch/unit.log`，未以外推替代。
- 构建：合并提交 `f47a5c5` → `git archive`（1,429,109 字节、543 条目）→ 部署机 `/home/edy/build-pr14-20261106` → 镜像 **`sha256:d488b57e089802b1f521e9ec6bedff569e5a2e37cbf1562757614d3f186333d6`**（`tubeflow-test:jitter-ab-20261106`，51 秒，1,103,305,301 字节）。镜像内 6 个后端文件 sha256 与合并树逐条相同（`domain.py 0849fbe0…`、`templates.py 4f501b9b…`、`compiler.py c9664e98…`、`api.py 3baa8ab2…`、`persistence.py 606d4e70…`、`orchestration.py f7d0d69c…`），迁移含 `0047`，`CADENCE_SENTENCE_V1` 可读。
- §52 等待：**首查即两项为 0**（`project_director_units` 的 `LEASED/SUBMITTING` = 0；运行中工作流 0、其中 pending `advance_project_director` = 0），03:19:0xZ 判定，**等待 0 分钟、只复查 1 次**。
- 部署：compose 备份 `runtime/operations/jitter-ab-20261106/docker-compose.yml.bak`（改前 md5 `92666d38da3826d7cf8051e82fea9f68`）；web/worker/migrate 三处改钉，`grep -c` 新 ID = 3；`.env` **未改**（前后 md5 均 `a02afdc2583a2d4bbde6b2ac7d65f896`）；改钉前用新镜像跑 `Settings()` 预校验得 `single_panel=True / recut=True / min_d=0.1 / strategy=tubeflow_storyboard_v4_natural_motion`。`up -d --no-build`，**T_dep 2026-11-06T03:19:25Z–03:19:38Z**；`migrate` 退出码 0，`alembic 0047` 不变。
- **更正（2026-11-06 复核）**：原记录此处称 web 容器名不是 compose 默认名，该说法有误——该名就是 `<project>-<service>-1` 形式的 compose 默认名（同时也没有保存当时的 `docker ps -a` 原始输出）。该条已删除；2026-11-06 的部署已把前后两份 `docker ps -a` 存入 `Pending issues/2026-11-06/evidence/motion_metrics/deploy/`。
- 校验：`/health/live` 200（web healthy 于 **03:21:08Z**），web/worker 均在新镜像且 healthy；两容器 `Settings()` 与预校验一致；4 个文件哈希在两个容器内各核一遍，全部与合并树相同；`/group-videos/contract` 返回 `{"available":true,...}`。
- 重启影响（10 分钟快照 03:28:28Z 与 03:30:16Z 两次）：被触碰的视频尝试 **4 条，全部是本次实验自身**（2 ARCHIVED + 2 RUNNING）；非实验尝试 **0**；新增故事板 **0**、`storyboard_director_revisions` 新增 **0**、`composition_recut_approvals_json <> '[]'` **0**；**12 个保护项目无任何尝试被触碰**。
- A/B 执行（容器内脚本，非 HTTP；见 `Pending issues/2026-11-06/evidence/jitter_ab/submit/`）：8 条尝试，`484746c4` 第 7、12 组 × seed `798160553724158`/`798160553724159` × 基线/节奏臂，全部 `ARCHIVED`、0 失败、0 重提；H3 **8/8 次**，DeepSeek **4 次**（只编译基线，`video_prompt_compilations` 本项目 0 行）。两组 `group_current_videos` 前后不变，`superseded_attempt_id` 指向这 8 条的记录 0 条。
- A/B 结果（`ab_measure.py`，frames 在部署机 web 容器、hf 在测试机）：4 对中变体整条隔帧停顿占比 **2 对更低、2 对更高**（第 7 组 0.49→0.30、0.53→0.50；第 12 组 0.11→0.32、0.97→1.00）；抖镜头 4 → 3；4 对提示词「去掉这一句后与基线逐字相同」。按事先写定的三条规则，**规则 1 不成立 ⇒ 结论为「无效或不确定」，未把变体设为默认**。目视 8 张接触表无多余人、无可读文字、无分屏/网格泄漏。证据 `Pending issues/2026-11-06/evidence/jitter_ab/ab/results/`（`*.gray.gz` 与 `*.mp4` 未入库）。
- 回滚：还原 compose 备份后 `up -d --no-build`，回到 `sha256:aa73e69215a1…`（director-stall-20261105）；本次无迁移，无需降级。**未触发回滚。**
- 清理：容器内 `/tmp/jitter_survey`、`/tmp/jitter_ab_out`、`/app/submit_step.py` 已删除；部署机 `/home/edy/jitter_ab_out{,.tar}`、`jitter_survey_stage*` 已删除。构建目录 `/home/edy/build-pr14-20261106` 保留。

## 43. 2026-11-06 视频观测指标上线（隔帧停顿占比与 HF，motion-metrics-20261106）

- 来源：main `bbd6f08`（合并 PR #15 `da60ef6`，base `36123e9`，2 个提交、10 个文件 +564/−3）。含 `video_generation/motion_metrics.py`（纯计算）、`adapters/video_motion_metrics.py`（ffmpeg + OpenCV）、归档时写 `probe_evidence.motion_metrics`、`numpy>=2,<3` 与 `opencv-python-headless>=4.10,<6`、Dockerfile 构建期 `RUN python -c "import cv2, numpy"`。**无迁移**（head 仍 `0047`）、**不改 `.env`**、**不改前端**。
- 门禁（**测试机**，分支 `da60ef6` 对 main `36123e9`）：单元 **main 2472 passed / 10 skipped；分支 2481 passed / 11 skipped**（+9 通过、+1 跳过 = 新增 10 条）；`tests/unit/test_video_motion_metrics.py` 单跑 **9 passed, 1 skipped**，跳过的是适配器那条（`tests\unit\test_video_motion_metrics.py:187: ffmpeg not on PATH`）——**按跳过记录，不算通过**；集成两侧 **19 failed / 122 passed / 7 skipped**（各收集 148），失败 nodeid 逐条相同且与上一轮完全一致；ruff **15 条逐字节相同**；mypy **12 errors / 7 files**，归一化后逐行相同（唯一差异「检查源文件数 206→208」= 新增两个模块）；`npm run check` 两侧 259 / 259 / 0。证据 `Pending issues/2026-11-06/evidence/motion_metrics/gates/`。
- 依赖（**测试机**，`pip install -e ".[dev]"`）：`numpy==2.5.4`、`opencv-python-headless==5.0.0.93`；`pip freeze` 前后差异**只有这两项**（外加 editable 指向从 git URL 变为本次工作树），其余包未动。`cv2 5.0.0`。
- 构建（**部署机**）：`git archive` 取 `da60ef6`（1,435,053 字节、546 条目）→ `/home/edy/build-pr15-20261106` → 镜像 **`sha256:287592d9973ee3c69cf11dd348dea7dfb630251d3bb4eb6d923e2f03f6d76f41`**（`tubeflow-test:motion-metrics-20261106`，04:27:48Z–04:28:38Z，50 秒）。构建日志中 **`#16 [12/12] RUN python -c "import cv2, numpy"` → `DONE 0.3s`**。镜像内 `python 3.12.16`、`cv2 5.0.0`、`numpy 2.5.4`；大小 **1,419,211,640 字节**，较 `d488b57e…`（1,103,305,301）**+315,906,339 字节（+28.6%）**。镜像内 4 个文件 sha256 与合并树逐条相同：`motion_metrics.py fcd35702…`、`adapters/video_motion_metrics.py 4a6ddf21…`、`orchestration.py e874b216…`、`factory.py 470c794a…`；迁移仍到 `0047`。
- §52 等待（**部署机**）：`04:42:09Z` 查 `project_director_units` 的 `LEASED/SUBMITTING` = **0**；`04:42:15Z` 查运行中工作流 = **0**、其中 pending `advance_project_director` = **0**。两项均为 0，**等待 0 分钟、只复查 1 次**（探针脚本 `/home/edy/pr14_temporal_check.py`，容器重建后需重新 `docker cp`）。
- 部署：compose 备份 `runtime/operations/motion-metrics-20261106/docker-compose.yml.bak`（改前 md5 `4f192a578cff247d8554fe26659a1df6`）；web/worker/migrate 三处改钉，`grep -c` 新 ID = 3；**`.env` 未改**（前后 md5 均 `a02afdc2583a2d4bbde6b2ac7d65f896`）。`up -d --no-build`，**T_dep 2026-11-06T04:42:22Z–04:42:35Z**；`migrate` 退出码 0，`alembic 0047` 不变。
- 校验：`/health/live` 200（web/worker healthy 于 **04:44:00Z**）；两容器均在新镜像；两容器 `Settings()` 仍为 `single_panel True / recut True / min_d 0.1`，且 `cv2 5.0.0`、`numpy 2.5.4` 可导入；4 个文件哈希在 web 与 worker 内各核一遍，全部与合并树相同；`/group-videos/contract` = `available:true`。
- 重启影响（10 分钟快照 **04:53:02Z**）：被触碰的视频尝试 **0 条**；新增故事板 **0**、`storyboard_director_revisions` 新增 **0**、`composition_recut_approvals_json <> '[]'` **0**；12 个保护项目无任何尝试被触碰；live 200，web/worker `Up 10 minutes (healthy)`。
- 指标复算（**部署机**，只读、无 Provider 调用）：worker 容器内按 `ab_export.py` 的读法从 MinIO 取 `7460885c` 的归档 MP4（3,937,767 字节，sha256 `aad949ca…`，与库内 `output_sha256` 和 `attempts.json` 的 `video_file_sha256` **三者一致**），用 `FfmpegOpenCvMotionExtractor().extract()` + `motion_metrics_evidence(fps=24.0)` 复算：**提取 1.512 秒、计算 0.001 秒**。与 `measure.json` 对照 **21 项全部一致**：`held_share 0.49`、`moving_windows 45`、`held_windows 22`、`hf 0.874`、`jitter_shots 2`，三镜 `t0/t1/moving/held/held_share/hf` 逐项相同（逐镜 HF 差 0.000，判据 ±0.005）。脚本与输出 `Pending issues/2026-11-06/evidence/motion_metrics/recompute/`。
- 遗留三件事：
  - **a) 容器名更正**：`docs/DEPLOYMENT.md` §42 与 `evidence/jitter_ab/deploy/README.md` §4 原有一条「现场偏差」称 web 容器名不是 compose 默认名——该说法有误，`tubeflowai-test-web-1` 正是 `<project>-<service>-1` 的默认名，且当时未保存 `docker ps -a`。两条已删除并各自加更正说明；本次部署前后各留一份 `docker ps -a` 于 `evidence/motion_metrics/deploy/`。
  - **b) 遗留 schema**：`select nspname from pg_namespace where nspname like 'contract\_%'` 命中 **1 个**：`contract_be30c85aac3347ffbf3bc45bc6ea12de`（**空 schema**，0 张表、无 `alembic_version`）。与「fixture 建好 schema 后 `alembic upgrade` 失败、清理未执行」的路径一致（`conftest.py` 的 `try/finally` 只包住 `yield` 之后）。**只列出，未删除**（删除需授权）。
  - **c) 第 7 组第 1 镜是否真实机位突跳**：同一提取器取该镜 `shifts[0:91]`，按 5 帧滑动平均残差排序，前 5 个帧号为 **54 / 68 / 62 / 44 / 66**（残差 21.584 / 15.203 / 9.112 / 9.069 / 8.845；对应位移 (dx,dy) = (−3.9,−6.25) / (4.32,0.67) / (3.26,−0.81) / (4.83,−1.06) / (−1.0,−3.26) 像素）。逐帧导出原分辨率（1056×608）画面并逐对目视：灯、门框、货架边缘、窗外树与远景人物在相邻两帧**位置一致**，只有人物姿态与纸箱在动。结论：**不是真实机位突跳**，是该镜主体占画面比例大、把相位相关带偏造成的测量误差；位移量级 3–7 px 也远小于真实跳变的量级。证据 `evidence/motion_metrics/recompute/shot1/`。
- 自然归档观察：截至 04:53:02Z，`probe_evidence_json ? 'motion_metrics'` 的尝试数为 **0**，且窗口内运行中工作流为 0、无 AUTO 运行——**「上线后第一条正常归档带 status=ok」待自然发生**，未为此触发生成，`FINAL_ACCEPTANCE` §47 该项**未勾**。
- 回滚：还原 compose 备份后 `up -d --no-build`，回到 `sha256:d488b57e0898…`（jitter-ab-20261106）。本次无迁移，无需降级；**未触发回滚**。
