# 交接包 v1 部署证据 — 2026-11-10（部署机）

- 来源提交：main `a9732d7`（`--no-ff` 合并 `claude/motion-naturalness-v1-20261109`，父 `528bc29`）。
- 镜像：`sha256:7b5056d4a7d66130614bbfd0244f97175684af82abec3dc26166611b85a68c0c`（`tubeflow-test:motion-edit-arm-20261109`，05:26:34Z–05:28:17Z，103 s）。
- §52：05:28:35Z 首查即 `leased_submitting=0 / running_workflows=0 / pending_advance_project_director=0`，等待 0 分钟、复查 1 次。
- T_dep：05:28:51Z–05:29:04Z；`migrate` 退出 0；`alembic 0047`；`.env` 前后 md5 `a02afdc2583a2d4bbde6b2ac7d65f896`。
- 健康：`/health/live` 200（05:30:27Z 起）、web/worker healthy 05:30:37Z、`/group-videos/contract` `available:true`。
- 10 分钟快照（05:39:10Z）：被触碰尝试 0、新增故事板 0、导演修订 0、重剪批准 0、12 个保护项目 0；web/worker `Up 10 minutes (healthy)`。
- 原始材料：`deploy-artifacts.txt`（改前/改后 `docker ps -a`、`up.log`、构建日志尾部）。

## 回滚（未触发）

还原 `/home/edy/runtime-operations/motion-edit-arm-20261109/docker-compose.yml.bak` 后 `docker compose up -d --no-build`，回到 `sha256:79b37e6ce50f…`（director-shot-count-20261107）。无迁移，无需降级。
