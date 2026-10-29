"""路由单元测试：python -m unittest discover -s tests -v"""
import ast
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent.router import (  # noqa: E402
    CASE,
    CHAT,
    CONCEPT,
    HOT,
    INTENTS,
    NORMATIVE,
    assess_confidence,
    classify_intent,
    decide,
)

CFG_FULL = {
    "routing": {"min_score": 20, "long_query_chars": 120, "force": ""},
    "web": {"enabled": True},
    "llm": {"cloud_enabled": True},
}

GOOD = [
    {"article_id": 1, "article_no": "第三十条", "score": 31.5,
     "law_name": "中华人民共和国劳动合同法", "content": "用人单位应当……", "source": "fts"},
    {"article_id": 2, "article_no": "第八十五条", "score": 22.0,
     "law_name": "中华人民共和国劳动合同法", "content": "用人单位有下列情形……", "source": "fts"},
]

WEAK = [
    {"article_id": 3, "article_no": "第一条", "score": 4.2,
     "law_name": "中华人民共和国劳动法", "content": "为了保护劳动者的合法权益……", "source": "fts"},
]


def cfg(**over):
    """基于完整配置做浅层覆盖，便于单条规则测试。"""
    base = copy.deepcopy(CFG_FULL)
    for dotted, value in over.items():
        section, key = dotted.split("__")
        base[section][key] = value
    return base


class ClassifyIntentTest(unittest.TestCase):
    def test_hot(self):
        for q in ("最近有什么热点案件", "今年刚出的司法解释", "昨天宣判的那起案件",
                  "这个事件网友争议很大"):
            self.assertEqual(classify_intent(q), HOT, q)

    def test_normative(self):
        for q in ("用人单位拖欠工资是否合法", "第三十条怎么规定的",
                  "第十二条的内容", "盗窃罪判几年", "公司违法解除要赔偿吗",
                  "试用期最长多久，有没有有效期限制"):
            self.assertEqual(classify_intent(q), NORMATIVE, q)

    def test_concept(self):
        for q in ("不当得利是什么意思", "善意取得的定义", "定金和订金有什么区别",
                  "表见代理怎么理解"):
            self.assertEqual(classify_intent(q), CONCEPT, q)

    def test_case(self):
        for q in ("本案的请求权基础如何确定", "请用鉴定式方法分析这个案情",
                  "这个行为如何定性", "这种情况法院怎么判"):
            self.assertEqual(classify_intent(q), CASE, q)

    def test_chat(self):
        for q in ("你好", "帮我写一首关于秋天的诗", "谢谢", ""):
            self.assertEqual(classify_intent(q), CHAT, repr(q))

    def test_none_like_input(self):
        self.assertEqual(classify_intent(None), CHAT)
        self.assertEqual(classify_intent("   "), CHAT)

    def test_priority_hot_beats_concept(self):
        self.assertEqual(classify_intent("最近的判决结果是什么"), HOT)

    def test_priority_normative_beats_concept(self):
        self.assertEqual(classify_intent("第三十条的含义怎么理解"), NORMATIVE)

    def test_priority_concept_beats_case(self):
        self.assertEqual(classify_intent("请求权这个概念怎么理解"), CONCEPT)

    def test_priority_case_falls_back_last(self):
        self.assertEqual(classify_intent("分析一下这个案情"), CASE)

    def test_article_number_forms(self):
        for q in ("第3条", "第一百二十条", "第〇条"):
            self.assertEqual(classify_intent(q), NORMATIVE, q)

    def test_result_is_known_intent(self):
        for q in ("热点", "第三十条", "是什么", "案情", "你好"):
            self.assertIn(classify_intent(q), INTENTS)


