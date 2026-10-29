# -*- coding: utf-8 -*-
"""FTS5 查询转义回归测试：1.3、a.b、引号这类字符不能让 MATCH 报语法错。"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import search as search_mod  # noqa: E402
from lawagent.segment import tokens  # noqa: E402


class MatchExprTest(unittest.TestCase):
    def test_tokens_are_quoted(self):
        self.assertEqual(search_mod._match(["合同", "违约"]), '"合同" "违约"')

    def test_dotted_number_is_quoted(self):
        self.assertEqual(search_mod._match(["1.3", "亿"]), '"1.3" "亿"')

    def test_double_quote_is_escaped(self):
        self.assertEqual(search_mod._match(['a"b']), '"a""b"')

    def test_fts5_accepts_dotted_token(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        conn.execute("INSERT INTO t(body) VALUES ('total 1.3 billion')")
        expr = search_mod._match(["total", "1.3", "billion"])
        rows = conn.execute("SELECT body FROM t WHERE t MATCH ?", (expr,)).fetchall()
        self.assertTrue(rows)
        conn.close()

    def test_plain_join_would_fail(self):
        """证明修复前的写法确实会炸，防止有人改回去。"""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM t WHERE t MATCH ?", ("total 1.3 billion",)).fetchall()
        conn.close()


if __name__ == "__main__":
    unittest.main()
