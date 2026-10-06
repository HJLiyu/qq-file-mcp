from __future__ import annotations

import hashlib
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_docx import make_docx

from qq_file_mcp.config import Settings
from qq_file_mcp.downloads import DownloadedFiles
from qq_file_mcp.server import create_server


async def test_registered_tools_have_correct_side_effect_annotations(settings):
    mcp = create_server(settings)
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "qq_status",
        "qq_find_groups",
        "qq_search_files",
        "qq_more_results",
        "qq_download_file",
        "qq_list_downloaded_files",
        "qq_read_downloaded_file",
        "qq_search_messages",
        "qq_read_message",
        "qq_prepare_submission",
        "qq_submit_submission",
        "qq_submission_receipt",
        "qq_search_homework",
        "qq_read_homework",
        "qq_download_homework_attachment",
        "qq_prepare_homework_submission",
        "qq_submit_homework",
    }
    for tool in tools:
        if tool.name == "qq_prepare_homework_submission":
            assert "file_path" in tool.inputSchema["properties"]
            assert tool.inputSchema["required"] == ["homework_ref"]
        assert tool.annotations.destructiveHint == (tool.name == "qq_submit_homework")
        assert tool.annotations.readOnlyHint == (
            tool.name
            not in {
                "qq_download_file",
                "qq_prepare_submission",
                "qq_submit_submission",
                "qq_download_homework_attachment",
                "qq_prepare_homework_submission",
                "qq_submit_homework",
            }
        )


async def test_real_stdio_handshake_and_safe_backend_error(tmp_path):
    env = dict(os.environ)
    env.update(
        {
            "NAPCAT_URL": "http://127.0.0.1:1",
            "NAPCAT_TOKEN": "test-secret",
            "ONEBOT_URL": "http://127.0.0.1:1",
            "ONEBOT_TOKEN": "test-secret",
            "QQ_FILE_BACKEND": "napcat",
            "QQ_FILE_STATE_DIR": str(tmp_path / "state"),
            "QQ_FILE_DOWNLOAD_DIR": str(tmp_path / "downloads"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "qq_file_mcp", "serve"], env=env
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "qq-file-mcp"
        tools = await session.list_tools()
        assert len(tools.tools) == 17
        submit = await session.call_tool("qq_submit_submission", {"preview_id": "unapproved"})
        assert submit.structuredContent["error"]["code"] == "SUBMISSIONS_DISABLED"
        result = await session.call_tool("qq_status", {})
        assert not result.isError
        assert result.structuredContent["ok"] is False
        assert result.structuredContent["error"]["code"] == "CONNECTION"
        # The same MCP process can read existing downloads while QQ is unreachable.
        folder = tmp_path / "downloads"
        folder.mkdir()
        (folder / "笔记.txt").write_text("计算物理\n数值方法", encoding="utf-8")
        listing = await session.call_tool("qq_list_downloaded_files", {"query": "笔记"})
        assert listing.structuredContent["ok"] is True
        ref = listing.structuredContent["files"][0]["file_id"]
        content = await session.call_tool("qq_read_downloaded_file", {"file_id": ref})
        assert content.structuredContent["ok"] is True
        assert content.structuredContent["units"][0] == {"index": 1, "text": "计算物理"}
        assert content.structuredContent["next_read"] is None
        make_docx(folder / "要求.docx", "<w:p><w:r><w:t>提交时间</w:t></w:r></w:p>")
        documents = await session.call_tool("qq_list_downloaded_files", {"query": "要求.docx"})
        entry = documents.structuredContent["files"][0]
        assert entry["can_read"] is True
        word = await session.call_tool("qq_read_downloaded_file", {"file_id": entry["file_id"]})
        assert word.structuredContent["ok"] is True
        assert word.structuredContent["unit"] == "block"
        assert word.structuredContent["units"] == [{"index": 1, "text": "提交时间"}]
        assert word.structuredContent["warnings"][0]["code"] == "DOCX_TEXT_ONLY"
        catalog = DownloadedFiles(
            Settings(token="test-secret", state_dir=tmp_path / "state", download_dir=folder)
        )
        catalog.register(
            folder / "笔记.txt",
            hashlib.sha256((folder / "笔记.txt").read_bytes()).hexdigest(),
            {
                "group_id": "10001",
                "group_name": "示例学习群",
                "source": "history",
                "publisher": {"user_id": "101", "name": "张老师", "nickname": "Teacher"},
                "time": 1700000000,
            },
        )
        filtered = await session.call_tool(
            "qq_list_downloaded_files",
            {
                "group": "示例学习群",
                "publisher": "张老师",
                "source": "history",
                "published_after": "2023-11-14T00:00:00+00:00",
            },
        )
        assert filtered.structuredContent["ok"] is True
        assert filtered.structuredContent["total_matches"] == 1
        assert (
            filtered.structuredContent["files"][0]["matched_provenance"][0]["publisher"]["user_id"]
            == "101"
        )
