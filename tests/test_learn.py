"""learn.py 单元测试（完全不联网：临时 sqlite + 假 llm）。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import db, learn  # noqa: E402
from lawagent.gate import CITE_RE  # noqa: E402
from lawagent.segment import segment  # noqa: E402

CFG = {
    "llm": {"backend": "local", "cloud_enabled": False, "model": "fake"},
    "gate": {"min_quote_len": 10, "retry_once": True},
    "retrieval": {"top_k": 12, "max_context": 8},
    "paths": {},
}

LABOR_30 = (
    "用人单位应当按照劳动合同约定和国家规定，向劳动者及时足额支付劳动报酬。\n"
    "用人单位拖欠或者未足额支付劳动报酬的，劳动者可以依法向当地人民法院申请支付令，"
    "人民法院应当依法发出支付令。"
)
LABOR_85 = (
    "用人单位有下列情形之一的，由劳动行政部门责令限期支付劳动报酬、加班费或者经济补偿；"
    "劳动报酬低于当地最低工资标准的，应当支付其差额部分；逾期不支付的，"
    "责令用人单位按应付金额百分之五十以上百分之一百以下的标准向劳动者加付赔偿金。"
)
CIVIL_311 = (
    "无处分权人将不动产或者动产转让给受让人的，所有权人有权追回；除法律另有规定外，"
    "符合下列情形的，受让人取得该不动产或者动产的所有权：（一）受让人受让该不动产或者动产时是善意；"
    "（二）以合理的价格转让；（三）转让的不动产或者动产依照法律规定应当登记的已经登记，"
    "不需要登记的已经交付给受让人。"
)
CIVIL_577 = (
    "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
    "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
)
CIVIL_143 = (
    "具备下列条件的民事法律行为有效：（一）行为人具有相应的民事行为能力；"
    "（二）意思表示真实；（三）不违反法律、行政法规的强制性规定，不违背公序良俗。"
)
CIVIL_188 = (
    "向人民法院请求保护民事权利的诉讼时效期间为三年。法律另有规定的，依照其规定。"
    "诉讼时效期间自权利人知道或者应当知道权利受到损害以及义务人之日起计算。"
)
CRIM_264 = (
    "盗窃公私财物，数额较大的，或者多次盗窃、入户盗窃、携带凶器盗窃、扒窃的，"
    "处三年以下有期徒刑、拘役或者管制，并处或者单处罚金。"
)
CRIM_20 = (
    "为了使国家、公共利益、本人或者他人的人身、财产和其他权利免受正在进行的不法侵害，"
    "而采取的制止不法侵害的行为，对不法侵害人造成损害的，属于正当防卫，不负刑事责任。"
)

LAWS = [
    (1, "中华人民共和国劳动合同法", "现行有效"),
    (2, "中华人民共和国民法典", "现行有效"),
    (3, "中华人民共和国刑法", "现行有效"),
]

ARTICLES = [
    (1, 1, "第三十条", LABOR_30),
    (2, 1, "第八十五条", LABOR_85),
    (3, 2, "第三百一十一条", CIVIL_311),
    (4, 2, "第五百七十七条", CIVIL_577),
    (5, 2, "第一百四十三条", CIVIL_143),
    (6, 2, "第一百八十八条", CIVIL_188),
    (7, 3, "第二百六十四条", CRIM_264),
    (8, 3, "第二十条", CRIM_20),
]


def make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    db.init(conn)
    for law_id, name, status in LAWS:
        conn.execute(
            "INSERT INTO laws(law_id, law_name, status) VALUES (?,?,?)", (law_id, name, status)
        )
    for art_id, law_id, no, content in ARTICLES:
        conn.execute(
            "INSERT INTO articles(article_id, law_id, article_no, seq_main, content) "
            "VALUES (?,?,?,?,?)",
            (art_id, law_id, no, int(art_id), content),
        )
        name = next(n for i, n, _ in LAWS if i == law_id)
        conn.execute(
            "INSERT INTO articles_fts(rowid, search_text) VALUES (?,?)",
            (art_id, "%s %s %s" % (name, no, segment(content))),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------- 假模型输出

QUIZ_JSON = json.dumps(
    [
        {
            "question": "用人单位拖欠劳动报酬，劳动者可以向法院申请什么？",
            "options": ["支付令", "搜查令", "传票", "限制出境"],
            "answer": "A",
            "article_ref": "《中华人民共和国劳动合同法》第三十条",
            "quote": "用人单位拖欠或者未足额支付劳动报酬的，劳动者可以依法向当地人民法院申请支付令",
            "explanation": "该条直接规定劳动者可申请支付令，法院应当依法发出。",
        },
        {
            "question": "用人单位逾期不支付劳动报酬的，应按应付金额的什么标准加付赔偿金？",
            "options": ["百分之十以上百分之五十以下", "百分之五十以上百分之一百以下",
                        "百分之一百以上百分之二百以下", "不予加付"],
            "answer": "B",
            "article_ref": "《中华人民共和国劳动合同法》第八十五条",
            "quote": "责令用人单位按应付金额百分之五十以上百分之一百以下的标准向劳动者加付赔偿金",
            "explanation": "第八十五条规定了逾期不支付的加付赔偿金比例。",
        },
    ],
    ensure_ascii=False,
)

QUIZ_JSON_BAD = json.dumps(
    [
        {
            "question": "用人单位拖欠工资时应当承担什么责任？",
            "options": ["A 罚款", "B 支付令", "C 拘留", "D 吊销执照"],
            "answer": "B",
            "article_ref": "《中华人民共和国劳动合同法》第九十九条",
            "quote": "用人单位拖欠工资的应当支付赔偿金",
            "explanation": "编造的条文，应当被门禁拦下。",
        }
    ],
    ensure_ascii=False,
)

EXPLAIN_OK = (
    "一、定义\n"
    "善意取得是指无处分权人处分他人财产，受让人在符合法定条件时取得该财产所有权的制度。\n\n"
    "二、条文依据\n"
    "《中华人民共和国民法典》第三百一十一条：“无处分权人将不动产或者动产转让给受让人的，"
    "所有权人有权追回；除法律另有规定外，符合下列情形的，受让人取得该不动产或者动产的所有权”\n\n"
    "三、例子\n"
    "甲将借来的相机以合理价格卖给不知情的乙并完成交付，乙依该条取得所有权。"
)

EXPLAIN_BAD_QUOTE = (
    "一、定义\n善意取得是买受人善意取得所有权。\n\n"
    "二、条文依据\n《中华人民共和国民法典》第三百一十一条：“无权处分的东西，失主不能随便要回。”\n\n"
    "三、例子\n甲卖乙的相机。"
)

CASE_CIVIL_OK = (
    "第1步：事实与争点整理\n甲与乙订立买卖合同并已交付货物，乙未支付价款，争点为甲可否请求付款。\n\n"
    "第2步：寻找请求权基础\n可考虑合同上的价款请求权与违约损害赔偿请求权，依据："
    "《中华人民共和国民法典》第五百七十七条：“当事人一方不履行合同义务或者履行合同义务不符合约定的，"
    "应当承担继续履行、采取补救措施或者赔偿损失等违约责任”\n\n"
    "第3步：构成要件检视\n合同成立并生效，乙未履行付款义务。\n\n"
    "第4步：抗辩与抗辩权\n未见同时履行抗辩、抵销或诉讼时效届满。\n\n"
    "第5步：结论\n甲可请求乙支付价款并承担违约责任。"
)

CASE_CRIM_OK = (
    "第1步：事实与罪名候选\n甲以非法占有为目的，秘密窃取他人财物，可能构成盗窃罪。\n\n"
    "第2步：构成要件该当性\n依据：《中华人民共和国刑法》第二百六十四条：“盗窃公私财物，数额较大的，"
    "或者多次盗窃、入户盗窃、携带凶器盗窃、扒窃的”\n\n"
    "第3步：违法性\n未见正当防卫等违法阻却事由，依据：《中华人民共和国刑法》第二十条："
    "“为了使国家、公共利益、本人或者他人的人身、财产和其他权利免受正在进行的不法侵害，"
    "而采取的制止不法侵害的行为，对不法侵害人造成损害的，属于正当防卫，不负刑事责任”\n\n"
    "第4步：有责性\n甲具有完全刑事责任能力，具备违法性认识可能性。\n\n"
    "第5步：结论\n甲构成盗窃罪。"
)

GRADE_OK = (
    "正确答案是A。依据《中华人民共和国劳动合同法》第三十条："
    "“用人单位拖欠或者未足额支付劳动报酬的，劳动者可以依法向当地人民法院申请支付令”。"
)


class LearnTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = make_db(self.db_path)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            self.tmp.cleanup()

    def patch_chat(self, *outputs):
        """按顺序返回固定文本；单个 Exception 实例表示抛错（模拟模型不可用）。"""
        seq = []
        for o in outputs:
            seq.append(o)
        return mock.patch("lawagent.llm.chat", side_effect=seq)

    def assert_shape(self, res):
        for key in ("answer", "citations", "gate"):
            self.assertIn(key, res, "缺少键 %s" % key)
        self.assertIn("pass", res["gate"])
        self.assertIn("citations", res["gate"])
        self.assertIn("errors", res["gate"])


# ---------------------------------------------------------------- 概念解释


class ExplainTest(LearnTestBase):
    def test_three_sections_and_gate_pass(self):
        with self.patch_chat(EXPLAIN_OK):
            res = learn.explain("善意取得", cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertEqual(res["sections"]["definition"][:4], "善意取得")
        self.assertIn("第三百一十一条", res["sections"]["basis"])
        self.assertTrue(res["sections"]["example"])
        self.assertEqual(len(res["citations"]), 1)
        self.assertTrue(res["llm"]["used"])

    def test_rewrites_once_when_gate_fails(self):
        with mock.patch("lawagent.llm.chat", side_effect=[EXPLAIN_BAD_QUOTE, EXPLAIN_OK]) as m:
            res = learn.explain("善意取得", cfg=CFG, conn=self.conn)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertIn("无处分权人", res["answer"])

    def test_fallback_when_model_unavailable(self):
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("no model")):
            res = learn.explain("善意取得", cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertFalse(res["llm"]["used"])
        self.assertIn("一、定义", res["answer"])
        self.assertIn("二、条文依据", res["answer"])
        self.assertIn("三、例子", res["answer"])
        # 兜底引文来自库内原文，仍然要过门禁
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])

    def test_fallback_quote_crossing_newlines(self):
        """库内条文带换行时，兜底引文仍要能被门禁逐字核对。"""
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("no model")):
            res = learn.explain("拖欠劳动报酬", cfg=CFG, conn=self.conn)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertTrue(any("第三十条" in c["article_no"] for c in res["citations"]))
        self.assertTrue(all(c["quote_ok"] for c in res["citations"]))

    def test_no_basis(self):
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("must not call")) as m:
            res = learn.explain("超导磁悬浮列车", cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(m.call_count, 0)
        self.assertIn("未找到直接依据", res["answer"])
        self.assertEqual(res["retrieved"], [])


# ---------------------------------------------------------------- 案例分析


class AnalyzeCaseTest(LearnTestBase):
    def test_civil_uses_claim_basis_method(self):
        facts = "甲与乙签订买卖合同并交付货物，乙未支付价款，甲请求乙承担违约责任。"
        with self.patch_chat(CASE_CIVIL_OK):
            res = learn.analyze_case(facts, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(res["discipline"], "civil")
        self.assertIn("请求权基础检视", res["method"])
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertGreaterEqual(len(res["steps"]), 5)
        self.assertEqual(res["steps"][0]["step"], 1)
        self.assertIn("第五百七十七条", res["answer"])

    def test_criminal_uses_stufen_method(self):
        facts = "甲故意秘密窃取他人财物，数额较大，是否构成盗窃罪、应否承担刑事责任？"
        with self.patch_chat(CASE_CRIM_OK):
            res = learn.analyze_case(facts, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(res["discipline"], "criminal")
        self.assertIn("阶层检验", res["method"])
        self.assertIn("构成要件该当性", res["method"])
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertEqual(len(res["steps"]), 5)
        self.assertIn("第二百六十四条", res["answer"])

    def test_fallback_builds_steps_from_template(self):
        facts = "甲故意秘密窃取他人财物，数额较大，是否构成盗窃罪？"
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("no model")):
            res = learn.analyze_case(facts, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(len(res["steps"]), 5)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertIn("阶层", res["answer"])

    def test_no_results(self):
        res = learn.analyze_case("超导磁悬浮列车晚点，乘客要求退票。", cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertIn("未找到直接依据", res["answer"])
        self.assertEqual(res["discipline"], "civil")


# ---------------------------------------------------------------- 出题 / 批改 / 错题本


class QuizTest(LearnTestBase):
    def rows(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def test_create_writes_items(self):
        with self.patch_chat(QUIZ_JSON):
            res = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertTrue(all(it["id"] for it in res["items"]))
        rows = self.rows("SELECT * FROM quiz_items ORDER BY id")
        self.assertEqual(len(rows), 2)
        for row in rows:
            opts = json.loads(row["options_json"])
            self.assertEqual(len(opts), 4)
            self.assertEqual([o["label"] for o in opts], ["A", "B", "C", "D"])
            self.assertIn(row["answer"], "ABCD")
            self.assertRegex(row["article_ref"], CITE_RE)
            self.assertTrue(row["created_at"])
        self.assertEqual(rows[0]["topic"], "拖欠劳动报酬")

    def test_invalid_item_rejected_then_repaired(self):
        with mock.patch("lawagent.llm.chat", side_effect=[QUIZ_JSON_BAD, QUIZ_JSON]) as m:
            res = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["gate"]["pass"])
        # 编造的第九十九条没有入库
        self.assertFalse(self.rows("SELECT * FROM quiz_items WHERE article_ref LIKE '%第九十九条%'"))
        self.assertTrue(res["rejected"])

    def test_fallback_when_model_unavailable(self):
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("no model")):
            res = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertGreaterEqual(res["count"], 1)
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertEqual(len(self.rows("SELECT * FROM quiz_items")), res["count"])

    def test_grade_wrong_records_wrong_answer(self):
        with self.patch_chat(QUIZ_JSON):
            created = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        item = created["items"][0]
        wrong = "C" if item["answer"] != "C" else "D"
        with self.patch_chat(GRADE_OK):
            res = learn.quiz_grade(item["id"], wrong, cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertFalse(res["correct"])
        self.assertTrue(res["recorded"])
        self.assertEqual(res["correct_answer"], item["answer"])
        self.assertIn("第三十条", res["answer"])
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        rows = self.rows("SELECT * FROM wrong_answers")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], item["id"])
        self.assertEqual(rows[0]["user_answer"], wrong)
        self.assertTrue(rows[0]["created_at"])

    def test_grade_correct_does_not_record(self):
        with self.patch_chat(QUIZ_JSON):
            created = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        item = created["items"][1]
        with self.patch_chat(GRADE_OK):
            res = learn.quiz_grade(item["id"], item["answer"], cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertTrue(res["correct"])
        self.assertFalse(res["recorded"])
        self.assertFalse(self.rows("SELECT * FROM wrong_answers"))

    def test_grade_accepts_option_text(self):
        with self.patch_chat(QUIZ_JSON):
            created = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        item = created["items"][0]
        text = [o for o in item["options"] if o["label"] == item["answer"]][0]["text"]
        with mock.patch("lawagent.llm.chat", side_effect=RuntimeError("no model")):
            res = learn.quiz_grade(item["id"], text, cfg=CFG, conn=self.conn)
        self.assertTrue(res["correct"])
        # 兜底讲解也必须带依据条文，并且门禁通过
        self.assertTrue(res["gate"]["pass"], res["gate"]["errors"])
        self.assertIn("第三十条", res["answer"])

    def test_grade_missing_item(self):
        res = learn.quiz_grade(9999, "A", cfg=CFG, conn=self.conn)
        self.assert_shape(res)
        self.assertFalse(res["correct"])
        self.assertEqual(res["error"], "题目不存在")

    def test_wrongbook_lists_entries(self):
        with self.patch_chat(QUIZ_JSON):
            created = learn.quiz_create("拖欠劳动报酬", n=2, cfg=CFG, conn=self.conn)
        item = created["items"][0]
        wrong = "C" if item["answer"] != "C" else "D"
        with self.patch_chat(GRADE_OK):
            learn.quiz_grade(item["id"], wrong, cfg=CFG, conn=self.conn)
        res = learn.wrongbook(conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(res["count"], 1)
        entry = res["items"][0]
        self.assertEqual(entry["question"], item["question"])
        self.assertEqual(entry["user_answer"], wrong)
        self.assertEqual(entry["correct_answer"], item["answer"])
        self.assertEqual(entry["article_ref"], item["article_ref"])
        self.assertTrue(entry["created_at"])

    def test_wrongbook_empty(self):
        res = learn.wrongbook(conn=self.conn)
        self.assert_shape(res)
        self.assertEqual(res["count"], 0)
        self.assertIn("为空", res["answer"])


if __name__ == "__main__":
    unittest.main()
