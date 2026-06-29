#!/usr/bin/env python3
"""
本地 HTTP 服务器 —— 让 HTML 中的「刷新数据」「生成研报」「AI分析」按钮真正可用。
用法: python3 server.py [--port 8899]
"""
import os, sys, json, subprocess, threading, time
from http.server import HTTPServer, SimpleHTTPRequestHandler

WORKDIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORKDIR, "results")
DEEP_DIR = os.path.join(RESULTS_DIR, "deep_dives")
DEFAULT_PORT = 8899

_last_run = 0

# ── 任务进度追踪 ──
_job = {"status": "idle", "tier": "", "started": 0, "target": 0, "done": 0}
_job_lock = threading.Lock()


def _count_existing():
    """统计各 tier 已有研报数。"""
    if not os.path.isdir(DEEP_DIR):
        return 0
    data_dir = os.path.join(DEEP_DIR, "data")
    if os.path.isdir(data_dir):
        return len([f for f in os.listdir(data_dir)
                    if len(f) == 11 and f.endswith(".json") and f[:6].isdigit()])
    return len([f for f in os.listdir(DEEP_DIR)
                if len(f) == 11 and f.endswith(".html") and f[:6].isdigit()])


def _count_tier_stocks(tier):
    """读取最新 CSV，统计指定 tier 的股票数。"""
    return len(_tier_stock_codes(tier))


def _tier_stock_codes(tier):
    """读取最新 CSV，返回指定 tier 的股票代码。"""
    csvs = sorted([f for f in os.listdir(RESULTS_DIR)
                   if f.startswith("astock_screen_") and f.endswith(".csv")],
                  reverse=True)
    if not csvs:
        return []
    tier_map = {"A": "A_可买入", "B": "B_优质待跌", "C": "C_接近合格", "all": None}
    target = tier_map.get(tier)
    codes = []
    with open(os.path.join(RESULTS_DIR, csvs[0]), encoding="utf-8-sig") as f:
        import csv
        for r in csv.DictReader(f):
            if target is None or r.get("tier", "") == target:
                codes.append(r.get("code", ""))
    return [c for c in codes if len(c) == 6 and c.isdigit()]


def _deep_json_exists(code):
    return os.path.exists(os.path.join(DEEP_DIR, "data", f"{code}.json"))


def _all_deep_json_exist(codes):
    return bool(codes) and all(_deep_json_exists(code) for code in codes)


class ScreenerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=RESULTS_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._handle_api()
        elif self._redirect_legacy_deep_link():
            return
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._handle_api()
        else:
            self.send_error(405)

    def _handle_api(self):
        from urllib.parse import parse_qs
        path = self.path.split("?")[0]
        qs = {k: v[0] for k, v in parse_qs(self.path.split("?")[1]).items()} if "?" in self.path else {}

        try:
            if path == "/api/refresh":
                mode = qs.get("mode", "quotes")
                if qs.get("fresh") == "1":
                    mode = "full"
                self._api_refresh(mode)
            elif path == "/api/deep":
                self._api_deep(qs.get("code", ""))
            elif path == "/api/layer4":
                self._api_layer4(qs.get("tier", "A"))
            elif path == "/api/status":
                self._api_status()
            else:
                self.send_json({"error": "unknown endpoint"})
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def _redirect_legacy_deep_link(self):
        path = self.path.split("?")[0]
        prefix = "/deep_dives/"
        if not path.startswith(prefix) or not path.endswith(".html"):
            return False
        code = path[len(prefix):-5]
        if len(code) != 6 or not code.isdigit():
            return False
        self.send_response(302)
        self.send_header("Location", f"/deep_dives/report.html?code={code}")
        self.end_headers()
        return True

    def _api_refresh(self, mode):
        global _last_run
        now = time.time()
        if now - _last_run < 5:
            self.send_json({"done": False, "cached": True, "msg": "冷却中，5秒后再试"})
            return
        args = [sys.executable, os.path.join(WORKDIR, "astock_screener.py")]
        if mode == "quotes":
            args.append("--quotes-fresh")
            label = "刷新行情"
        elif mode == "full":
            args.append("--fresh")
            label = "更新五层筛选"
        else:
            self.send_json({"error": "无效刷新模式"}, status=400)
            return
        _last_run = now
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=120, cwd=WORKDIR)
            self._send_process_result(result, label, {"mode": mode})
        except subprocess.TimeoutExpired:
            self.send_json({"error": f"{label}超时(>120s)", "mode": mode}, status=504)

    def _api_deep(self, code):
        if not code or len(code) != 6 or not code.isdigit():
            self.send_json({"error": "无效代码"}, status=400)
            return
        args = [sys.executable, os.path.join(WORKDIR, "stock_deep_dive.py"), "--code", code]
        if _deep_json_exists(code):
            args.append("--ai-only")
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=120, cwd=WORKDIR)
            self._send_process_result(result, f"{code} 研报生成", {"code": code})
        except subprocess.TimeoutExpired:
            self.send_json({"error": f"{code} 研报生成超时"}, status=504)

    def _api_layer4(self, tier):
        """对指定 tier 的全部标的运行 DeepSeek AI 定性分析（异步 + 进度追踪）"""
        global _job
        with _job_lock:
            if _job["status"] == "running":
                self.send_json({"done": False, "msg": "已有任务运行中",
                               "tier": _job["tier"], "progress": f"{_job['done']}/{_job['target']}"})
                return

        tier = tier.upper()
        if tier not in ("A", "B", "C"):
            tier = "A"
        tier_codes = _tier_stock_codes(tier)
        total = len(tier_codes)
        before = _count_existing()

        with _job_lock:
            _job = {"status": "running", "tier": tier, "started": time.time(),
                    "target": total, "done": 0, "before": before}

        def run():
            global _job
            exit_code = 0
            try:
                args = [sys.executable, os.path.join(WORKDIR, "stock_deep_dive.py"),
                        "--tier", tier, "--parallel", "20", "--ai-concurrency", "20"]
                if _all_deep_json_exist(tier_codes):
                    args.append("--ai-only")
                result = subprocess.run(
                    args,
                    capture_output=True, text=True, timeout=600, cwd=WORKDIR)
                exit_code = result.returncode
            except Exception:
                exit_code = 1
            with _job_lock:
                _job["status"] = "done" if exit_code == 0 else "failed"
                _job["done"] = _job["target"] if exit_code == 0 else _job["done"]
                _job["exit_code"] = exit_code

        threading.Thread(target=run, daemon=True).start()
        self.send_json({"done": False, "msg": f"已启动 Tier {tier} 分析（{total} 只）",
                       "tier": tier, "total": total})

    def _api_status(self):
        global _job
        csvs = sorted([f for f in os.listdir(RESULTS_DIR)
                       if f.startswith("astock_screen_") and f.endswith(".csv")],
                      reverse=True)
        latest_ts = csvs[0].replace("astock_screen_", "").replace(".csv", "") if csvs else ""
        deep_count = _count_existing()

        # 进度：按文件增量估算
        progress = None
        with _job_lock:
            if _job["status"] == "running":
                current = _count_existing()
                delta = current - _job.get("before", 0)
                _job["done"] = max(_job["done"], delta)

                # 估算完成数（每只股票生成1个HTML = 1个完成）
                elapsed = time.time() - _job["started"]
                if _job["done"] > 0 and elapsed > 2:
                    eta = int((_job["target"] - _job["done"]) * elapsed / _job["done"])
                    eta_str = f"约{eta//60}分{eta%60}秒" if eta > 0 else "<1分钟"
                else:
                    eta_str = "计算中…"
                progress = {
                    "tier": _job["tier"],
                    "done": _job["done"],
                    "target": _job["target"],
                    "elapsed": int(elapsed),
                    "eta": eta_str,
                }
            elif _job["status"] == "done":
                progress = {"done": True, "tier": _job["tier"]}
                _job["status"] = "idle"
            elif _job["status"] == "failed":
                progress = {"done": False, "failed": True, "tier": _job["tier"],
                            "exit_code": _job.get("exit_code", 1)}
                _job["status"] = "idle"

        self.send_json({"latest_ts": latest_ts, "deep_count": deep_count,
                       "progress": progress})

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_process_result(self, result, label, extra=None):
        data = {
            "done": result.returncode == 0,
            "output": result.stdout[-500:],
            "stderr": result.stderr[-500:],
            "exit_code": result.returncode,
        }
        if extra:
            data.update(extra)
        if result.returncode != 0:
            data["error"] = f"{label}失败"
            self.send_json(data, status=500)
            return
        self.send_json(data)

    def log_message(self, format, *args):
        pass


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    os.makedirs(RESULTS_DIR, exist_ok=True)
    try:
        server = HTTPServer(("127.0.0.1", args.port), ScreenerHandler)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"❌ 端口 {args.port} 已被占用，尝试：lsof -ti:{args.port} | xargs kill")
        else:
            print(f"❌ 启动失败: {e}")
        sys.exit(1)
    print(f"🚀 本地服务已启动: http://localhost:{args.port}")
    print(f"   状态: http://localhost:{args.port}/api/status")
    print(f"   刷新: http://localhost:{args.port}/api/refresh?fresh=1")
    print(f"   研报: http://localhost:{args.port}/api/deep?code=000423")
    print(f"   分析: http://localhost:{args.port}/api/layer4?tier=A")
    print("   Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
