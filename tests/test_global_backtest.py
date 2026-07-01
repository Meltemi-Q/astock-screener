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
    """Backtest v0: MOCK 占位管道的结构冒烟测试。

    ⚠️ 仅验证 signal → equity curve → metrics 管道结构与 mock 行为，
    不声称、不验证任何真实回测保证（次日成交/PIT 财报/无幸存者偏差均未实现）。
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

    def test_backtest_outputs_mock_metric_fields(self):
        """结构断言：metrics 含 mock_ 前缀的占位指标字段且为数值。

        指标数值本身是 mock，不代表任何真实业绩；此处只校验字段结构。
        """
        metrics = self.results.get("metrics", {})
        self.assertTrue(metrics.get("is_mock"))
        for key in ("mock_cagr_pct", "mock_max_drawdown_pct",
                    "mock_sharpe", "mock_win_rate_pct", "mock_turnover_per_year"):
            self.assertIn(key, metrics)
            self.assertIsInstance(metrics.get(key), (int, float))

    def test_backtest_does_not_emit_fake_benchmark(self):
        """诚实性断言：不得输出人为构造的基准/alpha 字段。"""
        metrics = self.results.get("metrics", {})
        self.assertNotIn("benchmark_cagr_pct", metrics)
        self.assertNotIn("benchmark_cagr", metrics)
        self.assertNotIn("alpha", metrics)

    def test_backtest_portfolio_values_are_absolute_values_not_returns(self):
        """portfolio_values 存的是期末组合绝对市值（非收益率），且字段名已正名。"""
        metrics = self.results.get("metrics", {})
        self.assertIn("portfolio_values", metrics)
        self.assertNotIn("annual_returns", metrics)
        for v in metrics.get("portfolio_values", {}).values():
            # 绝对市值应为正的大额资金量级，而非 [-1,1] 的收益率
            self.assertGreater(v, 1000)

    def test_backtest_empty_portfolio_stays_cash(self):
        """Empty portfolio should stay in cash (no phantom positions)."""
        curve = self.results.get("equity_curve", [])
        if not curve:
            self.skipTest("no equity curve data")
        for point in curve:
            # Cash should never be negative
            self.assertGreaterEqual(point.get("cash", 0), 0)

    def test_backtest_meta_is_marked_mock(self):
        """meta 标记为 mock 版本（不承诺次日成交等真实成交语义）。

        注意：v0 未实现次日成交、涨跌停、停牌等撮合语义；本测试只校验 mock 标记。
        """
        meta = self.results.get("meta", {})
        self.assertTrue(meta.get("is_mock"))
        self.assertEqual(meta.get("version"), "v0-fixed-sample-mock")

    def test_backtest_meta_honestly_declares_survivorship_bias(self):
        """诚实标注：meta 明确声明存在幸存者偏差（并非声称无偏 PIT）。

        v0 仅用当前 universe，不做真正的 point-in-time filing 约束；此处只校验诚实标注。
        """
        meta = self.results.get("meta", {})
        self.assertIn("survivorship_bias", meta)
        self.assertTrue(meta["survivorship_bias"])
        self.assertIn("limitations", meta)


class TestBacktestPlaceholder(unittest.TestCase):
    """Placeholder tests for Phase 3 v1/v2 backtest (not yet implemented)."""

    def test_backtest_missing_price_excludes_stock_and_reports_warning(self):
        """Missing price data should exclude stock from portfolio and log warning."""
        self.skipTest("Phase 3 v1: not yet implemented")


if __name__ == "__main__":
    unittest.main()
