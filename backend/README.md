# TubeFlow AI Test 后端

该目录是全新后端控制面的代码根，采用模块化单体结构：

- `domain`：纯领域规则和不可变值对象；
- `application`：用例、端口和事务边界；
- `adapters`：PostgreSQL、MinIO、Temporal、FFmpeg、CosyVoice、图片模型和 KineStack；
- `api`：FastAPI 传输层；
- `workflows`：Temporal 工作流和 Activity 装配。

实施与验收必须遵守仓库根目录的 `AGENTS.md` 和 `docs/planning/` 中的两份已确认文档。

## 本地质量检查

```powershell
python -m ruff check src tests
python -m mypy src
python -m pytest
```

真实 CosyVoice、图片模型和 H3 节点只能由用户明确操作触发，自动化测试必须使用 Fake 或隔离契约服务。
