from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from test_homework import Credentials, Native, reference
from test_service import FakeQQ

from qq_file_mcp.errors import QQFileError
from qq_file_mcp.homework import HomeworkService
from qq_file_mcp.homework_client import NativeHomeworkClient, document_url
from qq_file_mcp.service import FileService
from qq_file_mcp.submissions import SubmissionService

URL = "http://grouphw-1251316161.file.myqcloud.com/test.pdf"
PDF = b"%PDF-1.4\nprivate-own-answer\n%%EOF"


class FileNative(Native):
    def __init__(self):
        super().__init__()
        self.uploads = []
        self.verifications = []
        self.upload_failure = False
        self.verify_failure_at = None
        self.upload_waiting = None
        self.after_upload = None

    async def upload_document(self, owner, path, metadata):
        self.uploads.append((owner, Path(path), Path(path).read_bytes(), metadata))
        if self.upload_waiting:
            self.upload_waiting.set()
            await asyncio.Event().wait()
        if self.upload_failure:
            raise QQFileError("HOMEWORK_TIMEOUT", "private signed url")
        if self.after_upload:
            self.after_upload()
        return URL

    async def verify_document(self, url, metadata):
        self.verifications.append(url)
        if len(self.verifications) == self.verify_failure_at:
            raise QQFileError("HOMEWORK_FILE_VERIFY", "private signed url")
        assert metadata["sha256"] == hashlib.sha256(self.uploads[-1][2]).hexdigest()

    async def submit_document(self, group_id, homework_id, owner, text, metadata, url):
        self.writes.append((group_id, homework_id, owner, text, metadata["sha256"]))
        if self.failure:
            raise self.failure
        items = [{"type": "str", "text": text}] if text else []
        items.append(
            {"type": "file", "name": metadata["name"], "size": str(metadata["size"]), "url": url}
        )
        if not self.match:
            items[-1]["name"] = "other.pdf"
        self.rows[int(homework_id)]["feedback"].update(
            status=2,
            fb_content={
                "main": [{"id": "native-file-record", "uin": int(owner), "text": {"c": items}}]
            },
        )


@pytest.fixture
def documents(settings, tmp_path):
    settings = replace(
        settings,
        backend="snowluma",
        enable_submissions=True,
        submission_dir=tmp_path / "own-answers",
    )
    settings.submission_dir.mkdir()
    path = settings.submission_dir / "自己的答案.pdf"
    path.write_bytes(PDF)
    native = FileNative()
    service = HomeworkService(FileService(FakeQQ(settings), settings), native)
    return native, service, path


@pytest.mark.parametrize("suffix", [".pdf", ".doc", ".docx", ".PDF"])
async def test_file_preview_exact_metadata_and_no_upload(documents, suffix):
    native, service, path = documents
    path = path.rename(path.with_suffix(suffix))
    preview = await service.prepare(await reference(service), file_path=path.name, text=" 附言 ")
    assert preview["text"] == "附言" and preview["destination"] == "native_homework"
    assert preview["file_upload_experimental"] is True
    assert preview["file"] == {
        "path": str(path),
        "name": path.name,
        "size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }
    assert not native.uploads and not native.writes


async def test_native_file_verified_only_after_own_record_and_binary_hash(documents):
    native, service, path = documents
    preview = await service.prepare(await reference(service), file_path=str(path))
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == "verified_native_submission"
    assert receipt["native_submission_verified"] and receipt["native_file_bytes_verified"]
    assert receipt["native_file_uploaded"] and receipt["submission_attempted"]
    assert receipt["file"]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert len(native.verifications) == 2 and native.uploads[0][2] == PDF
    assert native.uploads[0][1] != path and not native.uploads[0][1].exists()
    saved = service.files.store.submission(preview["preview_id"])
    assert saved["value"] == {} and URL not in json.dumps(saved)
    assert "private-own-answer" not in json.dumps(saved)
    await service.submit(preview["preview_id"])
    assert len(native.uploads) == len(native.writes) == 1


@pytest.mark.parametrize("change", ["bytes", "remove", "owner", "disabled", "wrong_kind"])
async def test_preflight_changes_never_upload(documents, change):
    native, service, path = documents
    preview = await service.prepare(await reference(service), file_path=str(path))
    if change == "bytes":
        path.write_bytes(b"different bytes")
    elif change == "remove":
        path.unlink()
    elif change == "owner":
        service.files.client.owner = "90002"
    elif change == "disabled":
        service.settings = replace(service.settings, enable_submissions=False)
    elif change == "wrong_kind":
        with pytest.raises(QQFileError):
            await SubmissionService(service.files).submit(preview["preview_id"])
        assert not native.uploads
        return
    with pytest.raises(QQFileError):
        await service.submit(preview["preview_id"])
    assert not native.uploads and not native.writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


@pytest.mark.parametrize("suffix", [".pdf", ".doc", ".docx"])
@pytest.mark.parametrize("phase", ["prepare", "submit"])
@pytest.mark.parametrize("failure", ["locked", "read"])
async def test_local_document_io_failure_is_actionable_and_never_claims_or_uploads(
    documents, monkeypatch, suffix, phase, failure
):
    native, service, path = documents
    path = path.rename(path.with_suffix(suffix))
    ref = await reference(service)
    preview = await service.prepare(ref, file_path=str(path)) if phase == "submit" else None
    original_open = Path.open
    private_detail = "PRIVATE_FILESYSTEM_DETAIL"

    class FailingReader:
        def __init__(self, stream):
            self.stream = stream
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            self.reads += 1
            if self.reads > 1:
                raise OSError(5, private_detail, str(path))
            return self.stream.read(size)

    def unavailable_source(target, *args, **kwargs):
        if target == path:
            if failure == "locked":
                raise PermissionError(13, private_detail, str(path))
            return FailingReader(original_open(target, *args, **kwargs))
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unavailable_source)
    with pytest.raises(QQFileError) as caught:
        if preview:
            await service.submit(preview["preview_id"])
        else:
            await service.prepare(ref, file_path=str(path))
    assert caught.value.code == "SUBMISSION_IO"
    error = caught.value.as_dict()
    assert private_detail not in str(error) and str(path) not in str(error)
    assert not native.uploads and not native.writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))
    if preview:
        saved = service.files.store.submission(preview["preview_id"])
        assert saved["status"] == "ready" and saved["receipt"] is None
        assert saved["value"]["file"]["sha256"] == hashlib.sha256(PDF).hexdigest()
    else:
        with service.files.store._connect() as db:
            assert db.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0


