"""问答流水线：检索 -> 路由 -> 联网（可选）-> 生成 -> 门禁 -> 必要时重写。"""
import time
from datetime import date

from . import llm, router, websearch
from .config import load_config
from .db import connect
from .gate import verify
from .search import search

SYSTEM_PROMPT = """你是法学学习助手。回答必须满足：
1) 法律依据只能来自【库内条文（已核验）】，不得用网络内容代替法条；
2) 引用库内条文时必须写出《法名》第X条（例：《中华人民共和国民法典》第一千零八十七条），并用中文引号“”逐字引用原文；严禁用 [1]、[W1] 之类的编号代替法名条号；
3) 只有在引用网络信息时才使用 [Wn] 标注；没有网络资料时不得出现任何 [Wn]；
4) 每条结论后面都必须紧跟《法名》第X条，否则视为无效回答；
5) 依据不足时明确写明：未找到直接依据。
"""


def _context(results, max_n):
    blocks = []
    for i, r in enumerate(results[:max_n], 1):
        body = (r.get("content") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "……"
        blocks.append("[%d] 《%s》%s（%s）\n%s" % (i, r["law_name"], r["article_no"], r.get("status") or "未知", body))
    return "\n\n".join(blocks)


def _web_context(sources):
    blocks = []
    today = date.today().isoformat()
    for i, s in enumerate(sources, 1):
        body = (s.get("text") or s.get("snippet") or "").strip()
        blocks.append(
            "[W%d] %s（%s，抓取时间 %s）\n%s"
            % (i, s.get("title") or "无标题", s.get("url") or "", today, body)
        )
    return "\n\n".join(blocks)


def _add_reason(route, reason):
    reasons = route.setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _bigrams(text):
    s = "".join((text or "").split())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _relevant_hit(q, hit):
    """粗相关性：问题与标题/摘要共享至少 2 个二字片段（短查询放宽到 1）。"""
    qb = _bigrams(q)
    tb = _bigrams((hit.get("title") or "") + (hit.get("snippet") or ""))
    need = 2 if len(qb) >= 4 else 1
    return len(qb & tb) >= need


def _collect_web(q, cfg, route):
    """联网检索并抓取正文；任何异常都在此吞掉，不影响问答。"""
    web_cfg = cfg.get("web") or {}
    top_k = int(web_cfg.get("top_k", 5))
    timeout = int(web_cfg.get("timeout", 12))
    fetch_n = max(0, int(web_cfg.get("fetch_pages", 0)))
    max_chars = int(web_cfg.get("max_page_chars", 4000))
    failed = False
    try:
        hits = websearch.search_bing(q, top_k=top_k, timeout=timeout)
        hits = websearch.rank_results(hits)
    except Exception:
        hits = []
        failed = True
    hits = [h for h in hits if _relevant_hit(q, h)]
    if not hits:
        try:
            alt = websearch.search_ddg(q, top_k=top_k, timeout=timeout)
            alt = websearch.rank_results(alt)
            hits = [h for h in alt if _relevant_hit(q, h)]
            if hits:
                _add_reason(route, "Bing 结果不相关，已切 DuckDuckGo")
        except Exception:
            hits = []
    if not hits:
        _add_reason(route, "联网结果与问题不相关")
        return []

    sources = []
    for i, hit in enumerate(hits):
        url = (hit.get("url") or "").strip()
        item = {
            "title": (hit.get("title") or "").strip(),
            "url": url,
            "snippet": (hit.get("snippet") or "").strip(),
            "fetched": False,
            "text": "",
        }
        if i < fetch_n and url:
            try:
                text = websearch.fetch_page_text(url, timeout=timeout, max_chars=max_chars)
            except Exception:
                text = ""
                failed = True
            if text:
                item["text"] = text
                item["fetched"] = True
        sources.append(item)
    if failed:
        _add_reason(route, "联网失败/无结果")
    return sources


def _model_name(cfg, backend):
    llm_cfg = cfg.get("llm") or {}
    if backend == "cloud":
        return (llm_cfg.get("cloud") or {}).get("model", "")
    return llm_cfg.get("model", "")


def _generate(messages, cfg, preferred, reasons=None):
    """按 preferred -> local -> cloud 顺序尝试，返回 (answer, backend, model)。

    reasons 传入时记录回退原因（前端/日志可见，避免“标签写本地、实际走云端”）。
    """
    llm_cfg = cfg.get("llm") or {}
    seen = set()
    tried = []
    for backend in (preferred, "local", "cloud"):
        if backend not in ("local", "cloud") or backend in seen:
            continue
        if backend == "cloud" and backend != preferred and not llm_cfg.get("cloud_enabled", False):
            continue
        seen.add(backend)
        tried.append(backend)
        try:
            answer = llm.chat(messages, cfg, backend=backend)
        except Exception as exc:  # noqa: BLE001
            answer = None
            if reasons is not None:
                reasons.append("%s 生成异常：%s" % (backend, type(exc).__name__))
        if answer:
            if reasons is not None and backend != preferred:
                reasons.append("已从 %s 回退到 %s" % (preferred, backend))
            return answer, backend, _model_name(cfg, backend)
        if reasons is not None and backend == preferred:
            reasons.append("%s 生成无输出（空回答）" % backend)
    return None, "", ""


def _fallback_answer(results):
    lines = ["未调用生成模型，以下为检索到的条文原文："]
    for i, r in enumerate(results[:5], 1):
        body = (r.get("content") or "").strip().replace("\n", " ")
        if len(body) > 200:
            body = body[:200] + "……"
        lines.append("%d. 《%s》%s（%s）：%s" % (i, r["law_name"], r["article_no"], r.get("status") or "未知", body))
    return "\n".join(lines)


def ask(q: str, cfg=None, conn=None, top_k=None, prefer_backend: str = ""):
    cfg = cfg or load_config()
    own = conn is None
    conn = conn or connect(cfg.get("paths", {}).get("db"))
    t0 = time.time()
    try:
        k = int(top_k or cfg.get("retrieval", {}).get("top_k", 12))
        results = search(q, k=k, conn=conn)
        decision = router.decide(q, results, cfg)
        route = {
            "intent": decision.get("intent", "chat"),
            "use_web": bool(decision.get("use_web")),
            "backend": decision.get("backend", "local"),
            "domain": decision.get("domain", ""),
            "domain_label": decision.get("domain_label", ""),
            "domains": list(decision.get("domains") or []),
            "reasons": list(decision.get("reasons") or []),
        }

        if prefer_backend in ("local", "cloud"):
            route["backend"] = prefer_backend
            route["reasons"].append("并发排队过久，改走%s" % ("云端" if prefer_backend == "cloud" else "本地"))

        web_sources = []
        if route["use_web"] and (cfg.get("web") or {}).get("enabled", False):
            web_sources = _collect_web(q, cfg, route)

        # 无检索结果且未联网：保持原有“未找到直接依据”直出
        if not results and not web_sources:
            return {
                "query": q,
                "answer": "未找到直接依据（库内没有匹配条文的检索结果）。",
                "llm": {"used": False},
                "gate": {"pass": True, "citations": [], "errors": []},
                "retrieved": [],
                "route": route,
                "web_sources": [],
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        max_n = int(cfg.get("retrieval", {}).get("max_context", 8))
        lib_ctx = _context(results, max_n) if results else "（无匹配条文）"
        parts = ["【库内条文（已核验）】\n" + lib_ctx]
        if web_sources:
            parts.append("【网络信息（未核验，仅作线索）】\n" + _web_context(web_sources))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "问题：%s\n\n%s\n\n请给出简洁回答。" % (q, "\n\n".join(parts))},
        ]

        answer, used_backend, used_model = _generate(messages, cfg, route["backend"], route["reasons"])
        used = bool(answer)
        if not answer:
            answer = _fallback_answer(results)

        min_quote = int(cfg.get("gate", {}).get("min_quote_len", 10))
        require_citation = route["intent"] != "hot"
        gate = verify(answer, results, conn=conn, min_quote_len=min_quote, require_citation=require_citation)
        if (not gate["pass"]) and used and cfg.get("gate", {}).get("retry_once", True):
            repair = messages + [
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": "你的回答有以下问题：%s。请只使用【库内条文（已核验）】重写；"
                    "法律依据不得用网络内容代替，引用网络信息须用 [Wn] 标注；引用必须逐字。"
                    % "；".join(gate["errors"]),
                },
            ]
            answer2, backend2, model2 = _generate(repair, cfg, used_backend or route["backend"], route["reasons"])
            if answer2:
                gate2 = verify(answer2, results, conn=conn, min_quote_len=min_quote, require_citation=require_citation)
                if gate2["pass"] or len(gate2["errors"]) <= len(gate["errors"]):
                    answer, gate = answer2, gate2
                    used_backend, used_model = backend2, model2

        return {
            "query": q,
            "answer": answer,
            "llm": {
                "used": used,
                "backend": used_backend if used else "",
                "model": used_model if used else "",
            },
            "gate": gate,
            "route": route,
            "web_sources": [
                {
                    "title": s["title"],
                    "url": s["url"],
                    "snippet": s["snippet"],
                    "fetched": s["fetched"],
                }
                for s in web_sources
            ],
            "retrieved": [
                {
                    "law_name": r["law_name"],
                    "article_no": r["article_no"],
                    "status": r.get("status"),
                    "chapter": r.get("chapter"),
                    "score": round(float(r.get("score", 0)), 3),
                    "preview": (r.get("content") or "").strip().replace("\n", " ")[:160],
                }
                for r in results[:10]
            ],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    finally:
        if own:
            conn.close()
