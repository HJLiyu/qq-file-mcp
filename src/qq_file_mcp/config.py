from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from .errors import QQFileError


@dataclass(frozen=True)
class Settings:
    base_url: str = "http://127.0.0.1:3000"
    token: str = field(default="", repr=False)
    backend: str = "napcat"
    state_dir: Path = field(default_factory=lambda: Path.home() / ".qq-file-mcp")
    download_dir: Path = field(default_factory=lambda: Path.home() / "Downloads" / "QQ-File-MCP")
    allowed_roots: tuple[Path, ...] = ()
    request_timeout: float = 20
    search_timeout: float = 40
    download_timeout: float = 180
    directory_limit: int = 10000
    folder_limit: int = 100
    max_download_bytes: int = 512 * 1024 * 1024
    result_ttl: int = 3600

    def __post_init__(self):
        if self.backend not in {"napcat", "snowluma"}:
            raise QQFileError("CONFIG", "QQ_FILE_BACKEND 必须是 napcat 或 snowluma。")
        parsed = urlparse(self.base_url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if (
            parsed.scheme not in {"http", "https"}
            or not local
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise QQFileError(
                "CONFIG", "ONEBOT_URL（或 NAPCAT_URL）必须是本机回环地址，不能包含凭据或路径。"
            )
        if not self.token.strip():
            raise QQFileError(
                "CONFIG", "请先设置 ONEBOT_TOKEN（或 NAPCAT_TOKEN）；不允许无鉴权连接。"
            )
        for value in (
            self.request_timeout,
            self.search_timeout,
            self.download_timeout,
            self.directory_limit,
            self.folder_limit,
            self.max_download_bytes,
            self.result_ttl,
        ):
            if value <= 0:
                raise QQFileError("CONFIG", "超时、数量和大小上限必须大于零。")

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> Settings:
        # Explicit file only: never silently load an unrelated working-directory .env.
        values = dict(dotenv_values(env_file)) if env_file else {}
        values.update(os.environ)
        user_home = Path.home()
        default_roots = [
            user_home / "Documents" / "Tencent Files",
            user_home / "Documents" / "QQ Files",
            user_home / "AppData" / "Roaming" / "Tencent",
            user_home / "AppData" / "Local" / "Tencent",
            user_home / ".config" / "QQ",
        ]
        roots = values.get("QQ_FILE_ALLOWED_ROOTS", "")
        allowed = tuple(Path(p).expanduser().resolve() for p in roots.split(os.pathsep) if p)
        try:
            return cls(
                base_url=values.get(
                    "ONEBOT_URL", values.get("NAPCAT_URL", "http://127.0.0.1:3000")
                ).rstrip("/"),
                token=values.get("ONEBOT_TOKEN", values.get("NAPCAT_TOKEN", "")),
                backend=values.get("QQ_FILE_BACKEND", "napcat"),
                state_dir=Path(values.get("QQ_FILE_STATE_DIR", str(user_home / ".qq-file-mcp")))
                .expanduser()
                .resolve(),
                download_dir=Path(
                    values.get("QQ_FILE_DOWNLOAD_DIR", str(user_home / "Downloads" / "QQ-File-MCP"))
                )
                .expanduser()
                .resolve(),
                allowed_roots=allowed or tuple(p.resolve() for p in default_roots),
                request_timeout=float(values.get("QQ_FILE_REQUEST_TIMEOUT", "20")),
                search_timeout=float(values.get("QQ_FILE_SEARCH_TIMEOUT", "40")),
                download_timeout=float(values.get("QQ_FILE_DOWNLOAD_TIMEOUT", "180")),
                directory_limit=int(values.get("QQ_FILE_DIRECTORY_LIMIT", "10000")),
                folder_limit=int(values.get("QQ_FILE_FOLDER_LIMIT", "100")),
                max_download_bytes=int(values.get("QQ_FILE_MAX_DOWNLOAD_MB", "512")) * 1024 * 1024,
            )
        except (TypeError, ValueError) as exc:
            raise QQFileError("CONFIG", "配置中的数字或路径格式不正确。") from exc
