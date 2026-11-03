"""H3 输入门禁的冻结音色判定（§23.1：EL 无公共音色）。

真值表覆盖：provider 缺失/非法、EL 有/无克隆音色、CosyVoice 有/无公共音色。
不依赖数据库：直接构造 ProjectRow 实例即可（SQLAlchemy 允许未落库构造）。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.production_authority import (
    _frozen_voice_missing,
    _snapshot_tts_provider,
)

VOICE_VERSION = UUID("11111111-1111-1111-1111-111111111111")
CLONED_VOICE = UUID("22222222-2222-2222-2222-222222222222")


def _project(**overrides: object) -> ProjectRow:
    row = ProjectRow(title="gate-probe", locale="en-US", aspect_ratio="9:16")
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


@pytest.mark.parametrize(
    ("provider", "voice_version", "cloned_voice", "expected_missing"),
    [
        # CosyVoice：公共音色是唯一冻结音色
        ("cosyvoice", VOICE_VERSION, None, False),
        ("cosyvoice", None, None, True),
        ("cosyvoice", None, CLONED_VOICE, True),
        # EL：克隆音色是唯一冻结音色，公共音色为空是正常态
        ("elevenlabs", None, CLONED_VOICE, False),
        ("elevenlabs", None, None, True),
        ("elevenlabs", VOICE_VERSION, None, True),
    ],
)
def test_frozen_voice_gate_follows_snapshot_provider(
    provider: str, voice_version: UUID | None, cloned_voice: UUID | None, expected_missing: bool
) -> None:
    row = _project(
        tts_provider=provider,
        snapshot_tts_provider=provider,
        snapshot_voice_version_id=voice_version,
        snapshot_cloned_voice_id=cloned_voice,
    )

    assert _frozen_voice_missing(row) is expected_missing


def test_frozen_voice_gate_falls_back_to_project_provider_for_legacy_snapshots() -> None:
    """未写入 snapshot_tts_provider 的历史快照按项目配音源归一。"""

    assert _snapshot_tts_provider(_project(tts_provider="cosyvoice")) == "cosyvoice"
    assert _snapshot_tts_provider(_project(tts_provider="elevenlabs")) == "elevenlabs"
    legacy = _project(tts_provider="cosyvoice", snapshot_voice_version_id=VOICE_VERSION)
    assert _frozen_voice_missing(legacy) is False
    el_without_snapshot_marker = _project(
        tts_provider="elevenlabs", snapshot_cloned_voice_id=CLONED_VOICE
    )
    assert _frozen_voice_missing(el_without_snapshot_marker) is False


@pytest.mark.parametrize("provider", ["", "unknown-provider"])
def test_frozen_voice_gate_fails_closed_for_invalid_provider(provider: str) -> None:
    row = _project(
        tts_provider=provider,
        snapshot_tts_provider=provider,
        snapshot_voice_version_id=VOICE_VERSION,
        snapshot_cloned_voice_id=CLONED_VOICE,
    )

    assert _snapshot_tts_provider(row) is None
    assert _frozen_voice_missing(row) is True
