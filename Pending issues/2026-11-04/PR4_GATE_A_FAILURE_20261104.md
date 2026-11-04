# PR #4 Gate A 验证失败（2026-11-04）

状态：**FAILED，未合并，未部署**。
分支：`claude/single-panel-default-v2`，head `a969e13d3488c4e283d2e7e84b4c280013892197`，base `main` `3af818dd2cd4b99a5afe7e51c19f5954beffe8af`。
PR：`AJIANGlove/TubeFlow-AI---Test#4`，`mergeable_state=clean`，21 文件，+1742/−25，无迁移。

## 1. 执行命令

工作目录 `backend/`，解释器 `C:\Users\15202\.workbuddy\binaries\python\envs\tubeflow-test\Scripts\python.exe`，每次新建 `--basetemp`。

```text
pytest tests/unit -q -p no:cacheprovider --basetemp=<fresh> -rf --tb=short
ruff check src tests
mypy src
```

先跑 main `3af818d`，再切换 `a969e13d` 跑同口径。证据日志：`evidence/pr4_gate_a/`。

## 2. 结果对比

| 检查 | main `3af818d` | PR `a969e13d` | 差异 |
| --- | --- | --- | --- |
| 单元测试 | 5 failed / 2226 passed / 10 skipped / 33 warnings（93.05 s） | 6 failed / 2313 passed / 10 skipped / 33 warnings（95.50 s） | PR 多 1 个失败：`tests/unit/test_storyboard_batch.py::test_generate_skips_succeeded_without_force_and_regenerates_with_force` |
| ruff `src tests` | 15 errors | 15 errors | 错误行完全一致（`diff` 为空） |
| mypy `src` | 12 errors / 7 files（206 files checked） | 13 errors / 8 files（206 files checked） | **PR 新增 1 个 mypy 错误** |

### 2.1 单元测试失败的性质

main 的 5 个失败与 PR 的 6 个失败中，5 个是同一组：

- `test_storyboard_generation.py::test_completion_is_replay_safe_and_current_never_regresses`
- `test_storyboard_generation.py::test_hash_mismatch_checkpoint_survives_crash_and_is_rechecked`
- `test_worker_supervision.py::test_run_worker_stops_components_before_assembly_resources`
- `test_worker_supervision.py::test_run_worker_clears_stale_readiness_before_assembly_failure`
- `test_worker_supervision.py::test_run_worker_publishes_atomically_and_removes_readiness`

其中故事板两项的堆栈为 WorkBuddy 本机安全删除 shim：

```text
src\tubeflow_test\application\storyboards.py:1434: in _cleanup_temp
    path.unlink(missing_ok=True)
D:\dev\WorkBuddy\resources\app.asar.unpacked\cli\vendor\shim\sitecustomize.py:1275: in _safe_path_unlink
...
E   SystemExit: 1
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":50,"threshold":50,...}
```

PR 多出的 `test_generate_skips_succeeded_without_force_and_regenerates_with_force` 在同一分支**单独运行通过**（`1 passed`，`evidence/pr4_gate_a/pr4-one.log`），全量运行时同样触发上述 safe-delete 计数阈值。该失败是测试环境/顺序产物，不是 PR 业务逻辑断言失败；但全量口径下仍计入 PR 的失败集合。

### 2.2 ruff

main 与 PR 的 15 条错误集合完全一致（`diff` 空），PR 未新增 ruff 错误。当前 main 本身并非 clean：9 条 `E501` 以及其他既有规则错误；未修改。

### 2.3 mypy（阻断项）

PR 新增、main 不存在的错误：

```text
src\tubeflow_test\video_generation\panel_images.py:557: error: Argument 1 to "float" has incompatible type "float | tuple[int, ...] | None"; expected "str | Buffer | SupportsFloat | SupportsIndex"  [arg-type]
```

对应 PR 新增代码：

```python
region = luminance.crop(box).resize((1, 1), Image.Resampling.BOX)
return float(region.getpixel((0, 0)))
```

该文件与行由 PR #4 新增；main 同位置无此代码，也无此错误。按任务规则“任一 PR 导致的失败即停止，不自行修复”，Gate A 判定失败。

## 3. Gate A 结论与未执行项

- **未合并 PR #4**；`main` 仍为 `3af818dd2cd4b99a5afe7e51c19f5954beffe8af`，PR 仍 open。
- **未部署**；未修改部署机镜像/`.env`/compose；未产生任何 Provider 调用；未创建新项目。
- **Gate 1–5 全部未执行**：Gate A 在 mypy 新增错误处停止。
- **PostgreSQL 集成测试未运行**：按“发现 PR 导致的失败即停止”规则，在 mypy 阻断后停止；未把集成测试描述为通过。
- 本地工作区已切回 `main`，`git status` 干净；仅新增本报告与 `evidence/pr4_gate_a/` 日志。

## 4. 需要的修复（不由本任务实施）

由 PR 作者修复 `panel_images.py:557` 的类型收窄（例如显式断言/转换灰度像素值），使 `mypy src` 相对 main 不新增错误；随后重新执行 Gate A 全量验证。
