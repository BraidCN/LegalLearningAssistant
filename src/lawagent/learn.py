"""学习技能后端：概念解释（三段式）、鉴定式案例分析（民法/刑法）、出题 / 批改 / 错题本。

约定：
- 所有生成内容都必须过 gate.verify（条号存在性 + 逐字引文核对）；
- 门禁不过时按问答流水线（pipeline.ask）的原逻辑重写一次，只有不更差才采纳；
- 库内无匹配条文时直接回“未找到直接依据”，不调用模型编造；
- 全部对外函数返回 dict，且都包含 answer / citations / gate 三个键。
"""
import json
import re
from datetime import datetime

from . import llm
from .config import load_config
from .db import connect
from .gate import CITE_RE, verify
from .search import search

MAX_CONTEXT = 8

SYSTEM_PROMPT = (
    "你是法学学习助手，只能依据【库内条文（已核验）】作答。\n"
    "1) 引用格式必须是《法名》第X条，并用中文引号“”逐字引用原文片段，不得改写、不得省略字词；\n"
    "2) 不得编造条号，不得用推测、学理或网络内容代替法条；\n"
    "3) 库内没有对应条文时，必须在相应位置写明：未找到直接依据。\n"
)

# ---------------------------------------------------------------- 基础工具


def _cfg(cfg):
    return cfg if isinstance(cfg, dict) and cfg else load_config()


def _conn(cfg, conn):
    if conn is not None:
        return conn, False
    return connect((cfg.get("paths") or {}).get("db")), True


def _gate_cfg(cfg):
    g = cfg.get("gate") or {}
    return int(g.get("min_quote_len", 10)), bool(g.get("retry_once", True))


_QUOTE_BOUNDS = "。；！？"


def _quote(text, limit=80):
    """取一段逐字引文：在标点处收尾，避免门禁按句切分后留下碎片。

    返回的始终是原文的子串（前缀），可以逐字核对。
    """
    s = re.sub(r"\s*\n\s*", "", (text or "").strip())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    idx = max(cut.rfind(ch) for ch in _QUOTE_BOUNDS)
    if idx + 1 >= 15:
        return cut[: idx + 1]
    # 最近的标点太靠前（引文会过短）：往后找下一个标点再收尾
    for j in range(limit, len(s)):
        if s[j] in _QUOTE_BOUNDS:
            return s[: j + 1]
    return s


def _cite(article):
    return "《%s》%s" % (article["law_name"], article["article_no"])


