"""封面图生成 Provider 端口：三个通道的结果类型、错误语义与 Protocol。

照 `application/storyboards.py` 的 GridSheetImageProvider 与
`video_prompt/provider.py` 的模式：Protocol 定义在应用层，HTTP 适配器
（`adapters/cover_ref_analysis.py` / `cover_prompt.py` / `cover_image.py`）
与内存 fake（`adapters/in_memory_cover_providers.py`）实现同一合同。
消息构造/校验/装配全部在 `domain/cover.py`；本层只约定传输级输入输出。

稳定错误码族（适配器以 DomainError 抛出，供应用层落 failure_code）：

- ``cover_llm_provider_http_<status>``：LLM HTTP 状态失败。401/403 与其余
  4xx 为既定不重试（计划 §6）；408/425/429/5xx 为渠道故障——传输层最多
  重试 2 次，仍失败则以 ``CoverLlmChannelFailureError`` 抛出并可换备用模型一次；
- ``cover_llm_provider_timeout`` / ``cover_llm_provider_network``：超时/连接
  错误（渠道故障，同上语义）；
- ``cover_llm_response_invalid``：HTTP 200 但外层或内容非 JSON 对象，同模型
  重发一轮后仍失败（渠道故障）；
- ``cover_image_provider_http_<status>`` / ``cover_image_provider_timeout`` /
  ``cover_image_provider_network``：出图通道一次性失败，不自动重试、不换模型；
- ``cover_image_response_invalid``：出图响应契约不符（非 JSON、缺 data、
  b64_json/url 均缺失或 b64 无法解码）；
- ``cover_image_download_failed``：url 分支下载失败（传输错/HTTP 错/空/超限）；
- ``cover_image_qc_*``：技术质检失败（见 `adapters/cover_image_qc.py`）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tubeflow_test.domain.errors import DomainError

# LLM 消息按 OpenAI chat/completions 契约的映射列表传入；适配器不构造消息。
CoverLlmMessages = Sequence[Mapping[str, Any]]


class CoverLlmChannelFailureError(DomainError):
    """封面 LLM 渠道故障：同 baseURL/凭据仅换备用模型重发一次可能有意义。"""


@dataclass(frozen=True, slots=True)
class CoverLlmResult:
    """LLM 通道一次成功调用的结果：解析出的 JSON 对象与回执。

    model 为实际产出结果的模型（主模型或 fallback 切换后的备用模型），供
    生成版本记录；raw_response_sha256 为原始响应字节哈希（审计用）。
    """

    payload: dict[str, Any]
    receipt: str
    model: str
    raw_response_sha256: str


@dataclass(frozen=True, slots=True)
class CoverImageResult:
    """出图通道一次成功调用的结果：图片字节、其 sha256 与回执。

    source 标记字节来源（"b64_json" 直接解码 / "url" 下载）；sha256 在两种
    分支下都对最终字节计算，供应用层落 expected_sha256 并由 QC 复核。
    """

    content: bytes
    sha256: str
    receipt: str
    source: str


class CoverRefAnalysisProvider(Protocol):
    """参考图视觉解析通道（temperature 固定 0.1，由适配器封装）。"""

    def analyze(self, messages: CoverLlmMessages) -> CoverLlmResult: ...

    def close(self) -> None: ...


class CoverPromptProvider(Protocol):
    """封面文本通道：文字生成/画面两版/整改/评委四处复用，温度由调用方传入。"""

    def complete(self, messages: CoverLlmMessages, *, temperature: float) -> CoverLlmResult: ...

    def close(self) -> None: ...


class CoverImageProvider(Protocol):
    """封面出图通道：同步请求-响应，不自动换模型、不自动重试。"""

    def generate(self, *, prompt: str) -> CoverImageResult: ...

    def close(self) -> None: ...


class CoverAnchorImageProvider(Protocol):
    """封面锚图出图通道（v6）：参考图随任务以图像条件传入。

    同步封装"上传参考图 → 提交 → 轮询 → 下载"（grsai 异步任务）；任何失败
    抛封面既有稳定码族（DomainError），服务落 FAILED、不自动重提。
    """

    def generate(self, *, prompt: str, references: tuple[bytes, ...]) -> CoverImageResult: ...

    def close(self) -> None: ...


class CoverImageReviewProvider(Protocol):
    """出图后视觉复审通道：复用参考图解析同一视觉模型配置，消息由 domain 构造。"""

    def review(self, messages: CoverLlmMessages) -> CoverLlmResult: ...

    def close(self) -> None: ...


class CoverImageQc(Protocol):
    """封面图技术质检端口（照 `application/storyboards.py` 的 StoryboardTechnicalQc）。

    返回 (sha256, width, height, mime, byte_size)；失败抛 DomainError 稳定码。
    """

    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]: ...

    def make_thumbnail(self, path: Path, *, width: int, height: int) -> bytes:
        """生成复审用缩略图 PNG 字节（图像处理属本端口，domain 不依赖图像库）。"""
        ...

    def inspect_and_normalize(
        self, path: Path, expected_sha256: str
    ) -> tuple[str, str, int, int, str, int]:
        """锚图通道 QC：校验原始下载字节（哈希一致/可解码/格式），再归一化为
        2560x1440 写回 path，最后对归一化产物执行既有 QC。
        返回 (原始 sha256, 归一化 sha256, 宽, 高, mime, 归一化字节数)。
        失败抛 DomainError 稳定码（cover_image_qc_*）。"""
