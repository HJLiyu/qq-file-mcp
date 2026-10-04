from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .client import OneBotClient
from .config import Settings
from .errors import QQFileError
from .homework import HomeworkService
from .messages import MessageService
from .service import FileService
from .submissions import SubmissionService


async def run_command(args, settings):
    client = OneBotClient(settings)
    try:
        service = FileService(client, settings)
        if args.command == "homework":
            return await HomeworkService(service).search(
                args.group, args.keyword, args.publisher, args.cursor
            )
        if args.command == "homework-read":
            return await HomeworkService(service).read(
                args.homework_ref, args.section, args.char_offset, args.max_chars
            )
        if args.command == "homework-download":
            return await HomeworkService(service).download(args.attachment_ref)
        if args.command == "homework-prepare":
            return await HomeworkService(service).prepare(
                args.homework_ref, args.text, args.replace_existing, args.file
            )
        if args.command == "homework-submit":
            return await HomeworkService(service).submit(args.preview_id)
        if args.command == "messages":
            return await MessageService(service).search(
                args.group,
                args.keyword,
                args.publisher,
                args.published_after,
                args.published_before,
                args.max_messages,
                args.history_cursor,
                args.max_chars,
            )
        if args.command == "message":
            return await MessageService(service).read(
                args.message_ref, args.char_offset, args.max_chars
            )
        if args.command == "prepare":
            return await SubmissionService(service).prepare(
                args.group, args.text or "", args.file or ""
            )
        if args.command == "submit":
            return await SubmissionService(service).submit(args.preview_id)
        if args.command == "receipt":
            return SubmissionService(service).receipt(args.preview_id)
        if args.command == "doctor":
            return await service.status()
        if args.command == "groups":
            return await service.find_groups(args.query)
        if args.command == "search":
            return await service.search(
                args.group,
                args.filename,
                args.source,
                args.max_messages,
                args.history_cursor,
                args.publisher,
                args.published_after,
                args.published_before,
            )
        if args.command == "more":
            return await service.more_results(args.cursor)
        if args.command == "download":
            return await service.download(args.result_id)
        if args.command == "downloads":
            return await service.downloaded.list_files(
                args.query,
                args.limit,
                args.offset,
                args.group,
                args.publisher,
                args.source,
                args.published_after,
                args.published_before,
            )
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
    parser = argparse.ArgumentParser(description="QQ 群资料、聊天要求与提交预览工具")
    parser.add_argument("--env-file", help="显式指定本地配置文件（不自动搜索 .env）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动 stdio MCP 服务")
    sub.add_parser("doctor", help="检查本地 QQ 接口和登录状态")
    groups = sub.add_parser("groups", help="查找账号已加入的群")
    groups.add_argument("query", nargs="?", default="")
    search = sub.add_parser("search", help="实时搜索文件")
    search.add_argument("group")
    search.add_argument("filename", nargs="?", default="")
    search.add_argument("--source", choices=["both", "group_files", "history"], default="both")
    search.add_argument("--max-messages", type=int, default=1000)
    search.add_argument("--history-cursor")
    search.add_argument("--publisher", default="")
    search.add_argument("--published-after")
    search.add_argument("--published-before")
    more = sub.add_parser("more", help="读取其余候选")
    more.add_argument("cursor")
    download = sub.add_parser("download", help="下载选定结果")
    download.add_argument("result_id")
    downloads = sub.add_parser("downloads", help="离线查找已下载文件")
    downloads.add_argument("query", nargs="?", default="")
    downloads.add_argument("--limit", type=int, default=50)
    downloads.add_argument("--offset", type=int, default=0)
    downloads.add_argument("--group", default="")
    downloads.add_argument("--publisher", default="")
    downloads.add_argument(
        "--source", choices=["", "group_files", "history", "native_homework"], default=""
    )
    downloads.add_argument("--published-after")
    downloads.add_argument("--published-before")
    read = sub.add_parser("read", help="离线读取 PDF 页或文本行")
    read.add_argument("file_id")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--count", type=int)
    read.add_argument("--char-offset", type=int, default=0)
    read.add_argument("--max-chars", type=int, default=12000)
    messages = sub.add_parser("messages", help="检索群聊天中的作业要求和补充说明")
    messages.add_argument("group")
    messages.add_argument("keyword", nargs="?", default="")
    messages.add_argument("--publisher", default="")
    messages.add_argument("--published-after")
    messages.add_argument("--published-before")
    messages.add_argument("--max-messages", type=int, default=1000)
    messages.add_argument("--history-cursor")
    messages.add_argument("--max-chars", type=int, default=12000)
    msg = sub.add_parser("message", help="续读检索返回的消息正文")
    msg.add_argument("message_ref")
    msg.add_argument("--char-offset", type=int, default=0)
    msg.add_argument("--max-chars", type=int, default=12000)
    prepare = sub.add_parser("prepare", help="生成自己的提交预览，不发送")
    prepare.add_argument("group")
    payload = prepare.add_mutually_exclusive_group(required=True)
    payload.add_argument("--text")
    payload.add_argument("--file", help="专用提交目录中的文件")
    submit = sub.add_parser("submit", help="发送已批准的准确预览；必须已启用提交")
    submit.add_argument("preview_id")
    receipt = sub.add_parser("receipt", help="离线查看提交回执，不发送")
    receipt.add_argument("preview_id")
    homework = sub.add_parser("homework", help="实时查询原生群作业和自己的状态")
    homework.add_argument("group")
    homework.add_argument("keyword", nargs="?", default="")
    homework.add_argument("--publisher", default="")
    homework.add_argument("--cursor")
    hw_read = sub.add_parser("homework-read", help="读取原生作业、自己答案或评语")
    hw_read.add_argument("homework_ref")
    hw_read.add_argument("--section", default="requirements")
    hw_read.add_argument("--char-offset", type=int, default=0)
    hw_read.add_argument("--max-chars", type=int, default=12000)
    hw_download = sub.add_parser("homework-download", help="下载返回的原生作业附件")
    hw_download.add_argument("attachment_ref")
    hw_prepare = sub.add_parser("homework-prepare", help="准备原生文字或PDF/Word答案，不发送")
    hw_prepare.add_argument("homework_ref")
    hw_prepare.add_argument("--text", default="")
    hw_prepare.add_argument("--file", default="")
    hw_prepare.add_argument("--replace-existing", action="store_true")
    hw_submit = sub.add_parser("homework-submit", help="一次提交已获准确授权的原生答案预览")
    hw_submit.add_argument("preview_id")
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
