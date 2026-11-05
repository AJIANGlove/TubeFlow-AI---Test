# 部署镜像是否混入未提交改动（2026-11-05）

对象：`sha256:edee542d…`（`tubeflow-test:narration-jobid-20261104`，由 `01ccf01` 构建）。
方法：容器内文件取字节 → LF 归一 → sha256，与 `git show 01ccf01:<path>` 的 LF 归一 sha256 比对。
测试文件（`tests/**`）不入镜像，不参与比对。

## 结果

| 文件 | worker | web | `01ccf01` blob | 判定 |
| --- | --- | --- | --- | --- |
| `/app/frontend/app.js` | `f8106da2…` | `f8106da2…` | `f8106da2…` | 一致 |
| site-packages `api/cover.py` | `bbd4eb98…` | `bbd4eb98…` | `bbd4eb98…` | 一致 |
| site-packages `application/cover_images.py` | `8d3c004c…` | `8d3c004c…` | `8d3c004c…` | 一致 |
| `/app/frontend/frontend/cover-view.js` | `981c44ee…` | `981c44ee…` | `981c44ee…` | 一致 |

完整哈希：

```
app.js                     f8106da2bbc1aab6375b79595d5287c0e4ad1c9ebf12ab0873490c09dc4a2cd9
api/cover.py               bbd4eb982312c5bb32be046b96f9625a69da94231208b31656423acfd5490788
application/cover_images.py 8d3c004c0f252fd332b02a56a93fac0eb3ed23dbf430245ccbac2cd3cffb98e2
frontend/cover-view.js     981c44ee88a2720742bf814f2515b975495708184ccb71aa2afb1f8c0cd787b6
```

## 结论

**不一致文件：0 个。** 镜像 `edee542d…` 不含未提交代码。

原因（事实）：该镜像由 `git archive HEAD` 生成的 tar 包构建，`git archive` 只读对象库中的已提交内容，工作树里的未提交改动不会被读入。工作树当前仍有另一会话的未提交改动（`app.js`、`cover*.py`、其测试、`frontend/cover-view*`、`USER_DECISIONS.md` §34、`COVER_GENERATION_PLAN_V7`），与镜像无关。

## 今后构建口径

一律 `git worktree add <临时目录> <合并提交>`，在干净目录中打包构建，不再从当前工作树取文件。
