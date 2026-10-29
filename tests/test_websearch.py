"""联网检索模块单元测试（全离线）：python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import websearch  # noqa: E402
from lawagent.websearch import (  # noqa: E402
    _parse_360_html,
    _parse_bing_html,
    _parse_rss,
    clean_query,
    fetch_page_text,
    html_to_text,
    rank_results,
    search_360,
    search_bing,
    search_multi,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Bing: 民法典 司法解释</title>
    <item>
      <title>最高人民法院关于适用《中华人民共和国民法典》合同编通则若干问题的解释</title>
      <link>https://www.court.gov.cn/zixun/xiangqing/12345.html</link>
      <description>&lt;p&gt;该解释自2023年12月5日起施行，共&lt;b&gt;69条&lt;/b&gt;。&lt;/p&gt;</description>
    </item>
    <item>
      <title>民法典全文（国家法律法规数据库）</title>
      <link>https://flk.npc.gov.cn/detail2.html?ZmY4MDgx</link>
      <description>中华人民共和国民法典，2021年1月1日施行，共1260条。</description>
    </item>
    <item>
      <title>无链接的条目</title>
      <description>只有描述，应该仍被解析出来但 url 为空。</description>
    </item>
  </channel>
</rss>
"""

RSS_NS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>人民检察院民事诉讼监督规则</title>
      <link>https://www.spp.gov.cn/spp/xwfbh/2021.shtml</link>
      <dc:creator>最高人民检察院</dc:creator>
      <description>修订后的规则自2021年8月1日起施行。</description>
    </item>
  </channel>
</rss>
"""

HTML_SAMPLE = """<!DOCTYPE html>
<html><head><title>民法典 司法解释 - 搜索</title>
<style>.b_algo{color:red}</style>
<script>window.__x = "<p>假结果</p>";</script>
</head>
<body>
<ol id="b_results">
  <li class="b_algo b_algoBorder">
    <h2><a href="https://www.court.gov.cn/zixun/xiangqing/12345.html" h="ID=SERP,1">最高法发布民法典合同编通则<span>司法解释</span></a></h2>
    <div class="b_caption"><p class="b_lineclamp4">该解释共69条，自2023年12月5日起施行。</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://flk.npc.gov.cn/detail2.html?ZmY4MDgx">中华人民共和国民法典</a></h2>
    <p>2021年1月1日施行，共1260条。<strong>现行有效</strong></p>
    <p>第二个段落不应覆盖第一条摘要。</p>
  </li>
  <li class="b_ans"><h2><a href="https://example.com/ad">广告</a></h2><p>不是 b_algo</p></li>
  <li class="b_algo">
    <h2><a href="javascript:void(0)">点击展开</a></h2>
    <p>无有效链接。</p>
  </li>
