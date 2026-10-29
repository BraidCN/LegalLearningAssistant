"""纯规则路由：问题意图分类、检索置信度评估、本地/云端/联网决策。

设计约束：
- 只依赖标准库，不发网络请求，不加载模型，不读写文件；
- 所有决策可解释：decide() 的 reasons 逐条写明命中的规则；
- 规则顺序 ① force 覆盖 → ② hot → ③ 长问题 → ④ 置信不足 → ⑤ 默认本地。

routing.force 语义：
- "local"：强制本地模型且不联网（完全离线，覆盖其余规则）；
- "cloud"：强制云端模型（云端不可用时回落本地），是否联网仍按其余规则判定；
- "web"  ：强制联网（即使 web.enabled=false），模型优先云端。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

HOT = "hot"
NORMATIVE = "normative"
CONCEPT = "concept"
CASE = "case"
CHAT = "chat"

INTENTS = (HOT, NORMATIVE, CONCEPT, CASE, CHAT)

DEFAULT_MIN_SCORE = 20.0
DEFAULT_LONG_QUERY_CHARS = 120

# 关键词表：子串命中即算该意图；同一意图内按优先级顺序检查
_KEYWORDS = {
    HOT: (
        "最新", "最近", "近日", "今天", "昨天", "今年", "热点", "热搜", "新闻",
        "通报", "事件", "进展", "判决结果", "宣判", "案发", "爆出", "舆论",
        "网友", "争议", "刚刚", "本月",
    ),
    NORMATIVE: (
        "法条", "是否合法", "合法吗", "违法吗", "怎么处罚", "判几年", "承担责任",
        "赔偿", "能不能", "能否", "可以吗", "有效期",
        "怎么办", "怎么处理", "该怎么", "如何维权", "如何申请", "如何起诉",
        "算不算", "是否构成",
    ),
    CONCEPT: (
        "是什么", "定义", "含义", "区别", "怎么理解", "概念",
    ),
    CASE: (
        "案情", "案例分析", "如何定性", "请求权", "鉴定式", "怎么判",
    ),
}

# “第X条” 形式（含阿拉伯数字与中文数字）视为法条型提问
_ARTICLE_RE = re.compile(r"第[0-9０-９一二三四五六七八九十百千零〇两]+条")

_LEXICON_FILE = Path(__file__).resolve().parents[2] / "configs" / "keywords.json"
_ASCII_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+#.\-]*$")


@lru_cache(maxsize=1)
def load_lexicon() -> dict:
    """读取 configs/keywords.json；缺失或损坏时回退到内置热点词。"""
    try:
        data = json.loads(_LEXICON_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"time_words": list(_KEYWORDS[HOT]), "time_patterns": [], "domains": {}}
    if not isinstance(data, dict):
        return {"time_words": list(_KEYWORDS[HOT]), "time_patterns": [], "domains": {}}
    return {
        "time_words": list(data.get("time_words") or _KEYWORDS[HOT]),
        "time_patterns": list(data.get("time_patterns") or []),
        "domains": data.get("domains") if isinstance(data.get("domains"), dict) else {},
    }


def _word_hit(text: str, word: str) -> bool:
    """中文按子串匹配；英文缩写按词边界匹配，避免 AI 命中 BRAID / said。"""
    if not word:
        return False
    if _ASCII_WORD.match(word):
        pattern = r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(word)
        return re.search(pattern, text, re.I) is not None
    return word in text


def domain_hits(text: str) -> list[dict]:
    """返回命中的领域：[{"key","label","hits":[...]}]，按词表顺序。"""
    text = text or ""
    out = []
    for key, item in (load_lexicon().get("domains") or {}).items():
        if not isinstance(item, dict):
            continue
        hits = [w for w in (item.get("words") or []) if _word_hit(text, w)]
        if hits:
            out.append({"key": key, "label": item.get("label") or key,
                        "hits": sorted(hits, key=len, reverse=True)})
    return out


def time_hits(text: str) -> list[str]:
    """返回命中的时效词/年份，如 最近、今年、2026 年。"""
    text = text or ""
    lex = load_lexicon()
    out = [w for w in (lex.get("time_words") or []) if w in text]
    for pat in lex.get("time_patterns") or []:
        try:
            out.extend(m.group(0).strip() for m in re.finditer(pat, text))
        except re.error:
            continue
    return sorted(dict.fromkeys(out), key=len, reverse=True)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hits(text: str, words) -> list[str]:
    return [w for w in words if w in text]


def _keyword_reason(text: str) -> str:
    """按 热点/法条/概念/案例 的优先级返回首个命中说明，供 reasons 使用。"""
    for intent in (HOT, NORMATIVE):
        hit = _hits(text, _KEYWORDS[intent])
        if hit:
            # 长词优先，便于人读
            hit = sorted(hit, key=len, reverse=True)
            return "命中%s关键词：%s" % (intent, "、".join(hit[:3]))
    m = _ARTICLE_RE.search(text)
    if m:
        return "命中法条编号：%s" % m.group(0)
    for intent in (CONCEPT, CASE):
        hit = _hits(text, _KEYWORDS[intent])
        if hit:
            hit = sorted(hit, key=len, reverse=True)
            return "命中%s关键词：%s" % (intent, "、".join(hit[:3]))
    return "无领域关键词"


def classify_intent(q: str) -> str:
    """把问题归入 hot / normative / concept / case / chat 之一。

    优先级 hot > normative > concept > case > chat，空问题归 chat。
    """
    text = (q or "").strip()
    if not text:
        return CHAT
    if _hits(text, _KEYWORDS[HOT]):
        return HOT
    if _hits(text, _KEYWORDS[NORMATIVE]) or _ARTICLE_RE.search(text):
        return NORMATIVE
    if _hits(text, _KEYWORDS[CONCEPT]):
        return CONCEPT
    if _hits(text, _KEYWORDS[CASE]):
        return CASE
    if domain_hits(text):
        return NORMATIVE
    return CHAT


def assess_confidence(results: list[dict], min_score: float) -> dict:
    """评估检索结果置信度。

    top_score 取结果中的最高分（无结果为 0.0），confident 要求有结果且
    top_score >= min_score。score 字段异常时按 0.0 计。
    """
    items = [r for r in (results or []) if isinstance(r, dict)]
    scores = [_as_float(r.get("score"), 0.0) for r in items]
    top_score = max(scores) if scores else 0.0
    threshold = _as_float(min_score, DEFAULT_MIN_SCORE)
    count = len(items)
    return {
        "top_score": top_score,
        "count": count,
        "confident": count > 0 and top_score >= threshold,
    }


def _dedupe(items) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def decide(q: str, results: list[dict], cfg: dict) -> dict:
    """综合意图与置信度给出路由：{"intent", "use_web", "backend", "reasons"}。

    cfg 结构（缺省按不可用处理）::

        {"routing": {"min_score": 20, "long_query_chars": 120, "force": ""},
         "web": {"enabled": true},
         "llm": {"cloud_enabled": true}}
    """
    cfg = cfg or {}
    routing = cfg.get("routing") or {}
    web_cfg = cfg.get("web") or {}
    llm_cfg = cfg.get("llm") or {}

    web_enabled = bool(web_cfg.get("enabled", False))
    cloud_enabled = bool(llm_cfg.get("cloud_enabled", False))
    min_score = _as_float(routing.get("min_score"), DEFAULT_MIN_SCORE)
    try:
        long_chars = int(routing.get("long_query_chars", DEFAULT_LONG_QUERY_CHARS))
    except (TypeError, ValueError):
        long_chars = DEFAULT_LONG_QUERY_CHARS
    force = str(routing.get("force") or "").strip().lower()
    if force not in ("web", "cloud", "local"):
        force = ""

    text = (q or "").strip()
    intent = classify_intent(text)
    conf = assess_confidence(results, min_score)
    domains_found = domain_hits(text)
    times_found = time_hits(text)
    domain_time = bool(domains_found and times_found)

    reasons: list[str] = []
    reasons.append("意图=%s（%s）" % (intent, _keyword_reason(text)))
    reasons.append(
        "检索 top_score=%.1f / 阈值 %.1f，命中 %d 条，%s"
        % (conf["top_score"], min_score, conf["count"],
           "置信充分" if conf["confident"] else "置信不足")
    )
    if domains_found:
        reasons.append("领域=%s（%s）" % (
            "/".join(d["label"] for d in domains_found[:2]),
            "、".join(domains_found[0]["hits"][:3])))
    if times_found:
        reasons.append("时效=%s" % "、".join(times_found[:3]))
    if force:
        reasons.append("① routing.force=%s 强制覆盖" % force)
    if force == "local":
        # 完全离线：不再评估联网/云端升级
        reasons.append("① force=local 强制本地模型且不联网")
        return {
            "intent": intent,
            "use_web": False,
            "backend": "local",
            "domains": [d["key"] for d in domains_found],
            "domain": domains_found[0]["key"] if domains_found else "",
            "domain_label": domains_found[0]["label"] if domains_found else "",
            "reasons": _dedupe(reasons),
        }

    use_web = False
    backend = "local"

    # ② 时效性：领域词 + 时效词同时命中才联网（避免“电商/AI”这类词一出现就联网）
    if domain_time and web_enabled:
        use_web = True
        reasons.append("② 领域=%s + 时效=%s → 联网检索时效信息"
                       % (domains_found[0]["label"], "、".join(times_found[:2])))
    elif intent == HOT:
        if web_enabled:
            reasons.append("② 热点但缺少领域词，不强制联网，交置信度判断")
        else:
            reasons.append("② 热点但 web.enabled=false，保持不联网")

    # ② 热点问题：联网 + 云端（保留原行为）
    if intent == HOT:
        if not use_web and (web_enabled or force == "web"):
            use_web = True
            reasons.append("② 热点问题 → 联网检索时效信息")
        elif not use_web:
            reasons.append("② 热点问题但 web.enabled=false，保持不联网")
        if cloud_enabled:
            backend = "cloud"
            reasons.append("② 热点问题 → 云端模型")
        else:
            reasons.append("② 热点问题但 llm.cloud_enabled=false，保持本地")

    # ③ 长问题：云端上下文更强
    if len(text) > long_chars:
        if cloud_enabled:
            backend = "cloud"
            reasons.append("③ 长问题（%d 字 > %d）→ 云端模型" % (len(text), long_chars))
        else:
            reasons.append("③ 长问题（%d 字 > %d）但云端不可用，保持本地"
                           % (len(text), long_chars))

    # ④ 置信不足：联网补充 + 云端；闲聊不检索，不做升级
    if intent == CHAT:
        reasons.append("④ 闲聊类问题无需检索，跳过置信度升级")
    elif not conf["confident"]:
        if web_enabled or force == "web":
            use_web = True
            reasons.append("④ 置信不足 → 联网补充检索")
        else:
            reasons.append("④ 置信不足但 web.enabled=false，仅用库内结果")
        if cloud_enabled:
            backend = "cloud"
            reasons.append("④ 置信不足 → 交云端模型兜底")
        else:
            reasons.append("④ 置信不足但云端不可用，保持本地")
    else:
        reasons.append("④ 置信充分，无需联网升级")

    # ⑤ 默认：本地、不联网（仅在本轮未升级时写明）
    if not use_web and backend == "local" and not force:
        reasons.append("⑤ 默认：本地模型、不联网")

    # ① force 最终覆盖
    if force == "web":
        use_web = True
        if not web_enabled:
            reasons.append("① force=web 覆盖 web.enabled=false，强制联网")
        if cloud_enabled:
            backend = "cloud"
            reasons.append("① force=web → 云端模型")
        else:
            backend = "local"
            reasons.append("① force=web 但云端不可用，模型回落本地")
    elif force == "cloud":
        if cloud_enabled:
            backend = "cloud"
            reasons.append("① force=cloud 强制云端模型")
        else:
            backend = "local"
            reasons.append("① force=cloud 但 llm.cloud_enabled=false，回落本地")

    return {
        "intent": intent,
        "use_web": use_web,
        "backend": backend,
        "domains": [d["key"] for d in domains_found],
        "domain": domains_found[0]["key"] if domains_found else "",
        "domain_label": domains_found[0]["label"] if domains_found else "",
        "reasons": _dedupe(reasons),
    }
