# 部署环境 H3 配置（只读取证）

采集时间：2026-11-04T04:31Z–2026-11-04T04:33Z（UTC）
采集对象：部署机 `edy-ThinkCentre-M760e-N000`（192.168.110.200），项目目录 `/home/edy/tubeflowai-test`。
约束：只读；未启动/停止/重建任何容器；所有 token 仅记录存在性与长度，不记录值。

## 1. 容器与镜像

`docker compose -p tubeflowai-test ps` 实测：

| 服务 | 容器名 | 镜像 | 状态 |
| --- | --- | --- | --- |
| web | `tubeflowai-test-web-1` | `sha256:8278db3ed79d93f24d3e7f644884022d3926649790f7bff18f85cc2fdd4e0567` | Up 4 hours (healthy) |
| worker | `tubeflowai-test-worker-1` | `sha256:8278db3ed79d93f24d3e7f644884022d3926649790f7bff18f85cc2fdd4e0567` | Up 4 hours (healthy) |
| postgres | `tubeflowai-test-postgres-1` | `postgres:17-alpine` | Up 5 weeks (healthy) |
| minio | `tubeflowai-test-minio-1` | `minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` | Up 5 weeks (healthy) |
| temporal | `tubeflowai-test-temporal-1` | `temporalio/auto-setup:1.27` | Up 5 weeks (healthy) |
| mihomo | `tubeflowai-test-mihomo-1` | `metacubex/mihomo@sha256:bac1a74de365ea59270ba134016762c6be05654d55246518de256113bd1c225e` | Up 17 hours (healthy) |

H3 引擎容器：

| 容器名 | 镜像 ID | 状态 |
| --- | --- | --- |
| `kinestack-h3-clones-h3-engine-new-1-1` | `sha256:88c0df224d3a95808ae961142b4bc8588ca6c6ead71ab8a4895e2c811d0aa801` | Up 11 days (healthy) |
| `kinestack-h3-clones-h3-engine-new-2-1` | `sha256:88c0df224d3a95808ae961142b4bc8588ca6c6ead71ab8a4895e2c811d0aa801` | Up 11 days (healthy) |
| `kinestack-h3-clones-h3-engine-new-3-1` | `sha256:88c0df224d3a95808ae961142b4bc8588ca6c6ead71ab8a4895e2c811d0aa801` | Up 11 days (healthy) |

镜像标签：`kinestack-h3-clones-base:20260919` 的 ID 为上述 `88c0df22…`；`kinestack-h3-clones:hd-storyboard-20261020` 的 ID 为 `sha256:8f396a271b1c62aa04835ef62c951384742f6b13e63cfb890b625abbb50276a1`，当前三个运行容器均使用 `88c0df22…`。legacy 引擎容器（`h3-engine` / `h3-engine-2` / `h3-engine-3`）在 `docker ps -a` 中不存在。

## 2. 部署 `.env` 白名单值

以下为 `/home/edy/tubeflowai-test/.env` 中 H3 相关键的原文（未包含任何 TOKEN 行）：

```text
TUBEFLOW_H3_NODE_URLS=["http://h3-engine:8000","http://h3-engine-2:8000","http://h3-engine-3:8000"]
TUBEFLOW_H3_NODE_IDS=["fee2aa26-6994-4854-890f-47e711f0bd22","cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1","5baad724-6e27-4a14-84c6-4be4607a0bb5"]
TUBEFLOW_H3_NODE_INSTANCE_IDS=["kinestack-h3-engine-1","kinestack-h3-engine-2-1","kinestack-h3-engine-3-1"]
TUBEFLOW_H3_NODE_CAPACITIES=[2,2,2]
TUBEFLOW_H3_WORKFLOW_ID=h3.community.reference-images-to-video
TUBEFLOW_H3_WORKFLOW_VERSION=1.1.0-mvp
TUBEFLOW_H3_WORKFLOW_MANIFEST_SHA256=a8b0461398da3c6a585279e9af0f154caadb853b3d1d99154b08ef2b0ae86e30
TUBEFLOW_H3_RETIRED_NODE_IDS='["fee2aa26-6994-4854-890f-47e711f0bd22","cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1","5baad724-6e27-4a14-84c6-4be4607a0bb5"]'
TUBEFLOW_H3_DEFAULT_WORKFLOW_PROFILE=candidate
TUBEFLOW_H3_CANDIDATE_NODE_IDS='["h3-new-1","h3-new-2","h3-new-3"]'
TUBEFLOW_H3_CANDIDATE_NODE_URLS='["http://h3-engine-new-1:8000","http://h3-engine-new-2:8000","http://h3-engine-new-3:8000"]'
TUBEFLOW_H3_CANDIDATE_NODE_INSTANCE_IDS='["8kdu1z040qlsjb9v","ob6kefgwgyqkpy33","2cop10ldfhkcmhwx"]'
TUBEFLOW_H3_CANDIDATE_NODE_CAPACITIES='[2,2,2]'
TUBEFLOW_H3_CANDIDATE_WORKFLOW_ID=h3.tubeflow.storyboard-to-video
TUBEFLOW_H3_CANDIDATE_WORKFLOW_VERSION=1.0.0
TUBEFLOW_H3_CANDIDATE_WORKFLOW_MANIFEST_SHA256=65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9
TUBEFLOW_H3_CANDIDATE_PROJECT_IDS='[]'
TUBEFLOW_H3_SINGLE_PANEL_OPT_IN_ENABLED=true
TUBEFLOW_H3_SINGLE_PANEL_PROJECT_IDS=["dd4f88ef-bbe4-5ee4-ac00-bede0e5a507f"]
```

