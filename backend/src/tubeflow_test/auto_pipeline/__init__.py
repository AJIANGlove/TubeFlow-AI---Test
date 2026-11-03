"""AUTO 项目一键自动生产编排器（§15）。

包内聚：domain 为纯状态机与稳定原因码；ports 声明编排器所需的权威事实与
既有阶段服务端口；service 只做幂等推进（advance）、暂停/恢复（resume）与
安全踢点（kick/sweep）。领域层不依赖 FastAPI、ORM、Temporal 或 Provider SDK。
"""

from .domain import (
    AUTO_PIPELINE_PHASES,
    REASON_COMPOSITION_FAILED,
    REASON_DIRECTOR_PREPLAN_FAILED,
    REASON_DIRECTOR_VALIDATION_FAILED,
    REASON_OVERSIZED_SENTENCE,
    AutoPipelineRun,
    AutoPipelineState,
    storyboard_failed_reason,
    video_failed_reason,
)
from .service import AutoPipelineService

__all__ = [
    "AUTO_PIPELINE_PHASES",
    "AutoPipelineRun",
    "AutoPipelineService",
    "AutoPipelineState",
    "REASON_COMPOSITION_FAILED",
    "REASON_DIRECTOR_PREPLAN_FAILED",
    "REASON_DIRECTOR_VALIDATION_FAILED",
    "REASON_OVERSIZED_SENTENCE",
    "storyboard_failed_reason",
    "video_failed_reason",
]
