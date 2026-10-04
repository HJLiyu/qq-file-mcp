"""Fixed Tencent native homework web contracts, using in-memory QQ credentials."""

from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie

import httpx

from .errors import QQFileError

BASE = "https://qun.qq.com/cgi-bin/homework/"
PATHS = frozenset({"hw/get_hw_list.fcg", "hw/get_hw_detail.fcg", "fb/set_hw_feedback.fcg"})


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

    async def _post(self, path, owner, params):
        if path not in PATHS:
            raise QQFileError("ACTION_DENIED", "不允许这个原生作业接口。")
        if path == "fb/set_hw_feedback.fcg" and not self.settings.enable_submissions:
            raise QQFileError("SUBMISSIONS_DISABLED", "原生提交未启用。")
        credentials = await self.bridge.homework_credentials()
        if not isinstance(credentials, dict):
            raise QQFileError("HOMEWORK_AUTH", "当前 QQ 会话未提供原生作业登录状态。")
        cookies = credentials.get("cookies")
        token = credentials.get("token")
        if (
            not isinstance(cookies, str)
            or not 0 < len(cookies) <= 16384
            or not cookies.isascii()
            or any(c in cookies for c in "\r\n\x00")
            or not isinstance(token, int)
            or isinstance(token, bool)
            or not 0 <= token <= 2147483647
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
        form = {**params, "bkn": str(token)}
        try:
            # Separate per-request client: no OneBot token, proxy, redirects or stored cookies.
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=self.settings.request_timeout,
                transport=self.transport,
            ) as http:
                async with http.stream(
                    "POST",
                    BASE + path,
                    data=form,
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
        except httpx.TimeoutException as exc:
            raise QQFileError("HOMEWORK_TIMEOUT", "原生作业请求超时。") from exc
        except httpx.HTTPError as exc:
            raise QQFileError("HOMEWORK_CONNECTION", "无法连接 QQ 原生作业接口。") from exc
        except (ValueError, UnicodeError) as exc:
            raise QQFileError("HOMEWORK_PROTOCOL", "原生作业没有返回有效 JSON。") from exc
        if not isinstance(payload, dict) or type(payload.get("retcode")) is not int:
            raise QQFileError("HOMEWORK_PROTOCOL", "原生作业响应格式无效。")
        if payload["retcode"] != 0:
            # No arbitrary upstream message: it can contain session or answer data.
            raise QQFileError(
                "HOMEWORK_UPSTREAM", "原生作业接口未确认成功；检查登录、权限或作业状态。"
            )
        return payload.get("data")

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