TOKEN 行仅记录存在性：

```text
TUBEFLOW_H3_NODE_TOKENS            defined=yes  length=202
TUBEFLOW_H3_CANDIDATE_NODE_TOKENS  defined=yes  length=204
```

说明：上述长度是 `.env` 中 JSON 数组文本的长度；运行容器内解析后的单个 token 长度为 64（见第 3 节）。

## 3. 运行容器内 `Settings` 解析值

在 web 容器 `tubeflowai-test-web-1` 内加载 `tubeflow_test.config.Settings`，仅输出非敏感字段：

```json
{
  "default_workflow_profile": "candidate",
  "candidate_project_allowlist": [],
  "single_panel_opt_in_enabled": true,
  "single_panel_project_ids": [
    "dd4f88ef-bbe4-5ee4-ac00-bede0e5a507f"
  ],
  "retired_node_ids": [
    "fee2aa26-6994-4854-890f-47e711f0bd22",
    "cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1",
    "5baad724-6e27-4a14-84c6-4be4607a0bb5"
  ],
  "pools": [
    {
      "profile": "legacy",
      "workflow_id": "h3.community.reference-images-to-video",
      "workflow_version": "1.1.0-mvp",
      "manifest_sha256": "a8b0461398da3c6a585279e9af0f154caadb853b3d1d99154b08ef2b0ae86e30",
      "model_key": "minimax-h3-community",
      "model_version": "2026-08-13-community-duration-v1",
      "nodes": [
        {"node_id": "fee2aa26-6994-4854-890f-47e711f0bd22", "url": "http://h3-engine:8000", "instance": "kinestack-h3-engine-1", "capacity": 2, "token_present": true, "token_length": 64},
        {"node_id": "cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1", "url": "http://h3-engine-2:8000", "instance": "kinestack-h3-engine-2-1", "capacity": 2, "token_present": true, "token_length": 64},
        {"node_id": "5baad724-6e27-4a14-84c6-4be4607a0bb5", "url": "http://h3-engine-3:8000", "instance": "kinestack-h3-engine-3-1", "capacity": 2, "token_present": true, "token_length": 64}
      ]
    },
    {
      "profile": "candidate",
      "workflow_id": "h3.tubeflow.storyboard-to-video",
      "workflow_version": "1.0.0",
      "manifest_sha256": "65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9",
      "model_key": "minimax-h3-community",
      "model_version": "2026-08-13-community-duration-v1",
      "nodes": [
        {"node_id": "h3-new-1", "url": "http://h3-engine-new-1:8000", "instance": "8kdu1z040qlsjb9v", "capacity": 2, "token_present": true, "token_length": 64},
        {"node_id": "h3-new-2", "url": "http://h3-engine-new-2:8000", "instance": "ob6kefgwgyqkpy33", "capacity": 2, "token_present": true, "token_length": 64},
        {"node_id": "h3-new-3", "url": "http://h3-engine-new-3:8000", "instance": "2cop10ldfhkcmhwx", "capacity": 2, "token_present": true, "token_length": 64}
      ]
    }
  ]
}
```

## 4. `h3_engine_nodes` 表（只读）

查询列：`node_id | base_url | instance_identity | capacity | claimed_slots | enabled | health | workflow_id | workflow_version | manifest_sha256 | token_hash_present`。

