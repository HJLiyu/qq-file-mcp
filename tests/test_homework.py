from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from test_service import FakeQQ

from qq_file_mcp.client import OneBotClient
from qq_file_mcp.errors import QQFileError
from qq_file_mcp.homework import HomeworkService
from qq_file_mcp.homework_client import NativeHomeworkClient
from qq_file_mcp.service import FileService
from qq_file_mcp.submissions import SubmissionService


def assignment(hw_id=101, status=1):
    return {
        "hw_id": hw_id,
        "hw_title": "数值积分",
        "hw_type": 0,
        "puin": 80001,
        "pnick_name": "张老师",
        "ts_create": 1700000000,
        "user_status": status,
        "need_feedback": True,
        "content": {
            "c": [
                {"type": "str", "text": "PRIVATE_REQUIREMENT"},
                {"type": "img", "url": "https://p.qpic.cn/assignment.jpg"},
            ]
        },
        "feedback": {
            "uin": 90001,
            "status": status,
            "comment_status": 0,
            "fb_content": None,
            "feedback_ts": 0,
            "review_ts": 0,
        },
    }


class Native:
    def __init__(self):
        self.rows = {101: assignment(), 102: assignment(102)}
        self.pages = {1: [101], 2: [102]}
        self.end = {1: 0, 2: 1}
        self.writes = []
        self.failure = None
        self.match = True
        self.waiting = None
        self.read_failure = False

    async def list(self, group_id, page, owner):
        # Live list has empty publisher; detail must supply it before filtering.
        return {
            "homework": [
                {"hw_id": x, "puin": 0, "pnick_name": ""} for x in self.pages.get(page, [])
            ],
            "end_flag": self.end.get(page, 1),
        }

    async def detail(self, group_id, homework_id, owner):
        if self.writes and self.read_failure:
            raise QQFileError("HOMEWORK_TIMEOUT", "private upstream text must not leak")
        return deepcopy(self.rows[int(homework_id)])

    async def submit_text(self, group_id, homework_id, owner, text):
        self.writes.append((group_id, homework_id, owner, text))
        if self.waiting:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure
        row = self.rows[int(homework_id)]
        row["feedback"].update(
            status=2,
            fb_content={
                "main": [
                    {
                        "id": "native-feedback",
                        "uin": int(owner),
                        "text": {
                            "c": [
                                {"type": "str", "text": text if self.match else "different answer"}
                            ]
                        },
                    }
                ]
            },
        )


@pytest.fixture
def homework(settings):
    settings = replace(settings, backend="snowluma", enable_submissions=True)
    qq = FakeQQ(settings)
    native = Native()
    service = HomeworkService(FileService(qq, settings), native)
    return qq, native, service


async def reference(service):
    return (await service.search("学习群"))["homework"][0]["homework_ref"]


async def test_live_schema_publisher_filter_pagination_and_no_content_persisted(homework):
    _, native, service = homework
    first = await service.search("学习群", publisher="张老师")
    assert first["homework"][0]["publisher"]["user_id"] == "80001"
    assert first["coverage"]["exhaustive"] is False
    assert first["next_cursor"]  # A short page is not end when end_flag=0.
    second = await service.search("学习群", publisher="张老师", cursor=first["next_cursor"])
    assert second["homework"][0]["homework_id"] == "102"
    assert second["next_cursor"] is None
    with sqlite3.connect(service.files.store.path) as db:
        saved = str(db.execute("SELECT value FROM items").fetchall())
    assert "PRIVATE_REQUIREMENT" not in saved and "qpic.cn" not in saved
    assert not native.writes


@pytest.mark.parametrize(
    "change,code",
    [("owner", "ACCOUNT_CHANGED"), ("keyword", "CURSOR_MISMATCH"), ("group", "CURSOR_MISMATCH")],
)
async def test_cursor_binds_account_group_and_filters(homework, change, code):
    qq, _, service = homework
    first = await service.search("学习群")
    kwargs = {}
    if change == "owner":
        qq.owner = "90002"
    if change == "keyword":
        kwargs["keyword"] = "changed"
    if change == "group":
        qq.groups[0]["group_name"] = "新群名"
    with pytest.raises(QQFileError) as e:
        await service.search(qq.groups[0]["group_name"], cursor=first["next_cursor"], **kwargs)
    assert e.value.code == code


