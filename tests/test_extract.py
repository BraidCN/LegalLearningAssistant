# -*- coding: utf-8 -*-
"""文件抽取测试：md / txt / html / docx / pdf(缺失后端) / 不支持类型。"""
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lawagent import extract  # noqa: E402


def make_docx(paragraphs):
    """构造一个最小可解析的 docx。"""
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>%s</w:body></w:document>" % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


class ExtractTest(unittest.TestCase):
    def test_markdown_title(self):
        r = extract.extract("note.md", "# 民法总论笔记\n\n意思表示是核心。".encode("utf-8"))
        self.assertEqual(r["kind"], "md")
        self.assertEqual(r["title"], "民法总论笔记")
        self.assertIn("意思表示", r["text"])

    def test_txt_gbk(self):
        r = extract.extract("a.txt", "善意取得".encode("gb18030"))
        self.assertEqual(r["kind"], "text")
        self.assertIn("善意取得", r["text"])

    def test_html_strip(self):
        html = "<html><head><title>法理笔记</title></head><body><p>法律行为</p><script>x=1</script></body></html>"
        r = extract.extract("a.html", html.encode("utf-8"))
        self.assertEqual(r["kind"], "html")
        self.assertIn("法理笔记", r["title"])
        self.assertIn("法律行为", r["text"])
        self.assertNotIn("x=1", r["text"])

    def test_docx(self):
        r = extract.extract("a.docx", make_docx(["第一段：正当防卫", "第二段：防卫过当"]))
        self.assertEqual(r["kind"], "docx")
        self.assertIn("正当防卫", r["text"])
        self.assertIn("防卫过当", r["text"])

    def test_pdf_without_backend(self):
        try:
            import pypdf  # noqa: F401
        except Exception:
            try:
                import fitz  # noqa: F401
            except Exception:
                with self.assertRaises(extract.Unsupported):
                    extract.extract("a.pdf", b"%PDF-1.4\n%fake")
                return
        self.skipTest("环境里已装 PDF 后端")

    def test_caj_unsupported(self):
        with self.assertRaises(extract.Unsupported):
            extract.extract("a.caj", b"CAJ")

    def test_empty(self):
        with self.assertRaises(extract.Unsupported):
            extract.extract("a.txt", b"   \n  ")


if __name__ == "__main__":
    unittest.main()
