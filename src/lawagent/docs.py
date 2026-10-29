"""资料库：归档副本 + 分块 + FTS5 检索 + 归档回执 + 归档规则。

设计要点：
- 与法条库（articles.db）物理隔离，绝不改造权威层
- 索引只作线索，不作证据；引文核验只认 articles.db
- 所有路径由 configs/config.json 的 ingest 段控制（测试可注入临时目录）
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .config import ROOT, load_config
from .segment import segment

DEFAULT_DB = "data/db/docs.db"
DEFAULT_DIR = "data/docs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  doc_id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'owner',
  sha256 TEXT, name TEXT, kind TEXT, size INTEGER,
  path TEXT, text_path TEXT, title TEXT, target TEXT, course TEXT,
  chars INTEGER, chunks INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, seq INTEGER, page INTEGER, text TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text_seg, doc_id UNINDEXED, seq UNINDEXED, tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS receipts(
  receipt_id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'owner',
  doc_id TEXT, status TEXT, target TEXT, payload TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS rules(
  rule_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT 'owner',
  keyword TEXT NOT NULL, target TEXT, course TEXT, hits INTEGER DEFAULT 0, created_at TEXT,
  UNIQUE(user_id, keyword)
);
CREATE TABLE IF NOT EXISTS ask_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_ask_user ON ask_log(user_id);
"""

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _cfg(cfg=None):
    return cfg or load_config()


def _abs(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def db_path(cfg=None) -> Path:
    rel = _cfg(cfg).get("ingest", {}).get("docs_db", DEFAULT_DB)
    p = _abs(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def docs_dir(cfg=None) -> Path:
    p = _abs(_cfg(cfg).get("ingest", {}).get("docs_dir", DEFAULT_DIR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def users_dir(cfg=None) -> Path:
    ing = _cfg(cfg).get("ingest", {})
    rel = ing.get("users_dir")
    p = _abs(rel) if rel else (docs_dir(cfg).parent / "users")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _uid(user_id) -> str:
    uid = str(user_id or "owner").strip()
    if not _USER_ID_RE.match(uid):
        raise ValueError("非法 user_id")
    return uid


def _columns(conn, table: str):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _migrate(conn) -> None:
    for table in ("docs", "receipts", "rules"):
        if "user_id" not in _columns(conn, table):
            conn.execute(
                "ALTER TABLE %s ADD COLUMN user_id TEXT NOT NULL DEFAULT 'owner'" % table
            )
            conn.execute("UPDATE %s SET user_id='owner'" % table)

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'"
    ).fetchone()
    sql = (row["sql"] if row else "") or ""
    if "UNIQUE(user_id, keyword)" not in sql:
        # 旧库的 keyword 全局唯一会串用户，重建为按用户唯一
        conn.executescript(
            "DROP TABLE IF EXISTS rules_new;"
            "CREATE TABLE rules_new("
            " rule_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL DEFAULT 'owner', keyword TEXT NOT NULL,"
            " target TEXT, course TEXT, hits INTEGER DEFAULT 0, created_at TEXT,"
            " UNIQUE(user_id, keyword));"
            "INSERT INTO rules_new(rule_id, user_id, keyword, target, course, hits, created_at)"
            " SELECT rule_id, user_id, keyword, target, course, hits, created_at FROM rules;"
            "DROP TABLE rules;"
            "ALTER TABLE rules_new RENAME TO rules;"
        )
    conn.commit()


def connect(cfg=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(cfg)), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(name: str) -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "file").strip()) or "file"
    return base[:120]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_file(name: str, data: bytes, cfg=None, user_id: str = "owner") -> dict:
    """把原件存到该用户的 data/users/<user_id>/docs/ 下（同名不覆盖）。"""
    day = time.strftime("%Y%m%d")
    folder = users_dir(cfg) / _uid(user_id) / "docs" / day
    folder.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(name)
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, ""
    path = folder / safe
    n = 1
    while path.exists():
        path = folder / ("%s-%d%s%s" % (stem, n, dot, ext))
        n += 1
    path.write_bytes(data)
    return {"path": str(path), "sha256": sha256_bytes(data), "size": len(data), "saved_name": path.name}


def chunk_text(text: str, size: int = 700, overlap: int = 80):
    """按段落边界切块，块间少量重叠，避免切断句子。"""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        while len(p) > size * 2:  # 超长段落硬切
            head, p = p[:size], p[size - overlap:]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(head)
        if len(buf) + len(p) + 1 <= size:
            buf = (buf + "\n\n" + p).strip() if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = (buf[-overlap:] + "\n\n" + p).strip() if buf else p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def add_document(info: dict, data: bytes = None, cfg=None, user_id: str = "owner") -> dict:
    """登记一篇资料：存副本、存正文、分块入 FTS。info 来自 extract.extract()。

    同一用户上传同一内容（sha256 相同）只登记一次，重复上传直接返回已有记录。
    """
    cfg2 = _cfg(cfg)
    ing = cfg2.get("ingest", {})
    uid = _uid(user_id)
    if data is not None:
        digest = sha256_bytes(data)
        conn0 = connect(cfg2)
        try:
            row = conn0.execute(
                "SELECT * FROM docs WHERE sha256=? AND user_id=? LIMIT 1", (digest, uid)
            ).fetchone()
        finally:
            conn0.close()
        if row is not None:
            d = dict(row)
            d["duplicate"] = True
            return d
    size = len(data) if data is not None else int(info.get("size") or 0)
    saved = save_file(info.get("name") or "file", data or b"", cfg2, uid) if data is not None else {"path": "", "sha256": "", "size": size}
    text = info.get("text") or ""
    text_path = ""
    if saved["path"]:
        tp = Path(saved["path"] + ".txt")
        tp.write_text(text, encoding="utf-8")
        text_path = str(tp)

    doc_id = uuid.uuid4().hex[:12]
    chunks = chunk_text(text, int(ing.get("chunk_chars", 700)), int(ing.get("chunk_overlap", 80)))
    conn = connect(cfg2)
    try:
        conn.execute(
            "INSERT INTO docs(doc_id, user_id, sha256, name, kind, size, path, text_path, title,"
            " target, course, chars, chunks, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, uid, saved.get("sha256", ""), info.get("name", ""), info.get("kind", ""), size,
             saved.get("path", ""), text_path, info.get("title", ""), "", "", len(text), len(chunks), _now()),
        )
        for seq, c in enumerate(chunks):
            cur = conn.execute("INSERT INTO chunks(doc_id, seq, page, text) VALUES (?,?,?,?)",
                               (doc_id, seq, int(info.get("pages") or 0), c))
            conn.execute("INSERT INTO chunks_fts(rowid, text_seg, doc_id, seq) VALUES (?,?,?,?)",
                         (cur.lastrowid, segment(c), doc_id, seq))
        conn.commit()
    finally:
        conn.close()
    return {**info, "doc_id": doc_id, "path": saved.get("path", ""), "text_path": text_path,
            "sha256": saved.get("sha256", ""), "size": size, "chunk_count": len(chunks),
            "chunks": len(chunks)}


