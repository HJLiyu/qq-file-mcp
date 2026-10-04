from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import pytest

from qq_file_mcp.downloads import DownloadedFiles
from qq_file_mcp.errors import QQFileError
from qq_file_mcp.metadata import FileFilters, parse_bound


def origin(
    group="学习群",
    group_id="10001",
    publisher="张老师",
    user_id="101",
    source="group_files",
    stamp=1700000000,
):
    return {
        "group_name": group,
        "group_id": group_id,
        "publisher": {"user_id": user_id, "name": publisher, "nickname": "Teacher"},
        "source": source,
        "time": stamp,
        "file_name": "notes.txt",
    }


def record(local, name, context):
    path = local.root / name
    path.write_text("notes", encoding="utf-8")
    return local.register(path, hashlib.sha256(b"notes").hexdigest(), context)


@pytest.fixture
def local(settings):
    settings.download_dir.mkdir()
    return DownloadedFiles(settings)


async def test_offline_combined_group_publisher_source_and_date_filters(local):
    record(local, "a.txt", origin())
    record(local, "b.txt", origin(group="工作群", group_id="20002"))
    record(local, "c.txt", origin(publisher="李老师", user_id="102"))
    record(local, "d.txt", origin(source="history"))
    result = await local.list_files(
        group="学习",
        publisher="张老师",
        source="group_files",
        published_after="2023-11-14T00:00:00+00:00",
        published_before="2023-11-15T00:00:00+00:00",
    )
    assert [x["file_name"] for x in result["files"]] == ["a.txt"]
    assert result["files"][0]["provenance_status"] == "recorded"
    assert result["files"][0]["matched_provenance"][0]["publisher"]["user_id"] == "101"
    by_id = await local.list_files(group="10001", publisher="101", source="history")
    assert [x["file_name"] for x in by_id["files"]] == ["d.txt"]
    assert (await local.list_files(group="100", publisher="10"))["files"] == []


async def test_provenance_survives_expiring_results_and_service_restart(local):
    ref = record(local, "notes.txt", origin())
    with local.store._connect() as db:
        db.execute("UPDATE items SET expires=0")
    restarted = DownloadedFiles(local.settings)
    result = await restarted.list_files(group="学习群", publisher="ＴＥＡＣＨＥＲ")
    assert result["total_matches"] == 1
    content = await restarted.read(result["files"][0]["file_id"])
    assert content["units"][0]["text"] == "notes"
    with pytest.raises(QQFileError):
        await restarted.read(ref)


async def test_unknown_or_changed_files_are_not_assigned_a_group(local):
    (local.root / "untracked.txt").write_text("notes", encoding="utf-8")
    ref = record(local, "known.txt", origin())
    listing = await local.list_files(group="学习群")
    assert listing["total_matches"] == 1
    assert listing["coverage"]["files_without_provenance"] == 1
    (local.root / "known.txt").write_text("changed contents", encoding="utf-8")
    listing = await local.list_files(group="学习群")
    assert listing["total_matches"] == 0
    assert listing["coverage"]["files_without_provenance"] == 2
    with pytest.raises(QQFileError) as error:
        await local.read(ref)
    assert error.value.code == "FILE_CHANGED"


async def test_filters_must_match_one_origin_not_mix_two_groups(local):
    record(local, "notes.txt", origin(group="甲群", group_id="1", publisher="Alice", user_id="11"))
    path = local.root / "notes.txt"
    local.register(
        path,
        hashlib.sha256(b"notes").hexdigest(),
        origin(group="乙群", group_id="2", publisher="Bob", user_id="22"),
    )
    assert (await local.list_files(group="甲群", publisher="Bob"))["files"] == []
    assert (await local.list_files(group="乙群", publisher="Bob"))["total_matches"] == 1
    assert len((await local.list_files())["files"][0]["provenance"]) == 2


async def test_persistent_metadata_whitelist_excludes_bodies_urls_and_tokens(local):
    context = {
        **origin(),
        "message": "PRIVATE_BODY",
        "url": "PRIVATE_URL",
        "token": "PRIVATE_TOKEN",
    }
    context["publisher"]["token"] = "NESTED_TOKEN"
    record(local, "notes.txt", context)
    db = sqlite3.connect(local.store.path)
    try:
        values = db.execute("SELECT value FROM downloads").fetchone()[0]
    finally:
        db.close()
    assert not any(
        secret in values
        for secret in ("PRIVATE_BODY", "PRIVATE_URL", "PRIVATE_TOKEN", "NESTED_TOKEN")
    )


@pytest.mark.parametrize(
    "after,before",
    [
        ("bad date", None),
        ("2026-10-01T12:00:00", None),
        ("2026-10-02", "2026-10-01"),
    ],
)
def test_invalid_or_ambiguous_time_filters(after, before):
    with pytest.raises(QQFileError) as error:
        FileFilters.create(published_after=after, published_before=before)
    assert error.value.code == "INPUT"


def test_date_bounds_are_inclusive_whole_local_days():
    low, high = parse_bound("2026-10-01"), parse_bound("2026-10-01", upper=True)
    assert datetime.fromtimestamp(low).hour == 0
    assert datetime.fromtimestamp(high).hour == 23
    filters = FileFilters(published_after=low, published_before=high)
    assert filters.matches({}, low) and filters.matches({}, high)
    assert not filters.matches({}, None)
