"""门禁单元测试：python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent.gate import verify  # noqa: E402

RETRIEVED = [
    {
        "law_name": "中华人民共和国劳动合同法",
        "article_no": "第三十条",
        "content": "用人单位应当按照劳动合同约定和国家规定，向劳动者及时足额支付劳动报酬。用人单位拖欠或者未足额支付劳动报酬的，劳动者可以依法向当地人民法院申请支付令，人民法院应当依法发出支付令。",
    }
]


class GateTest(unittest.TestCase):
    def test_pass_with_verbatim_quote(self):
        answer = (
            "《中华人民共和国劳动合同法》第三十条：“用人单位拖欠或者未足额支付劳动报酬的，"
            "劳动者可以依法向当地人民法院申请支付令，人民法院应当依法发出支付令。”"
        )
        rep = verify(answer, RETRIEVED)
        self.assertTrue(rep["pass"], rep["errors"])
        self.assertEqual(len(rep["citations"]), 1)
        self.assertTrue(rep["citations"][0]["quote_ok"])

    def test_fail_when_article_not_found(self):
        answer = "《中华人民共和国劳动合同法》第九十九条：“用人单位应当支付工资。”"
        rep = verify(answer, RETRIEVED)
        self.assertFalse(rep["pass"])
        self.assertTrue(any("不在库内" in e for e in rep["errors"]))

    def test_fail_when_quote_paraphrased(self):
        answer = "《中华人民共和国劳动合同法》第三十条：“公司欠薪的话，员工可以去法院要钱。”"
        rep = verify(answer, RETRIEVED)
        self.assertFalse(rep["pass"])
        self.assertTrue(any("无法逐字核对" in e for e in rep["errors"]))

    def test_fail_when_no_citation(self):
        rep = verify("拖欠工资可以申请仲裁。", RETRIEVED)
        self.assertFalse(rep["pass"])

    def test_law_name_short_form(self):
        answer = "《劳动合同法》第三十条：“用人单位拖欠或者未足额支付劳动报酬的，劳动者可以依法向当地人民法院申请支付令。”"
        rep = verify(answer, RETRIEVED)
        self.assertTrue(rep["pass"], rep["errors"])


if __name__ == "__main__":
    unittest.main()
