import hashlib
import struct
import zipfile

import pytest

from qq_file_mcp.downloads import DownloadedFiles
from qq_file_mcp.errors import QQFileError

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path, body, namespace=W, encoding="utf-8"):
    xml = f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode(encoding))
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )


@pytest.fixture
def local(settings):
    settings.download_dir.mkdir()
    return DownloadedFiles(settings)


@pytest.mark.parametrize("namespace", [W, "http://purl.oclc.org/ooxml/wordprocessingml/main"])
async def test_docx_body_table_order_and_scope_in_real_reader(local, namespace):
    path = local.root / "作业要求.docx"
    make_docx(
        path,
        "<w:p><w:r><w:t>计算物理 &amp; 数值方法</w:t><w:tab/><w:t>要求</w:t>"
        "<w:br/><w:t>第二行</w:t></w:r><w:hyperlink><w:r><w:t>链接文字</w:t></w:r>"
        "</w:hyperlink></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>题号</w:t></w:r></w:p>"
        "</w:tc><w:tc><w:p><w:r><w:t>一</w:t></w:r></w:p><w:p><w:r><w:t>积分</w:t>"
        "</w:r></w:p></w:tc></w:tr></w:tbl><w:sdt><w:sdtContent><w:p><w:r>"
        "<w:t>截止时间</w:t></w:r></w:p></w:sdtContent></w:sdt>",
        namespace=namespace,
    )
    listing = await local.list_files("作业")
    assert listing["files"][0]["can_read"] is True
    result = await local.read(listing["files"][0]["file_id"])
    assert result["format"] == "docx"
    assert result["unit"] == "block"
    assert result["total_units"] == 3
    assert result["units"] == [
        {"index": 1, "text": "计算物理 & 数值方法\t要求\n第二行链接文字"},
        {"index": 2, "text": "题号\t一\n积分"},
        {"index": 3, "text": "截止时间"},
    ]
    assert result["next_read"] is None
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["content_is_untrusted"] is True
    assert result["warnings"][0]["code"] == "DOCX_TEXT_ONLY"
    assert "页码" in result["warnings"][0]["message"]


async def test_docx_continuation_keeps_long_paragraph_and_table_remainders(local):
    path = local.root / "长段落.docx"
    make_docx(
        path,
        "<w:p><w:r><w:t>甲乙丙丁戊己庚辛</w:t></w:r></w:p><w:tbl><w:tr>"
        "<w:tc><w:p><w:r><w:t>abcdefgh</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>末尾</w:t></w:r></w:p>",
    )
    ref = local.register(path)
    result = await local.read(ref, count=2, max_chars=3)
    assert result["next_read"] == {"start": 1, "char_offset": 3, "count": 2}
    fragments = []
    for _ in range(10):
        fragments.extend(item["text"] for item in result["units"])
        if result["next_read"] is None:
            break
        result = await local.read(ref, max_chars=3, **result["next_read"])
    assert "".join(fragments) == "甲乙丙丁戊己庚辛abcdefgh末尾"
    assert result["next_read"] is None
    partial = await local.read(ref, start=2, count=1)
    assert partial["units"] == [{"index": 2, "text": "abcdefgh"}]
    assert partial["next_read"]["start"] == 3


async def test_docx_revisions_and_omitted_objects_are_explicit(local):
    path = local.root / "修订.docx"
    make_docx(
        path,
        "<w:p><w:r><w:t>正文</w:t><w:drawing><w:txbxContent><w:p><w:r><w:t>文本框</w:t>"
        "</w:r></w:p></w:txbxContent></w:drawing></w:r><w:del><w:r><w:t>旧要求</w:t>"
        "</w:r></w:del><w:ins><w:r><w:t>新要求</w:t></w:r></w:ins></w:p>",
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../../outside.txt", "不得解压")
        archive.writestr("word/header1.xml", "损坏的页眉也不解析")
    before = set(local.root.rglob("*"))
    result = await local.read(local.register(path))
    assert result["units"] == [{"index": 1, "text": "正文新要求"}]
    assert {notice["code"] for notice in result["warnings"]} == {"DOCX_TEXT_ONLY", "DOCX_REVISIONS"}
    assert set(local.root.rglob("*")) == before


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
async def test_docx_xml_declarations_are_rejected_before_entity_expansion(local, encoding):
    path = local.root / "实体.docx"
    xml = f'<!DOCTYPE document [<!ENTITY x "private">]><w:document xmlns:w="{W}">'
    xml += "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml.encode(encoding))
    with pytest.raises(QQFileError) as error:
        await local.read(local.register(path))
    assert error.value.code == "READ_FORMAT"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("mode", ["zip", "missing", "duplicate", "xml", "namespace", "body"])
async def test_broken_docx_returns_safe_error(local, mode):
    path = local.root / "损坏.docx"
    make_docx(path, "")
    if mode == "zip":
        path.write_bytes(b"not a zip private")
    elif mode == "duplicate":
        with pytest.warns(UserWarning), zipfile.ZipFile(path, "a") as archive:
            archive.writestr("word/document.xml", "private")
    else:
        with zipfile.ZipFile(path, "w") as archive:
            value = {
                "missing": "private",
                "xml": "<broken private",
                "namespace": "<document><body>private</body></document>",
                "body": f'<w:document xmlns:w="{W}"/>',
            }[mode]
            archive.writestr("other.xml" if mode == "missing" else "word/document.xml", value)
    with pytest.raises(QQFileError) as error:
        await local.read(local.register(path))
    assert error.value.code == "READ_FORMAT"
    assert "private" not in str(error.value)
    assert str(path) not in str(error.value)


@pytest.mark.parametrize("mode", ["size", "nodes", "depth", "entries"])
async def test_docx_expansion_and_structure_limits(local, mode):
    path = local.root / "过大.docx"
    body = {
        "size": "x" * (8 * 1024 * 1024),
        "nodes": "<w:p/>" * 100001,
        "depth": "<w:sdt>" * 129 + "</w:sdt>" * 129,
        "entries": "",
    }[mode]
    make_docx(path, body)
    if mode == "entries":
        with zipfile.ZipFile(path, "a") as archive:
            for index in range(4096):
                archive.writestr(f"extra/{index}", "")
    with pytest.raises(QQFileError) as error:
        await local.read(local.register(path))
    assert error.value.code == "DOCX_TOO_LARGE"


async def test_docx_encrypted_zip_member_is_rejected(local):
    path = local.root / "加密.docx"
    make_docx(path, "")
    data = bytearray(path.read_bytes())
    for signature, flag_offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        start = data.index(signature)
        flags = struct.unpack_from("<H", data, start + flag_offset)[0]
        struct.pack_into("<H", data, start + flag_offset, flags | 1)
    path.write_bytes(data)
    with pytest.raises(QQFileError) as error:
        await local.read(local.register(path))
    assert error.value.code == "DOCX_ENCRYPTED"


async def test_empty_docx_has_an_explicit_empty_block_and_scope_warning(local):
    path = local.root / "空白.docx"
    make_docx(path, "")
    result = await local.read(local.register(path))
    assert result["units"] == [{"index": 1, "text": ""}]
    assert result["next_read"] is None
    assert result["warnings"][0]["code"] == "DOCX_TEXT_ONLY"
