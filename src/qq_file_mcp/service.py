from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .downloads import DownloadedFiles
from .errors import QQFileError
from .files import copy_download, download_url, normalize
from .metadata import FileFilters, publisher_info, publisher_matches
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
    # message_id is a hash, NOT a chronological sequence. NapCat exposes real_seq;
    # SnowLuma exposes the QQ sequence as message_seq instead.
    return (
        number(message.get("time")),
        number(message.get("real_seq", message.get("message_seq"))),
    )


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
        self.downloaded = DownloadedFiles(settings, self.store)
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
            "backend": self.settings.backend,
            "submission_dir": str(self.settings.submission_dir),
            "submissions_enabled": self.settings.enable_submissions,
            "native_homework_supported": self.settings.backend == "snowluma",
            "native_homework": {
                "session_verified": False,
                "read": self.settings.backend == "snowluma",
                "submission_formats": ["text"] if self.settings.backend == "snowluma" else [],
                "file_submission": False,
                "submissions_enabled": self.settings.enable_submissions,
                "message": "能力声明不保证当前登录可用；实际原生查询才能验证会话。",
            },
        }

    async def _owner(self) -> str:
        info = await self.client.call("get_login_info")
        owner = str((info or {}).get("user_id", ""))
        if not owner or owner == "0":
            raise QQFileError(
                "NOT_LOGGED_IN", "本地 QQ 接口未登录；请检查桌面 QQ 与桥接工具的加载状态。"
            )
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
        context = {key: value for key, value in context.items() if not key.startswith("_")}
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
            "publisher": context.get("publisher", {}),
        }

    def _page(self, results: list, owner: str) -> tuple[list, str | None]:
        if len(results) <= 50:
            return results, None
        token = self.store.put(
            "results", {"owner": owner, "backend": self.settings.backend, "items": results[50:]}
        )
        return results[:50], token

    def _check_backend(self, saved: dict):
        # References created before backend support belong to the original NapCat client.
        if saved.get("backend", "napcat") != self.settings.backend:
            raise QQFileError("BACKEND_CHANGED", "接入方式已切换，请重新搜索。")

    async def more_results(self, cursor: str) -> dict:
        async with self.lock:
            saved = self.store.get(cursor, "results")
            self._check_backend(saved)
            if await self._owner() != saved["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已切换，请重新搜索。")
            items, next_cursor = self._page(saved["items"], saved["owner"])
            return {"ok": True, "results": items, "results_cursor": next_cursor}

    async def search(
        self,
        group: str,
        filename: str = "",
        source: str = "both",
        max_messages: int = 1000,
        history_cursor: str | None = None,
        publisher: str = "",
        published_after: str | None = None,
        published_before: str | None = None,
    ) -> dict:
        filters = FileFilters.create(publisher, published_after, published_before)
        if (
            not isinstance(group, str)
            or not group.strip()
            or len(group) > 200
            or not isinstance(filename, str)
            or len(filename) > 200
            or (
                not filename.strip()
                and not filters.publisher
                and filters.published_after is None
                and filters.published_before is None
            )
        ):
            raise QQFileError(
                "INPUT", "请提供群名或群号，并提供文件名、发布人或发布时间；明确列出全部时用 *。"
            )
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
            warnings: list[dict] = []
            profile, publisher_choices = await self._resolve_publisher(
                selected["group_id"],
                filters,
                warnings,
            )
            if publisher_choices:
                return {
                    "ok": True,
                    "needs_publisher_selection": True,
                    "group": selected,
                    "publishers": publisher_choices,
                    "message": "发布人匹配到多个成员，请选择 QQ 号后重新查询。",
                }
            resume = None
            if history_cursor:
                resume = self.store.get(history_cursor, "history")
                self._check_backend(resume)
                if (
                    resume["owner"] != owner
                    or resume["group_id"] != selected["group_id"]
                    or resume["keyword"] != normalize(filename)
                    or resume.get("filters", FileFilters().as_dict()) != filters.as_dict()
                ):
                    raise QQFileError(
                        "CURSOR_MISMATCH", "继续查询标识与账号、群、文件名或发布人/时间条件不一致。"
                    )
                source = "history"
            base = {
                "owner": owner,
                "backend": self.settings.backend,
                **selected,
                "_filters": filters,
                "_publisher_profile": profile,
            }
            results: list[dict] = []
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
                "filters": filters.as_dict(),
                "results": page,
                "matched_in_this_scan": len(results),
                "results_cursor": results_cursor,
                "history_cursor": cursor,
                "coverage": coverage,
                "warnings": warnings,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "message": "结果仅覆盖本次实际搜索范围；未找到不代表文件不存在。",
            }

    async def _resolve_publisher(self, group_id, filters, warnings):
        query = filters.publisher
        if not query or query.isdecimal():
            return None, []
        try:
            members = await self._call(
                time.monotonic() + self.settings.search_timeout,
                "get_group_member_list",
                group_id=group_id,
                no_cache=True,
            )
            if not isinstance(members, list):
                raise QQFileError("PROTOCOL", "群成员列表格式不正确。")
            profiles = {
                str(m["user_id"]): publisher_info({"sender": m})
                for m in members
                if isinstance(m, dict) and m.get("user_id")
            }
            choices = [p for p in profiles.values() if publisher_matches(query, p)]
            exact = [
                p for p in choices if query in {normalize(p["name"]), normalize(p["nickname"])}
            ]
            choices = exact or choices
            if len(choices) > 1:
                return None, choices
            if choices:
                filters.publisher = choices[0]["user_id"]
                return choices[0], []
            warnings.append(
                {
                    "code": "PUBLISHER_NOT_IN_MEMBER_LIST",
                    "message": "成员列表未匹配发布人，仅匹配附件返回的名字；可能遗漏历史成员。",
                }
            )
        except QQFileError as exc:
            warnings.append(
                {
                    "code": "PUBLISHER_LOOKUP_UNAVAILABLE",
                    "message": "无法获取成员名字，仅匹配文件/消息中实际返回的发布人信息。",
                    "cause": exc.code,
                }
            )
        return None, []

    @staticmethod
    def _publisher(base, value, *, directory=False):
        publisher = publisher_info(value, directory=directory)
        profile = base.get("_publisher_profile")
        if profile and profile["user_id"] == publisher["user_id"]:
            publisher = profile
        return publisher

    async def _directories(self, base, keyword, results, warnings, coverage, deadline):
        info = {
            "files_scanned": 0,
            "publisher_metadata_missing": 0,
            "publication_time_missing": 0,
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
        # Some implementations ignore file_count; enforce our own processing limit.
        for item in data["files"][: self.settings.directory_limit]:
            if not isinstance(item, dict):
                continue
            info["files_scanned"] += 1
            name = str(item.get("file_name", ""))
            publisher = self._publisher(base, item, directory=True)
            stamp = number(item.get("upload_time"))
            if not any(publisher.values()):
                info["publisher_metadata_missing"] += 1
            if not stamp:
                info["publication_time_missing"] += 1
            if (
                item.get("file_id")
                and matches(keyword, name)
                and base["_filters"].matches(publisher, stamp)
            ):
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
                            "time": stamp,
                            "publisher": publisher,
                        }
                    )
                )

    async def _history(
        self,
        base,
        keyword,
        results,
        warnings,
        coverage,
        deadline,
        budget,
        resume,
        *,
        projector=None,
        max_chars=12000,
    ):
        anchor = resume.get("anchor") if resume else None
        seen = set(resume.get("seen", [])) if resume else set()
        recent_seen = list(resume.get("seen", [])) if resume else []
        scanned = 0
        times: list[int] = []
        stop = "message_limit"
        returned_chars = 0
        info = {
            "messages_scanned": 0,
            "requested_limit": budget,
            "exhaustive": False,
            "unsupported_segments": 0,
            "publisher_metadata_missing": 0,
            "publication_time_missing": 0,
            "note": "仅包含当前会话可获取的消息。合并转发与在线文件不在本版范围内。",
        }
        coverage["history"] = info
        if projector:
            info["unparsed_segments"] = {}
            info["note"] = "仅覆盖可获取的消息；图片/语音/转发未提取正文，不能据此排除其他要求。"
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
                    if projector and (len(results) >= 50 or returned_chars >= max_chars):
                        stop = "result_limit"
                        break
                    key = message_key(message)
                    if key in seen:
                        continue
                    seen.add(key)
                    recent_seen.append(key)
                    scanned += 1
                    stamp = number(message.get("time"))
                    publisher = self._publisher(base, message)
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
                    if projector:
                        if not any(publisher.values()):
                            info["publisher_metadata_missing"] += 1
                        if not stamp:
                            info["publication_time_missing"] += 1
                        for segment in segments:
                            if isinstance(segment, dict):
                                kind = str(segment.get("type"))
                                if kind not in {"text", "file", "at", "reply"}:
                                    counts = info["unparsed_segments"]
                                    counts[kind] = counts.get(kind, 0) + 1
                        projected = projector(base, message, keyword)
                        if projected:
                            text = projected["text"]
                            snippet = text[: min(2000, max_chars - returned_chars)]
                            projected["text"] = snippet
                            projected["text_truncated"] = len(snippet) < len(text)
                            projected["total_text_chars"] = len(text)
                            results.append(projected)
                            returned_chars += len(snippet)
                    for index, item in () if projector else attachments(message):
                        if not any(publisher.values()):
                            info["publisher_metadata_missing"] += 1
                        if not stamp:
                            info["publication_time_missing"] += 1
                        name = str(item.get("file", item.get("name", "")))
                        if matches(keyword, name) and base["_filters"].matches(publisher, stamp):
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
                                        "publisher": publisher,
                                    }
                                )
                            )
                    anchor = key
                info["messages_scanned"] = scanned
                if stop == "result_limit":
                    break
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
        if self.settings.backend == "snowluma" and scanned == 0 and stop == "no_more_returned":
            warnings.append(
                {
                    "source": "history",
                    "code": "HISTORY_ANCHOR_UNAVAILABLE",
                    "message": (
                        "桥接会话可能尚未收到该群的消息起点，或历史不可用；"
                        "空结果不能证明没有附件。群文件目录仍可查询。"
                    ),
                }
            )
        if stop in {"message_limit", "time_limit", "result_limit", "upstream_error"} and anchor:
            return self.store.put(
                "message_history" if projector else "history",
                {
                    "owner": base["owner"],
                    "backend": self.settings.backend,
                    "group_id": base["group_id"],
                    "keyword": normalize(keyword),
                    "filters": base["_filters"].as_dict(),
                    "anchor": anchor,
                    "seen": recent_seen[-200:],
                },
            )
        return None

    async def download(self, result_id: str) -> dict:
        async with self.lock:
            saved = self.store.get(result_id, "file")
            self._check_backend(saved)
            if await self._owner() != saved["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已切换，请重新搜索。")
            if saved["size"] > self.settings.max_download_bytes:
                raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
            selected, _ = await self._group(saved["group_id"])
            if selected is None:
                raise QQFileError("GROUP_NOT_FOUND", "当前账号已无法确认这个群。")
            busid = 102
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
                busid = number(current[0].get("busid")) or 102
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
                old_publisher = saved.get("publisher", {}).get("user_id")
                if (
                    old_publisher
                    and publisher_info(message)["user_id"] != old_publisher
                    or saved.get("time")
                    and number(message.get("time")) != saved["time"]
                ):
                    raise QQFileError("FILE_CHANGED", "聊天附件的发送人或时间已变化，请重新搜索。")
                file_id = str(current[0]["file_id"])
                busid = number(current[0].get("busid")) or 102
            # Resolve only freshly observed file IDs. Never try a filename fallback.
            # QQ's native completion callback can stall; prefer the refreshed HTTPS URL.
            try:
                link = await self.client.call(
                    "get_group_file_url", group_id=saved["group_id"], file_id=file_id, busid=busid
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
                return await self._download_result(saved, result, "qq_https")
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
            return await self._download_result(saved, result, "local_cache")

    async def _download_result(self, saved, result, method):
        publisher = saved.get("publisher", {})
        warnings = []
        if publisher.get("user_id") and not publisher.get("nickname"):
            try:
                members = await self._call(
                    time.monotonic() + min(5, self.settings.request_timeout),
                    "get_group_member_list",
                    group_id=saved["group_id"],
                    no_cache=True,
                )
                if not isinstance(members, list):
                    raise QQFileError("PROTOCOL", "群成员列表格式不正确。")
                profile = next(
                    (
                        publisher_info({"sender": m})
                        for m in members
                        if isinstance(m, dict) and str(m.get("user_id")) == publisher["user_id"]
                    ),
                    None,
                )
                if profile:
                    publisher = profile
            except QQFileError:
                warnings.append(
                    {
                        "code": "PUBLISHER_ALIAS_UNAVAILABLE",
                        "message": "文件已下载；无法补全当前昵称/群名片，保留已知信息。",
                    }
                )
        origin = {**saved, "publisher": publisher}
        return {
            "ok": True,
            **result,
            "source": saved["source"],
            "group_id": saved["group_id"],
            "group_name": saved["group_name"],
            "publisher": publisher,
            "published_at": saved.get("time"),
            "download_method": method,
            "local_file_id": self.downloaded.register(
                Path(result["path"]), result["sha256"], origin
            ),
            "warnings": warnings,
        }
