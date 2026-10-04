"""On-demand textual evidence; persist message identities, never chat bodies."""

from __future__ import annotations

import hashlib
import json
import time

from .errors import QQFileError
from .files import normalize
from .metadata import FileFilters
from .service import attachments, message_key, number


def message_content(message: dict) -> dict:
    segments = message.get("message")
    if not isinstance(segments, list):
        raise QQFileError("MESSAGE_FORMAT", "请将 OneBot 消息格式配置为 array。")
    text, mentions, replies, unsupported = [], [], [], []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            continue
        data, kind = segment["data"], segment.get("type")
        if kind == "text":
            text.append(str(data.get("text") or ""))
        elif kind == "at":
            mentions.append(str(data.get("qq") or ""))
        elif kind == "reply":
            replies.append(str(data.get("id") or ""))
        elif kind != "file":
            unsupported.append(str(kind))
    files = [
        {
            "index": index,
            "name": str(data.get("file", data.get("name", ""))),
            "size": number(data.get("file_size", data.get("size"))),
        }
        for index, data in attachments(message)
    ]
    return {
        "text": "".join(text),
        "mentions": mentions,
        "reply_to": replies,
        "files": files,
        "unsupported_segments": unsupported,
    }


def content_hash(content: dict) -> str:
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class MessageService:
    def __init__(self, files):
        self.files = files

    def _project(self, base, message, keyword):
        if str(message.get("group_id", "")) != base["group_id"]:
            raise QQFileError("PROTOCOL", "接口返回了不同群的消息，已停止读取。")
        content = message_content(message)
        publisher = self.files._publisher(base, message)
        stamp = number(message.get("time"))
        if not base["_filters"].matches(publisher, stamp):
            return None
        searchable = content["text"] + "\n" + "\n".join(f["name"] for f in content["files"])
        if keyword.strip() != "*" and normalize(keyword) not in normalize(searchable):
            return None
        ref = self.files.store.put(
            "message",
            {
                "owner": base["owner"],
                "backend": base["backend"],
                "group_id": base["group_id"],
                "message_id": message_key(message),
                "sender_id": publisher["user_id"],
                "time": stamp,
                "content_hash": content_hash(content),
            },
        )
        file_results = [
            self.files._candidate(
                {
                    **base,
                    "source": "history",
                    "file_name": item["name"],
                    "size": item["size"],
                    "message_id": message_key(message),
                    "segment_index": item["index"],
                    "publisher": publisher,
                    "time": stamp,
                }
            )
            for item in content["files"]
        ]
        return {
            **content,
            "files": file_results,
            "message_ref": ref,
            "message_id": message_key(message),
            "sender": publisher,
            "time": stamp,
        }

    async def search(
        self,
        group,
        keyword="",
        publisher="",
        published_after=None,
        published_before=None,
        max_messages=1000,
        history_cursor=None,
        max_chars=12000,
    ):
        filters = FileFilters.create(publisher, published_after, published_before)
        if (
            not isinstance(group, str)
            or not group.strip()
            or len(group) > 200
            or not isinstance(keyword, str)
            or len(keyword) > 200
            or (
                not keyword.strip()
                and not filters.publisher
                and filters.published_after is None
                and filters.published_before is None
            )
            or not isinstance(max_messages, int)
            or not 1 <= max_messages <= 5000
            or not isinstance(max_chars, int)
            or not 1000 <= max_chars <= 20000
        ):
            raise QQFileError(
                "INPUT", "请指定群、关键词/发布人/时间；消息数1–5000，字符数1000–20000。"
            )
        async with self.files.lock:
            started = time.monotonic()
            owner = await self.files._owner()
            selected, choices = await self.files._group(group)
            if choices:
                return {"ok": True, "needs_group_selection": True, "groups": choices}
            warnings = []
            profile, choices = await self.files._resolve_publisher(
                selected["group_id"], filters, warnings
            )
            if choices:
                return {
                    "ok": True,
                    "needs_publisher_selection": True,
                    "publishers": choices,
                    "group": selected,
                }
            resume = None
            if history_cursor:
                resume = self.files.store.get(history_cursor, "message_history")
                self.files._check_backend(resume)
                if (
                    resume["owner"] != owner
                    or resume["group_id"] != selected["group_id"]
                    or resume["keyword"] != normalize(keyword)
                    or resume["filters"] != filters.as_dict()
                ):
                    raise QQFileError("CURSOR_MISMATCH", "继续查询条件、群或账号发生变化。")
            base = {
                **selected,
                "owner": owner,
                "backend": self.files.settings.backend,
                "_filters": filters,
                "_publisher_profile": profile,
            }
            results, coverage = [], {}
            cursor = await self.files._history(
                base,
                keyword,
                results,
                warnings,
                coverage,
                time.monotonic() + self.files.settings.search_timeout,
                max_messages,
                resume,
                projector=self._project,
                max_chars=max_chars,
            )
            return {
                "ok": True,
                "group": selected,
                "messages": results,
                "filters": filters.as_dict(),
                "history_cursor": cursor,
                "coverage": coverage,
                "warnings": warnings,
                "native_homework_included": False,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "message": (
                    "仅返回实际可取回的消息；图片/语音/转发未解析，不能保证找到所有补充要求。"
                ),
            }

    async def read(self, message_ref, char_offset=0, max_chars=12000):
        if (
            not isinstance(char_offset, int)
            or char_offset < 0
            or not isinstance(max_chars, int)
            or not 1 <= max_chars <= 20000
        ):
            raise QQFileError("INPUT", "字符偏移必须非负；单次字符数1–20000。")
        async with self.files.lock:
            saved = self.files.store.get(message_ref, "message")
            self.files._check_backend(saved)
            if await self.files._owner() != saved["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已切换，请重新搜索消息。")
            message = await self.files.client.call("get_msg", message_id=saved["message_id"])
            if not isinstance(message, dict):
                raise QQFileError("STALE_MESSAGE", "消息已不可用，请重新搜索。")
            content = message_content(message)
            sender = self.files._publisher({}, message)
            if (
                str(message.get("group_id", "")) != saved["group_id"]
                or message_key(message) != saved["message_id"]
                or sender["user_id"] != saved["sender_id"]
                or number(message.get("time")) != saved["time"]
                or content_hash(content) != saved["content_hash"]
            ):
                raise QQFileError("STALE_MESSAGE", "消息身份或内容已改变，请重新搜索。")
            text = content.pop("text")
            end = min(len(text), char_offset + max_chars)
            return {
                "ok": True,
                **content,
                "group_id": saved["group_id"],
                "message_id": saved["message_id"],
                "sender": sender,
                "time": saved["time"],
                "text": text[char_offset:end],
                "total_text_chars": len(text),
                "next_read": {
                    "message_ref": message_ref,
                    "char_offset": end,
                    "max_chars": max_chars,
                }
                if end < len(text)
                else None,
            }