def _context(results, max_n=MAX_CONTEXT):
    blocks = []
    for i, r in enumerate(results[:max_n], 1):
        body = (r.get("content") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "……"
        blocks.append(
            "[%d] 《%s》%s（%s）\n%s"
            % (i, r["law_name"], r["article_no"], r.get("status") or "未知", body)
        )
    return "\n\n".join(blocks)


def _public_hits(results, n=10):
    """返回给前端的检索摘要（与 pipeline 保持同样的字段）。"""
    return [
        {
            "law_name": r["law_name"],
            "article_no": r["article_no"],
            "status": r.get("status"),
            "score": round(float(r.get("score") or 0), 3),
            "preview": (r.get("content") or "").strip().replace("\n", " ")[:160],
        }
        for r in results[:n]
    ]


def _generate(messages, cfg, preferred="local"):
    """按 preferred -> local -> cloud 尝试，返回 (text, backend)；全失败返回 ("", "")。"""
    llm_cfg = cfg.get("llm") or {}
    seen = set()
    for backend in (preferred, "local", "cloud"):
        if backend not in ("local", "cloud") or backend in seen:
            continue
        seen.add(backend)
        if backend == "cloud" and backend != preferred and not llm_cfg.get("cloud_enabled", False):
            continue
        try:
            answer = llm.chat(messages, cfg, backend=backend)
        except Exception:
            answer = None
        answer = (answer or "").strip()
        if answer:
            return answer, backend
    return "", ""


def _preferred_backend(cfg):
    """学习技能对引文格式要求高，云端指令遵循更稳：默认优先云端（可配置关闭）。"""
    learn_cfg = cfg.get("learn") or {}
    if (cfg.get("llm") or {}).get("cloud_enabled") and learn_cfg.get("prefer_cloud", True):
        return "cloud"
    b = ((cfg.get("llm") or {}).get("backend") or "").strip()
    return "cloud" if b in ("cloud", "openai") else "local"


def _repair_messages(answer, errors):
    """原流水线口径：把门禁报错回灌，要求只用库内条文重写。"""
    return [
        {"role": "assistant", "content": answer},
        {
            "role": "user",
            "content": (
                "你的回答有以下问题：%s。请只使用【库内条文（已核验）】重写；"
                "引用必须写成《法名》第X条并逐字引用原文；库内没有依据时写“未找到直接依据”。"
            )
            % "；".join(errors),
        },
    ]


def _repair_once(messages, answer, gate, cfg, results, conn, min_quote,
                 require_citation=True, preferred=None):
    """门禁不过 -> 重写一次，采纳条件与 pipeline 一致（通过，或错误不增加）。"""
    retry_ok = _cfg(cfg).get("gate", {}).get("retry_once", True)
    if not retry_ok or not answer or not gate.get("errors"):
        return answer, gate, ""
    text2, backend2 = _generate(
        messages + _repair_messages(answer, gate["errors"]),
        cfg,
        preferred or _preferred_backend(cfg),
    )
    if not text2:
        return answer, gate, ""
    gate2 = verify(text2, results, conn=conn, min_quote_len=min_quote,
                   require_citation=require_citation)
    if gate2["pass"] or len(gate2["errors"]) <= len(gate["errors"]):
        return text2, gate2, backend2
    return answer, gate, ""


def _empty_gate():
    return {"pass": True, "citations": [], "errors": []}


def _merge_gates(gates):
    citations, errors = [], []
    for g in gates:
        citations.extend(g.get("citations") or [])
        errors.extend(g.get("errors") or [])
    return {"pass": not errors, "citations": citations, "errors": errors}


def _no_basis(reason):
    return {
        "answer": "未找到直接依据（%s）。" % reason,
        "citations": [],
        "gate": _empty_gate(),
    }


# ---------------------------------------------------------------- 概念解释

EXPLAIN_TEMPLATE = (
    "静默按以下三段式作答，三个标题必须原样出现，不要添加其他标题：\n"
    "一、定义\n"
    "（2-4 句话说明该概念的准确含义、成立要件或法律效果；只作定义，不展开案例）\n"
    "二、条文依据\n"
    "（每条依据单独一行，格式：《法名》第X条：“逐字原文”。引文必须与上文条文完全一致；"
    "库内没有对应条文时写：未找到直接依据）\n"
    "三、例子\n"
    "（一个具体、简短的事例，说明该概念如何适用，并指明落到哪一条）"
)

_SECTION_RE = re.compile(
    r"(?:^|\n)[^\S\n]*(?:[一二三][、.．]|[①②③]|[（(][一二三][)）])[^\S\n]*"
    r"(定义|条文依据|法条依据|法律依据|依据|例子|示例|举例)"
)
_SECTION_KEY = {"定义": "definition", "例子": "example", "示例": "example", "举例": "example"}


def _split_explain(text):
    """把三段式文本切成 dict；找不到的段留空。"""
    out = {"definition": "", "basis": "", "example": ""}
    spans = []
    for m in _SECTION_RE.finditer(text or ""):
        key = _SECTION_KEY.get(m.group(1))
        if key is None:
            key = "basis"
        spans.append((m.start(), m.end(), key))
    for i, (_, end, key) in enumerate(spans):
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        body = (text[end:stop] or "").strip()
        if body and not out[key]:
            out[key] = body
    return out


def _explain_fallback(term, results):
    lines = [
        "一、定义",
        "%s：未调用生成模型，以下不作概括，仅列示库内条文原文。" % term,
        "",
        "二、条文依据",
    ]
    for r in results[:3]:
        lines.append("%s：“%s”" % (_cite(r), _quote(r.get("content"), 80)))
    lines += ["", "三、例子", "（未调用生成模型，未生成示例。）"]
    return "\n".join(lines)


def explain(term, cfg=None, conn=None):
    """概念解释：①定义 ②条文依据（条号 + 逐字引文）③一个例子。"""
    term = (term or "").strip()
    cfg = _cfg(cfg)
    conn, own = _conn(cfg, conn)
    min_quote, _ = _gate_cfg(cfg)
    try:
        if not term:
            return dict(
                term="",
                sections={"definition": "", "basis": "", "example": ""},
                retrieved=[],
                llm={"used": False, "backend": "", "model": ""},
                **_no_basis("未提供要解释的概念"),
            )

        k = int((cfg.get("retrieval") or {}).get("top_k", 12))
        results = search(term, k=k, conn=conn)
        if not results:
            return dict(
                term=term,
                sections={"definition": "", "basis": "", "example": ""},
                retrieved=[],
                llm={"used": False, "backend": "", "model": ""},
                **_no_basis("库内没有与“%s”匹配的条文" % term),
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "概念：%s\n\n【库内条文（已核验）】\n%s\n\n%s"
                % (term, _context(results), EXPLAIN_TEMPLATE),
            },
        ]
        answer, backend = _generate(messages, cfg, _preferred_backend(cfg))
        used = bool(answer)
        if not answer:
            answer = _explain_fallback(term, results)

        gate = verify(answer, results, conn=conn, min_quote_len=min_quote,
                      require_citation=True)
        if used and not gate["pass"]:
            answer, gate, backend = _repair_once(
                messages, answer, gate, cfg, results, conn, min_quote,
                preferred=backend or _preferred_backend(cfg),
            )

        # 本地模型改写引文时，升级到云端再试一次（云端指令遵循更稳）
        if not gate["pass"] and (cfg.get("llm") or {}).get("cloud_enabled"):
            text2, backend2 = _generate(messages, cfg, "cloud")
            if text2:
                gate2 = verify(text2, results, conn=conn, min_quote_len=min_quote,
                               require_citation=True)
                if gate2["pass"] or len(gate2["errors"]) < len(gate["errors"]):
                    answer, gate, backend = text2, gate2, backend2

        # 仍不过：退成“条文直出”模板，保证引文逐字可核
        if not gate["pass"]:
            fb = _explain_fallback(term, results)
            gfb = verify(fb, results, conn=conn, min_quote_len=min_quote,
                         require_citation=True)
            # 模型答得完整但引文没过门禁时，保留内容并由 gate 标注问题，
            # 不用短模板把整段回答换掉（那看起来就像“答案被截断”）。
            if (not used) or len(answer) < 300:
                answer, gate = fb, gfb

        return {
            "term": term,
            "answer": answer,
            "sections": _split_explain(answer),
            "citations": gate["citations"],
            "gate": gate,
            "retrieved": _public_hits(results),
            "llm": {"used": used, "backend": backend if used else "", "model": ""},
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 鉴定式案例分析

CIVIL_METHOD = "请求权基础检视"
CRIMINAL_METHOD = "阶层检验（构成要件该当性→违法性→有责性）"

CIVIL_STEPS = [
    ("事实与争点整理", "列出当事人、时间、行为，归纳争议焦点与可能的请求内容"),
    ("寻找请求权基础", "依次检查：合同（含缔约过失）、无因管理、物权返还、不当得利、侵权"),
    ("构成要件检视", "对每个候选请求权逐项检验成立要件（意思表示、履行、损害、因果关系、违法性）"),
    ("抗辩与抗辩权", "检查清偿、抵销、免除、诉讼时效、同时履行抗辩、先诉抗辩等"),
    ("结论", "得出某项请求权成立 / 不成立，以及可以主张的内容"),
]

CRIMINAL_STEPS = [
    ("事实与罪名候选", "归纳行为事实，列出可能触犯的罪名"),
    ("构成要件该当性", "逐项检验行为、对象、结果、因果关系、主观故意或过失"),
    ("违法性", "检查正当防卫、紧急避险、被害人承诺、法令行为等违法阻却事由"),
    ("有责性", "检查责任能力、违法性认识可能性、期待可能性"),
    ("结论", "罪名是否成立、既遂未遂、共同犯罪与量刑情节"),
]

CIVIL_KEYWORDS = ("合同", "侵权", "物权", "婚姻", "继承", "请求权")
CRIMINAL_KEYWORDS = ("罪", "刑", "故意", "过失", "防卫")


def detect_discipline(facts):
    """按关键词计数判断：民法 / 刑法。返回 (discipline, 命中关键词)。"""
    text = facts or ""
    civil = {kw: text.count(kw) for kw in CIVIL_KEYWORDS if kw in text}
    criminal = {kw: text.count(kw) for kw in CRIMINAL_KEYWORDS if kw in text}
    cs, xs = sum(civil.values()), sum(criminal.values())
    if xs > cs:
        disc = "criminal"
    elif cs > xs:
        disc = "civil"
    elif "罪" in criminal:
        disc = "criminal"
    else:
        disc = "civil"
    return disc, {"civil": civil, "criminal": criminal}


def _case_brief(disc):
    if disc == "criminal":
        return CRIMINAL_METHOD, CRIMINAL_STEPS
    return CIVIL_METHOD, CIVIL_STEPS


_STEP_RE = re.compile(r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*步\s*[:：.、]?\s*(.*)")


def _split_steps(text, template):
    """按“第N步”切段；切不出来就按模板顺序整体归入第一步之外的空段。"""
    marks = list(_STEP_RE.finditer(text or ""))
    steps = []
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = (text[m.end():stop] or "").strip()
        title = (m.group(2) or "").strip()
        if not title and i < len(template):
            title = template[i][0]
        steps.append({"step": i + 1, "title": title or "第%d步" % (i + 1), "text": body})
    if not steps and text:
        steps = [{"step": 1, "title": template[0][0] if template else "分析", "text": text.strip()}]
    return steps


def _case_prompt(facts, results, method, steps):
    plan = "\n".join(
        "%d) %s：%s" % (i, t, h) for i, (t, h) in enumerate(steps, 1)
    )
    return (
        "案件事实：\n%s\n\n【库内条文（已核验）】\n%s\n\n"
        "请用【%s】方法分步分析，严格按下列步骤，每步以“第N步：标题”开头：\n%s\n\n"
        "要求：每一步都要标出依据条文，格式《法名》第X条：“逐字原文”；"
        "该步库内没有对应条文时写“未找到直接依据”，不得编造条号。"
        % (facts, _context(results), method, plan)
    )


def _case_fallback(facts, results, method, steps, disc):
    lines = ["【%s】" % method]
    for i, (title, hint) in enumerate(steps, 1):
        lines.append("第%d步：%s" % (i, title))
        if i == 1:
            lines.append("待分析事实：%s" % (facts[:300] or "（未提供）"))
        elif results:
            r = results[(i - 1) % len(results)]
            lines.append("检验要点：%s。" % hint)
            lines.append("依据：%s：“%s”" % (_cite(r), _quote(r.get("content"), 60)))
        else:
            lines.append("检验要点：%s。未找到直接依据。" % hint)
        lines.append("")
    lines.append("（未调用生成模型，以上为按%s模板列出的检验框架。）"
                 % ("阶层" if disc == "criminal" else "请求权"))
    return "\n".join(lines)


def analyze_case(facts, cfg=None, conn=None):
    """鉴定式案例分析：民法走请求权基础检视，刑法走阶层检验，每步标依据条文。"""
    facts = (facts or "").strip()
    cfg = _cfg(cfg)
    conn, own = _conn(cfg, conn)
    min_quote, _ = _gate_cfg(cfg)
    try:
        disc, keywords = detect_discipline(facts)
        method, steps = _case_brief(disc)
        base = {
            "facts": facts,
            "discipline": disc,
            "discipline_label": "刑法" if disc == "criminal" else "民法",
            "method": method,
            "keywords": keywords,
        }
        if not facts:
            return dict(
                steps=[],
                retrieved=[],
                llm={"used": False, "backend": ""},
                **base,
                **_no_basis("未提供案件事实"),
            )

        k = int((cfg.get("retrieval") or {}).get("top_k", 12))
        results = search(facts, k=k, conn=conn)
        if not results:
            return dict(
                steps=[
                    {"step": i, "title": t, "text": "未找到直接依据。"}
                    for i, (t, _) in enumerate(steps, 1)
                ],
                retrieved=[],
                llm={"used": False, "backend": ""},
                **base,
                **_no_basis("库内没有与案件事实匹配的条文"),
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _case_prompt(facts, results, method, steps)},
        ]
        answer, backend = _generate(messages, cfg, _preferred_backend(cfg))
        used = bool(answer)
        if not answer:
            answer = _case_fallback(facts, results, method, steps, disc)

        gate = verify(answer, results, conn=conn, min_quote_len=min_quote,
                      require_citation=True)
        if used and not gate["pass"]:
            answer, gate, backend = _repair_once(
                messages, answer, gate, cfg, results, conn, min_quote,
                preferred=backend or _preferred_backend(cfg),
            )

        if not gate["pass"] and (cfg.get("llm") or {}).get("cloud_enabled"):
            text2, backend2 = _generate(messages, cfg, "cloud")
            if text2:
                gate2 = verify(text2, results, conn=conn, min_quote_len=min_quote,
                               require_citation=True)
                if gate2["pass"] or len(gate2["errors"]) < len(gate["errors"]):
                    answer, gate, backend = text2, gate2, backend2

        if not gate["pass"]:
            fb = _case_fallback(facts, results, method, steps, disc)
            gfb = verify(fb, results, conn=conn, min_quote_len=min_quote,
                         require_citation=True)
            # 模型答得完整但引文没过门禁时，保留内容并由 gate 标注问题，
            # 不用短模板把整段回答换掉（那看起来就像“答案被截断”）。
            if (not used) or len(answer) < 300:
                answer, gate = fb, gfb

        return dict(
            base,
            answer=answer,
            steps=_split_steps(answer, steps),
            citations=gate["citations"],
            gate=gate,
            retrieved=_public_hits(results),
            llm={"used": used, "backend": backend if used else "", "model": ""},
        )
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 出题

QUIZ_SCHEMA = """CREATE TABLE IF NOT EXISTS quiz_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'owner',
  topic TEXT NOT NULL,
  question TEXT NOT NULL,
  options_json TEXT NOT NULL,
  answer TEXT NOT NULL,
  article_ref TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wrong_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'owner',
  item_id INTEGER NOT NULL,
  user_answer TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wrong_item ON wrong_answers(item_id);
"""


def _ensure_quiz_schema(conn):
    """建表 + 旧库补 user_id（旧行归 owner）。"""
    conn.executescript(QUIZ_SCHEMA)
    for table in ("quiz_items", "wrong_answers"):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if "user_id" not in cols:
            conn.execute(
                "ALTER TABLE %s ADD COLUMN user_id TEXT NOT NULL DEFAULT 'owner'" % table
            )
            conn.execute("UPDATE %s SET user_id='owner'" % table)
    conn.commit()

QUIZ_TEMPLATE = (
    "出题主题：%s\n\n【库内条文（已核验）】\n%s\n\n"
    "请只依据上面的条文出 %d 道单选题。只输出 JSON 数组，不要任何说明文字和代码块标记：\n"
    '[{"question":"题干",'
    '"options":["选项A文字","选项B文字","选项C文字","选项D文字"],'
    '"answer":"A",'
    '"article_ref":"《法名》第X条",'
    '"quote":"该条逐字原文片段（与库内条文完全一致）",'
    '"explanation":"一句话解析"}]\n'
    "要求：每题 4 个选项且只有一个正确；answer 只能是 A/B/C/D；"
    "quote 必须逐字摘自对应条文，不得改写；article_ref 必须是库内条文的条号。"
)

_OPTION_PREFIX = re.compile(r"^\s*[ABCD][\s.、:：)）]\s*")
_ANSWER_PREFIX = re.compile(r"^\s*([ABCD])(?:\s*$|[\s.、:：)）])")


def _extract_json(text):
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    tries = [s]
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        a, b = s.find(open_ch), s.rfind(close_ch)
        if a >= 0 and b > a:
            tries.append(s[a:b + 1])
    for cand in tries:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _as_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "questions", "quiz", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _norm_answer(answer, texts):
    """答案归一成 0-3 的下标；无法识别返回 None。"""
    if answer is None:
        return None
    if isinstance(answer, int) and 0 <= answer <= 3:
        return answer
    s = str(answer).strip()
    if not s:
        return None
    m = _ANSWER_PREFIX.match(s)
    if m:
        return "ABCD".index(m.group(1))
    if len(s) == 1 and s.upper() in "ABCD":
        return "ABCD".index(s.upper())
    for i, t in enumerate(texts):
        if t and (t == s or t in s or s in t):
            return i
    return None


def _norm_options(raw):
    if isinstance(raw, dict):
        raw = [raw[k] for k in sorted(raw)]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for o in raw:
        if isinstance(o, dict):
            o = o.get("text") or o.get("content") or o.get("label") or ""
        o = _OPTION_PREFIX.sub("", str(o)).strip()
        if o:
            out.append(o)
    return out


def _quiz_item(raw, idx, results, conn, min_quote):
    """单题校验；合格返回 (item, "")，否则 (None, 原因)。"""
    if not isinstance(raw, dict):
        return None, "第%d题：不是对象" % idx
    question = str(raw.get("question") or raw.get("题干") or "").strip()
    texts = _norm_options(raw.get("options") or raw.get("选项"))
    ans = _norm_answer(raw.get("answer") or raw.get("答案"), texts)
    ref_raw = str(raw.get("article_ref") or raw.get("依据条文") or "").strip()
    quote = str(raw.get("quote") or raw.get("引文") or "").strip()
    explanation = str(raw.get("explanation") or raw.get("解析") or "").strip()

    errs = []
    if not question:
        errs.append("题干为空")
    if len(texts) != 4:
        errs.append("选项数不是 4")
    if ans is None:
        errs.append("答案无法识别为 A/B/C/D")
    m = CITE_RE.search(ref_raw)
    if not m:
        errs.append("依据条文格式应为《法名》第X条")
    if len(quote) < min_quote:
        errs.append("引文过短，无法逐字核对")
    if errs:
        return None, "第%d题：%s" % (idx, "；".join(errs))

    ref = "《%s》%s" % (m.group(1), m.group(2))
    gate = verify("%s：“%s”" % (ref, quote), results, conn=conn,
                  min_quote_len=min_quote, require_citation=True)
    if not gate["pass"]:
        return None, "第%d题：%s" % (idx, "；".join(gate["errors"]))
    c = gate["citations"][0] if gate["citations"] else {}

    return {
        "question": question,
        "options": [{"label": "ABCD"[i], "text": t} for i, t in enumerate(texts)],
        "answer": "ABCD"[ans],
        "article_ref": ref,
        "quote": quote,
        "explanation": explanation,
        "in_context": bool(c.get("found_in_context")),
        "gate": gate,
    }, ""


def _extra_articles(conn, exclude, limit=12):
    """为兜底出题补干扰项：从库里另取条文（已排除检索结果）。"""
    rows = conn.execute(
        "SELECT a.article_no, a.content, l.law_name, l.status FROM articles a "
        "JOIN laws l ON l.law_id = a.law_id ORDER BY a.article_id LIMIT 200"
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if (d["law_name"], d["article_no"]) in exclude:
            continue
        if len((d.get("content") or "").strip()) < 20:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def _fallback_quiz(topic, results, n, min_quote, conn=None):
    """无模型兜底：用库内条文原文出“该条主要内容”型单题，答案位置轮转。"""
    pool, seen = [], set()
    for r in results:
        key = (r["law_name"], r["article_no"])
        if key in seen or len((r.get("content") or "").strip()) < min_quote:
            continue
        seen.add(key)
        pool.append(r)
    source = [dict(r) for r in pool]
    if conn is not None:
        source += _extra_articles(conn, seen)

    items = []
    for i, r in enumerate(pool):
        if len(items) >= n:
            break
        key = (r["law_name"], r["article_no"])
        quote = _quote(r.get("content"), 60)
        texts = [quote]
        for o in source:
            if len(texts) >= 4:
                break
            if (o["law_name"], o["article_no"]) == key:
                continue
            t = _quote(o.get("content"), 60)
            if t and t not in texts:
                texts.append(t)
        if len(texts) < 4:
            continue
        pos = i % 4
        texts.insert(pos, texts.pop(0))
        ref = _cite(r)
        gate = verify("%s：“%s”" % (ref, quote), results, conn=conn,
                      min_quote_len=min_quote, require_citation=True)
        items.append({
            "question": "《%s》%s规定的主要内容是？" % (r["law_name"], r["article_no"]),
            "options": [{"label": "ABCD"[j], "text": t} for j, t in enumerate(texts)],
            "answer": "ABCD"[pos],
            "article_ref": ref,
            "quote": quote,
            "explanation": "本题直接考查该条原文，正确选项为该条原文表述。",
            "in_context": True,
            "gate": gate,
        })
    return items


def _store_quiz(conn, topic, items, now, user_id="owner"):
    for it in items:
        cur = conn.execute(
            "INSERT INTO quiz_items(user_id, topic, question, options_json, answer, article_ref,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, topic, it["question"], json.dumps(it["options"], ensure_ascii=False),
             it["answer"], it["article_ref"], now),
        )
        it["id"] = cur.lastrowid
    conn.commit()


def quiz_create(topic, n=5, cfg=None, conn=None, user_id="owner"):
    """按主题从库内条文出 n 道单选题，写入 quiz_items（归属 user_id）。"""
    topic = (topic or "").strip()
    n = max(1, min(int(n or 5), 20))
    cfg = _cfg(cfg)
    conn, own = _conn(cfg, conn)
    min_quote, _ = _gate_cfg(cfg)
    try:
        _ensure_quiz_schema(conn)
        base = {"topic": topic, "requested": n, "llm": {"used": False, "backend": ""}}
        if not topic:
            return dict(base, items=[], count=0, retrieved=[],
                        **_no_basis("未提供出题主题"))

        k = int((cfg.get("retrieval") or {}).get("top_k", 12))
        results = search(topic, k=max(k, n * 3), conn=conn)
        if not results:
            return dict(base, items=[], count=0, retrieved=[],
                        **_no_basis("库内没有与“%s”匹配的条文，无法出题" % topic))

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": QUIZ_TEMPLATE % (topic, _context(results), n)},
        ]
        raw, backend = _generate(messages, cfg, _preferred_backend(cfg))
        used = bool(raw)
        items, errs = [], []
        all_errs = []
        if used:
            items, errs = _parse_quiz(raw, results, conn, min_quote)
            all_errs.extend(errs)

        retry_ok = cfg.get("gate", {}).get("retry_once", True)
        if used and (errs or len(items) < n) and retry_ok:
            fix = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    "上面的题目有以下问题：%s。请重新生成完整的 JSON 数组，"
                    "quote 必须逐字摘自库内条文，article_ref 必须能在库内找到。"
                    % ("；".join(errs[:6]) or "题目数量不足")},
            ]
            raw2, backend2 = _generate(fix, cfg, backend or _preferred_backend(cfg))
            if raw2:
                items2, errs2 = _parse_quiz(raw2, results, conn, min_quote)
                all_errs.extend(errs2)
                if len(items2) > len(items) or (len(items2) == len(items) and len(errs2) < len(errs)):
                    items, errs, raw, backend = items2, errs2, raw2, backend2

        if not items:
            items = _fallback_quiz(topic, results, n, min_quote, conn)
            used = False if items else used

        _store_quiz(conn, topic, items, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id)
        gate = _merge_gates([it["gate"] for it in items]) if items else _empty_gate()

        return {
            "topic": topic,
            "requested": n,
            "count": len(items),
            "items": items,
            "answer": "已生成 %d 道关于“%s”的单选题。" % (len(items), topic),
            "citations": gate["citations"],
            "gate": gate,
            "rejected": list(dict.fromkeys(all_errs)),
            "retrieved": _public_hits(results),
            "llm": {"used": used, "backend": backend if used else ""},
        }
    finally:
        if own:
            conn.close()


