"""封面应用服务共享件：项目上下文端口、勾选维度存储编码、共享错误码。

项目作用域的封面动作只在项目冻结（存在不可变快照，即 snapshot_script 非空）
后开放；三个封面服务（风格/提示词/封面图）共用同一项目事实读取端口。

勾选维度在仓储中的存储形态为 ``{"selected": [扁平勾选 id, ...]}``（值即
`domain.cover.normalize_ref_dims` 的输出，保序、不折叠大类），草稿
``dims``/``analysis_dims``、频道风格版本 ``dims``/``analysis_dims`` 与提示词版本
``style_dims_used`` 统一使用该编码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from tubeflow_test.domain.cover import normalize_ref_dims

# 项目未冻结（无不可变快照）时项目作用域封面动作的稳定失败码。
COVER_PROJECT_NOT_FROZEN = "cover_project_not_frozen"
# 参考约束文本按勾选维度回读失败（dim_line_invalid）的稳定失败码：
# 在计额/模型调用前拒绝。
COVER_REF_TEXT_INVALID = "COVER_REF_TEXT_INVALID"


@dataclass(frozen=True, slots=True)
class CoverProjectContext:
    """封面编排所需的项目事实；frozen_script 为 None 表示项目未冻结。"""

    project_id: UUID
    channel_id: UUID
    title: str
    frozen_script: str | None
    locale: str
    # 项目快照角色图 id（按快照顺序；v5 §1.4 默认角色绑定解析用）。
    snapshot_character_image_ids: tuple[UUID, ...] = ()


class CoverProjectContextPort(Protocol):
    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None: ...


def dims_to_storage(flat: object) -> dict[str, Any]:
    """扁平勾选 id 列表 → 仓储编码 {"selected": [...]}（经 normalize_ref_dims 规整）。"""
    return {"selected": normalize_ref_dims(flat)}


def dims_from_storage(stored: object) -> list[str]:
    """仓储编码 → 扁平勾选 id 列表；非预期形态按空勾选处理（与 normalize 一致）。"""
    selected = stored.get("selected") if isinstance(stored, dict) else None
    return normalize_ref_dims(selected)
