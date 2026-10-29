# -*- coding: utf-8 -*-
"""归档 Agent 测试：决策、待确认、执行落盘、引用核验、规则记忆。"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lawagent import agent, docs, extract  # noqa: E402


def make_articles_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE laws(law_id INTEGER PRIMARY KEY, law_name TEXT, status TEXT);"
        "CREATE TABLE articles(article_id INTEGER PRIMARY KEY, law_id INTEGER, article_no TEXT, content TEXT);"
    )
    conn.execute("INSERT INTO laws VALUES (97,'中华人民共和国民法典','现行有效')")
    conn.execute("INSERT INTO laws VALUES (50,'中华人民共和国合同法','已废止')")
    conn.execute("INSERT INTO articles(law_id, article_no, content) VALUES "
                 "(97,'第一百三十三条','民事法律行为是民事主体通过意思表示设立、变更、终止民事法律关系的行为。')")
    conn.execute("INSERT INTO articles(law_id, article_no, content) VALUES "
                 "(97,'第一百四十三条','具备下列条件的民事法律行为有效：（一）行为人具有相应的民事行为能力。')")
    conn.commit()
    conn.close()


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {
            "paths": {"db": os.path.join(self.tmp, "articles.db")},
            "gate": {"min_quote_len": 10},
            "ingest": {"docs_db": os.path.join(self.tmp, "docs.db"),
                       "docs_dir": os.path.join(self.tmp, "docs"),
                       "wiki_raw": os.path.join(self.tmp, "raw"),
                       "reports_dir": os.path.join(self.tmp, "reports"),
                       "confirm_below": 0.6},
            "llm": {"backend": "none"},
        }
        make_articles_db(self.cfg["paths"]["db"])
        self._orig = agent.llm_chat
        agent.llm_chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no llm"))

    def tearDown(self):
        agent.llm_chat = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, name="民法总论课堂笔记.md", text="# 民法总论笔记\n\n意思表示是法律行为的核心。\n\n《民法典》第一百三十三条：民事法律行为是民事主体通过意思表示设立、变更、终止民事法律关系的行为。"):
        info = extract.extract(name, text.encode("utf-8"))
        return docs.add_document(info, text.encode("utf-8"), self.cfg)

    def test_need_confirm_when_ambiguous(self):
        doc = self._doc(name="IMG_0001.md", text="# 随手记\n\n今天看了点东西。")
        r = agent.run(doc["doc_id"], prompt="", cfg=self.cfg)
        self.assertEqual(r["status"], "need_confirm")
        self.assertTrue(r.get("options"))

    def test_explicit_note_writes_inbox(self):
        doc = self._doc()
        r = agent.run(doc["doc_id"], prompt="这是民法总论的课堂笔记，归到课程笔记", cfg=self.cfg)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["target"], "note")
        self.assertEqual(r["plan"]["course"], "civil-general")
        files = list(Path(self.cfg["ingest"]["wiki_raw"]).rglob("*.md"))
        self.assertTrue(files, "应写入 wiki 收件箱")
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("# 民法总论笔记", body)

    def test_verify_citations_pass_and_miss(self):
        conn = sqlite3.connect(self.cfg["paths"]["db"])
        conn.row_factory = sqlite3.Row
        text = ("《民法典》第一百三十三条：民事法律行为是民事主体通过意思表示设立、变更、终止民事法律关系的行为。"
                "《民法典》第九百九十九条：根本不存在的条文。")
        ver = agent.verify_citations(text, conn)
        self.assertEqual(ver["checked"], 2)
        self.assertEqual(ver["failed"], 1)
        self.assertEqual(ver["items"][0]["quote_ok"], True)
        self.assertFalse(ver["items"][1]["found"])
        conn.close()

    def test_law_target_writes_report_only(self):
        doc = self._doc()
        r = agent.run(doc["doc_id"], prompt="核对法条", cfg=self.cfg)
        self.assertEqual(r["target"], "law")
        self.assertTrue(Path(r["produced"]).exists())
        self.assertIn("共发现条文引用", Path(r["produced"]).read_text(encoding="utf-8"))

    def test_confirm_learns_rule(self):
        doc = self._doc(name="判例汇编.md", text="# 判例\n\n正当防卫案例若干。")
        r = agent.run(doc["doc_id"], prompt="", cfg=self.cfg)
        self.assertEqual(r["status"], "need_confirm")
        r2 = agent.confirm(r["receipt_id"], "case", "", self.cfg)
        self.assertEqual(r2["target"], "case")
        self.assertIsNotNone(docs.find_rule("判例汇编 第二版", self.cfg))


if __name__ == "__main__":
    unittest.main()