@pytest.mark.parametrize("source", ["outside", "empty", "too_large", "unsupported", "link"])
async def test_native_file_root_size_format_and_link_guards(documents, tmp_path, source):
    native, service, path = documents
    if source == "outside":
        path = tmp_path / "elsewhere.pdf"
        path.write_bytes(PDF)
    elif source == "empty":
        path.write_bytes(b"")
    elif source == "too_large":
        service.files.settings = replace(service.settings, max_submission_bytes=1)
    elif source == "unsupported":
        path = path.rename(path.with_suffix(".exe"))
    elif source == "link":
        link = path.with_name("linked.pdf")
        try:
            link.symlink_to(path)
        except OSError:
            pytest.skip("symlink privileges unavailable")
        path = link
    with pytest.raises(QQFileError):
        await service.prepare(await reference(service), file_path=str(path))
    assert not native.uploads and not native.writes


@pytest.mark.parametrize(
    "mode,status,writes",
    [
        ("upload_timeout", "outcome_unknown", 0),
        ("changed_answer", "upload_only", 0),
        ("bad_uploaded_bytes", "upload_only", 0),
        ("submit_timeout", "outcome_unknown", 1),
        ("wrong_record", "accepted_by_native", 1),
        ("bad_readback_bytes", "accepted_by_native", 1),
    ],
)
async def test_upload_and_submission_failures_persist_honest_receipt(
    documents, mode, status, writes
):
    native, service, path = documents
    preview = await service.prepare(await reference(service), file_path=str(path))
    if mode == "upload_timeout":
        native.upload_failure = True
    elif mode == "changed_answer":
        native.after_upload = lambda: native.rows[101]["feedback"].update(comment_status=1)
    elif mode == "bad_uploaded_bytes":
        native.verify_failure_at = 1
    elif mode == "submit_timeout":
        native.failure = QQFileError("HOMEWORK_TIMEOUT", "private signed url")
    elif mode == "wrong_record":
        native.match = False
    elif mode == "bad_readback_bytes":
        native.verify_failure_at = 2
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == status and not receipt["native_submission_verified"]
    assert not receipt["native_file_bytes_verified"] and not receipt["retry_allowed"]
    assert "private signed url" not in str(receipt) and URL not in str(receipt)
    restored = HomeworkService(FileService(service.files.client, service.settings), native)
    assert await restored.submit(preview["preview_id"]) == receipt
    assert len(native.uploads) == 1 and len(native.writes) == writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


async def test_cancelled_upload_blocks_retry_and_cleans_snapshot(documents):
    native, service, path = documents
    preview = await service.prepare(await reference(service), file_path=str(path))
    native.upload_waiting = asyncio.Event()
    task = asyncio.create_task(service.submit(preview["preview_id"]))
    await native.upload_waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipt = await service.submit(preview["preview_id"])
    assert receipt["status"] == "outcome_unknown"
    assert len(native.uploads) == 1 and not native.writes
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


async def test_concurrent_file_submissions_have_one_upload(documents):
    native, service, path = documents
    preview = await service.prepare(await reference(service), file_path=str(path))
    other = HomeworkService(FileService(service.files.client, service.settings), native)
    results = await asyncio.gather(
        service.submit(preview["preview_id"]), other.submit(preview["preview_id"])
    )
    assert any(x["status"] == "verified_native_submission" for x in results)
    assert len(native.uploads) == len(native.writes) == 1
    assert not list((service.settings.state_dir / "submission-staging").glob("*"))