</ol>
</body></html>
"""


class RssParseTest(unittest.TestCase):
    def test_parse_basic(self):
        res = _parse_rss(RSS_SAMPLE)
        self.assertEqual(len(res), 3)
        first = res[0]
        self.assertEqual(first["url"], "https://www.court.gov.cn/zixun/xiangqing/12345.html")
        self.assertIn("合同编通则", first["title"])
        self.assertNotIn("<", first["snippet"])
        self.assertNotIn("&lt;", first["snippet"])
        self.assertIn("69条", first["snippet"])
        self.assertEqual(first["source"], "bing")
        self.assertEqual(set(first), {"title", "url", "snippet", "source"})

    def test_parse_namespaced(self):
        res = _parse_rss(RSS_NS_SAMPLE)
        self.assertEqual(len(res), 1)
        self.assertIn("spp.gov.cn", res[0]["url"])
        self.assertIn("2021年8月1日", res[0]["snippet"])

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(_parse_rss(""), [])
        self.assertEqual(_parse_rss("   "), [])
        self.assertEqual(_parse_rss("not xml at all"), [])
        self.assertEqual(_parse_rss("<rss><channel></channel></rss>"), [])

    def test_description_whitespace_collapsed(self):
        xml = (
            "<rss><channel><item><title> 甲\n 乙 </title>"
            "<link>https://a.gov.cn/x</link>"
            "<description>第一行\n\n   第二行&nbsp;&nbsp;结束</description>"
            "</item></channel></rss>"
        )
        res = _parse_rss(xml)
        self.assertEqual(res[0]["title"], "甲 乙")
        self.assertEqual(res[0]["snippet"], "第一行 第二行 结束")


class BingHtmlParseTest(unittest.TestCase):
    def test_parse_b_algo(self):
        res = _parse_bing_html(HTML_SAMPLE)
        urls = [r["url"] for r in res]
        self.assertEqual(
            urls,
            [
                "https://www.court.gov.cn/zixun/xiangqing/12345.html",
                "https://flk.npc.gov.cn/detail2.html?ZmY4MDgx",
            ],
        )
        self.assertEqual(res[0]["title"], "最高法发布民法典合同编通则司法解释")
        self.assertIn("69条", res[0]["snippet"])
        self.assertEqual(res[0]["source"], "bing")
        # 嵌套的 strong 不丢失，且首个 p 的摘要不被后续 p 覆盖
        self.assertIn("现行有效", res[1]["snippet"])
        self.assertNotIn("第二个段落", res[1]["snippet"])
        # script/style 里的伪结果与广告块不应出现
        self.assertFalse(any("广告" in r["title"] for r in res))

    def test_void_tags_do_not_break_depth(self):
        html = (
            '<li class="b_algo"><h2><a href="https://a.gov.cn/1">标题<br>一行</a></h2>'
            "<p>摘要<br/>续行</p></li>"
            '<li class="b_algo"><h2><a href="https://b.gov.cn/2">第二条</a></h2></li>'
        )
        res = _parse_bing_html(html)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["url"], "https://a.gov.cn/1")
        self.assertEqual(res[1]["url"], "https://b.gov.cn/2")

    def test_parse_empty(self):
        self.assertEqual(_parse_bing_html(""), [])
        self.assertEqual(_parse_bing_html("<html><body>无结果</body></html>"), [])


class HtmlToTextTest(unittest.TestCase):
    def test_strips_script_style(self):
        html = (
            "<html><head><style>body{color:#000}</style>"
            "<script>var a = 1 < 2;</script></head>"
            "<body><noscript>请开启脚本</noscript><h1>标题</h1><p>正文第一段。</p></body></html>"
        )
        text = html_to_text(html)
        self.assertIn("标题", text)
        self.assertIn("正文第一段", text)
        self.assertNotIn("color:#000", text)
        self.assertNotIn("var a", text)
        self.assertNotIn("请开启脚本", text)
        self.assertNotIn("<", text)

    def test_entities_and_whitespace(self):
        html = "<div>甲&nbsp;&nbsp;乙 &amp; 丙\n\n\t 丁　戊</div>"
        self.assertEqual(html_to_text(html), "甲 乙 & 丙 丁 戊")

    def test_empty(self):
        self.assertEqual(html_to_text(""), "")
        self.assertEqual(html_to_text("   "), "")


class RankTest(unittest.TestCase):
    def test_official_domains_first(self):
        items = [
            {"title": "某律所解读", "url": "https://www.lawyerblog.com/a", "snippet": "", "source": "bing"},
            {"title": "法律日报", "url": "https://www.legaldaily.com.cn/b", "snippet": "", "source": "bing"},
            {"title": "最高法", "url": "https://www.court.gov.cn/c", "snippet": "", "source": "bing"},
            {"title": "数据库", "url": "https://flk.npc.gov.cn/d", "snippet": "", "source": "bing"},
            {"title": "门户", "url": "https://www.gov.cn/e", "snippet": "", "source": "bing"},
        ]
        ranked = rank_results(items)
        self.assertEqual(
            [r["url"] for r in ranked],
            [
                "https://flk.npc.gov.cn/d",
                "https://www.court.gov.cn/c",
                "https://www.gov.cn/e",
                "https://www.legaldaily.com.cn/b",
                "https://www.lawyerblog.com/a",
            ],
        )
        # 字段不变、条数不变、原列表未被修改
        self.assertEqual(sorted(ranked[0]), ["snippet", "source", "title", "url"])
        self.assertEqual(len(ranked), 5)
        self.assertEqual(items[0]["url"], "https://www.lawyerblog.com/a")

    def test_stable_within_same_tier(self):
        items = [
            {"title": "a", "url": "https://www.court.gov.cn/1", "snippet": "", "source": "bing"},
            {"title": "b", "url": "https://www.spp.gov.cn/2", "snippet": "", "source": "bing"},
            {"title": "c", "url": "https://www.court.gov.cn/3", "snippet": "", "source": "bing"},
        ]
        ranked = rank_results(items)
        self.assertEqual([r["title"] for r in ranked], ["a", "c", "b"])

    def test_non_official_and_bad_urls(self):
        items = [
            {"title": "x", "url": "", "snippet": "", "source": "bing"},
            {"title": "y", "url": "https://www.moj.gov.cn/z", "snippet": "", "source": "bing"},
        ]
        self.assertEqual([r["title"] for r in rank_results(items)], ["y", "x"])
        self.assertEqual(rank_results([]), [])


class FetchTest(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        for bad in ("ftp://example.com/a", "file:///C:/Windows/win.ini", "javascript:alert(1)", "", "example.com"):
            self.assertEqual(fetch_page_text(bad), "")

    def test_extracts_and_truncates(self):
        body = "<html><body><p>%s</p></body></html>" % ("法" * 100)
        with mock.patch.object(websearch, "_fetch", return_value=body):
            self.assertEqual(fetch_page_text("http://example.com/a", max_chars=10), "法" * 10)
            self.assertEqual(len(fetch_page_text("https://example.com/a", max_chars=4000)), 100)

    def test_fetch_error_returns_empty(self):
        with mock.patch.object(websearch, "_fetch", side_effect=OSError("boom")):
            self.assertEqual(fetch_page_text("https://example.com/a"), "")

    def test_ssl_context_keeps_verification_on(self):
        import ssl

        ctx = websearch._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreater(ctx.cert_store_stats().get("x509", 0), 0)


class SearchBingTest(unittest.TestCase):
    def test_empty_query_no_network(self):
        with mock.patch.object(websearch, "_fetch", side_effect=AssertionError("不应联网")):
            self.assertEqual(search_bing("", 5), [])
            self.assertEqual(search_bing("   ", 5), [])
            self.assertEqual(search_bing("民法典", 0), [])

    def test_rss_path_and_cap(self):
        with mock.patch.object(websearch, "_fetch", return_value=RSS_SAMPLE) as m:
            res = search_bing("民法典 司法解释", 2)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["source"], "bing")
        self.assertEqual(m.call_count, 1)
        self.assertIn("format=rss", m.call_args[0][0])
        self.assertIn("setlang=zh-CN", m.call_args[0][0])

    def test_html_fallback_when_rss_empty(self):
        def fake(url, timeout=12):
            if "format=rss" in url:
                return ""
            return HTML_SAMPLE

        with mock.patch.object(websearch, "_fetch", side_effect=fake):
            res = search_bing("民法典 司法解释", 5)
        self.assertEqual(len(res), 2)
        self.assertIn("court.gov.cn", res[0]["url"])
        self.assertEqual(res[0]["source"], "bing")

    def test_all_failures_return_empty(self):
        with mock.patch.object(websearch, "_fetch", side_effect=TimeoutError("timeout")):
            self.assertEqual(search_bing("民法典", 3), [])
        with mock.patch.object(websearch, "_fetch", return_value="<html>nope</html>"):
            self.assertEqual(search_bing("民法典", 3), [])


HTML360_SAMPLE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>民法典 司法解释_360搜索</title>
<style>.res-list{color:#000}</style>
<script>var fake = "<li class=\\"res-list\\"><h3><a href=\\"https://evil.example/x\\">假结果</a></h3></li>";</script>
</head><body>
<ul class="result">
  <li class="res-list" data-result-type="normal" data-result-pos="1">
    <h3 class="res-title"><a href="https://www.so.com/link?m=abc%2Bdef" data-mdurl="https://www.court.gov.cn/zixun/xiangqing/12345.html" target="_blank">最高法发布<em>民法典</em>合同编通则<span>司法解释</span></a></h3>
    <div class="res-rich"><span class="g-c-gray">2023年12月5日&nbsp;-&nbsp;</span><span class="res-list-summary" data-result-summary>该解释共<em>69条</em>，自2023年12月5日起施行。</span></div>
  </li>
  <li class="res-list" data-result-type="normal">
    <h3 class="res-title "><a href="https://flk.npc.gov.cn/detail2.html?ZmY4MDgx">中华人民共和国民法典</a></h3>
    <div class="res-rich"><p>2021年1月1日施行，共1260条。</p></div>
  </li>
  <li class="result">
    <h3><a href="https://www.spp.gov.cn/spp/xwfbh/2021.shtml">人民检察院民事诉讼监督规则</a></h3>
    <p class="res-desc">修订后的规则自2021年8月1日起施行。</p>
  </li>
  <li class="res-list">
    <h3 class="res-title"><a href="https://ai.so.com/search/abc123?src=so_result">AI 聚合问答</a></h3>
    <p class="res-desc">不应被当作网页结果。</p>
  </li>
  <li class="res-list">
    <h3 class="res-title"><a href="javascript:void(0)">展开更多</a></h3>
    <p class="res-desc">无有效链接。</p>
  </li>
  <li class="res-list">
    <h3 class="res-title"><a href="/link?m=relative">相对链接结果</a></h3>
    <p class="res-desc">应被跳过。</p>
  </li>
</ul>
</body></html>
"""


