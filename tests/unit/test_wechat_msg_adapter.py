"""Unit tests for v0.1.1 WeChatMsg JSON adapter (§C.-42b LIVE-driven).

Verifies _build_wechat_prompt_from_messages handles:
  - Type=1 (text) — content passes through
  - Type=3/43/47 (image/video/sticker) — placeholders, not silently dropped
  - Type=49 (appmsg) — XML <title> extraction or raw fallback
  - Type=10000 (sysmsg) — content passes through
  - IsSender=1 — sets is_self_message hint + n_self_msgs / is_solo_self
  - Field aliases (StrContent/NickName/CreateTime vs content/sender/ts)
"""
from __future__ import annotations

from memexa.cli.wizards import (
    _build_wechat_prompt_from_messages,
    _extract_appmsg_title,
    _wechatmsg_content,
    _WECHAT_TYPE_PLACEHOLDER,
)


class TestWechatMsgContent:
    def test_text_passthrough(self):
        assert _wechatmsg_content({"Type": 1, "StrContent": "你好"}) == "你好"

    def test_text_via_alias_content(self):
        assert _wechatmsg_content({"Type": 1, "content": "hi"}) == "hi"

    def test_image_placeholder(self):
        assert _wechatmsg_content({"Type": 3, "StrContent": ""}) == "[图片]"

    def test_video_placeholder(self):
        assert _wechatmsg_content({"Type": 43, "StrContent": ""}) == "[视频]"

    def test_sticker_placeholder(self):
        assert _wechatmsg_content({"Type": 47, "StrContent": ""}) == "[表情]"

    def test_voice_placeholder(self):
        assert _wechatmsg_content({"Type": 34, "StrContent": ""}) == "[语音]"

    def test_location_placeholder(self):
        assert _wechatmsg_content({"Type": 48, "StrContent": ""}) == "[位置]"

    def test_appmsg_with_title(self):
        xml = "<msg><appmsg><title>分享标题</title></appmsg></msg>"
        out = _wechatmsg_content({"Type": 49, "StrContent": xml})
        assert out == "[分享: 分享标题]"

    def test_appmsg_no_title_fallback(self):
        xml = "<msg><appmsg><url>http://x</url></appmsg></msg>"
        out = _wechatmsg_content({"Type": 49, "StrContent": xml})
        # Falls back to raw XML
        assert "appmsg" in out

    def test_appmsg_empty(self):
        out = _wechatmsg_content({"Type": 49, "StrContent": ""})
        assert out == "[分享]"

    def test_sysmsg_passthrough(self):
        out = _wechatmsg_content(
            {"Type": 10000, "StrContent": "Alice 撤回了一条消息"}
        )
        assert out == "Alice 撤回了一条消息"

    def test_unknown_type_no_content(self):
        out = _wechatmsg_content({"Type": 9999, "StrContent": ""})
        assert "9999" in out

    def test_missing_type_treated_as_text(self):
        assert _wechatmsg_content({"StrContent": "无 Type 字段"}) == "无 Type 字段"

    def test_type_string_handled(self):
        # WeChatMsg may emit Type as string
        assert _wechatmsg_content({"Type": "3", "StrContent": ""}) == "[图片]"


class TestAppmsgTitle:
    def test_basic(self):
        assert _extract_appmsg_title(
            "<msg><appmsg><title>hello</title></appmsg></msg>"
        ) == "hello"

    def test_no_title(self):
        assert _extract_appmsg_title("<msg></msg>") is None

    def test_empty(self):
        assert _extract_appmsg_title("") is None

    def test_chinese(self):
        assert _extract_appmsg_title(
            "<msg><appmsg><title>中文标题</title></appmsg></msg>"
        ) == "中文标题"


class TestPromptBuilder:
    def test_mixed_types_no_silent_drop(self):
        msgs = [
            {"Type": 1, "StrContent": "今晚吃饭吗",
             "CreateTime": "2026-05-15T18:00:00+08:00", "NickName": "Alice"},
            {"Type": 3, "StrContent": "",
             "CreateTime": "2026-05-15T18:01:00+08:00", "NickName": "Alice"},
            {"Type": 47, "StrContent": "",
             "CreateTime": "2026-05-15T18:02:00+08:00", "NickName": "Alice"},
        ]
        out = _build_wechat_prompt_from_messages("batch1", "room", msgs)
        assert out["n_msgs"] == 3  # all 3 survive (not just text)
        contents = [m["content"] for m in out["messages"]]
        assert "今晚吃饭吗" in contents
        assert "[图片]" in contents
        assert "[表情]" in contents

    def test_is_sender_hint_solo_self(self):
        msgs = [
            {"Type": 1, "StrContent": "记一下：买狗粮",
             "CreateTime": "2026-05-15T18:00:00+08:00", "NickName": "Me",
             "IsSender": 1},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["n_self_msgs"] == 1
        assert out["n_other_msgs"] == 0
        assert out["is_solo_self"] is True
        assert out["is_self_chat"] is True
        assert out["messages"][0].get("is_self_message") is True

    def test_is_sender_hint_dyad(self):
        msgs = [
            {"Type": 1, "StrContent": "好啊",
             "CreateTime": "2026-05-15T18:00:00+08:00", "NickName": "Me",
             "IsSender": 1},
            {"Type": 1, "StrContent": "出去吃饭吗",
             "CreateTime": "2026-05-15T17:00:00+08:00", "NickName": "Alice",
             "IsSender": 0},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["n_self_msgs"] == 1
        assert out["n_other_msgs"] == 1
        assert out["is_solo_self"] is False
        assert out["is_self_chat"] is True
        assert out["is_group_chat"] is False  # only 2 senders

    def test_aliases_camelcase(self):
        # WeChatMsg native fields
        msgs = [
            {"Type": 1, "StrContent": "x", "CreateTime": 1714694400,
             "NickName": "Bob"},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["messages"][0]["sender"] == "Bob"
        assert out["messages"][0]["content"] == "x"

    def test_aliases_demo(self):
        # Demo format
        msgs = [
            {"sender": "Bob", "content": "x", "send_time": "2024-01-01T10:00:00+08:00"},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["messages"][0]["sender"] == "Bob"
        assert out["messages"][0]["content"] == "x"

    def test_empty_content_dropped(self):
        # Type=1 with empty content should still drop (no signal)
        msgs = [
            {"Type": 1, "StrContent": "",
             "CreateTime": "2026-05-15T18:00:00+08:00", "NickName": "Alice"},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["n_msgs"] == 0

    def test_appmsg_share_extracted(self):
        msgs = [
            {"Type": 49, "SubType": 5,
             "StrContent": "<msg><appmsg><title>看看这个</title></appmsg></msg>",
             "CreateTime": "2026-05-15T18:00:00+08:00", "NickName": "Alice"},
        ]
        out = _build_wechat_prompt_from_messages("b", "room", msgs)
        assert out["n_msgs"] == 1
        assert out["messages"][0]["content"] == "[分享: 看看这个]"
