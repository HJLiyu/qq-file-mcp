"""Isolated reader. Invoked as a script, with no QQ client or credentials."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".xml",
    ".html",
    ".css",
    ".tex",
    ".rst",
    ".py",
    ".js",
    ".ts",
    ".c",
    ".cpp",
    ".h",
    ".sh",
    ".ps1",
    ".bat",
}


class FileReadError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def file_format(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".pdf", ".docx"}:
        return suffix[1:]
    return "text" if suffix in TEXT_EXTENSIONS else "unsupported"


def file_identity(info) -> dict:
    return {
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def inspect_path(root: Path, path: Path):
    """Reject symlinks/junctions in every component, and paths outside the pinned root."""
    try:
        relative = path.absolute().relative_to(root)
        if ".." in relative.parts or not relative.parts:
            raise ValueError
        current = root
        info = current.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError
        for part in relative.parts:
            current /= part
            info = current.lstat()
            if is_link(info):
                raise ValueError
        if not current.resolve(strict=True).is_relative_to(root):
            raise ValueError
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
        return info
    except FileNotFoundError as exc:
        raise FileReadError("LOCAL_FILE_MISSING", "本地文件已不存在，请重新列出下载文件。") from exc
    except (ValueError, OSError, RuntimeError) as exc:
        raise FileReadError(
            "LOCAL_PATH_DENIED", "只允许读取下载目录内的普通文件，拒绝链接或越界路径。"
        ) from exc


def decode_text(data):
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        choices = ["utf-32"]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        choices = ["utf-16"]
    else:
        choices = ["utf-8-sig", "gb18030"]
    for encoding in choices:
        try:
            text = data.decode(encoding)
        except UnicodeError:
            continue
        if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            break
        return text, encoding
    raise FileReadError("READ_FORMAT", "无法按支持的文本编码读取，或文件包含二进制内容。")


def docx_blocks(data):
    """Read the main body only, without extracting archives or following relationships."""
    limit = 8 * 1024 * 1024
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 4096:
            raise FileReadError("DOCX_TOO_LARGE", "DOCX 内部文件数量超过4096，停止解析。")
        matches = [entry for entry in entries if entry.filename == "word/document.xml"]
        if len(matches) != 1:
            raise FileReadError("READ_FORMAT", "DOCX 正文缺失或重复，无法可靠读取。")
        entry = matches[0]
        if entry.flag_bits & 1:
            raise FileReadError("DOCX_ENCRYPTED", "暂不支持加密 DOCX，请使用未加密的副本。")
        if entry.file_size > limit:
            raise FileReadError("DOCX_TOO_LARGE", "DOCX 正文展开后超过8 MiB，停止解析。")
        with archive.open(entry) as stream:
            xml = stream.read(limit + 1)
        if len(xml) > limit:
            raise FileReadError("DOCX_TOO_LARGE", "DOCX 正文展开后超过8 MiB，停止解析。")
    # Removing NULs also detects declarations in UTF-16/32 XML.
    declarations = xml.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise FileReadError("READ_FORMAT", "DOCX 正文包含不支持的 XML 声明，停止解析。")
    depth, nodes = 0, 0
    parser = ET.iterparse(io.BytesIO(xml), events=("start", "end"))
    for event, _ in parser:
        if event == "start":
            depth += 1
            nodes += 1
            if depth > 128 or nodes > 100000:
                raise FileReadError("DOCX_TOO_LARGE", "DOCX 正文结构过于复杂，停止解析。")
        else:
            depth -= 1
    root = parser.root
    namespaces = {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
    namespace = root.tag.removeprefix("{").split("}")[0]
    if namespace not in namespaces or root.tag != f"{{{namespace}}}document":
        raise FileReadError("READ_FORMAT", "文件不是支持的 DOCX 正文格式。")
    w = f"{{{namespace}}}"
    body = root.find(w + "body")
    if body is None:
        raise FileReadError("READ_FORMAT", "DOCX 缺少正文，无法读取。")
    omitted = {w + tag for tag in ("drawing", "pict", "object", "del", "moveFrom", "txbxContent")}
    containers = {w + tag for tag in ("body", "sdt", "sdtContent", "customXml", "ins", "moveTo")}

    def paragraph_text(paragraph):
        parts, pending = [], [paragraph]
        while pending:
            node = pending.pop()
            if node.tag in omitted or not node.tag.startswith(w):
                continue
            if node.tag == w + "t":
                parts.append(node.text or "")
            elif node.tag == w + "tab":
                parts.append("\t")
            elif node.tag in {w + "br", w + "cr"}:
                parts.append("\n")
            else:
                pending.extend(reversed(node))
        return "".join(parts)

    def paragraphs(container):
        pending = [container]
        while pending:
            node = pending.pop()
            if node.tag == w + "p":
                yield paragraph_text(node)
            elif node.tag in containers or node.tag == w + "tc":
                pending.extend(reversed(node))

    blocks, pending = [], [body]
    while pending:
        node = pending.pop()
        if node.tag == w + "p":
            blocks.append(paragraph_text(node))
        elif node.tag == w + "tbl":
            for row in node.findall(w + "tr"):
                blocks.append(
                    "\t".join("\n".join(paragraphs(cell)) for cell in row.findall(w + "tc"))
                )
        elif node.tag in containers:
            pending.extend(reversed(node))
    notices = [
        {
            "code": "DOCX_TEXT_ONLY",
            "message": "按正文段落和表格行读取，不提供页码；图片、公式、页眉页脚、批注、"
            "文本框、嵌套表格、自动编号和版式未解析。",
        }
    ]
    if any(
        node.tag in {w + "ins", w + "del", w + "moveFrom", w + "moveTo"} for node in body.iter()
    ):
        notices.append(
            {
                "code": "DOCX_REVISIONS",
                "message": "文档含修订；读取包含插入文字、跳过删除文字，请在原文确认最终要求。",
            }
        )
    return blocks or [""], notices


def extract(request, data):
    kind = request["format"]
    encoding = None
    notices = []
    if kind == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FileReadError("PDF_ENCRYPTED", "暂不支持加密 PDF，请使用未加密的副本。")
        total = len(reader.pages)

        def unit_text(index):
            page = reader.pages[index]
            contents = page.get_contents()
            if contents and len(contents.get_data()) > 8 * 1024 * 1024:
                raise FileReadError("PAGE_TOO_LARGE", "PDF 页内容流过大，停止解析此页。")
            return page.extract_text() or ""
    elif kind == "docx":
        blocks, notices = docx_blocks(data)
        total = len(blocks)

        def unit_text(index):
            return blocks[index]
    else:
        text, encoding = decode_text(data)
        lines = text.splitlines() or [""]
        total = len(lines)

        def unit_text(index):
            return lines[index]

    start, count, offset = request["start"], request["count"], request["char_offset"]
    if start > total:
        raise FileReadError("INVALID_INPUT", "起始页、行或正文块超出文件范围。")
    units, remaining, truncated, next_read = [], request["max_chars"], False, None
    for index in range(start - 1, min(total, start - 1 + count)):
        text = unit_text(index)
        current_offset = offset if index == start - 1 else 0
        if current_offset > len(text):
            raise FileReadError("INVALID_INPUT", "字符偏移超出当前页、行或正文块的范围。")
        fragment = text[current_offset : current_offset + remaining]
        units.append({"index": index + 1, "text": fragment})
        remaining -= len(fragment)
        if current_offset + len(fragment) < len(text):
            truncated = True
            next_read = {
                "start": index + 1,
                "char_offset": current_offset + len(fragment),
                "count": count,
            }
            break
        if index + 1 < total:
            next_read = {"start": index + 2, "char_offset": 0, "count": count}
        else:
            next_read = None
        if remaining == 0:
            truncated = next_read is not None
            break
    result = {
        "unit": "page" if kind == "pdf" else "block" if kind == "docx" else "line",
        "total_units": total,
        "units": units,
        "start": start,
        "end": units[-1]["index"],
        "truncated": truncated,
        "next_read": next_read,
        "warnings": notices,
    }
    if encoding:
        result["encoding"] = encoding
    empty_pages = [item["index"] for item in units if not item["text"].strip()]
    if kind == "pdf" and empty_pages:
        result["warnings"].append(
            {
                "code": "NO_EXTRACTABLE_TEXT",
                "pages": empty_pages,
                "message": "所读取页面没有可提取文字，可能为扫描页或空白页；本工具不进行 OCR。",
            }
        )
    return result


def read_request(request):
    root, path = Path(request["root"]), Path(request["path"])
    expected = request["identity"]
    if file_identity(inspect_path(root, path)) != expected:
        raise FileReadError("FILE_CHANGED", "本地文件已变化，请重新列出下载文件。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if file_identity(os.fstat(stream.fileno())) != expected:
            raise FileReadError("FILE_CHANGED", "本地文件已变化，请重新列出下载文件。")
        data = stream.read(request["max_bytes"] + 1)
        if len(data) > request["max_bytes"]:
            raise FileReadError("FILE_TOO_LARGE", "文件超过配置的读取大小上限。")
        if file_identity(os.fstat(stream.fileno())) != expected:
            raise FileReadError("FILE_CHANGED", "本地文件在读取过程中变化，请重新查询。")
    digest = hashlib.sha256(data).hexdigest()
    if request.get("sha256") and request["sha256"] != digest:
        raise FileReadError("FILE_CHANGED", "本地内容与下载时的 SHA256 不一致，请重新下载。")
    result = extract(request, data)
    if file_identity(inspect_path(root, path)) != expected:
        raise FileReadError("FILE_CHANGED", "本地文件在读取过程中变化，请重新查询。")
    return {"ok": True, **result, "sha256": digest, "content_is_untrusted": True}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.simplefilter("ignore")
    try:
        result = read_request(json.load(sys.stdin))
    except FileReadError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except Exception:
        # Never echo parser exceptions or document contents into diagnostics.
        result = {
            "ok": False,
            "error": {
                "code": "READ_FORMAT",
                "message": "文档解析失败，可能损坏或使用了不支持的格式。",
            },
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
