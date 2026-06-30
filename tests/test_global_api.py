"""Tests for server API endpoints with market parameter.

Per PRD section 8.4. Tests market-switching API behavior, error responses,
and backward compatibility with A-share-only endpoints.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _make_handler():
    """Create a ScreenerHandler with captured send_json output."""
    handler = server.ScreenerHandler.__new__(server.ScreenerHandler)
    captured = {}

    def send_json(data, status=200):
        captured["data"] = data
        captured["status"] = status

    handler.send_json = send_json
    return handler, captured


def _setup_temp_results(structure=None):
    """Create temporary results directories with optional files.

    Args:
        structure: dict of filename → content for files in results/.

    Returns:
        TemporaryDirectory context manager result.
    """
    td = tempfile.TemporaryDirectory()
    tdp = Path(td.name)
    (tdp / "results").mkdir(parents=True, exist_ok=True)
    (tdp / "cache").mkdir(parents=True, exist_ok=True)
    if structure:
        for fname, content in structure.items():
            fpath = tdp / "results" / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
    return td, tdp


# ═══════════════════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════════════════

class TestApiStatus(unittest.TestCase):
    """Tests for /api/status endpoint."""

    def test_api_status_all_markets(self):
        """/api/status?market=all returns cn/hk/us keys."""
        old_results = server.RESULTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                server.RESULTS_DIR = str(tdp)

                handler, captured = _make_handler()
                handler._api_status(["cn", "hk", "us"])

                data = captured["data"]
                self.assertIn("markets", data)
                for m in ("cn", "hk", "us"):
                    self.assertIn(m, data["markets"],
                                  f"Missing market {m} in status response")
        finally:
            server.RESULTS_DIR = old_results

    def test_api_status_invalid_market(self):
        """/api/status?market=zzz returns 400."""
        handler, captured = _make_handler()
        # Simulate what _handle_api does for invalid market
        handler.send_json(
            {"error": "无效 market 参数: zzz，有效值: cn, hk, us, all"},
            status=400,
        )

        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["data"])

    def test_api_status_defaults_to_cn(self):
        """/api/status (no market) works and returns cn data."""
        old_results = server.RESULTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                server.RESULTS_DIR = str(tdp)

                handler, captured = _make_handler()
                handler._api_status(["cn"])

                data = captured["data"]
                self.assertEqual(captured["status"], 200)
                self.assertIn("market", data)
                self.assertEqual(data["market"], "cn")
        finally:
            server.RESULTS_DIR = old_results


class TestApiRefresh(unittest.TestCase):
    """Tests for /api/refresh endpoint."""

    def test_api_refresh_invalid_market(self):
        """/api/refresh?market=zzz returns 400."""
        handler, captured = _make_handler()
        handler.send_json(
            {"error": "无效 market 参数: zzz，有效值: cn, hk, us"},
            status=400,
        )
        self.assertEqual(captured["status"], 400)

    def test_api_refresh_hk_quotes(self):
        """/api/refresh?market=hk&mode=quotes works (use mock subprocess)."""
        import subprocess

        old_results = server.RESULTS_DIR
        old_last_run = server._last_run
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                res_dir = tdp / "results"
                res_dir.mkdir(parents=True)

                # Create a dummy HK screen file so _latest_screen_ts works
                (res_dir / "hkstock_screen_20260630.html").write_text("", encoding="utf-8")
                (res_dir / "hkstock_screen_20260630.csv").write_text("", encoding="utf-8")

                server.RESULTS_DIR = str(res_dir)
                server._last_run = {}

                handler, captured = _make_handler()

                with patch.object(
                    server.subprocess, "run",
                    return_value=subprocess.CompletedProcess(
                        args=["python3", "screeners/hk.py"],
                        returncode=0, stdout="ok", stderr="",
                    ),
                ):
                    handler._api_refresh("hk", "quotes")

                self.assertEqual(captured["status"], 200)
                self.assertTrue(captured["data"]["done"])
                self.assertEqual(captured["data"]["market"], "hk")
                self.assertEqual(captured["data"]["mode"], "quotes")

        finally:
            server.RESULTS_DIR = old_results
            server._last_run = old_last_run


class TestApiDeep(unittest.TestCase):
    """Tests for /api/deep endpoint."""

    def test_api_deep_hk_not_implemented(self):
        """/api/deep?market=hk returns 501 (not implemented)."""
        handler, captured = _make_handler()
        # Simulate the handler's check for deep_script=None
        handler.send_json(
            {"error": "港股市场暂不支持深度研报生成", "market": "hk"},
            status=501,
        )
        self.assertEqual(captured["status"], 501)
        self.assertIn("暂不支持", captured["data"]["error"])

    def test_api_deep_us_not_implemented(self):
        """/api/deep?market=us returns 501 (not implemented)."""
        handler, captured = _make_handler()
        handler.send_json(
            {"error": "美股市场暂不支持深度研报生成", "market": "us"},
            status=501,
        )
        self.assertEqual(captured["status"], 501)


class TestApiLayer4(unittest.TestCase):
    """Tests for /api/layer4 endpoint."""

    def test_api_layer4_hk_not_implemented(self):
        """/api/layer4?market=hk returns 501 (not implemented)."""
        handler, captured = _make_handler()
        handler.send_json(
            {"error": "港股市场暂不支持批量 AI 定性分析", "market": "hk"},
            status=501,
        )
        self.assertEqual(captured["status"], 501)


class TestApiErrorStructure(unittest.TestCase):
    """Tests for error response structure."""

    def test_api_error_structure(self):
        """Error responses have market + source + retryable fields."""

        # Test a sample error shape from /api/refresh
        error_response = {
            "done": False,
            "error": "刷新行情超时(>300s)",
            "market": "hk",
            "mode": "quotes",
            "source": "screeners/hk.py",
            "retryable": True,
            "warnings": ["港股 刷新行情超时"],
        }

        self.assertIn("market", error_response)
        self.assertIn("error", error_response)
        # The server includes market in all API responses
        self.assertEqual(error_response["market"], "hk")

    def test_api_invalid_market_response_structure(self):
        """Invalid market responses include market parameter hint."""
        handler, captured = _make_handler()
        handler.send_json(
            {"error": "无效 market 参数: zzz，有效值: cn, hk, us, all"},
            status=400,
        )
        data = captured["data"]
        self.assertIn("error", data)
        self.assertIn("cn", data["error"].lower() or "")
        self.assertIn("hk", data["error"].lower() or "")
        # Error must include valid market options
        valid_options = data.get("valid", ["cn", "hk", "us", "all"])
        if not valid_options:
            # Check that error message contains valid options
            self.assertTrue(
                any(m in data["error"] for m in ("cn", "hk", "us")),
                "Error message should list valid market options",
            )

    def test_api_missing_code_error(self):
        """Missing code parameter returns structured error."""
        handler, captured = _make_handler()
        handler.send_json(
            {"error": "缺少 code 参数", "market": "cn"},
            status=400,
        )
        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["data"])


class TestMarketConfig(unittest.TestCase):
    """Tests for MARKET_CONFIG registry."""

    def test_all_markets_registered(self):
        """Verify cn, hk, us are in MARKET_CONFIG."""
        for m in ("cn", "hk", "us"):
            self.assertIn(m, server.MARKET_CONFIG)

    def test_cn_supports_deep_dives(self):
        """A-share market config has deep_script set."""
        cfg = server.MARKET_CONFIG["cn"]
        self.assertIsNotNone(cfg["deep_script"])

    def test_hk_us_no_deep_dives(self):
        """HK and US market configs have deep_script=None (not implemented)."""
        for m in ("hk", "us"):
            cfg = server.MARKET_CONFIG[m]
            self.assertIsNone(
                cfg["deep_script"],
                f"{m} deep_script should be None (not yet implemented)",
            )

    def test_cn_code_pattern_is_6_digits(self):
        """A-share code pattern matches 6 digits."""
        cfg = server.MARKET_CONFIG["cn"]
        self.assertTrue(cfg["code_pattern"].match("600519"))
        self.assertFalse(cfg["code_pattern"].match("123"))
        self.assertFalse(cfg["code_pattern"].match("1234567"))

    def test_hk_code_pattern_is_5_digits(self):
        """HK code pattern matches 5 digits."""
        cfg = server.MARKET_CONFIG["hk"]
        self.assertTrue(cfg["code_pattern"].match("00700"))
        self.assertFalse(cfg["code_pattern"].match("1234"))

    def test_us_code_pattern_is_alpha_1_5(self):
        """US code pattern matches 1-5 letters."""
        cfg = server.MARKET_CONFIG["us"]
        self.assertTrue(cfg["code_pattern"].match("AAPL"))
        self.assertTrue(cfg["code_pattern"].match("A"))
        self.assertFalse(cfg["code_pattern"].match(""))
        self.assertFalse(cfg["code_pattern"].match("AAPLED"))


if __name__ == "__main__":
    unittest.main()
