"""Expiring query references and persistent download metadata; no bodies, URLs or secrets."""

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
            db.execute(
                "CREATE TABLE IF NOT EXISTS downloads "
                "(root TEXT, path TEXT, value TEXT, PRIMARY KEY(root, path))"
            )
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

    def record_download(self, root: str, path: str, identity: dict, sha256: str, origin: dict):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT value FROM downloads WHERE root=? AND path=?", (root, path)
            ).fetchone()
            saved = json.loads(old[0]) if old else {}
            origins = (
                saved.get("provenance", [])
                if (saved.get("sha256") == sha256 and saved.get("identity") == identity)
                else []
            )
            if origin not in origins:
                origins.append(origin)
            value = {"identity": identity, "sha256": sha256, "provenance": origins}
            db.execute(
                "INSERT OR REPLACE INTO downloads VALUES (?, ?, ?)",
                (root, path, json.dumps(value, ensure_ascii=False)),
            )

    def downloaded_records(self, root: str) -> dict:
        with self._connect() as db:
            return {
                path: json.loads(value)
                for path, value in db.execute(
                    "SELECT path, value FROM downloads WHERE root=?", (root,)
                )
            }
