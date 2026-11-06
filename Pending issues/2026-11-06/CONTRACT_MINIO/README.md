# 测试专用 MinIO（`tubeflow-contract-minio`）

2026-11-06 按 `NEXT_STEPS_IMPLEMENTATION_PLAN_20261106.md` D2 建立，供集成测试使用（12 条用例缺 `TUBEFLOW_TEST_MINIO_*`）。**本记录先建后写，未跑完整集成（等 C1 合并）。**

## 容器

| 项 | 值 |
| --- | --- |
| 名称 | `tubeflow-contract-minio` |
| 镜像 | `minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`（= `docker-compose.yml` 钉死的那个 digest，部署机原有，未拉取） |
| 容器 Id | `e31abb181027442324fd9ac151663566c7c4faaa36d60287526aec9e24c0c547` |
| 启动时刻 | 2026-11-06T08:53:30Z |
| 端口 | 仅 `127.0.0.1:59000 -> 9000`（不发布 9001 控制台） |
| 网络 | 仅默认 `bridge`；**未加入** `tubeflowai-test_default` / `kinestack_control` |
| 数据卷 | `tubeflow-contract-minio-data`（独立卷，2026-11-06T16:53:30+08:00 创建） |
| 重启策略 | `unless-stopped` |
| 账号 | 随机生成，存 `runtime/operations/contract-minio-20261106/credentials.txt`（`runtime/` 已 gitignore，不入库）；部署机副本 `/home/edy/tubeflow-contract-minio.env`（600，`--env-file` 注入，不进命令行） |

## 核对

1. 新实例桶列表为空：`mc ls local` 无输出；python `list_buckets()` → `[]`。
2. 本机经 SSH 隧道 `-L 127.0.0.1:59100:127.0.0.1:59000` 连通：列空 → 建 `connectivity-probe` → 删 → 仍为空（25 ms）。
3. **线上 MinIO 未改动**（前后逐字对比）：

| 对象 | 创建容器前 | 之后 |
| --- | --- | --- |
| `tubeflowai-test-minio-1` Id | `fb4ce984048f43545aa05aac1f76356471a5f8938f357ec32cc48002b3133b15` | 同 |
| 同上 `StartedAt` | `2026-09-30T04:46:21.719067866Z` | 同 |
| 同上挂载 | `tubeflowai-test_minio_data->/data` | 同 |
| 卷 `tubeflowai-test_minio_data` 创建时间 | `2026-09-23T14:38:44+08:00` | 同 |

## 本机 59000 已被占用（运维注意）

建立隧道时发现**测试机本机 59000 已被一个旧的 SSH 转发占用**（PID 17916，指向线上 MinIO），导致首个请求打到线上 MinIO 并返回 `InvalidAccessKeyId`。改用本机 `59100` → 部署机 `59000` 后正常。以后复用隧道时先查 `netstat -ano | grep 59000`。

## 回滚

只删这两个对象：

```
docker rm -f tubeflow-contract-minio
docker volume rm tubeflow-contract-minio-data
rm -f /home/edy/tubeflow-contract-minio.env
```

不触碰 `tubeflowai-test-minio-1` 与卷 `tubeflowai-test_minio_data`。

## 集成测试接法（C1 合并后再跑）

```
ssh -L 127.0.0.1:59100:127.0.0.1:59000 edy@192.168.110.200 -N
TUBEFLOW_TEST_MINIO_ENDPOINT=127.0.0.1:59100
TUBEFLOW_TEST_MINIO_ACCESS_KEY=<credentials.txt 的 MINIO_ROOT_USER>
TUBEFLOW_TEST_MINIO_SECRET_KEY=<credentials.txt 的 MINIO_ROOT_PASSWORD>
```
