from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class H3NodePool:
    profile: str
    node_urls: tuple[str, ...]
    node_ids: tuple[str, ...]
    node_instance_ids: tuple[str, ...]
    node_tokens: tuple[str, ...]
    node_capacities: tuple[int, ...]
    node_gpu_status_urls: tuple[str, ...]
    model_key: str
    model_version: str
    workflow_id: str
    workflow_version: str
    manifest_sha256: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TUBEFLOW_", extra="ignore")

    database_url: str = "postgresql+psycopg://tubeflow:tubeflow@postgres/tubeflow"
    identity_setup_username: str | None = None
    identity_setup_password: str | None = None
    session_secret: str = Field(default="development-only-change-me-32-bytes", min_length=32)
    allowed_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    cookie_secure: bool = False
    frontend_dir: Path | None = None
    public_voice_catalog_path: Path | None = None
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "tubeflow"
    minio_secret_key: str = "development-only-change-me"
    minio_bucket: str = "tubeflow-private"
    temporal_target: str = "temporal:7233"
    narration_task_queue: str = "tubeflow-narration"
    h3_task_queue: str = "tubeflow-h3-video"
    worker_ready_file: str = "/tmp/tubeflow-worker-ready"
    cosyvoice_url: str = "http://cosyvoice:8000"
    cosyvoice_token: SecretStr = SecretStr("")
    cosyvoice_max_concurrency: int = Field(default=4, ge=1, le=4)
    elevenlabs_api_base: str = "https://api.elevenlabs.io"
    elevenlabs_proxy: str = "http://mihomo:7890"
    elevenlabs_tts_model: str = "eleven_multilingual_v2"
    elevenlabs_per_key_concurrency: int = Field(default=4, ge=1, le=5)
    elevenlabs_duration_guard_seconds: int = Field(default=60, ge=10, le=600)
    # §35：voice_settings 默认 None = 不发送（EL 按音色存储默认值合成，行为与既有版本一致）；
    # 仅在显式配置后经 env 注入（v4 裸跑：stability/similarity_boost=0.5、speaker_boost=true）。
    # 回退 v2 时清空这三项 env 即恢复原行为，不得给它们设置非 None 的代码默认值。
    elevenlabs_stability: float | None = Field(default=None, ge=0.0, le=1.0)
    elevenlabs_similarity_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    elevenlabs_use_speaker_boost: bool | None = None
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    ffmpeg_version: str = "configured-at-deploy"
    storyboard_provider_url: str = "http://storyboard-provider:8080"
    storyboard_provider_token: str = ""
    storyboard_provider_model: str = "gpt-image-2.5"
    storyboard_provider_kind: str = "contract"
    storyboard_pix_provider_url: str = "https://api.lk888.ai"
    storyboard_pix_provider_token: SecretStr = SecretStr("")
    storyboard_pix_provider_timeout_seconds: float = Field(default=120.0, gt=0)
    storyboard_provider_max_concurrency: int = Field(default=20, ge=1, le=64)
    storyboard_provider_timeout_seconds: float = Field(default=30.0, gt=0)
    storyboard_provider_quality: str = "high"
    director_provider_url: str = "https://grsai.dakka.com.cn/v1"
    director_provider_token: SecretStr = SecretStr("")
    director_provider_model: str = "gpt-5.6-sol"
    # 导演单次调用预算：与 PROJECT_DIRECTOR_UNIT_LEASE、DIRECTOR_CALL_ACTIVITY_TIMEOUT 联动
    # 不变式：HTTP 超时 < 租约 ≤ 活动超时。
    # 记录见 runtime/operations/director_timeout_fix_plan_20261013.md。
    director_provider_timeout_seconds: float = Field(default=900.0, gt=0)
    video_prompt_provider_url: str = "https://api.deepseek.com/v1"
    video_prompt_provider_token: SecretStr = SecretStr("")
    video_prompt_provider_model: str = "deepseek-v4-flash"
    video_prompt_provider_timeout_seconds: float = Field(default=45.0, gt=0)
    # 2026-11-03 A/B（Pending issues/2026-11-03/VIDEO_QUALITY_AB_RESULTS_20261103.md）后默认 v4。
    video_prompt_strategy: str = "tubeflow_storyboard_v4_natural_motion"
    h3_single_panel_opt_in_enabled: bool = False
    h3_single_panel_project_ids: tuple[UUID, ...] = ()
    # USER_DECISIONS §46：尚无任何故事板的项目在首张故事板时钉网格 v2 + 逐格参考
    # （crop-grid-v2 + v4 单格提示词）。只影响新钉版项目；已钉项目不随开关变化。
    storyboard_single_panel_new_projects: bool = False
    sentence_split_provider_url: str = "https://api.deepseek.com/v1"
    sentence_split_provider_token: SecretStr = SecretStr("")
    sentence_split_provider_model: str = "deepseek-v4-flash"
    sentence_split_provider_timeout_seconds: float = Field(default=45.0, gt=0)
    sentence_split_estimated_seconds_threshold: float = Field(default=12.0, gt=0, le=15.0)
    cover_analysis_provider_url: str = "https://grsai.dakka.com.cn/v1"
    cover_analysis_provider_token: SecretStr = SecretStr("")
    cover_analysis_provider_model: str = "gpt-5.5"
    cover_analysis_provider_fallback_model: str = "gemini-3.1-pro"
    cover_analysis_provider_timeout_seconds: float = Field(default=120.0, gt=0)
    cover_prompt_provider_url: str = "https://api.deepseek.com/v1"
    cover_prompt_provider_token: SecretStr = SecretStr("")
    cover_prompt_provider_model: str = "deepseek-v4-flash"
    cover_prompt_provider_fallback_model: str = "deepseek-v4-pro"
    cover_prompt_provider_timeout_seconds: float = Field(default=300.0, gt=0)
    cover_image_provider_url: str = "https://api.lingkeai.ai/v1"
    cover_image_provider_token: SecretStr = SecretStr("")
    cover_image_provider_model: str = "gpt-image-2"
    cover_image_provider_size: str = "2560x1440"
    cover_image_provider_quality: str = "high"
    cover_image_provider_timeout_seconds: float = Field(default=180.0, gt=0)
    # 出图后视觉复审开关（2026-10-15 用户确认默认开）：关闭则完全跳过复审，不新增调用。
    cover_image_review_enabled: bool = True
    # v6 锚图通道（§18.13）：风格档案带参考图时经 grsai 垫图出图；复用故事板 grsai 配置。
    cover_image_anchor_enabled: bool = True
    cover_image_anchor_model: str = "nano-banana-pro"
    media_command_timeout_seconds: float = Field(default=3600.0, gt=0)
    # 合成 DISPATCHED 工作流失联判定时长（秒）：须大于活动 start_to_close（30min）+重试余量。
    composition_stale_reconcile_seconds: float = Field(default=2100.0, gt=0)
    director_parallelism: int = Field(default=4, ge=1, le=16)
    h3_node_urls: tuple[str, ...] = ()
    h3_node_ids: tuple[str, ...] = ()
    h3_node_instance_ids: tuple[str, ...] = ()
    h3_node_tokens: tuple[str, ...] = ()
    h3_node_capacities: tuple[int, ...] = ()
    h3_node_gpu_status_urls: tuple[str, ...] = ()
    h3_gpu_agent_token: SecretStr = SecretStr("")
    h3_model_key: str = "minimax-h3-community"
    h3_model_version: str = "2026-08-13-community-duration-v1"
    h3_workflow_id: str = "h3.community.reference-images-to-video"
    h3_workflow_version: str = "1.1.0-mvp"
    h3_workflow_manifest_sha256: str = ""
    h3_candidate_node_urls: tuple[str, ...] = ()
    h3_candidate_node_ids: tuple[str, ...] = ()
    h3_candidate_node_instance_ids: tuple[str, ...] = ()
    h3_candidate_node_tokens: tuple[str, ...] = ()
    h3_candidate_node_capacities: tuple[int, ...] = ()
    h3_candidate_node_gpu_status_urls: tuple[str, ...] = ()
    h3_candidate_workflow_id: str = "h3.tubeflow.storyboard-to-video"
    h3_candidate_workflow_version: str = "1.0.0"
    h3_candidate_workflow_manifest_sha256: str = ""
    h3_candidate_project_ids: tuple[UUID, ...] = ()
    h3_default_workflow_profile: str = "legacy"
    h3_retired_node_ids: tuple[str, ...] = ()
    h3_stall_accepted_seconds: float = Field(default=1800.0, gt=0)
    h3_stall_running_seconds: float = Field(default=3600.0, gt=0)
    h3_stalled_poll_seconds: float = Field(default=300.0, gt=0)
    # §15：AUTO 编排器的巡检间隔（重启安全网；踢点失败时兜底推进）。
    auto_pipeline_sweep_interval_seconds: float = Field(default=20.0, gt=0)

    @property
    def h3_node_pools(self) -> tuple[H3NodePool, ...]:
        return (
            H3NodePool(
                "legacy", self.h3_node_urls, self.h3_node_ids, self.h3_node_instance_ids,
                self.h3_node_tokens, self.h3_node_capacities, self.h3_node_gpu_status_urls,
                self.h3_model_key, self.h3_model_version, self.h3_workflow_id,
                self.h3_workflow_version, self.h3_workflow_manifest_sha256,
            ),
            H3NodePool(
                "candidate", self.h3_candidate_node_urls, self.h3_candidate_node_ids,
                self.h3_candidate_node_instance_ids, self.h3_candidate_node_tokens,
                self.h3_candidate_node_capacities, self.h3_candidate_node_gpu_status_urls,
                self.h3_model_key, self.h3_model_version, self.h3_candidate_workflow_id,
                self.h3_candidate_workflow_version, self.h3_candidate_workflow_manifest_sha256,
            ),
        )

    @property
    def h3_configured(self) -> bool:
        return any(pool.node_urls for pool in self.h3_node_pools)

    @property
    def storyboard_pix_provider_configured(self) -> bool:
        return bool(self.storyboard_pix_provider_token.get_secret_value().strip())

    @property
    def director_provider_configured(self) -> bool:
        return bool(self.director_provider_token.get_secret_value().strip())

    @property
    def video_prompt_provider_configured(self) -> bool:
        return bool(self.video_prompt_provider_token.get_secret_value().strip())

    @property
    def sentence_split_provider_configured(self) -> bool:
        return bool(self.sentence_split_provider_token.get_secret_value().strip())

    @property
    def cover_analysis_provider_configured(self) -> bool:
        return bool(self.cover_analysis_provider_token.get_secret_value().strip())

    @property
    def cover_prompt_provider_configured(self) -> bool:
        return bool(self.cover_prompt_provider_token.get_secret_value().strip())

    @property
    def cover_image_provider_configured(self) -> bool:
        return bool(self.cover_image_provider_token.get_secret_value().strip())

    @property
    def h3_gpu_probe_configured(self) -> bool:
        """GPU 探测需要共享 token 且至少一个节点配置了探测端点；否则一律 fail-closed。"""
        return bool(self.h3_gpu_agent_token.get_secret_value().strip()) and any(
            url.strip() for pool in self.h3_node_pools for url in pool.node_gpu_status_urls
        )

    @model_validator(mode="after")
    def validate_h3_nodes(self) -> Settings:
        if self.video_prompt_strategy not in {
            "kinestack_v6_port",
            "tubeflow_enhanced_v1",
            "tubeflow_storyboard_v2",
            "tubeflow_storyboard_v3",
            "tubeflow_storyboard_v3_single_panel_opt_in_v1",
            "tubeflow_storyboard_v4_natural_motion",
        }:
            raise ValueError("video prompt strategy must be a known template version")
        if self.h3_single_panel_project_ids and not self.h3_single_panel_opt_in_enabled:
            raise ValueError("single-panel project allowlist requires the opt-in switch")
        if self.h3_single_panel_opt_in_enabled and not self.h3_single_panel_project_ids:
            raise ValueError("single-panel opt-in requires an explicit project allowlist")
        if self.storyboard_provider_quality not in {"auto", "low", "medium", "high"}:
            raise ValueError("Storyboard Provider quality must be auto, low, medium, or high")
        if self.storyboard_pix_provider_configured:
            parsed_pix_url = urlsplit(self.storyboard_pix_provider_url.strip())
            if (
                parsed_pix_url.scheme != "https"
                or parsed_pix_url.hostname != "api.lk888.ai"
                or parsed_pix_url.path not in {"", "/"}
            ):
                raise ValueError("Storyboard Pix Provider URL must be https://api.lk888.ai")
        if self.video_prompt_provider_configured:
            parsed_video_prompt_url = urlsplit(self.video_prompt_provider_url.strip())
            if (
                parsed_video_prompt_url.scheme not in {"http", "https"}
                or not parsed_video_prompt_url.netloc
            ):
                raise ValueError(
                    "Video prompt Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.video_prompt_provider_model.strip():
                raise ValueError("Video prompt Provider model is required when enabled")
        if self.sentence_split_provider_configured:
            parsed_sentence_split_url = urlsplit(self.sentence_split_provider_url.strip())
            if (
                parsed_sentence_split_url.scheme not in {"http", "https"}
                or not parsed_sentence_split_url.netloc
            ):
                raise ValueError(
                    "Sentence split Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.sentence_split_provider_model.strip():
                raise ValueError("Sentence split Provider model is required when enabled")
        if (
            self.cosyvoice_url.startswith("https://")
            and not self.cosyvoice_token.get_secret_value()
        ):
            raise ValueError("CosyVoice token is required for an HTTPS provider")
        parsed_elevenlabs_api_base = urlsplit(self.elevenlabs_api_base.strip())
        if (
            parsed_elevenlabs_api_base.scheme not in {"http", "https"}
            or not parsed_elevenlabs_api_base.netloc
        ):
            raise ValueError("ElevenLabs API base must be an absolute HTTP(S) URL")
        elevenlabs_proxy = self.elevenlabs_proxy.strip()
        if elevenlabs_proxy and not elevenlabs_proxy.startswith("http://"):
            # 只接受 HTTP(S) 代理：httpx 的 socks5 需要未安装的 `socksio` 依赖，
            # 允许该 scheme 会在建连时才失败（mihomo 默认 7890 即 mixed HTTP 端口）。
            raise ValueError("ElevenLabs proxy must be empty or an http:// URL")
        if self.director_provider_configured:
            parsed_director_url = urlsplit(self.director_provider_url.strip())
            if (
                parsed_director_url.scheme not in {"http", "https"}
                or not parsed_director_url.netloc
            ):
                raise ValueError(
                    "Director Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.director_provider_model.strip():
                raise ValueError("Director Provider model is required when enabled")
        if self.cover_analysis_provider_configured:
            parsed_cover_analysis_url = urlsplit(self.cover_analysis_provider_url.strip())
            if (
                parsed_cover_analysis_url.scheme not in {"http", "https"}
                or not parsed_cover_analysis_url.netloc
            ):
                raise ValueError(
                    "Cover analysis Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.cover_analysis_provider_model.strip():
                raise ValueError("Cover analysis Provider model is required when enabled")
        if self.cover_prompt_provider_configured:
            parsed_cover_prompt_url = urlsplit(self.cover_prompt_provider_url.strip())
            if (
                parsed_cover_prompt_url.scheme not in {"http", "https"}
                or not parsed_cover_prompt_url.netloc
            ):
                raise ValueError(
                    "Cover prompt Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.cover_prompt_provider_model.strip():
                raise ValueError("Cover prompt Provider model is required when enabled")
        if self.cover_image_provider_configured:
            parsed_cover_image_url = urlsplit(self.cover_image_provider_url.strip())
            if (
                parsed_cover_image_url.scheme not in {"http", "https"}
                or not parsed_cover_image_url.netloc
            ):
                raise ValueError(
                    "Cover image Provider URL must be an absolute HTTP(S) URL when enabled"
                )
            if not self.cover_image_provider_model.strip():
                raise ValueError("Cover image Provider model is required when enabled")
        node_ids: list[str] = []
        instance_ids: list[str] = []
        for pool in self.h3_node_pools:
            sizes = {
                len(pool.node_urls), len(pool.node_ids), len(pool.node_instance_ids),
                len(pool.node_tokens), len(pool.node_capacities),
            }
            if len(sizes) != 1:
                raise ValueError("H3 node ID, instance, URL, token, and capacity lists must align")
            node_ids.extend(pool.node_ids)
            instance_ids.extend(pool.node_instance_ids)
            if any(capacity < 1 for capacity in pool.node_capacities):
                raise ValueError("H3 capacities must be positive")
            if pool.node_gpu_status_urls:
                if len(pool.node_gpu_status_urls) != len(pool.node_urls):
                    raise ValueError("H3 node GPU status URLs must align with configured H3 nodes")
                for url in pool.node_gpu_status_urls:
                    if not url.strip():
                        continue
                    parsed = urlsplit(url.strip())
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise ValueError("H3 node GPU status URLs must be absolute HTTP(S) URLs")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("H3 node IDs must be unique and stable across pools")
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("H3 instance identities must be unique and stable across pools")
        for pool in self.h3_node_pools:
            if pool.node_urls and (
                len(pool.manifest_sha256) != 64
                or any(value not in "0123456789abcdef" for value in pool.manifest_sha256)
            ):
                raise ValueError("H3 raw workflow manifest sha256 is required")
        approved_storyboard_workflows = {
            "h3.tubeflow.storyboard-to-video",
            "h3.tubeflow.storyboard-to-video-hd",
        }
        if self.h3_candidate_node_urls and (
            self.h3_candidate_workflow_id not in approved_storyboard_workflows
            or self.h3_candidate_workflow_version not in {"1.0.0", "1.0.1"}
        ):
            raise ValueError("H3 candidate workflow must be an approved storyboard version")
        if self.h3_default_workflow_profile not in {"legacy", "candidate"}:
            raise ValueError("H3 default workflow profile must be legacy or candidate")
        if not set(self.h3_retired_node_ids).issubset(self.h3_node_ids):
            raise ValueError("H3 retired node IDs must belong to the configured legacy pool")
        if (
            self.h3_candidate_project_ids or self.h3_default_workflow_profile == "candidate"
        ) and not self.h3_candidate_node_urls:
            raise ValueError("H3 candidate projects require configured candidate nodes")
        return self
