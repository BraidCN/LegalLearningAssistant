"""检索：FTS5 关键词路 + 向量路，用 RRF 倒数排序融合。

没有向量索引（或嵌入服务不可用）时，行为与纯 FTS 版本完全一致：
关键词路 -> 同义词扩展 OR -> LIKE 兜底。向量路只在「连接指向 configs
里那个 articles.db」时启用（索引 ids 是那个库的），临时库/内存库直接跳过。

score 字段保持 FTS 量纲（-bm25），因为 router 用它和 routing.min_score 比；
纯向量命中按 ``余弦 × VEC_SCORE_SCALE`` 折算。融合排序分放在 ``rrf`` 字段。
"""
from pathlib import Path
import sqlite3

from .config import ROOT, load_config, load_synonyms
from .db import connect
from .segment import normalize_query, tokens

try:  # 向量路是可选增强：import 失败也不能影响 FTS
    from . import vector
except Exception:  # pragma: no cover - vector.py 只用标准库，正常不会失败
    vector = None

# RRF 常数：score = Σ 1/(K + rank)，rank 从 1 起
RRF_K = 60

# 向量命中折算到 FTS 量纲的系数（bge-m3 余弦 0.7 → 21 分，约等于 min_score 20）
VEC_SCORE_SCALE = 30.0

_SQL = (
    "SELECT a.article_id, a.article_no, a.chapter, a.section, a.content, "
    "l.law_name, l.status, bm25(articles_fts) AS bm "
    "FROM articles_fts JOIN articles a ON a.article_id = articles_fts.rowid "
    "JOIN laws l ON l.law_id = a.law_id "
    "WHERE articles_fts MATCH ? ORDER BY bm LIMIT ?"
)

_LIKE_SQL = (
    "SELECT a.article_id, a.article_no, a.chapter, a.section, a.content, "
    "l.law_name, l.status, 0.0 AS bm "
    "FROM articles a JOIN laws l ON l.law_id = a.law_id "
    "WHERE a.content LIKE ? LIMIT ?"
)

_BY_ID_SQL = (
    "SELECT a.article_id, a.article_no, a.chapter, a.section, a.content, "
    "l.law_name, l.status "
    "FROM articles a JOIN laws l ON l.law_id = a.law_id "
    "WHERE a.article_id IN (%s)"
)


