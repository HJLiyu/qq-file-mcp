from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .client import OneBotClient
from .config import Settings
from .errors import QQFileError
from .service import FileService


async def run_command(args, settings):
    client = OneBotClient(settings)
    try:
        service = FileService(client, settings)
        if args.command == "doctor":
            return await service.status()
        if args.command == "groups":
            return await service.find_groups(args.query)
        if args.command == "search":
            return await service.search(
                args.group, args.filename, args.source, args.max_messages, args.history_cursor
            )
        if args.command == "more":
            return await service.more_results(args.cursor)
        if args.command == "download":
            return await service.download(args.result_id)
        if args.command == "downloads":
            return await service.downloaded.list_files(args.query, args.limit, args.offset)
        if args.command == "read":
            return await service.downloaded.read(
                args.file_id, args.start, args.count, args.char_offset, args.max_chars
            )
    finally:
        await client.close()


def main():
    # PowerShell pipes may select cp1252; JSON and MCP must preserve Chinese file names.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="QQ 群文件本地检索工具")
    parser.add_argument("--env-file", help="显式指定本地配置文件（不自动搜索 .env）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动 stdio MCP 服务")
    sub.add_parser("doctor", help="检查本地 QQ 接口和登录状态")
    groups = sub.add_parser("groups", help="查找账号已加入的群")
    groups.add_argument("query", nargs="?", default="")
    search = sub.add_parser("search", help="实时搜索文件")
    search.add_argument("group")
    search.add_argument("filename")
    search.add_argument("--source", choices=["both", "group_files", "history"], default="both")
    search.add_argument("--max-messages", type=int, default=1000)
    search.add_argument("--history-cursor")
    more = sub.add_parser("more", help="读取其余候选")
    more.add_argument("cursor")
    download = sub.add_parser("download", help="下载选定结果")
    download.add_argument("result_id")
    downloads = sub.add_parser("downloads", help="离线查找已下载文件")
    downloads.add_argument("query", nargs="?", default="")
    downloads.add_argument("--limit", type=int, default=50)
    downloads.add_argument("--offset", type=int, default=0)
    read = sub.add_parser("read", help="离线读取 PDF 页或文本行")
    read.add_argument("file_id")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--count", type=int)
    read.add_argument("--char-offset", type=int, default=0)
    read.add_argument("--max-chars", type=int, default=12000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        settings = Settings.load(args.env_file)
        if args.command == "serve":
            from .server import create_server

            create_server(settings).run(transport="stdio")
            return
        result = asyncio.run(run_command(args, settings))
    except QQFileError as exc:
        result = exc.as_dict()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        logging.error("Command failure: %s", type(exc).__name__)
        result = {"ok": False, "error": {"code": "INTERNAL", "message": "本地工具发生错误。"}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
