from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import httpx
import pytest

from qq_file_mcp.client import OneBotClient
from qq_file_mcp.config import Settings
from qq_file_mcp.errors import QQFileError
from qq_file_mcp.files import copy_download, download_url, safe_filename, validate_download_url
from qq_file_mcp.service import FileService
from qq_file_mcp.state import ResultStore


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:3000",
        "http://127.0.0.1@evil.test",
        "file:///tmp/napcat",
        "http://127.0.0.1/?token=secret",
    ],
)
def test_only_loopback_backend(url):
    with pytest.raises(QQFileError):
        Settings(base_url=url, token="secret")


def test_token_required():
    with pytest.raises(QQFileError, match="NAPCAT_TOKEN"):
        Settings()


async def test_transport_auth_and_allowlist(settings):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": []})

    client = OneBotClient(settings, transport=httpx.MockTransport(respond))
    try:
        assert await client.call("get_group_list") == []
        with pytest.raises(QQFileError, match="允许列表"):
            await client.call("send_group_msg", group_id="10001", message="unwanted")
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {}, "AUTH"),
        (302, {}, "REDIRECT"),
        (200, {"status": "failed", "retcode": 1200, "message": "SECRET_CHAT_TEXT"}, "UPSTREAM"),
        (200, {"hello": "world"}, "PROTOCOL"),
    ],
)
async def test_transport_failure_never_leaks_payload(settings, status, body, code):
    client = OneBotClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json=body, headers={"location": "https://evil.test"}
            )
        ),
    )
    try:
        with pytest.raises(QQFileError) as caught:
            await client.call("get_status")
        assert caught.value.code == code
        assert "SECRET_CHAT_TEXT" not in str(caught.value)
    finally:
        await client.close()


def test_download_cannot_escape_or_overwrite(settings, tmp_path):
    source = settings.allowed_roots[0] / "cache"
    source.write_bytes(b"test")
    result = copy_download(str(source), "../../CON.txt", settings, 4)
    from pathlib import Path

    target = Path(result["path"])
    assert target.parent == settings.download_dir
    assert target.read_bytes() == b"test"
    outside = tmp_path / "private.env"
    outside.write_text("secret")
    with pytest.raises(QQFileError, match="允许目录"):
        copy_download(str(outside), "private.env", settings)
    with pytest.raises(QQFileError, match="大小"):
        copy_download(str(source), "bad-size.txt", settings, 5)


@pytest.mark.parametrize(
    "name",
    [
        "CON.txt",
        "NUL",
        "AUX.pdf",
        "LPT1.log",
        "..\\foo.txt",
        "a:b.txt",
        "evil\u202efile.exe",
        ".",
        "",
    ],
)
def test_windows_names_are_safe(name):
    cleaned = safe_filename(name)
    assert cleaned and "/" not in cleaned and "\\" not in cleaned and ":" not in cleaned
    assert "\u202e" not in cleaned
    assert cleaned.upper().split(".")[0] not in {"CON", "NUL", "AUX", "LPT1"}


def test_oversized_file_is_rejected_before_copy(configured):
    settings = configured(max_download_bytes=2)
    source = settings.allowed_roots[0] / "large"
    source.write_bytes(b"123")
    with pytest.raises(QQFileError, match="大小上限"):
        copy_download(str(source), "large", settings)
    assert not settings.download_dir.exists()


def test_expired_or_wrong_kind_reference_rejected(settings):
    store = ResultStore(settings.state_dir)
    reference = store.put("file", {"file_name": "test"})
    with pytest.raises(QQFileError):
        store.get(reference, "history")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE items SET expires=0")
    with pytest.raises(QQFileError, match="过期"):
        store.get(reference, "file")


async def test_timeout_returns_coverage_instead_of_false_absence(configured):
    class SlowQQ:
        async def call(self, action, **params):
            if action == "get_login_info":
                return {"user_id": 90001}
            if action == "get_group_list":
                return [{"group_id": 10001, "group_name": "test"}]
            await asyncio.sleep(1)

    service = FileService(SlowQQ(), configured(search_timeout=0.01))
    result = await service.search("10001", "file", "group_files")
    assert result["coverage"]["group_files"]["status"] == "partial"
    assert result["warnings"][0]["code"] == "TIMEOUT"
    assert result["results"] == []


def test_settings_dont_repr_secrets(settings):
    assert settings.token not in repr(settings)
    assert "test-secret" not in json.dumps(QQFileError("AUTH", "令牌不正确").as_dict())


def test_cli_chinese_json_survives_windows_pipe(tmp_path):
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "cp1252", "NAPCAT_TOKEN": "", "ONEBOT_TOKEN": ""})
    result = subprocess.run(
        [sys.executable, "-m", "qq_file_mcp", "doctor"], env=env, capture_output=True, timeout=20
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["error"]["code"] == "CONFIG"
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8")


@pytest.mark.parametrize(
    "url",
    [
        "http://qq.com/file",
        "https://qq.com.evil.test/file",
        "https://127.0.0.1/file",
        "https://qq.com:1234/file",
        "https://secret@qq.com/file",
        "file:///secret",
    ],
)
def test_download_url_rejects_non_qq_or_local_targets(url):
    with pytest.raises(QQFileError):
        validate_download_url(url)


async def test_https_download_has_no_backend_credential(settings):
    def respond(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"demo", headers={"content-length": "4"})

    result = await download_url(
        "https://test.ftn.qq.com/file?ticket=private",
        "测试.pdf",
        settings,
        4,
        httpx.MockTransport(respond),
    )
    assert result["bytes"] == 4
    assert "ticket" not in json.dumps(result)
    assert not list(settings.state_dir.glob("*.part"))


async def test_download_redirect_revalidated(settings):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:3000/get_file"})

    with pytest.raises(QQFileError, match="允许"):
        await download_url(
            "https://test.ftn.qq.com/file", "bad", settings, transport=httpx.MockTransport(respond)
        )
    assert len(calls) == 1


async def test_partial_download_is_discarded(settings):
    with pytest.raises(QQFileError, match="大小"):
        await download_url(
            "https://test.ftn.qq.com/file",
            "bad",
            settings,
            100,
            httpx.MockTransport(lambda request: httpx.Response(200, content=b"no")),
        )
    assert not list(settings.state_dir.glob("*.part"))
    assert not settings.download_dir.exists()
