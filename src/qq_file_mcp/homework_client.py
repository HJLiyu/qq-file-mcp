"""Fixed Tencent native homework web contracts, using in-memory QQ credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .errors import QQFileError
from .files import HOMEWORK_FILE_HOST, validate_download_url

BASE = "https://qun.qq.com/cgi-bin/homework/"
UPLOAD_PATH = "document_upload"
URLS = {
    **{
        p: BASE + p
        for p in ("hw/get_hw_list.fcg", "hw/get_hw_detail.fcg", "fb/set_hw_feedback.fcg")
    },
    UPLOAD_PATH: "https://qun.qq.com/cgi-bin/hw/util/file",
}
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def document_url(value):
    """Only the observed native homework file bucket; no unrelated group-file URL."""
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(ord(c) < 33 for c in value):
        raise QQFileError("HOMEWORK_UPLOAD_PROTOCOL", "原生文件接口未返回可用附件地址。")
    url = "https://" + value[7:] if value.startswith("http://") else value
    validate_download_url(url)
    parsed = urlparse(url)
    if parsed.hostname != HOMEWORK_FILE_HOST or parsed.fragment or not parsed.path.strip("/"):
        raise QQFileError("HOMEWORK_UPLOAD_PROTOCOL", "附件不属于已验证的原生作业文件存储。")
    return url


def cookie_bkn(jar):
    # Tencent's getBkn uses skey first, otherwise the first 10 characters of p_skey.
    key = jar["skey"].value if "skey" in jar else ""
    if not key and "p_skey" in jar:
        key = jar["p_skey"].value[:10]
    if not key:
        raise QQFileError("HOMEWORK_AUTH", "原生作业缺少有效的网页登录状态。")
    value = 5381
    for char in key:
        value = (value * 33 + ord(char)) & 0xFFFFFFFF
    return value & 0x7FFFFFFF


def native_id(value) -> str:
    value = str(value)
    if (
        len(value) > 20
        or not value.isascii()
        or not value.isdecimal()
        or not 0 < int(value) < 2**64
    ):
        raise QQFileError("HOMEWORK_PROTOCOL", "原生作业身份格式无效。")
    return value


class NativeHomeworkClient:
    def __init__(self, bridge, settings, transport=None):
        self.bridge = bridge
        self.settings = settings
        self.transport = transport

    async def _post(self, path, owner, params, upload=None):
        if path not in URLS:
            raise QQFileError("ACTION_DENIED", "不允许这个原生作业接口。")
        if path in {"fb/set_hw_feedback.fcg", UPLOAD_PATH} and not self.settings.enable_submissions:
            raise QQFileError("SUBMISSIONS_DISABLED", "原生提交未启用。")
        credentials = await self.bridge.homework_credentials()
        if not isinstance(credentials, dict):
            raise QQFileError("HOMEWORK_AUTH", "当前 QQ 会话未提供原生作业登录状态。")
        cookies = credentials.get("cookies")
        if (
            not isinstance(cookies, str)
            or not 0 < len(cookies) <= 16384
            or not cookies.isascii()
            or any(c in cookies for c in "\r\n\x00")
        ):
            raise QQFileError("HOMEWORK_AUTH", "原生作业登录凭据不可用，请检查桌面 QQ 登录。")
        jar = SimpleCookie()
        try:
            jar.load(cookies)
            cookie_owner = jar["uin"].value.removeprefix("o").lstrip("0")
        except (KeyError, ValueError, CookieError):
            cookie_owner = ""
        if cookie_owner != native_id(owner):
            raise QQFileError("ACCOUNT_CHANGED", "QQ 网页账号与桥接账号不一致，请重新查询。")
        form = {**params, "bkn": str(cookie_bkn(jar))}
        try:
            # Separate per-request client: no OneBot token, proxy, redirects or stored cookies.
            timeout = self.settings.download_timeout if upload else self.settings.request_timeout
            async with (
                asyncio.timeout(timeout),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=timeout,
                    transport=self.transport,
                ) as http,
            ):
                async with http.stream(
                    "POST",
                    URLS[path],
                    data=form,
                    files=upload,
                    headers={
                        "Cookie": cookies,
                        "Origin": "https://qun.qq.com",
                        "Referer": "https://qun.qq.com/homework/features/v2/detail.html",
                        "User-Agent": "Mozilla/5.0",
                    },
                ) as response:
                    if response.is_redirect:
                        raise QQFileError("HOMEWORK_AUTH", "原生作业要求重新登录；未跟随重定向。")
                    if response.status_code in {401, 403}:
                        raise QQFileError("HOMEWORK_AUTH", "原生作业拒绝当前 QQ 登录状态。")
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise QQFileError("HOMEWORK_LIMIT", "原生作业响应超过大小上限。")
                    payload = json.loads(body)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise QQFileError("HOMEWORK_TIMEOUT", "原生作业请求超时。") from exc
        except httpx.HTTPError as exc:
            raise QQFileError("HOMEWORK_CONNECTION", "无法连接 QQ 原生作业接口。") from exc
        except (ValueError, UnicodeError) as exc:
            raise QQFileError("HOMEWORK_PROTOCOL", "原生作业没有返回有效 JSON。") from exc
        if not isinstance(payload, dict) or type(payload.get("retcode")) is not int:
            raise QQFileError("HOMEWORK_PROTOCOL", "原生作业响应格式无效。")
        if payload["retcode"] != 0 or payload.get("cgicode", 0) != 0:
            # No arbitrary upstream message: it can contain session or answer data.
            raise QQFileError(
                "HOMEWORK_UPSTREAM", "原生作业接口未确认成功；检查登录、权限或作业状态。"
            )
        return payload.get("data")

    async def upload_document(self, owner, path, metadata):
        mime = DOCUMENT_TYPES.get(Path(metadata["name"]).suffix.lower())
        if not mime:
            raise QQFileError("HOMEWORK_FILE_TYPE", "原生文件提交目前支持 PDF、DOC、DOCX。")
        with Path(path).open("rb") as source:
            data = await self._post(
                UPLOAD_PATH, owner, {}, {"file": (metadata["name"], source, mime)}
            )
        url = data.get("url") if isinstance(data, dict) else None
        if isinstance(url, dict):
            url = url.get("origin")
        document_url(url)
        return url  # Ephemeral original URL, never a public tool result or database value.

    async def verify_document(self, url, metadata):
        """Read the native record's bytes independently, without QQ login credentials."""
        url = document_url(url)
        try:
            async with (
                asyncio.timeout(self.settings.download_timeout),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.settings.download_timeout,
                    transport=self.transport,
                ) as http,
            ):
                async with http.stream("GET", url) as response:
                    if response.is_redirect:
                        raise QQFileError(
                            "HOMEWORK_FILE_VERIFY", "原生文件读回要求重定向，未确认。"
                        )
                    response.raise_for_status()
                    digest, total = hashlib.sha256(), 0
                    async for block in response.aiter_bytes(1024 * 1024):
                        total += len(block)
                        if total > metadata["size"] or total > self.settings.max_submission_bytes:
                            raise QQFileError(
                                "HOMEWORK_FILE_VERIFY", "原生文件读回大小与预览不一致。"
                            )
                        digest.update(block)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise QQFileError(
                "HOMEWORK_FILE_VERIFY", "原生作业文件字节读回失败，尚未确认。"
            ) from exc
        if total != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
            raise QQFileError("HOMEWORK_FILE_VERIFY", "原生文件读回哈希与预览不一致。")

    async def submit_document(self, group_id, homework_id, owner, text, metadata, url):
        document_url(url)
        items = [{"type": "str", "text": text}] if text else []
        items.append(
            {"type": "file", "name": metadata["name"], "size": str(metadata["size"]), "url": url}
        )
        return await self._post(
            "fb/set_hw_feedback.fcg",
            owner,
            {
                "gid": native_id(group_id),
                "hw_id": native_id(homework_id),
                "modify": "1",
                "comment_info": json.dumps(
                    {"text": {"c": items}, "uin": native_id(owner)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )

    async def list(self, group_id, page, owner):
        return await self._post(
            "hw/get_hw_list.fcg",
            owner,
            {
                "group_id": native_id(group_id),
                "cmd": "20",  # Current user's assignments, not teacher/classmate feedback list.
                "num": str(page),
                "page_size": "10",
                "client_type": "2",
                "need_hw_detail": "1",
            },
        )

    async def detail(self, group_id, homework_id, owner):
        return await self._post(
            "hw/get_hw_detail.fcg",
            owner,
            {
                "group_id": native_id(group_id),
                "hw_id": native_id(homework_id),
                "need_fb_detail": "0",
                "client_type": "2",
            },
        )

    async def submit_text(self, group_id, homework_id, owner, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise QQFileError("INPUT", "原生作业答案需为非空纯文字，最多5000字符。")
        return await self._post(
            "fb/set_hw_feedback.fcg",
            owner,
            {
                "gid": native_id(group_id),
                "hw_id": native_id(homework_id),
                "modify": "1",
                "comment_info": json.dumps(
                    {"text": {"c": [{"type": "str", "text": text}]}, "uin": native_id(owner)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