def _parse_quiz(raw, results, conn, min_quote):
    data = _extract_json(raw)
    if data is None:
        return [], ["返回内容不是合法 JSON"]
    items, errs = [], []
    for i, obj in enumerate(_as_items(data), 1):
        item, why = _quiz_item(obj, i, results, conn, min_quote)
        if item:
            items.append(item)
        else:
            errs.append(why)
    if not items and not errs:
        errs.append("返回内容为空")
    return items, errs


# ---------------------------------------------------------------- 批改 / 错题本


def _article_by_ref(conn, ref):
    m = CITE_RE.search(ref or "")
    if not m:
        return None
    row = conn.execute(
        "SELECT a.article_no, a.content, l.law_name, l.status FROM articles a "
        "JOIN laws l ON l.law_id = a.law_id WHERE l.law_name LIKE ? AND a.article_no = ? LIMIT 1",
        ("%" + m.group(1) + "%", m.group(2)),
    ).fetchone()
    return dict(row) if row is not None else None


def _grade_fallback(correct_label, article):
    if article:
        return "正确答案是 %s。依据%s：“%s”。" % (
            correct_label, _cite(article), _quote(article.get("content"), 60))
    return "正确答案是 %s。" % correct_label


def _grade_prompt(item, correct_label):
    opts = "；".join("%s. %s" % (o.get("label"), o.get("text")) for o in item["options"])
    return (
        "题目：%s\n选项：%s\n正确答案：%s\n依据条文：%s\n\n"
        "请用一句话讲解这道题为什么选 %s，并逐字引用依据条文原文（格式《法名》第X条：“原文”）。"
        "只输出这句话。"
        % (item["question"], opts, correct_label, item["article_ref"], correct_label)
    )