class AssessConfidenceTest(unittest.TestCase):
    def test_empty_results(self):
        rep = assess_confidence([], 20)
        self.assertEqual(rep, {"top_score": 0.0, "count": 0, "confident": False})
        self.assertIsInstance(rep["top_score"], float)
        self.assertIsInstance(rep["count"], int)

    def test_high_confidence(self):
        rep = assess_confidence(GOOD, 20)
        self.assertEqual(rep["count"], 2)
        self.assertAlmostEqual(rep["top_score"], 31.5)
        self.assertTrue(rep["confident"])

    def test_low_confidence(self):
        rep = assess_confidence(WEAK, 20)
        self.assertEqual(rep["count"], 1)
        self.assertAlmostEqual(rep["top_score"], 4.2)
        self.assertFalse(rep["confident"])

    def test_boundary_equal_is_confident(self):
        self.assertTrue(assess_confidence([{"score": 20.0}], 20)["confident"])
        self.assertFalse(assess_confidence([{"score": 19.9}], 20)["confident"])

    def test_scores_as_strings_and_missing(self):
        rep = assess_confidence([{"score": "18.5"}, {"article_id": 9}], 10)
        self.assertEqual(rep["count"], 2)
        self.assertAlmostEqual(rep["top_score"], 18.5)
        self.assertTrue(rep["confident"])

    def test_negative_and_none_scores(self):
        rep = assess_confidence([{"score": None}, {"score": -3}], 1)
        self.assertAlmostEqual(rep["top_score"], 0.0)
        self.assertFalse(rep["confident"])
        # 负数与缺省不应把 top_score 拉成负值
        self.assertGreaterEqual(rep["top_score"], 0.0)

    def test_bad_score_type_does_not_raise(self):
        rep = assess_confidence([{"score": object()}], 0)
        self.assertAlmostEqual(rep["top_score"], 0.0)

    def test_top_score_is_max_not_first(self):
        rep = assess_confidence([{"score": 3}, {"score": 27}], 20)
        self.assertAlmostEqual(rep["top_score"], 27.0)
        self.assertTrue(rep["confident"])


