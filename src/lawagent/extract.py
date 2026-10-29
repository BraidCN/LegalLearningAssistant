"""文件文本抽取（纯标准库 + 可选 PDF 后端）。

支持：md / markdown / txt / csv / json / html / docx
PDF：优先 pypdf，其次 PyMuPDF（fitz）；都没装时抛 Unsupported 并给安装提示
CAJ / 图片：明确不支持，提示转 PDF 或走 OCR（二期）
"""
from __future__ import annotations

import html as _html
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

TEXT_EXTS = {
    "md": "md", "markdown": "md", "txt": "text", "log": "text", "csv": "text",
    "tsv": "text", "json": "text", "yaml": "text", "yml": "text", "py": "text", "rst": "text",
}
HTML_EXTS = {"html": "html", "htm": "html", "xhtml": "html"}
UNSUPPORTED_HINTS = {
    "caj": "CAJ 是知网专有格式，请先导出为 PDF 再上传。",
    "png": "图片需要 OCR（二期支持），请先把图片转成可复制的 PDF 或文本。",
    "jpg": "图片需要 OCR（二期支持），请先把图片转成可复制的 PDF 或文本。",
    "jpeg": "图片需要 OCR（二期支持），请先把图片转成可复制的 PDF 或文本。",
    "doc": "旧版 .doc 请先另存为 .docx。",
}


class Unsupported(Exception):
    """当前环境无法解析该文件（含给用户看的中文提示）。"""


def ext_of(name: str) -> str:
    return (Path(name or "").suffix.lstrip(".") or "").lower()


def sniff(name: str, data: bytes) -> str:
    """按扩展名 + 文件头判断实际类型。"""
    e = ext_of(name)
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:2] == b"PK" and e == "docx":
        return "docx"
    if e in TEXT_EXTS:
        return TEXT_EXTS[e]
    if e in HTML_EXTS:
        return HTML_EXTS[e]
    if e == "docx":
        return "docx"
    if e == "pdf":
        return "pdf"
    if e in UNSUPPORTED_HINTS:
        raise Unsupported(UNSUPPORTED_HINTS[e])
    # 无扩展名：能解码成文本就当纯文本
    try:
        data[:4000].decode("utf-8")
        return "text"
    except Exception:
        raise Unsupported("无法识别的文件类型（%s）。" % (name or "未命名"))


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "hr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unsupported("这个 .docx 文件已损坏或不是真正的 docx。")
    try:
        xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except KeyError:
        raise Unsupported("docx 内缺少正文（word/document.xml）。")
    xml = xml.replace("</w:p>", "\n").replace("</w:tr>", "\n")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_text(data: bytes):
    """返回 (text, pages)。"""
    try:
        import pypdf  # noqa: PLC0415
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        chunks = []
        for p in reader.pages:
            try:
                chunks.append(p.extract_text() or "")
            except Exception:
                chunks.append("")
        text = "\n\n".join(chunks).strip()
        if text:
            return text, pages
        raise Unsupported("PDF 没有可提取的文本层（可能是扫描件），需要 OCR（二期）。")
    except Unsupported:
        raise
    except Exception:
        pass
    try:
        import fitz  # noqa: PLC0415
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n\n".join(page.get_text() or "" for page in doc).strip()
        pages = doc.page_count
        if not text:
            raise Unsupported("PDF 没有可提取的文本层（可能是扫描件），需要 OCR（二期）。")
        return text, pages
    except Unsupported:
        raise
    except Exception:
        raise Unsupported("PDF 解析需要先装 pypdf 或 PyMuPDF（pip install pypdf），装好后即可用。")


def _title_of(kind: str, text: str, name: str) -> str:
    stem = Path(name or "").stem
    if kind == "md":
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            return m.group(1).strip()[:120]
    if kind == "html":
        m = re.search(r"<title[^>]*>(.+?)</title>", text, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("---", "==")):
            first = line
            break
    return (first[:60] if first and len(first) <= 60 else stem) or "未命名"


def extract(name: str = "", data: bytes = None, path: str = "") -> dict:
    """抽取文本。返回 {text, kind, chars, pages, title, name}。"""
    if data is None:
        if not path:
            raise ValueError("extract 需要 data 或 path")
        name = name or Path(path).name
        data = Path(path).read_bytes()
    kind = sniff(name, data)
    pages = 0
    if kind in ("text", "md"):
        text = _decode(data)
    elif kind == "html":
        raw = _decode(data)
        parser = _HTMLText()
        parser.feed(raw)
        text = parser.text()
        # html 的 title 从原始文本里取
        text_title = re.search(r"<title[^>]*>(.+?)</title>", raw, re.I | re.S)
        if text_title:
            text = "# " + _html.unescape(text_title.group(1)).strip() + "\n\n" + text
    elif kind == "docx":
        text = _docx_text(data)
    elif kind == "pdf":
        text, pages = _pdf_text(data)
    else:
        raise Unsupported("不支持的类型：%s" % kind)
    text = (text or "").strip()
    if not text:
        raise Unsupported("文件里没有可提取的文字（空文档或纯图片）。")
    return {
        "name": name or "未命名",
        "kind": kind,
        "text": text,
        "chars": len(text),
        "pages": pages,
        "title": _title_of(kind, text, name),
    }