def quiz_grade(item_id, user_answer, cfg=None, conn=None, user_id="owner"):
    """判对错，给依据条文与一句话讲解；答错写入 wrong_answers（归属 user_id）。"""
    cfg = _cfg(cfg)
    conn, own = _conn(cfg, conn)
    min_quote, _ = _gate_cfg(cfg)
    try:
        _ensure_quiz_schema(conn)
        row = conn.execute(
            "SELECT * FROM quiz_items WHERE id = ? AND user_id = ?", (int(item_id), user_id)
        ).fetchone()
        if row is None:
            return dict(item_id=item_id, correct=False, user_answer=user_answer,
                        **_no_basis("题目 id=%s 不存在" % item_id), error="题目不存在")

        options = json.loads(row["options_json"])
        texts = [o.get("text", "") for o in options]
        labels = [o.get("label", "ABCD"[i]) for i, o in enumerate(options)]
        correct_idx = _norm_answer(row["answer"], texts)
        user_idx = _norm_answer(user_answer, texts)
        correct_label = labels[correct_idx] if correct_idx is not None else str(row["answer"])

        if user_idx is None:
            return {
                "item_id": int(item_id),
                "question": row["question"],
                "options": options,
                "correct": False,
                "correct_answer": correct_label,
                "user_answer": user_answer,
                "article_ref": row["article_ref"],
                "answer": "答案“%s”无法识别，请回答 A/B/C/D 或选项原文。" % (user_answer,),
                "citations": [],
                "gate": _empty_gate(),
                "error": "答案无法识别",
                "recorded": False,
            }

        correct = user_idx == correct_idx
        article = _article_by_ref(conn, row["article_ref"])
        retrieved = [article] if article else []

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _grade_prompt(
                {"question": row["question"], "options": options,
                 "article_ref": row["article_ref"]}, correct_label)},
        ]
        explanation, backend = _generate(messages, cfg, _preferred_backend(cfg))
        used = bool(explanation)
        gate = verify(explanation or "", retrieved, conn=conn,
                      min_quote_len=min_quote, require_citation=True) if used else _empty_gate()
        if used and not gate["pass"]:
            explanation, gate, backend = _repair_once(
                messages, explanation, gate, cfg, retrieved, conn, min_quote,
                preferred=backend or _preferred_backend(cfg),
            )
        if not used or not gate["pass"] or not explanation:
            explanation = _grade_fallback(correct_label, article)
            gate = verify(explanation, retrieved, conn=conn, min_quote_len=min_quote,
                          require_citation=True) if retrieved else _empty_gate()
            used = False

        recorded = False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not correct:
            conn.execute(
                "INSERT INTO wrong_answers(user_id, item_id, user_answer, created_at)"
                " VALUES (?,?,?,?)",
                (user_id, int(item_id), str(user_answer), now),
            )
            conn.commit()
            recorded = True

        return {
            "item_id": int(item_id),
            "topic": row["topic"],
            "question": row["question"],
            "options": options,
            "correct": correct,
            "correct_answer": correct_label,
            "correct_text": texts[correct_idx] if correct_idx is not None else "",
            "user_answer": user_answer,
            "user_label": labels[user_idx],
            "article_ref": row["article_ref"],
            "answer": explanation,
            "explanation": explanation,
            "citations": gate["citations"],
            "gate": gate,
            "recorded": recorded,
            "llm": {"used": used, "backend": backend if used else ""},
        }
    finally:
        if own:
            conn.close()


