from __future__ import annotations

import json

import httpx
import pytest

from qq_file_mcp.client import OneBotClient
from qq_file_mcp.config import Settings
from qq_file_mcp.errors import QQFileError
from qq_file_mcp.service import FileService


def test_generic_environment_and_legacy_fallback(tmp_path, monkeypatch):
    for key in ("ONEBOT_URL", "ONEBOT_TOKEN", "QQ_FILE_BACKEND", "NAPCAT_URL", "NAPCAT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("NAPCAT_URL=http://127.0.0.1:3002\nNAPCAT_TOKEN=legacy\n")
    legacy = Settings.load(env)
    assert legacy.base_url == "http://127.0.0.1:3002" and legacy.token == "legacy"
    assert legacy.backend == "napcat"
    env.write_text(
        "QQ_FILE_BACKEND=snowluma\nONEBOT_URL=http://127.0.0.1:3003\n"
        "ONEBOT_TOKEN=shared-session\nNAPCAT_TOKEN=old\n"
    )
    shared = Settings.load(env)
    assert shared.backend == "snowluma" and shared.token == "shared-session"
    assert shared.base_url == "http://127.0.0.1:3003"


def test_unknown_backend_rejected(configured):
    with pytest.raises(QQFileError, match="QQ_FILE_BACKEND"):
        configured(backend="unknown")


@pytest.mark.parametrize("backend", ["napcat", "snowluma"])
async def test_history_anchor_wire_format(configured, backend):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    client = OneBotClient(configured(backend=backend), httpx.MockTransport(respond))
    try:
        await client.call(
            "get_group_msg_history",
            group_id="10001",
            message_seq="987654321",
            count=100,
            reverse_order=True,
            disable_get_url=True,
            parse_mult_msg=False,
        )
        await client.call("get_msg", message_id="987654321")
    finally:
        await client.close()
    if backend == "snowluma":
        assert sent[0] == {
            "group_id": "10001",
            "message_id": 987654321,
            "count": 100,
            "reverse_order": True,
        }
        assert sent[1]["message_id"] == 987654321
    else:
        assert sent[0]["message_seq"] == "987654321"
        assert "message_id" not in sent[0]
        assert sent[1]["message_id"] == "987654321"


async def test_snowluma_file_cache_action_rejected_without_request(configured):
    def respond(request):
        raise AssertionError("Group files cannot use SnowLuma's media-only get_file")

    client = OneBotClient(configured(backend="snowluma"), httpx.MockTransport(respond))
    try:
        with pytest.raises(QQFileError, match="群文件"):
            await client.call("get_file", file_id="file-id")
    finally:
        await client.close()


async def test_directory_bound_when_backend_ignores_requested_count(configured):
    class UnlimitedQQ:
        async def call(self, action, **params):
            if action == "get_login_info":
                return {"user_id": 90001}
            if action == "get_group_list":
                return [{"group_id": 10001, "group_name": "test"}]
            if action == "get_group_root_files":
                return {
                    "files": [{"file_id": str(i), "file_name": f"file{i}.pdf"} for i in range(10)],
                    "folders": [],
                }
            raise AssertionError(action)

    service = FileService(UnlimitedQQ(), configured(backend="snowluma", directory_limit=3))
    result = await service.search("10001", "*", "group_files")
    assert result["matched_in_this_scan"] == 3
    assert result["coverage"]["group_files"]["files_scanned"] == 3
    assert result["coverage"]["group_files"]["status"] == "limited"


async def test_snowluma_history_continuation_and_attachment_download(configured, monkeypatch):
    # SnowLuma emits QQ's sequence in message_seq, hashed IDs in message_id.
    # Every timestamp is identical to expose incorrectly sorting by hashed IDs.
    messages = [
        {
            "message_id": (i * 7919) % 100003 + 1,
            "message_seq": i,
            "group_id": 10001,
            "time": 1700000000,
            "message": [
                {
                    "type": "file",
                    "data": {
                        "file_id": f"attachment-{i}",
                        "file": f"课件-{i}.pdf",
                        "file_size": 4,
                    },
                }
            ]
            if i % 10 == 0
            else [{"type": "text", "data": {"text": "PRIVATE_BODY"}}],
        }
        for i in range(1, 131)
    ]
    requested_urls = []

    def respond(request):
        params = json.loads(request.content)
        action = request.url.path.removeprefix("/")
        if action == "get_login_info":
            data = {"user_id": 90001}
        elif action == "get_group_list":
            data = [{"group_id": 10001, "group_name": "学习群"}]
        elif action == "get_group_msg_history":
            rows = messages
            if params.get("message_id"):
                assert isinstance(params["message_id"], int)
                index = next(
                    i for i, m in enumerate(rows) if m["message_id"] == params["message_id"]
                )
                rows = rows[: index + 1]
            data = {"messages": rows[-params["count"] :]}
        elif action == "get_msg":
            data = next(m for m in messages if m["message_id"] == params["message_id"])
        elif action == "get_group_file_url":
            requested_urls.append(params)
            data = {"url": "https://test.ftn.qq.com/selected-file"}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def fake_download(url, name, settings, size):
        assert name == "课件-130.pdf" and size == 4
        return {"path": "verified.pdf", "bytes": 4, "sha256": "a" * 64}

    monkeypatch.setattr("qq_file_mcp.service.download_url", fake_download)
    settings = configured(backend="snowluma")
    client = OneBotClient(settings, httpx.MockTransport(respond))
    try:
        service = FileService(client, settings)
        first = await service.search("10001", "课件", "history", 20)
        second = await service.search("10001", "课件", "history", 200, first["history_cursor"])
        names = [r["file_name"] for batch in (first, second) for r in batch["results"]]
        assert len(names) == len(set(names)) == 13
        assert first["coverage"]["history"]["messages_scanned"] == 20
        assert second["coverage"]["history"]["messages_scanned"] == 110
        assert first["results"][0]["file_name"] == "课件-130.pdf"
        downloaded = await service.download(first["results"][0]["result_id"])
        assert downloaded["download_method"] == "qq_https"
        assert requested_urls == [{"group_id": "10001", "file_id": "attachment-130", "busid": 102}]
        assert b"PRIVATE_BODY" not in service.store.path.read_bytes()
    finally:
        await client.close()


async def test_empty_snowluma_history_reports_possible_missing_anchor(configured):
    def respond(request):
        action = request.url.path.removeprefix("/")
        data = {
            "get_login_info": {"user_id": 90001},
            "get_group_list": [{"group_id": 10001, "group_name": "test"}],
            "get_group_msg_history": {"messages": []},
        }[action]
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    settings = configured(backend="snowluma")
    client = OneBotClient(settings, httpx.MockTransport(respond))
    try:
        result = await FileService(client, settings).search("10001", "*", "history")
        assert result["coverage"]["history"]["messages_scanned"] == 0
        assert result["warnings"][0]["code"] == "HISTORY_ANCHOR_UNAVAILABLE"
        assert not result["coverage"]["history"]["exhaustive"]
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["file", "history", "results"])
async def test_backend_switch_invalidates_existing_references(configured, kind):
    class SameAccount:
        async def call(self, action, **params):
            if action == "get_login_info":
                return {"user_id": 90001}
            if action == "get_group_list":
                return [{"group_id": 10001, "group_name": "test"}]
            raise AssertionError("Must not forward references across backend implementations")

    service = FileService(SameAccount(), configured(backend="snowluma"))
    reference = service.store.put(kind, {"owner": "90001", "group_id": "10001", "keyword": "*"})
    with pytest.raises(QQFileError) as caught:
        if kind == "file":
            await service.download(reference)
        elif kind == "history":
            await service.search("10001", "*", "history", history_cursor=reference)
        else:
            await service.more_results(reference)
    assert caught.value.code == "BACKEND_CHANGED"
