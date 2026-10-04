from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from qq_file_mcp.downloads import DownloadedFiles
from qq_file_mcp.errors import QQFileError


def make_pdf(path, texts, password=None):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(f"BT /F1 12 Tf 30 700 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    writer.write(path)


@pytest.fixture
def local(settings):
    settings.download_dir.mkdir()
    return DownloadedFiles(settings)


async def test_discover_existing_files_in_subfolders_and_read_without_qq(local):
    folder = local.root / "演示"
    folder.mkdir()
    path = folder / "Ｌｅｃｔ３笔记.txt"
    path.write_text("计算物理\n数值积分\n蒙特卡洛\n", encoding="utf-8")
    found = await local.list_files("lect3")
    assert found["total_matches"] == 1
    entry = found["files"][0]
    assert entry["relative_path"] == "演示/Ｌｅｃｔ３笔记.txt"
    result = await local.read(entry["file_id"], start=2, count=1)
    assert result["unit"] == "line"
    assert result["units"] == [{"index": 2, "text": "数值积分"}]
    assert result["next_read"]["start"] == 3
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["content_is_untrusted"] is True


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
async def test_chinese_text_encodings(local, encoding):
    path = local.root / "中文.md"
    path.write_bytes("资料一\n资料二".encode(encoding))
    ref = local.register(path)
    result = await local.read(ref)
    assert [unit["text"] for unit in result["units"]] == ["资料一", "资料二"]
    assert result["next_read"] is None


async def test_pdf_page_ranges_and_mid_page_continuation_never_skip_text(local):
    path = local.root / "lecture.pdf"
    make_pdf(path, ["abcdefghijklmnopqrstuvwxyz", "Page two", "Page three"])
    ref = local.register(path)
    first = await local.read(ref, count=2, max_chars=10)
    assert first["total_units"] == 3
    assert first["unit"] == "page"
    assert first["units"] == [{"index": 1, "text": "abcdefghij"}]
    assert first["truncated"] is True
    fragments = []
    result = first
    for _ in range(10):
        fragments.extend(item["text"] for item in result["units"])
        if result["next_read"] is None:
            break
        result = await local.read(ref, max_chars=10, **result["next_read"])
    assert "".join(fragments) == "abcdefghijklmnopqrstuvwxyzPage twoPage three"
    assert result["next_read"] is None
    page_two = await local.read(ref, start=2, count=1)
    assert page_two["units"] == [{"index": 2, "text": "Page two"}]
    assert page_two["next_read"]["start"] == 3


async def test_text_long_line_continuation(local):
    path = local.root / "long.txt"
    path.write_text("abcdefg\nhij", encoding="utf-8")
    ref = local.register(path)
    first = await local.read(ref, count=2, max_chars=3)
    assert first["next_read"]["start"] == 1
    assert first["next_read"]["char_offset"] == 3
    second = await local.read(ref, max_chars=20, **first["next_read"])
    assert second["units"] == [{"index": 1, "text": "defg"}, {"index": 2, "text": "hij"}]


async def test_empty_or_image_only_pdf_reports_no_text(local):
    path = local.root / "scan.pdf"
    make_pdf(path, [""])
    result = await local.read(local.register(path))
    assert result["units"] == [{"index": 1, "text": ""}]
    assert result["warnings"][0]["code"] == "NO_EXTRACTABLE_TEXT"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("encrypted", "PDF_ENCRYPTED"),
        ("broken", "READ_FORMAT"),
        ("binary", "READ_FORMAT"),
        ("unsupported", "UNSUPPORTED_FORMAT"),
    ],
)
async def test_bad_or_unsupported_files_are_explicit(local, mode, code):
    path = local.root / ("data.zip" if mode == "unsupported" else "data.pdf")
    if mode == "encrypted":
        make_pdf(path, ["secret"], password="password")
    elif mode == "binary":
        path = local.root / "data.txt"
        path.write_bytes(b"abc\x00\x01")
    else:
        path.write_bytes(b"not a document")
    with pytest.raises(QQFileError) as error:
        await local.read(local.register(path))
    assert error.value.code == code


async def test_changes_missing_and_expired_references(local):
    path = local.root / "data.txt"
    path.write_text("old", encoding="utf-8")
    ref = local.register(path)
    path.write_text("new longer", encoding="utf-8")
    with pytest.raises(QQFileError, match="变化") as error:
        await local.read(ref)
    assert error.value.code == "FILE_CHANGED"
    ref = local.register(path)
    path.unlink()
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "LOCAL_FILE_MISSING"
    path.write_text("new", encoding="utf-8")
    ref = local.register(path)
    with local.store._connect() as db:
        db.execute("UPDATE items SET expires=0 WHERE id=?", (ref,))
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "EXPIRED_REFERENCE"