def get_doc(doc_id: str, cfg=None, user_id: str = "owner"):
    conn = connect(cfg)
    try:
        row = conn.execute(
            "SELECT * FROM docs WHERE doc_id=? AND user_id=?", (doc_id, _uid(user_id))
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def read_doc_text(doc: dict) -> str:
    tp = doc.get("text_path") or ""
    if tp and Path(tp).exists():
        return Path(tp).read_text(encoding="utf-8", errors="replace")
    return ""


def list_docs(limit: int = 50, cfg=None, user_id: str = "owner"):
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT doc_id, name, kind, title, target, course, chars, chunks, created_at "
            "FROM docs WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (_uid(user_id), limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search(q: str, k: int = 8, cfg=None, user_id: str = "owner"):
    """FTS5 检索（jieba 预分词），只返回当前用户的资料。命中不足时 LIKE 兜底。"""
    q = (q or "").strip()
    if not q:
        return []
    uid = _uid(user_id)
    toks = [t for t in segment(q).split() if len(t) >= 2 or re.match(r"^[A-Za-z0-9]+$", t)]
    conn = connect(cfg)
    try:
        rows = []
        if toks:
            match = " OR ".join('"%s"' % t.replace('"', '""') for t in toks)
            rows = conn.execute(
                "SELECT c.text AS text, c.seq AS seq, d.doc_id AS doc_id, d.name AS name, "
                "d.title AS title, bm25(chunks_fts) AS score "
                "FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.rowid JOIN docs d ON d.doc_id=c.doc_id "
                "WHERE chunks_fts MATCH ? AND d.user_id=? ORDER BY score LIMIT ?",
                (match, uid, k)).fetchall()
        if not rows:
            like = "%" + q[:40] + "%"
            rows = conn.execute(
                "SELECT c.text AS text, c.seq AS seq, d.doc_id AS doc_id, d.name AS name, "
                "d.title AS title, 0 AS score FROM chunks c JOIN docs d ON d.doc_id=c.doc_id "
                "WHERE c.text LIKE ? AND d.user_id=? ORDER BY c.chunk_id LIMIT ?",
                (like, uid, k)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            txt = d.pop("text", "")
            d["preview"] = txt[:180].replace("\n", " ")
            out.append(d)
        return out
    finally:
        conn.close()


def log_ask(user_id: str = "owner", cfg=None) -> None:
    """只记录提问者与时间，不保存问题正文。"""
    conn = connect(cfg)
    try:
        conn.execute(
            "INSERT INTO ask_log(user_id, created_at) VALUES (?,?)",
            (_uid(user_id), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def stats(cfg=None) -> dict:
    """资料库总条数 + 总提问数（管理后台概览）。"""
    conn = connect(cfg)
    try:
        docs_n = conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        asks_n = conn.execute("SELECT COUNT(*) AS n FROM ask_log").fetchone()["n"]
        return {"docs": int(docs_n), "questions": int(asks_n)}
    finally:
        conn.close()


def stats_by_user(cfg=None) -> dict:
    """每个用户的资料数与提问数；没有记录的用户不出现。"""
    conn = connect(cfg)
    try:
        out: dict = {}
        for row in conn.execute("SELECT user_id, COUNT(*) AS n FROM docs GROUP BY user_id"):
            out.setdefault(row["user_id"], {})["docs"] = int(row["n"])
        for row in conn.execute("SELECT user_id, COUNT(*) AS n FROM ask_log GROUP BY user_id"):
            out.setdefault(row["user_id"], {})["questions"] = int(row["n"])
        return out
    finally:
        conn.close()


def save_receipt(payload: dict, cfg=None, user_id: str = "owner") -> str:
    rid = payload.get("receipt_id") or uuid.uuid4().hex[:12]
    payload["receipt_id"] = rid
    payload.setdefault("created_at", _now())
    conn = connect(cfg)
    try:
        conn.execute("INSERT OR REPLACE INTO receipts(receipt_id, user_id, doc_id, status, target, payload,"
                     " created_at) VALUES (?,?,?,?,?,?,?)",
                     (rid, _uid(user_id), payload.get("doc_id", ""), payload.get("status", ""),
                      payload.get("target", ""), json.dumps(payload, ensure_ascii=False, default=str),
                      payload["created_at"]))
        conn.commit()
    finally:
        conn.close()
    return rid


def list_receipts(limit: int = 20, cfg=None, user_id: str = "owner"):
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT payload FROM receipts WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
            (_uid(user_id), limit)).fetchall()
        return [json.loads(r["payload"]) for r in rows]
    finally:
        conn.close()


def get_receipt(rid: str, cfg=None, user_id: str = "owner"):
    conn = connect(cfg)
    try:
        row = conn.execute(
            "SELECT payload FROM receipts WHERE receipt_id=? AND user_id=?",
            (rid, _uid(user_id))).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


def add_rule(keyword: str, target: str, course: str = "", cfg=None, user_id: str = "owner") -> None:
    """记住用户的归档选择：下次同关键词文件直接命中（按用户隔离）。"""
    kw = (keyword or "").strip()
    if not kw or not target:
        return
    conn = connect(cfg)
    try:
        conn.execute(
            "INSERT INTO rules(user_id, keyword, target, course, hits, created_at) VALUES (?,?,?,?,0,?) "
            "ON CONFLICT(user_id, keyword) DO UPDATE SET target=excluded.target, course=excluded.course",
            (_uid(user_id), kw[:120], target, course or "", _now()))
        conn.commit()
    finally:
        conn.close()


def find_rule(text: str, cfg=None, user_id: str = "owner"):
    """返回当前用户命中的规则（关键词包含匹配，长关键词优先）。"""
    uid = _uid(user_id)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT keyword, target, course, hits FROM rules WHERE user_id=?"
            " ORDER BY length(keyword) DESC", (uid,)).fetchall()
        t = (text or "")
        for r in rows:
            if r["keyword"] and r["keyword"] in t:
                conn.execute(
                    "UPDATE rules SET hits=hits+1 WHERE user_id=? AND keyword=?",
                    (uid, r["keyword"]))
                conn.commit()
                return dict(r)
        return None
    finally:
        conn.close()
