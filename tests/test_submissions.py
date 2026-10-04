from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_service import FakeQQ

from qq_file_mcp.client import OneBotClient
from qq_file_mcp.errors import QQFileError
from qq_file_mcp.service import FileService
from qq_file_mcp.submissions import SubmissionService


class SubmitQQ(FakeQQ):
    def __init__(self, settings):
        super().__init__(settings)
        self.writes = []
        self.failure = None
        self.waiting = None

    async def call(self, action, **params):
        if action in {"send_group_msg", "upload_group_file"}:
            self.writes.append((action, params))
            if action == "upload_group_file":
                self.uploaded_bytes = Path(params["file"]).read_bytes()
            if self.waiting:
                self.waiting.set()
                await asyncio.Event().wait()
            if self.failure:
                raise self.failure
            return (
                {"file_id": "file-receipt"}
                if action == "upload_group_file"
                else {"message_id": -321}
            )
        return await super().call(action, **params)


@pytest.fixture
def submission(settings, tmp_path):
    settings = replace(settings, submission_dir=tmp_path / "submissions", enable_submissions=True)
    settings.submission_dir.mkdir()
    qq = SubmitQQ(settings)
    return qq, SubmissionService(FileService(qq, settings))


async def test_preview_has_exact_destination_and_never_sends(submission):
    qq, service = submission
    preview = await service.prepare("学习群", text="我的答案 [CQ:at,qq=all]")
    assert preview["group"] == {"group_id": "10001", "group_name": "学习群"}
    assert preview["native_homework_submission"] is False
    assert preview["expires_at"] > time.time()
    assert preview["text"] == "我的答案 [CQ:at,qq=all]" and not qq.writes
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == "accepted_by_bridge" and receipt["teacher_accepted"] is None
    assert receipt["message_id"] == "-321"
    assert qq.writes[0][1]["message"] == [{"type": "text", "data": {"text": preview["text"]}}]
    assert qq.writes[0][1]["auto_escape"] is True
    assert await service.submit(preview["preview_id"]) == receipt
    assert len(qq.writes) == 1
    with sqlite3.connect(service.files.store.path) as db:
        value = db.execute("SELECT value FROM submissions").fetchone()[0]
    assert value == "{}"


async def test_file_snapshot_matches_preview_and_is_removed_after_attempt(submission):
    qq, service = submission
    path = service.root / "作业.pdf"
    path.write_bytes(b"own answer")
    preview = await service.prepare("学习群", file_path="作业.pdf")
    assert preview["file"]["sha256"] == hashlib.sha256(b"own answer").hexdigest()
    receipt = await service.submit(preview["preview_id"])
    assert receipt["file_id"] == "file-receipt" and qq.uploaded_bytes == b"own answer"
    staged_path = Path(qq.writes[0][1]["file"])
    assert staged_path != path and not staged_path.exists()
    assert qq.writes[0][1]["name"] == "作业.pdf" and path.read_bytes() == b"own answer"
    assert receipt["chat_message_published"] is None


async def test_changed_bytes_with_preserved_identity_never_send(submission):
    qq, service = submission
    path = service.root / "answer.txt"
    path.write_bytes(b"before")
    old = path.stat()
    preview = await service.prepare("学习群", file_path=str(path))
    path.write_bytes(b"after!")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(QQFileError) as caught:
        await service.submit(preview["preview_id"])
    assert caught.value.code == "SUBMISSION_CHANGED" and not qq.writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


@pytest.mark.parametrize(
    "change,code",
    [
        ("owner", "ACCOUNT_CHANGED"),
        ("group", "GROUP_CHANGED"),
        ("backend", "BACKEND_CHANGED"),
        ("expiry", "EXPIRED_REFERENCE"),
    ],
)
async def test_preview_binds_identity_destination_backend_and_expiry(submission, change, code):
    qq, service = submission
    preview = await service.prepare("学习群", text="自己的答案")
    if change == "owner":
        qq.owner = "90002"
    elif change == "group":
        qq.groups[0]["group_name"] = "改名群"
    elif change == "backend":
        service.files.settings = replace(service.settings, backend="snowluma")
    else:
        with sqlite3.connect(service.files.store.path) as db:
            db.execute("UPDATE submissions SET expires=0")
    with pytest.raises(QQFileError) as caught:
        await service.submit(preview["preview_id"])
    assert caught.value.code == code and not qq.writes


async def test_uncertain_outcome_is_persistent_and_never_retried(submission):
    qq, service = submission
    preview = await service.prepare("学习群", text="自己的答案")
    qq.failure = QQFileError("TIMEOUT", "PRIVATE_UPSTREAM_ERROR")
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == "outcome_unknown" and receipt["error_code"] == "TIMEOUT"
    assert "PRIVATE_UPSTREAM_ERROR" not in str(receipt)
    restarted = SubmissionService(FileService(qq, service.settings))
    assert await restarted.submit(preview["preview_id"]) == receipt
    assert restarted.receipt(preview["preview_id"]) == receipt and len(qq.writes) == 1


