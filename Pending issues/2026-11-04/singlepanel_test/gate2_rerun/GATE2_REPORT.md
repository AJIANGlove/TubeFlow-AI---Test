# Gate 2 — 开关关闭部署与 30 分钟观察（2026-11-04）

镜像 `sha256:238bd9ead5f33a2f186c838210da1ec13da93f59ef857b05e11fdd3d8b468b57`
（`tubeflow-test:crop-grid-v2-rev-20261104`，合并后 main `1707d85`）。
开关 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=false` 写入部署机 `.env`，对
web / worker / migrate 全部 backend 服务生效。无迁移。

## 1. 部署

| 项 | 值 |
| --- | --- |
| 部署窗口 | 2026-11-04 17:45:08–17:45:40（+0800） |
| 命令 | `docker compose up -d --no-build`（`/home/edy/tubeflowai-test`） |
| compose 备份 | `runtime/operations/crop-grid-v2-rev-20261104/docker-compose.yml.bak` |
| `.env` 备份 | `.env.bak-single-panel-off-20261104`（改前 sha256 `c9b13c50…`） |
| 三处 image | web / worker / migrate 均为 `sha256:238bd9ea…` |
| 附带重建 | `.env` 变更使 `mihomo` 一并重建（healthy） |

部署后校验：

- `docker compose ps`：web / worker / minio / postgres / temporal / mihomo 全部 healthy；`migrate` Exited(0)。
- web 启动约 2.5 分钟：17:45:40 起 `Waiting for application startup`，17:48:23 起 healthy；启动期 `/health/live` 返回 000，之后 200（17:48 起持续 200，T+30 复核仍 200）。
- `alembic_version` = `0046`（不变）。
- web 与 worker 容器内 `Settings().storyboard_single_panel_new_projects` 均为 `False`；`env` 实读 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=false`。
- 两容器 `docker inspect` 的镜像 ID 均为 `sha256:238bd9ea…`。

## 2. 观察（T0 = 2026-11-04T09:45:08Z，T+30 = 10:15:24Z）

| 时点 | v2 键 | 故事板行 | 视频尝试行 | 非终态视频 | 在途故事板 |
| --- | ---: | ---: | ---: | ---: | ---: |
| T-0（09:44:57，部署前） | 0 | 5482 | 4383 | 97 | 10 |
| T+4（09:48:33） | 0 | 5482 | 4385 | 98 | 10 |
| T+10（09:55:44） | 0 | 5482 | 4389 | 98 | 10 |
| T+20（10:05:47） | 0 | 5482 | 4394 | 99 | 10 |
| T+30（10:15:24） | 0 | 5482 | 4398 | 99 | 10 |

逐组比对（`gate2_compare.py`，`gate2_diff.json`）：

| 指标 | 值 |
| --- | ---: |
| 组数（前后） | 4348 / 4348 |
| 部署前已有当前成片的组 | 4129 |
| 其中计数器发生变化 | **0** |
| 其中丢失当前成片 | **0** |
| 新增组 | 0 |
| 全部变化组 | 15（全部为部署前未完成的组） |

变化的 15 组：`a0e52ea0` 13 组各 +1 次尝试（0→1，首轮尝试）；`d725172f` 2 组，
`e75f27ff` attempt_rows 2→3 且 intent_max 0→1、`1702cdeb` 1→2。

窗口内故事板：新增 0 条，带 `grid_layout_version` 的新增 0 条，`input_json` 含该键的全库仍为 0 条。

## 3. 结论（按 Step 8 的两条判据）

1. 既有项目的故事板没有 `input_json.grid_layout_version`：成立（全库 0，部署前后一致）。
2. 已完成组没有新的故事板或视频尝试：成立（4129 个已完成的组计数器全不变）。

Gate 2 通过。开关未开启，未创建测试项目，第 3–5 关未执行。

## 4. 观察期内的外部变化（非本次部署动作，如实记录）

- `d725172f` 的 AUTO 在 17:45 之后由 PAUSED（round 9，`video_failed:intent_rejected`）
  变为 RUNNING（round 10）；即被外部恢复了一次付费授权轮次。本次部署未调用任何
  resume/pause 接口，也未触碰该项目。
- 该项目的 1 次 ARCHIVED、1 次 RUNNING、1 次 SUCCEEDED 尝试状态变化与上述恢复同源，
  与部署无关；其所在组在 T0 时都没有当前成片。
- `a0e52ea0` 自 T0 起一直为 RUNNING（round 5，phase video），窗口内的 12 次 ARCHIVED、
  5 次 CREATED、3 次 RUNNING 尝试变化来自它自身的 AUTO 推进。
