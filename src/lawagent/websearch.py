"""联网检索：Bing RSS 优先、HTML 兜底；360/DDG 补充与多源自动切换；网页正文抓取与来源排序。

只依赖标准库（urllib / xml.etree / html.parser / json），不新增第三方依赖。
"""
import gzip
import glob
import html as html_mod
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from html.entities import name2codepoint
from html.parser import HTMLParser

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BING_RSS = "https://www.bing.com/search?q=%s&format=rss&setlang=zh-CN"
BING_HTML = "https://www.bing.com/search?q=%s&setlang=zh-CN"
SO360_HTML = "https://www.so.com/s?q=%s"

# 多源检索的兜底顺序（configs/config.json 的 web.providers 优先）。
DEFAULT_PROVIDERS = ("bing", "ddg", "360")

# 各源别名 -> 内部名；未知名字直接跳过。
_PROVIDER_ALIASES = {
    "bing": "bing",
    "ddg": "ddg",
    "duckduckgo": "ddg",
    "360": "360",
    "so": "360",
    "so360": "360",
    "360so": "360",
}

# 问句里的噪声词（保守清单：只去疑问/寒暄开场，不碰“最新/进展”等实词）。
_NOISE_PHRASES = (
    "请问一下",
    "请问下",
    "请问",
    "请教一下",
    "请教",
    "麻烦问一下",
    "我想知道",
    "想问一下",
    "有哪些",
    "有什么",
    "应该怎么",
    "怎么办理",
    "怎么处理",
    "怎么办",
)

# 官方来源优先：越靠前越权威，子域自动命中（如 flk.npc.gov.cn）。
OFFICIAL_DOMAINS = (
    "npc.gov.cn",
    "court.gov.cn",
    "spp.gov.cn",
    "moj.gov.cn",
    "gov.cn",
    "legaldaily.com.cn",
    "xinhuanet.com",
    "people.com.cn",
    "chinacourt.org",
    "legalinfo.gov.cn",
)

_MAX_BYTES = 2_000_000
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def _collapse(text: str) -> str:
    """压缩全部空白（含全角空格、&nbsp;）为单个空格。"""
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_tags(text: str) -> str:
    """去掉 HTML 标签并反转义实体，用于 RSS description。"""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _collapse(html_mod.unescape(text))


def _bigrams(text: str) -> set:
    """去掉全部空白后取二字片段集合，用于粗相关性判断。"""
    s = "".join((text or "").split())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


# 粗相关性阈值：命中多少个二字片段才算“相关”。默认 1（与 pipeline 口径一致）；
# 长问句误判多时调高（2~3 可有效压掉“民法_百度百科”这类只沾一个词的结果）。
MIN_BIGRAM_HITS = 1


def _relevant_hit(query: str, hit: dict) -> bool:
    """问题与标题/摘要至少共享 MIN_BIGRAM_HITS 个二字片段——挡住搜索引擎的无关缓存页。"""
    body = (hit.get("title") or "") + (hit.get("snippet") or "")
    return len(_bigrams(query) & _bigrams(body)) >= max(1, int(MIN_BIGRAM_HITS))


def clean_query(query: str) -> str:
    """去掉问句噪声词（请问/有什么/怎么办…），保留实词；去完太短则返回原串。"""
    raw = _collapse(query or "")
    if not raw:
        return ""
    text = raw
    for phrase in _NOISE_PHRASES:
        if phrase in text:
            text = text.replace(phrase, " ")
    text = _collapse(text)
    text = text.strip(" \t，。！？、；：“”‘’()（）?？!.,;:~～-")
    if len(text) < 2:
        return raw
    return text


def _dedupe_cap(items: list[dict], top_k: int, source: str = "") -> list[dict]:
    """按 url 去重、补齐 source 字段并截断到 top_k。"""
    out, seen = [], set()
    for r in items or []:
        url = _collapse(r.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "title": _collapse(r.get("title") or ""),
                "url": url,
                "snippet": _collapse(r.get("snippet") or ""),
                "source": source or _collapse(r.get("source") or ""),
            }
        )
        if len(out) >= top_k:
            break
    return out


