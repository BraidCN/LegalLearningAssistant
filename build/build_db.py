"""JSONL -> SQLite（laws / articles / articles_fts）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from lawagent.db import DEFAULT_DB, connect, init  # noqa: E402
from lawagent.segment import segment  # noqa: E402

SRC = ROOT / "data" / "interim" / "articles.jsonl"


def main():
    if not SRC.exists():
        print("缺少", SRC, "，请先运行 build/parse_articles.py")
        return 1
    if DEFAULT_DB.exists():
        DEFAULT_DB.unlink()
    conn = connect()
    init(conn)
    law_ids = {}
    n = 0
    with SRC.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            name = r["law_name"]
            if name not in law_ids:
                cur = conn.execute(
                    "INSERT INTO laws(law_name, law_level, issuer, passed_date, effective_date, status, source_name) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, r.get("law_level"), r.get("issuer"), r.get("passed_date"),
                     r.get("effective_date"), r.get("status"), r.get("source_name")),
                )
                law_ids[name] = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO articles(law_id, article_no, seq_main, seq_sub, chapter, section, content) "
                "VALUES (?,?,?,?,?,?,?)",
                (law_ids[name], r["article_no"], r.get("seq_main", 0), r.get("seq_sub", 0),
                 r.get("chapter", ""), r.get("section", ""), r["content"]),
            )
            aid = cur.lastrowid
            text = "%s %s %s" % (name, r["article_no"], segment(r["content"]))
            conn.execute("INSERT INTO articles_fts(rowid, search_text) VALUES (?,?)", (aid, text))
            n += 1
    conn.commit()
    print("laws:", conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0])
    print("articles:", n)
    print("fts:", conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