async def test_upload_transport_multipart_scope_csrf_and_exact_native_content(settings, tmp_path):
    creds, calls = Credentials(), []
    creds.value["token"] = 999  # Bridge token is not Tencent's current skey-based bkn.
    creds.value["cookies"] = "uin=o090001; skey=abc;"
    path = tmp_path / "answer.pdf"
    path.write_bytes(PDF)
    metadata = {"name": "answer.pdf", "size": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.host == "grouphw-1251316161.file.myqcloud.com"
            assert "cookie" not in request.headers and "authorization" not in request.headers
            return httpx.Response(200, content=PDF)
        assert request.url.host == "qun.qq.com" and "authorization" not in request.headers
        if request.url.path == "/cgi-bin/hw/util/file":
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert b'name="file"; filename="answer.pdf"' in request.content
            assert PDF in request.content and b'name="bkn"' in request.content
            assert b"193485963" in request.content  # Known DJB2 vector: abc.
            return httpx.Response(200, json={"retcode": 0, "data": {"url": URL}})
        form = parse_qs(request.content.decode())
        assert form["bkn"] == ["193485963"]
        assert json.loads(form["comment_info"][0]) == {
            "uin": "90001",
            "text": {
                "c": [
                    {"type": "str", "text": "说明"},
                    {"type": "file", "name": "answer.pdf", "size": str(len(PDF)), "url": URL},
                ]
            },
        }
        return httpx.Response(200, json={"retcode": 0, "data": {}})

    native = NativeHomeworkClient(
        creds, replace(settings, enable_submissions=True), httpx.MockTransport(handler)
    )
    url = await native.upload_document("90001", path, metadata)
    await native.verify_document(url, metadata)
    await native.submit_document("10001", "101", "90001", "说明", metadata, url)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "mode",
    ["bad_url", "group_url", "redirect", "too_large", "hash", "invalid_response", "disabled"],
)
async def test_file_transport_rejects_unknown_storage_and_unverified_bytes(
    settings, tmp_path, mode
):
    calls, creds = [], Credentials()
    path = tmp_path / "answer.pdf"
    path.write_bytes(PDF)
    metadata = {"name": path.name, "size": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            url = (
                "https://evil.example/private"
                if mode == "bad_url"
                else "https://qq.com/group-file"
                if mode == "group_url"
                else URL
            )
            data = {} if mode == "invalid_response" else {"url": url}
            return httpx.Response(200, json={"retcode": 0, "data": data})
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/private"})
        return httpx.Response(200, content=PDF + b"extra" if mode == "too_large" else b"different")

    native = NativeHomeworkClient(
        creds,
        replace(settings, enable_submissions=mode != "disabled"),
        httpx.MockTransport(handler),
    )
    with pytest.raises(QQFileError):
        url = await native.upload_document("90001", path, metadata)
        await native.verify_document(url, metadata)
    assert len(calls) == (
        0
        if mode == "disabled"
        else 1
        if mode in {"bad_url", "group_url", "invalid_response"}
        else 2
    )


@pytest.mark.parametrize(
    "cookies",
    [
        "uin=o090001; skey=abc; p_skey=other;",
        "uin=o090001; skey=; p_skey=abc;",
    ],
)
async def test_csrf_uses_session_cookie_not_bridge_token(settings, cookies):
    creds = Credentials()
    creds.value = {"cookies": cookies, "token": None}

    def handler(request):
        assert parse_qs(request.content.decode())["bkn"] == ["193485963"]
        return httpx.Response(200, json={"retcode": 0, "data": {}})

    native = NativeHomeworkClient(creds, settings, httpx.MockTransport(handler))
    await native.list("10001", 1, "90001")


async def test_upload_and_byte_readback_have_total_deadlines(settings, tmp_path):
    path = tmp_path / "answer.pdf"
    path.write_bytes(PDF)
    metadata = {"name": path.name, "size": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}
    calls = []

    async def hanging(request):
        calls.append(request)
        await asyncio.Event().wait()

    native = NativeHomeworkClient(
        Credentials(),
        replace(settings, enable_submissions=True, download_timeout=0.01),
        httpx.MockTransport(hanging),
    )
    with pytest.raises(QQFileError) as e:
        await native.upload_document("90001", path, metadata)
    assert e.value.code == "HOMEWORK_TIMEOUT"
    with pytest.raises(QQFileError) as e:
        await native.verify_document(URL, metadata)
    assert e.value.code == "HOMEWORK_FILE_VERIFY" and len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://grouphw-1251316161.file.myqcloud.com.evil.example/file.pdf",
        "https://other.file.myqcloud.com/file.pdf",
        "http://evil.example/file.pdf",
        "https://user@grouphw-1251316161.file.myqcloud.com/file.pdf",
        "https://grouphw-1251316161.file.myqcloud.com:444/file.pdf",
    ],
)
def test_native_document_host_is_exact(url):
    with pytest.raises(QQFileError):
        document_url(url)
