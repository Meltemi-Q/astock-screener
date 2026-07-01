"""astock_screener 生产默认路径的单元测试。

覆盖本次审计修复：商誉排雷口径、报告期年份推导、原子写、OCF 合并口径近似。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astock_screener as a


def _base_record(**over):
    """一条各项优秀、默认能过五层的 A 股 record；用 over 覆盖单项做排雷测试。"""
    r = {
        "code": "600000", "name": "测试股", "industry": "白酒", "is_st": False,
        "roe": 30.0, "gross_margin": 91.0, "net_margin": 50.0,
        "net_profit": 8e10, "revenue": 1.6e11,
        "yoy": 22.0, "cagr": 20.0, "g": 20.0,
        "ocf_to_profit": 1.2, "debt_ratio": 18.0, "goodwill_ratio": 0.0,
        "pe_ttm": 18.0, "pe_dyn": 18.0, "pb": 8.0, "mktcap": 8e11,
        "eyield": 100.0 / 18.0, "peg": 18.0 / 20.0,
        "exp_ret": 100.0 / 18.0 + 20.0,
        "reasonable_pe": 20.0, "fair_mktcap": 8e10 * 20.0,
        "discount": 1.0 - 8e11 / (8e10 * 20.0),
        "deduct_ratio": 0.95,
    }
    r.update(over)
    return r


class TestGoodwillMineSweep(unittest.TestCase):
    """商誉率 ≥30%（净资产，百分数口径）必须被第0层排雷。"""

    def test_goodwill_90pct_swept(self):
        r = _base_record(goodwill_ratio=90.0)  # 90%
        ind = a.industry_median_pe([r])
        deepest, tier, fails = a.evaluate(r, ind)
        self.assertTrue(any("商誉" in f for f in fails), f"90% 商誉必须被排雷: {fails}")
        self.assertEqual(deepest, 0)
        self.assertNotIn(tier, ("A_可买入", "B_优质待跌", "C_接近合格"))

    def test_goodwill_below_threshold_passes(self):
        r = _base_record(goodwill_ratio=10.0)  # 10% < 30%
        ind = a.industry_median_pe([r])
        _, _, fails = a.evaluate(r, ind)
        self.assertFalse(any("商誉" in f for f in fails), f"10% 商誉不应被排雷: {fails}")

    def test_goodwill_exactly_30pct_swept(self):
        r = _base_record(goodwill_ratio=30.0)  # 边界：≥30% 排雷
        ind = a.industry_median_pe([r])
        _, _, fails = a.evaluate(r, ind)
        self.assertTrue(any("商誉" in f for f in fails))


class TestReportYear(unittest.TestCase):
    """报告期年份应按当前日期推导，不再硬编码。"""

    def test_default_report_year_not_hardcoded(self):
        from datetime import datetime
        now = datetime.now()
        y = a._default_report_year()
        # 默认上一年；1~4 月再回退一年
        expected = now.year - 1 - (1 if now.month <= 4 else 0)
        self.assertEqual(y, expected)
        self.assertLessEqual(y, now.year - 1)

    def test_config_report_year_resolved(self):
        self.assertIsNotNone(a.CONFIG["report_year"])
        self.assertIsInstance(a.CONFIG["report_year"], int)


class TestAtomicWrite(unittest.TestCase):
    """原子写：tmp + os.replace，不留半截文件。"""

    def test_atomic_write_text(self):
        import tempfile
        d = tempfile.mkdtemp()
        fp = os.path.join(d, "out.txt")
        a._atomic_write(fp, "hello", mode="w", encoding="utf-8")
        with open(fp, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")
        # 无残留 tmp
        self.assertFalse(any(x.startswith("out.txt.tmp") for x in os.listdir(d)))

    def test_atomic_write_overwrite(self):
        import tempfile
        d = tempfile.mkdtemp()
        fp = os.path.join(d, "out.txt")
        a._atomic_write(fp, "v1", mode="w", encoding="utf-8")
        a._atomic_write(fp, "v2", mode="w", encoding="utf-8")
        with open(fp, encoding="utf-8") as f:
            self.assertEqual(f.read(), "v2")


class TestLossMineSweep(unittest.TestCase):
    """亏损/无营收必须被排雷（fail-closed）。"""

    def test_loss_swept(self):
        r = _base_record(net_profit=-1e8)
        ind = a.industry_median_pe([r])
        _, tier, fails = a.evaluate(r, ind)
        self.assertIn("亏损", fails)
        self.assertNotIn(tier, ("A_可买入", "B_优质待跌", "C_接近合格"))

    def test_missing_debt_ratio_swept(self):
        r = _base_record(debt_ratio=None)
        ind = a.industry_median_pe([r])
        _, _, fails = a.evaluate(r, ind)
        self.assertTrue(any("负债率" in f for f in fails))


if __name__ == "__main__":
    unittest.main()
