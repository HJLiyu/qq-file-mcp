from __future__ import annotations

from copy import deepcopy

import pytest

from qq_file_mcp.errors import QQFileError
from qq_file_mcp.service import FileService


def file_item(name="课件.pdf", size=4, upload=1700000000, uploader=1, file_id="fresh-id"):
    return {
        "file_id": file_id,
        "file_name": name,
        "size": size,
        "upload_time": upload,
        "uploader": uploader,
    }


def message(index, file=False):
    # Intentionally non-monotonic short IDs; sorting by these would skip messages.
    msg_id = str((index * 7919) % 100003 + 1)
    return {
        "message_id": msg_id,
        "message_seq": msg_id,
        "real_seq": str(index),
        "group_id": 10001,
        "time": 1700000000 + index,
        "message": [
            {
                "type": "file",
                "data": {
                    "file": f"课件-{index}.pdf",
                    "file_size": 4,
                    "file_id": f"attachment-{index}",
                },
            }
        ]
        if file
        else [{"type": "text", "data": {"text": "PRIVATE_CHAT_BODY"}}],
    }


class FakeQQ:
    def __init__(self, settings):
        self.settings = settings
        self.owner = "90001"
        self.calls = []
        self.groups = [{"group_id": 10001, "group_name": "学习群"}]
        self.files = [file_item()]
        self.folders = []
        self.folder_files = {}
        self.members = [{"user_id": 1, "nickname": "老师", "card": "张老师"}]
        self.history = [message(i, file=i % 10 == 0) for i in range(1, 131)]
        self.repeat = False
        self.fail_history = False
        self.downloaded = None

    async def call(self, action, **params):
        self.calls.append((action, deepcopy(params)))
        if action == "get_status":
            return {"online": True}
        if action == "get_login_info":
            return {"user_id": self.owner}
        if action == "get_group_list":
            return deepcopy(self.groups)
        if action == "get_group_member_list":
            return deepcopy(self.members)
        if action == "get_group_root_files":
            return {
                "files": deepcopy(self.files[: params["file_count"]]),
                "folders": deepcopy(self.folders),
            }
        if action == "get_group_files_by_folder":
            return {
                "files": deepcopy(self.folder_files.get(params["folder_id"], [])),
                "folders": [],
            }
        if action == "get_group_msg_history":
            if self.fail_history:
                raise QQFileError("UPSTREAM", "历史不可用")
            rows = self.history
            anchor = params.get("message_seq")
            if anchor and not self.repeat:
                index = next(i for i, m in enumerate(rows) if m["message_id"] == anchor)
                # Pinned NapCat/QQ native behavior, verified live: False goes FORWARD.
                rows = rows[: index + 1] if params.get("reverse_order") else rows[index:]
            return {"messages": deepcopy(rows[-params["count"] :])}
        if action == "get_msg":
            return deepcopy(
                next(m for m in self.history if m["message_id"] == params["message_id"])
            )
        if action == "get_group_file_url":
            raise QQFileError("UPSTREAM", "Packet API unavailable in this fixture")
        if action == "get_file":
            self.downloaded = params["file_id"]
            path = self.settings.allowed_roots[0] / "cached-file"
            path.write_bytes(b"demo")
            return {"file": str(path)}
        raise AssertionError(action)


@pytest.fixture
def qq(settings):
    return FakeQQ(settings)


async def test_directory_search_beyond_50_and_folder(qq, settings):
    qq.files = [file_item(f"普通{i}.txt") for i in range(65)] + [file_item("ＡＩ课件.PDF")]
    qq.folders = [{"folder_id": "folder-1", "folder_name": "资料"}]
    qq.folder_files["folder-1"] = [file_item("AI课件-补充.pdf")]
    result = await FileService(qq, settings).search("学习群", "ai课件", "group_files")
    assert len(result["results"]) == 2
    assert result["coverage"]["group_files"]["files_scanned"] == 67
    assert result["results"][1]["folder"] == "资料"
    assert any(w["code"] == "NESTED_FOLDERS_UNVERIFIED" for w in result["warnings"])


async def test_ambiguous_group_never_queries_files(qq, settings):
    qq.groups.append({"group_id": 10002, "group_name": "学习群"})
    result = await FileService(qq, settings).search("学习群", "课件")
    assert result["needs_group_selection"]
    assert not any(action == "get_group_root_files" for action, _ in qq.calls)


async def test_explicit_wildcard_lists_all_files(qq, settings):
    qq.files = [file_item("first.pdf"), file_item("second.docx")]
    result = await FileService(qq, settings).search("10001", "*", "group_files")
    assert result["matched_in_this_scan"] == 2


async def test_publisher_only_query_resolves_member_and_matches_uploader(qq, settings):
    qq.files = [file_item("first.pdf", uploader=1), file_item("second.pdf", uploader=2)]
    service = FileService(qq, settings)
    found = await service.search("学习群", publisher="张老师", source="group_files")
    assert [x["file_name"] for x in found["results"]] == ["first.pdf"]
    assert found["results"][0]["publisher"] == {
        "user_id": "1",
        "name": "张老师",
        "nickname": "老师",
    }
    assert found["filters"]["publisher"] == "1"