def _rows_to_dicts(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["score"] = -float(d.pop("bm", 0.0))
        d["source"] = "fts"
        out.append(d)
    return out


def _match(toks, op: str = " ") -> str:
    """把分词结果转成 FTS5 MATCH 表达式：每个词加双引号，避免 1.3 / a.b 里的点被当成语法。"""
    quoted = ['"%s"' % str(t).replace('"', '""') for t in toks if str(t).strip()]
    return op.join(quoted)


def _fts(conn, match: str, k: int):
    rows = conn.execute(_SQL, (match, k)).fetchall()
    return _rows_to_dicts(rows)


def _fts_law(conn, match: str, k: int, law_id: int):
    rows = conn.execute(
        "SELECT a.article_id, a.article_no, a.chapter, a.section, a.content, "
        "l.law_name, l.status, bm25(articles_fts) AS bm "
        "FROM articles_fts JOIN articles a ON a.article_id = articles_fts.rowid "
        "JOIN laws l ON l.law_id = a.law_id "
        "WHERE articles_fts MATCH ? AND l.law_id = ? ORDER BY bm LIMIT ?",
        (match, law_id, k),
    ).fetchall()
    return _rows_to_dicts(rows)


def _law_hint(conn, q: str):
    """识别问题里出现的法名，返回 (law_id, 命中的名字)。"""
    best = None
    for r in conn.execute("SELECT law_id, law_name FROM laws").fetchall():
        name = r["law_name"]
        short = name.replace("中华人民共和国", "")
        for cand in (name, short):
            if cand and len(cand) >= 2 and cand in q:
                if best is None or len(cand) > len(best[1]):
                    best = (r["law_id"], cand)
    return best


def _search_with_law(conn, q, k, law_id, cand):
    qt = normalize_query(q).replace(cand, " ")
    toks = tokens(qt) or tokens(normalize_query(q))
    if not toks:
        return []
    res = _fts_law(conn, _match(toks), k, law_id)
    if len(res) < max(3, k // 4):
        match = _match(dict.fromkeys(toks), " OR ")
        seen = {r["article_id"] for r in res}
        for r in _fts_law(conn, match, k, law_id):
            if r["article_id"] not in seen:
                res.append(r)
                seen.add(r["article_id"])
    return res


def _fts_pipeline(conn, q: str, k: int):
    """纯 FTS 路（法名过滤 + 同义词扩展），不含 LIKE 兜底。"""
    toks = tokens(normalize_query(q))
    if not toks:
        return []
    res = []
    hint = _law_hint(conn, q)
    if hint is not None:
        res = _search_with_law(conn, q, k, hint[0], hint[1])
    if not res:
        res = _fts(conn, _match(toks), k)
        if len(res) < max(3, k // 4):
            extra = []
            for key, vals in load_synonyms().items():
                if key and key in q:
                    extra.extend(vals)
            more_toks = tokens(" ".join(extra)) if extra else []
            match = _match(dict.fromkeys(toks + more_toks), " OR ")
            seen = {r["article_id"] for r in res}
            for r in _fts(conn, match, k):
                if r["article_id"] not in seen:
                    res.append(r)
                    seen.add(r["article_id"])
    return res


def _like_fallback(conn, q: str, k: int):
    like = "%" + (q or "").strip() + "%"
    return _rows_to_dicts(conn.execute(_LIKE_SQL, (like, k)).fetchall())


# --------------------------------------------------------------------------
# RRF 融合
# --------------------------------------------------------------------------
def rrf_fuse(rankings, rrf_k: int = RRF_K):
    """倒数排序融合。

    rankings: 若干路结果列表，每项至少含 article_id，列表顺序即排名（从 1 起）。
    返回按融合分降序的 [{"article_id", "rrf", "sources", "docs"}]；
    同分按 article_id 排序，保证结果确定。
    """
    agg = {}
    for ranking in rankings or []:
        for rank, item in enumerate(ranking or [], 1):
            if not isinstance(item, dict):
                continue
            aid = item.get("article_id")
            if aid is None:
                continue
            slot = agg.setdefault(
                aid, {"article_id": aid, "rrf": 0.0, "sources": [], "docs": []}
            )
            slot["rrf"] += 1.0 / (rrf_k + rank)
            src = item.get("source")
            if src and src not in slot["sources"]:
                slot["sources"].append(src)
            slot["docs"].append(item)
    out = sorted(agg.values(), key=lambda s: (-s["rrf"], str(s["article_id"])))
    for slot in out:
        slot["rrf"] = round(slot["rrf"], 8)
    return out


def _same_db(conn, cfg) -> bool:
    """连接指向的库是否就是索引对应的库。

    向量索引的 ids 只对应 configs 里那个 articles.db；拿临时库/内存库去融合
    会掺进无关条文（单测经常这么干），所以对不上就直接跳过向量路。
    """
    rel = str((cfg.get("paths") or {}).get("db") or "data/db/articles.db")
    target = Path(rel)
    target = target if target.is_absolute() else (ROOT / target)
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        current = row[2] if row else ""
    except Exception:
        return True
    if not current:
        return False
    try:
        return Path(current).resolve() == target.resolve()
    except OSError:
        return False


def _vector_rows(q: str, k: int, conn, cfg):
    """向量路结果；任何异常都降级为空列表（退回纯 FTS）。"""
    if vector is None:
        return []
    emb = (cfg or {}).get("embedding") or {}
    if not emb.get("enabled", True):
        return []
    if conn is not None and not _same_db(conn, cfg):
        return []
    depth = max(int(emb.get("top_k") or 50), k)
    try:
        return vector.vector_search(q, k=depth, conn=conn, cfg=cfg)
    except Exception:
        return []


def _score_for(docs):
    """保留 FTS 量纲给 router：取 FTS 分与（余弦×系数）中的较大者。"""
    fts_scores = [float(d.get("score") or 0.0) for d in docs if d.get("source") == "fts"]
    vec_scores = [float(d.get("score") or 0.0) for d in docs if d.get("source") == "vector"]
    return round(max(fts_scores + [s * VEC_SCORE_SCALE for s in vec_scores] + [0.0]), 4)


def _fill_missing(conn, docs, ids):
    """用库里的完整记录补齐融合结果需要的字段。"""
    known = {}
    for d in docs:
        if d.get("law_name") and d.get("content"):
            known[d["article_id"]] = d
    missing = [i for i in ids if i not in known]
    if missing:
        sql = _BY_ID_SQL % ",".join("?" * len(missing))
        for r in conn.execute(sql, missing).fetchall():
            known[r["article_id"]] = dict(r)
    return known


def _rrf_weighted(fts_rows, vec_rows, rrf_k: int = 60, w_fts: float = 0.7, w_vec: float = 1.0):
    """加权 RRF：向量路权重更高（语义/大白话），FTS 保留精确串优势。"""
    slots = {}
    for rows, weight, tag in ((fts_rows, w_fts, "fts"), (vec_rows, w_vec, "vector")):
        for rank, item in enumerate(rows, 1):
            aid = item["article_id"]
            slot = slots.setdefault(aid, {"article_id": aid, "rrf": 0.0, "docs": [], "sources": [], "vec_rank": 9999})
            slot["rrf"] += weight / (rrf_k + rank)
            slot["docs"].append(item)
            slot["sources"].append(item.get("source") or tag)
            if tag == "vector":
                slot["vec_rank"] = min(slot["vec_rank"], rank)
    return sorted(slots.values(), key=lambda s: (-s["rrf"], s["vec_rank"], s["article_id"]))


def _fuse(conn, fts_rows, vec_rows, k: int):
    fused = _rrf_weighted(fts_rows, vec_rows)[:k]
    if not fused:
        return []
    ids = [f["article_id"] for f in fused]
    pool = _fill_missing(conn, fts_rows, ids)
    out = []
    for slot in fused:
        base = pool.get(slot["article_id"])
        if base is None:
            continue
        d = {
            "article_id": slot["article_id"],
            "article_no": base.get("article_no"),
            "chapter": base.get("chapter"),
            "section": base.get("section"),
            "content": base.get("content"),
            "law_name": base.get("law_name"),
            "status": base.get("status"),
            "score": _score_for(slot["docs"]),
            "rrf": slot["rrf"],
        }
        d["source"] = ("hybrid" if len(slot["sources"]) > 1
                       else (slot["sources"][0] if slot["sources"] else "fts"))
        out.append(d)
    return out


def _prefer_current(rows, k: int):
    """同一法条有多个历史版本时保留现行有效版本；不改变原有排序。"""
    seen, order = {}, []
    for r in rows:
        key = (r.get("law_name"), r.get("article_no"))
        if key not in seen:
            seen[key] = r
            order.append(key)
        elif (r.get("status") or "") == "现行有效" and (seen[key].get("status") or "") != "现行有效":
            seen[key] = r
    return [seen[key] for key in order][:k]


def search(q: str, k: int = 12, conn=None, cfg=None):
    """检索入口：FTS + 向量 RRF 融合；索引缺失时等价于纯 FTS。"""
    own = conn is None
    conn = conn or connect()
    try:
        toks = tokens(normalize_query(q))
        if not toks:
            return []
        cfg = cfg if cfg is not None else load_config()
        try:
            fts_rows = _fts_pipeline(conn, q, k)
        except sqlite3.OperationalError:
            # FTS5 语法兜底：任何怪字符（1.3、a.b、引号）都不该让整次检索失败
            fts_rows = []
        vec_rows = _vector_rows(q, k, conn, cfg)
        if vec_rows:
            fused = _fuse(conn, fts_rows, vec_rows, k)
            if fused:
                return _prefer_current(fused, k)
        if fts_rows:
            return _prefer_current(fts_rows, k)
        return _prefer_current(_like_fallback(conn, q, k), k)
    finally:
        if own:
            conn.close()