def _local(tag) -> str:
    """去掉 XML 命名空间，返回小写本地名。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _normalize_entities(text: str) -> str:
    """把 XML 不认识的 HTML 命名实体（&nbsp; 等）换成数字引用。"""
    def repl(m):
        name = m.group(1)
        cp = name2codepoint.get(name)
        if cp is None:
            return m.group(0)
        return "&#%d;" % cp

    return re.sub(r"&([a-zA-Z][a-zA-Z0-9]{1,31});", repl, text)


# ---------------------------------------------------------------- HTTP


def _decode(raw: bytes, content_type: str) -> str:
    charset = ""
    m = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1).strip().lower()
    candidates = [charset] if charset and charset not in ("utf-8", "utf8") else []
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


_CTX = None


def _ssl_context():
    """带较大 CA 包的 SSL 上下文（本机默认信任库不含 CFCA 根，会误判 gov.cn）。

    依次尝试 SSL_CERT_FILE、Python/Git/MSYS 的 ca-bundle；全部不可用时回落到
    系统默认上下文。始终开启证书校验，不做 verify=False。
    """
    global _CTX
    if _CTX is not None:
        return _CTX
    paths = []
    env = os.environ.get("SSL_CERT_FILE")
    if env:
        paths.append(env)
    paths.append(os.path.join(sys.prefix, "ssl", "cacert.pem"))
    paths.append(r"C:\Program Files\Common Files\SSL\cert.pem")
    paths.append(r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt")
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    paths.extend(
        sorted(
            glob.glob(
                os.path.join(
                    local, "hermes", "tools", "git-*", "mingw64", "etc", "ssl",
                    "certs", "ca-bundle.crt",
                )
            ),
            reverse=True,
        )
    )
    ctx = None
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            cand = ssl.create_default_context(cafile=path)
        except (OSError, ssl.SSLError, ValueError):
            continue
        if cand.cert_store_stats().get("x509", 0) > 0:
            ctx = cand
            break
    _CTX = ctx or ssl.create_default_context()
    return _CTX


_OPENER = None
_COOKIE_JAR = None
_BING_WARMED = False


def _get_opener():
    """带 Cookie 的 opener（Bing 无 Cookie 会返回无关缓存页）。"""
    global _OPENER, _COOKIE_JAR
    if _OPENER is None:
        import http.cookiejar

        _COOKIE_JAR = http.cookiejar.CookieJar()
        _OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(_COOKIE_JAR),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
    return _OPENER


def _warm_bing() -> None:
    """访问一次 Bing 首页拿会话 Cookie；失败不重试、不影响后续。"""
    global _BING_WARMED
    if _BING_WARMED:
        return
    _BING_WARMED = True
    try:
        req = urllib.request.Request(
            "https://cn.bing.com/",
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        _get_opener().open(req, timeout=8).read(2048)
    except Exception:
        pass


def _fetch(url: str, timeout: int = 12) -> str:
    """GET 一个页面，返回解码后的文本；任何失败抛异常由调用方兜住。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        },
    )
    if "bing.com" in url:
        _warm_bing()
    with _get_opener().open(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_BYTES)
        headers = resp.headers
        ctype = headers.get("Content-Type") or ""
        enc = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    return _decode(raw, ctype)


# ---------------------------------------------------------------- RSS


def _parse_rss(xml_text: str) -> list[dict]:
    """解析 Bing RSS：<item> 的 title / link / description。失败返回 []。"""
    if not xml_text or not xml_text.strip():
        return []
    raw = xml_text.strip()
    stripped = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw)
    root = None
    for candidate in (raw, stripped, _normalize_entities(stripped)):
        try:
            root = ET.fromstring(candidate.encode("utf-8"))
            break
        except (ET.ParseError, ValueError, UnicodeDecodeError):
            continue
    if root is None:
        return []

    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = link = desc = ""
        for child in list(item):
            name = _local(child.tag)
            if name == "title":
                title = title or (child.text or "")
            elif name == "link":
                link = link or ((child.text or "").strip() or child.get("href", ""))
            elif name == "description":
                desc = desc or (child.text or "")
        title = _strip_tags(title)
        link = _collapse(link)
        if not (title or link):
            continue
        out.append(
            {
                "title": title,
                "url": link,
                "snippet": _strip_tags(desc),
                "source": "bing",
            }
        )
    return out


# ---------------------------------------------------------------- HTML


