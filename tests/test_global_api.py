"""Tests for server API endpoints with market parameter.

Per PRD section 8.4. Tests market-switching API behavior, error responses,
and backward compatibility with A-share-only endpoints.
"""

from __future__ import annotations

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

def _make_handler(path: str = "/", headers: dict | None = None):
    """Create a ScreenerHandler with captured send_json output.

    Sets ``path``/``headers`` so real routing methods (_handle_api,
    _check_auth) can run without a live socket.
    """
    handler = server.ScreenerHandler.__new__(server.ScreenerHandler)
    captured = {}

    def send_json(data, status=200):
        captured["data"] = data
        captured["status"] = status

    handler.send_json = send_json
    handler.path = path

    class _Headers(dict):
        def get(self, key, default=""):
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            return default

    handler.headers = _Headers(headers or {})
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
        """/api/status?market=zzz returns 400 via real routing."""
        handler, captured = _make_handler(path="/api/status?market=zzz")
        handler._handle_api()

        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["data"])
        self.assertIn("zzz", captured["data"]["error"])

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

    def test_output_validation_normalizes_short_tier_codes(self):
        """HK/US CSV short tier codes are normalized for status cards."""
        import csv
        from screeners import output_validation

        old_rule = output_validation.MARKET_RULES["us"].copy()
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                output_validation.MARKET_RULES["us"]["min_rows"] = 1
                output_validation.MARKET_RULES["us"]["required_codes"] = ()
                output_validation.MARKET_RULES["us"]["min_tier_signals"] = 1

                with (tdp / "usstock_screen_20260630.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as f:
                    writer = csv.DictWriter(f, fieldnames=["code", "tier"])
                    writer.writeheader()
                    writer.writerow({"code": "AAPL", "tier": "A"})
                    writer.writerow({"code": "MSFT", "tier": "B"})
                    writer.writerow({"code": "NVDA", "tier": "C"})
                (tdp / "usstock_screen_20260630.html").write_text("", encoding="utf-8")

                status = output_validation.validate_market_result(str(tdp), "us", "20260630")

            self.assertTrue(status["valid"], status)
            self.assertEqual(status["tier_counts"]["A_可买入"], 1)
            self.assertEqual(status["tier_counts"]["B_优质待跌"], 1)
            self.assertEqual(status["tier_counts"]["C_接近合格"], 1)
        finally:
            output_validation.MARKET_RULES["us"] = old_rule

    def test_output_validation_rejects_all_dash_tiers(self):
        """A full-size market artifact with no A/B/C signal is not ready."""
        import csv
        from screeners import output_validation

        old_rule = output_validation.MARKET_RULES["hk"].copy()
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                output_validation.MARKET_RULES["hk"]["min_rows"] = 2
                output_validation.MARKET_RULES["hk"]["required_codes"] = ()
                output_validation.MARKET_RULES["hk"]["min_tier_signals"] = 1

                with (tdp / "hkstock_screen_20260630.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as f:
                    writer = csv.DictWriter(f, fieldnames=["code", "tier"])
                    writer.writeheader()
                    writer.writerow({"code": "00700", "tier": "-"})
                    writer.writerow({"code": "09988", "tier": "-"})
                (tdp / "hkstock_screen_20260630.html").write_text("", encoding="utf-8")

                status = output_validation.validate_market_result(str(tdp), "hk", "20260630")

            self.assertFalse(status["valid"], status)
            self.assertEqual(status["tier_signal_count"], 0)
            self.assertTrue(
                any("tier_signal_count" in err for err in status["errors"]),
                status["errors"],
            )
        finally:
            output_validation.MARKET_RULES["hk"] = old_rule


class TestApiRefresh(unittest.TestCase):
    """Tests for /api/refresh endpoint."""

    def test_api_refresh_invalid_market(self):
        """/api/refresh?market=zzz returns 400 via real routing."""
        handler, captured = _make_handler(path="/api/refresh?market=zzz")
        handler._handle_api()
        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["data"])

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

                # Create a dummy HK screen file so file-count logic works.
                # Artifact validation is patched below; production validation
                # is covered separately.
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
                ), patch.object(
                    server,
                    "latest_market_result",
                    return_value={
                        "status": "ready",
                        "latest": {
                            "ts": "20260630",
                            "row_count": 1200,
                            "tier_counts": {},
                            "warnings": [],
                        },
                        "latest_invalid": None,
                        "checked": 1,
                    },
                ):
                    handler._api_refresh("hk", "quotes")

                self.assertEqual(captured["status"], 200)
                self.assertTrue(captured["data"]["done"])
                self.assertEqual(captured["data"]["market"], "hk")
                self.assertEqual(captured["data"]["mode"], "quotes")
                self.assertEqual(captured["data"]["effective_mode"], "full")

        finally:
            server.RESULTS_DIR = old_results
            server._last_run = old_last_run

    def test_api_status_invalid_result_is_not_ready(self):
        """Small HK sample output is reported as invalid, not ready."""
        old_results = server.RESULTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                server.RESULTS_DIR = str(tdp)
                (tdp / "hkstock_screen_20260630.csv").write_text(
                    "rank,tier,code,name\n1,A,00700,Tencent\n",
                    encoding="utf-8",
                )
                (tdp / "hkstock_screen_20260630.html").write_text("", encoding="utf-8")

                handler, captured = _make_handler()
                handler._api_status(["hk"])

                self.assertEqual(captured["status"], 200)
                self.assertFalse(captured["data"]["done"])
                self.assertEqual(captured["data"]["status"], "invalid")
                self.assertEqual(captured["data"]["latest_invalid_ts"], "20260630")
                self.assertTrue(captured["data"]["errors"])

        finally:
            server.RESULTS_DIR = old_results


