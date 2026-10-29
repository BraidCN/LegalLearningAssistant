"""lawtext 增量更新：下载最新 tarball → 按文件哈希比对 → 只重导变化的法规。

用法：
  python build/update_lawtext.py              # 下载 + 更新 + 提示重建向量
  python build/update_lawtext.py --no-download
  python build/update_lawtext.py --init-manifest   # 首次：只记录当前文件哈希
"""
import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "build"))
from lawagent.db import connect  # noqa: E402
from lawagent.segment import segment  # noqa: E402
from import_lawtext import DIRS, SRC, STATUS_MAP, parse_articles, parse_front  # noqa: E402

DATA = ROOT / "data" / "raw" / "lawtext"
TARBALL = DATA / "laws-latest.tar.gz"
MIRROR = "https://ghfast.top/https://github.com/lawtext/laws/archive/refs/heads/main.tar.gz"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS lawtext_files(
        lk_id TEXT PRIMARY KEY, path TEXT, sha256 TEXT, updated_at TEXT)""")
    conn.commit()


def download():
    print("下载 lawtext 最新版…")
    r = subprocess.run(["curl", "-L", "--retry", "3", "--max-time", "1800", "-o", str(TARBALL), MIRROR])
    if r.returncode != 0 or not TARBALL.exists():
        raise SystemExit("下载失败")
    out = DATA / "latest"
    if out.exists():
        shutil.rmtree(out)
    with tarfile.open(TARBALL) as t:
        t.extractall(out)
    root = next(p for p in out.iterdir() if p.is_dir())
    print("解压到", root)
    return root / "content"


def init_manifest(conn, content: Path):
    ensure_table(conn)
    n = 0
    for d in DIRS:
        for p in sorted((content / d).glob("*.md")):
            meta, _ = parse_front(p.read_text(encoding="utf-8", errors="replace"))
            lk = meta.get("id")
            if not lk:
                continue
            conn.execute("INSERT OR REPLACE INTO lawtext_files(lk_id,path,sha256,updated_at) VALUES (?,?,?,?)",
                         (lk, p.name, sha256(p), time.strftime("%Y-%m-%d")))
            n += 1
    conn.commit()
    print("manifest 初始化：%d 个文件" % n)


def update(conn, content: Path):
    ensure_table(conn)
    known = {r[0]: r[1] for r in conn.execute("SELECT lk_id, sha256 FROM lawtext_files")}
    changed, added, same = [], [], 0
    for d in DIRS:
        for p in sorted((content / d).glob("*.md")):
            meta, body = parse_front(p.read_text(encoding="utf-8", errors="replace"))
            lk = meta.get("id")
            if not lk:
                continue
            h = sha256(p)
            if lk not in known:
                added.append((p, meta, body, h))
            elif known[lk] != h:
                changed.append((p, meta, body, h))
            else:
                same += 1
    print("新增 %d / 变更 %d / 未变 %d" % (len(added), len(changed), same))

    stats = {"new": 0, "updated": 0, "articles": 0}
    for p, meta, body, h in added + changed:
        arts = parse_articles(body)
        if not arts:
            continue
        name = meta["title"].strip()
        status = STATUS_MAP.get(meta.get("status", ""), "未知")
        level = meta.get("group") or p.parent.name
        url = (meta.get("urls_list") or [""])[0]
        lk = meta.get("id")
        row = conn.execute("SELECT law_id FROM laws WHERE lk_id=?", (lk,)).fetchone()
        if row:
            law_id = row[0]
            conn.execute("DELETE FROM articles_fts WHERE rowid IN (SELECT article_id FROM articles WHERE law_id=?)", (law_id,))
            conn.execute("DELETE FROM articles WHERE law_id=?", (law_id,))
            conn.execute("UPDATE laws SET law_name=?,law_level=?,issuer=?,passed_date=?,effective_date=?,status=?,source_url=? WHERE law_id=?",
                         (name, level, meta.get("author", ""), meta.get("date", ""),
                          meta.get("effective_date", ""), status, url, law_id))
            stats["updated"] += 1
        else:
            law_id = conn.execute("SELECT COALESCE(MAX(law_id),0)+1 FROM laws").fetchone()[0]
            conn.execute("INSERT INTO laws(law_id,law_name,law_level,issuer,passed_date,effective_date,status,source_name,lk_id,source_url)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (law_id, name, level, meta.get("author", ""), meta.get("date", ""),
                          meta.get("effective_date", ""), status, "lawtext", lk, url))
            stats["new"] += 1
        seen = set()
        for a in arts:
            if a["article_no"] in seen:
                continue
            seen.add(a["article_no"])
            cur = conn.execute("INSERT INTO articles(law_id,article_no,seq_main,seq_sub,chapter,section,content)"
                               " VALUES (?,?,?,?,?,?,?)",
                               (law_id, a["article_no"], a["seq_main"], 0, a["chapter"], a["section"], a["content"]))
            conn.execute("INSERT INTO articles_fts(rowid,search_text) VALUES (?,?)",
                         (cur.lastrowid, "%s %s %s" % (name, a["article_no"], segment(a["content"]))))
            stats["articles"] += 1
        conn.execute("INSERT OR REPLACE INTO lawtext_files(lk_id,path,sha256,updated_at) VALUES (?,?,?,?)",
                     (lk, p.name, h, time.strftime("%Y-%m-%d")))
        conn.commit()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--init-manifest", action="store_true")
    a = ap.parse_args()
    content = SRC if a.no_download else download()
    conn = connect()
    if a.init_manifest:
        init_manifest(conn, content)
        return
    stats = update(conn, content)
    print("更新完成：新增 %d 部 / 更新 %d 部 / 新插入 %d 条" % (stats["new"], stats["updated"], stats["articles"]))
    changed = bool(stats["new"] or stats["updated"])
    if changed:
        print("下一步：重建向量索引 →  .venv\\Scripts\\python.exe build\\embed.py --no-resume")
    conn.close()
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
