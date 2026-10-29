# -*- coding: utf-8 -*-
"""资料库测试：入库、分块、FTS 检索、回执、归档规则。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lawagent import docs, extract  # noqa: E402


def make_cfg(tmp):
    return {"ingest": {"docs_db": os.path.join(tmp, "docs.db"),
                       "docs_dir": os.path.join(tmp, "docs"),
                       "chunk_chars": 120, "chunk_overlap": 20}}


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_and_search(self):
        text = "# 民法笔记\n\n意思表示是法律行为的核心要素。\n\n善意取得需要受让人善意。"
        info = extract.extract("民法笔记.md", text.encode("utf-8"))
        doc = docs.add_document(info, text.encode("utf-8"), self.cfg)
        self.assertTrue(doc["doc_id"])
        self.assertGreaterEqual(doc["chunk_count"], 1)

        hits = docs.search("善意取得", 5, self.cfg)
        self.assertTrue(hits, "FTS 应能检索到内容")
        self.assertIn("善意", hits[0]["preview"])

    def test_docs_and_receipts(self):
        info = extract.extract("x.md", b"# t\n\nxx")
        doc = docs.add_document(info, b"# t\n\nxx", self.cfg)
        listed = docs.list_docs(10, self.cfg)
        self.assertEqual(listed[0]["doc_id"], doc["doc_id"])

        rid = docs.save_receipt({"doc_id": doc["doc_id"], "status": "done", "target": "note"}, self.cfg)
        got = docs.get_receipt(rid, self.cfg)
        self.assertEqual(got["status"], "done")
        self.assertEqual(len(docs.list_receipts(5, self.cfg)), 1)

    def test_rules(self):
        docs.add_rule("民法笔记", "note", "civil-general", self.cfg)
        hit = docs.find_rule("这是民法笔记的第二版", self.cfg)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["target"], "note")
        self.assertIsNone(docs.find_rule("完全无关的内容", self.cfg))

    def test_dedupe_by_sha(self):
        data = "# t\n\n同一份内容".encode("utf-8")
        d1 = docs.add_document(extract.extract("a.md", data), data, self.cfg)
        d2 = docs.add_document(extract.extract("b.md", data), data, self.cfg)
        self.assertEqual(d1["doc_id"], d2["doc_id"])
        self.assertTrue(d2.get("duplicate"))
        self.assertEqual(len(docs.list_docs(10, self.cfg)), 1)


if __name__ == "__main__":
    unittest.main()
