from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import Settings
from .errors import QQFileError
from .files import copy_download, download_url, normalize
from .state import ResultStore


def number(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def matches(keyword: str, filename: str) -> bool:
    return keyword.strip() == "*" or normalize(keyword) in normalize(filename)


def fingerprint(item: dict) -> tuple:
    return (
        item.get("file_name"),
        number(item.get("size", item.get("file_size"))),
        str(item.get("upload_time", "")),
        str(item.get("uploader", "")),
    )


def message_key(message: dict) -> str:
    return str(message.get("message_id", message.get("message_seq", "")))


def message_order(message: dict) -> tuple:
    # message_id is a hash, NOT a chronological sequence. real_seq comes from QQ.
    return (number(message.get("time")), number(message.get("real_seq")))


def attachments(message: dict):
    segments = message.get("message", [])
    if not isinstance(segments, list):
        return
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or segment.get("type") != "file":
            continue
        data = segment.get("data", {})
        if isinstance(data, dict) and data.get("file_id"):
            yield index, data


class FileService:
    def __init__(self, client, settings: Settings, store: ResultStore | None = None):
        self.client = client
        self.settings = settings
        self.store = store or ResultStore(settings.state_dir, settings.result_ttl)
        # Serialize operations because the upstream file-ID cache can change on refresh.
        self.lock = asyncio.Lock()

    async def status(self) -> dict:
        status = await self.client.call("get_status")
        info = await self.client.call("get_login_info")
        return {
            "ok": True,
            "online": bool(status and status.get("online")),
            "account_id": str(info.get("user_id", "")),
            "download_dir": str(self.settings.download_dir),
            "transport": "local-stdio",
        }

    async def _owner(self) -> str:
        info = await self.client.call("get_login_info")
        owner = str((info or {}).get("user_id", ""))
        if not owner or owner == "0":
            raise QQFileError("NOT_LOGGED_IN", "请先在 NapCat 本地页面完成 QQ 登录。")
        return owner

    async def find_groups(self, query: str = "") -> dict:
        groups = await self.client.call("get_group_list", no_cache=True)
        if not isinstance(groups, list):
            raise QQFileError("PROTOCOL", "群列表格式不正确。")
        query = normalize(query)
        found = [
            {"group_id": str(g["group_id"]), "group_name": str(g.get("group_name", ""))}
            for g in groups
            if isinstance(g, dict)
            and g.get("group_id") is not None
            and (
                not query
                or query == str(g["group_id"])
                or query in normalize(str(g.get("group_name", "")))
            )
        ]
        return {"ok": True, "groups": found, "count": len(found)}

    async def _group(self, query: str) -> tuple[dict | None, list]:
        found = (await self.find_groups(query))["groups"]
        exact_id = [g for g in found if g["group_id"] == query.strip()]
        exact_name = [g for g in found if normalize(g["group_name"]) == normalize(query)]
        choices = exact_id or exact_name or found
        if not choices:
            raise QQFileError("GROUP_NOT_FOUND", "当前 QQ 账号中未找到这个群。")
        return (choices[0], []) if len(choices) == 1 else (None, choices)

    async def _call(self, deadline: float, action: str, **params):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QQFileError("TIMEOUT", "本轮搜索达到时间上限。")
        try:
            return await asyncio.wait_for(self.client.call(action, **params), remaining)
        except TimeoutError as exc:
            raise QQFileError("TIMEOUT", "本轮搜索达到时间上限。") from exc

    def _candidate(self, context: dict) -> dict:
        result_id = self.store.put("file", context)
        return {
            "result_id": result_id,
            "file_name": context["file_name"],
            "size": context["size"],
            "source": context["source"],
            "group_id": context["group_id"],
            "group_name": context["group_name"],
            "folder": context.get("folder_name"),
            "time": context.get("time"),
        }

    def _page(self, results: list, owner: str) -> tuple[list, str | None]:
        if len(results) <= 50:
            return results, None
        token = self.store.put("results", {"owner": owner, "items": results[50:]})
        return results[:50], token

    async def more_results(self, cursor: str) -> dict:
        async with self.lock:
            saved = self.store.get(cursor, "results")
            if await self._owner() != saved["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已切换，请重新搜索。")
            items, next_cursor = self._page(saved["items"], saved["owner"])
            return {"ok": True, "results": items, "results_cursor": next_cursor}

    async def search(
        self,
        group: str,
        filename: str,
        source: str = "both",
        max_messages: int = 1000,
        history_cursor: str | None = None,
    ) -> dict:
        if not group.strip() or not filename.strip() or len(filename) > 200:
            raise QQFileError("INPUT", "请提供群名或群号，以及 1–200 字符的文件名关键词。")
        if source not in {"both", "group_files", "history"} or not 1 <= max_messages <= 5000:
            raise QQFileError("INPUT", "搜索来源或消息数量不合法（消息数量范围 1–5000）。")
        async with self.lock:
            started = time.monotonic()
            owner = await self._owner()
            selected, choices = await self._group(group)
            if choices:
                return {
                    "ok": True,
                    "needs_group_selection": True,
                    "groups": choices,
                    "message": "匹配到多个群，请选择群号后重新搜索。",
                }
            assert selected is not None
            resume = None
            if history_cursor:
                resume = self.store.get(history_cursor, "history")
                if (
                    resume["owner"] != owner
                    or resume["group_id"] != selected["group_id"]
                    or resume["keyword"] != normalize(filename)
                ):
                    raise QQFileError("CURSOR_MISMATCH", "继续查询标识与账号、群或关键词不一致。")
                source = "history"
            base = {"owner": owner, **selected}
            results: list[dict] = []
            warnings: list[dict] = []
            coverage: dict = {}
            cursor = None
            # Each source has its own bounded budget so a large directory cannot starve history.
            if source in {"both", "group_files"}:
                await self._directories(
                    base,
                    filename,
                    results,
                    warnings,
                    coverage,
                    time.monotonic() + self.settings.search_timeout,
                )
            if source in {"both", "history"}:
                cursor = await self._history(
                    base,
                    filename,
                    results,
                    warnings,
                    coverage,
                    time.monotonic() + self.settings.search_timeout,
                    max_messages,
                    resume,
                )
            page, results_cursor = self._page(results, owner)
            return {
                "ok": True,
                "group": selected,
                "keyword": filename,
                "results": page,
                "matched_in_this_scan": len(results),
                "results_cursor": results_cursor,
                "history_cursor": cursor,
                "coverage": coverage,
                "warnings": warnings,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "message": "结果仅覆盖本次实际搜索范围；未找到不代表文件不存在。",
            }

    async def _directories(self, base, keyword, results, warnings, coverage, deadline):
        info = {
            "files_scanned": 0,
            "folders_scanned": 0,
            "folders_returned": 0,
            "per_directory_limit": self.settings.directory_limit,
            "status": "returned_directories_scanned",
            "exhaustive": False,
        }
        coverage["group_files"] = info
        try:
            root = await self._call(
                deadline,
                "get_group_root_files",
                group_id=base["group_id"],
                file_count=self.settings.directory_limit,
            )
            if not isinstance(root, dict) or not isinstance(root.get("files"), list):
                raise QQFileError("PROTOCOL", "群文件目录返回格式不正确。")
            folders = root.get("folders") or []
            info["folders_returned"] = len(folders)
            batches = [("", "根目录", root)]
            self._directory_matches(base, keyword, batches.pop()[2], "", "根目录", results, info)
            if len(root["files"]) + len(folders) >= self.settings.directory_limit:
                warnings.append({"source": "group_files", "code": "DIRECTORY_LIMIT"})
                info["status"] = "limited"
            for folder in folders[: self.settings.folder_limit]:
                folder_id = str(folder.get("folder_id", folder.get("folder", "")))
                if not folder_id:
                    warnings.append({"source": "group_files", "code": "INVALID_FOLDER"})
                    continue
                data = await self._call(
                    deadline,
                    "get_group_files_by_folder",
                    group_id=base["group_id"],
                    folder_id=folder_id,
                    file_count=self.settings.directory_limit,
                )
                info["folders_scanned"] += 1
                self._directory_matches(
                    base,
                    keyword,
                    data,
                    folder_id,
                    str(folder.get("folder_name", "")),
                    results,
                    info,
                )
                if len(data.get("files", [])) >= self.settings.directory_limit:
                    warnings.append({"source": "group_files", "code": "DIRECTORY_LIMIT"})
                    info["status"] = "limited"
            if len(folders) > self.settings.folder_limit:
                warnings.append({"source": "group_files", "code": "FOLDER_LIMIT"})
                info["status"] = "limited"
            if folders:
                warnings.append(
                    {
                        "source": "group_files",
                        "code": "NESTED_FOLDERS_UNVERIFIED",
                        "message": "当前接口未可靠暴露多层子目录，本次覆盖根目录和返回的一级目录。",
                    }
                )
        except QQFileError as exc:
            info["status"] = "partial"
            warnings.append({"source": "group_files", "code": exc.code, "message": str(exc)})

    def _directory_matches(self, base, keyword, data, folder_id, folder_name, results, info):
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise QQFileError("PROTOCOL", "群文件目录返回格式不正确。")
        for item in data["files"]:
            if not isinstance(item, dict):
                continue
            info["files_scanned"] += 1
            name = str(item.get("file_name", ""))
            if item.get("file_id") and matches(keyword, name):
                results.append(
                    self._candidate(
                        {
                            **base,
                            "source": "group_files",
                            "file_name": name,
                            "size": number(item.get("size", item.get("file_size"))),
                            "folder_id": folder_id,
                            "folder_name": folder_name,
                            "fingerprint": list(fingerprint(item)),
                            "time": number(item.get("upload_time")),
                        }
                    )
                )

    async def _history(self, base, keyword, results, warnings, coverage, deadline, budget, resume):
        anchor = resume.get("anchor") if resume else None
        seen = set(resume.get("seen", [])) if resume else set()
        recent_seen = list(resume.get("seen", [])) if resume else []
        scanned = 0
        times: list[int] = []
        stop = "message_limit"
        info = {
            "messages_scanned": 0,
            "requested_limit": budget,
            "exhaustive": False,
            "unsupported_segments": 0,
            "note": "仅包含当前会话可获取的消息。合并转发与在线文件不在本版范围内。",
        }
        coverage["history"] = info
        try:
            while scanned < budget:
                params = {
                    "group_id": base["group_id"],
                    # QQ counts raw events before conversion. Tiny requests can return
                    # only the anchor; fetch full pages, process only the scan budget.
                    "count": 100,
                    # In QQ getMsgsIncludeSelf, True walks toward OLDER messages.
                    "reverse_order": True,
                    "disable_get_url": True,
                    "parse_mult_msg": False,
                }
                if anchor:
                    params["message_seq"] = str(anchor)
                data = await self._call(deadline, "get_group_msg_history", **params)
                if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                    raise QQFileError("PROTOCOL", "聊天历史返回格式不正确。")
                messages = data["messages"]
                fresh = [
                    m
                    for m in messages
                    if isinstance(m, dict)
                    and message_key(m)
                    and message_key(m) not in seen
                    and message_key(m) != anchor
                ]
                if not fresh:
                    stop = "no_more_returned" if not messages else "repeated_page"
                    break
                # Process newest first; keep the oldest processed ID as an opaque continuation.
                fresh.sort(key=message_order, reverse=True)
                for message in fresh[: budget - scanned]:
                    key = message_key(message)
                    if key in seen:
                        continue
                    seen.add(key)
                    recent_seen.append(key)
                    scanned += 1
                    stamp = number(message.get("time"))
                    if stamp:
                        times.append(stamp)
                    segments = message.get("message", [])
                    if not isinstance(segments, list):
                        raise QQFileError(
                            "MESSAGE_FORMAT", "请将 OneBot messagePostFormat 配置为 array。"
                        )
                    info["unsupported_segments"] += sum(
                        1
                        for s in segments
                        if isinstance(s, dict) and s.get("type") in {"onlinefile", "forward"}
                    )
                    for index, item in attachments(message):
                        name = str(item.get("file", item.get("name", "")))
                        if matches(keyword, name):
                            results.append(
                                self._candidate(
                                    {
                                        **base,
                                        "source": "history",
                                        "file_name": name,
                                        "size": number(item.get("file_size", item.get("size"))),
                                        "message_id": key,
                                        "segment_index": index,
                                        "time": stamp,
                                    }
                                )
                            )
                    anchor = key
                info["messages_scanned"] = scanned
        except QQFileError as exc:
            stop = "time_limit" if exc.code == "TIMEOUT" else "upstream_error"
            warnings.append({"source": "history", "code": exc.code, "message": str(exc)})
        info.update(
            {
                "messages_scanned": scanned,
                "stop_reason": stop,
                "oldest_time": min(times) if times else None,
                "newest_time": max(times) if times else None,
            }
        )
        if stop in {"message_limit", "time_limit"} and anchor:
            return self.store.put(
                "history",
                {
                    "owner": base["owner"],
                    "group_id": base["group_id"],
                    "keyword": normalize(keyword),
                    "anchor": anchor,
                    "seen": recent_seen[-200:],
                },
            )
        return None

    async def download(self, result_id: str) -> dict:
        async with self.lock:
            saved = self.store.get(result_id, "file")
            if await self._owner() != saved["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已切换，请重新搜索。")
            if saved["size"] > self.settings.max_download_bytes:
                raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
            selected, _ = await self._group(saved["group_id"])
            if selected is None:
                raise QQFileError("GROUP_NOT_FOUND", "当前账号已无法确认这个群。")
            if saved["source"] == "group_files":
                params = {
                    "group_id": saved["group_id"],
                    "file_count": self.settings.directory_limit,
                }
                action = "get_group_root_files"
                if saved["folder_id"]:
                    action = "get_group_files_by_folder"
                    params["folder_id"] = saved["folder_id"]
                data = await self.client.call(action, **params)
                current = [
                    item
                    for item in (data or {}).get("files", [])
                    if list(fingerprint(item)) == saved["fingerprint"]
                ]
                if len(current) != 1 or not current[0].get("file_id"):
                    raise QQFileError(
                        "FILE_CHANGED", "文件已变化、不可见或无法唯一确认，请重新搜索。"
                    )
                file_id = str(current[0]["file_id"])
            else:
                message = await self.client.call("get_msg", message_id=saved["message_id"])
                if (
                    str((message or {}).get("group_id", "")) != saved["group_id"]
                    or message_key(message) != saved["message_id"]
                ):
                    raise QQFileError("STALE_MESSAGE", "消息身份无法确认，请重新搜索。")
                current = [
                    item
                    for index, item in attachments(message)
                    if index == saved["segment_index"]
                    and str(item.get("file", item.get("name", ""))) == saved["file_name"]
                    and number(item.get("file_size", item.get("size"))) == saved["size"]
                ]
                if len(current) != 1:
                    raise QQFileError("FILE_CHANGED", "聊天附件已变化，请重新搜索。")
                file_id = str(current[0]["file_id"])
            # Resolve only freshly observed file IDs. Never try a filename fallback.
            # QQ's native completion callback can stall; prefer the refreshed HTTPS URL.
            try:
                link = await self.client.call(
                    "get_group_file_url", group_id=saved["group_id"], file_id=file_id
                )
            except QQFileError as exc:
                if exc.code != "UPSTREAM":
                    raise
                link = None
            if isinstance(link, dict) and link.get("url"):
                try:
                    result = await asyncio.wait_for(
                        download_url(link["url"], saved["file_name"], self.settings, saved["size"]),
                        self.settings.download_timeout,
                    )
                except TimeoutError as exc:
                    raise QQFileError("DOWNLOAD_TIMEOUT", "QQ 下载超时，可稍后重试。") from exc
                return {
                    "ok": True,
                    **result,
                    "source": saved["source"],
                    "group_id": saved["group_id"],
                    "download_method": "qq_https",
                }
            try:
                data = await asyncio.wait_for(
                    self.client.call("get_file", file_id=file_id), self.settings.download_timeout
                )
            except TimeoutError as exc:
                raise QQFileError("DOWNLOAD_TIMEOUT", "QQ 下载超时，可稍后重试。") from exc
            if not isinstance(data, dict):
                raise QQFileError("PROTOCOL", "下载接口返回格式不正确。")
            result = await asyncio.to_thread(
                copy_download,
                str(data.get("file", "")),
                saved["file_name"],
                self.settings,
                saved["size"],
            )
            return {
                "ok": True,
                **result,
                "source": saved["source"],
                "group_id": saved["group_id"],
                "download_method": "local_cache",
            }
