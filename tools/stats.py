"""数据体检：python tools/stats.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent.db import connect  # noqa: E402


def main():
    conn = connect()
    laws = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    arts = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print("法律：%d 部，条文：%d 条" % (laws, arts))
    print("\n条文最多的 8 部法：")
    rows = conn.execute(
        "SELECT l.law_name, COUNT(*) c FROM articles a JOIN laws l ON l.law_id = a.law_id "
        "GROUP BY l.law_id ORDER BY c DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        print("  %-28s %5d 条" % (r["law_name"], r["c"]))
    print("\n关键法核对：")
    for name in ["中华人民共和国民法典", "中华人民共和国刑法", "中华人民共和国民事诉讼法",
                 "中华人民共和国刑事诉讼法", "中华人民共和国行政处罚法"]:
        n = conn.execute(
            "SELECT COUNT(*) FROM articles a JOIN laws l ON l.law_id=a.law_id WHERE l.law_name=?",
            (name,),
        ).fetchone()[0]
        print("  %-28s %5d 条" % (name, n))
    print("\n抽样：")
    for r in conn.execute(
        "SELECT l.law_name, a.article_no, substr(a.content,1,50) t FROM articles a "
        "JOIN laws l ON l.law_id=a.law_id ORDER BY a.article_id DESC LIMIT 3"
    ):
        print("  《%s》%s：%s..." % (r["law_name"], r["article_no"], r["t"]))
    conn.close()


if __name__ == "__main__":
    main()