class _BingHTMLParser(HTMLParser):
    """抓 li.b_algo（也兼容 div.b_algo）：h2>a 与首个 p 的文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._depth = 0
        self._cur = None
        self._in_h2 = 0
        self._in_a = 0
        self._in_p = 0
        self._tbuf = []
        self._sbuf = []

    # -- 内部
    def _reset(self):
        self._cur = None
        self._in_h2 = self._in_a = self._in_p = 0
        self._tbuf, self._sbuf = [], []

    def _flush(self):
        cur = self._cur
        if cur:
            url = _collapse(cur.get("url", ""))
            title = _collapse(cur.get("title", ""))
            if url and not url.lower().startswith(("javascript:", "#")):
                self.results.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": _collapse(cur.get("snippet", "")),
                        "source": "bing",
                    }
                )
        self._reset()

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_d = {str(k).lower(): (v or "") for k, v in attrs}
        classes = (attrs_d.get("class") or "").split()

        if not self._depth:
            if "b_algo" in classes:
                self._reset()
                self._cur = {"title": "", "url": "", "snippet": ""}
                self._depth = 1
            return

        if tag not in _VOID_TAGS:
            self._depth += 1
        if tag == "h2":
            self._in_h2 += 1
        elif tag == "a":
            if not self._cur["url"] and (self._in_h2 or not self._cur["title"]):
                href = attrs_d.get("href") or ""
                if href:
                    self._cur["url"] = href
                    self._in_a += 1
                    self._tbuf = []
        elif tag == "p" and not self._cur["snippet"]:
            self._in_p += 1
            self._sbuf = []

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self._depth or not isinstance(self._cur, dict):
            return
        if tag == "h2" and self._in_h2:
            self._in_h2 -= 1
        elif tag == "a" and self._in_a:
            self._in_a -= 1
            if not self._cur["title"]:
                self._cur["title"] = "".join(self._tbuf)
            self._tbuf = []
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            if not self._cur["snippet"]:
                self._cur["snippet"] = "".join(self._sbuf)
            self._sbuf = []
        if tag not in _VOID_TAGS:
            self._depth -= 1
            if self._depth == 0:
                self._flush()

    def handle_data(self, data):
        if not self._depth or not data:
            return
        if self._in_a:
            self._tbuf.append(data)
        elif self._in_p:
            self._sbuf.append(data)

    def close(self):
        super().close()
        if self._depth:
            self._flush()


def _parse_bing_html(html_text: str) -> list[dict]:
    """解析 Bing 结果页的 li.b_algo 块。失败返回 []。"""
    if not html_text or not html_text.strip():
        return []
    parser = _BingHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return parser.results or []
    return parser.results


class _So360Parser(HTMLParser):
    """抓 360（so.com）结果块：li.res-list / li.result，标题 h3>a，摘要取第一个总结节点。

    标题链接常被包成 so.com/link?m=… 跳转，真实地址在 data-mdurl 上，优先用它。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._depth = 0
        self._cur = None
        self._in_h3 = 0
        self._in_title_a = 0
        self._tbuf = []
        self._snip_level = 0
        self._sbuf = []
        self._all = []

    # -- 内部
    def _reset(self):
        self._cur = None
        self._in_h3 = self._in_title_a = 0
        self._tbuf, self._sbuf, self._all = [], [], []
        self._snip_level = 0

    def _flush(self):
        cur = self._cur
        if cur:
            url = _collapse(cur.get("url", ""))
            title = _collapse(cur.get("title", ""))
            snippet = _collapse(cur.get("snippet", ""))
            body = _collapse("".join(self._all))
            if not title and body:
                title = body[:60]
            if not snippet and body:
                # 无独立摘要节点时用整块文本兜底，并去掉开头的标题重复。
                if title and body.startswith(title):
                    body = body[len(title):].lstrip(" ：:|-")
                snippet = body[:180]
            low = url.lower()
            skip = (
                not low.startswith(("http://", "https://"))
                or "//ai.so.com/" in low
                or low.startswith(("javascript:", "#"))
            )
            if url and title and not skip:
                self.results.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "source": "360",
                    }
                )
        self._reset()

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_d = {str(k).lower(): (v or "") for k, v in attrs}
        classes = (attrs_d.get("class") or "").split()

        if not self._depth:
            hit = tag == "li" and any(
                c == "result" or c.startswith("res-list") for c in classes
            )
            if hit:
                self._reset()
                self._cur = {"title": "", "url": "", "snippet": ""}
                self._depth = 1
            return

        if tag not in _VOID_TAGS:
            self._depth += 1

        if tag == "h3":
            self._in_h3 += 1
        elif tag == "a":
            if self._in_h3 and not self._cur["url"]:
                real = attrs_d.get("data-mdurl") or attrs_d.get("data-url") or ""
                href = real or attrs_d.get("href") or ""
                if href:
                    self._cur["url"] = href
                    self._in_title_a += 1
                    self._tbuf = []
        elif not self._snip_level and not self._cur["snippet"] and self._is_snippet_node(
            tag, attrs_d, classes
        ):
            self._snip_level = self._depth
            self._sbuf = []

    @staticmethod
    def _is_snippet_node(tag, attrs_d, classes) -> bool:
        if "data-result-summary" in attrs_d:
            return True
        if tag not in ("p", "div", "span", "li"):
            return False
        return any(
            c.startswith(("res-list-summary", "res-desc", "res-summary"))
            for c in classes
        )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self._depth or not isinstance(self._cur, dict):
            return
        if tag not in _VOID_TAGS:
            self._depth -= 1
        if tag == "h3" and self._in_h3:
            self._in_h3 -= 1
        elif tag == "a" and self._in_title_a:
            self._in_title_a -= 1
            if not self._cur["title"]:
                self._cur["title"] = "".join(self._tbuf)
            self._tbuf = []
        if self._snip_level and self._depth < self._snip_level:
            self._snip_level = 0
            if not self._cur["snippet"]:
                self._cur["snippet"] = "".join(self._sbuf)
            self._sbuf = []
        if self._depth <= 0:
            self._depth = 0
            self._flush()

    def handle_data(self, data):
        if not self._depth or not data:
            return
        self._all.append(data)
        if self._snip_level:
            self._sbuf.append(data)
        elif self._in_title_a:
            self._tbuf.append(data)

    def close(self):
        super().close()
        if self._depth:
            self._flush()


