"""Own submissions: exact previews, one transport attempt, honest receipts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
from pathlib import Path

from .errors import QQFileError
from .files import safe_filename
from .reader_worker import FileReadError, file_identity, inspect_path, is_link


class SubmissionService:
    def __init__(self, files):
        self.files = files
        self.settings = files.settings
        self.root = self.settings.submission_dir.absolute()

    def _inspect(self, path):
        try:
            info = inspect_path(self.root, path)
        except FileReadError as exc:
            raise QQFileError(
                "SUBMISSION_PATH", "提交文件必须位于专用提交目录中，且不能是链接。"
            ) from exc
        if not 0 < info.st_size <= self.settings.max_submission_bytes:
            raise QQFileError("SUBMISSION_SIZE", "提交文件为空或超过提交大小上限。")
        return info

    def _file(self, path, *, staging=False):
        """Hash an opened identity; commit uses an independent verified snapshot."""
        info = self._inspect(path)
        identity = file_identity(info)
        staged = None
        try:
            if staging:
                directory = self.settings.state_dir / "submission-staging"
                directory.mkdir(parents=True, exist_ok=True)
                if is_link(directory.lstat()):
                    raise QQFileError("SUBMISSION_PATH", "提交暂存目录不能是链接。")
                staged = tempfile.NamedTemporaryFile(
                    dir=directory, prefix="submission-", suffix=path.suffix, delete=False
                )
            digest, total = hashlib.sha256(), 0
            with path.open("rb") as source:
                if file_identity(os.fstat(source.fileno())) != identity:
                    raise QQFileError("SUBMISSION_CHANGED", "提交文件已改变，请重新准备预览。")
                while block := source.read(1024 * 1024):
                    total += len(block)
                    if total > self.settings.max_submission_bytes:
                        raise QQFileError("SUBMISSION_SIZE", "提交文件超过大小上限。")
                    digest.update(block)
                    if staged:
                        staged.write(block)
                if file_identity(os.fstat(source.fileno())) != identity:
                    raise QQFileError("SUBMISSION_CHANGED", "提交文件已改变，请重新准备预览。")
            if file_identity(self._inspect(path)) != identity or total != info.st_size:
                raise QQFileError("SUBMISSION_CHANGED", "提交文件已改变，请重新准备预览。")
            return {
                "path": str(path),
                "name": safe_filename(path.name),
                "size": total,
                "sha256": digest.hexdigest(),
                "identity": identity,
            }, (Path(staged.name) if staged else None)
        except BaseException:
            if staged:
                staged.close()
                Path(staged.name).unlink(missing_ok=True)
            raise
        finally:
            if staged:
                staged.close()

    async def prepare(self, group: str, text: str = "", file_path: str = ""):
        if (
            not isinstance(group, str)
            or not group.strip()
            or len(group) > 200
            or not isinstance(text, str)
            or len(text) > 8000
            or not isinstance(file_path, str)
            or len(file_path) > 4096
            or bool(text.strip()) == bool(file_path.strip())
        ):
            raise QQFileError("INPUT", "指定群和文字或一个文件；文字最多8000字符，两者只能选一。")
        async with self.files.lock:
            owner = await self.files._owner()
            selected, choices = await self.files._group(group)
            if choices:
                return {"ok": True, "needs_group_selection": True, "groups": choices}
            value = {
                "owner": owner,
                "backend": self.settings.backend,
                "group": selected,
                "kind": "file" if file_path.strip() else "text",
                "text": text,
                "file": None,
            }
            if file_path.strip():
                path = Path(file_path)
                if not path.is_absolute():
                    path = self.root / path
                value["file"], _ = await asyncio.to_thread(self._file, path)
            preview_id, expires = self.files.store.create_submission(value)
            return {
                "ok": True,
                "preview_id": preview_id,
                "expires_at": expires,
                "account_id": owner,
                "group": selected,
                "kind": value["kind"],
                "text": text,
                "file": {key: value["file"][key] for key in ("path", "name", "size", "sha256")}
                if value["file"]
                else None,
                "submissions_enabled": self.settings.enable_submissions,
                "destination": "group_files" if value["file"] else "group_chat",
                "native_homework_submission": False,
                "message": (
                    "此步骤未发送。请展示群名/群号与完整文字或文件信息；"
                    "需要用户明确授权准确目标与内容；已有明确授权且未变时无需重复确认。"
                ),
            }

    def receipt(self, preview_id):
        saved = self.files.store.submission(preview_id)
        return saved["receipt"] or {
            "ok": True,
            "preview_id": preview_id,
            "status": (
                "outcome_unknown"
                if saved["status"] == "sending"
                else "expired"
                if saved["expires"] <= time.time()
                else saved["status"]
            ),
            "retry_allowed": False,
            "message": "若发送进行中或进程曾中断，先在 QQ 核对；不能以同一预览重新发送。",
        }

    async def stage_file(self, metadata):
        task = asyncio.create_task(
            asyncio.to_thread(self._file, Path(metadata["path"]), staging=True)
        )
        try:
            actual, staged = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                _, unused = await task
                unused.unlink(missing_ok=True)
            finally:
                raise
        if actual != metadata:
            staged.unlink(missing_ok=True)
            raise QQFileError("SUBMISSION_CHANGED", "提交文件已改变，请重新准备并确认。")
        return staged

    async def submit(self, preview_id):
        if not self.settings.enable_submissions:
            raise QQFileError(
                "SUBMISSIONS_DISABLED", "发送未启用；设置 QQ_FILE_ENABLE_SUBMISSIONS=true 后重载。"
            )
        async with self.files.lock:
            saved = self.files.store.submission(preview_id)
            if saved["status"] != "ready":
                return self.receipt(preview_id)
            if saved["expires"] <= time.time():
                raise QQFileError("EXPIRED_REFERENCE", "提交预览已过期，请重新准备并确认。")
            value = saved["value"]
            if value.get("kind") not in {"text", "file"}:
                raise QQFileError(
                    "SUBMISSION_KIND", "该预览不是群聊天/群文件提交；请使用原生作业提交工具。"
                )
            self.files._check_backend(value)
            if await self.files._owner() != value["owner"]:
                raise QQFileError("ACCOUNT_CHANGED", "账号已改变，请重新准备并确认。")
            selected, _ = await self.files._group(value["group"]["group_id"])
            if selected != value["group"]:
                raise QQFileError("GROUP_CHANGED", "目标群名称或身份已改变，请重新准备并确认。")
            staged = None
            if value["file"]:
                staged = await self.stage_file(value["file"])
            try:
                receipt = {
                    "ok": True,
                    "preview_id": preview_id,
                    "group": selected,
                    "kind": value["kind"],
                    "account_id": value["owner"],
                    "file": {key: value["file"][key] for key in ("name", "size", "sha256")}
                    if value["file"]
                    else None,
                    "native_homework_submission": False,
                    "teacher_accepted": None,
                    "chat_message_published": None,
                    "retry_allowed": False,
                    "completed_at": None,
                    "status": "outcome_unknown",
                    "message": "发送进行中或进程中断；可能已经发送，请先在QQ核对，不要重试。",
                }
                if not self.files.store.claim_submission(preview_id, receipt):
                    return self.receipt(preview_id)
                try:
                    if staged:
                        data = await self.files.client.call(
                            "upload_group_file",
                            group_id=selected["group_id"],
                            file=str(staged),
                            name=value["file"]["name"],
                            folder="/",
                            upload_file=True,
                        )
                        key = "file_id"
                    else:
                        data = await self.files.client.call(
                            "send_group_msg",
                            group_id=selected["group_id"],
                            message=[{"type": "text", "data": {"text": value["text"]}}],
                            auto_escape=True,
                        )
                        key = "message_id"
                    if not isinstance(data, dict) or not data.get(key):
                        raise QQFileError("PROTOCOL", "提交接口未返回可用回执。")
                    receipt.update(
                        status="accepted_by_bridge",
                        **{key: str(data[key])},
                        message="桥接返回成功回执；不代表老师已收阅或原生群作业已提交。",
                    )
                    if key == "file_id":
                        receipt["message"] += "文件上传回执也不能证明群聊天通知发布成功。"
                except (Exception, asyncio.CancelledError) as exc:
                    receipt.update(
                        status="outcome_unknown",
                        error_code=getattr(exc, "code", "INTERNAL"),
                        message=(
                            "接口调用未取得可靠回执，可能已经发送；请先在 QQ 核对，不要自动重试。"
                        ),
                    )
                    if isinstance(exc, asyncio.CancelledError):
                        self.files.store.finish_submission(preview_id, "outcome_unknown", receipt)
                        raise
                receipt["completed_at"] = time.time()
                self.files.store.finish_submission(preview_id, receipt["status"], receipt)
                return receipt
            finally:
                if staged:
                    staged.unlink(missing_ok=True)
