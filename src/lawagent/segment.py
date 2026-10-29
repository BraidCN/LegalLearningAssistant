"""中文分词与查询归一化。"""
import re

import jieba

from .config import ROOT
from .cnnum import int_to_cn

_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
_DICT_LOADED = False


def _ensure_dict() -> None:
    global _DICT_LOADED
    if _DICT_LOADED:
        return
    p = ROOT / "configs" / "lexicon" / "law_terms.txt"
    if p.exists():
        jieba.load_userdict(str(p))
    _DICT_LOADED = True


def segment(text: str) -> str:
    _ensure_dict()
    toks = []
    for t in jieba.lcut_for_search(text or ""):
        t = t.strip()
        if not t or _PUNCT_ONLY.match(t):
            continue
        toks.append(t)
    return " ".join(toks)


def tokens(text: str):
    return segment(text).split()


def normalize_query(q: str) -> str:
    return re.sub(r"第\s*(\d+)\s*条", lambda m: "第%s条" % int_to_cn(int(m.group(1))), q or "")
