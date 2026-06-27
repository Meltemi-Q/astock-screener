import csv
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RegressionTests(unittest.TestCase):
    def test_run_sh_help_exits_without_starting_screener(self):
        result = subprocess.run(
            ["bash", "run.sh", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("用法", result.stdout)
        self.assertNotIn("抓取数据中", result.stdout + result.stderr)

    def test_stock_deep_dive_help_renders(self):
        result = subprocess.run(
            [sys.executable, "stock_deep_dive.py", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("--no-kline", result.stdout)

    def test_api_deep_reports_subprocess_failure(self):
        import server

        handler = server.ScreenerHandler.__new__(server.ScreenerHandler)
        captured = {}

        def send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        handler.send_json = send_json
        failed = subprocess.CompletedProcess(
            args=["stock_deep_dive.py"], returncode=1, stdout="not found", stderr="boom"
        )
        with patch.object(server.subprocess, "run", return_value=failed):
            handler._api_deep("999999")

        self.assertFalse(captured["data"]["done"])
        self.assertEqual(captured["data"]["exit_code"], 1)
        self.assertGreaterEqual(captured["status"], 400)

    def test_generated_html_initializes_api_before_status_fetch(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn("location.origin", source)
        self.assertLess(source.index("var API="), source.index('fetch(API+"/api/status")'))

    def test_deep_dive_generation_writes_shared_shell_and_json_payload(self):
        import stock_deep_dive

        stock = {
            "code": "000001",
            "name": "平安银行",
            "industry": "银行",
            "price": 10.5,
            "pe_ttm": 5.2,
            "pb": 0.6,
            "mktcap": 200000000000,
            "kline": {"day": [], "week": [], "month": []},
            "peers": [
                {
                    "code": "600000",
                    "name": "浦发银行",
                    "tier": "B_优质待跌",
                    "pe": 5.8,
                    "roe": 8.0,
                    "gm": 0,
                    "mktcap": 1800,
                    "has_deep": True,
                }
            ],
        }
        financials = [
            {
                "year": "2024",
                "rev": 1000000000,
                "netp": 100000000,
                "roe": 10.0,
                "gm": 30.0,
                "nm": 10.0,
                "debt": 60.0,
                "roa": 1.0,
                "eps": 1.2,
                "cf_oper": 120000000,
                "ocf_ratio": 1.2,
            }
        ]

        with tempfile.TemporaryDirectory() as td:
            deep_dir = Path(td) / "deep_dives"
            deep_dir.mkdir()
            legacy_html_path = deep_dir / "000001.html"

            result_path = stock_deep_dive.generate_html(
                stock, financials, None, str(legacy_html_path), "20260627"
            )

            data_path = deep_dir / "data" / "000001.json"
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            self.assertEqual(str(data_path), result_path)
            self.assertEqual(payload["meta"]["code"], "000001")
            self.assertEqual(payload["meta"]["name"], "平安银行")
            self.assertTrue((deep_dir / "report.html").exists())
            self.assertTrue((deep_dir / "assets" / "deep_dive.js").exists())
            self.assertFalse(legacy_html_path.exists())

    def test_deep_dive_index_links_to_shared_report_shell(self):
        import stock_deep_dive

        with tempfile.TemporaryDirectory() as td:
            idx_path = stock_deep_dive.generate_index(
                [
                    {
                        "code": "000001",
                        "name": "平安银行",
                        "tier": "B_优质待跌",
                        "price": "10.5",
                        "roe": "10",
                        "pe": "5.2",
                        "gm": "30",
                        "mktcap": "2000",
                        "industry": "银行",
                        "score": "70",
                    }
                ],
                td,
                "20260627",
            )

            html = Path(idx_path).read_text(encoding="utf-8")
            self.assertIn("report.html?code=000001", html)
            self.assertNotIn('href="000001.html"', html)

    def test_screener_marks_json_deep_dive_reports_and_links_shared_shell(self):
        import astock_screener

        record = {
            "code": "000001",
            "name": "平安银行",
            "price": 10.5,
            "industry": "银行",
            "tier": "B_优质待跌",
            "score": 70,
            "deepest": 4,
            "roe": 10.0,
            "gross_margin": 30.0,
            "net_margin": 10.0,
            "yoy": 12.0,
            "cagr": 11.0,
            "pe_ttm": 5.2,
            "peg": 0.5,
            "exp_ret": 15.0,
            "discount": 0.4,
            "ocf_to_profit": 1.2,
            "deduct_ratio": 0.9,
            "debt_ratio": 60.0,
            "goodwill_ratio": 0.0,
            "mktcap": 200000000000,
            "notes": [],
            "fails": [],
        }
        old_out_dir = astock_screener.OUT_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                results_dir = Path(td) / "results"
                data_dir = results_dir / "deep_dives" / "data"
                data_dir.mkdir(parents=True)
                (data_dir / "000001.json").write_text("{}", encoding="utf-8")
                out = results_dir / "astock_screen_20260627.html"
                astock_screener.OUT_DIR = str(results_dir)

                astock_screener.write_html([record], str(out), 2025, 1, (0, 1, 0))

                html = out.read_text(encoding="utf-8")
                self.assertIn("deep_dives/report.html?code='+v", html)
                self.assertIn('"deepCount": 1', html)
        finally:
            astock_screener.OUT_DIR = old_out_dir

    def test_fetch_stock_full_uses_shared_spot_cache_without_refetching_market_data(self):
        import stock_deep_dive

        old_cache = stock_deep_dive._FINANCIAL_CACHE
        try:
            stock_deep_dive._FINANCIAL_CACHE = {
                "income": {"000001": []},
                "balance": {"000001": []},
                "cashflow": {"000001": []},
            }
            with patch.object(stock_deep_dive, "_existing_report_codes", return_value=set()):
                stock = stock_deep_dive.fetch_stock_full(
                    "000001",
                    name="平安银行",
                    industry="银行",
                    csv_rows=[],
                    no_kline=True,
                    spot_cache={
                        "000001": {
                            "f2": 10.5,
                            "f9": 5.0,
                            "f23": 0.6,
                            "f20": 200000000000,
                            "f115": 5.2,
                        }
                    },
                )

            self.assertEqual(stock["price"], 10.5)
            self.assertEqual(stock["pe_ttm"], 5.2)
        finally:
            stock_deep_dive._FINANCIAL_CACHE = old_cache

    def test_auto_prefetch_financials_only_for_batch_runs(self):
        import stock_deep_dive

        self.assertFalse(stock_deep_dive.should_prefetch_financials("auto", 1, True))
        self.assertFalse(stock_deep_dive.should_prefetch_financials("auto", 20, False))
        self.assertTrue(stock_deep_dive.should_prefetch_financials("auto", 80, False))
        self.assertTrue(stock_deep_dive.should_prefetch_financials("always", 1, True))
        self.assertFalse(stock_deep_dive.should_prefetch_financials("never", 200, False))

    def test_deepseek_limiter_caps_concurrent_ai_calls(self):
        import stock_deep_dive

        active = 0
        peak = 0
        guard = threading.Lock()

        def fake_deepseek(stock, financials):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return {"thesis": stock["code"]}

        limiter = threading.BoundedSemaphore(2)
        with patch.object(stock_deep_dive, "deepseek_analyze", side_effect=fake_deepseek):
            threads = [
                threading.Thread(
                    target=stock_deep_dive.run_deepseek_with_limiter,
                    args=({"code": str(i).zfill(6)}, [], limiter),
                )
                for i in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertLessEqual(peak, 2)

    def test_ai_only_updates_existing_json_without_refetching_stock_data(self):
        import stock_deep_dive

        old_out_dir = stock_deep_dive.OUT_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                deep_dir = Path(td) / "deep_dives"
                data_dir = deep_dir / "data"
                data_dir.mkdir(parents=True)
                payload = {
                    "meta": {
                        "code": "000001",
                        "name": "平安银行",
                        "industry": "银行",
                        "screen_ts": "20260627",
                        "generated_at": "2026-06-27 12:00",
                    },
                    "quote": {"price": 10.5, "pe_ttm": 5.2, "pb": 0.6, "mktcap": 200000000000},
                    "financials": [{"year": "2025", "roe": 10.0, "gm": 30.0, "netp": 100}],
                    "peers": [],
                    "kline": {"day": [], "week": [], "month": []},
                    "analysis": None,
                }
                (data_dir / "000001.json").write_text(json.dumps(payload), encoding="utf-8")
                stock_deep_dive.OUT_DIR = str(deep_dir)

                with patch.object(stock_deep_dive, "fetch_stock_full", side_effect=AssertionError("should not refetch")):
                    with patch.object(stock_deep_dive, "deepseek_analyze", return_value={"thesis": "测试逻辑"}):
                        updated = stock_deep_dive.run_ai_only_for_existing_report("000001")

                saved = json.loads((data_dir / "000001.json").read_text(encoding="utf-8"))
                self.assertTrue(updated)
                self.assertEqual(saved["analysis"]["thesis"], "测试逻辑")
        finally:
            stock_deep_dive.OUT_DIR = old_out_dir

    def test_layer4_report_handles_missing_codes_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shutil.copy(ROOT / "layer4_report.py", tmp / "layer4_report.py")
            (tmp / "results").mkdir()
            (tmp / "cache" / "verdicts").mkdir(parents=True)
            (tmp / "cache" / "_summary.md").write_text("## 摘要\n\n测试摘要", encoding="utf-8")

            csv_path = tmp / "results" / "astock_screen_20260627.csv"
            with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["code", "tier", "score", "name", "industry", "pe_ttm", "discount"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "code": "600519",
                        "tier": "A_可买入",
                        "score": "70",
                        "name": "贵州茅台",
                        "industry": "白酒",
                        "pe_ttm": "18",
                        "discount": "0.2",
                    }
                )

            verdict = {
                "code": "600519",
                "qual_score": 80,
                "moat_score": 9,
                "moat_type": "品牌",
                "industry_outlook": "稳定",
                "value_trap_risk": "低",
                "final_verdict": "买入候选",
                "confidence": "高",
                "thesis": "测试逻辑",
            }
            (tmp / "cache" / "verdicts" / "0.json").write_text(
                json.dumps(verdict, ensure_ascii=False), encoding="utf-8"
            )

            result = subprocess.run(
                [sys.executable, "layer4_report.py"],
                cwd=tmp,
                text=True,
                capture_output=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(list((tmp / "results").glob("astock_layer4_final_*.html")))


if __name__ == "__main__":
    unittest.main()
