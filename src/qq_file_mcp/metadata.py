"""Publication metadata and shared filters. Publisher means uploader or sender."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from .errors import QQFileError
from .files import normalize


def identifier(value) -> str:
    return str(value) if value and str(value) != "0" else ""


def publisher_info(value: dict, *, directory: bool = False) -> dict:
    if directory:
        return {
            "user_id": identifier(value.get("uploader")),
            "name": str(value.get("uploader_name") or ""),
            "nickname": "",
        }
    sender = value.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    return {
        "user_id": identifier(sender.get("user_id", value.get("user_id"))),
        "name": str(sender.get("card") or sender.get("nickname") or ""),
        "nickname": str(sender.get("nickname") or ""),
    }


def publisher_matches(query: str, publisher: dict) -> bool:
    query = normalize(query)
    if not query:
        return True
    if query.isdecimal():
        return query == publisher.get("user_id", "")
    return any(query in normalize(publisher.get(key, "")) for key in ("name", "nickname"))


def parse_bound(value: str | None, *, upper: bool = False) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise QQFileError("INPUT", "发布时间请使用 YYYY-MM-DD 或带时区的 ISO 日期时间。")
    try:
        stamp = datetime.fromisoformat(value.strip())
        if len(value.strip()) == 10:
            stamp = datetime.combine(stamp.date(), time(23, 59, 59) if upper else time())
            stamp = stamp.astimezone()
        elif stamp.tzinfo is None:
            raise ValueError
        return int(stamp.timestamp())
    except (ValueError, OverflowError, OSError) as exc:
        raise QQFileError("INPUT", "发布时间请使用 YYYY-MM-DD 或带时区的 ISO 日期时间。") from exc


@dataclass
class FileFilters:
    publisher: str = ""
    published_after: int | None = None
    published_before: int | None = None

    @classmethod
    def create(cls, publisher="", published_after=None, published_before=None):
        if not isinstance(publisher, str) or len(publisher) > 200:
            raise QQFileError("INPUT", "发布人关键词最多200字符。")
        after, before = parse_bound(published_after), parse_bound(published_before, upper=True)
        if after is not None and before is not None and after > before:
            raise QQFileError("INPUT", "发布时间范围的起点不能晚于终点。")
        return cls(normalize(publisher), after, before)

    def as_dict(self):
        return {
            "publisher": self.publisher,
            "published_after": self.published_after,
            "published_before": self.published_before,
        }

    def matches(self, publisher, stamp) -> bool:
        if not publisher_matches(self.publisher, publisher):
            return False
        if self.published_after is not None or self.published_before is not None:
            if not stamp:
                return False
            if self.published_after is not None and stamp < self.published_after:
                return False
            if self.published_before is not None and stamp > self.published_before:
                return False
        return True


def provenance(context: dict) -> dict:
    """Whitelist metadata only; do not persist message bodies, URLs or credentials."""
    saved = {
        key: context[key]
        for key in (
            "group_id",
            "group_name",
            "source",
            "file_name",
            "size",
            "time",
            "publisher",
            "folder_id",
            "folder_name",
            "message_id",
            "segment_index",
            "owner",
            "backend",
        )
        if key in context
    }
    if "publisher" in saved:
        saved["publisher"] = {
            key: saved["publisher"].get(key, "") for key in ("user_id", "name", "nickname")
        }
    return saved