async def test_duplicate_publisher_names_require_user_id_selection(qq, settings):
    qq.members.append({"user_id": 2, "card": "张老师", "nickname": "Other"})
    result = await FileService(qq, settings).search("10001", publisher="张老师")
    assert result["needs_publisher_selection"] is True
    assert len(result["publishers"]) == 2
    assert not any(action == "get_group_root_files" for action, _ in qq.calls)


async def test_numeric_publisher_is_exact_and_skips_member_lookup(qq, settings):
    qq.files = [file_item("one.pdf", uploader=1), file_item("ten.pdf", uploader=10)]
    result = await FileService(qq, settings).search("10001", publisher="1", source="group_files")
    assert [x["file_name"] for x in result["results"]] == ["one.pdf"]
    assert not any(action == "get_group_member_list" for action, _ in qq.calls)


async def test_history_sender_and_date_filters_bind_continuation(qq, settings):
    for m in qq.history:
        m["sender"] = {
            "user_id": 1 if int(m["real_seq"]) > 100 else 2,
            "card": "张老师" if int(m["real_seq"]) > 100 else "其他",
        }
    service = FileService(qq, settings)
    first = await service.search(
        "10001",
        publisher="1",
        source="history",
        max_messages=20,
        published_after="2023-11-14T22:15:20+00:00",
    )
    assert len(first["results"]) == 2
    assert first["results"][0]["publisher"]["user_id"] == "1"
    with pytest.raises(QQFileError) as error:
        await service.search(
            "10001",
            publisher="2",
            history_cursor=first["history_cursor"],
            published_after="2023-11-14T22:15:20+00:00",
        )
    assert error.value.code == "CURSOR_MISMATCH"
    with pytest.raises(QQFileError) as error:
        await service.search(
            "10001",
            publisher="1",
            history_cursor=first["history_cursor"],
            published_after="2023-11-14T22:15:21+00:00",
        )
    assert error.value.code == "CURSOR_MISMATCH"
    continued = await service.search(
        "10001",
        publisher="1",
        history_cursor=first["history_cursor"],
        published_after="2023-11-14T22:15:20+00:00",
    )
    assert continued["matched_in_this_scan"] == 0


async def test_historical_publisher_missing_from_members_matches_observed_name(qq, settings):
    for m in qq.history:
        m["sender"] = {"user_id": 3, "nickname": "以前的老师"}
    found = await FileService(qq, settings).search(
        "10001", publisher="以前的老师", source="history"
    )
    assert found["matched_in_this_scan"] == 13
    assert found["warnings"][0]["code"] == "PUBLISHER_NOT_IN_MEMBER_LIST"


async def test_missing_publisher_metadata_is_reported(qq, settings):
    qq.files = [file_item(uploader=0)]
    found = await FileService(qq, settings).search("10001", publisher="1", source="group_files")
    assert found["matched_in_this_scan"] == 0
    assert found["coverage"]["group_files"]["publisher_metadata_missing"] == 1


async def test_changed_history_sender_rejects_download(qq, settings):
    for m in qq.history:
        m["sender"] = {"user_id": 1}
    service = FileService(qq, settings)
    found = await service.search("10001", "课件-130", "history", publisher="1")
    qq.history[-1]["sender"]["user_id"] = 2
    with pytest.raises(QQFileError) as error:
        await service.download(found["results"][0]["result_id"])
    assert error.value.code == "FILE_CHANGED"


async def test_alias_lookup_failure_does_not_lose_successful_download(qq, settings):
    call = qq.call

    async def unavailable(action, **params):
        if action == "get_group_member_list":
            raise QQFileError("TIMEOUT", "Unavailable")
        return await call(action, **params)

    qq.call = unavailable
    service = FileService(qq, settings)
    found = await service.search("10001", "课件", "group_files")
    downloaded = await service.download(found["results"][0]["result_id"])
    assert downloaded["ok"] is True
    assert downloaded["publisher"]["user_id"] == "1"
    assert downloaded["warnings"][0]["code"] == "PUBLISHER_ALIAS_UNAVAILABLE"


async def test_unavailable_member_lookup_falls_back_to_observed_name(qq, settings):
    qq.files[0]["uploader_name"] = "张老师"
    call = qq.call

    async def unavailable(action, **params):
        if action == "get_group_member_list":
            raise QQFileError("UPSTREAM", "Unavailable")
        return await call(action, **params)

    qq.call = unavailable
    result = await FileService(qq, settings).search(
        "10001", publisher="张老师", source="group_files"
    )
    assert result["matched_in_this_scan"] == 1
    assert result["warnings"][0]["code"] == "PUBLISHER_LOOKUP_UNAVAILABLE"


