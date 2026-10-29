"""中文数字转换测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent.cnnum import cn_to_int, int_to_cn  # noqa: E402


class CnNumTest(unittest.TestCase):
    def test_int_to_cn(self):
        self.assertEqual(int_to_cn(1), "一")
        self.assertEqual(int_to_cn(10), "十")
        self.assertEqual(int_to_cn(11), "十一")
        self.assertEqual(int_to_cn(37), "三十七")
        self.assertEqual(int_to_cn(105), "一百零五")
        self.assertEqual(int_to_cn(1260), "一千二百六十")

    def test_cn_to_int(self):
        self.assertEqual(cn_to_int("一"), 1)
        self.assertEqual(cn_to_int("十"), 10)
        self.assertEqual(cn_to_int("三十七"), 37)
        self.assertEqual(cn_to_int("一百零五"), 105)
        self.assertEqual(cn_to_int("1260"), 1260)


if __name__ == "__main__":
    unittest.main()
