import csv
import json
import shutil
import subprocess
import sys
import tempfile
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
            self.assertTrue((tmp / "results" / "astock_layer4_final_20260627.html").exists())


if __name__ == "__main__":
    unittest.main()