async def test_download_returns_reference_that_can_be_read_without_qq(qq, settings):
    qq.files = [file_item("notes.txt")]
    service = FileService(qq, settings)
    found = await service.search("10001", "notes", "group_files")
    downloaded = await service.download(found["results"][0]["result_id"])
    before = len(qq.calls)
    content = await service.downloaded.read(downloaded["local_file_id"])
    assert content["units"] == [{"index": 1, "text": "demo"}]
    assert content["sha256"] == downloaded["sha256"]
    assert len(qq.calls) == before
    from qq_file_mcp.downloads import DownloadedFiles

    fresh = DownloadedFiles(settings)
    listing = await fresh.list_files(group="学习群", publisher="1")
    assert listing["total_matches"] == 1
    assert listing["files"][0]["provenance"][0]["source"] == "group_files"
    assert (await fresh.list_files(group="学习群", publisher="老师"))["total_matches"] == 1


async def test_history_continuation_no_gaps_or_duplicate_boundary(qq, settings):
    service = FileService(qq, settings)
    first = await service.search("学习群", "课件", "history", 100)
    assert first["coverage"]["history"]["messages_scanned"] == 100
    assert first["coverage"]["history"]["oldest_time"] == 1700000031
    second = await service.search("10001", "课件", "history", 100, first["history_cursor"])
    assert second["coverage"]["history"]["messages_scanned"] == 30
    names = [r["file_name"] for batch in (first, second) for r in batch["results"]]
    assert len(names) == len(set(names)) == 13
    assert "课件-10.pdf" in names
    assert second["history_cursor"] is None
    assert not second["coverage"]["history"]["exhaustive"]
    assert "PRIVATE_CHAT_BODY" not in service.store.path.read_bytes().decode(errors="ignore")


async def test_repeated_page_stops(qq, settings):
    qq.repeat = True
    result = await FileService(qq, settings).search("10001", "课件", "history", 1000)
    assert result["coverage"]["history"]["stop_reason"] == "repeated_page"
    assert result["coverage"]["history"]["messages_scanned"] == 100
    assert len([a for a, _ in qq.calls if a == "get_group_msg_history"]) == 2


async def test_filtered_native_events_do_not_break_small_scan_budget(settings):
    class FilteredQQ(FakeQQ):
        async def call(self, action, **params):
            data = await super().call(action, **params)
            if action == "get_group_msg_history" and params["count"] < 100:
                # Native count includes events the OneBot converter drops; short pages
                # can contain only the anchor even though older chat messages exist.
                data["messages"] = (
                    data["messages"][-1:] if params.get("message_seq") else data["messages"][2:]
                )
            return data

    result = await FileService(FilteredQQ(settings), settings).search("10001", "*", "history", 20)
    assert result["coverage"]["history"]["messages_scanned"] == 20
    assert result["history_cursor"]


async def test_one_source_failure_preserves_other_results(qq, settings):
    qq.fail_history = True
    result = await FileService(qq, settings).search("10001", "课件")
    assert len(result["results"]) == 1
    assert result["warnings"][0]["source"] == "history"
    assert result["coverage"]["history"]["stop_reason"] == "upstream_error"


async def test_directory_cap_reported(qq, configured):
    qq.files = [file_item(str(i)) for i in range(5)]
    result = await FileService(qq, configured(directory_limit=3)).search(
        "10001", "missing", "group_files"
    )
    assert not result["results"]
    assert result["coverage"]["group_files"]["status"] == "limited"
    assert not result["coverage"]["group_files"]["exhaustive"]


async def test_download_refreshes_id_and_never_overwrites(qq, settings):
    service = FileService(qq, settings)
    result = await service.search("10001", "课件", "group_files")
    qq.files[0]["file_id"] = "refreshed-id-after-restart"
    first = await service.download(result["results"][0]["result_id"])
    second = await service.download(result["results"][0]["result_id"])
    assert qq.downloaded == "refreshed-id-after-restart"
    assert first["path"] != second["path"]
    assert first["bytes"] == 4
    assert len(first["sha256"]) == 64


async def test_history_download_refreshes_message(qq, settings):
    service = FileService(qq, settings)
    result = await service.search("10001", "课件-130", "history", 20)
    await service.download(result["results"][0]["result_id"])
    assert qq.downloaded == "attachment-130"


async def test_ambiguous_file_refresh_refuses_download(qq, settings):
    service = FileService(qq, settings)
    result = await service.search("10001", "课件", "group_files")
    qq.files.append(file_item(file_id="different-file-identical-metadata"))
    with pytest.raises(QQFileError, match="唯一"):
        await service.download(result["results"][0]["result_id"])
    assert qq.downloaded is None


async def test_account_change_invalidates_reference(qq, settings):
    service = FileService(qq, settings)
    result = await service.search("10001", "课件", "group_files")
    qq.owner = "different-account"
    with pytest.raises(QQFileError, match="账号已切换"):
        await service.download(result["results"][0]["result_id"])


async def test_result_pagination_and_cursor_binding(qq, settings):
    qq.files = [file_item(f"课件{i}.pdf") for i in range(61)]
    service = FileService(qq, settings)
    result = await service.search("10001", "课件", "group_files")
    assert len(result["results"]) == 50
    remainder = await service.more_results(result["results_cursor"])
    assert len(remainder["results"]) == 11
    assert not remainder["results_cursor"]
    history = await service.search("10001", "课件", "history", 10)
    with pytest.raises(QQFileError, match="不一致"):
        await service.search("10001", "different", history_cursor=history["history_cursor"])
