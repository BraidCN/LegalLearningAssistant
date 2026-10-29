"""中文数字与阿拉伯数字转换（条号用）。"""
_DIG = "零一二三四五六七八九"


def int_to_cn(n: int) -> str:
    if n < 0:
        return str(n)
    if n == 0:
        return "零"
    units = ["", "十", "百", "千", "万"]
    digits = str(n)
    out = ""
    zero_pending = False
    length = len(digits)
    for idx, ch in enumerate(digits):
        d = int(ch)
        pos = length - idx - 1
        if pos >= len(units):
            out += ch
            continue
        if d == 0:
            if out:
                zero_pending = True
            continue
        if zero_pending:
            out += "零"
            zero_pending = False
        out += _DIG[d] + units[pos]
    if out.startswith("一十"):
        out = out[1:]
    return out


def cn_to_int(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    mp = {c: i for i, c in enumerate(_DIG)}
    mp["两"] = 2
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    cur = 0
    for ch in s:
        if ch in mp:
            cur = mp[ch]
        elif ch in units:
            if cur == 0:
                cur = 1
            total += cur * units[ch]
            cur = 0
    return total + cur