async def test_repeated_and_empty_pages_report_incomplete(homework):
    _, native, service = homework
    first = await service.search("学习群")
    native.pages[2] = [101]
    repeated = await service.search("学习群", cursor=first["next_cursor"])
    assert repeated["coverage"]["stop_reason"] == "repeated_page"
    assert repeated["homework"] == [] and repeated["next_cursor"] is None
    native.pages[2] = []
    native.end[2] = 0
    empty = await service.search("学习群", cursor=first["next_cursor"])
    assert empty["coverage"]["stop_reason"] == "empty_page"
    assert not empty["coverage"]["exhaustive"] and empty["next_cursor"] is None


async def test_real_native_null_end_page_is_empty_not_protocol_failure(homework):
    _, native, service = homework

    async def null_page(group_id, page, owner):
        return {"homework": None, "end_flag": 1}

    native.list = null_page
    result = await service.search("学习群")
    assert result["homework"] == [] and result["next_cursor"] is None
    assert result["coverage"]["stop_reason"] == "upstream_end"


async def test_read_long_requirements_own_answer_teacher_score(homework):
    _, native, service = homework
    row = native.rows[101]
    row["content"]["c"][0]["text"] = "推导" * 1000
    row["feedback"].update(
        status=3,
        fb_content={
            "main": [
                {"id": "own", "uin": 90001, "text": {"c": [{"type": "str", "text": "own answer"}]}}
            ],
            "comment": [
                {"uin": 80001, "text": {"score": "A+", "c": [{"type": "str", "text": "评语"}]}}
            ],
        },
    )
    ref = await reference(service)
    first = await service.read(ref, max_chars=1000)
    second = await service.read(**first["next_read"])
    assert first["text"] + second["text"] == "推导" * 1000
    assert first["own_status"]["state"] == "graded"
    assert first["own_status"]["teacher_accepted"] is None
    assert first["media"][0]["type"] == "img"
    assert (await service.read(ref, section="own_submission:0"))["text"] == "own answer"
    assert (await service.read(ref, section="teacher_comment:0"))["score"] == "A+"


async def test_own_answer_continuation_never_mixes_changed_versions(homework):
    _, native, service = homework
    native.rows[101]["feedback"].update(
        status=2,
        fb_content={
            "main": [
                {
                    "id": "own",
                    "uin": 90001,
                    "text": {"c": [{"type": "str", "text": "A" * 2000}]},
                }
            ]
        },
    )
    ref = await reference(service)
    first = await service.read(ref, section="own_submission:0", max_chars=1000)
    native.rows[101]["feedback"]["fb_content"]["main"][0]["text"]["c"][0]["text"] = "B" * 2000
    with pytest.raises(QQFileError) as e:
        await service.read(**first["next_read"])
    assert e.value.code == "HOMEWORK_CHANGED"


@pytest.mark.parametrize(
    "change,code",
    [
        ("owner", "ACCOUNT_CHANGED"),
        ("group", "GROUP_CHANGED"),
        ("content", "HOMEWORK_CHANGED"),
        ("feedback_owner", "HOMEWORK_OWNER"),
    ],
)
async def test_reference_read_revalidates_identity_and_never_returns_classmate_answer(
    homework, change, code
):
    qq, native, service = homework
    ref = await reference(service)
    if change == "owner":
        qq.owner = "90002"
    if change == "group":
        qq.groups[0]["group_name"] = "changed"
    if change == "content":
        native.rows[101]["content"]["c"][0]["text"] = "changed"
    if change == "feedback_owner":
        native.rows[101]["feedback"]["uin"] = 90002
    with pytest.raises(QQFileError) as e:
        await service.read(ref)
    assert e.value.code == code


async def test_attachment_reference_refresh_download_and_no_arbitrary_url(homework, monkeypatch):
    _, native, service = homework
    # Real native details still return legacy http:// QQ image URLs.
    native.rows[101]["content"]["c"][1]["url"] = "http://p.qpic.cn/assignment.jpg"
    ref = await reference(service)
    attachment = (await service.read(ref))["media"][0]["attachment_ref"]
    calls = []

    async def fake_download(url, name, settings):
        import hashlib

        calls.append(url)
        settings.download_dir.mkdir()
        path = settings.download_dir / name
        path.write_bytes(b"image")
        return {
            "path": str(path),
            "file_name": name,
            "bytes": 5,
            "sha256": hashlib.sha256(b"image").hexdigest(),
        }

    monkeypatch.setattr("qq_file_mcp.homework.download_url", fake_download)
    result = await service.download(attachment)
    assert result["local_file_id"] and calls == ["https://p.qpic.cn/assignment.jpg"]
    native.rows[101]["content"]["c"][1]["url"] = "https://evil.example/file"
    with pytest.raises(QQFileError) as e:
        await service.download(attachment)
    assert e.value.code == "HOMEWORK_CHANGED" and len(calls) == 1


