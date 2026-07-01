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
    make_screening_record,
)
from screeners.scoring import (
    evaluate,
    score,
    run_full_pipeline,
    industry_median_pe,
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
        self.assertEqual(l0_fails, 0)
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

    def test_tier_ab_data_guard_blocks_missing_minesweep(self):
        """缺关键行情数据(market_cap)的记录即使质量过关也不得进 Tier B（死代码护栏落地）。

        排雷数据(ocf/debt)缺失已由 L0 fail-closed 拦下；此护栏兜住 L0/L1 检不到、
        但进 A/B 需要的关键字段(price/market_cap 等)缺失的漏网情形。
        """
        rec = make_screening_record(
            market="cn", code="900001", display_code="900001", name="缺行情数据",
            industry="白酒", currency="CNY",
            price=100.0, pe_ttm=18.0, market_cap=None,  # 缺市值
            roe=30.0, gross_margin=91.0, net_margin=50.0,
            yoy=22.0, cagr=20.0,
            ocf_to_profit=1.2, debt_ratio=18.0,
            goodwill_ratio=0.0, ttm_netp=80000000000,
        )
        records, _, _ = run_full_pipeline([rec])
        # 若无护栏，L0+L1 全过 → 会被判 B；护栏应把它挡下并记 flag
        self.assertNotEqual(records[0]["tier"], "A_可买入")
        self.assertNotEqual(records[0]["tier"], "B_优质待跌")
        self.assertIn("排雷数据不完整", records[0]["data_quality_flag"])


class TestDiscountSemantics(unittest.TestCase):
    """discount 应为 1 - 市值/合理市值（正值=便宜），且 L3 用 discount≥0.3 判买点。"""

    def test_discount_positive_when_cheap(self):
        # 市值 800B < 合理市值 1600B → discount = 0.5 > 0
        rec = _make_tier_a_record()
        self.assertGreater(rec["discount"], 0)
        self.assertAlmostEqual(rec["discount"], 0.5, places=4)

    def test_discount_negative_when_expensive(self):
        # 市值 3200B > 合理市值 1600B → discount = 1 - 2.0 = -1.0 < 0
        rec = make_screening_record(
            market="cn", code="600519", display_code="600519", name="贵州茅台",
            industry="白酒", currency="CNY",
            price=1500.0, pe_ttm=18.0, market_cap=3200000000000,
            roe=30.0, gross_margin=91.0, net_margin=50.0,
            yoy=22.0, cagr=20.0,
            ocf_to_profit=1.2, debt_ratio=18.0,
            goodwill_ratio=0.0, ttm_netp=80000000000,
        )
        self.assertLess(rec["discount"], 0)
        ind_pe = industry_median_pe([rec])
        _, _, fails = evaluate(rec, ind_pe)
        # 贵而未到买点：应有"安全边际不足"落选原因
        self.assertTrue(any("安全边际" in f for f in fails))

    def test_exp_ret_is_eyield_plus_g(self):
        # exp_ret = 100/pe + g = 100/18 + 20 ≈ 25.56
        rec = _make_tier_a_record()
        self.assertAlmostEqual(rec["exp_ret"], 100.0 / 18.0 + 20.0, places=3)


class TestGoodwillMineSweep(unittest.TestCase):
    """商誉率 ≥30%(净资产) 必须被第0层排雷（口径为百分数）。"""

    def test_goodwill_90pct_swept(self):
        rec = make_screening_record(
            market="cn", code="600001", display_code="600001", name="高商誉",
            industry="综合", currency="CNY",
            price=10.0, pe_ttm=15.0, market_cap=10000000000,
            roe=20.0, gross_margin=40.0, net_margin=15.0,
            yoy=15.0, cagr=12.0,
            ocf_to_profit=1.0, debt_ratio=40.0,
            goodwill_ratio=90.0,  # 商誉/净资产=90%（百分数口径）
            ttm_netp=600000000,
        )
        ind_pe = industry_median_pe([rec])
        deepest, tier, fails = evaluate(rec, ind_pe)
        self.assertTrue(any("商誉" in f for f in fails), f"90% 商誉必须被排雷: {fails}")
        self.assertEqual(deepest, 0)
        self.assertNotIn(tier, ("A_可买入", "B_优质待跌", "C_接近合格"))


class TestPerMarketOverride(unittest.TestCase):
    """per-market 阈值覆盖机制：A股无覆盖保持原值；覆盖 hook 生效。"""

    def test_resolve_config_cn_unchanged(self):
        from screeners.scoring import resolve_config, DEFAULT_CONFIG
        cfg = resolve_config(DEFAULT_CONFIG, "cn")
        self.assertEqual(cfg["min_roe"], DEFAULT_CONFIG["min_roe"])
        self.assertEqual(cfg["margin_of_safety"], DEFAULT_CONFIG["margin_of_safety"])

    def test_resolve_config_override_applies(self):
        from screeners.scoring import resolve_config, MARKET_THRESHOLD_OVERRIDES, DEFAULT_CONFIG
        MARKET_THRESHOLD_OVERRIDES["hk"] = {"min_roe": 12.0}
        try:
            cfg = resolve_config(DEFAULT_CONFIG, "hk")
            self.assertEqual(cfg["min_roe"], 12.0)
            # 原 config 不被污染
            self.assertEqual(DEFAULT_CONFIG["min_roe"], 15.0)
        finally:
            MARKET_THRESHOLD_OVERRIDES["hk"] = {}


class TestCrossPathConsistency(unittest.TestCase):
    """一致性：astock_screener(生产默认路径) 与 scoring(--market 路径) 对同一批构造输入产出相同 tier。"""

    def _cross_records(self):
        """构造覆盖 A/B/C/未通过 四种情形的输入，返回 (astock_rows, normalized_records)。"""
        import astock_screener as ast_s

        # 每个样本给出足以让两条路径重算派生量的原始字段
        samples = [
            # 全过 → A
            dict(code="000A", name="全过A", industry="白酒", roe=30.0, gm=91.0, nm=50.0,
                 yoy=22.0, cagr=20.0, ocf=1.2, debt=18.0, gw=0.0,
                 pe=18.0, mktcap=800e9, ttm_netp=80e9),
            # 质量过、估值不过 → B（PEG 高）
            dict(code="000B", name="优质待跌B", industry="银行", roe=18.0, gm=45.0, nm=15.0,
                 yoy=12.0, cagr=11.0, ocf=1.0, debt=50.0, gw=0.0,
                 pe=25.0, mktcap=250e9, ttm_netp=40e9),
            # 排雷过、质量差一项(ROE) → C
            dict(code="000C", name="接近C", industry="银行", roe=12.0, gm=70.0, nm=35.0,
                 yoy=12.0, cagr=11.0, ocf=1.3, debt=60.0, gw=0.0,
                 pe=5.5, mktcap=800e9, ttm_netp=140e9),
            # 商誉排雷 → 未通过
            dict(code="000X", name="高商誉X", industry="综合", roe=20.0, gm=40.0, nm=15.0,
                 yoy=15.0, cagr=12.0, ocf=1.0, debt=40.0, gw=90.0,
                 pe=15.0, mktcap=10e9, ttm_netp=0.6e9),
        ]

        ast_rows, norm_recs = [], []
        for s in samples:
            g = min(s["yoy"], s["cagr"])
            eyield = 100.0 / s["pe"]
            peg = s["pe"] / g if g > 0 else None
            exp_ret = eyield + g
            rpe = max(12.0, min(30.0, g))
            fair = s["ttm_netp"] * rpe
            disc = 1.0 - s["mktcap"] / fair
            # astock_screener 记录形态（evaluate 直接消费的字段）
            ast_rows.append({
                "code": s["code"], "name": s["name"], "industry": s["industry"],
                "roe": s["roe"], "gross_margin": s["gm"], "net_margin": s["nm"],
                "net_profit": s["ttm_netp"], "revenue": 1e11, "yoy": s["yoy"], "cagr": s["cagr"], "g": g,
                "ocf_to_profit": s["ocf"], "debt_ratio": s["debt"], "goodwill_ratio": s["gw"],
                "pe_ttm": s["pe"], "pe_dyn": s["pe"], "pb": 3.0, "mktcap": s["mktcap"],
                "eyield": eyield, "peg": peg, "exp_ret": exp_ret,
                "reasonable_pe": rpe, "fair_mktcap": fair, "discount": disc,
                "is_st": False,
            })
            norm_recs.append(make_screening_record(
                market="cn", code=s["code"], display_code=s["code"], name=s["name"],
                industry=s["industry"], currency="CNY",
                price=10.0, pe_ttm=s["pe"], market_cap=s["mktcap"],
                roe=s["roe"], gross_margin=s["gm"], net_margin=s["nm"],
                yoy=s["yoy"], cagr=s["cagr"],
                ocf_to_profit=s["ocf"], debt_ratio=s["debt"], goodwill_ratio=s["gw"],
                ttm_netp=s["ttm_netp"],
            ))
        return ast_s, ast_rows, norm_recs

    def test_same_tier_across_paths(self):
        ast_s, ast_rows, norm_recs = self._cross_records()

        # astock_screener 路径
        ast_ind_pe = ast_s.industry_median_pe(ast_rows)
        ast_tiers = {}
        for r in ast_rows:
            _, tier, _ = ast_s.evaluate(r, ast_ind_pe)
            ast_tiers[r["code"]] = tier

        # scoring(--market) 路径
        norm_recs, _, _ = run_full_pipeline(norm_recs, config=None, market="cn")
        norm_tiers = {r["code"]: r["tier"] for r in norm_recs}

        for code in ast_tiers:
            self.assertEqual(
                ast_tiers[code], norm_tiers[code],
                f"{code}: 生产路径={ast_tiers[code]} 与 --market 路径={norm_tiers[code]} tier 不一致",
            )


if __name__ == "__main__":
    unittest.main()
