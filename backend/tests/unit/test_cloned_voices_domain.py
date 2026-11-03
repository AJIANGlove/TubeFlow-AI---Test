"""§23 克隆音色领域规则单元测试：删除保护三类引用、默认门禁、清理选择器、槽位判定。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.cloned_voices import (
    ClonedVoiceUpload,
    CloneOutcome,
    cleanup_selection,
    ensure_unique_name,
    normalize_cloned_voice_name,
    normalize_cloned_voice_remark,
    slot_available,
    source_suffix,
)
from tubeflow_test.identity_project.domain import (
    ClonedVoice,
    ClonedVoiceUsage,
    TtsProvider,
    channel_default_voice_gate,
    cloned_voice_cleanup_candidates,
    cloned_voice_delete_guard,
    normalized_tts_provider,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _voice(name: str = "旁白", *, created_at: datetime = NOW) -> ClonedVoice:
    return ClonedVoice(
        id=uuid4(),
        channel_id=uuid4(),
        owner_id=uuid4(),
        name=name,
        remark="",
        source_object_key=f"cloned-voices/{name}/source.wav",
        source_sha256="a" * 64,
        source_size_bytes=128,
        source_media_type="audio/wav",
        created_at=created_at,
    )


def test_delete_guard_protects_frozen_snapshot_but_allows_unreferenced() -> None:
    cloned_voice_delete_guard(ClonedVoiceUsage())
    with pytest.raises(DomainError) as captured:
        cloned_voice_delete_guard(ClonedVoiceUsage(frozen_snapshot_references=1))
    assert captured.value.code == "cloned_voice_frozen_snapshot"


def test_delete_guard_reports_draft_and_channel_default_with_distinct_codes() -> None:
    with pytest.raises(DomainError) as draft:
        cloned_voice_delete_guard(ClonedVoiceUsage(draft_references=2))
    assert draft.value.code == "cloned_voice_draft_reference"
    with pytest.raises(DomainError) as default:
        cloned_voice_delete_guard(
            ClonedVoiceUsage(draft_references=1, channel_default_references=1)
        )
    assert default.value.code == "cloned_voice_channel_default"


def test_cleanup_selector_skips_protected_and_orders_oldest_first() -> None:
    oldest, middle, newest, protected = (
        _voice("oldest", created_at=NOW - timedelta(days=3)),
        _voice("middle", created_at=NOW - timedelta(days=2)),
        _voice("newest", created_at=NOW - timedelta(days=1)),
        _voice("protected", created_at=NOW - timedelta(days=9)),
    )
    items = (
        (newest, ClonedVoiceUsage(), newest.created_at),
        (protected, ClonedVoiceUsage(channel_default_references=1), protected.created_at),
        (oldest, ClonedVoiceUsage(), oldest.created_at),
        (middle, ClonedVoiceUsage(draft_references=1), middle.created_at),
    )
    assert cloned_voice_cleanup_candidates(items, limit=5) == (oldest, newest)
    assert cloned_voice_cleanup_candidates(items, limit=1) == (oldest,)
    assert cloned_voice_cleanup_candidates(items, limit=0) == ()


def test_channel_default_gate_requires_the_matching_voice_kind() -> None:
    voice_version_id, cloned_voice_id = uuid4(), uuid4()
    channel_default_voice_gate(TtsProvider.COSYVOICE.value, voice_version_id, None)
    channel_default_voice_gate(TtsProvider.ELEVENLABS.value, None, cloned_voice_id)
    with pytest.raises(DomainError) as cosyvoice:
        channel_default_voice_gate(TtsProvider.COSYVOICE.value, None, cloned_voice_id)
    assert cosyvoice.value.code == "channel_default_voice_required"
    with pytest.raises(DomainError) as elevenlabs:
        channel_default_voice_gate(TtsProvider.ELEVENLABS.value, voice_version_id, None)
    assert elevenlabs.value.code == "channel_default_voice_required"


def test_provider_normalization_rejects_unknown_values() -> None:
    assert normalized_tts_provider("elevenlabs") == "elevenlabs"
    assert normalized_tts_provider(TtsProvider.COSYVOICE) == "cosyvoice"
    with pytest.raises(DomainError) as captured:
        normalized_tts_provider("azure")
    assert captured.value.code == "invalid_tts_provider"


def test_name_and_remark_validation_and_uniqueness() -> None:
    assert normalize_cloned_voice_name("  旁白  ") == "旁白"
    assert normalize_cloned_voice_remark("  备注  ") == "备注"
    with pytest.raises(DomainError) as empty:
        normalize_cloned_voice_name("   ")
    assert empty.value.code == "invalid_cloned_voice_name"
    with pytest.raises(DomainError) as long_remark:
        normalize_cloned_voice_remark("x" * 2001)
    assert long_remark.value.code == "invalid_cloned_voice_remark"
    ensure_unique_name("旁白二", (_voice("旁白"),))
    with pytest.raises(DomainError) as conflict:
        ensure_unique_name("旁白", (_voice("旁白"),))
    assert conflict.value.code == "cloned_voice_name_conflict"


def test_source_suffix_whitelist_falls_back_to_wav() -> None:
    assert source_suffix("sample.MP3") == ".mp3"
    assert source_suffix("voice.flac") == ".flac"
    assert source_suffix("voice") == ".wav"
    assert source_suffix("voice.xyz") == ".wav"


def test_slot_availability_matrix() -> None:
    assert slot_available(3, 30) is True
    assert slot_available(30, 30) is False
    assert slot_available(None, 30) is None
    assert slot_available(3, None) is None
    assert slot_available(3, 0) is None


def test_cleanup_selection_only_targets_unreferenced_voices_present_in_the_account() -> None:
    free = _voice("free", created_at=NOW - timedelta(days=2))
    used = _voice("used", created_at=NOW - timedelta(days=3))
    absent = _voice("absent", created_at=NOW - timedelta(days=4))
    freshly_cloned = _voice("fresh", created_at=NOW - timedelta(days=5))
    mappings = (
        (free.id, "el-free", free, ClonedVoiceUsage()),
        (used.id, "el-used", used, ClonedVoiceUsage(draft_references=1)),
        (absent.id, "el-absent", absent, ClonedVoiceUsage()),
        (freshly_cloned.id, "el-fresh", freshly_cloned, ClonedVoiceUsage()),
    )
    selection = cleanup_selection(
        account_voice_ids=("el-free", "el-used", "el-fresh"),
        mappings=mappings,
        keep_el_voice_ids=frozenset({"el-fresh"}),
        needed=3,
    )
    # 受保护（used）、远端不存在（absent）、本次新建（fresh）都不在清理范围内。
    assert selection == ((free.id, "el-free"),)
    assert (
        cleanup_selection(
            account_voice_ids=("el-free",),
            mappings=mappings,
            keep_el_voice_ids=frozenset(),
            needed=0,
        )
        == ()
    )


def test_clone_outcome_failure_projection() -> None:
    key_id = uuid4()
    ok = CloneOutcome(key_id, "主号", True, el_voice_id="el-1")
    failed = CloneOutcome(key_id, "备号", False, error_code="provider_rejected")
    assert ClonedVoiceUpload("n", "", "a.wav", b"x", "audio/wav").name == "n"
    assert ok.ok and ok.el_voice_id == "el-1"
    assert not failed.ok and failed.error_code == "provider_rejected"