async def test_prepared_text_is_exact_no_send_verified_receipt_single_attempt(homework):
    _, native, service = homework
    preview = await service.prepare(await reference(service), "  我的答案 [CQ:at,qq=all]  ")
    assert preview["destination"] == "native_homework" and not native.writes
    assert preview["text"] == "我的答案 [CQ:at,qq=all]"
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == "verified_native_submission"
    assert receipt["native_submission_verified"] is True
    assert receipt["feedback_id"] == "native-feedback" and receipt["teacher_accepted"] is None
    assert await service.submit(preview["preview_id"]) == receipt
    assert len(native.writes) == 1
    with sqlite3.connect(service.files.store.path) as db:
        saved = db.execute("SELECT value,receipt FROM submissions").fetchone()
    assert saved[0] == "{}" and "我的答案" not in saved[1]


async def test_existing_media_answer_requires_explicit_replacement(homework):
    _, native, service = homework
    native.rows[101]["feedback"].update(
        status=2,
        fb_content={
            "main": [
                {
                    "id": "old",
                    "uin": 90001,
                    "text": {"c": [{"type": "file", "name": "own.pdf"}]},
                }
            ]
        },
    )
    ref = await reference(service)
    with pytest.raises(QQFileError) as e:
        await service.prepare(ref, "new answer")
    assert e.value.code == "HOMEWORK_ALREADY_SUBMITTED"
    preview = await service.prepare(ref, "new answer", True)
    assert preview["replace_existing"] is True and not native.writes


@pytest.mark.parametrize(
    "change,code",
    [
        ("content", "HOMEWORK_CHANGED"),
        ("answer", "HOMEWORK_CHANGED"),
        ("expiry", "EXPIRED_REFERENCE"),
        ("backend", "BACKEND_CHANGED"),
    ],
)
async def test_preview_changes_never_write(homework, change, code):
    _, native, service = homework
    preview = await service.prepare(await reference(service), "answer")
    if change == "content":
        native.rows[101]["hw_title"] = "new"
    if change == "answer":
        native.rows[101]["feedback"]["comment_status"] = 1
    if change == "expiry":
        with sqlite3.connect(service.files.store.path) as db:
            db.execute("UPDATE submissions SET expires=0")
    if change == "backend":
        service.files.settings = replace(service.settings, backend="napcat")
    with pytest.raises(QQFileError) as e:
        await service.submit(preview["preview_id"])
    assert e.value.code == code and not native.writes


async def test_disabled_cross_kind_and_external_homework_never_send(homework):
    _, native, service = homework
    generic = SubmissionService(service.files)
    preview = await service.prepare(await reference(service), "answer")
    with pytest.raises(QQFileError) as e:
        await generic.submit(preview["preview_id"])
    assert e.value.code == "SUBMISSION_KIND"
    chat = await generic.prepare("学习群", text="answer")
    with pytest.raises(QQFileError) as e:
        await service.submit(chat["preview_id"])
    assert e.value.code == "SUBMISSION_KIND"
    service.settings = replace(service.settings, enable_submissions=False)
    with pytest.raises(QQFileError) as e:
        await service.submit(preview["preview_id"])
    assert e.value.code == "SUBMISSIONS_DISABLED"
    native.rows[101]["content"]["c"].append({"type": "exam", "appid": "external"})
    with pytest.raises(QQFileError) as e:
        await service.prepare(await reference(service), "answer")
    assert e.value.code == "HOMEWORK_FORMAT" and not native.writes


@pytest.mark.parametrize(
    "mode,status",
    [
        ("timeout", "outcome_unknown"),
        ("mismatch", "accepted_by_native"),
        ("read_timeout", "accepted_by_native"),
    ],
)
async def test_uncertain_or_unverified_outcome_is_persistent_never_retry(homework, mode, status):
    _, native, service = homework
    preview = await service.prepare(await reference(service), "answer")
    if mode == "timeout":
        native.failure = QQFileError("HOMEWORK_TIMEOUT", "sensitive detail")
    if mode == "mismatch":
        native.match = False
    if mode == "read_timeout":
        native.read_failure = True
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == status and receipt["retry_allowed"] is False
    assert receipt["native_submission_verified"] is False
    assert "sensitive" not in str(receipt)
    restored = HomeworkService(FileService(service.files.client, service.settings), native)
    assert await restored.submit(preview["preview_id"]) == receipt
    assert len(native.writes) == 1


