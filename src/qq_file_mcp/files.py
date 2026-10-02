from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings
from .errors import QQFileError


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def safe_filename(value: str) -> str:
    cleaned = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", cleaned).strip(" .")
    cleaned = cleaned[:180].rstrip(" .") or "download"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned


def copy_download(source: str, name: str, settings: Settings, expected_size: int = 0) -> dict:
    # Only the local NapCat get_file response supplies this path, never the agent/user.
    if not source or source.startswith(("http:", "https:", "file:", "\\\\", "//")):
        raise QQFileError("DOWNLOAD_PATH", "NapCat 未返回可用的本机文件路径。")
    try:
        path = Path(source).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise QQFileError("DOWNLOAD_MISSING", "下载缓存不存在，文件可能失效。") from exc
    if not any(path.is_relative_to(root.resolve()) for root in settings.allowed_roots):
        raise QQFileError(
            "DOWNLOAD_ROOT", "QQ 缓存路径不在允许目录中；请配置 QQ_FILE_ALLOWED_ROOTS。"
        )
    if not path.is_file():
        raise QQFileError("DOWNLOAD_PATH", "返回的路径不是普通文件。")
    size = path.stat().st_size
    if size > settings.max_download_bytes:
        raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
    if expected_size and expected_size != size:
        raise QQFileError("SIZE_MISMATCH", "文件大小与搜索结果不一致，请重新搜索。")
    root = settings.download_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(name)
    digest = hashlib.sha256()
    written = 0
    created: Path | None = None
    try:
        for number in range(1000):
            base = Path(filename)
            candidate = root / (filename if number == 0 else f"{base.stem} ({number}){base.suffix}")
            try:
                output = candidate.open("xb")
                created = candidate
                break
            except FileExistsError:
                continue
        else:
            raise QQFileError("NAME_COLLISION", "下载目录同名文件过多，请整理后重试。")
        with output, path.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_download_bytes:
                    raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != size:
            raise QQFileError("SIZE_MISMATCH", "复制期间文件发生变化，请重新下载。")
        return {
            "path": str(created),
            "file_name": created.name,
            "bytes": written,
            "sha256": digest.hexdigest(),
        }
    except BaseException:
        if created is not None:
            created.unlink(missing_ok=True)
        raise


def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    trusted = any(
        host == domain or host.endswith("." + domain)
        for domain in ("qq.com", "qq.com.cn", "qpic.cn", "weiyun.com")
    )
    try:
        valid_port = parsed.port in {None, 443}
    except ValueError:
        valid_port = False
    if (
        parsed.scheme != "https"
        or not trusted
        or parsed.username
        or parsed.password
        or not valid_port
    ):
        raise QQFileError("DOWNLOAD_URL", "下载链接不属于允许的 QQ HTTPS 文件域名。")


async def download_url(
    url: str,
    name: str,
    settings: Settings,
    expected_size: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Stream an ephemeral QQ URL with no OneBot auth header and validate each redirect."""
    import tempfile

    validate_download_url(url)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        # No cookies, environment proxy or authorization header shared with OneBot.
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        ) as http:
            for _ in range(5):
                validate_download_url(url)
                async with http.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise QQFileError("DOWNLOAD_REDIRECT", "下载重定向缺少地址。")
                        url = urljoin(url, location)
                        continue
                    response.raise_for_status()
                    header_size = response.headers.get("content-length")
                    if header_size and int(header_size) > settings.max_download_bytes:
                        raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
                    with tempfile.NamedTemporaryFile(
                        dir=settings.state_dir, suffix=".part", delete=False
                    ) as temp:
                        temp_path = Path(temp.name)
                        size = 0
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > settings.max_download_bytes:
                                raise QQFileError("FILE_TOO_LARGE", "文件超过配置的下载大小上限。")
                            temp.write(chunk)
                    if expected_size and size != expected_size:
                        raise QQFileError(
                            "SIZE_MISMATCH", "下载大小与搜索结果不一致，已丢弃不完整文件。"
                        )
                    # The staging file is our own, not an arbitrary backend-provided path.
                    from dataclasses import replace

                    staging_settings = replace(
                        settings, allowed_roots=(settings.state_dir.resolve(),)
                    )
                    return copy_download(str(temp_path), name, staging_settings, expected_size)
            raise QQFileError("DOWNLOAD_REDIRECT", "下载重定向次数过多。")
    except httpx.HTTPError as exc:
        raise QQFileError("DOWNLOAD_HTTP", "QQ 下载请求未成功；链接可能失效或网络不可达。") from exc
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