async def test_outside_root_and_link_substitution_are_rejected(local, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(QQFileError) as error:
        local.register(outside)
    assert error.value.code == "LOCAL_PATH_DENIED"
    path = local.root / "data.txt"
    path.write_text("approved", encoding="utf-8")
    ref = local.register(path)
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this Windows account")
    assert (await local.list_files())["files"] == []
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "LOCAL_PATH_DENIED"


async def test_listing_pagination_and_explicit_scan_cap(local):
    for i in range(6):
        (local.root / f"{i}.txt").write_text(str(i), encoding="utf-8")
    first = await local.list_files(limit=2)
    second = await local.list_files(limit=2, offset=first["next_offset"])
    assert len(first["files"]) == len(second["files"]) == 2
    assert {x["relative_path"] for x in first["files"]}.isdisjoint(
        {x["relative_path"] for x in second["files"]}
    )
    capped = DownloadedFiles(replace(local.settings, directory_limit=3))
    listing = await capped.list_files()
    assert len(listing["files"]) == 3
    assert listing["coverage"]["complete"] is False
    assert listing["warnings"][0]["code"] == "LOCAL_SCAN_LIMIT"


async def test_size_limit_and_invalid_ranges(local):
    path = local.root / "data.txt"
    path.write_text("data", encoding="utf-8")
    small = DownloadedFiles(replace(local.settings, max_read_bytes=3))
    with pytest.raises(QQFileError) as error:
        await small.read(small.register(path))
    assert error.value.code == "FILE_TOO_LARGE"
    ref = local.register(path)
    for kwargs in [
        {"start": 0},
        {"count": 501},
        {"max_chars": 20001},
        {"char_offset": -1},
        {"start": 2},
        {"char_offset": 100},
    ]:
        with pytest.raises(QQFileError) as error:
            await local.read(ref, **kwargs)
        assert error.value.code == "INVALID_INPUT"


async def test_parser_timeout_kills_and_reaps_actual_worker(local):
    path = local.root / "data.txt"
    path.write_text("data", encoding="utf-8")
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -9)

    async def stalled(_):
        await asyncio.sleep(10)

    process.communicate.side_effect = stalled
    timed = DownloadedFiles(replace(local.settings, read_timeout=0.02))
    with patch("qq_file_mcp.downloads.asyncio.create_subprocess_exec", return_value=process):
        with pytest.raises(QQFileError) as error:
            await timed.read(timed.register(path))
    assert error.value.code == "READ_TIMEOUT"
    process.wait.assert_awaited_once()
    assert process.returncode == -9


async def test_download_expected_hash_detects_same_stat_content_substitution(local):
    path = local.root / "data.txt"
    path.write_text("old", encoding="utf-8")
    original = path.stat()
    ref = local.register(path, hashlib.sha256(b"old").hexdigest())
    path.write_text("new", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "FILE_CHANGED"


async def test_real_timeout_reaps_child_interpreter(local, monkeypatch):
    path = local.root / "data.txt"
    path.write_text("data", encoding="utf-8")
    launch = asyncio.create_subprocess_exec
    children = []

    async def start_stalled_worker(executable, _worker, **kwargs):
        if os.name == "nt":
            assert ".venv" not in executable
        child = await launch(executable, "-c", "import time; time.sleep(10)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        "qq_file_mcp.downloads.asyncio.create_subprocess_exec", start_stalled_worker
    )
    timed = DownloadedFiles(replace(local.settings, read_timeout=0.1))
    with pytest.raises(QQFileError) as error:
        await timed.read(timed.register(path))
    assert error.value.code == "READ_TIMEOUT"
    assert children[0].returncode is not None


async def test_forged_relative_path_cannot_escape_download_root(local, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    ref = local.store.put("local_file", {"relative_path": "../outside.txt", "identity": {}})
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "LOCAL_PATH_DENIED"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
async def test_windows_junction_is_not_scanned_or_read(local, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    junction = local.root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, check=True
    )
    try:
        listing = await local.list_files()
        assert listing["files"] == []
        assert listing["coverage"]["skipped_links_or_unreadable"] == 1
        with pytest.raises(QQFileError) as error:
            local.register(junction / "private.txt")
        assert error.value.code == "LOCAL_PATH_DENIED"
    finally:
        junction.rmdir()