async def test_cancel_after_claim_redacts_answer_and_blocks_retry(homework):
    _, native, service = homework
    preview = await service.prepare(await reference(service), "private answer")
    native.waiting = asyncio.Event()
    task = asyncio.create_task(service.submit(preview["preview_id"]))
    await native.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = service.files.store.submission(preview["preview_id"])
    assert saved["value"] == {} and saved["receipt"]["status"] == "outcome_unknown"
    await service.submit(preview["preview_id"])
    assert len(native.writes) == 1


async def test_concurrent_services_only_one_native_write(homework):
    _, native, service = homework
    preview = await service.prepare(await reference(service), "answer")
    other = HomeworkService(FileService(service.files.client, service.settings), native)
    outcomes = await asyncio.gather(
        service.submit(preview["preview_id"]),
        other.submit(preview["preview_id"]),
        return_exceptions=True,
    )
    assert len(native.writes) == 1
    assert any(
        isinstance(x, dict) and x["status"] == "verified_native_submission" for x in outcomes
    )


@pytest.mark.parametrize("answer", ["", " ", "a" * 5001])
async def test_native_answer_limits_never_create_preview_or_send(homework, answer):
    _, native, service = homework
    with pytest.raises(QQFileError) as e:
        await service.prepare(await reference(service), answer)
    assert e.value.code == "INPUT" and not native.writes


async def test_list_timeout_cancels_all_detail_tasks(homework):
    _, native, service = homework
    service.settings = replace(service.settings, search_timeout=0.01)
    native.pages[1] = [101, 102]
    cancelled = []

    async def hanging_detail(group_id, homework_id, owner):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(homework_id)

    native.detail = hanging_detail
    with pytest.raises(QQFileError) as e:
        await service.search("学习群")
    assert e.value.code == "HOMEWORK_TIMEOUT" and set(cancelled) == {"101", "102"}


async def test_internal_credentials_not_generic_action_and_fixed_domain(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"retcode": 0, "status": "ok", "data": {"cookies": "secret"}}
        )

    bridge = OneBotClient(replace(settings, backend="snowluma"), httpx.MockTransport(handler))
    try:
        with pytest.raises(QQFileError):
            await bridge.call("get_credentials", domain="evil.example")
        await bridge.homework_credentials()
        assert len(calls) == 1 and json.loads(calls[0].content) == {"domain": "qun.qq.com"}
    finally:
        await bridge.close()


class Credentials:
    def __init__(self):
        self.value = {"cookies": "uin=o090001; skey=private-cookie;", "token": 123}

    async def homework_credentials(self):
        return self.value


async def test_native_transport_exact_contract_and_no_bridge_token(settings):
    creds = Credentials()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"retcode": 0, "data": {}})

    native = NativeHomeworkClient(
        creds, replace(settings, enable_submissions=True), httpx.MockTransport(handler)
    )
    await native.list("10001", 1, "90001")
    await native.detail("10001", "101", "90001")
    await native.submit_text("10001", "101", "90001", "自己的答案")
    for r in calls:
        assert r.url.host == "qun.qq.com" and "authorization" not in r.headers
        assert r.headers["cookie"] == creds.value["cookies"]
    form = parse_qs(calls[2].content.decode())
    assert form["gid"] == ["10001"] and form["hw_id"] == ["101"] and form["modify"] == ["1"]
    assert json.loads(form["comment_info"][0]) == {
        "text": {"c": [{"type": "str", "text": "自己的答案"}]},
        "uin": "90001",
    }


@pytest.mark.parametrize(
    "mode,code",
    [
        ("wrong_account", "ACCOUNT_CHANGED"),
        ("redirect", "HOMEWORK_AUTH"),
        ("upstream", "HOMEWORK_UPSTREAM"),
        ("html", "HOMEWORK_PROTOCOL"),
        ("large", "HOMEWORK_LIMIT"),
    ],
)
async def test_native_transport_credentials_response_errors_sanitized(settings, mode, code):
    creds = Credentials()
    calls = []
    if mode == "wrong_account":
        creds.value["cookies"] = "uin=o90002; skey=private-cookie"

    def handler(request):
        calls.append(request)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example"})
        if mode == "upstream":
            return httpx.Response(200, json={"retcode": 42, "msg": "private-cookie"})
        if mode == "html":
            return httpx.Response(200, text="private-cookie")
        if mode == "large":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        return httpx.Response(200, json={"retcode": 0, "data": {}})

    native = NativeHomeworkClient(creds, settings, httpx.MockTransport(handler))
    with pytest.raises(QQFileError) as e:
        await native.list("10001", 1, "90001")
    assert e.value.code == code and "private-cookie" not in str(e.value)
    assert len(calls) == (0 if mode == "wrong_account" else 1)