class So360ParseTest(unittest.TestCase):
    def test_parse_res_list(self):
        res = _parse_360_html(HTML360_SAMPLE)
        # 只留三条有效结果：ai.so.com / javascript / 相对链接都被跳过
        self.assertEqual(
            [r["url"] for r in res],
            [
                "https://www.court.gov.cn/zixun/xiangqing/12345.html",
                "https://flk.npc.gov.cn/detail2.html?ZmY4MDgx",
                "https://www.spp.gov.cn/spp/xwfbh/2021.shtml",
            ],
        )
        first = res[0]
        self.assertEqual(first["title"], "最高法发布民法典合同编通则司法解释")
        self.assertIn("69条", first["snippet"])
        self.assertNotIn("<", first["snippet"])
        self.assertEqual(first["source"], "360")
        self.assertEqual(set(first), {"title", "url", "snippet", "source"})
        # 跳转链接换成 data-mdurl 的真实地址
        self.assertNotIn("so.com/link", first["url"])
        # 无摘要节点时用整块文本兜底，且不重复标题
        self.assertIn("1260条", res[1]["snippet"])
        self.assertFalse(res[1]["snippet"].startswith("中华人民共和国民法典"))
        # li.result 也能识别，p.res-desc 作为摘要
        self.assertIn("2021年8月1日", res[2]["snippet"])
        self.assertFalse(any("AI 聚合" in r["title"] for r in res))
        self.assertFalse(any("假结果" in r["title"] for r in res))

    def test_parse_empty(self):
        self.assertEqual(_parse_360_html(""), [])
        self.assertEqual(_parse_360_html("   "), [])
        self.assertEqual(_parse_360_html("<html><body>没有结果</body></html>"), [])


