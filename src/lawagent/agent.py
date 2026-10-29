"""归档 Agent：拖进来的文件 + 用户提示词 → 决策 → 执行工具 → 核验 → 回执。

Agent 闭环（四个阶段，全部写入回执，可追溯）：
  1. 理解：读提示词 + 文件元信息 + 首段，产出结构化归档计划（target / course / title / confidence / reason）
  2. 决策：规则记忆（用户改过一次就记住）→ 显式提示词 → 模型判断 → 关键词兜底
  3. 执行：写 wiki 收件箱 / 生成文献页 / 只读核对法条 / 建资料索引（工具化，每步有 ok/耗时/说明）
  4. 核验：把文件里出现的《法名》第X条逐条对照 articles.db（存在性 + 逐字），写进回执

纪律：法条库是权威层，Agent 只能读、不能改；索引可删可重建，永不作证据。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import docs, extract
from . import gate as G
from .cnnum import int_to_cn
from .config import ROOT, load_config
from .llm import chat as llm_chat

TARGETS = {
    "note": "课程笔记",
    "paper": "法学论文 / 文献",
    "doc": "资料（讲义 / 法规 / 其他）",
    "case": "案例",
    "law": "法条核对",
}
COURSES = {
    "civil-general": "民法",
    "criminal-general": "刑法总论",
    "criminal-specific": "刑法分论",
    "jurisprudence": "法理学",
    "roman-law": "罗马法",
    "evidence": "证据法",
    "confiscation": "追缴没收法",
    "energy": "环境法",
}
# 提示词里的显式意图词（命中即视为用户已指定，置信度拉到 0.75 以上）
HINTS = {
    "paper": ["论文", "文献", "期刊", "学术", "学报", "载于"],
    "note": ["笔记", "课堂", "上课", "听课"],
    "case": ["案例", "判例", "裁判", "指导案例", "判决"],
    "law": ["法条核对", "核对法条", "条文核对", "修正案", "立法", "入库核对"],
    "doc": ["资料", "讲义", "法规", "条例", "司法解释", "课件", "ppt", "报告"],
}
COURSE_WORDS = {
    "civil-general": ["民法", "物权", "合同", "侵权", "婚姻", "继承"],
    "criminal-general": ["刑法总论", "总论", "犯罪论", "刑罚论"],
    "criminal-specific": ["刑法分论", "分论", "罪名", "分则"],
    "jurisprudence": ["法理", "法哲学", "法的本体", "法律行为"],
    "roman-law": ["罗马法"],
    "evidence": ["证据法", "证据"],
    "confiscation": ["追缴", "没收"],
    "energy": ["环境法", "能源法"],
}

PLAN_PROMPT = """你是法学资料归档助手。根据【用户提示词】【文件信息】【正文开头】决定归档方式，只输出一个 JSON 对象，不要解释。

可选 target：
- note：课程笔记（要按课程归档，后续编译成 wiki 页）
- paper：法学论文/文献（要生成文献页，并核验其中引用的法条）
- doc：一般资料（讲义、法规全文、课件等，只做全文索引）
- case：案例（判例、指导案例，进案例索引）
- law：要求核对法条（只生成核对报告，绝不修改法条库）
- unknown：无法判断

可选 course（只对 note/paper/case 有意义，填不准则留空）：
{courses}

输出格式：
{{"target":"note|paper|doc|case|law|unknown","course":"<英文键或空串>","title":"<不超过 40 字的标题>","tags":["..."],"confidence":0.0-1.0,"reason":"<一句话理由>"}}

【用户提示词】
{prompt}

【文件信息】
文件名：{name}
类型：{kind}；字数：{chars}；页数：{pages}

