"""把 just-laws 的 README.md 解析成条级 JSONL。"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from lawagent.cnnum import cn_to_int  # noqa: E402

SRC = ROOT / "data" / "raw" / "just-laws" / "docs"
OUT = ROOT / "data" / "interim" / "articles.jsonl"

SKIP_DIRS = {".vuepress", "category", "MessageBoard", "public", "node_modules"}

CN = "零〇一二三四五六七八九十百千两0-9"
ART_RE = re.compile(r"^(?:-\s*)?\*\*(第[" + CN + r"]+条(?:之[" + CN + r"]+)?)\*\*\s*(.*)$")
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _seq(no: str):
    m = re.match(r"^第([零〇一二三四五六七八九十百千两0-9]+)条(?:之([零〇一二三四五六七八九十]+))?$", no)
    if not m:
        return 0, 0
    return cn_to_int(m.group(1)), (cn_to_int(m.group(2)) if m.group(2) else 0)


H_HEAD = re.compile(r"^#{2,3}\s+(.*)$")
BIAN_RE = re.compile(r"第[零〇一二三四五六七八九十百]+编")
ZHANG_RE = re.compile(r"第[零〇一二三四五六七八九十百]+章")
JIE_RE = re.compile(r"第[零〇一二三四五六七八九十百]+节")


def _first_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("# ") and not s.startswith("## "):
                return s[2:].strip()
    except Exception:
        pass
    return ""


def parse_doc(path: Path, initial_chapter: str = ""):
    """返回 (title, preamble, articles)。articles 每项含 article_no/chapter/section/content。"""
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    i = 0
    if lines and lines[0].strip() == "---":
        j = 1
        while j < len(lines) and lines[j].strip() != "---":
            j += 1
        i = j + 1
    title = None
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("# ") and not s.startswith("## "):
            title = s[2:].strip()
            i += 1
            break
        i += 1
    if title is None:
        return None, [], []

    preamble = []
    articles = []
    cur = None
    chapter = initial_chapter
    section = ""
    in_toc = False

    def close():
        nonlocal cur
        if cur:
            body = "\n".join(cur["lines"]).strip()
            body = re.sub(r"\n{3,}", "\n\n", body)
            if body:
                articles.append({
                    "article_no": cur["article_no"],
                    "chapter": cur["chapter"],
                    "section": cur["section"],
                    "content": body,
                })
            cur = None

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        m_head = H_HEAD.match(line)
        if m_head:
            h = m_head.group(1).strip()
            if "目录" in h:
                in_toc = True
                i += 1
                continue
            in_toc = False
            if BIAN_RE.search(h):
                chapter = h
                section = ""
            elif ZHANG_RE.search(h):
                if chapter and "编" in chapter:
                    section = h
                else:
                    chapter = h
                    section = ""
            elif JIE_RE.search(h):
                section = h
            i += 1
            continue
        if in_toc:
            i += 1
            continue

        m = ART_RE.match(stripped)
        if m:
            close()
            cur = {"article_no": m.group(1), "chapter": chapter, "section": section, "lines": []}
            rest = m.group(2).strip()
            if rest:
                cur["lines"].append(rest)
            i += 1
            continue

        if cur is None:
            if stripped and not stripped.startswith(">") and not stripped.startswith("<!--"):
                preamble.append(stripped)
        else:
            if stripped:
                cur["lines"].append(stripped)
        i += 1
    close()
    return title, preamble, articles


def _is_extra_doc(title: str) -> bool:
    """分卷目录中的修正案／决定类文件：不并入现行条文。"""
    return (not title) or ("修正案" in title) or ("决定" in title)


def _merge_split_law(base_dir: Path):
    merged = []
    for sub in sorted(base_dir.glob("*.md")):
        if sub.name.lower() == "readme.md":
            continue
        head = _first_title(sub)
        if _is_extra_doc(head):
            continue
        init = head if BIAN_RE.search(head) else ""
        _t, _pre, arts = parse_doc(sub, initial_chapter=init)
        merged.extend(arts)
    return merged


def main():
    files = sorted(SRC.glob("*/*/README.md"))
    print("发现", len(files), "个候选文件")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    laws = 0
    skipped = []
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        for p in files:
            rel = p.relative_to(SRC)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            try:
                title, preamble, articles = parse_doc(p)
            except Exception as exc:
                skipped.append((str(rel), "解析异常 %s" % exc))
                continue
            if title is None:
                skipped.append((str(rel), "无标题"))
                continue

            law_name = title
            if not articles:
                try:
                    articles = _merge_split_law(p.parent)
                except Exception as exc:
                    skipped.append((str(rel), "分卷解析异常 %s" % exc))
                    continue

            if not articles:
                skipped.append((str(rel), "无条文"))
                continue

            seen = set()
            uniq = []
            for a in articles:
                if a["article_no"] in seen:
                    continue
                seen.add(a["article_no"])
                uniq.append(a)

            pre = "\n".join(preamble)
            passed = DATE_RE.search(pre)
            passed_date = "-".join(passed.groups()) if passed else ""
            eff = re.search(r"自?\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*起?\s*施行", pre)
            effective_date = "-".join(eff.groups()) if eff else ""
            issuer = ""
            if "全国人民代表大会常务委员会" in pre:
                issuer = "全国人民代表大会常务委员会"
            elif "全国人民代表大会" in pre:
                issuer = "全国人民代表大会"

            laws += 1
            for a in uniq:
                seq_main, seq_sub = _seq(a["article_no"])
                row = {
                    "law_name": law_name,
                    "law_level": "法律",
                    "issuer": issuer,
                    "passed_date": passed_date,
                    "effective_date": effective_date,
                    "status": "现行有效",
                    "source_name": "just-laws",
                    "article_no": a["article_no"],
                    "seq_main": seq_main,
                    "seq_sub": seq_sub,
                    "chapter": a["chapter"],
                    "section": a["section"],
                    "content": a["content"],
                    "dept": rel.parts[0],
                    "slug": str(rel.parent).replace("\\", "/"),
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            total += len(uniq)

    print("法律 %d 部，条文 %d 条 -> %s" % (laws, total, OUT))
    if skipped:
        print("跳过 %d 个：" % len(skipped))
        for name, why in skipped:
            print("  -", name, "|", why)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