class Search360Test(unittest.TestCase):
    def test_empty_query_no_network(self):
        with mock.patch.object(websearch, "_fetch", side_effect=AssertionError("不应联网")):
            self.assertEqual(search_360("", 5), [])
            self.assertEqual(search_360("   ", 5), [])
            self.assertEqual(search_360("民法典", 0), [])

    def test_parses_caps_and_dedupes(self):
        dup = (
            '<ul class="result">'
            '<li class="res-list"><h3><a href="https://a.gov.cn/1">民法典 第一条</a></h3>'
            "<p class=\"res-desc\">摘要一</p></li>"
            '<li class="res-list"><h3><a href="https://a.gov.cn/1">民法典 重复</a></h3>'
            "<p class=\"res-desc\">摘要二</p></li>"
            '<li class="res-list"><h3><a href="https://b.gov.cn/2">民法典 第二条</a></h3>'
            "<p class=\"res-desc\">摘要三</p></li>"
            "</ul>"
        )
        with mock.patch.object(websearch, "_fetch", return_value=dup) as m:
            res = search_360("民法典 司法解释", 2)
        self.assertEqual(len(res), 2)
        self.assertEqual([r["url"] for r in res], ["https://a.gov.cn/1", "https://b.gov.cn/2"])
        self.assertEqual(res[0]["title"], "民法典 第一条")
        self.assertTrue(all(r["source"] == "360" for r in res))
        self.assertEqual(m.call_count, 1)
        url = m.call_args[0][0]
        self.assertIn("www.so.com/s?q=", url)
        self.assertIn("%E6%B0%91%E6%B3%95", url)  # 民法典 的 URL 编码

    def test_failure_returns_empty(self):
        with mock.patch.object(websearch, "_fetch", side_effect=TimeoutError("timeout")):
            self.assertEqual(search_360("民法典", 3), [])
        with mock.patch.object(websearch, "_fetch", return_value=""):
            self.assertEqual(search_360("民法典", 3), [])


