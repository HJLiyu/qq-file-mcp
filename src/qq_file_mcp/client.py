from __future__ import annotations

import httpx

from .config import Settings
from .errors import QQFileError

# This is deliberately not a generic OneBot proxy. No write/group-admin APIs.
ALLOWED_ACTIONS = frozenset(
    {
        "get_status",
        "get_login_info",
        "get_version_info",
        "get_group_list",
        "get_group_root_files",
        "get_group_files_by_folder",
        "get_group_file_url",
        "get_group_msg_history",
        "get_msg",
        "get_file",
    }
)


class OneBotClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.download_timeout = settings.download_timeout
        self.http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=settings.request_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    async def call(self, action: str, **params):
        if action not in ALLOWED_ACTIONS:
            raise QQFileError("ACTION_DENIED", "该接口不在检索与下载允许列表中。")
        try:
            kwargs = {"timeout": self.download_timeout} if action == "get_file" else {}
            response = await self.http.post(f"/{action}", json=params, **kwargs)
            if response.status_code in {401, 403}:
                raise QQFileError("AUTH", "NapCat 拒绝访问，请检查本地访问令牌。")
            if response.is_redirect:
                raise QQFileError("REDIRECT", "本地接口返回重定向，已拒绝转发凭据。")
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise QQFileError("TIMEOUT", f"{action} 超时；稍后重试或缩小搜索范围。") from exc
        except httpx.HTTPError as exc:
            raise QQFileError("CONNECTION", "无法连接本地 NapCat，请检查运行状态和端口。") from exc
        except ValueError as exc:
            raise QQFileError("PROTOCOL", "本地接口没有返回有效 JSON。") from exc
        if not isinstance(payload, dict) or "retcode" not in payload:
            raise QQFileError("PROTOCOL", "本地接口返回格式不符合 OneBot。")
        if payload.get("status") != "ok" or payload.get("retcode") != 0:
            # Never reflect arbitrary upstream text (could include credentials/chat text).
            raise QQFileError(
                "UPSTREAM", f"{action} 未成功；账号、权限、历史或文件可用性需要检查。"
            )
        return payload.get("data")
