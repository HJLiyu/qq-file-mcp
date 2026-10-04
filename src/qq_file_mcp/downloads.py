"""Offline discovery and bounded reading of files inside the download directory."""

from __future__ import annotations

import asyncio
import json
import os
import site
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .errors import QQFileError
from .files import normalize
from .reader_worker import FileReadError, file_format, file_identity, inspect_path, is_link
from .state import ResultStore


class DownloadedFiles:
    def __init__(self, settings: Settings, store: ResultStore | None = None):
        self.settings = settings
        self.root = settings.download_dir.resolve()
        self.store = store or ResultStore(settings.state_dir, settings.result_ttl)
        self.read_slots = asyncio.Semaphore(2)

    def _inspect(self, path):
        try:
            return inspect_path(self.root, path)
        except FileReadError as exc:
            raise QQFileError(exc.code, exc.message) from exc

    def register(self, path: Path, sha256: str | None = None) -> str:
        info = self._inspect(path)
        return self.store.put(
            "local_file",
            {
                "relative_path": path.absolute().relative_to(self.root).as_posix(),
                "identity": file_identity(info),
                "sha256": sha256,
            },
        )

    def _scan(self, query, limit, offset):
        found, scanned, folders, skipped, complete = [], 0, 0, 0, True
        if self.root.exists():
            if is_link(self.root.lstat()) or not self.root.is_dir():
                raise QQFileError("LOCAL_PATH_DENIED", "下载目录必须是普通目录。")

            def on_error(_):
                nonlocal complete
                complete = False

            for directory, dirs, filenames in os.walk(
                self.root, followlinks=False, onerror=on_error
            ):
                folders += 1
                if folders > self.settings.folder_limit:
                    complete = False
                    break
                allowed_dirs = []
                for name in sorted(dirs):
                    try:
                        if not is_link((Path(directory) / name).lstat()):
                            allowed_dirs.append(name)
                        else:
                            skipped += 1
                    except OSError:
                        complete = False
                dirs[:] = allowed_dirs
                for name in sorted(filenames):
                    if scanned >= self.settings.directory_limit:
                        complete = False
                        break
                    scanned += 1
                    path = Path(directory) / name
                    try:
                        info = self._inspect(path)
                    except QQFileError:
                        skipped += 1
                        continue
                    if normalize(query) not in normalize(name):
                        continue
                    kind = file_format(path)
                    found.append(
                        {
                            "file_name": name,
                            "relative_path": path.relative_to(self.root).as_posix(),
                            "bytes": info.st_size,
                            "modified_ns": info.st_mtime_ns,
                            "format": kind,
                            "can_read": kind != "unsupported",
                            "_identity": file_identity(info),
                        }
                    )
                if not complete and scanned >= self.settings.directory_limit:
                    break
        found.sort(key=lambda item: (-item["modified_ns"], normalize(item["relative_path"])))
        selected = found[offset : offset + limit]
        for item in selected:
            item["file_id"] = self.store.put(
                "local_file",
                {
                    "relative_path": item["relative_path"],
                    "identity": item.pop("_identity"),
                    "sha256": None,
                },
            )
        return {
            "ok": True,
            "download_dir": str(self.root),
            "files": selected,
            "total_matches": len(found),
            "next_offset": offset + limit if offset + limit < len(found) else None,
            "coverage": {
                "complete": complete,
                "scanned_files": scanned,
                "scanned_folders": folders,
                "skipped_links_or_unreadable": skipped,
            },
            "warnings": []
            if complete
            else [
                {
                    "code": "LOCAL_SCAN_LIMIT",
                    "message": "下载目录未完整扫描：达到数量上限或有目录无法访问。",
                }
            ],
        }

    async def list_files(self, query: str = "", limit: int = 50, offset: int = 0) -> dict:
        if (
            not isinstance(query, str)
            or len(query) > 200
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or not isinstance(offset, int)
            or not 0 <= offset <= self.settings.directory_limit
        ):
            raise QQFileError(
                "INVALID_INPUT", "关键词最多200字符，每页1–100项，偏移不能超过扫描上限。"
            )
        return await asyncio.to_thread(self._scan, query.strip(), limit, offset)

    async def read(
        self,
        file_id: str,
        start: int = 1,
        count: int | None = None,
        char_offset: int = 0,
        max_chars: int = 12000,
    ) -> dict:
        saved = self.store.get(file_id, "local_file")
        path = self.root / saved["relative_path"]
        info = self._inspect(path)
        if file_identity(info) != saved["identity"]:
            raise QQFileError("FILE_CHANGED", "本地文件已变化，请重新列出下载文件。")
        kind = file_format(path)
        if kind == "unsupported":
            raise QQFileError(
                "UNSUPPORTED_FORMAT", "暂支持 PDF 和常见文本文件；不解压、不执行文件。"
            )
        if info.st_size > self.settings.max_read_bytes:
            raise QQFileError("FILE_TOO_LARGE", "文件超过配置的读取大小上限。")
        count = (3 if kind == "pdf" else 200) if count is None else count
        if (
            not isinstance(start, int)
            or start < 1
            or not isinstance(count, int)
            or not 1 <= count <= (10 if kind == "pdf" else 500)
            or not isinstance(char_offset, int)
            or char_offset < 0
            or not isinstance(max_chars, int)
            or not 1 <= max_chars <= 20000
        ):
            raise QQFileError(
                "INVALID_INPUT", "起点从1开始；每次最多10页或500行、20000字符，偏移不可为负。"
            )
        request = {
            "root": str(self.root),
            "path": str(path),
            "identity": saved["identity"],
            "sha256": saved.get("sha256"),
            "format": kind,
            "start": start,
            "count": count,
            "char_offset": char_offset,
            "max_chars": max_chars,
            "max_bytes": self.settings.max_read_bytes,
        }
        async with self.read_slots:
            result = await self._run_reader(request)
        if not result.get("ok"):
            error = result["error"]
            raise QQFileError(error["code"], error["message"])
        return {
            **result,
            "file_id": file_id,
            "file_name": path.name,
            "relative_path": saved["relative_path"],
            "format": kind,
            "bytes": info.st_size,
        }

    async def _run_reader(self, request):
        env = dict(os.environ)
        for name in ("ONEBOT_TOKEN", "NAPCAT_TOKEN"):
            env.pop(name, None)
        env["PYTHONIOENCODING"] = "utf-8"
        # Windows venv python.exe is a launcher. Launch the actual interpreter so
        # timeout/cancellation kills the parser itself, without an orphaned child.
        executable = sys.executable
        if os.name == "nt":
            executable = sys._base_executable
            env["PYTHONPATH"] = os.pathsep.join(site.getsitepackages())
        worker = Path(__file__).with_name("reader_worker.py")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            **options,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode("utf-8")),
                self.settings.read_timeout,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise QQFileError(
                "READ_TIMEOUT", "文档解析超时，已停止读取进程；可缩小范围重试。"
            ) from exc
        if process.returncode:
            raise QQFileError("READ_FAILED", "文档读取进程异常退出，文件可能损坏。")
        try:
            return json.loads(output.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise QQFileError("READ_FAILED", "文档读取进程未返回有效结果。") from exc