def _parse_360_html(html_text: str) -> list[dict]:
    """解析 360 搜索结果页的 res-list 块。失败返回 []。"""
    if not html_text or not html_text.strip():
        return []
    parser = _So360Parser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return parser.results or []
    return parser.results


class _TextParser(HTMLParser):
    """把 HTML 收成纯文本：块级标签处留空格。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            if self._skip:
                self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """HTML -> 纯文本；去掉 script/style/noscript 与注释，压缩空白。"""
    if not html:
        return ""
    text = re.sub(r"(?s)<!--.*?-->", " ", html)
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", text)
    parser = _TextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return _collapse(re.sub(r"(?s)<[^>]+>", " ", html_mod.unescape(text)))
    return _collapse(html_mod.unescape("".join(parser.chunks)))


# ---------------------------------------------------------------- 对外接口


def _parse_ddg_html(html_text: str, top_k: int = 5) -> list[dict]:
    """解析 DuckDuckGo HTML 结果（result__a / result__snippet）。失败返回 []。"""
    if not html_text:
        return []
    links = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        re.S,
    )
    snips = re.findall(
        r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
        html_text,
        re.S,
    )
    out = []
    seen = set()
    for idx, (href, title) in enumerate(links):
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = urllib.parse.unquote(m.group(1))
        if not href.startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)
        clean_title = html_mod.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = ""
        if idx < len(snips):
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snips[idx])).strip()
        out.append({"title": clean_title, "url": href, "snippet": snippet, "source": "ddg"})
        if len(out) >= top_k:
            break
    return out


def search_ddg(query: str, top_k: int = 5, timeout: int = 12) -> list[dict]:
    """DuckDuckGo HTML 搜索；Bing 返回无关结果时兜底。失败返回 []。"""
    q = (query or "").strip()
    if not q:
        return []
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    try:
        raw = _fetch(url, timeout=timeout)
    except Exception:
        return []
    return _parse_ddg_html(raw, top_k)


def search_bing(query: str, top_k: int = 5, timeout: int = 12) -> list[dict]:
    """Bing 联网检索；RSS 优先，无结果时抓 HTML。异常一律返回 []。"""
    try:
        q = (query or "").strip()
        k = int(top_k)
        if not q or k <= 0:
            return []
        quoted = urllib.parse.quote_plus(q)
        results = []
        try:
            xml_text = _fetch(BING_RSS % quoted, timeout)
            results = _parse_rss(xml_text) if xml_text else []
        except Exception:
            results = []
        if not results:
            try:
                html_text = _fetch(BING_HTML % quoted, timeout)
                results = _parse_bing_html(html_text) if html_text else []
            except Exception:
                results = []

        out, seen = [], set()
        for r in results:
            url = _collapse(r.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(
                {
                    "title": _collapse(r.get("title") or ""),
                    "url": url,
                    "snippet": _collapse(r.get("snippet") or ""),
                    "source": "bing",
                }
            )
            if len(out) >= k:
                break
        return out
    except Exception:
        return []


def search_360(query: str, top_k: int = 5, timeout: int = 12) -> list[dict]:
    """360（so.com）网页搜索；去重截断，异常返回 []。"""
    try:
        q = _collapse(query or "")
        k = int(top_k)
        if not q or k <= 0:
            return []
        raw = _fetch(SO360_HTML % urllib.parse.quote_plus(q), timeout=timeout)
        if not raw:
            return []
        return _dedupe_cap(_parse_360_html(raw), k, "360")
    except Exception:
        return []


def _config_candidates() -> list[str]:
    """配置文件候选路径：LAWAGENT_CONFIG 优先，其次仓库内 configs/config.json。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    cands = [os.path.join(root, "configs", "config.json")]
    env = os.environ.get("LAWAGENT_CONFIG")
    return ([env] if env else []) + cands