class DecideTest(unittest.TestCase):
    def test_normal_query_local_without_web(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, CFG_FULL)
        self.assertEqual(rep["intent"], NORMATIVE)
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "local")
        self.assertTrue(any("默认" in r for r in rep["reasons"]))

    def test_reason_mentions_article_number(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, CFG_FULL)
        self.assertTrue(any("第三十条" in r for r in rep["reasons"]), rep["reasons"])

    def test_hot_goes_web_and_cloud(self):
        rep = decide("最近这起劳动争议的热点判决结果", GOOD, CFG_FULL)
        self.assertEqual(rep["intent"], HOT)
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")

    def test_hot_without_web_keeps_cloud(self):
        rep = decide("最近的热点案件", GOOD, cfg(web__enabled=False))
        self.assertEqual(rep["intent"], HOT)
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")

    def test_hot_without_cloud_falls_back_local(self):
        rep = decide("最近的热点案件", GOOD, cfg(llm__cloud_enabled=False))
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "local")

    def test_low_confidence_adds_web_and_cloud(self):
        rep = decide("这个行为如何定性", WEAK, CFG_FULL)
        self.assertEqual(rep["intent"], CASE)
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")

    def test_low_confidence_with_web_disabled_still_uses_cloud(self):
        rep = decide("这个行为如何定性", WEAK, cfg(web__enabled=False))
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")
        self.assertTrue(any("web.enabled=false" in r for r in rep["reasons"]))

    def test_empty_results_low_confidence(self):
        rep = decide("不当得利是什么意思", [], CFG_FULL)
        self.assertFalse(assess_confidence([], 20)["confident"])
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")

    def test_threshold_from_cfg(self):
        loose = decide("不当得利是什么意思", WEAK, cfg(routing__min_score=1))
        self.assertFalse(loose["use_web"])
        self.assertEqual(loose["backend"], "local")

    def test_long_query_goes_cloud_without_web(self):
        q = "劳动合同" * 31  # 124 字 > 120
        self.assertGreater(len(q), 120)
        rep = decide(q, GOOD, CFG_FULL)
        self.assertEqual(rep["backend"], "cloud")
        self.assertFalse(rep["use_web"])

    def test_long_query_boundary(self):
        q = "劳动合同" * 30  # 恰好 120 字，不算长问题
        self.assertEqual(len(q), 120)
        rep = decide(q, GOOD, CFG_FULL)
        self.assertEqual(rep["backend"], "local")
        self.assertFalse(rep["use_web"])

    def test_long_query_without_cloud_stays_local(self):
        q = "劳动合同" * 31
        rep = decide(q, GOOD, cfg(llm__cloud_enabled=False))
        self.assertEqual(rep["backend"], "local")
        self.assertFalse(rep["use_web"])

    def test_force_web_overrides_disabled_flag(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, cfg(routing__force="web", web__enabled=False))
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")
        self.assertTrue(any("force=web" in r for r in rep["reasons"]))

    def test_force_cloud_on_confident_query(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, cfg(routing__force="cloud"))
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")
        self.assertTrue(any("force=cloud" in r for r in rep["reasons"]))

    def test_force_cloud_without_cloud_falls_back_local(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD,
                     cfg(routing__force="cloud", llm__cloud_enabled=False))
        self.assertEqual(rep["backend"], "local")
        self.assertTrue(any("回落本地" in r for r in rep["reasons"]))

    def test_force_local_beats_hot(self):
        rep = decide("最近的热点案件", GOOD, cfg(routing__force="local"))
        self.assertEqual(rep["intent"], HOT)
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "local")
        self.assertTrue(any("force=local" in r for r in rep["reasons"]))
        self.assertFalse(any("云端模型" in r for r in rep["reasons"]), rep["reasons"])

    def test_force_local_beats_low_confidence(self):
        rep = decide("这个行为如何定性", WEAK, cfg(routing__force="local"))
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "local")

    def test_invalid_force_ignored(self):
        for bad in ("", "auto", None, "net"):
            rep = decide("劳动合同法第三十条是什么规定", GOOD,
                         cfg(routing__force=bad, web__enabled=False))
            self.assertFalse(rep["use_web"], bad)
            self.assertEqual(rep["backend"], "local", bad)

    def test_force_web_trimmed_and_case_insensitive(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, cfg(routing__force="  WeB  "))
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")

    def test_empty_cfg_is_safe(self):
        rep = decide("劳动合同法第三十条是什么规定", GOOD, {})
        self.assertEqual(rep["intent"], NORMATIVE)
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "local")

    def test_chat_does_not_escalate(self):
        rep = decide("你好呀", [], CFG_FULL)
        self.assertEqual(rep["intent"], CHAT)
        self.assertFalse(rep["use_web"])
        self.assertEqual(rep["backend"], "local")

    def test_shape_and_reason_types(self):
        rep = decide("最近的热点案件", GOOD, CFG_FULL)
        self.assertEqual(set(rep), {"intent", "use_web", "backend", "reasons"})
        self.assertIn(rep["backend"], ("local", "cloud"))
        self.assertIn(rep["intent"], INTENTS)
        self.assertIsInstance(rep["use_web"], bool)
        self.assertTrue(rep["reasons"])
        for r in rep["reasons"]:
            self.assertIsInstance(r, str)
            self.assertTrue(r.strip())

    def test_decision_is_pure_and_deterministic(self):
        cfg_copy = copy.deepcopy(CFG_FULL)
        res_copy = copy.deepcopy(GOOD)
        first = decide("最近的热点案件", GOOD, CFG_FULL)
        second = decide("最近的热点案件", GOOD, CFG_FULL)
        self.assertEqual(first, second)
        self.assertEqual(CFG_FULL, cfg_copy)
        self.assertEqual(GOOD, res_copy)

    def test_malformed_results_do_not_raise(self):
        rep = decide("这个行为如何定性", [{"score": None}, {}], CFG_FULL)
        self.assertTrue(rep["use_web"])
        self.assertEqual(rep["backend"], "cloud")


class NoSideEffectImportTest(unittest.TestCase):
    """router 必须保持纯规则：只 import 标准库，不依赖第三方或网络库。"""

    def test_only_stdlib_imports(self):
        src = (ROOT / "src" / "lawagent" / "router.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        self.assertTrue(modules.issubset({"__future__", "re"}), modules)


if __name__ == "__main__":
    unittest.main()
