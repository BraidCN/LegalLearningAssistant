"""引用门禁：条号存在性 + 引文逐字核对。"""
import re
import unicodedata

CITE_RE = re.compile(
    r"《([^《》\n]{2,40})》(第[零〇一二三四五六七八九十百千两0-9]+条(?:之[零〇一二三四五六七八九十]+)?)"
)

_STRIP = set("，。、；：？！“”‘’（）()《》〈〉…—·,.!?;:\"'[]【】 \u3000\n\r\t")


def _norm_law(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "")
    s = s.replace("《", "").replace("》", "")
    s = re.sub(r"\s+", "", s)
    if s.startswith("中华人民共和国"):
        s = s[len("中华人民共和国"):]
    return s


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(ch for ch in s if ch not in _STRIP)


def _grams(s: str, n: int):
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _sentence_bounds(text: str, pos: int):
    left = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in "。！？\n；":
            left = i + 1
            break
    right = len(text)
    for j in range(pos, len(text)):
        if text[j] in "。！？\n；":
            right = j
            break
    return left, right


def verify(answer: str, retrieved, conn=None, min_quote_len: int = 10,
           require_citation: bool = True) -> dict:
    by_key = {}
    contents = {}
    for r in retrieved:
        key = (_norm_law(r["law_name"]), r["article_no"])
        by_key[key] = r
        contents[key] = _norm_text(r["content"])

    citations = []
    errors = []
    answer = answer or ""
    for m in CITE_RE.finditer(answer):
        law = _norm_law(m.group(1))
        art = m.group(2)
        key = (law, art)
        hit = by_key.get(key)
        found_db = hit is not None
        if hit is None and conn is not None:
            row = conn.execute(
                "SELECT a.content FROM articles a JOIN laws l ON l.law_id = a.law_id "
                "WHERE l.law_name LIKE ? AND a.article_no = ? LIMIT 1",
                ("%" + law + "%", art),
            ).fetchone()
            if row is not None:
                found_db = True
                contents[key] = _norm_text(row["content"])
        left, right = _sentence_bounds(answer, m.start())
        sent = answer[left:right]
        sent_wo = CITE_RE.sub("", sent)
        quote_ok = False
        if found_db:
            body = contents.get(key, "")
            quote_ok = bool(_grams(_norm_text(sent_wo), min_quote_len) & _grams(body, min_quote_len))
            if not quote_ok:
                # 引文可能写在下一条/下一行：检查引用点之后 600 字内的“…”逐字匹配
                tail = answer[m.end(): m.end() + 600]
                for q in re.findall(r"“([^”]{10,})”", tail):
                    if _grams(_norm_text(q), min_quote_len) & _grams(body, min_quote_len):
                        quote_ok = True
                        break
        citations.append({
            "law": m.group(1),
            "article_no": art,
            "found_in_context": hit is not None,
            "found_in_db": found_db,
            "quote_ok": quote_ok,
            "sentence": sent.strip()[:160],
        })
        if not found_db:
            errors.append("《%s》%s 不在库内" % (m.group(1), art))
        elif not quote_ok:
            errors.append("《%s》%s 的引文无法逐字核对" % (m.group(1), art))
    if not citations and require_citation:
        errors.append("回答中没有引用任何条文")
    return {"pass": not errors, "citations": citations, "errors": errors}
