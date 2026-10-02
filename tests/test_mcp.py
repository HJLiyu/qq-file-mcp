from __future__ import annotations

import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
    }
    for tool in tools:
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.readOnlyHint == (tool.name != "qq_download_file")


async def test_real_stdio_handshake_and_safe_backend_error(tmp_path):
    env = dict(os.environ)
    env.update(
        {
            "NAPCAT_URL": "http://127.0.0.1:1",
            "NAPCAT_TOKEN": "test-secret",
            "QQ_FILE_STATE_DIR": str(tmp_path / "state"),
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
        assert len(tools.tools) == 5
        result = await session.call_tool("qq_status", {})
        assert not result.isError
        assert result.structuredContent["ok"] is False
        assert result.structuredContent["error"]["code"] == "CONNECTION"
