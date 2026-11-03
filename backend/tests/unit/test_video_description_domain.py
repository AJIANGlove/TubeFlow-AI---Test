from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.domain import (
    DEFAULT_DESCRIPTION_PROMPT,
    DescriptionPayload,
    DescriptionProjectSource,
    DescriptionSegment,
    DescriptionTemplate,
    assemble_description,
    build_description_messages,
    description_input_sha256,
    format_description_time,
    parse_description_payload,
    parse_srt,
    validate_description_content,
    validate_template_prompt,
)

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def _srt(count: int = 7) -> bytes:
    blocks = []
    for index in range(1, count + 1):
        start = (index - 1) * 2_000
        end = index * 2_000
        blocks.append(
            f"{index}\n00:00:{start // 1000:02},000 --> 00:00:{end // 1000:02},000\n字幕 {index}"
        )
    return ("\n\n".join(blocks) + "\n").encode()


def _source(content: bytes = _srt()) -> DescriptionProjectSource:
    project_id, channel_id = uuid4(), uuid4()
    return DescriptionProjectSource(
        project_id=project_id,
        owner_id=uuid4(),
        channel_id=channel_id,
        title="测试标题",
        script="冻结文案全文",
        locale="ko-KR",
        channel_name="韩国小生意经济财经",
        channel_url="https://www.youtube.com/@test",
        composition_id=uuid4(),
        srt_asset_id=uuid4(),
        srt_sha256="a" * 64,
        srt_entries=parse_srt(content),
    )


def _template(prompt: str = DEFAULT_DESCRIPTION_PROMPT) -> DescriptionTemplate:
    return DescriptionTemplate(uuid4(), uuid4(), 1, prompt, NOW, NOW)


def _payload() -> dict[str, object]:
    return {
        "hook": "钩子句一。钩子句二。",
        "intro_title": "开场反转",
        "segments": [
            {"entry": 2, "title": "成本拆解"},
            {"entry": 3, "title": "客流结构"},
            {"entry": 4, "title": "固定支出"},
            {"entry": 5, "title": "利润去向"},
            {"entry": 6, "title": "最终答案"},
        ],
        "learn_points": ["要点一", "要点二", "要点三", "要点四", "要点五"],
        "closing": "无论处在哪个阶段，这条视频都适用。",
        "disclaimer_key": "finance",
    }


def test_parse_srt_validates_sequence_timing_and_text() -> None:
    entries = parse_srt(_srt(3))
    assert [(x.sequence, x.start_ms, x.end_ms) for x in entries] == [
        (1, 0, 2000),
        (2, 2000, 4000),
        (3, 4000, 6000),
    ]
    with pytest.raises(DomainError) as invalid:
        parse_srt(b"1\n00:00:00,000 --> 00:00:02,000\n\n")
    assert invalid.value.code == "description_srt_invalid"


def test_template_validation_requires_known_authoritative_placeholders() -> None:
    assert "{{timeline}}" in validate_template_prompt(DEFAULT_DESCRIPTION_PROMPT)
    with pytest.raises(DomainError) as missing:
        validate_template_prompt("只写文案 {{script}}")
    assert missing.value.code == "description_template_placeholder_required"
    with pytest.raises(DomainError) as unknown:
        validate_template_prompt(
            "{{topic}} {{language}} {{script}} {{channel_name}} {{timeline}} {{unknown}}"
        )
    assert unknown.value.code == "description_template_placeholder_unknown"


def test_payload_and_assembly_use_real_srt_times_and_actual_channel_name() -> None:
    source = _source()
    payload = parse_description_payload(_payload(), source.srt_entries)
    content = assemble_description(payload, source)
    assert "0:00 인트로: 开场反转" in content
    assert "0:02 成本拆解" in content
    assert "0:10 最终答案" in content
    assert "🔔 韩国小生意经济财经 채널을" in content
    assert "【频道名】" not in content
    assert "금융, 투자 또는 법률 조언" in content


@pytest.mark.parametrize(
    ("segments", "code"),
    [
        ([{"entry": 1, "title": "重复 Intro"}], "description_output_invalid"),
        ([{"entry": 3, "title": "越界"}] * 5, "description_output_invalid"),
    ],
)
def test_payload_rejects_unusable_timeline_references(
    segments: list[dict[str, object]], code: str
) -> None:
    payload = _payload()
    payload["segments"] = segments
    with pytest.raises(DomainError) as invalid:
        parse_description_payload(payload, _source().srt_entries)
    assert invalid.value.code == code


def test_payload_rejects_timecode_hashtag_and_bad_disclaimer() -> None:
    for key, value in (("hook", "0:00 开场"), ("closing", "#tag 收尾")):
        payload = _payload()
        payload[key] = value
        with pytest.raises(DomainError) as invalid:
            parse_description_payload(payload, _source().srt_entries)
        assert invalid.value.code == "description_output_invalid"
    payload = _payload()
    payload["disclaimer_key"] = "other"
    with pytest.raises(DomainError):
        parse_description_payload(payload, _source().srt_entries)


def test_prompt_messages_and_input_hash_are_stable() -> None:
    source = _source()
    template = _template()
    messages = build_description_messages(template.prompt, source)
    assert messages[0]["role"] == "system"
    assert "韩国小生意经济财经" in messages[1]["content"]
    assert "1 | 00:00:00,000 --> 00:00:02,000 | 字幕 1" in messages[1]["content"]
    repaired = build_description_messages(
        template.prompt, source, repair_reason="entry 越界"
    )
    assert "entry 越界" in repaired[1]["content"]
    assert description_input_sha256(template, source) == description_input_sha256(template, source)


def test_manual_content_validation_and_time_format() -> None:
    assert validate_description_content(" 手动简介\n第二行 ") == "手动简介\n第二行"
    assert format_description_time(0) == "0:00"
    assert format_description_time(61_000) == "1:01"
    assert format_description_time(3_661_000) == "1:01:01"
    with pytest.raises(DomainError):
        validate_description_content(" ")


def test_payload_dataclass_round_trip_shape() -> None:
    payload = DescriptionPayload(
        "hook",
        "intro",
        (DescriptionSegment(2, "章节"),),
        ("要点",),
        "closing",
        "general",
    )
    assert payload.as_dict()["segments"] == [{"entry": 2, "title": "章节"}]
