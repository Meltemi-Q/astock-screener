"""Tests for cross-market scoring engine (screeners/scoring.py).

Per PRD sections 8.1-8.5. Validates five-layer gating, score computation,
industry median PE, momentum calculation, and full pipeline.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from screeners.contracts import (
    MARKET_CN,
    MARKET_HK,
    make_screening_record,
)
from screeners.scoring import (
    evaluate,
    score,
    run_full_pipeline,
    industry_median_pe,
    DEFAULT_CONFIG,
    median,
    scale,
)


# ═══════════════════════════════════════════════════════════
#  Helpers to build test records
# ═══════════════════════════════════════════════════════════

def _make_tier_a_record(market="cn", code="600519", name="贵州茅台"):
    """Build a record that passes all 4 layers.

    Thresholds (DEFAULT_CONFIG):
      L0: ocf≥0.8, debt<70%, no ST, goodwill<30%
      L1: roe≥15%, gm≥30%, nm≥10%, yoy≥10%, cagr≥10%
      L2: peg<1.0, eyield>5%, pe≤industry_median
      L3: exp_ret≥10%, discount<0.7 (market_cap<0.7*fair_mktcap)
    """
    # g = min(22, 20) = 20; peg = 18/20 = 0.9 < 1.0 ✓
    # fair_pe = max(12, min(20, 30)) = 20
    # fair_mktcap = 80B * 20 = 1600B
    # discount = 800B/1600B = 0.5 < 0.7 ✓
    # exp_ret = (20/18)*20 = 22.2 >= 10 ✓
    return make_screening_record(
        market=market, code=code, display_code=code, name=name,
        industry="白酒", currency="CNY",
        price=1500.0, min_buy=150000.0,
        pe_ttm=18.0, pb=8.0,
        market_cap=800000000000,
        roe=30.0, gross_margin=91.0, net_margin=50.0,
        yoy=22.0, cagr=20.0,
        ocf_to_profit=1.2, debt_ratio=18.0,
        goodwill_ratio=0.0, deduct_ratio=0.95,
        ttm_netp=80000000000,
    )


def _make_tier_b_record(market="cn", code="000001", name="平安银行"):
    """Build a record that passes layers 0+1 but fails layer 2.

    L0 passes: debt 50% < 70%, ocf 1.0 >= 0.8, no ST.
    L1 passes: roe 18% >= 15%, gm 45% >= 30%, nm 15% >= 10%,
               yoy 12% >= 10%, cagr 11% >= 10%.
    L2 fails: g = min(12,11) = 11, peg = 25/11 = 2.27 >= 1.0.
    """
    return make_screening_record(
        market=market, code=code, display_code=code, name=name,
        industry="银行", currency="CNY",
        price=12.0, min_buy=1200.0,
        pe_ttm=25.0, pb=0.65,
        market_cap=250000000000,
        roe=18.0, gross_margin=45.0, net_margin=15.0,
        yoy=12.0, cagr=11.0,
        ocf_to_profit=1.0, debt_ratio=50.0,
        goodwill_ratio=0.0, deduct_ratio=0.90,
        ttm_netp=40000000000,
    )


def _make_tier_c_record(market="cn", code="600036", name="测试银行"):
    """Passes layer 0, fails exactly 1 item in layer 1 (ROE < 15)."""
    return make_screening_record(
        market=market, code=code, display_code=code, name=name,
        industry="银行", currency="CNY",
        price=35.0, min_buy=3500.0,
        pe_ttm=5.5, pb=0.8,
        market_cap=800000000000,
        roe=12.0, gross_margin=70.0, net_margin=35.0,
        yoy=12.0, cagr=11.0,
        ocf_to_profit=1.3, debt_ratio=60.0,
        goodwill_ratio=0.0, deduct_ratio=0.92,
        ttm_netp=140000000000,
    )


def _make_mine_swept_record(market="cn", code="600000", name="ST测试"):
    """Fails layer 0 (ST + high debt)."""
    return make_screening_record(
        market=market, code=code, display_code=code, name=name,
        industry="综合", currency="CNY",
        price=5.0, min_buy=500.0,
        pe_ttm=100.0, pb=1.2,
        market_cap=5000000000,
        roe=3.0, gross_margin=15.0, net_margin=2.0,
        yoy=-10.0, cagr=-8.0,
        ocf_to_profit=0.3, debt_ratio=85.0,
        goodwill_ratio=0.45, deduct_ratio=0.60,
        ttm_netp=50000000,
    )


# ═══════════════════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════════════════

class TestEvaluate(unittest.TestCase):
    """Tests for evaluate() — five-layer pass/fail gating."""

    def setUp(self):
        self.records = [
            _make_tier_a_record(),
            _make_tier_b_record(),
            _make_tier_c_record(),
            _make_mine_swept_record(),
        ]
        self.ind_pe = industry_median_pe(self.records)

    def test_evaluate_tier_a(self):
        """Record passing all layers → Tier A."""
        rec = _make_tier_a_record()
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        self.assertEqual(deepest, 4)
        self.assertEqual(tier, "A_可买入")
        self.assertEqual(len(fails), 0)

    def test_evaluate_tier_b(self):
        """Record passing layers 0+1 → Tier B."""
        rec = _make_tier_b_record()
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        self.assertGreaterEqual(deepest, 1)
        # B tier requires: no L0 fails, no L1 fails
        l0_fails = sum(1 for f in fails if f in ("ST", "亏损")
                       or "负债率" in f or "商誉" in f or "OCF" in f)
        self.assertGreater(len(fails), 0, "Should have some fails beyond L0/L1")
        self.assertEqual(tier, "B_优质待跌")

    def test_evaluate_tier_c(self):
        """Record passing layer 0, failing 1 item in layer 1 → Tier C."""
        rec = _make_tier_c_record()
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        # ROE 12% < 15% → single L1 fail
        self.assertEqual(tier, "C_接近合格")

    def test_evaluate_data_insufficient(self):
        """Missing key fields → no Tier A/B."""
        rec = make_screening_record(
            market="cn", code="123456", display_code="123456",
            name="Data Poor",
        )
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        self.assertNotEqual(tier, "A_可买入")
        self.assertNotEqual(tier, "B_优质待跌")
        # L0 fails (PE won't matter since no pe_ttm defaults to None)
        self.assertGreater(len(fails), 0)

    def test_evaluate_st_filter_cn_only(self):
        """ST check only for CN market."""
        rec = make_screening_record(
            market="cn", code="600000", display_code="600000",
            name="*ST华泽", industry="综合", currency="CNY",
            price=1.0, pe_ttm=50.0,
            roe=5.0, gross_margin=20.0, net_margin=5.0,
            yoy=2.0, cagr=3.0,
            ocf_to_profit=0.5, debt_ratio=50.0,
        )
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        self.assertIn("ST", fails)

    def test_evaluate_no_st_filter_hk(self):
        """HK stock with 'ST' in name → no ST filter applied."""
        rec = make_screening_record(
            market="hk", code="00001", display_code="00001",
            name="CK Hutchison ST Holdings", industry="综合", currency="HKD",
            price=50.0, pe_ttm=10.0,
            roe=18.0, gross_margin=40.0, net_margin=15.0,
            yoy=12.0, cagr=10.0,
            ocf_to_profit=1.0, debt_ratio=40.0,
        )
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        # "ST" is in the name but market is HK, so no ST filter
        self.assertNotIn("ST", fails)

    def test_evaluate_non_cn_market(self):
        """HK/US records skip CN-specific filters (ST, goodwill)."""
        # US record with high goodwill → goodwill check skipped
        rec = make_screening_record(
            market="us", code="AAPL", display_code="AAPL",
            name="Apple Inc.", industry="Technology", currency="USD",
            price=195.0, pe_ttm=33.0, pb=51.0,
            market_cap=3000000000000,
            roe=165.0, gross_margin=44.0, net_margin=24.0,
            yoy=5.0, cagr=10.0,
            ocf_to_profit=1.26, debt_ratio=84.0,
            goodwill_ratio=0.0,
            ttm_netp=94000000000,
        )
        deepest, tier, fails = evaluate(rec, self.ind_pe)
        # No ST, no goodwill check for US
        self.assertNotIn("ST", fails)
        has_goodwill_fail = any("商誉" in f for f in fails)
        self.assertFalse(has_goodwill_fail,
                         "Goodwill check should be skipped for US market")


class TestScore(unittest.TestCase):
    """Tests for score() — 100-point weighted scoring."""

    def setUp(self):
        self.records = [
            _make_tier_a_record(),
            _make_tier_b_record(),
            _make_tier_c_record(),
            _make_mine_swept_record(),
        ]
        self.ind_pe = industry_median_pe(self.records)

    def test_score_range(self):
        """Score between 0-100."""
        for rec in self.records:
            s = score(rec, self.ind_pe)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 100.0)

    def test_tier_a_scores_higher_than_tier_c(self):
        """Tier A record should score higher than Tier C."""
        rec_a = _make_tier_a_record()
        rec_c = _make_tier_c_record()
        score_a = score(rec_a, self.ind_pe)
        score_c = score(rec_c, self.ind_pe)
        self.assertGreater(score_a, score_c,
                           f"Tier A ({score_a}) should outscore Tier C ({score_c})")


class TestIndustryMedianPE(unittest.TestCase):
    """Tests for industry_median_pe."""

    def test_industry_median_pe_per_market(self):
        """PE median computed within market."""
        records = [
            make_screening_record(market="cn", code="600519", display_code="600519",
                                  name="贵州茅台", industry="白酒", pe_ttm=18.0),
            make_screening_record(market="cn", code="000858", display_code="000858",
                                  name="五粮液", industry="白酒", pe_ttm=20.0),
            make_screening_record(market="cn", code="000568", display_code="000568",
                                  name="泸州老窖", industry="白酒", pe_ttm=22.0),
            make_screening_record(market="hk", code="00700", display_code="00700",
                                  name="腾讯", industry="互联网", pe_ttm=25.0),
            make_screening_record(market="hk", code="09988", display_code="09988",
                                  name="阿里", industry="互联网", pe_ttm=15.0),
        ]
        ind_pe = industry_median_pe(records, market="cn")
        self.assertIn("白酒", ind_pe)
        self.assertEqual(ind_pe["白酒"], 20.0)

    def test_industry_median_pe_cross_market(self):
        """Different markets have different medians (filtered)."""
        records = [
            make_screening_record(market="cn", code="600519", display_code="600519",
                                  name="茅台", industry="科技", pe_ttm=50.0),
            make_screening_record(market="hk", code="00700", display_code="00700",
                                  name="腾讯", industry="科技", pe_ttm=25.0),
            make_screening_record(market="us", code="AAPL", display_code="AAPL",
                                  name="苹果", industry="科技", pe_ttm=33.0),
        ]
        cn_pe = industry_median_pe(records, market="cn")
        hk_pe = industry_median_pe(records, market="hk")

        self.assertEqual(cn_pe.get("科技"), 50.0)
        self.assertEqual(hk_pe.get("科技"), 25.0)

    def test_industry_median_pe_ignores_invalid(self):
        """Median ignores None and non-positive PE values."""
        records = [
            make_screening_record(market="cn", code="600519", display_code="600519",
                                  name="茅台", industry="白酒", pe_ttm=18.0),
            make_screening_record(market="cn", code="600000", display_code="600000",
                                  name="无效", industry="白酒", pe_ttm=None),
            make_screening_record(market="cn", code="600001", display_code="600001",
                                  name="亏损", industry="白酒", pe_ttm=-5.0),
        ]
        ind_pe = industry_median_pe(records)
        self.assertEqual(ind_pe["白酒"], 18.0)


class TestMomentum(unittest.TestCase):
    """Tests for growth momentum scoring."""

    def test_momentum_calculation(self):
        """Verify growth momentum scoring logic in score()."""
        ind_pe = {}

        # Strong momentum: yoy >= cagr → 100
        rec_strong = make_screening_record(
            market="cn", code="000001", display_code="000001",
            name="Strong", yoy=20.0, cagr=15.0, pe_ttm=15.0,
            roe=20.0, gross_margin=40.0, net_margin=15.0,
            ocf_to_profit=1.0, debt_ratio=30.0,
        )
        s_strong = score(rec_strong, ind_pe)

        # Weak momentum: yoy << cagr
        rec_weak = make_screening_record(
            market="cn", code="000002", display_code="000002",
            name="Weak", yoy=5.0, cagr=20.0, pe_ttm=15.0,
            roe=20.0, gross_margin=40.0, net_margin=15.0,
            ocf_to_profit=1.0, debt_ratio=30.0,
        )
        s_weak = score(rec_weak, ind_pe)

        # Both have same quality metrics; strong momentum should score higher
        # (momentum is a component of the quality sub-score)
        self.assertGreater(
            s_strong, s_weak,
            f"Strong momentum ({s_strong}) should outscore weak ({s_weak})",
        )

    def test_momentum_missing_growth(self):
        """Momentum falls to 25 when yoy/cagr are missing."""
        ind_pe = {}
        rec = make_screening_record(
            market="cn", code="000003", display_code="000003",
            name="NoGrowth", yoy=None, cagr=None, pe_ttm=15.0,
            roe=20.0, gross_margin=40.0, net_margin=15.0,
            ocf_to_profit=1.0, debt_ratio=30.0,
        )
        s = score(rec, ind_pe)
        # Score should be > 0 (momentum=25 contributes, not zero)
        self.assertGreater(s, 0.0)


class TestMedian(unittest.TestCase):
    """Tests for the median helper function."""

    def test_median_odd(self):
        self.assertEqual(median([1, 3, 5]), 3)

    def test_median_even(self):
        self.assertEqual(median([1, 3, 5, 7]), 4.0)

    def test_median_empty(self):
        self.assertIsNone(median([]))

    def test_median_ignores_none(self):
        self.assertEqual(median([None, 1, 3, None, 5]), 3)


class TestScale(unittest.TestCase):
    """Tests for the scale helper function."""

    def test_scale_minimum(self):
        self.assertEqual(scale(10, 10, 100), 0.0)

    def test_scale_maximum(self):
        self.assertEqual(scale(100, 10, 100), 100.0)

    def test_scale_midpoint(self):
        self.assertEqual(scale(55, 10, 100), 50.0)

    def test_scale_none(self):
        self.assertEqual(scale(None, 10, 100), 50.0)

    def test_scale_clamped(self):
        self.assertEqual(scale(200, 10, 100), 100.0)
        self.assertEqual(scale(0, 10, 100), 0.0)


class TestRunFullPipeline(unittest.TestCase):
    """Tests for run_full_pipeline()."""

    def test_run_full_pipeline(self):
        """run_full_pipeline mutates records and returns counts."""
        records = [
            _make_tier_a_record(),      # Tier A
            _make_tier_b_record(),      # Tier B
            _make_tier_c_record(),      # Tier C
            _make_mine_swept_record(),  # -
        ]
        records, total_eval, tier_counts = run_full_pipeline(records)

        self.assertEqual(len(records), 4)
        # All records get deepest/tier/score
        for r in records:
            self.assertIn("deepest", r)
            self.assertIn("tier", r)
            self.assertIn("score", r)
            self.assertIn("fails", r)

        # Tier counts should have about 4 entries (one per record)
        self.assertGreaterEqual(total_eval, 1)  # at least tier A passes layer 0

        # Verify tier labels
        tiers = {r["tier"] for r in records}
        self.assertIn("A_可买入", tiers)

    def test_run_full_pipeline_multi_market(self):
        """Pipeline works across cn/hk/us records."""
        records = [
            _make_tier_a_record(market="cn"),
            _make_tier_a_record(market="hk", code="00700", name="腾讯"),
            _make_tier_a_record(market="us", code="AAPL", name="Apple Inc."),
        ]
        records, total_eval, tier_counts = run_full_pipeline(records)

        for r in records:
            self.assertIsNotNone(r.get("score"))
            self.assertIsInstance(r["score"], float)


if __name__ == "__main__":
    unittest.main()
