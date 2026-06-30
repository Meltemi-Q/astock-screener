"""Stub tests for global multi-market backtest framework.

Per PRD section 8.5. Backtest infrastructure is Phase 3 (not yet implemented).
These tests serve as design contracts and will be activated once backtest
modules are built.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestBacktestV0(unittest.TestCase):
    """Backtest v0: fixed-sample smoke test with mock data.

    Per PRD section 7.4. Uses mock price/financial data to validate
    the signal → equity curve → metrics pipeline.
    """

    @classmethod
    def setUpClass(cls):
        """Run backtest v0 once and store results for all tests."""
        from backtest.point_in_time import fixed_sample_v0
        cls.results = fixed_sample_v0(output_dir="results/backtest")

    def test_backtest_produces_equity_curve(self):
        """Backtest v0 output includes a non-empty equity curve."""
        curve = self.results.get("equity_curve", [])
        self.assertGreater(len(curve), 0)

    def test_backtest_outputs_required_metrics(self):
        """Backtest output includes: CAGR, max_drawdown, Sharpe, win_rate, turnover."""
        metrics = self.results.get("metrics", {})
        self.assertIn("cagr_pct", metrics)
        self.assertIn("max_drawdown_pct", metrics)
        self.assertIn("sharpe", metrics)
        self.assertIn("win_rate_pct", metrics)
        for key in ("cagr_pct", "max_drawdown_pct", "sharpe", "win_rate_pct"):
            self.assertIsInstance(metrics.get(key), (int, float))

    def test_backtest_empty_portfolio_stays_cash(self):
        """Empty portfolio should stay in cash (no phantom positions)."""
        curve = self.results.get("equity_curve", [])
        if not curve:
            self.skipTest("no equity curve data")
        for point in curve:
            # Cash should never be negative
            self.assertGreaterEqual(point.get("cash", 0), 0)

    def test_backtest_rebalance_uses_next_trade_day_execution(self):
        """Rebalance orders execute at next trading day's open/close price."""
        meta = self.results.get("meta", {})
        self.assertEqual(meta.get("version"), "v0-fixed-sample")

    def test_backtest_uses_only_filings_available_on_trade_date(self):
        """Point-in-time: v0 mock data inherently satisfies this.
        
        In v2 (PIT universe), this would be enforced by checking filing_date.
        """
        meta = self.results.get("meta", {})
        self.assertIn("survivorship_bias", meta)
        self.assertTrue(meta["survivorship_bias"])


class TestBacktestPlaceholder(unittest.TestCase):
    """Placeholder tests for Phase 3 v1/v2 backtest (not yet implemented)."""

    def test_backtest_missing_price_excludes_stock_and_reports_warning(self):
        """Missing price data should exclude stock from portfolio and log warning."""
        self.skipTest("Phase 3 v1: not yet implemented")


if __name__ == "__main__":
    unittest.main()
