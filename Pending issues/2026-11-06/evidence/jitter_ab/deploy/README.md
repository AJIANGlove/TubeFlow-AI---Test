# PR #14 部署记录（jitter-ab-20261106）

时间均为 UTC。**部署机** = `192.168.110.200`；**测试机** = 实施方机器。

## 1. 来源与门禁

- main `f47a5c5`（`git merge --no-ff ba7dbac`，PR #14；base `28eda2f`；4 个提交、17 个文件 +1471/−35）。合并后 `git diff --stat ba7dbac HEAD` 为空。
- 门禁结论与原始输出：`../gates/SUMMARY.md`、`../gates/{main,branch}/`。
- 本 PR **无迁移**（head 仍 `0047`）、**不改 `.env`**、**不改前端**（17 个文件里无 `app.js`/`styles.css`/`index.html`/`frontend/`）。

## 2. 构建（部署机上执行）

| 项 | 值 |
| --- | --- |
| 归档 | 测试机 `git archive --format=tar.gz f47a5c5 backend index.html app.js styles.css frontend` → 1,429,109 字节、543 条目 |
| 构建目录 | 部署机 `/home/edy/build-pr14-20261106`（`build.log` 同目录） |
| 命令 | `sudo docker build -f backend/Dockerfile -t tubeflow-test:jitter-ab-20261106 .` |
| 时间 | 03:17:08Z → 03:17:59Z（51 秒，pip 层缓存命中） |
| 镜像 | `tubeflow-test:jitter-ab-20261106` = `sha256:d488b57e089802b1f521e9ec6bedff569e5a2e37cbf1562757614d3f186333d6`，1,103,305,301 字节 |
| digest 可用性 | `docker run --rm --network none sha256:d488b57e… echo DIGEST_OK` → `DIGEST_OK`（可直接钉） |

镜像内文件与合并树逐文件 sha256 相同（`/usr/local/lib/python3.12/site-packages/tubeflow_test/`）：

| 文件 | sha256 |
| --- | --- |
| `video_prompt/domain.py` | `0849fbe059654f7b06412596b1f481f43c6ec3b7ee12ed6ed7ad32c4faf64f37` |
| `video_prompt/templates.py` | `4f501b9b19eca2b05209f144cf61a4becf3db581a8414c8c41d931ba5bc2ab2c` |
| `video_prompt/compiler.py` | `c9664e98b06473c583ce6b0c09dd7de30a0b7b5415ca948b5497d3c21e6e43d4` |
| `video_generation/api.py` | `3baa8ab23e339f38702dcc9bca89c0136a40404f6ca6f49fe0302280fc3878c5` |
| `video_generation/persistence.py` | `606d4e70b01fae3b7644f1bfd450d9dcd8229e964bceda061e2406a44643e64f` |
| `video_generation/orchestration.py` | `f7d0d69c213decd3772f21fc6c95363810c8dae7385bb08e42201bed74143c70` |

迁移目录含 `0047_director_composition_recut.py`（未新增）；镜像内 `hasattr(domain, "CADENCE_SENTENCE_V1") == True`、策略常量 `tubeflow_storyboard_v4_single_panel_cadence_v1` 可读。

## 3. §52 部署前等待（两项计数）

| 时刻 | `project_director_units` LEASED/SUBMITTING | 运行中工作流 | pending `advance_project_director` | 处置 |
| --- | ---: | ---: | ---: | --- |
| 03:19:0x（首查） | 0 | 0 | 0 | 两项均为 0，无需等待，立即部署 |

- 单元状态分布（同一时刻）：`SUCCEEDED` 7746、`INTENT_PERSISTED` 5413、`FAILED` 117、`REMOTE_STATE_UNKNOWN` 1。
- 等待开始 = 等待结束 = 03:19:0x；**等待 0 分钟**，只做了 1 次复查。
- 计数命令：`docker exec tubeflowai-test-postgres-1 psql -U tubeflow -d tubeflow` + 容器内 `temporalio` 客户端遍历 `ExecutionStatus='Running'` 的 `pending_activities`（脚本 `/home/edy/pr14_temporal_check.py`）。