```text
5baad724-6e27-4a14-84c6-4be4607a0bb5|http://h3-engine-3:8000|kinestack-h3-engine-3-1|2|0|f|OFFLINE|h3.community.reference-images-to-video|1.1.0-mvp|a8b0461398da3c6a585279e9af0f154caadb853b3d1d99154b08ef2b0ae86e30|t
cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1|http://h3-engine-2:8000|kinestack-h3-engine-2-1|2|0|f|OFFLINE|h3.community.reference-images-to-video|1.1.0-mvp|a8b0461398da3c6a585279e9af0f154caadb853b3d1d99154b08ef2b0ae86e30|t
fee2aa26-6994-4854-890f-47e711f0bd22|http://h3-engine:8000|kinestack-h3-engine-1|2|0|f|OFFLINE|h3.community.reference-images-to-video|1.1.0-mvp|a8b0461398da3c6a585279e9af0f154caadb853b3d1d99154b08ef2b0ae86e30|t
h3-new-1|http://h3-engine-new-1:8000|8kdu1z040qlsjb9v|2|0|t|OFFLINE|h3.tubeflow.storyboard-to-video|1.0.0|65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9|t
h3-new-2|http://h3-engine-new-2:8000|ob6kefgwgyqkpy33|2|2|t|READY|h3.tubeflow.storyboard-to-video|1.0.0|65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9|t
h3-new-3|http://h3-engine-new-3:8000|2cop10ldfhkcmhwx|2|2|t|READY|h3.tubeflow.storyboard-to-video|1.0.0|65bf745305377b97d167b191f5e61a25ecd8142e236768e062c768ce7bd249e9|t
```

## 5. 引擎侧配置

三个引擎容器的环境变量读取结果：

```text
kinestack-h3-clones-h3-engine-new-1-1  H3_WORKFLOW_PROFILE=candidate-hd  ENGINE_TOKEN present length=64
kinestack-h3-clones-h3-engine-new-2-1  H3_WORKFLOW_PROFILE=candidate-hd  ENGINE_TOKEN present length=64
kinestack-h3-clones-h3-engine-new-3-1  H3_WORKFLOW_PROFILE=candidate-hd  ENGINE_TOKEN present length=64
```

`/home/edy/KineStackData/config/h3-clones.env` 中未找到以 `H3_WORKFLOW_PROFILE=` 开头的行（`grep '^H3_WORKFLOW_PROFILE='` 无输出）；运行容器内的上述值来自容器实际环境。

## 6. `/capabilities` 自报摘要（原始响应见 `capabilities_<node>.json`）

| 节点 | profile | HTTP | 响应 item 数 | `h3.tubeflow.storyboard-to-video` |
| --- | --- | --- | ---: | --- |
| `h3-new-1` | candidate | 200 | 3 | 1.0.0 / manifest `65bf7453…` / AVAILABLE |
| `h3-new-2` | candidate | 200 | 3 | 1.0.0 / manifest `65bf7453…` / AVAILABLE |
| `h3-new-3` | candidate | 200 | 3 | 1.0.0 / manifest `65bf7453…` / AVAILABLE |
| `fee2aa26-6994-4854-890f-47e711f0bd22` | legacy | 无响应 | 0 | `probe_error: URLError: [Errno -3] Temporary failure in name resolution` |
| `cb883e07-5f2b-4aaf-bc9e-e86f52cbaad1` | legacy | 无响应 | 0 | 同上 |
| `5baad724-6e27-4a14-84c6-4be4607a0bb5` | legacy | 无响应 | 0 | 同上 |

candidate 三台的 `/capabilities` 还各返回 `h3.kinestack.text-to-video-hd@1.0.1`、`h3.kinestack.reference-images-to-video-hd@1.0.1`、`h3.tubeflow.storyboard-to-video@1.0.0` 三项，状态均为 `AVAILABLE`。

## 7. 事实边界

- `TUBEFLOW_H3_DEFAULT_WORKFLOW_PROFILE` 的部署值为 `candidate`；候选项目 allowlist 为空数组 `[]`。
- legacy 池 workflow 为 `h3.community.reference-images-to-video@1.1.0-mvp`；candidate 池 workflow 为 `h3.tubeflow.storyboard-to-video@1.0.0`。
- `h3_engine_nodes` 中 legacy 三台 `enabled=false`、`health=OFFLINE`；candidate `h3-new-1` 为 `OFFLINE`，`h3-new-2` / `h3-new-3` 为 `READY` 且 `claimed_slots=2`。
- legacy 引擎容器不存在于当前 Docker 主机；其 `/capabilities` 探测在 `kinestack_control` 网络内因名称解析失败而无 HTTP 响应。