def wrongbook(conn=None, cfg=None, user_id="owner"):
    """错题本：题干 / 你的答案 / 正确答案 / 依据条文 / 时间（只列当前用户）。"""
    cfg = _cfg(cfg)
    conn, own = _conn(cfg, conn)
    try:
        _ensure_quiz_schema(conn)
        rows = conn.execute(
            "SELECT w.id AS wrong_id, w.item_id, w.user_answer, w.created_at, "
            "q.question, q.options_json, q.answer, q.article_ref, q.topic "
            "FROM wrong_answers w LEFT JOIN quiz_items q"
            " ON q.id = w.item_id AND q.user_id = w.user_id "
            "WHERE w.user_id = ? ORDER BY w.id DESC",
            (user_id,)
        ).fetchall()
        items = []
        for r in rows:
            try:
                options = json.loads(r["options_json"]) if r["options_json"] else []
            except Exception:
                options = []
            labels = [o.get("label", "ABCD"[i]) for i, o in enumerate(options)]
            texts = [o.get("text", "") for o in options]
            idx = _norm_answer(r["answer"], texts)
            label = labels[idx] if idx is not None else str(r["answer"])
            items.append({
                "wrong_id": r["wrong_id"],
                "item_id": r["item_id"],
                "topic": r["topic"],
                "question": r["question"],
                "options": options,
                "user_answer": r["user_answer"],
                "correct_answer": label,
                "correct_text": texts[idx] if idx is not None else "",
                "article_ref": r["article_ref"],
                "created_at": r["created_at"],
            })
        return {
            "count": len(items),
            "items": items,
            "answer": ("错题本共 %d 道。" % len(items)) if items else "错题本为空。",
            "citations": [],
            "gate": _empty_gate(),
            "note": "错题本为本地练习记录，不构成法律意见。",
        }
    finally:
        if own:
            conn.close()
