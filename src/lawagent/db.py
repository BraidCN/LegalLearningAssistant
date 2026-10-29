"""SQLite 连接与表结构。"""
import sqlite3
from pathlib import Path

from .config import ROOT

DEFAULT_DB = ROOT / "data" / "db" / "articles.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS laws (
  law_id INTEGER PRIMARY KEY,
  law_name TEXT NOT NULL UNIQUE,
  law_level TEXT,
  issuer TEXT,
  passed_date TEXT,
  effective_date TEXT,
  status TEXT,
  source_name TEXT
);
CREATE TABLE IF NOT EXISTS articles (
  article_id INTEGER PRIMARY KEY,
  law_id INTEGER NOT NULL,
  article_no TEXT NOT NULL,
  seq_main INTEGER,
  seq_sub INTEGER DEFAULT 0,
  chapter TEXT,
  section TEXT,
  content TEXT NOT NULL,
  UNIQUE(law_id, article_no)
);
CREATE INDEX IF NOT EXISTS idx_articles_law ON articles(law_id);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(search_text, tokenize='unicode61');
"""


def connect(path=None):
    p = Path(path) if path else DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
