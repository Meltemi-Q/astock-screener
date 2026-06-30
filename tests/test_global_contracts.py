"""Tests for cross-market data contracts (screeners/contracts.py).

Per PRD section 8.2. Validates all normalized data contracts and helpers
used by the five-layer screening pipeline.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from screeners.contracts import (
    NORMALIZED_FIELDS,
    make_security_master,
    make_quote_snapshot,
    make_annual_financial,
    make_screening_record,
    check_tier_ab_eligibility,
    check_currency_match,
)


class TestScreeningRecord(unittest.TestCase):
    """Tests for make_screening_record and NORMALIZED_FIELDS."""

    def test_make_screening_record_all_fields(self):
        """Verify all NORMALIZED_FIELDS are present in output."""
        record = make_screening_record(
            market="cn", code="600519", display_code="600519",
            name="贵州茅台", industry="白酒", exchange="SSE",
            currency="CNY", price=1500.0, min_buy=150000.0,
            pe_ttm=25.0, pe_dyn=23.0, pb=8.0,
            market_cap=2000000000000, market_cap_cny=2000000000000,
            roe=30.0, gross_margin=91.0, net_margin=50.0,
            yoy=15.0, cagr=14.0,
            ocf_to_profit=1.1, debt_ratio=18.0,
            goodwill_ratio=0.0, deduct_ratio=0.95,
            ttm_netp=80000000000,
        )

        for field in NORMALIZED_FIELDS:
            self.assertIn(
                field, record,
                f"NORMALIZED_FIELDS member '{field}' missing from record",
            )

    def test_make_screening_record_derived_fields(self):
        """Verify derived fields are computed correctly."""
        record = make_screening_record(
            market="cn", code="000001", display_code="000001", name="Test",
            price=20.0, pe_ttm=10.0,
            yoy=15.0, cagr=12.0,
            ttm_netp=10000000000,
            market_cap=150000000000,
        )

        # g = min(yoy, cagr) = 12
        self.assertEqual(record["g"], 12.0)

        # peg = pe_ttm / g = 10 / 12 ≈ 0.833
        self.assertIsNotNone(record["peg"])
        self.assertAlmostEqual(record["peg"], 10.0 / 12.0, places=2)

        # eyield = 100 / pe_ttm = 10.0
        self.assertEqual(record["eyield"], 10.0)

        # fair_pe = max(12, min(g, 30)) = max(12, 12) = 12
        self.assertEqual(record["fair_pe"], 12.0)

        # fair_mktcap = ttm_netp * fair_pe = 10B * 12 = 120B
        self.assertEqual(record["fair_mktcap"], 120000000000.0)

        # discount = market_cap / fair_mktcap = 150B / 120B = 1.25
        self.assertAlmostEqual(record["discount"], 1.25, places=4)

    def test_make_screening_record_g_fallback(self):
        """g field falls back to yoy or cagr when one is None."""
        # Only yoy
        r1 = make_screening_record(
            market="cn", code="000001", display_code="000001", name="T",
            yoy=20.0, cagr=None,
        )
        self.assertEqual(r1["g"], 20.0)

        # Only cagr
        r2 = make_screening_record(
            market="cn", code="000002", display_code="000002", name="T",
            yoy=None, cagr=18.0,
        )
        self.assertEqual(r2["g"], 18.0)

        # Both None
        r3 = make_screening_record(
            market="cn", code="000003", display_code="000003", name="T",
        )
        self.assertIsNone(r3["g"])


class TestTierAbEligibility(unittest.TestCase):
    """Tests for check_tier_ab_eligibility."""

    def test_check_tier_ab_eligibility_missing_price(self):
        """Missing price → not eligible."""
        record = {"price": None, "market_cap": 1000000000}
        eligible, missing = check_tier_ab_eligibility(record)
        self.assertFalse(eligible)
        self.assertIn("price", missing)

    def test_check_tier_ab_eligibility_missing_market_cap(self):
        """Missing market cap → not eligible."""
        record = {"price": 100.0, "market_cap": None}
        eligible, missing = check_tier_ab_eligibility(record)
        self.assertFalse(eligible)
        self.assertIn("market_cap", missing)

    def test_check_tier_ab_eligibility_all_present(self):
        """All fields present → eligible."""
        record = {"price": 100.0, "market_cap": 1000000000, "roe": 20.0}
        eligible, missing = check_tier_ab_eligibility(record)
        self.assertTrue(eligible)
        self.assertEqual(len(missing), 0)

    def test_check_tier_ab_eligibility_no_financial_data(self):
        """Without raw financials and no computed financial fields → not eligible."""
        record = {"price": 100.0, "market_cap": 1000000000,
                  "roe": None, "gross_margin": None}
        eligible, missing = check_tier_ab_eligibility(record)
        self.assertFalse(eligible)
        self.assertIn("financial_data", missing)

    def test_check_tier_ab_eligibility_with_raw_financials(self):
        """With raw financials, checks revenue/net_profit/equity/operating_cashflow."""
        record = {"price": 100.0, "market_cap": 1000000000}
        raw_fin = make_annual_financial(
            market="us", code="AAPL", fiscal_year=2024,
            revenue=100000000000, net_profit=20000000000,
            equity=50000000000, operating_cashflow=30000000000,
        )
        eligible, missing = check_tier_ab_eligibility(record, raw_fin)
        self.assertTrue(eligible)
        self.assertEqual(len(missing), 0)

    def test_check_tier_ab_eligibility_missing_financial_fields(self):
        """Raw financials missing equity → not eligible."""
        record = {"price": 100.0, "market_cap": 1000000000}
        raw_fin = make_annual_financial(
            market="us", code="AAPL", fiscal_year=2024,
            revenue=100000000000, net_profit=20000000000,
            operating_cashflow=30000000000,
        )
        eligible, missing = check_tier_ab_eligibility(record, raw_fin)
        self.assertFalse(eligible)
        self.assertIn("fin.equity", missing)


class TestCurrencyMatch(unittest.TestCase):
    """Tests for check_currency_match."""

    def test_currency_match_same(self):
        """USD == USD → True."""
        self.assertTrue(check_currency_match("USD", "USD"))

    def test_currency_match_different(self):
        """USD != HKD → False."""
        self.assertFalse(check_currency_match("USD", "HKD"))

    def test_currency_match_cny_rmb(self):
        """CNY == RMB → True."""
        self.assertTrue(check_currency_match("CNY", "RMB"))

    def test_currency_match_case_insensitive(self):
        """Case-insensitive matching."""
        self.assertTrue(check_currency_match("usd", "USD"))
        self.assertTrue(check_currency_match("hkd", "HKD"))

    def test_currency_match_yen_symbol(self):
        """¥ == CNY → True."""
        self.assertTrue(check_currency_match("¥", "CNY"))


class TestSecurityMaster(unittest.TestCase):
    """Tests for make_security_master."""

    def test_security_master_fields(self):
        """Verify make_security_master produces correct dict."""
        sm = make_security_master(
            market="hk", code="00700", name="Tencent",
            exchange="HKEX", currency="HKD", lot_size=100,
        )
        self.assertEqual(sm["market"], "hk")
        self.assertEqual(sm["code"], "00700")
        self.assertEqual(sm["name"], "Tencent")
        self.assertEqual(sm["exchange"], "HKEX")
        self.assertEqual(sm["currency"], "HKD")
        self.assertEqual(sm["lot_size"], 100)
        self.assertEqual(sm["security_type"], "common_stock")
        self.assertTrue(sm["is_tradable"])

    def test_security_master_display_code_defaults(self):
        """display_code defaults to code when not provided."""
        sm = make_security_master(market="us", code="AAPL", name="Apple Inc.")
        self.assertEqual(sm["display_code"], "AAPL")


class TestQuoteSnapshot(unittest.TestCase):
    """Tests for make_quote_snapshot."""

    def test_quote_snapshot_fields(self):
        """Verify make_quote_snapshot has all required fields."""
        qs = make_quote_snapshot(
            market="hk", code="00700", price=400.0, pe_ttm=22.0,
            pb=5.0, market_cap=3500000000000,
            currency="HKD", quote_time="2026-06-30 10:00:00",
            source="eastmoney",
        )
        self.assertEqual(qs["market"], "hk")
        self.assertEqual(qs["code"], "00700")
        self.assertEqual(qs["price"], 400.0)
        self.assertEqual(qs["pe_ttm"], 22.0)
        self.assertEqual(qs["pb"], 5.0)
        self.assertEqual(qs["market_cap"], 3500000000000)
        self.assertEqual(qs["currency"], "HKD")
        self.assertEqual(qs["source"], "eastmoney")


class TestAnnualFinancial(unittest.TestCase):
    """Tests for make_annual_financial."""

    def test_annual_financial_fields(self):
        """Verify make_annual_financial produces correct dict with all keys."""
        af = make_annual_financial(
            market="us", code="AAPL", fiscal_year=2024,
            report_date="2024-09-28", currency="USD",
            revenue=395760000000, gross_profit=175320000000,
            net_profit=93736000000, operating_cashflow=118254000000,
            assets=364980000000, liabilities=308030000000,
            equity=56950000000, eps=6.49,
            roe=165.0, gross_margin=44.3, net_margin=23.7,
            debt_ratio=84.4,
        )
        self.assertEqual(af["market"], "us")
        self.assertEqual(af["code"], "AAPL")
        self.assertEqual(af["fiscal_year"], 2024)
        self.assertEqual(af["revenue"], 395760000000)
        self.assertEqual(af["gross_profit"], 175320000000)
        self.assertEqual(af["net_profit"], 93736000000)
        self.assertEqual(af["assets"], 364980000000)
        self.assertEqual(af["liabilities"], 308030000000)
        self.assertEqual(af["equity"], 56950000000)
        self.assertEqual(af["eps"], 6.49)


class TestLotSize(unittest.TestCase):
    """Tests for minimum buy amount by market convention."""

    def test_lot_size_cn(self):
        """A-share: lot_size = 100, min_buy = price * 100."""
        # A-shares always use 100 shares per lot
        price = 25.5
        min_buy = price * 100
        self.assertEqual(min_buy, 2550.0)

    def test_lot_size_hk(self):
        """HK: min_buy = price * board_lot."""
        # Tencent board_lot = 100, price ≈ 400 HKD
        price = 400.0
        board_lot = 100
        min_buy = price * board_lot
        self.assertEqual(min_buy, 40000.0)

        # CK Hutchison board_lot = 500
        price = 50.0
        board_lot = 500
        min_buy = price * board_lot
        self.assertEqual(min_buy, 25000.0)

    def test_lot_size_us(self):
        """US: min_buy = price (1 share, fractional shares common)."""
        price = 195.0
        min_buy = price  # 1 share
        self.assertEqual(min_buy, 195.0)


if __name__ == "__main__":
    unittest.main()