## 4. 改钉与部署

- compose 备份：`runtime/operations/jitter-ab-20261106/docker-compose.yml.bak`；改前哈希 `docker-compose.yml` `92666d38da3826d7cf8051e82fea9f68`、`.env` `a02afdc2583a2d4bbde6b2ac7d65f896`。
- web / worker / migrate 三处 `image:`（第 6、50、84 行）由 `sha256:aa73e69215a1…` 改为 `sha256:d488b57e0898…`；`grep -c` 新 ID = **3**。
- **`.env` 未改**：部署前后 md5 均为 `a02afdc2583a2d4bbde6b2ac7d65f896`。
- 改钉前 `Settings()` 预校验（`docker compose run --rm --no-deps -T web python -c ...`）：`single_panel_new_projects True`、`recut True`、`recut_min_distance 0.1`、`video_prompt_strategy tubeflow_storyboard_v4_natural_motion`、`h3_configured True`。
- 部署：`docker compose up -d --no-build`，**T_dep 2026-11-06T03:19:25Z – 03:19:38Z**；`migrate` 退出码 0；`alembic_version` = `0047`（未变）。
- **注意（现场偏差）**：web 容器实际名为 `tubeflowai-test-web-1`，不是 compose 默认的 `<project>-web-1`。compose 依 `com.docker.compose.project` + `service=web` 标签识别并**原地重建**了它（日志 `Container tubeflowai-test-web-1 Recreated/Started`），名称保留，8000 端口无冲突、无重复容器。为统一命名而执行的 `docker rename` 被 daemon 拒绝（`Renaming a container with the same name as its current name`），未产生任何变更。

## 5. 部署后校验

| 项 | 结果 |
| --- | --- |
| `/health/live` | 200（首次 200 在 03:20:5x，web healthy 于 **03:21:08Z**） |
| 镜像 | web、worker 均 `sha256:d488b57e0898…`；migrate `Exited (0)` |
| 健康 | web healthy、worker healthy |
| 两容器 `Settings()` | 均为 `single_panel True / recut True / min_d 0.1 / strategy tubeflow_storyboard_v4_natural_motion` |
| 4 个文件哈希（web 与 worker 各一遍） | `domain.py 0849fbe0…`、`api.py 3baa8ab2…`、`persistence.py 606d4e70…`、`orchestration.py f7d0d69c…`，两容器一致且与合并树相同 |
| `/group-videos/contract` | `{"available":true,"status":"AVAILABLE",...}` |
| 前端 | 本次未改前端，未做静态资源校验 |

## 6. 回滚

```bash
cd /home/edy/tubeflowai-test
cp runtime/operations/jitter-ab-20261106/docker-compose.yml.bak docker-compose.yml
sudo docker compose up -d --no-build      # 回到 sha256:aa73e69215a1…（director-stall-20261105）
```

本次无迁移，无需降级；旧镜像仍在部署机。

## 7. 重启影响（10 分钟快照）

两次快照，口径同 DEPLOYMENT §41（`updated_at >= T_dep 起点 03:19:25Z`）：

| 时刻 | 被触碰的视频尝试 | 非实验尝试 | 新增故事板 | 导演修订 | 回切放行 | live |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 03:28:28Z（T+8m50s） | 4 条，**全部是本次实验自身**（2 RUNNING + 1 SUCCEEDED + 1 ARCHIVED，均 `484746c4`） | 0 | 0 | 0 | 0 | 200 |
| 03:30:16Z（T+10m38s） | 4 条，全部是本次实验自身（2 ARCHIVED + 2 RUNNING） | 0 | 0 | 0 | 0 | 200 |

- `error_code` 分布：空（无任何失败）。
- **12 个保护项目**（d725172f、a0e52ea0、2c291aba、23c59183、247462a4、faf27eb5、039e36e3、3206fa93、fd3bf04d、7cf2e79d、f881122d、ff67c5e9）在窗口内**无任何尝试被触碰**（查询返回 0 行）。
- web / worker 均 `Up (healthy)`；`alembic 0047`。
- 结论：本次重启未打断任何既有项目——部署时既无运行中工作流也无在途导演单元（§3）。
