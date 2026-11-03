"""出图后视觉复审 domain 测试：消息构造与结果校验（纯函数，零 I/O）。"""

from __future__ import annotations

import base64

from tubeflow_test.domain.cover import (
    build_cover_image_review_messages,
    validate_cover_image_review,
)


def _b64(data: bytes = b"\xff\xd8\xff-jpeg") -> str:
    return base64.b64encode(data).decode("ascii")


def test_review_messages_full_context_and_two_images() -> None:
    messages = build_cover_image_review_messages(
        title="标题甲",
        cover_text="FIRST PERIOD?",
        style_text="风格：艺术流派：平涂卡通",
        image_base64=_b64(),
        thumbnail_base64=_b64(b"thumb"),
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "blocker" in messages[0]["content"]
    content = messages[1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "image_url"]
    text = content[0]["text"]
    assert "【标题】\n标题甲" in text
    assert "FIRST PERIOD?" in text
    assert "平涂卡通" in text
    assert "168×94" in text
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")  # 无魔数默认 png


def test_review_messages_empty_optional_blocks() -> None:
    messages = build_cover_image_review_messages(
        title="",
        cover_text="",
        style_text="  ",
        image_base64=_b64(),
        thumbnail_base64=_b64(),
    )
    text = messages[1]["content"][0]["text"]
    assert "【标题】" not in text
    assert "【封面文字" not in text
    assert "【风格既定描述】" not in text
    assert "无风格描述时跳过本项" in text


def test_validate_review_ok_computed_locally_not_model_claim() -> None:
    validated = validate_cover_image_review(
        {
            "ok": True,
            "issues": [
                {
                    "kind": "text_spelling",
                    "severity": "blocker",
                    "description": "  拼写错误  ",
                    "suggested_edit": " 改成 X ",
                }
            ],
        }
    )
    assert validated["ok"] is False
    issue = validated["issues"][0]
    assert issue == {
        "kind": "text_spelling",
        "severity": "blocker",
        "description": "拼写错误",
        "suggested_edit": "改成 X",
    }


def test_validate_review_severity_and_fields_normalized() -> None:
    validated = validate_cover_image_review(
        {
            "issues": [
                {"kind": "other", "severity": "BLOCKER", "description": "大问题"},
                {"kind": "other", "severity": "fatal", "description": "非 blocker 一律 advice"},
                {"kind": "", "description": "  "},
                "not-a-mapping",
                {"description": "缺 kind 与 severity"},
            ]
        }
    )
    assert validated["ok"] is False
    assert [i["severity"] for i in validated["issues"]] == ["blocker", "advice", "advice"]
    assert validated["issues"][2]["kind"] == "general"
    assert validated["issues"][2]["suggested_edit"] == ""


def test_validate_review_empty_and_cap() -> None:
    assert validate_cover_image_review(None) == {"issues": [], "ok": True}
    assert validate_cover_image_review({"issues": "bad"}) == {"issues": [], "ok": True}
    raw = {
        "issues": [
            {"kind": "other", "severity": "advice", "description": "x"} for _ in range(25)
        ]
    }
    validated = validate_cover_image_review(raw)
    assert len(validated["issues"]) == 20
    assert validated["ok"] is True
    long_text = "长" * 600
    trimmed = validate_cover_image_review(
        {"issues": [{"description": long_text, "suggested_edit": long_text}]}
    )
    assert len(trimmed["issues"][0]["description"]) == 500
    assert len(trimmed["issues"][0]["suggested_edit"]) == 500