def _hit(title, url, snippet="", source="bing"):
    return {"title": title, "url": url, "snippet": snippet, "source": source}


IRRELEVANT = [_hit("今日天气预报", "https://weather.example/1", "多云转晴，气温 18 度")]


class CleanQueryTest(unittest.TestCase):
    def test_removes_noise_words(self):
        self.assertEqual(clean_query("请问民法典的诉讼时效是多久？"), "民法典的诉讼时效是多久")
        self.assertEqual(clean_query("民法典 有哪些 司法解释"), "民法典 司法解释")
        self.assertEqual(clean_query("  请教一下：合同怎么处理  "), "合同")
        self.assertEqual(clean_query(""), "")

    def test_keeps_semantic_words(self):
        # “最新/进展”这类实词不能删
        self.assertEqual(clean_query("民法典 最新进展"), "民法典 最新进展")
        # 去完太短则回退原串
        self.assertEqual(clean_query("怎么办"), "怎么办")


class SearchMultiTest(unittest.TestCase):
    def test_first_relevant_source_wins(self):
        bing_hits = list(IRRELEVANT)
        ddg_hits = [_hit("民法典司法解释全文", "https://www.court.gov.cn/a", "共69条", "ddg")]
        with mock.patch.object(websearch, "search_bing", return_value=bing_hits) as mb, \
                mock.patch.object(websearch, "search_ddg", return_value=ddg_hits) as md, \
                mock.patch.object(websearch, "search_360", side_effect=AssertionError("不应调用")) as m360:
            res = search_multi("民法典 司法解释", 3, providers=["bing", "ddg", "360"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["source"], "ddg")
        self.assertEqual(res[0]["url"], "https://www.court.gov.cn/a")
        self.assertEqual(mb.call_count, 1)
        self.assertEqual(md.call_count, 1)
        self.assertEqual(m360.call_count, 0)

    def test_all_irrelevant_returns_empty(self):
        with mock.patch.object(websearch, "search_bing", return_value=list(IRRELEVANT)) as mb, \
                mock.patch.object(websearch, "search_ddg", return_value=list(IRRELEVANT)) as md, \
                mock.patch.object(websearch, "search_360", return_value=list(IRRELEVANT)) as m360:
            res = search_multi("民法典 司法解释", 3, providers=["bing", "ddg", "360"])
        self.assertEqual(res, [])
        self.assertEqual([mb.call_count, md.call_count, m360.call_count], [1, 1, 1])

    def test_source_error_falls_through(self):
        ddg_hits = [_hit("民法典司法解释", "https://www.spp.gov.cn/b", "", "ddg")]
        with mock.patch.object(websearch, "search_bing", side_effect=OSError("boom")), \
                mock.patch.object(websearch, "search_ddg", return_value=ddg_hits):
            res = search_multi("民法典 司法解释", 3, providers=["bing", "ddg"])
        self.assertEqual([r["source"] for r in res], ["ddg"])

    def test_uses_configured_order(self):
        so_hits = [_hit("民法典司法解释新规", "https://www.court.gov.cn/c", "", "360")]
        with mock.patch.object(websearch, "_load_providers", return_value=["360", "bing"]), \
                mock.patch.object(websearch, "search_360", return_value=so_hits) as m360, \
                mock.patch.object(websearch, "search_bing", side_effect=AssertionError("不应调用")):
            res = search_multi("民法典 司法解释", 2)
        self.assertEqual(res[0]["source"], "360")
        self.assertEqual(m360.call_count, 1)

    def test_query_cleaned_before_search(self):
        hits = [_hit("民法典司法解释", "https://www.court.gov.cn/d", "", "bing")]
        with mock.patch.object(websearch, "search_bing", return_value=hits) as mb:
            search_multi("请问民法典 司法解释有什么规定", 2, providers=["bing"])
        self.assertEqual(mb.call_args[0][0], "民法典 司法解释 规定")

    def test_dedupe_cap_and_source_field(self):
        hits = [
            _hit("民法典司法解释一", "https://a.gov.cn/1", "", "bing"),
            _hit("民法典司法解释一（重复）", "https://a.gov.cn/1", "", "bing"),
            _hit("民法典司法解释二", "https://b.gov.cn/2", "", "bing"),
            _hit("民法典司法解释三", "https://c.gov.cn/3", "", "bing"),
        ]
        with mock.patch.object(websearch, "search_bing", return_value=hits):
            res = search_multi("民法典 司法解释", 2, providers=["bing"])
        self.assertEqual(len(res), 2)
        self.assertEqual([r["url"] for r in res], ["https://a.gov.cn/1", "https://b.gov.cn/2"])
        self.assertTrue(all(r["source"] == "bing" for r in res))
        self.assertEqual(set(res[0]), {"title", "url", "snippet", "source"})

    def test_empty_query_and_zero_k(self):
        with mock.patch.object(websearch, "search_bing", side_effect=AssertionError("不应联网")):
            self.assertEqual(search_multi("", 3), [])
            self.assertEqual(search_multi("   ", 3), [])
            self.assertEqual(search_multi("民法典", 0), [])

    def test_all_sources_raise_returns_empty(self):
        with mock.patch.object(websearch, "search_bing", side_effect=OSError("a")), \
                mock.patch.object(websearch, "search_ddg", side_effect=OSError("b")), \
                mock.patch.object(websearch, "search_360", side_effect=OSError("c")):
            self.assertEqual(search_multi("民法典", 3), [])

    def test_default_providers_from_config(self):
        # 仓库内 configs/config.json 的 web.providers 为 bing/ddg/360
        self.assertEqual(websearch._load_providers(), ["bing", "ddg", "360"])

    def test_min_bigram_hits_is_tunable(self):
        weak = [_hit("民法（法律）_百度百科", "https://baike.baidu.com/item/民法", "民法是法律部门", "bing")]
        with mock.patch.object(websearch, "search_bing", return_value=weak), \
                mock.patch.object(websearch, "search_360", return_value=[]):
            self.assertEqual(len(search_multi("民法典 司法解释", 3, providers=["bing"])), 1)
        with mock.patch.object(websearch, "MIN_BIGRAM_HITS", 2), \
                mock.patch.object(websearch, "search_bing", return_value=weak):
            self.assertEqual(search_multi("民法典 司法解释", 3, providers=["bing"]), [])


if __name__ == "__main__":
    unittest.main()
