# 部署证据：导演镜头数拒收重试提示（director-shot-count-20261107）

全部时间为**部署机 `date -u`**（两台机器时钟互校一致）。本次操作发生在 2026-11-08 UTC（补丁与文档日期为 2026-11-07）。

## 1. 归档与构建

| 项 | 值 |
| --- | --- |
| 合并提交 | `b44b1d0`（`--no-ff`，父 `b3b244a`） |
| 归档 | `git archive --format=tar.gz HEAD backend index.html app.js styles.css frontend` → **1,443,297 字节 / 552 条目** |
| 构建目录 | `/home/edy/build-director-shot-count-20261107`（**未**在 `/home/edy/tubeflowai-test` 源码树构建） |
| 首次构建 | 15:36:11Z–15:36:15Z **失败**：`pip install` 拉 `setuptools-84.2.1` 时清华源 `pypi.tuna.tsinghua.edu.cn` 返回 **HTTP 403 Forbidden** |
| 处置 | 仅改**构建目录**内 `backend/Dockerfile` 第 17 行镜像源：`https://pypi.tuna.tsinghua.edu.cn/simple` → `https://mirrors.aliyun.com/pypi/simple`（§30 先例；仓库 Dockerfile 未改，原文件留存 `/home/edy/dsc-Dockerfile.orig`）。探针：tuna `403` / aliyun `200` / pypi.org `200` |
| 二次构建 | 15:36:38Z–15:37:37Z（59 s）**成功** |
| 镜像 | **`sha256:79b37e6ce50f344c1e930be0703b2f187bcaa8e56eaf25faa3d3b5d511a062d3`**（`tubeflow-test:director-shot-count-20261107`），**1,418,801,480 字节**（上一版 `287592d9…` 为 1,419,211,640，−410,160 / −0.03%） |
| 构建日志要点 | `#14 pip install` DONE 43.2s、`#16 [12/12] RUN python -c "import cv2, numpy"` **DONE 0.4s**、`#17` DONE 15.1s |

镜像内容核对（`docker run --rm`）：

- `tubeflow_test.application.project_director.__file__` → `/usr/local/lib/python3.12/site-packages/...`，**sha256 `c875e4196cef675965c60c3928279b4c34a1d152aaedaaaf0ff18d0f5d77915a`**，与本仓库工作树文件逐字节相同（git blob 为 LF，哈希 `8b7aa273…`；工作树/归档为 CRLF，故哈希不同，**非内容差异**）。
- `_unit_shot_maximum(8) = 8`、`_unit_shot_maximum(3) = 6`、`_SHOT_COUNT_FAILURE = director_output_shot_count_invalid`、`_shot_count_notice` 存在。
- `/app/migrations/versions` 末尾为 `0047_director_composition_recut.py`。

## 2. §52 等待（改钉与重启前）

脚本 `/home/edy/dsc_s52_check.sh`（读 `project_director_units` 的 `LEASED/SUBMITTING` + 容器内跑 `/home/edy/pr14_temporal_check.py`）。

| 时刻 | `LEASED/SUBMITTING` | 运行中工作流 | pending `advance_project_director` |
| --- | ---: | ---: | ---: |
| 2026-11-08T15:38:02Z | **0** | **0** | **0** |

两项均为 0 ⇒ **等待 0 分钟、只复查 1 次**。另查 `select … from project_director_units where status in ('LEASED','SUBMITTING')` 返回空集。

## 3. 改钉与上线

| 项 | 值 |
| --- | --- |
| compose | `/home/edy/tubeflowai-test/docker-compose.yml` |
| 备份 | `runtime/operations/director-shot-count-20261107/docker-compose.yml.bak` |
| md5 改前 → 改后 | `15052f5e37efb9256bd95ddd0d25d5b5` → `2c075fe70d3343255199b0bd916bf941` |
| 钉位 | 第 6 / 50 / 84 行（web / worker / migrate） |
| `grep -c` | 旧摘要改前 **3** → 改后 **0**；新摘要改后 **3** |
| `.env` | **未改**，md5 前后均 `a02afdc2583a2d4bbde6b2ac7d65f896` |
| 上线 | `docker compose up -d --no-build`，**T_dep 2026-11-08T15:38:14Z–15:38:27Z**（13 s），退出 0 |
| migrate | `tubeflowai-test-migrate-1` **Exited (0)**；`alembic_version` 仍 **`0047`** |

## 4. 部署后校验

| 检查 | 结果 |
| --- | --- |
| web / worker healthy | **2026-11-08T15:40:02Z** 两者 `healthy` |
| 镜像一致 | 两个业务容器均 `79b37e6ce50f`（web `f8d197e2ce90`、worker `0251f36d8406`），已重建 |
| `/health/live` | **200** |
| `/group-videos/contract` | `{"available":true,"status":"AVAILABLE",…}` |
| 容器内代码 | web 与 worker 的 `project_director.py` sha256 **均 `c875e419…`**；`_unit_shot_maximum(8)=8`、`_SHOT_COUNT_FAILURE` 均存在 |
| 容器内配置（两容器相同） | `director_composition_recut=True`、`min_distance=0.1`、`storyboard_single_panel_new_projects=True`、`h3_single_panel_opt_in_enabled=True`、`video_prompt_strategy=tubeflow_storyboard_v4_natural_motion` |

重启影响（两次快照；T_dep = 15:38:14Z）：

| 指标 | 15:41:35Z（+3m21s） | **15:48:42Z（+10m28s）** |
| --- | ---: | ---: |
| 被触碰的视频尝试（`video_generation_attempts.updated_at >= T_dep`） | 0 | **0** |
| 新增故事板 `storyboard_generations` | 0 | **0** |
| 新增 `storyboard_director_revisions` | 0 | **0** |
| `composition_recut_approvals_json <> '[]'` | 0 | **0** |
| `project_director_units` 的 `LEASED/SUBMITTING` | 0 | **0** |
| `/health/live` | 200 | **200** |
| worker 状态 | — | `Up 10 minutes (healthy)` |

保护名单项目：因「被触碰的视频尝试」全库为 **0**，12 个保护项目必然零触碰（未再按 UUID 前缀单独统计——该查询因前缀不是完整 UUID 报错，未重试，以全库 0 为证据）。

## 5. 回滚

还原 `runtime/operations/director-shot-count-20261107/docker-compose.yml.bak` 后 `docker compose up -d --no-build`，回到 `sha256:287592d9973ee3c69cf11dd348dea7dfb630251d3bb4eb6d923e2f03f6d76f41`（motion-metrics-20261106）。本次**无迁移**，无需 `alembic downgrade`。**未触发回滚。**

## 6. 本镜像附带 PR #19

`bf22055`（PR #19）改的 `adapters/persistence/database.py` 随本镜像一起上线；部署机 `.env` 的数据库 URL **不含 `options=`**（C1 第 0 节），该改动的合并逻辑在无 `options=` 时行为等价，**线上行为不变**。

## 7. 未做

未触发生成、未调用任何 Provider、未改 `.env`、未改仓库 `docker-compose.yml` 的钉、未动保护名单项目。构建目录 `/home/edy/build-director-shot-count-20261107` 保留。
