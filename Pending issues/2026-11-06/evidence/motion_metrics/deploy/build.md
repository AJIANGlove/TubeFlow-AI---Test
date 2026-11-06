# 构建与部署原始数字（部署机）

## 构建

| 项 | 值 |
| --- | --- |
| 归档 | `git archive --format=tar.gz da60ef6 backend index.html app.js styles.css frontend` → 1,435,053 字节、546 条目 |
| 构建目录 | `/home/edy/build-pr15-20261106`（`build.log` 同目录） |
| 命令 | `docker build -f backend/Dockerfile -t tubeflow-test:motion-metrics-20261106 .` |
| 时间 | 04:27:48Z → 04:28:38Z（50 秒） |
| 关键步骤 | `#16 [12/12] RUN python -c "import cv2, numpy"` → **`#16 DONE 0.3s`** |
| 镜像 | `sha256:287592d9973ee3c69cf11dd348dea7dfb630251d3bb4eb6d923e2f03f6d76f41`，**1,419,211,640 字节** |
| 对比上一版 | `d488b57e…` = 1,103,305,301 字节 ⇒ **+315,906,339 字节（+28.6%）** |
| 镜像内版本 | `python 3.12.16`、`cv2 5.0.0`、`numpy 2.5.4` |
| 迁移 | `/app/migrations/versions/` 仍到 `0047_director_composition_recut.py` |

镜像内 4 个文件 sha256（与合并树逐条相同）：

| 文件 | sha256 |
| --- | --- |
| `video_generation/motion_metrics.py` | `fcd35702dc90a7dc35515171bcf8ef3e2a8bbb698ec8a9a93f105fad195a6df7` |
| `adapters/video_motion_metrics.py` | `4a6ddf21e3fb41c66f354e305d8ef5291f09b9dfd8adab81d9aa695d177bba8a` |
| `video_generation/orchestration.py` | `e874b216a54bebe6541b82c15c6b86e41c8904452bfe67daed8ee33bfe85326a` |
| `video_generation/factory.py` | `470c794a3e724e9d4e0f771669026d619abf56eaf060a2cefe600fbfa91a773e` |

## 部署

| 项 | 值 |
| --- | --- |
| compose 备份 | `runtime/operations/motion-metrics-20261106/docker-compose.yml.bak` |
| 改前 md5 | compose `4f192a578cff247d8554fe26659a1df6`、`.env` `a02afdc2583a2d4bbde6b2ac7d65f896` |
| 改钉 | web / worker / migrate 三处（第 6、50、84 行）→ `287592d9…`；`grep -c` = 3 |
| `.env` | **未改**，部署后仍 `a02afdc2583a2d4bbde6b2ac7d65f896` |
| T_dep | **2026-11-06T04:42:22Z – 04:42:35Z** |
| migrate | Exited (0) |
| alembic | `0047`（未变） |

## 部署后校验

| 项 | 结果 |
| --- | --- |
| `/health/live` | 200（web/worker healthy 于 04:44:00Z） |
| 镜像 | web 与 worker 均 `sha256:287592d9…` |
| 两容器设置 | `single_panel True / recut True / min_d 0.1`（与部署前一致） |
| 两容器依赖 | `cv2 5.0.0`、`numpy 2.5.4` 可导入 |
| 4 文件哈希 | web 与 worker 各一遍，全部 MATCH |
| `/group-videos/contract` | `{"available":true,"status":"AVAILABLE",...}` |

## 10 分钟快照（04:53:02Z，T_dep 后 10 分 27 秒）

被触碰的视频尝试 **0 条**；新增故事板 **0**；`storyboard_director_revisions` 新增 **0**；
`composition_recut_approvals_json <> '[]'` **0**；`probe_evidence_json ? 'motion_metrics'` **0**；
12 个保护项目无触碰；live 200；web / worker `Up 10 minutes (healthy)`。

## 回滚

```bash
cd /home/edy/tubeflowai-test
cp runtime/operations/motion-metrics-20261106/docker-compose.yml.bak docker-compose.yml
docker compose up -d --no-build      # 回到 sha256:d488b57e0898…（jitter-ab-20261106）
```

本次无迁移，无需降级；旧镜像仍在部署机。**未触发回滚。**