【正文开头】
{excerpt}
"""


def _extract_json(text: str):
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def guess_target(prompt: str) -> str:
    p = prompt or ""
    for target, words in HINTS.items():
        if any(w in p for w in words):
            return target
    return ""


def guess_course(text: str) -> str:
    t = text or ""
    best, best_n = "", 0
    for course, words in COURSE_WORDS.items():
        n = sum(1 for w in words if w in t)
        if n > best_n:
            best, best_n = course, n
    return best


def _llm_plan(prompt, doc, excerpt, cfg):
    courses = "、".join("%s（%s）" % (v, k) for k, v in COURSES.items())
    msg = PLAN_PROMPT.format(
        courses=courses,
        prompt=(prompt or "（用户没有写提示词）")[:800],
        name=doc.get("name", ""),
        kind=doc.get("kind", ""),
        chars=doc.get("chars", 0),
        pages=doc.get("pages", 0) or "—",
        excerpt=(excerpt or "")[:1200],
    )
    fast = json.loads(json.dumps(cfg, ensure_ascii=False))
    llm = fast.setdefault("llm", {})
    llm["timeout"] = 90
    llm.setdefault("cloud", {})["timeout"] = 25
    for backend in ("cloud", "local"):
        try:
            raw = llm_chat(
                [{"role": "system", "content": "你只输出合法 JSON。"},
                 {"role": "user", "content": msg}],
                cfg=fast, backend=backend,
            )
        except Exception:
            continue
        data = _extract_json(raw or "")
        if isinstance(data, dict) and data.get("target") in TARGETS:
            return data, backend
    return None, ""


def plan(prompt: str, doc: dict, excerpt: str, cfg=None, rule: dict = None, forced: str = "") -> dict:
    """产出归档计划。优先级：用户手选 > 规则记忆 > 模型 > 关键词兜底。"""
    cfg = cfg or load_config()
    p = (prompt or "").strip()

    if forced in TARGETS:
        return {"target": forced, "course": (rule or {}).get("course", "") or guess_course(p + "\n" + excerpt),
                "title": doc.get("title") or Path(doc.get("name", "")).stem,
                "tags": [], "confidence": 1.0, "reason": "用户指定归档方式", "source": "user"}

    if rule:
        return {"target": rule.get("target", "doc"), "course": rule.get("course", ""),
                "title": doc.get("title") or Path(doc.get("name", "")).stem,
                "tags": [], "confidence": 0.95,
                "reason": "命中归档记忆：“%s” → %s" % (rule.get("keyword", ""), TARGETS.get(rule.get("target"), rule.get("target"))),
                "source": "rule"}

    explicit = guess_target(p)
    data, backend = _llm_plan(p, doc, excerpt, cfg)
    if data:
        out = {
            "target": data.get("target") or "doc",
            "course": data.get("course") if data.get("course") in COURSES else "",
            "title": (str(data.get("title") or "").strip()[:60]) or doc.get("title") or Path(doc.get("name", "")).stem,
            "tags": [str(t)[:20] for t in (data.get("tags") or []) if str(t).strip()][:8],
            "confidence": max(0.0, min(1.0, float(data.get("confidence") or 0.5))),
            "reason": str(data.get("reason") or "")[:200],
            "source": "llm:" + backend,
        }
        if explicit:
            out["confidence"] = max(out["confidence"], 0.8)
            out["reason"] = (out["reason"] + "；提示词含显式意图词").strip("；")
        if not out["course"]:
            out["course"] = guess_course(p + "\n" + excerpt)
        return out

    target = explicit or "doc"
    conf = 0.75 if explicit else 0.35
    return {"target": target, "course": guess_course(p + "\n" + excerpt),
            "title": doc.get("title") or Path(doc.get("name", "")).stem,
            "tags": [], "confidence": conf,
            "reason": "模型不可用，按关键词规则推断" if explicit else "模型不可用且提示词无明确意图",
            "source": "keyword"}


# ---------------- 引用核验（只读 articles.db） ----------------

_ART_RE = re.compile(r"^第([零〇一二三四五六七八九十百千两0-9]+)条(之[零〇一二三四五六七八九十]+)?$")


def _art_norm(art: str) -> str:
    s = (art or "").replace(" ", "")
    m = _ART_RE.match(s)
    if not m:
        return s
    num, suffix = m.group(1), m.group(2) or ""
    if num.isdigit():
        num = int_to_cn(int(num))
    return "第%s条%s" % (num, suffix)


def law_index(conn):
    out = {}
    for r in conn.execute("SELECT law_id, law_name, status FROM laws"):
        out.setdefault(G._norm_law(r["law_name"]), (r["law_id"], r["law_name"], r["status"]))
    return out


def verify_citations(text: str, conn, min_quote_len: int = 10) -> dict:
    """核对文本里的《法名》第X条：存在性 + 逐字 + 效力状态。"""
    text = text or ""
    idx = law_index(conn)
    items, seen = [], set()
    for m in G.CITE_RE.finditer(text):
        law_raw, art_raw = m.group(1), m.group(2)
        law_norm, art = G._norm_law(law_raw), _art_norm(art_raw)
        key = (law_norm, art)
        if key in seen:
            continue
        seen.add(key)
        hit = idx.get(law_norm)
        item = {"law": law_raw.strip("《》"), "article_no": art, "found": False, "quote_ok": None,
                "status": "", "sentence": "", "warning": ""}
        if hit:
            law_id, law_name, status = hit
            row = conn.execute("SELECT content FROM articles WHERE law_id=? AND article_no=? LIMIT 1",
                               (law_id, art)).fetchone()
            if row is not None:
                item["found"] = True
                item["status"] = status or ""
                left, right = G._sentence_bounds(text, m.start())
                sent = text[left:right].strip()
                item["sentence"] = sent[:160]
                sent_wo = G.CITE_RE.sub("", sent)
                norm = G._norm_text(sent_wo)
                if len(norm) < min_quote_len:
                    item["quote_ok"] = None  # 只写条号、没引原文
                else:
                    item["quote_ok"] = bool(G._grams(norm, min_quote_len) & G._grams(G._norm_text(row["content"]), min_quote_len))
                if status and status != "现行有效":
                    item["warning"] = "该条文状态为「%s」" % status
        items.append(item)

    failed = [i for i in items if not i["found"] or i["quote_ok"] is False]
    warn = [i for i in items if i["warning"]]
    return {
        "checked": len(items),
        "passed": len([i for i in items if i["found"] and i["quote_ok"] is not False]),
        "failed": len(failed),
        "warnings": len(warn),
        "items": items,
    }


# ---------------- 执行工具 ----------------


def _wiki_raw(cfg):
    rel = _ing(cfg).get("wiki_raw") or ""
    if rel:
        p = Path(rel)
        if not p.is_absolute():
            p = ROOT / p
        return p
    return ROOT / "data" / "wiki_raw"


def _ing(cfg):
    return (cfg or {}).get("ingest", {})


def _wiki_inbox_dir(cfg, course: str, user_id: str = "owner") -> Path:
    """owner 沿用 raw/law/<course>/inbox；其他用户写 raw/users/<uid>/<course>/inbox。"""
    raw = _wiki_raw(cfg)
    uid = docs._uid(user_id)
    safe_course = re.sub(r"[^A-Za-z0-9_-]+", "", str(course or ""))[:64]
    if uid == "owner":
        d = raw / "law" / safe_course / "inbox" if safe_course else raw / "inbox"
    else:
        base = raw / "users" / uid
        d = base / safe_course / "inbox" if safe_course else base / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _front_matter(p: dict, doc: dict) -> str:
    lines = ["---", "title: %s" % p["title"], "type: %s" % p["target"],
             "course: %s" % (p.get("course") or ""), "source_file: %s" % doc.get("name", ""),
             "ingested: %s" % time.strftime("%Y-%m-%d %H:%M"), "confidence: %.2f" % p.get("confidence", 0),
             "tags: [%s]" % ", ".join(p.get("tags") or []), "---", ""]
    return "\n".join(lines)


def write_note(p: dict, doc: dict, text: str, excerpt: str, cfg,
               user_id: str = "owner") -> str:
    """写入当前用户的 wiki 收件箱（待编译）。返回绝对路径。"""
    folder = _wiki_inbox_dir(cfg, p.get("course") or "", user_id)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", p["title"])[:80] or doc.get("name", "note")
    path = folder / (name + ".md")
    n = 1
    while path.exists():
        path = folder / ("%s-%d.md" % (name, n))
        n += 1
    head = [_front_matter(p, doc), "# %s" % p["title"], "",
            "> 来源：%s ｜ 归档：%s ｜ 类型：%s" % (doc.get("name", ""), time.strftime("%Y-%m-%d"),
                                            TARGETS.get(p["target"], p["target"])), ""]
    if p["target"] == "paper":
        head += ["## 题录", "", "| 项 | 内容 |", "|---|---|",
                 "| 标题 | %s |" % p["title"], "| 作者 | （待补） |",
                 "| 刊物 / 来源 | （待补） |", "| 年份 | （待补） |", ""]
    abstract = "（原文较短，全文见下）" if len(text.strip()) <= 800 else (excerpt or "")[:800]
    body = head + ["## 摘要", "", abstract, "", "## 原文", "", text]
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path)


def append_citation_table(path: str, ver: dict) -> str:
    """文献页末尾追加“引文核验表”（写论文/综述可直接引用）。"""
    lines = ["", "", "## 引文核验表", "", "| 《法名》条号 | 库内存在 | 引文逐字 | 状态 | 备注 |", "|---|---|---|---|---|"]
    for it in (ver or {}).get("items") or []:
        q = "—" if it.get("quote_ok") is None else ("✓" if it.get("quote_ok") else "✗")
        lines.append("| 《%s》%s | %s | %s | %s | %s |" % (
            it.get("law", ""), it.get("article_no", ""), "✓" if it.get("found") else "✗", q,
            it.get("status") or "—", it.get("warning") or ""))
    with Path(path).open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def law_report(p: dict, doc: dict, ver: dict, cfg) -> str:
    """法条核对报告（只写报告，绝不改库）。"""
    out = Path(_ing(cfg).get("reports_dir") or (ROOT / "data" / "reports"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("lawcheck-%s-%s.md" % (time.strftime("%Y%m%d-%H%M"), doc.get("doc_id", "doc")))
    lines = ["# 法条核对报告：%s" % doc.get("name", ""), "",
             "> 生成：%s ｜ 仅核对，未改动法条库" % time.strftime("%Y-%m-%d %H:%M"), "",
             "## 结论", "",
             "- 共发现条文引用 **%d** 处，通过 **%d**，未通过 **%d**，效力状态提醒 **%d**"
             % (ver["checked"], ver["passed"], ver["failed"], ver["warnings"]), "",
             "## 明细", "", "| 《法名》条号 | 库内存在 | 引文逐字 | 状态 | 备注 |", "|---|---|---|---|---|"]
    for it in ver["items"]:
        q = "—" if it["quote_ok"] is None else ("✓" if it["quote_ok"] else "✗")
        lines.append("| 《%s》%s | %s | %s | %s | %s |" % (
            it["law"], it["article_no"], "✓" if it["found"] else "✗", q,
            it["status"] or "—", it["warning"] or ""))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _step(steps, tool, fn):
    t = time.time()
    try:
        detail = fn()
        steps.append({"tool": tool, "ok": True, "ms": int((time.time() - t) * 1000), "detail": str(detail or "")})
        return True, detail
    except Exception as exc:  # noqa: BLE001
        steps.append({"tool": tool, "ok": False, "ms": int((time.time() - t) * 1000),
                      "detail": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
        return False, None


def run(doc_id: str, prompt: str = "", target: str = "", cfg=None,
        user_id: str = "owner") -> dict:
    """主流程：决策 → 执行 → 核验 → 回执（只处理当前用户的文件）。"""
    cfg = cfg or load_config()
    t0 = time.time()
    doc = docs.get_doc(doc_id, cfg, user_id)
    if not doc:
        return {"status": "failed", "error": "找不到这篇文件（doc_id=%s）" % doc_id}
    text = docs.read_doc_text(doc)
    excerpt = text[:800]
    steps = []

    rule = docs.find_rule(
        (prompt or "") + "\n" + doc.get("title", "") + "\n" + doc.get("name", ""),
        cfg, user_id,
    )
    p = plan(prompt, doc, excerpt, cfg, rule, target)
    steps.append({"tool": "decide", "ok": True, "ms": 0,
                  "detail": "%s（%s，置信度 %.2f）：%s" % (
                      TARGETS.get(p["target"], p["target"]), p.get("source", ""),
                      p.get("confidence", 0), p.get("reason", ""))})

    threshold = float(_ing(cfg).get("confirm_below", 0.6))
    if not rule and not (target or "").strip() and p.get("confidence", 0) < threshold:
        receipt = {
            "receipt_id": None, "doc_id": doc_id, "status": "need_confirm",
            "target": p["target"], "doc": {"name": doc.get("name"), "title": doc.get("title"),
                                           "kind": doc.get("kind"), "chars": doc.get("chars")},
            "plan": p, "steps": steps, "citations": {"checked": 0, "items": []},
            "question": "这篇文件我没把握，应该归档到哪一类？",
            "options": [{"target": k, "label": v} for k, v in TARGETS.items()],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        receipt["receipt_id"] = docs.save_receipt(receipt, cfg, user_id)
        return receipt

    # 1) 任何目标都先建索引（先索引，后归档，保证随时可检索）
    def _index():
        conn = docs.connect(cfg)
        try:
            return "%d 块已入索引" % (doc.get("chunks") or 0)
        finally:
            conn.close()
    _step(steps, "index_doc", _index)

    # 2) 按目标落地
    produced = ""
    if p["target"] in ("note", "paper", "case"):
        ok, path = _step(steps, "write_wiki_inbox",
                         lambda: write_note(p, doc, text, excerpt, cfg, user_id))
        produced = path or ""
    elif p["target"] == "law":
        pass  # 先核验，后写报告
    else:  # doc
        steps.append({"tool": "copy_archive", "ok": True, "ms": 0,
                      "detail": doc.get("path", "") or "原件已归档"})

    # 3) 引用核验（所有类型都做）
    conn = None
    ver = {"checked": 0, "passed": 0, "failed": 0, "warnings": 0, "items": []}
    try:
        import sqlite3
        dbp = (cfg.get("paths") or {}).get("db", "data/db/articles.db")
        p_db = Path(dbp) if Path(dbp).is_absolute() else (ROOT / dbp)
        if not p_db.exists():
            raise FileNotFoundError(str(p_db))
        conn = sqlite3.connect("file:%s?mode=ro" % str(p_db).replace("\\", "/"), uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        ok, res = _step(steps, "verify_citations",
                        lambda: verify_citations(text, conn, int(cfg.get("gate", {}).get("min_quote_len", 10))))
        if ok and res:
            ver = res
            steps[-1]["detail"] = "共 %d 处引用：通过 %d、未通过 %d、状态提醒 %d" % (
                ver["checked"], ver["passed"], ver["failed"], ver["warnings"])
        if p["target"] == "law":
            ok2, rpath = _step(steps, "write_law_report", lambda: law_report(p, doc, ver, cfg))
            produced = rpath or produced
        elif p["target"] == "paper" and produced:
            _step(steps, "append_citation_table", lambda: append_citation_table(produced, ver))
    except Exception as exc:  # noqa: BLE001
        steps.append({"tool": "verify_citations", "ok": False, "ms": 0,
                      "detail": "无法连接法条库：%s" % str(exc)[:200]})
    finally:
        if conn is not None:
            conn.close()

    status = "done" if all(s["ok"] for s in steps) else "partial"
    notes = []
    if p["target"] in ("note", "paper", "case"):
        uid = docs._uid(user_id)
        if uid == "owner":
            notes.append("已写入 wiki 收件箱，待编译（运行 一键整理.cmd 生成 wiki 页）")
        else:
            notes.append("已写入你的 wiki 收件箱（raw/users/%s/），待编译："
                         "python compile_inbox.py --user %s" % (uid, uid))
    if p["target"] == "law":
        notes.append("只生成核对报告，法条库未改动（授权层需人工确认）")
    if ver["checked"] == 0:
        notes.append("文件中未发现《法名》第X条 形式的引用")
    if ver["failed"]:
        notes.append("有 %d 处引用与库内原文对不上，请人工复核" % ver["failed"])

    receipt = {
        "receipt_id": None, "doc_id": doc_id, "status": status, "target": p["target"],
        "doc": {"name": doc.get("name"), "title": doc.get("title"), "kind": doc.get("kind"),
                "chars": doc.get("chars"), "chunks": doc.get("chunks"), "path": doc.get("path")},
        "plan": p, "steps": steps, "citations": ver, "produced": produced, "notes": notes,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    receipt["receipt_id"] = docs.save_receipt(receipt, cfg, user_id)
    return receipt


def confirm(receipt_id: str, target: str, course: str = "", cfg=None,
            user_id: str = "owner") -> dict:
    """用户确认归档去向：记住这条规则，然后重跑（只操作当前用户的回执/文件）。"""
    cfg = cfg or load_config()
    r = docs.get_receipt(receipt_id, cfg, user_id)
    if not r:
        return {"status": "failed", "error": "找不到回执（receipt_id=%s）" % receipt_id}
    doc_id = r.get("doc_id", "")
    doc = docs.get_doc(doc_id, cfg, user_id)
    if not doc:
        return {"status": "failed", "error": "找不到原文件记录"}
    keyword = (doc.get("title") or Path(doc.get("name", "")).stem or "")[:60]
    if keyword:
        docs.add_rule(keyword, target, course, cfg, user_id)
    receipt = run(doc_id, prompt="", target=target, cfg=cfg, user_id=user_id)
    if course and receipt.get("plan"):
        receipt["plan"]["course"] = course
    return receipt
