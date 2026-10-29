# -*- coding: utf-8 -*-
"""多用户认证与隔离测试：邀请码、令牌、禁用、A/B 数据隔离、首启迁移。"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "serve"))

from lawagent import auth, docs  # noqa: E402
import app  # noqa: E402


def make_cfg(tmp):
    return {
        "paths": {
            "db": os.path.join(tmp, "articles.db"),
            "auth_db": os.path.join(tmp, "auth.db"),
            "admin_token_file": os.path.join(tmp, "admin_token.txt"),
        },
        "ingest": {
            "docs_db": os.path.join(tmp, "docs.db"),
            "docs_dir": os.path.join(tmp, "docs"),
            "users_dir": os.path.join(tmp, "users"),
            "chunk_chars": 120,
            "chunk_overlap": 20,
        },
        "llm": {"backend": "none"},
        "gate": {"min_quote_len": 10, "retry_once": True},
        "retrieval": {"top_k": 12},
    }


class QuietHandler(app.Handler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass


class AuthApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self._old_cfg = app.CONFIG
        app.CONFIG = self.cfg
        self.boot = auth.init(self.cfg)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        app.CONFIG = self._old_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers ----------

    def request(self, method, path, token=None, payload=None, raw=None, headers=None):
        data = raw
        if data is None and payload is not None:
            data = json.dumps(payload).encode("utf-8")
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                try:
                    return resp.status, (json.loads(body.decode("utf-8")) if body else {})
                except Exception:
                    return resp.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            return exc.code, parsed

    def admin_token(self):
        return self.boot["admin_token"]

    def create_invite(self, max_uses=1, days=7):
        status, data = self.request(
            "POST", "/api/admin/invites", self.admin_token(),
            {"max_uses": max_uses, "days": days, "note": "test"},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["code"]), 8)
        return data["code"]

    def register(self, code, name):
        return self.request("POST", "/api/auth/register", payload={"code": code, "name": name})

    def register_ok(self, name):
        code = self.create_invite()
        status, data = self.register(code, name)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["token"].startswith("lk_"))
        return data

    def upload(self, token, name="minfa-note.md",
               text="# 民法笔记\n\n善意取得需要受让人善意。\n\n意思表示是法律行为的核心。"):
        boundary = "----lawagenttest"
        body = (
            "--%s\r\n"
            'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "%s\r\n"
            "--%s--\r\n" % (boundary, name, text, boundary)
        ).encode("utf-8")
        return self.request(
            "POST", "/api/upload", token, raw=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )

    # ---------- 公开端点 / 鉴权 ----------

    def test_public_and_unauthorized(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        status, _ = self.request("GET", "/")
        self.assertEqual(status, 200)

        status, body = self.request("POST", "/api/ask", payload={"q": "拖欠工资怎么办"})
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized", "code": 401})

        status, body = self.request("POST", "/api/ask", token="lk_invalid", payload={"q": "x"})
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], 401)

    def test_register_me_and_rotate(self):
        code = self.create_invite(max_uses=2)
        status, user = self.register(code, "  张三  ")
        self.assertEqual(status, 200, user)
        self.assertTrue(user["user_id"].startswith("u_"))
        self.assertEqual(len(user["user_id"]), 14)
        self.assertEqual(user["name"], "张三")

        status, me = self.request("GET", "/api/auth/me", user["token"])
        self.assertEqual(status, 200)
        self.assertEqual(me, {"user_id": user["user_id"], "name": "张三", "role": "user"})

        status, rotated = self.request("POST", "/api/auth/rotate", user["token"], {})
        self.assertEqual(status, 200, rotated)
        self.assertNotEqual(rotated["token"], user["token"])

        status, _ = self.request("GET", "/api/auth/me", user["token"])
        self.assertEqual(status, 401)
        status, me2 = self.request("GET", "/api/auth/me", rotated["token"])
        self.assertEqual(status, 200)
        self.assertEqual(me2["user_id"], user["user_id"])

    def test_invite_reuse_and_expired(self):
        code = self.create_invite(max_uses=1)
        status, _ = self.register(code, "A")
        self.assertEqual(status, 200)

        status, body = self.register(code, "B")
        self.assertEqual(status, 400)
        self.assertIn("用完", body["error"])

        expired = self.create_invite(max_uses=1, days=-1)
        status, body = self.register(expired, "C")
        self.assertEqual(status, 400)
        self.assertIn("过期", body["error"])

    def test_disabled_user_401_403(self):
        user = self.register_ok("阿甲")
        status, users = self.request("GET", "/api/admin/users", self.admin_token())
        self.assertEqual(status, 200)
        self.assertNotIn("token_hash", json.dumps(users, ensure_ascii=False))
        self.assertTrue(any(u["user_id"] == user["user_id"] for u in users["items"]))

        status, body = self.request(
            "POST", "/api/admin/users/%s/disable" % user["user_id"],
            self.admin_token(), {"disabled": True},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["user"]["disabled"])

        status, body = self.request("GET", "/api/auth/me", user["token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 403)

        status, body = self.request("GET", "/api/docs", user["token"])
        self.assertEqual(status, 403)

        status, body = self.request("GET", "/api/docs", "lk_bogus")
        self.assertEqual(status, 401)

        status, body = self.request(
            "POST", "/api/admin/users/%s/disable" % user["user_id"],
            self.admin_token(), {"disabled": False},
        )
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/auth/me", user["token"])
        self.assertEqual(status, 200)

    def test_admin_endpoints_require_admin(self):
        user = self.register_ok("普通用户")
        status, body = self.request("POST", "/api/admin/invites", user["token"],
                                    {"max_uses": 1})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], 403)
        status, _ = self.request("GET", "/api/admin/users", user["token"])
        self.assertEqual(status, 403)

    # ---------- 数据隔离 ----------

    def test_docs_isolation_between_users(self):
        a = self.register_ok("A")
        b = self.register_ok("B")

        status, uploaded = self.upload(a["token"])
        self.assertEqual(status, 200, uploaded)
        self.assertTrue(uploaded["doc"]["doc_id"])

        status, docs_a = self.request("GET", "/api/docs", a["token"])
        self.assertEqual(status, 200)
        self.assertEqual(docs_a["count"], 1, docs_a)
        self.assertEqual(docs_a["items"][0]["name"], "minfa-note.md")

        status, docs_b = self.request("GET", "/api/docs", b["token"])
        self.assertEqual(status, 200)
        self.assertEqual(docs_b["count"], 0, docs_b)

        status, hit_a = self.request("POST", "/api/docs/search", a["token"],
                                     {"q": "善意取得"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(hit_a["count"], 1)

        status, hit_b = self.request("POST", "/api/docs/search", b["token"],
                                     {"q": "善意取得"})
        self.assertEqual(status, 200)
        self.assertEqual(hit_b["count"], 0, hit_b)

        status, empty = self.request("GET", "/api/receipts", b["token"])
        self.assertEqual(status, 200)
        self.assertEqual(empty["items"], [])

    def test_programmatic_isolation(self):
        info_a = {"name": "a.md", "kind": "md", "title": "A 笔记", "text": "善意取得需要善意。"}
        docs.add_document(info_a, b"a-unique", self.cfg, "u_aaaaaaaaaaaa")
        info_b = {"name": "b.md", "kind": "md", "title": "B 笔记", "text": "善意取得需要善意。"}
        docs.add_document(info_b, b"b-unique", self.cfg, "u_bbbbbbbbbbbb")

        self.assertEqual(len(docs.list_docs(10, self.cfg, "u_aaaaaaaaaaaa")), 1)
        self.assertEqual(len(docs.list_docs(10, self.cfg, "u_bbbbbbbbbbbb")), 1)
        self.assertEqual(docs.search("善意取得", 5, self.cfg, "u_aaaaaaaaaaaa")[0]["name"], "a.md")
        self.assertEqual(docs.search("善意取得", 5, self.cfg, "u_bbbbbbbbbbbb")[0]["name"], "b.md")

        docs.add_rule("共同笔记", "note", "", self.cfg, "u_aaaaaaaaaaaa")
        docs.add_rule("共同笔记", "case", "", self.cfg, "u_bbbbbbbbbbbb")
        self.assertEqual(docs.find_rule("共同笔记", self.cfg, "u_aaaaaaaaaaaa")["target"], "note")
        self.assertEqual(docs.find_rule("共同笔记", self.cfg, "u_bbbbbbbbbbbb")["target"], "case")


class AuthBootstrapTest(unittest.TestCase):
    """首启：创建 owner、写管理员令牌文件、旧数据迁移为 owner。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old_docs_db(self):
        conn = sqlite3.connect(self.cfg["ingest"]["docs_db"])
        conn.executescript(
            "CREATE TABLE docs(doc_id TEXT PRIMARY KEY, sha256 TEXT, name TEXT, kind TEXT,"
            " size INTEGER, path TEXT, text_path TEXT, title TEXT, target TEXT, course TEXT,"
            " chars INTEGER, chunks INTEGER, created_at TEXT);"
            "CREATE TABLE receipts(receipt_id TEXT PRIMARY KEY, doc_id TEXT, status TEXT,"
            " target TEXT, payload TEXT, created_at TEXT);"
            "CREATE TABLE rules(rule_id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT UNIQUE,"
            " target TEXT, course TEXT, hits INTEGER DEFAULT 0, created_at TEXT);"
        )
        conn.execute("INSERT INTO docs(doc_id, name, created_at) VALUES ('old1','旧笔记','2026-01-01')")
        conn.execute("INSERT INTO receipts(receipt_id, doc_id, payload, created_at)"
                     " VALUES ('r1','old1','{}','2026-01-01')")
        conn.execute("INSERT INTO rules(keyword, target, created_at)"
                     " VALUES ('旧规则','note','2026-01-01')")
        conn.commit()
        conn.close()

    def _old_articles_db(self):
        conn = sqlite3.connect(self.cfg["paths"]["db"])
        conn.executescript(
            "CREATE TABLE quiz_items(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT,"
            " question TEXT, options_json TEXT, answer TEXT, article_ref TEXT, created_at TEXT);"
            "CREATE TABLE wrong_answers(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER,"
            " user_answer TEXT, created_at TEXT);"
        )
        conn.execute("INSERT INTO quiz_items(topic, question, options_json, answer, created_at)"
                     " VALUES ('t','q','[]','A','2026-01-01')")
        conn.execute("INSERT INTO wrong_answers(item_id, user_answer, created_at)"
                     " VALUES (1,'B','2026-01-01')")
        conn.commit()
        conn.close()

    def test_first_start_owner_and_migration(self):
        self._old_docs_db()
        self._old_articles_db()
        boot = auth.init(self.cfg)

        self.assertTrue(boot["bootstrapped"])
        self.assertTrue(boot["admin_token"].startswith("lk_"))
        self.assertTrue(Path(boot["admin_token_file"]).exists())
        self.assertEqual(Path(boot["admin_token_file"]).read_text(encoding="utf-8").strip(),
                         boot["admin_token"])

        owner = auth.authenticate(boot["admin_token"], self.cfg)
        self.assertEqual(owner["user_id"], "owner")
        self.assertEqual(owner["role"], "admin")
        self.assertEqual(auth.get_user("owner", self.cfg)["name"], "owner")

        conn = docs.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT user_id FROM docs").fetchone()["user_id"], "owner")
            self.assertEqual(conn.execute("SELECT user_id FROM receipts").fetchone()["user_id"], "owner")
            self.assertEqual(conn.execute("SELECT user_id FROM rules").fetchone()["user_id"], "owner")
        finally:
            conn.close()

        conn = sqlite3.connect(self.cfg["paths"]["db"])
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(conn.execute("SELECT user_id FROM quiz_items").fetchone()["user_id"], "owner")
            self.assertEqual(conn.execute("SELECT user_id FROM wrong_answers").fetchone()["user_id"], "owner")
        finally:
            conn.close()

        # 第二次启动不再生成新令牌
        boot2 = auth.init(self.cfg)
        self.assertFalse(boot2["bootstrapped"])
        self.assertEqual(boot2["admin_token"], "")


if __name__ == "__main__":
    unittest.main()
