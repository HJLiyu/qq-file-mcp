"""Small expiring result store; no chat bodies, file URLs or account secrets."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from .errors import QQFileError


class ResultStore:
    def __init__(self, directory: Path, ttl: int = 3600):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
        self.path = directory / "results.sqlite3"
        self.ttl = ttl
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS items "
                "(id TEXT PRIMARY KEY, kind TEXT, value TEXT, expires REAL)"
            )
            db.execute("DELETE FROM items WHERE expires < ?", (time.time(),))
        if os.name != "nt":
            self.path.chmod(0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def put(self, kind: str, value: dict) -> str:
        item_id = secrets.token_urlsafe(18)
        with self._connect() as db:
            db.execute("DELETE FROM items WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?)",
                (item_id, kind, json.dumps(value, ensure_ascii=False), time.time() + self.ttl),
            )
        return item_id

    def get(self, item_id: str, kind: str) -> dict:
        if not isinstance(item_id, str) or len(item_id) > 128:
            raise QQFileError("INVALID_REFERENCE", "无效的结果或继续查询标识。")
        with self._connect() as db:
            row = db.execute(
                "SELECT value, expires FROM items WHERE id=? AND kind=?", (item_id, kind)
            ).fetchone()
        if not row or row[1] < time.time():
            raise QQFileError("EXPIRED_REFERENCE", "结果已过期或不属于本机，请重新搜索。")
        return json.loads(row[0])
