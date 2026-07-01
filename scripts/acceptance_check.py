#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repeatable local acceptance checks for the stock screener project.

This script keeps expensive/repetitive verification out of ad-hoc AI review:

- unit tests
- syntax and lightweight flake8 gates
- live market data probes
- generated artifact validation
- local HTTP/homepage smoke checks
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
PY = sys.executable
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(cmd: list[str], *, timeout: int | None = None) -> int:
    print("\n$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, timeout=timeout)
    return proc.returncode


def check_cmd(name: str, cmd: list[str], *, timeout: int | None = None) -> bool:
    code = run(cmd, timeout=timeout)
    ok = code == 0
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def run_live_data_tests() -> bool:
    """Run live tests despite unittest's top-level argv parsing."""
    code = (
        "import sys,unittest;"
        "sys.argv.append('--live');"
        "import tests.test_global_data_sources as mod;"
        "suite=unittest.defaultTestLoader.loadTestsFromTestCase(mod.LiveDataSourceTests);"
        "result=unittest.TextTestRunner(verbosity=2).run(suite);"
        "raise SystemExit(0 if result.wasSuccessful() else 1)"
    )
    return check_cmd("live data probes", [PY, "-c", code], timeout=90)


def validate_artifacts(markets: list[str], strict: bool) -> bool:
    from screeners.output_validation import latest_market_result

    ok = True
    print("\n== Artifact validation ==")
    for market in markets:
        status = latest_market_result(str(RESULTS_DIR), market)
        latest = status.get("latest") or status.get("latest_invalid")
        summary = {
            "market": market,
            "status": status["status"],
            "ts": latest.get("ts") if latest else None,
            "row_count": latest.get("row_count") if latest else 0,
            "errors": latest.get("errors") if latest else [],
            "warnings": latest.get("warnings") if latest else [],
        }
        print(json.dumps(summary, ensure_ascii=False))
        if strict and status["status"] != "ready":
            ok = False
    return ok


def wait_for_http(url: str, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return 200 <= resp.status < 500
        except Exception:
            time.sleep(0.3)
    return False


def http_smoke(port: int) -> bool:
    print("\n== HTTP smoke ==")
    proc = subprocess.Popen(
        [PY, "server.py", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_http(f"http://127.0.0.1:{port}/api/status?market=all"):
            print("FAIL server did not become ready")
            return False

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?market=all", timeout=5) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        for market in ("cn", "hk", "us"):
            if market not in status.get("markets", {}):
                print(f"FAIL missing market in status: {market}")
                return False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cbond/status", timeout=5) as resp:
            cbond_status = json.loads(resp.read().decode("utf-8"))
        if "status" not in cbond_status:
            print("FAIL missing cbond status")
            return False

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/screen.html", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        required = [
            'data-refresh="cn"',
            'data-refresh="hk"',
            'data-refresh="us"',
            'id="progress-cn"',
            'id="progress-hk"',
            'id="progress-us"',
            'id="progress-cbond"',
            'data-cbond-refresh="1"',
            "可转债双低",
            "更新五层筛选",
            "themeToggle",
        ]
        missing = [token for token in required if token not in html]
        if missing:
            print("FAIL screen.html missing tokens: " + ", ".join(missing))
            return False
        print("OK   HTTP status + screen.html controls")
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run repeatable project acceptance checks.")
    ap.add_argument("--live", action="store_true", help="Run live network data-source probes.")
    ap.add_argument(
        "--strict-artifacts",
        action="store_true",
        help="Fail if any market has no valid full-market artifact.",
    )
    ap.add_argument(
        "--markets",
        default="cn,hk,us",
        help="Comma-separated markets for artifact validation (default: cn,hk,us).",
    )
    ap.add_argument("--http", action="store_true", help="Run local HTTP homepage smoke check.")
    ap.add_argument("--port", type=int, default=8897, help="Port for --http smoke server.")
    args = ap.parse_args()

    checks = [
        check_cmd("unit tests", [PY, "-m", "unittest", "discover", "-s", "tests", "-v"], timeout=120),
        check_cmd(
            "py_compile",
            [
                PY,
                "-m",
                "py_compile",
                "astock_screener.py",
                "global_screener.py",
                "global_deep_dive.py",
                "cbond_double_low.py",
                "server.py",
                "stock_deep_dive.py",
                "layer4_report.py",
                *[str(p) for p in sorted((ROOT / "screeners").glob("*.py"))],
                *[str(p) for p in sorted((ROOT / "data_sources").glob("*.py"))],
                *[str(p) for p in sorted((ROOT / "backtest").glob("*.py"))],
                *[str(p) for p in sorted((ROOT / "scripts").glob("*.py"))],
            ],
        ),
        check_cmd(
            "flake8 F/E9",
            [
                PY,
                "-m",
                "flake8",
                "--select=F,E9",
                "astock_screener.py",
                "global_screener.py",
                "global_deep_dive.py",
                "cbond_double_low.py",
                "server.py",
                "stock_deep_dive.py",
                "layer4_report.py",
                "screeners",
                "data_sources",
                "backtest",
                "tests",
                "scripts",
            ],
        ),
        check_cmd("run.sh syntax", ["bash", "-n", "run.sh"]),
        check_cmd("git diff whitespace", ["git", "diff", "--check"]),
    ]

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    checks.append(validate_artifacts(markets, args.strict_artifacts))

    if args.live:
        checks.append(run_live_data_tests())
    if args.http:
        checks.append(http_smoke(args.port))

    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
