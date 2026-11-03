# MANUAL H3 源故事板媒体行锁自等待

## 状态

已修复、已部署；真实生产 opt-in 首轮已成功越过原失败点并完成 `ARCHIVED／COMPLETE`。

## 影响范围

仅隔离项目 `dd4f88ef-bbe4-5ee4-ac00-bede0e5a507f` 的 MANUAL H3 意图创建。修复不改变提示词、参考图、默认策略、其他项目、TTS、故事板模型或 UNKNOWN 恢复规则。

## 现象与证据

两次真实创建均在本地数据库阶段失败，未生成 attempt ID，也未触发 Provider 提交。错误为：

```text
psycopg.errors.LockNotAvailable: canceling statement due to lock timeout
while locking tuple (3758,14) in relation "media_assets"
asset_id=70a94791-0d01-58d0-be79-909f611a7aba
```

该资产为源 `STORYBOARD_GRID`。失败后核验试点 attempt、outbox、Provider audit 均为 0，因此两次均未消耗 H3 提交额度。

## 根因

`ManualH3IntentWriter._create_once` 先在外层事务中锁定源故事板媒体行，随后真实 `SqlObjectVideoAssetStore.read_verified` 使用另一会话对同一行执行 `FOR UPDATE`。第二个会话等待第一个会话持有的锁，而第一个事务必须等待读取返回后才能继续，形成自等待。

新增 PostgreSQL 回归用例在修复前精确复现 `LockNotAvailable`，修复后同一用例通过。测试使用独立 `contract_*` schema，并在结束后清理，不修改业务 schema。

## 最小修复

只调整 `_create_once` 内的顺序：

1. 在源故事板媒体行锁之前完成真实 source loader／资产读取和输入校验。
2. 保留原媒体行锁、current generation 校验、原子 binding、节点校验及所有事务守卫。
3. 不延长锁超时，不绕过编排，不伪造成功状态。

提交：`fed58d8`（`Fix manual H3 source media lock ordering`），分支 `feature/single-panel-opt-in`。提交仅包含：

- `backend/src/tubeflow_test/video_generation/persistence.py`
- `backend/tests/integration/test_h3_manual_media_lock.py`

工作区既有的 `update` 导入、`reopen_failed_items`、`oldest_waiting_job_id` 及其他配音／AUTO 修改均未纳入。

## 验证结果

- 新增 PostgreSQL 用例：修复前 `1 failed`，修复后 `1 passed`。
- 相关单元、手动绑定、单格 opt-in、假 Engine PostgreSQL 回归：`122 passed`。
- 已提交内容的干净完整副本：后端全量单元 `2160 passed, 0 failed, 10 skipped`；跳过均为本机 FFmpeg 未配置，未计为通过。
- 干净副本 Ruff：通过。
- 干净副本严格 mypy：在未修改的 `identity_project/repository.py` 第 1288、1306、1322 行报告三处既有缺注解；未修复基线同样失败。未将该检查描述为通过，本次目标文件未出现新增报错。
- 最终暂存差异检查通过，只有两个目标文件。

## 部署与回滚

- 新镜像：`tubeflow-test:single-panel-lock-fix-20261103`
- 镜像 ID：`sha256:705f9ddf3f76fd49b3220da16dd784164b2438e6c4f97f71f93faf75892a9347`
- 基底：现役 `d27c90e4`，只叠加已提交的 `persistence.py`；未带入其他未提交文件。
- 镜像内修复文件 SHA-256：`8edf51b96e1855cec9a0de36016260eaf0fa844d8cb2a97cad86f995236263fd`。
- 部署前活动视频、AUTO RUNNING、未决批次、Temporal running 均为 0。
- 部署后 web／worker healthy，`/health/live` 返回 `{"status":"ok"}`，迁移保持 `0046`，allowlist 仍只有试点项目。
- compose 备份：`/home/edy/tubeflowai-test/docker-compose.yml.bak-h3-lock-fix-20261103-114446`。
- 回滚镜像：`sha256:d27c90e4c2da86184aeeddc3b462181ab6e7d8028a0a871c046e818df7ef7a1c`。

## 结论边界

锁缺陷已消除，但这只证明生产意图可正常创建和归档，不等于三次视觉复验全部通过，也不构成扩大 allowlist 或设为默认策略的依据。
