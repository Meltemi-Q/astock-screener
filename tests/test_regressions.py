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

    def test_run_sh_stops_existing_project_servers_before_starting(self):
        source = (ROOT / "run.sh").read_text(encoding="utf-8")

        self.assertIn("stop_existing_project_servers", source)
        self.assertIn("server.py --port", source)
        self.assertIn("lsof -tiTCP", source)
        self.assertLess(
            source.index("stop_existing_project_servers"),
            source.index("for try_port in"),
        )

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

    def test_cbond_double_low_help_renders(self):
        result = subprocess.run(
            [sys.executable, "cbond_double_low.py", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("--max-double-low", result.stdout)
        self.assertIn("可转债双低策略", result.stdout)

    def test_cbond_deep_dive_help_renders(self):
        result = subprocess.run(
            [sys.executable, "cbond_deep_dive.py", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("--from-screen", result.stdout)
        self.assertIn("可转债深度分析", result.stdout)

    def test_cbond_double_low_links_to_internal_deep_report(self):
        source = (ROOT / "cbond_double_low.py").read_text(encoding="utf-8")

        self.assertIn("cbond_deep/report.html?code=", source)
        self.assertIn("ensure_cbond_deep_shell", source)

    def test_cbond_deep_uses_convertible_bond_specific_analysis(self):
        report_js = (ROOT / "templates" / "cbond_deep" / "assets" / "cbond_deep.js").read_text(encoding="utf-8")
        script = (ROOT / "cbond_deep_dive.py").read_text(encoding="utf-8")

        self.assertIn("双低性", report_js)
        self.assertIn("下跌保护", report_js)
        self.assertIn("强赎/回售", report_js)
        self.assertIn("轮动纪律", script)
        self.assertIn("可转债双低策略研究员", script)

    def test_cbond_deep_scores_candidate_and_reject(self):
        import cbond_deep_dive

        financials = [{
            "year": 2025,
            "roe": 18.0,
            "debt": 38.0,
            "ocf_ratio": 1.3,
            "netp_yoy": 25.0,
        }]
        candidate = {
            "status": "基础候选",
            "price": 112.0,
            "premium_rt": 12.0,
            "double_low": 124.0,
            "rating": "AA+",
            "remaining_scale": 20.0,
            "remaining_years": 2.0,
            "turnover": 80_000_000,
        }
        rejected = dict(candidate, status="剔除", rating="A", remaining_scale=0.5)

        candidate_scores = cbond_deep_dive.compute_scores(candidate, financials)
        rejected_scores = cbond_deep_dive.compute_scores(rejected, financials)

        self.assertGreater(candidate_scores["total"], 70)
        self.assertLessEqual(rejected_scores["total"], 45)
        self.assertIn(cbond_deep_dive.action_label(candidate, candidate_scores), {"篮子候选", "小仓试跑"})
        self.assertIn("event_risk", candidate_scores)

    def test_convertible_bond_quote_board_estimates_remaining_scale(self):
        from data_sources.convertible_bonds import parse_quote_board_row

        row = parse_quote_board_row({
            "f12": "128142",
            "f14": "新乳转债",
            "f2": 115.51,
            "f20": 829106523,
        })

        self.assertEqual(row["code"], "128142")
        self.assertAlmostEqual(row["remaining_scale"], 7.18, places=2)

    def test_cbond_strategy_classifies_buy_watch_and_reject(self):
        import argparse
        from datetime import date
        import cbond_double_low

        args = argparse.Namespace(
            min_rating="AA-",
            min_scale=2.0,
            min_years=0.5,
            max_price=130.0,
            max_premium=30.0,
            max_double_low=150.0,
        )
        today = date(2026, 7, 1)
        records = [
            {
                "code": "113001", "name": "候选转债", "stock_name": "正常股份",
                "price": 110.0, "premium_rt": 15.0, "double_low": 125.0,
                "rating": "AA", "remaining_scale": 5.0, "remaining_years": 2.0,
                "listing_date": "2024-01-01 00:00:00", "delist_date": None,
                "has_quote_board": True,
            },
            {
                "code": "113002", "name": "观察转债", "stock_name": "正常股份",
                "price": 132.0, "premium_rt": 20.0, "double_low": 152.0,
                "rating": "AA", "remaining_scale": 5.0, "remaining_years": 2.0,
                "listing_date": "2024-01-01 00:00:00", "delist_date": None,
                "has_quote_board": True,
            },
            {
                "code": "113003", "name": "风险转债", "stock_name": "*ST风险",
                "price": 90.0, "premium_rt": 10.0, "double_low": 100.0,
                "rating": "A", "remaining_scale": 1.0, "remaining_years": 0.2,
                "listing_date": "2024-01-01 00:00:00", "delist_date": None,
                "has_quote_board": True,
            },
        ]

        out = {r["code"]: r for r in cbond_double_low.classify_records(records, args, today)}
        self.assertEqual(out["113001"]["status"], "基础候选")
        self.assertIn(out["113001"]["final_action"], {"小仓试跑", "观察"})
        self.assertIn("enhanced_status", out["113001"])
        self.assertEqual(out["113002"]["status"], "观察")
        self.assertEqual(out["113003"]["status"], "剔除")
        self.assertIn("评级低于", out["113003"]["risk_reasons"])

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
            handler._api_deep("cn", "999999")

        self.assertFalse(captured["data"]["done"])
        self.assertIn("error", captured["data"])
        self.assertGreaterEqual(captured["status"], 400)

    def test_api_cbond_deep_invokes_cbond_deep_script(self):
        import server

        handler = server.ScreenerHandler.__new__(server.ScreenerHandler)
        captured = {}

        def send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        handler.send_json = send_json
        ok = subprocess.CompletedProcess(
            args=["cbond_deep_dive.py"], returncode=0, stdout="ok", stderr=""
        )
        with patch.object(server, "_cbond_deep_json_exists", return_value=False):
            with patch.object(server.subprocess, "run", return_value=ok) as run:
                handler._api_cbond_deep("113042")

        args = run.call_args.args[0]
        self.assertTrue(captured["data"]["done"])
        self.assertIn("cbond_deep_dive.py", args[1])
        self.assertIn("--code", args)
        self.assertIn("113042", args)

    def test_generated_html_initializes_api_before_status_fetch(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn("location.origin", source)
        self.assertLess(source.index("var API="), source.index('fetch(API+"/api/status")'))

    def test_stable_entrypoints_are_lightweight_aliases_to_latest_screen(self):
        import astock_screener

        old_out_dir = astock_screener.OUT_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                results_dir = Path(td) / "results"
                results_dir.mkdir()
                (results_dir / "astock_screen_20260628.html").write_text(
                    "<html>latest screen</html>", encoding="utf-8"
                )
                astock_screener.OUT_DIR = str(results_dir)

                astock_screener.write_latest_entrypoints("20260628")

                for name in ("index.html", "astock_screen.html"):
                    html = (results_dir / name).read_text(encoding="utf-8")
                    self.assertIn("astock_screen_20260628.html", html)
                    self.assertIn("固定入口", html)
                    self.assertLess(len(html), 3000)
        finally:
            astock_screener.OUT_DIR = old_out_dir

    def test_server_stable_routes_resolve_to_latest_dated_screen(self):
        import server

        old_results_dir = server.RESULTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                results_dir = Path(td)
                (results_dir / "astock_screen.html").write_text("alias", encoding="utf-8")
                (results_dir / "astock_screen_20260627.html").write_text("old", encoding="utf-8")
                latest = results_dir / "astock_screen_20260628.html"
                latest.write_text("latest", encoding="utf-8")
                rows = ["rank,tier,code,name"]
                rows.append("1,A,000001,stock1")
                rows.extend(f"{i},-,{i:06d},stock{i}" for i in range(2, 4002))
                (results_dir / "astock_screen_20260628.csv").write_text(
                    "\n".join(rows),
                    encoding="utf-8",
                )
                server.RESULTS_DIR = str(results_dir)

                self.assertEqual(Path(server._latest_screen_path()), latest)

            source = (ROOT / "server.py").read_text(encoding="utf-8")
            self.assertIn('astock_screen.html', source)
            self.assertIn('def _latest_screen_path', source)
            self.assertIn('ConnectionResetError', source)
            self.assertIn('BrokenPipeError', source)
        finally:
            server.RESULTS_DIR = old_results_dir

    def test_market_home_links_are_rewritten_to_latest_result(self):
        source = (ROOT / "templates" / "screen.html").read_text(encoding="utf-8")

        self.assertIn('linkEl.setAttribute("href", data.latest_href', source)
        self.assertIn('data.stable_href||MARKET_META[market].stable', source)

    def test_market_home_shows_candidate_pool_in_addition_to_tier_a(self):
        source = (ROOT / "templates" / "screen.html").read_text(encoding="utf-8")

        self.assertIn("var signal=a+b+c", source)
        self.assertIn("候选池", source)
        self.assertIn("候选 \"+signal+\" 只", source)

    def test_market_leaderboards_show_candidate_pool_not_only_tier_a(self):
        for rel in ("astock_screener.py", "screeners/hk.py", "screeners/us.py"):
            with self.subTest(file=rel):
                source = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("候选榜 Top 10", source)
                self.assertIn("a.concat(b)", source)
                self.assertIn("本期无 A/B 候选", source)

    def test_hk_us_rows_link_to_shared_deep_dive_report(self):
        cases = {
            "screeners/hk.py": "deep_dives/report.html?market=hk&code=",
            "screeners/us.py": "deep_dives/report.html?market=us&code=",
        }
        for rel, expected in cases.items():
            with self.subTest(file=rel):
                source = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn(expected, source)
                self.assertIn('href="deep_dives/report.html?market=', source)

    def test_server_stable_market_routes_use_http_redirect(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")

        self.assertIn("send_response(302)", source)
        self.assertIn('send_header("Location", latest_name)', source)

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
            self.assertIn('id="backLink"', html)
            self.assertIn('href="../astock_screen.html"', html)
            self.assertNotIn('href="../astock_screen_20260627.html"', html)

    def test_deep_dive_index_accepts_csv_metric_field_names(self):
        import stock_deep_dive

        with tempfile.TemporaryDirectory() as td:
            idx_path = stock_deep_dive.generate_index(
                [
                    {
                        "code": "688336",
                        "name": "三生国健",
                        "tier": "A_可买入",
                        "price": "41.46",
                        "roe": "41.32",
                        "pe_ttm": "12.49",
                        "gross_margin": "92.0708953302",
                        "mktcap_yi": "371.575",
                        "industry": "生物制品",
                        "score": "86.24",
                    }
                ],
                td,
                "20260628",
            )

            html = Path(idx_path).read_text(encoding="utf-8")
            self.assertIn("<td>12.49</td>", html)
            self.assertIn("<td>92.1%</td>", html)
            self.assertIn("<td>371.6亿</td>", html)
            self.assertNotIn("<td>%</td>", html)
            self.assertNotIn("<td>亿</td>", html)

    def test_deep_dive_shell_does_not_overwrite_latest_back_link_with_old_payload_ts(self):
        source = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.js").read_text(encoding="utf-8")

        self.assertIn('../astock_screen.html', source)
        self.assertNotIn('meta.screen_ts||"20260627"', source)
        self.assertNotIn('backLink").href="../astock_screen_"+(meta.screen_ts', source)
        self.assertNotIn('backLink").href="../astock_screen_"+d.latest_ts+".html"', source)

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
                self.assertIn("deep_dives/report.html?code='+ec", html)
                self.assertIn('"deepCount": 1', html)
        finally:
            astock_screener.OUT_DIR = old_out_dir

    def test_screener_ignores_legacy_html_deep_dive_reports(self):
        import astock_screener

        record = {
            "code": "002895",
            "name": "川恒股份",
            "price": 30.46,
            "industry": "农化制品",
            "tier": "-",
            "score": 32.75,
            "deepest": 0,
            "roe": 18.2,
            "gross_margin": 30.3,
            "net_margin": 15.1,
            "yoy": 31.8,
            "cagr": 18.4,
            "pe_ttm": 14.8,
            "peg": 0.8,
            "exp_ret": 25.2,
            "discount": 0.197,
            "ocf_to_profit": 0.48,
            "deduct_ratio": 0.99,
            "debt_ratio": 36.0,
            "goodwill_ratio": 0.0,
            "mktcap": 18460000000,
            "notes": [],
            "fails": [],
        }
        old_out_dir = astock_screener.OUT_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                results_dir = Path(td) / "results"
                legacy_dir = results_dir / "deep_dives"
                legacy_dir.mkdir(parents=True)
                (legacy_dir / "002895.html").write_text("<html></html>", encoding="utf-8")
                out = results_dir / "astock_screen_20260627.html"
                astock_screener.OUT_DIR = str(results_dir)

                astock_screener.write_html([record], str(out), 2025, 1, (0, 0, 0))

                html = out.read_text(encoding="utf-8")
                self.assertIn('"deepCount": 0', html)
                self.assertIn('"deep": 0', html)
        finally:
            astock_screener.OUT_DIR = old_out_dir

    def test_deep_dive_shell_generates_missing_report_instead_of_dead_error(self):
        source = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.js").read_text(encoding="utf-8")

        self.assertIn("generateMissingReport(code)", source)
        self.assertIn('fetch(API+"/api/deep?market="+market+"&code="+code)', source)
        self.assertIn("function marketFromLocation()", source)
        self.assertIn("function payloadName(market,code)", source)

    def test_global_deep_dive_generates_prefixed_market_payloads(self):
        import global_deep_dive

        self.assertEqual(global_deep_dive.payload_filename("hk", "01530"), "hk_01530.json")
        self.assertEqual(global_deep_dive.payload_filename("us", "calm"), "us_CALM.json")

    def test_global_deep_dive_uses_tencent_kline_for_hk_us(self):
        source = (ROOT / "global_deep_dive.py").read_text(encoding="utf-8")

        self.assertIn("web.ifzq.gtimg.cn/appstock/app/fqkline/get", source)
        self.assertIn('f"us{code}.OQ"', source)
        self.assertIn('f"hk{code}"', source)

    def test_industry_chart_reserves_bottom_space_for_rotated_labels(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn("H=210,labelPad=76", source)
        self.assertIn("ctx.translate(x+barW/2,H-labelPad+58)", source)

    def test_industry_chart_limits_labels_on_narrow_viewports(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn("visibleCount=W<420?6:8", source)
        self.assertIn("META.topInds.slice(0,visibleCount)", source)

    def test_funnel_chart_reserves_bottom_label_space(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn(".funnel{display:flex;gap:0;height:170px;", source)
        self.assertIn("padding:18px 6px 40px", source)

    def test_stale_screen_html_is_preserved_as_historical_snapshot(self):
        import astock_screener

        old_out_dir = astock_screener.OUT_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                results_dir = Path(td) / "results"
                results_dir.mkdir()
                old = results_dir / "astock_screen_20260627.html"
                latest = results_dir / "astock_screen_20260628.html"
                old.write_text("<html>old 352</html>", encoding="utf-8")
                latest.write_text("<html>latest 301</html>", encoding="utf-8")
                astock_screener.OUT_DIR = str(results_dir)

                astock_screener.write_stale_screen_redirects("20260628")

                html = old.read_text(encoding="utf-8")
                self.assertEqual("<html>old 352</html>", html)
        finally:
            astock_screener.OUT_DIR = old_out_dir

    def test_light_theme_tokens_are_used_for_main_and_deep_pages(self):
        main_source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")
        deep_css = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.css").read_text(encoding="utf-8")

        self.assertIn("--bg:#f6f8fb;--text:#172033", main_source)
        self.assertIn("--bg:#f6f8fb;--text:#172033", deep_css)

    def test_theme_toggle_exists_on_main_and_deep_pages(self):
        main_source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")
        report_html = (ROOT / "templates" / "deep_dive" / "report.html").read_text(encoding="utf-8")
        report_js = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.js").read_text(encoding="utf-8")
        report_css = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.css").read_text(encoding="utf-8")
        index_source = (ROOT / "stock_deep_dive.py").read_text(encoding="utf-8")

        self.assertIn('id="themeToggle"', main_source)
        self.assertIn('localStorage.setItem("theme"', main_source)
        self.assertIn(':root[data-theme="dark"]', main_source)
        self.assertIn('id="themeToggle"', report_html)
        self.assertIn('localStorage.setItem("theme"', report_js)
        self.assertIn(':root[data-theme="dark"]', report_css)
        self.assertIn('id="themeToggle"', index_source)

    def test_dashboard_controls_and_leaderboard_are_responsive(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn(".leaderboard{display:flex;flex-direction:column;gap:6px;font-size:12px;overflow:visible}", source)
        self.assertNotIn("max-height:170px;overflow-y:auto", source)
        self.assertIn(".controls input#q{flex:1 1 160px;min-width:140px;max-width:220px}", source)
        self.assertIn("input[type=checkbox]{min-height:auto;width:auto;padding:0}", source)
        self.assertIn("@media(max-width:720px)", source)

    def test_refresh_actions_distinguish_quotes_and_full_screening(self):
        main_source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")
        server_source = (ROOT / "server.py").read_text(encoding="utf-8")

        self.assertIn('id="quotesRefreshBtn"', main_source)
        self.assertIn('id="fullRefreshBtn"', main_source)
        self.assertIn('fetch(API+"/api/refresh?mode=quotes")', main_source)
        self.assertIn('fetch(API+"/api/refresh?mode=full")', main_source)
        self.assertIn('--quotes-fresh', server_source)
        self.assertIn('qs.get("mode", "quotes")', server_source)
        self.assertIn('id="refreshHint"', main_source)
        self.assertIn('盘后适合更新五层筛选', main_source)
        self.assertIn('盘后：适合更新五层筛选', main_source)

    def test_refresh_actions_return_to_stable_screen_entrypoint(self):
        main_source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn('function goStableEntry()', main_source)
        self.assertIn('window.location.href="astock_screen.html"', main_source)
        self.assertNotIn("window.location.reload()},800", main_source)

    def test_main_long_running_actions_show_progress_panel(self):
        main_source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn('id="progressPanel"', main_source)
        self.assertIn('role="progressbar"', main_source)
        self.assertIn(".progress-panel", main_source)
        self.assertIn("@keyframes progressSlide", main_source)
        self.assertIn("function startProgress", main_source)
        self.assertIn("function setProgress", main_source)
        self.assertIn("function finishProgress", main_source)
        self.assertIn('startProgress(loadingText', main_source)
        self.assertIn('setProgress(pct,"Tier "+p.tier+" 定性分析"', main_source)
        self.assertIn('progressTrack").setAttribute("aria-valuenow"', main_source)
        self.assertIn("progressMetaBase", main_source)
        self.assertIn("progressElapsedText()", main_source)

    def test_deep_dive_long_running_actions_show_inline_progress(self):
        report_js = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.js").read_text(encoding="utf-8")
        report_css = (ROOT / "templates" / "deep_dive" / "assets" / "deep_dive.css").read_text(encoding="utf-8")

        self.assertIn(".status.working", report_css)
        self.assertIn(".mini-progress", report_css)
        self.assertIn(".deep-gen.loading", report_css)
        self.assertIn("function miniProgressMarkup", report_js)
        self.assertIn("function setInlineProgress", report_js)
        self.assertIn("showStatus(message, working)", report_js)
        self.assertIn('showStatus("本地还没有这只股票的深度研报，正在生成 "+code+" …",true)', report_js)
        self.assertIn('setInlineProgress(prog,"DeepSeek 分析中…")', report_js)
        self.assertIn('setLinkLoading(el,true,"生成中…")', report_js)

    def test_run_sh_opens_stable_screen_entrypoint(self):
        run_source = (ROOT / "run.sh").read_text(encoding="utf-8")

        self.assertIn("astock_screen.html", run_source)
        self.assertNotIn('open "http://localhost:$SERVE_PORT/astock_screen_${TS}.html"', run_source)

    def test_server_refresh_mode_maps_to_expected_screener_args(self):
        import server

        handler = server.ScreenerHandler.__new__(server.ScreenerHandler)
        captured = []

        def send_json(data, status=200):
            captured.append((data, status))

        def fake_run(args, **kwargs):
            captured.append(("args", args))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

        handler.send_json = send_json
        old_last_run = server._last_run
        try:
            with patch.object(server.subprocess, "run", side_effect=fake_run):
                server._last_run = {}
                handler._api_refresh("cn", "quotes")
                server._last_run = {}
                handler._api_refresh("cn", "full")

            quote_args = captured[0][1]
            full_args = captured[2][1]
            self.assertIn("--quotes-fresh", quote_args)
            self.assertNotIn("--fresh", quote_args)
            self.assertIn("--fresh", full_args)
        finally:
            server._last_run = old_last_run

    def test_server_uses_threading_http_server_for_parallel_status_requests(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")

        self.assertIn("ThreadingHTTPServer", source)
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", args.port)', source)

    def test_quotes_fresh_bypasses_only_spot_cache(self):
        source = (ROOT / "astock_screener.py").read_text(encoding="utf-8")

        self.assertIn('ap.add_argument("--quotes-fresh"', source)
        self.assertIn("FORCE_SPOT_REFRESH = True", source)
        self.assertIn("ttl_hours=0 if FORCE_SPOT_REFRESH else CONFIG", source)

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

    def test_deepseek_defaults_to_pro_model(self):
        import stock_deep_dive

        self.assertEqual(stock_deep_dive.DEEPSEEK_MODEL, "deepseek-v4-pro")

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
