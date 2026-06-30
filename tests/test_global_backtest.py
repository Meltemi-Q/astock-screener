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


class TestBacktestPlaceholder(unittest.TestCase):
    """Placeholder tests for Phase 3 backtest module.

    These will be activated when backtest/ is implemented.
    """

    def test_backtest_uses_only_filings_available_on_trade_date(self):
        """Point-in-time: use only filings available on trade date, not future."""
        self.skipTest("Phase 3: backtest not implemented")

    def test_backtest_empty_portfolio_stays_cash(self):
        """Empty portfolio should stay in cash (no phantom positions)."""
        self.skipTest("Phase 3: backtest not implemented")

    def test_backtest_missing_price_excludes_stock_and_reports_warning(self):
        """Missing price data should exclude stock from portfolio and log warning."""
        self.skipTest("Phase 3: backtest not implemented")

    def test_backtest_rebalance_uses_next_trade_day_execution(self):
        """Rebalance orders execute at next trading day's open/close price."""
        self.skipTest("Phase 3: backtest not implemented")

    def test_backtest_outputs_required_metrics(self):
        """Backtest output includes: CAGR, max_drawdown, Sharpe, win_rate, turnover."""
        self.skipTest("Phase 3: backtest not implemented")


if __name__ == "__main__":
    unittest.main()