async def test_two_instances_claim_only_one_attempt(submission):
    qq, service = submission
    preview = await service.prepare("学习群", text="自己的答案")
    other = SubmissionService(FileService(qq, service.settings))
    await asyncio.gather(service.submit(preview["preview_id"]), other.submit(preview["preview_id"]))
    assert len(qq.writes) == 1


async def test_cancelled_transport_saves_unknown_and_cleans_snapshot(submission):
    qq, service = submission
    (service.root / "answer.txt").write_bytes(b"answer")
    preview = await service.prepare("学习群", file_path="answer.txt")
    qq.waiting = asyncio.Event()
    task = asyncio.create_task(service.submit(preview["preview_id"]))
    await qq.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.receipt(preview["preview_id"])["status"] == "outcome_unknown"
    assert not Path(qq.writes[0][1]["file"]).exists()


async def test_arbitrary_paths_ambiguous_groups_and_double_payload_rejected(submission, tmp_path):
    qq, service = submission
    outside = tmp_path / "private.env"
    outside.write_text("PRIVATE", encoding="utf-8")
    for path in (str(outside), "../private.env", "https://example.com/answer"):
        with pytest.raises(QQFileError):
            await service.prepare("学习群", file_path=path)
    with pytest.raises(QQFileError):
        await service.prepare("学习群", text="text", file_path="answer.txt")
    qq.groups.append({"group_id": 10002, "group_name": "学习群"})
    ambiguous = await service.prepare("学习群", text="text")
    assert ambiguous["needs_group_selection"] is True and not qq.writes


async def test_opt_in_limits_allowlist_to_two_submission_actions(settings):
    requests = []
    client = OneBotClient(
        replace(settings, enable_submissions=True),
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(
                    200,
                    json={"status": "ok", "retcode": 0, "data": {"message_id": 1}},
                )
            )
        ),
    )
    try:
        await client.call("send_group_msg", group_id="10001", message=[])
        await client.call("upload_group_file", group_id="10001", file="local")
        with pytest.raises(QQFileError):
            await client.call("delete_group_file", group_id="10001", file_id="x")
        assert len(requests) == 2
    finally:
        await client.close()


async def test_disabled_submission_still_allows_prepare(submission):
    qq, active = submission
    service = SubmissionService(FileService(qq, replace(active.settings, enable_submissions=False)))
    preview = await service.prepare("学习群", text="自己的答案")
    assert preview["submissions_enabled"] is False
    with pytest.raises(QQFileError) as caught:
        await service.submit(preview["preview_id"])
    assert caught.value.code == "SUBMISSIONS_DISABLED" and not qq.writes


async def test_crash_state_retains_only_pending_receipt_not_answer(submission):
    qq, service = submission
    preview = await service.prepare("学习群", text="PRIVATE_OWN_ANSWER")
    pending = {
        "ok": True,
        "preview_id": preview["preview_id"],
        "group": preview["group"],
        "status": "outcome_unknown",
        "retry_allowed": False,
    }
    assert service.files.store.claim_submission(preview["preview_id"], pending)
    with sqlite3.connect(service.files.store.path) as db:
        assert db.execute("SELECT value FROM submissions").fetchone()[0] == "{}"
    restarted = SubmissionService(FileService(qq, service.settings))
    assert await restarted.submit(preview["preview_id"]) == pending and not qq.writes


async def test_cancellation_during_snapshot_reaps_thread_and_removes_file(submission, monkeypatch):
    qq, service = submission
    (service.root / "answer.txt").write_bytes(b"answer")
    preview = await service.prepare("学习群", file_path="answer.txt")
    entered, finish = threading.Event(), threading.Event()
    copy = service._file

    def stalled(*args, **kwargs):
        entered.set()
        finish.wait(timeout=3)
        return copy(*args, **kwargs)

    monkeypatch.setattr(service, "_file", stalled)
    task = asyncio.create_task(service.submit(preview["preview_id"]))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not qq.writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
async def test_submission_junction_cannot_upload_outside_directory(submission, tmp_path):
    qq, service = submission
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private")
    junction = service.root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, check=True
    )
    try:
        with pytest.raises(QQFileError) as caught:
            await service.prepare("学习群", file_path="junction/private.txt")
        assert caught.value.code == "SUBMISSION_PATH" and not qq.writes
    finally:
        junction.rmdir()


async def test_empty_and_oversized_submission_file_are_rejected(submission):
    qq, active = submission
    service = SubmissionService(FileService(qq, replace(active.settings, max_submission_bytes=3)))
    for contents in (b"", b"1234"):
        (service.root / "answer.txt").write_bytes(contents)
        with pytest.raises(QQFileError) as caught:
            await service.prepare("学习群", file_path="answer.txt")
        assert caught.value.code == "SUBMISSION_SIZE" and not qq.writes