def _load_providers() -> list[str]:
    """读取 web.providers；缺失/损坏/全未知时回落 DEFAULT_PROVIDERS。"""
    for path in _config_candidates():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            continue
        web = cfg.get("web") if isinstance(cfg, dict) else None
        names = web.get("providers") if isinstance(web, dict) else None
        if not isinstance(names, list):
            continue
        out = []
        for name in names:
            key = _PROVIDER_ALIASES.get(str(name).strip().lower())
            if key and key not in out:
                out.append(key)
        if out:
            return out
    return list(DEFAULT_PROVIDERS)


_SEARCH_FUNCS = {"bing": "search_bing", "ddg": "search_ddg", "360": "search_360"}


def search_multi(
    query: str, top_k: int = 5, timeout: int = 12, providers=None
) -> list[dict]:
    """多源检索：按配置顺序（Bing→DDG→360）尝试，取第一个“有相关结果”的源。

    相关性 = 问句与 title+snippet 至少共享一个二字片段（与 pipeline 口径一致），
    用来挡住搜索引擎在长问句下返回的无关页面。每条的 source 标明来源；
    全部源失败或无相关结果时返回 []。providers 可显式覆盖配置（测试用）。
    """
    q_raw = _collapse(query or "")
    if not q_raw:
        return []
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        return []
    if k <= 0:
        return []

    q = clean_query(q_raw) or q_raw
    if providers is None:
        names = _load_providers()
    else:
        names = []
        for name in providers or ():
            key = _PROVIDER_ALIASES.get(str(name).strip().lower())
            if key and key not in names:
                names.append(key)
        if not names:
            names = list(DEFAULT_PROVIDERS)

    for name in names:
        fn = globals().get(_SEARCH_FUNCS.get(name, ""))
        if not callable(fn):
            continue
        try:
            hits = fn(q, top_k=k, timeout=timeout) or []
        except Exception:
            continue
        good = [h for h in hits if _relevant_hit(q, h)]
        if good:
            return _dedupe_cap(good, k, name)
    return []


def fetch_page_text(url: str, timeout: int = 12, max_chars: int = 4000) -> str:
    """抓取 http/https 网页正文；失败或非法 URL 返回 ""。"""
    try:
        parts = urllib.parse.urlsplit(url or "")
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        html_text = _fetch(url, timeout)
        if not html_text:
            return ""
        text = html_to_text(html_text)
        limit = int(max_chars)
        if limit > 0 and len(text) > limit:
            text = text[:limit]
        return text
    except Exception:
        return ""


def _official_rank(url: str) -> int:
    """官方域名优先级：数值越小越靠前，非官方为 len(OFFICIAL_DOMAINS)。"""
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower()
    except Exception:
        return len(OFFICIAL_DOMAINS)
    if not host:
        return len(OFFICIAL_DOMAINS)
    for port in (":80", ":443"):
        if host.endswith(port):
            host = host[: -len(port)]
    if host.startswith("www."):
        host = host[4:]
    for i, dom in enumerate(OFFICIAL_DOMAINS):
        if host == dom or host.endswith("." + dom):
            return i
    return len(OFFICIAL_DOMAINS)


def rank_results(results: list[dict]) -> list[dict]:
    """官方源优先排序（稳定），不改任何字段。"""
    if not results:
        return []
    return sorted(results, key=lambda r: _official_rank(r.get("url") or ""))
