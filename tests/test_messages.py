from __future__ import annotations

import sqlite3

import pytest
from test_service import FakeQQ, message

from qq_file_mcp.errors import QQFileError
from qq_file_mcp.messages import MessageService
from qq_file_mcp.service import FileService


def chat(index, text, sender=1):
    value = message(index)
    value["sender"] = {"user_id": sender, "nickname": "老师" if sender == 1 else "同学"}
    value["message"] = [{"type": "text", "data": {"text": text}}]
    return value


@pytest.fixture
def chats(settings):
    qq = FakeQQ(settings)
    qq.history = [
        chat(1, "作业：第2章第3题"),
        chat(2, "补充：提交代码和误差分析"),
        chat(3, "补充：我猜只要答案", 2),
    ]
    return qq, MessageService(FileService(qq, settings))


async def test_sender_only_query_preserves_evidence_without_persisting_text(chats):
    qq, service = chats
    result = await service.search("学习群", publisher="张老师")
    assert [m["text"] for m in result["messages"]] == [
        "补充：提交代码和误差分析",
        "作业：第2章第3题",
    ]
    assert all(m["sender"]["user_id"] == "1" and m["time"] for m in result["messages"])
    assert result["coverage"]["history"]["messages_scanned"] == 3
    assert result["native_homework_included"] is False
    with sqlite3.connect(service.files.store.path) as db:
        saved = " ".join(r[0] for r in db.execute("SELECT value FROM items"))
    assert "误差分析" not in saved and "第2章第3题" not in saved
    assert not any(a.startswith("send_") for a, _ in qq.calls)


async def test_keyword_and_date_filter_and_message_read(chats):
    _, service = chats
    result = await service.search(
        "学习群", "补充", publisher="1", published_after="2023-11-14T22:13:22+00:00"
    )
    assert len(result["messages"]) == 1
    row = result["messages"][0]
    first = await service.read(row["message_ref"], max_chars=3)
    parts = [first["text"]]
    while first["next_read"]:
        first = await service.read(**first["next_read"])
        parts.append(first["text"])
    assert "".join(parts) == row["text"]


async def test_char_limit_and_backwards_cursor_do_not_skip_older_messages(chats):
    qq, service = chats
    qq.history = [chat(i, "要求" * 1500 + str(i)) for i in range(1, 4)]
    seen = []
    cursor = None
    for _ in range(3):
        result = await service.search("学习群", "要求", max_chars=1000, history_cursor=cursor)
        assert len(result["messages"]) == 1
        row = result["messages"][0]
        assert row["text_truncated"] is True and len(row["text"]) == 1000
        whole = await service.read(row["message_ref"])
        assert len(whole["text"]) == 3001
        seen.append(row["message_id"])
        cursor = result["history_cursor"]
    assert seen == [message(i)["message_id"] for i in (3, 2, 1)]


async def test_result_limit_and_namespace_bound_cursors(chats):
    qq, service = chats
    qq.history = [chat(i, "作业") for i in range(1, 61)]
    result = await service.search("学习群", "作业")
    assert len(result["messages"]) == 50
    cursor = result["history_cursor"]
    older = await service.search("学习群", "作业", history_cursor=cursor)
    assert len(older["messages"]) == 10
    assert len({m["message_id"] for m in result["messages"] + older["messages"]}) == 60
    with pytest.raises(QQFileError, match="条件"):
        await service.search("学习群", "其他", history_cursor=cursor)
    with pytest.raises(QQFileError) as caught:
        await service.files.search("学习群", "作业", history_cursor=cursor)
    assert caught.value.code == "EXPIRED_REFERENCE"


async def test_attachment_search_returns_downloadable_reference_and_unsupported_markers(chats):
    qq, service = chats
    value = message(1, file=True)
    value["message"].extend(
        [
            {"type": "image", "data": {"url": "DO_NOT_RETURN_URL"}},
            {"type": "reply", "data": {"id": "123"}},
            {"type": "at", "data": {"qq": "all"}},
        ]
    )
    qq.history = [value]
    result = await service.search("学习群", "课件")
    row = result["messages"][0]
    assert row["unsupported_segments"] == ["image"]
    assert result["coverage"]["history"]["unparsed_segments"] == {"image": 1}
    assert row["reply_to"] == ["123"] and row["mentions"] == ["all"]
    assert "DO_NOT_RETURN_URL" not in str(result)
    downloaded = await service.files.download(row["files"][0]["result_id"])
    assert downloaded["ok"] is True


async def test_ambiguity_failure_and_mutated_message_are_explicit(chats):
    qq, service = chats
    qq.members.append({"user_id": 2, "card": "张老师"})
    result = await service.search("学习群", publisher="张老师")
    assert result["needs_publisher_selection"] is True
    assert not any(a == "get_group_msg_history" for a, _ in qq.calls)
    result = await service.search("学习群", "作业")
    ref = result["messages"][0]["message_ref"]
    qq.history[0]["message"][0]["data"]["text"] = "已经改变"
    with pytest.raises(QQFileError) as caught:
        await service.read(ref)
    assert caught.value.code == "STALE_MESSAGE"
    qq.fail_history = True
    failed = await service.search("学习群", "作业")
    assert failed["warnings"][0]["code"] == "UPSTREAM"
    assert failed["coverage"]["history"]["exhaustive"] is False


async def test_cross_group_message_is_rejected_and_account_change_blocks_read(chats):
    qq, service = chats
    result = await service.search("学习群", "作业")
    ref = result["messages"][0]["message_ref"]
    qq.owner = "90002"
    with pytest.raises(QQFileError) as caught:
        await service.read(ref)
    assert caught.value.code == "ACCOUNT_CHANGED"
    qq.history[0]["group_id"] = 10002
    result = await service.search("学习群", "作业")
    assert result["messages"] == []
    assert result["warnings"][0]["code"] == "PROTOCOL"
