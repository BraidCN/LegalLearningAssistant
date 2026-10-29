"""命令行入口。

用法：
  python cli.py search "拖欠工资"
  python cli.py ask "公司拖欠工资怎么办"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from lawagent import pipeline  # noqa: E402
from lawagent.config import load_config  # noqa: E402
from lawagent.search import search  # noqa: E402


def main(argv):
    if len(argv) < 3 or argv[1] not in ("search", "ask"):
        print('用法: python cli.py search "拖欠工资" | python cli.py ask "公司拖欠工资怎么办"')
        return 2
    cmd, q = argv[1], " ".join(argv[2:])
    cfg = load_config()
    if cmd == "search":
        rows = search(q, k=10)
        for i, r in enumerate(rows, 1):
            print("%2d. 《%s》%s（%s） score=%.2f" % (i, r["law_name"], r["article_no"], r.get("status"), r.get("score", 0)))
            print("    " + (r.get("content") or "").strip().replace("\n", " ")[:100])
        if not rows:
            print("（无结果）")
        return 0
    out = pipeline.ask(q, cfg=cfg)
    print("== 回答 ==")
    print(out["answer"])
    print("\n== 门禁 ==", "通过" if out["gate"]["pass"] else "未通过")
    for c in out["gate"]["citations"]:
        print(" - 《%s》%s 在库:%s 引文:%s" % (c["law"], c["article_no"], c["found_in_db"], c["quote_ok"]))
    for e in out["gate"]["errors"]:
        print(" ! " + e)
    route = out.get("route") or {}
    if route:
        print("\n== 路由 ==", "意图=%s 后端=%s 联网=%s" % (
            route.get("intent"), route.get("backend"), "是" if route.get("use_web") else "否"))
        for r in route.get("reasons", []):
            print(" - " + r)
        print(" - 网络来源：%d 条" % len(out.get("web_sources") or []))
    print("\n耗时 %d ms，检索到 %d 条（门禁检查 %d 处引用）" % (
        out["elapsed_ms"], len(out.get("retrieved", [])), len(out["gate"]["citations"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
