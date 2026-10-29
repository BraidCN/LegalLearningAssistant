# -*- coding: utf-8 -*-
"""领域词 + 时效词路由测试：只有两者同时命中才联网。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import router  # noqa: E402

CFG = {
    "routing": {"min_score": 20, "long_query_chars": 120, "force": ""},
    "web": {"enabled": True},
    "llm": {"cloud_enabled": False},
}
HIGH = [{"score": 80.0}]
LOW = []

CASES = [
    # (问题, 检索结果, 期望联网, 期望领域)
    ("最近AI生成内容有什么新规", HIGH, True, "ai"),
    ("今年数据出境有什么新政策", HIGH, True, "data"),
    ("2026年网络安全新规", HIGH, True, "network"),
    ("人脸识别最新规定", HIGH, True, "data"),
    ("电商法最新修订", HIGH, True, "ecommerce"),
    ("平台经济反垄断最近案例", HIGH, True, "ecommerce"),
    ("今年 AI 立法进展", HIGH, True, "ai"),
    ("直播带货虚假宣传怎么处罚", HIGH, False, "ecommerce"),
    ("电商平台二选一违法吗", HIGH, False, "ecommerce"),
    ("人工智能生成的内容有著作权吗", HIGH, False, "ai"),
    ("帮信罪怎么判", HIGH, False, "network"),
    ("最近有哪些新的司法解释", HIGH, True, ""),
    ("AIGC 标识办法", HIGH, False, "ai"),
    ("专利侵权怎么赔偿", HIGH, False, "tech"),
    ("虚拟货币交易合法吗", HIGH, False, "network"),
    ("开源许可证有法律效力吗", HIGH, False, "tech"),
    ("BRAID 是什么", HIGH, False, ""),
    ("人工智能是什么", HIGH, False, "ai"),
    ("最近有什么新闻", HIGH, True, ""),
    ("数据安全法全文", HIGH, False, "data"),
]


class DomainRoutingTest(unittest.TestCase):
    def test_cases(self):
        for q, results, want_web, want_domain in CASES:
            d = router.decide(q, results, CFG)
            self.assertEqual(d["use_web"], want_web, "%s -> use_web=%s" % (q, d["use_web"]))
            self.assertEqual(d["domain"], want_domain, "%s -> domain=%s" % (q, d["domain"]))

    def test_confidence_still_triggers_web(self):
        d = router.decide("公司拖欠工资怎么办", LOW, CFG)
        self.assertTrue(d["use_web"])

    def test_web_disabled_never_fetches(self):
        cfg = dict(CFG, web={"enabled": False})
        d = router.decide("最近AI生成内容有什么新规", HIGH, cfg)
        self.assertFalse(d["use_web"])

    def test_ai_word_boundary(self):
        self.assertFalse(router.domain_hits("BRAID 项目进展"))
        self.assertTrue(router.domain_hits("AI 换脸是否违法"))

    def test_domain_question_is_normative(self):
        self.assertEqual(router.classify_intent("人工智能生成的内容有著作权吗"), router.NORMATIVE)

    def test_reasons_explain_decision(self):
        d = router.decide("最近AI生成内容有什么新规", HIGH, CFG)
        text = " ".join(d["reasons"])
        self.assertIn("领域=AI", text)
        self.assertIn("时效=最近", text)


if __name__ == "__main__":
    unittest.main()