class TestApiDeep(unittest.TestCase):
    """Tests for /api/deep endpoint."""

    def test_api_deep_hk_uses_global_deep_script(self):
        """/api/deep?market=hk invokes global_deep_dive.py."""
        import subprocess

        handler, captured = _make_handler()
        with patch.object(
            server.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=["global_deep_dive.py"], returncode=0, stdout="ok", stderr="",
            ),
        ) as run_mock:
            handler._api_deep("hk", "01530")

        self.assertEqual(captured["status"], 200)
        self.assertTrue(captured["data"]["done"])
        args = run_mock.call_args.args[0]
        self.assertIn("global_deep_dive.py", args[1])
        self.assertIn("--market", args)
        self.assertIn("hk", args)

    def test_api_deep_us_uses_global_deep_script(self):
        """/api/deep?market=us invokes global_deep_dive.py."""
        import subprocess

        handler, captured = _make_handler()
        with patch.object(
            server.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=["global_deep_dive.py"], returncode=0, stdout="ok", stderr="",
            ),
        ) as run_mock:
            handler._api_deep("us", "calm")

        self.assertEqual(captured["status"], 200)
        self.assertTrue(captured["data"]["done"])
        self.assertEqual(captured["data"]["code"], "CALM")
        args = run_mock.call_args.args[0]
        self.assertIn("--market", args)
        self.assertIn("us", args)


class TestApiLayer4(unittest.TestCase):
    """Tests for /api/layer4 endpoint."""

    def test_api_layer4_hk_supported_but_requires_tier_codes(self):
        """/api/layer4?market=hk no longer returns not-implemented."""
        handler, captured = _make_handler()
        with patch.object(server, "_tier_stock_codes", return_value=[]):
            handler._api_layer4("hk", "A")

        self.assertEqual(captured["status"], 200)
        self.assertFalse(captured["data"]["done"])
        self.assertIn("无可用标的", captured["data"]["msg"])


class TestApiErrorStructure(unittest.TestCase):
    """Tests for error response structure (real routing)."""

    def test_api_unknown_endpoint(self):
        """Unknown /api/* path returns 404 via real routing."""
        handler, captured = _make_handler(path="/api/nope")
        handler._handle_api()
        self.assertEqual(captured["status"], 404)
        self.assertIn("error", captured["data"])

    def test_api_invalid_market_response_structure(self):
        """Invalid market responses list valid options via real routing."""
        handler, captured = _make_handler(path="/api/status?market=zzz")
        handler._handle_api()
        data = captured["data"]
        self.assertEqual(captured["status"], 400)
        self.assertIn("error", data)
        self.assertTrue(
            all(m in data["error"] for m in ("cn", "hk", "us")),
            "Error message should list valid market options",
        )

    def test_api_missing_code_error(self):
        """Missing code parameter returns structured 400 via real routing."""
        handler, captured = _make_handler(path="/api/deep?market=cn")
        handler._handle_api()
        self.assertEqual(captured["status"], 400)
        self.assertIn("error", captured["data"])
        self.assertEqual(captured["data"]["market"], "cn")


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

    def test_hk_us_support_global_deep_dives(self):
        """HK and US market configs use the cross-market deep-dive script."""
        for m in ("hk", "us"):
            cfg = server.MARKET_CONFIG[m]
            self.assertEqual(cfg["deep_script"], "global_deep_dive.py")

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


