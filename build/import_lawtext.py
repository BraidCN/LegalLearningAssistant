"""把 lawtext（FLK 转制）导入法条库：带效力状态、施行日期、官方链接。

用法：
  python build/import_lawtext.py --limit 20 --dry-run
  python build/import_lawtext.py
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from lawagent.db import DEFAULT_DB, connect  # noqa: E402
from lawagent.segment import segment  # noqa: E402

SRC = ROOT / "data" / "raw" / "lawtext" / "laws-main" / "content"
DIRS = ["宪法", "法律", "行政法规", "司法解释", "监察法规"]
STATUS_MAP = {"有效": "现行有效", "已修改": "已修改", "已废止": "已废止",
              "尚未生效": "尚未生效", "未知": "未知"}
ART_RE = re.compile(r"^-?\s*\*\*(第[一二三四五六七八九十百千零〇]+条(?:之[一二三四五六七八九十]+)?)\*\*\s*(.*)$")
CHAP_RE = re.compile(r"^##\s+(第[一二三四五六七八九十百千]+章.*)$")
SECT_RE = re.compile(r"^###\s+(第[一二三四五六七八九十百千]+节.*)$")


def parse_front(text: str):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw, body = text[3:end], text[end + 4:]
    meta, key = {}, None
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - ") and key:
            meta.setdefault(key + "_list", []).append(line[4:].strip().strip("'"))
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            key = k.strip()
            meta[key] = v.strip().strip("'\"")
    return meta, body


def parse_articles(body: str):
    out, chapter, section, cur = [], "", "", None
    for line in body.splitlines():
        s = line.strip()
        m = CHAP_RE.match(s)
        if m:
            chapter, section = m.group(1).strip(), ""
            continue
        m = SECT_RE.match(s)
        if m:
            section = m.group(1).strip()
            continue
        if s.startswith("## ") or s.startswith("---") or s.startswith(">"):
            continue
        m = ART_RE.match(s)
        if m:
            if cur:
                out.append(cur)
            cur = {"article_no": m.group(1), "chapter": chapter, "section": section,
                   "content": m.group(2).strip()}
            continue
        if cur is not None and s and not s.startswith("- 第"):
            cur["content"] += ("\n" + s) if cur["content"] else s
    if cur:
        out.append(cur)
    for i, a in enumerate(out, 1):
        a["seq_main"] = i
    return out


def migrate(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(laws)")]
    if "lk_id" in cols:
        return
    conn.executescript("""
    PRAGMA foreign_keys=off;
    ALTER TABLE laws RENAME TO laws_old;
    CREATE TABLE laws (
      law_id INTEGER PRIMARY KEY,
      law_name TEXT NOT NULL,
      law_level TEXT,
      issuer TEXT,
      passed_date TEXT,
      effective_date TEXT,
      status TEXT,
      source_name TEXT,
      lk_id TEXT,
      source_url TEXT,
      superseded_by TEXT
    );
    INSERT INTO laws(law_id,law_name,law_level,issuer,passed_date,effective_date,status,source_name)
      SELECT law_id,law_name,law_level,issuer,passed_date,effective_date,status,source_name FROM laws_old;
    DROP TABLE laws_old;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_laws_lk ON laws(lk_id) WHERE lk_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_laws_name ON laws(law_name);
    """)
    conn.commit()
    print("laws 表已迁移（增加 lk_id / source_url / superseded_by）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    migrate(conn)
    existing = {r[0]: r[1] for r in conn.execute("SELECT law_name, law_id FROM laws")}
    max_id = conn.execute("SELECT COALESCE(MAX(law_id),0) FROM laws").fetchone()[0]

    files = []
    for d in DIRS:
        files += sorted((SRC / d).glob("*.md"))
    if args.limit:
        files = files[:args.limit]

    stats = {"docs": 0, "new_laws": 0, "updated": 0, "articles": 0, "skipped": 0}
    for p in files:
        meta, body = parse_front(p.read_text(encoding="utf-8", errors="replace"))
        arts = parse_articles(body)
        if not arts or not meta.get("title"):
            stats["skipped"] += 1
            continue
        name = meta["title"].strip()
        status = STATUS_MAP.get(meta.get("status", ""), "未知")
        level = meta.get("group") or p.parent.name
        issuer = meta.get("author", "")
        passed = meta.get("date", "")
        effective = meta.get("effective_date", "")
        url = (meta.get("urls_list") or [""])[0]
        lk_id = meta.get("id", "")
        stats["docs"] += 1

        if args.dry_run:
            print("%-6s %-40s %-30s %3d 条" % (level, name[:40], status, len(arts)))
            continue

        dup = conn.execute("SELECT law_id, source_name FROM laws WHERE law_name=? AND status=?",
                           (name, status)).fetchone()
        if dup:
            conn.execute("UPDATE laws SET lk_id=COALESCE(lk_id,?), source_url=COALESCE(source_url,?),"
                         " effective_date=COALESCE(NULLIF(effective_date,''),?) WHERE law_id=?",
                         (lk_id, url, effective, dup[0]))
            stats["updated"] += 1
            continue

        max_id += 1
        conn.execute(
            "INSERT INTO laws(law_id,law_name,law_level,issuer,passed_date,effective_date,status,source_name,lk_id,source_url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (max_id, name, level, issuer, passed, effective, status, "lawtext", lk_id, url))
        seen = set()
        for a in arts:
            if a["article_no"] in seen:
                stats["dup_articles"] = stats.get("dup_articles", 0) + 1
                continue
            seen.add(a["article_no"])
            cur = conn.execute(
                "INSERT INTO articles(law_id,article_no,seq_main,seq_sub,chapter,section,content)"
                " VALUES (?,?,?,?,?,?,?)",
                (max_id, a["article_no"], a["seq_main"], 0, a["chapter"], a["section"], a["content"]))
            aid = cur.lastrowid
            conn.execute("INSERT INTO articles_fts(rowid,search_text) VALUES (?,?)",
                         (aid, "%s %s %s" % (name, a["article_no"], segment(a["content"]))))
            stats["articles"] += 1
        stats["new_laws"] += 1
        if stats["new_laws"] % 100 == 0:
            conn.commit()
            print("... 已导入", stats["new_laws"], "部")

    if not args.dry_run:
        conn.commit()
    print(json_dump(stats))
    print("lawtext 覆盖：", conn.execute("SELECT COUNT(*) FROM laws WHERE source_name='lawtext'").fetchone()[0])
    print("总 laws：", conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0],
          " 总 articles：", conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
    conn.close()


def json_dump(d):
    return " | ".join("%s=%s" % (k, v) for k, v in d.items())


if __name__ == "__main__":
    main()