class TestPublicDeploySecurity(unittest.TestCase):
    """Tests for public-deploy hardening: auth, cooldown, XSS, quota."""

    def setUp(self):
        # 每个用例前重置全局状态，避免相互影响。
        server._last_run = {}
        with server._quota_lock:
            server._quota_state["day"] = ""
            server._quota_state["count"] = 0

    def test_billing_endpoint_requires_token_when_configured(self):
        """Setting SCREENER_TOKEN gates billing endpoints with 401."""
        old = server.AUTH_TOKEN
        try:
            server.AUTH_TOKEN = "secret123"
            handler, captured = _make_handler(path="/api/refresh?market=cn")
            handler._handle_api()
            self.assertEqual(captured["status"], 401)

            # Wrong token still 401.
            handler, captured = _make_handler(
                path="/api/refresh?market=cn",
                headers={"X-Auth-Token": "wrong"},
            )
            handler._handle_api()
            self.assertEqual(captured["status"], 401)
        finally:
            server.AUTH_TOKEN = old

    def test_status_endpoint_not_gated_by_token(self):
        """Read-only /api/status stays open even with token set."""
        old = server.AUTH_TOKEN
        old_results = server.RESULTS_DIR
        try:
            server.AUTH_TOKEN = "secret123"
            with tempfile.TemporaryDirectory() as td:
                server.RESULTS_DIR = str(Path(td))
                handler, captured = _make_handler(path="/api/status?market=cn")
                handler._handle_api()
                self.assertEqual(captured["status"], 200)
        finally:
            server.AUTH_TOKEN = old
            server.RESULTS_DIR = old_results

    def test_correct_token_via_query_passes_auth(self):
        """Correct ?token= passes the auth gate (reaches format validation)."""
        old = server.AUTH_TOKEN
        try:
            server.AUTH_TOKEN = "secret123"
            # deep with bad code but valid token → 400 (format), not 401.
            handler, captured = _make_handler(
                path="/api/deep?market=cn&code=abc&token=secret123"
            )
            handler._handle_api()
            self.assertEqual(captured["status"], 400)
        finally:
            server.AUTH_TOKEN = old

    def test_deep_cooldown_returns_429_on_rapid_repeat(self):
        """Second deep call for same code within cooldown returns 429."""
        import subprocess

        ok = subprocess.CompletedProcess(
            args=["deep"], returncode=0, stdout="ok", stderr=""
        )
        with patch.object(server.subprocess, "run", return_value=ok):
            handler, captured = _make_handler()
            handler._api_deep("cn", "600519")
            self.assertEqual(captured["status"], 200)

            handler2, captured2 = _make_handler()
            handler2._api_deep("cn", "600519")
            self.assertEqual(captured2["status"], 429)
            self.assertTrue(captured2["data"].get("cached"))

    def test_unified_screen_html_escapes_script_close_tag(self):
        """Injected JSON escapes </ so a market label can't break out."""
        old_results = server.RESULTS_DIR
        old_template = server.TEMPLATE_SCREEN
        try:
            with tempfile.TemporaryDirectory() as td:
                server.RESULTS_DIR = str(Path(td))
                server.TEMPLATE_SCREEN = str(Path(td) / "nonexistent.html")
                with patch.object(
                    server, "_market_status",
                    return_value={"latest_ts": "</script><script>alert(1)</script>",
                                  "file_count": 0},
                ):
                    body = server._build_unified_screen_html().decode("utf-8")
                self.assertNotIn("</script><script>alert", body)
                self.assertIn("<\\/script>", body)
        finally:
            server.RESULTS_DIR = old_results
            server.TEMPLATE_SCREEN = old_template

    def test_err_tail_hidden_unless_debug(self):
        """_err_tail returns empty unless SCREENER_DEBUG on."""
        old = server.DEBUG_ERRORS
        try:
            server.DEBUG_ERRORS = False
            self.assertEqual(server._err_tail("secret path /opt/x"), "")
            server.DEBUG_ERRORS = True
            self.assertIn("secret", server._err_tail("secret path"))
        finally:
            server.DEBUG_ERRORS = old


if __name__ == "__main__":
    unittest.main()
