#!/usr/bin/env python3
"""
本地 HTTP 服务器 —— 支持多市场 (A股/港股/美股) 的五层选股服务。
用法: python3 server.py [--port 8899]

Endpoints:
  /api/status?market=cn|hk|us|all       状态查询
  /api/refresh?market=cn|hk|us&mode=quotes|full  刷新数据
  /api/layer4?market=cn|hk|us&tier=A|B|C        AI 定性分析
  /api/deep?market=cn|hk|us&code=XXXXXX         个股深度研报

向后兼容: 不带 market 参数默认 market=cn (A 股)，保持与旧版 HTML 仪表盘兼容。
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
import threading
import time
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from screeners.output_validation import latest_market_result

# ── 路径 ────────────────────────────────────────────────
WORKDIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORKDIR, "results")
DEEP_DIR = os.path.join(RESULTS_DIR, "deep_dives")
TEMPLATE_SCREEN = os.path.join(WORKDIR, "templates", "screen.html")
DEFAULT_PORT = 8899

# ── 市场配置注册表 ──────────────────────────────────────
# 每个市场定义其文件前缀、脚本路径、代码模式等，方便统一路由。
MARKET_CONFIG = {
    "cn": {
        "label": "A股",
        "html_prefix": "astock_screen",
        "csv_prefix": "astock_screen",
        "md_prefix": "astock_shortlist",
        "stable_name": "astock_screen.html",
        "screen_script": "astock_screener.py",
        "screen_module": "astock_screener",
        "deep_script": "stock_deep_dive.py",
        "code_pattern": re.compile(r"^\d{6}$"),
        "code_desc": "6位数字代码",
        "tier_supported": True,
        "fresh_flags": {
            "full": ["--fresh"],
            "quotes": ["--quotes-fresh"],
        },
    },
    "hk": {
        "label": "港股",
        "html_prefix": "hkstock_screen",
        "csv_prefix": "hkstock_screen",
        "md_prefix": "hkstock_shortlist",
        "stable_name": "hkstock_screen.html",
        "screen_script": "screeners/hk.py",
        "screen_module": "screeners.hk",
        "deep_script": None,  # 港股深度研报暂未实现
        "code_pattern": re.compile(r"^\d{5}$"),
        "code_desc": "5位数字代码",
        "tier_supported": True,
        "fresh_flags": {},  # hk.py 通过模块调用，不传 fresh 参数
    },
    "us": {
        "label": "美股",
        "html_prefix": "usstock_screen",
        "csv_prefix": "usstock_screen",
        "md_prefix": "usstock_shortlist",
        "stable_name": "usstock_screen.html",
        "screen_script": "screeners/us.py",
        "screen_module": "screeners.us",
        "deep_script": None,  # 美股深度研报暂未实现
        "code_pattern": re.compile(r"^[A-Za-z]{1,5}$"),
        "code_desc": "1-5位字母代码",
        "tier_supported": True,
        "fresh_flags": {},  # us.py 通过模块调用，不传 fresh 参数
    },
}

VALID_MARKETS = frozenset(MARKET_CONFIG.keys())
ALL_MARKETS = ["cn", "hk", "us"]

# ── 全局状态 ────────────────────────────────────────────
_last_run: dict[str, float] = {}  # market → last run timestamp

# 任务进度追踪 (每个 market 独立)
_jobs: dict[str, dict] = {m: {"status": "idle", "tier": "", "started": 0, "target": 0, "done": 0} for m in ALL_MARKETS}
_job_lock = threading.Lock()

# 文件名正则
DATE_HTML_RE = re.compile(r"^(astock_screen|hkstock_screen|usstock_screen)_\d{8}\.html$")


# ── 辅助函数 ────────────────────────────────────────────

def _get_market(market_arg: str | None) -> str:
    """解析并验证 market 参数，默认返回 "cn"。"""
    if not market_arg:
        return "cn"
    m = market_arg.strip().lower()
    if m not in VALID_MARKETS:
        return ""  # 调用方检查并返回 400
    return m


def _get_markets(market_arg: str | None) -> list[str]:
    """解析 market 参数，支持 "all"。"""
    if not market_arg:
        return ["cn"]
    m = market_arg.strip().lower()
    if m == "all":
        return list(ALL_MARKETS)
    if m not in VALID_MARKETS:
        return []
    return [m]


def _latest_screen_path(market: str = "cn", results_dir: str | None = None) -> str:
    """返回指定市场最新有效日期版总表 HTML 的绝对路径。"""
    results_dir = results_dir or RESULTS_DIR
    ts = _latest_screen_ts(market)
    if not ts:
        return ""
    cfg = MARKET_CONFIG[market]
    return os.path.join(results_dir, f"{cfg['html_prefix']}_{ts}.html")


def _latest_screen_ts(market: str) -> str:
    """返回指定市场最新有效 CSV 的时间戳 (YYYYMMDD)，无有效数据返回空字符串。"""
    status = latest_market_result(RESULTS_DIR, market)
    latest = status.get("latest") if status.get("status") == "ready" else None
    return latest.get("ts", "") if latest else ""


def _latest_screen_href(market: str) -> str:
    """返回指定市场最新 HTML 的 URL 路径。"""
    ts = _latest_screen_ts(market)
    if not ts:
        return ""
    cfg = MARKET_CONFIG[market]
    return f"{cfg['html_prefix']}_{ts}.html"


def _latest_screen_file_count(market: str) -> int:
    """返回指定市场结果目录下的产出文件数 (HTML+CSV+MD)。"""
    cfg = MARKET_CONFIG[market]
    prefix = cfg["html_prefix"]
    if not os.path.isdir(RESULTS_DIR):
        return 0
    return len([
        f for f in os.listdir(RESULTS_DIR)
        if f.startswith(f"{prefix}_") and DATE_HTML_RE.match(f)
    ])


def _market_status(market: str) -> dict:
    """返回单个市场的状态快照。"""
    result_status = latest_market_result(RESULTS_DIR, market)
    count = _latest_screen_file_count(market)
    cfg = MARKET_CONFIG[market]
    if result_status["status"] == "ready":
        latest = result_status["latest"]
        ts = latest["ts"]
        return {
            "latest_ts": ts,
            "latest_href": f"{cfg['html_prefix']}_{ts}.html",
            "stable_href": cfg["stable_name"],
            "file_count": count,
            "row_count": latest.get("row_count", 0),
            "tier_counts": latest.get("tier_counts", {}),
            "status": "ready",
            "warnings": latest.get("warnings", []),
        }
    if result_status["status"] == "invalid":
        invalid = result_status["latest_invalid"]
        return {
            "latest_ts": None,
            "latest_href": "",
            "stable_href": cfg["stable_name"],
            "file_count": count,
            "status": "invalid",
            "row_count": invalid.get("row_count", 0),
            "latest_invalid_ts": invalid.get("ts"),
            "latest_invalid_href": f"{cfg['html_prefix']}_{invalid.get('ts')}.html",
            "errors": invalid.get("errors", []),
            "warnings": invalid.get("warnings", []),
        }
    return {
        "latest_ts": None,
        "latest_href": "",
        "stable_href": cfg["stable_name"],
        "file_count": count,
        "status": "not_generated",
        "warnings": [f"{cfg['label']}暂无有效筛选结果，请先运行更新五层筛选"],
    }


def _count_deep_existing() -> int:
    """统计 A 股深度研报已有数量 (仅 CN 市场)。"""
    if not os.path.isdir(DEEP_DIR):
        return 0
    data_dir = os.path.join(DEEP_DIR, "data")
    if os.path.isdir(data_dir):
        return len([f for f in os.listdir(data_dir)
                    if len(f) == 11 and f.endswith(".json") and f[:6].isdigit()])
    return len([f for f in os.listdir(DEEP_DIR)
                if len(f) == 11 and f.endswith(".html") and f[:6].isdigit()])


def _deep_json_exists(code: str) -> bool:
    """检查 A 股深度研报 JSON 数据文件是否存在。"""
    return os.path.exists(os.path.join(DEEP_DIR, "data", f"{code}.json"))


def _all_deep_json_exist(codes: list[str]) -> bool:
    """检查给定代码列表中是否全部已有深度研报数据文件。"""
    return bool(codes) and all(_deep_json_exists(c) for c in codes)


def _tier_stock_codes(market: str, tier: str) -> list[str]:
    """读取指定市场最新 CSV，返回指定 tier 的股票代码列表。"""
    cfg = MARKET_CONFIG[market]
    prefix = cfg["csv_prefix"]
    if not os.path.isdir(RESULTS_DIR):
        return []
    csvs = sorted([
        f for f in os.listdir(RESULTS_DIR)
        if f.startswith(f"{prefix}_") and f.endswith(".csv")
    ], reverse=True)
    if not csvs:
        return []
    tier_map = {"A": "A_可买入", "B": "B_优质待跌", "C": "C_接近合格", "all": None}
    target = tier_map.get(tier.upper())
    codes = []
    with open(os.path.join(RESULTS_DIR, csvs[0]), encoding="utf-8-sig") as f:
        import csv
        for r in csv.DictReader(f):
            if target is None or r.get("tier", "") == target:
                c = r.get("code", "")
                if c:
                    codes.append(c)
    return codes


def _count_tier_stocks(market: str, tier: str) -> int:
    """统计指定市场指定 tier 的股票数。"""
    return len(_tier_stock_codes(market, tier))


# ── 统一市场入口页 ──────────────────────────────────────

UNIFIED_SCREEN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全球多市场五层选股</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:light;--bg:#f6f8fb;--text:#172033;--heading:#0f172a;--muted:#64748b;--surface:#fff;--border:#dbe4f0;--green:#16a34a;--red:#dc2626;--link:#2563eb}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0f1115;--text:#e6e8eb;--heading:#f8fafc;--muted:#9aa4b2;--surface:#131820;--border:#232936;--green:#3ddc84;--red:#ff6b6b;--link:#7fb3ff}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px}
header{padding:24px 32px;background:linear-gradient(135deg,#ffffff,#eef4ff);border-bottom:1px solid var(--border)}
:root[data-theme="dark"] header{background:linear-gradient(135deg,#161a22,#0f1115)}
h1{margin:0 0 6px;font-size:22px;color:var(--heading)}
.sub{color:var(--muted);font-size:13px}
main{max-width:900px;margin:32px auto;padding:0 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;text-decoration:none;color:inherit;transition:box-shadow .2s,transform .15s;display:block}
.card:hover{box-shadow:0 4px 16px rgba(15,23,42,.08);transform:translateY(-1px)}
.card h2{margin:0 0 6px;font-size:17px;color:var(--heading)}
.card .status{font-size:12px;margin-top:8px;display:flex;align-items:center;gap:6px}
.card .status .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.card .status .dot.ready{background:var(--green)}
.card .status .dot.missing{background:var(--red)}
.card .meta{font-size:12px;color:var(--muted);margin-top:10px}
footer{padding:20px 32px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}
.theme-btn{position:fixed;top:16px;right:24px;cursor:pointer;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:6px 12px;font-size:12px;z-index:10}
@media(max-width:640px){header{padding:16px 16px 20px}main{padding:0 12px}.grid{grid-template-columns:1fr}}
</style></head><body>
<button class="theme-btn" id="themeToggle" title="切换暗色/亮色主题">🌙 暗色</button>
<header>
<h1>🌍 全球多市场「五层选股流水线」</h1>
<div class="sub">A股 · 港股 · 美股 — 统一量化筛选 + AI 定性分析</div>
</header>
<main>
<div class="grid">
__CARDS__
</div>
</main>
<footer>
数据来源：东方财富公开接口 · SEC EDGAR (XBRL 10-K) · Nasdaq Trader · HKEX 证券主表
· 第0-3层为量化筛选，第4层定性需人工把关
</footer>
<script>
var DATA=__DATA__;
function setTheme(theme){
 document.documentElement.setAttribute("data-theme",theme);
 localStorage.setItem("theme",theme);
 var btn=document.getElementById("themeToggle");
 if(btn)btn.textContent=theme==="dark"?"☀️ 亮色":"🌙 暗色";
}
(function(){
 var s=localStorage.getItem("theme");
 if(!s)s=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
 setTheme(s);
 document.getElementById("themeToggle").addEventListener("click",function(){
  setTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark");
 });
})();
</script></body></html>"""


def _build_unified_screen_html() -> bytes:
    """构建多市场统一入口页 HTML。"""
    if os.path.exists(TEMPLATE_SCREEN):
        with open(TEMPLATE_SCREEN, encoding="utf-8") as f:
            return f.read().encode("utf-8")

    cards_parts = []
    market_data = {}
    for m in ALL_MARKETS:
        cfg = MARKET_CONFIG[m]
        status = _market_status(m)
        ts = status.get("latest_ts", "")
        emoji = {"cn": "🇨🇳", "hk": "🇭🇰", "us": "🇺🇸"}.get(m, "")
        exchange = {"cn": "沪深", "hk": "港交所", "us": "NYSE/NASDAQ"}.get(m, "")
        card = f"""
<a class="card" href="{cfg['stable_name']}">
  <h2>{emoji} {cfg['label']}</h2>
  <div class="meta">五层量化筛选 + 交互式仪表盘</div>
  <div class="meta">交易所: {exchange}</div>
  <div class="status">
    <span class="dot {'ready' if ts else 'missing'}"></span>
    {'已生成 · ' + ts if ts else '尚未生成'}
    {' · ' + str(status['file_count']) + ' 份' if status['file_count'] else ''}
  </div>
</a>"""
        cards_parts.append(card)
        market_data[m] = {"ts": ts, "label": cfg["label"]}

    html = UNIFIED_SCREEN_HTML.replace("__CARDS__", "\n".join(cards_parts))
    html = html.replace("__DATA__", json.dumps(market_data, ensure_ascii=False))
    return html.encode("utf-8")


# ── HTTP 处理器 ────────────────────────────────────────

class ScreenerHandler(SimpleHTTPRequestHandler):
    """多市场五层选股 HTTP 处理器。

    保留对旧版 A 股仪表盘的向后兼容：不带 market 参数的请求默认
    路由到 A 股 (market=cn)。
    """

    def __init__(self, *args, **kwargs):
        # 默认 serve results/ 目录下的静态文件
        super().__init__(*args, directory=RESULTS_DIR, **kwargs)

    # ── 路由分发 ─────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]

        # ── API 端点 ──
        if self.path.startswith("/api/"):
            self._handle_api()
            return

        # ── 统一入口页 ──
        if path in ("/screen.html", "/", "/index.html"):
            self._serve_unified_screen()
            return

        # ── 市场稳定入口 → 重定向到最新日期页 ──
        for m, cfg in MARKET_CONFIG.items():
            if path == f"/{cfg['stable_name']}":
                self._serve_market_stable(m)
                return

        # ── 旧版路径兼容: /astock_screen.html → A 股最新 ──
        if path == "/astock_screen.html":
            self._serve_market_stable("cn")
            return

        # ── 旧版 deep_dives 重定向 ──
        if self._redirect_legacy_deep_link():
            return

        # ── 其他 → 静态文件服务 ──
        super().do_GET()

    def do_HEAD(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/screen.html"):
            self._serve_unified_screen(head_only=True)
            return
        for m, cfg in MARKET_CONFIG.items():
            if path == f"/{cfg['stable_name']}":
                self._serve_market_stable(m, head_only=True)
                return
        if path == "/astock_screen.html":
            self._serve_market_stable("cn", head_only=True)
            return
        super().do_HEAD()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._handle_api()
        else:
            self.send_error(405)

    # ── 统一入口页 ──────────────────────────────────

    def _serve_unified_screen(self, head_only: bool = False):
        """返回多市场统一入口页 /screen.html。"""
        body = _build_unified_screen_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ── 市场稳定入口 (重定向到最新日期 HTML) ────────

    def _serve_market_stable(self, market: str, head_only: bool = False):
        """返回市场稳定入口页，自动重定向到最新日期总表 HTML。"""
        cfg = MARKET_CONFIG[market]
        status = _market_status(market)
        latest_ts = status.get("latest_ts")
        if latest_ts:
            latest_name = f"{cfg['html_prefix']}_{latest_ts}.html"
            target_js = json.dumps(latest_name, ensure_ascii=False)
            refresh_meta = f'<meta http-equiv="refresh" content="0; url={latest_name}">'
            redirect_js = f"<script>location.replace({target_js})</script>"
            message = f'正在打开最新总表：<a href="{latest_name}">{latest_name}</a>'
        else:
            latest_name = ""
            refresh_meta = ""
            redirect_js = ""
            reasons = status.get("errors") or status.get("warnings") or ["暂无有效正式筛选结果"]
            reason_html = "".join(f"<li>{r}</li>" for r in reasons)
            invalid_link = ""
            if status.get("latest_invalid_href"):
                invalid = status["latest_invalid_href"]
                invalid_link = (
                    f'<p>最近一次产物未通过校验：'
                    f'<a href="{invalid}">{invalid}</a>，仅供排查，不作为正式结果。</p>'
                )
            message = (
                f"<p>{cfg['label']}暂无可验收的正式结果。</p>"
                f"<ul>{reason_html}</ul>{invalid_link}"
            )

        stable_name = cfg["stable_name"]
        label = cfg["label"]

        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<title>{label}五层选股固定入口</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#172033}}
.box{{max-width:560px;margin:14vh auto;padding:24px;background:#fff;border:1px solid #dbe4f0;border-radius:8px}}
a{{color:#2563eb}}
.back{{margin-top:12px;font-size:13px}}
@media(prefers-color-scheme:dark){{body{{background:#0f1115;color:#e6e8eb}}.box{{background:#131820;border-color:#232936}}a{{color:#7fb3ff}}}}
</style></head><body>
<div class="box">
<h1>{label}五层选股固定入口</h1>
{message}
<p>日期页继续作为后台历史产物保留；日常请访问 <code>{stable_name}</code>。</p>
<div class="back"><a href="/screen.html">← 返回全市场总览</a></div>
</div>
{redirect_js}
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ── API 路由分发 ─────────────────────────────────

    def _handle_api(self):
        """解析 API 路径和查询参数，分发到具体处理器。"""
        path = self.path.split("?")[0]
        qs: dict[str, str] = {}
        if "?" in self.path:
            qs = {k: v[0] for k, v in parse_qs(self.path.split("?")[1]).items()}

        try:
            if path == "/api/refresh":
                market = _get_market(qs.get("market", ""))
                if not market:
                    self.send_json({"error": f"无效 market 参数: {qs.get('market')}，有效值: cn, hk, us"}, status=400)
                    return
                mode = qs.get("mode", "quotes")
                if qs.get("fresh") == "1":
                    mode = "full"
                self._api_refresh(market, mode)

            elif path == "/api/deep":
                market = _get_market(qs.get("market", ""))
                if not market:
                    self.send_json({"error": f"无效 market 参数: {qs.get('market')}，有效值: cn, hk, us"}, status=400)
                    return
                self._api_deep(market, qs.get("code", ""))

            elif path == "/api/layer4":
                market = _get_market(qs.get("market", ""))
                if not market:
                    self.send_json({"error": f"无效 market 参数: {qs.get('market')}，有效值: cn, hk, us"}, status=400)
                    return
                self._api_layer4(market, qs.get("tier", "A"))

            elif path == "/api/status":
                markets = _get_markets(qs.get("market", ""))
                if not markets:
                    self.send_json({"error": f"无效 market 参数: {qs.get('market')}，有效值: cn, hk, us, all"}, status=400)
                    return
                self._api_status(markets)

            else:
                self.send_json({"error": "unknown endpoint"}, status=404)

        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    # ── 旧版 deep_dives 重定向 ──────────────────────

    def _redirect_legacy_deep_link(self) -> bool:
        """旧版 /deep_dives/XXXXXX.html → /deep_dives/report.html?code=XXXXXX。"""
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

    # ── API: /api/refresh ────────────────────────────

    def _api_refresh(self, market: str, mode: str):
        """执行指定市场的数据刷新。

        Args:
            market: cn, hk, us.
            mode: "quotes" (仅行情) 或 "full" (全量重新抓取)。
        """
        global _last_run
        now = time.time()
        last = _last_run.get(market, 0)
        if now - last < 5:
            self.send_json({
                "done": False, "cached": True,
                "market": market,
                "msg": "冷却中，5秒后再试",
            })
            return

        cfg = MARKET_CONFIG[market]
        _last_run[market] = now

        label_map = {"quotes": "刷新行情", "full": "更新五层筛选"}
        requested_mode = mode
        effective_mode = mode
        pre_warnings: list[str] = []
        if market != "cn" and mode == "quotes":
            effective_mode = "full"
            pre_warnings.append(
                f"{cfg['label']}暂不支持只刷新行情，已改为更新五层筛选"
            )
        fresh_label = label_map.get(effective_mode, f"刷新({effective_mode})")

        args = [sys.executable, os.path.join(WORKDIR, cfg["screen_script"])]

        # 市场特定参数
        if effective_mode in cfg.get("fresh_flags", {}):
            args.extend(cfg["fresh_flags"][effective_mode])
        # HK/US: 直接运行 screener 脚本即可 (数据源内部有缓存逻辑)

        try:
            result = subprocess.run(
                args, capture_output=True, text=True,
                timeout=300, cwd=WORKDIR,
            )
        except subprocess.TimeoutExpired:
            self.send_json({
                "error": f"{fresh_label}超时(>300s)",
                "market": market,
                "mode": requested_mode,
                "effective_mode": effective_mode,
                "warnings": pre_warnings,
            }, status=504)
            return
        except FileNotFoundError:
            self.send_json({
                "error": f"脚本未找到: {cfg['screen_script']}",
                "market": market,
                "source": cfg["screen_script"],
            }, status=500)
            return

        latest_ts = _latest_screen_ts(market)
        status_info = _market_status(market)
        data = {
            "done": result.returncode == 0,
            "market": market,
            "latest_ts": latest_ts,
            "latest_href": _latest_screen_href(market),
            "stable_href": cfg["stable_name"],
            "progress": None,
            "warnings": list(pre_warnings),
            "mode": requested_mode,
            "effective_mode": effective_mode,
            "status": status_info.get("status"),
        }
        if result.returncode != 0:
            data["error"] = f"{fresh_label}失败 (exit code={result.returncode})"
            data["stderr_tail"] = result.stderr[-500:] if result.stderr else ""
            data["warnings"].append(f"{cfg['label']} {fresh_label}返回非零退出码")
            status = 500
        elif status_info.get("status") != "ready":
            data["done"] = False
            data["error"] = f"{cfg['label']}产物未通过正式验收"
            data["errors"] = status_info.get("errors", [])
            data["latest_invalid_ts"] = status_info.get("latest_invalid_ts")
            data["latest_invalid_href"] = status_info.get("latest_invalid_href")
            data["row_count"] = status_info.get("row_count", 0)
            data["warnings"].extend(status_info.get("warnings", []))
            status = 422
        else:
            data["row_count"] = status_info.get("row_count", 0)
            data["tier_counts"] = status_info.get("tier_counts", {})
            data["warnings"].extend(status_info.get("warnings", []))
            status = 200

        self.send_json(data, status=status)

    # ── API: /api/deep ───────────────────────────────

    def _api_deep(self, market: str, code: str):
        """为指定股票生成深度研报。

        Args:
            market: cn (当前仅 A 股支持深度研报)。
            code: 股票代码。
        """
        cfg = MARKET_CONFIG[market]

        # ── 代码格式校验 ──
        if not code:
            self.send_json({
                "error": "缺少 code 参数", "market": market,
            }, status=400)
            return

        if not cfg["code_pattern"].match(code.strip()):
            self.send_json({
                "error": f"无效股票代码格式 (期望: {cfg['code_desc']})",
                "market": market, "code": code,
            }, status=400)
            return

        # ── 市场支持检查 ──
        if cfg["deep_script"] is None:
            self.send_json({
                "error": f"{cfg['label']}市场暂不支持深度研报生成",
                "market": market,
            }, status=501)
            return

        args = [
            sys.executable,
            os.path.join(WORKDIR, cfg["deep_script"]),
            "--code", code.strip(),
        ]
        if market == "cn" and _deep_json_exists(code.strip()):
            args.append("--ai-only")

        try:
            result = subprocess.run(
                args, capture_output=True, text=True,
                timeout=180, cwd=WORKDIR,
            )
        except subprocess.TimeoutExpired:
            self.send_json({
                "error": f"{code} 研报生成超时(>180s)",
                "market": market, "code": code,
            }, status=504)
            return

        data = {
            "done": result.returncode == 0,
            "market": market,
            "code": code,
            "output_tail": result.stdout[-500:] if result.stdout else "",
            "warnings": [],
        }
        if result.returncode != 0:
            data["error"] = f"{code} 研报生成失败 (exit code={result.returncode})"
            data["stderr_tail"] = result.stderr[-500:] if result.stderr else ""
            self.send_json(data, status=500)
            return

        self.send_json(data)

    # ── API: /api/layer4 ──────────────────────────────

    def _api_layer4(self, market: str, tier: str):
        """对指定市场的 Tier X 全部标的运行 AI 定性分析（异步 + 进度追踪）。

        Args:
            market: cn (当前仅 A 股支持)。
            tier: A, B, C。
        """
        cfg = MARKET_CONFIG[market]

        # ── 市场支持检查 ──
        if not cfg.get("tier_supported") or cfg["deep_script"] is None:
            self.send_json({
                "error": f"{cfg['label']}市场暂不支持批量 AI 定性分析",
                "market": market,
            }, status=501)
            return

        tier = tier.upper()
        if tier not in ("A", "B", "C"):
            tier = "A"

        # ── 并发控制 ──
        job_key = market
        global _jobs
        with _job_lock:
            if _jobs.get(job_key, {}).get("status") == "running":
                self.send_json({
                    "done": False,
                    "market": market,
                    "msg": "已有任务运行中",
                    "tier": _jobs[job_key]["tier"],
                })
                return

        # ── 获取标的列表 ──
        tier_codes = _tier_stock_codes(market, tier)
        total = len(tier_codes)

        if total == 0:
            self.send_json({
                "done": False,
                "market": market,
                "msg": f"Tier {tier} 无可用标的 (请先运行选股)",
            })
            return

        before = _count_deep_existing()

        with _job_lock:
            _jobs[job_key] = {
                "status": "running", "tier": tier,
                "started": time.time(), "target": total,
                "done": 0, "before": before,
            }

        def _run_layer4():
            exit_code = 0
            try:
                args = [
                    sys.executable,
                    os.path.join(WORKDIR, cfg["deep_script"]),
                    "--tier", tier,
                    "--parallel", "20",
                    "--ai-concurrency", "20",
                ]
                if _all_deep_json_exist(tier_codes):
                    args.append("--ai-only")
                result = subprocess.run(
                    args, capture_output=True, text=True,
                    timeout=600, cwd=WORKDIR,
                )
                exit_code = result.returncode
            except Exception:
                exit_code = 1
            with _job_lock:
                _jobs[job_key]["status"] = "done" if exit_code == 0 else "failed"
                _jobs[job_key]["done"] = _jobs[job_key]["target"] if exit_code == 0 else _jobs[job_key]["done"]
                _jobs[job_key]["exit_code"] = exit_code
                _jobs[job_key]["market"] = market

        threading.Thread(target=_run_layer4, daemon=True).start()

        self.send_json({
            "done": False,
            "market": market,
            "msg": f"已启动 {cfg['label']} Tier {tier} 分析（{total} 只）",
            "tier": tier,
            "total": total,
        })

    # ── API: /api/status ──────────────────────────────

    def _api_status(self, markets: list[str]):
        """返回指定市场(们)的状态。

        单市场响应格式 (向后兼容):
          {"done": true, "market": "cn", "latest_ts": "...",
           "latest_href": "...", "stable_href": "...",
           "progress": null, "warnings": []}

        多市场 (all) 响应格式:
          {"markets": {"cn": {...}, "hk": {...}, "us": {...}},
           "progress": null}
        """
        with _job_lock:
            jobs_snapshot = {k: dict(v) for k, v in _jobs.items()}

        # ── 多市场模式 ──
        if len(markets) > 1:
            result: dict = {"markets": {}, "progress": None}
            for m in markets:
                result["markets"][m] = _market_status(m)
            self.send_json(result)
            return

        # ── 单市场模式 (向后兼容旧版 HTML 仪表盘) ──
        market = markets[0]
        cfg = MARKET_CONFIG[market]
        status_info = _market_status(market)
        ts = status_info.get("latest_ts")
        deep_count = _count_deep_existing() if market == "cn" else 0

        # ── 进度追踪 ──
        progress = None
        job = jobs_snapshot.get(market, {})
        if job.get("status") == "running":
            current = deep_count
            delta = current - job.get("before", 0)
            job["done"] = max(job.get("done", 0), delta)
            elapsed = time.time() - job["started"]
            if job["done"] > 0 and elapsed > 2:
                eta = int((job["target"] - job["done"]) * elapsed / job["done"]) if job["done"] > 0 else 0
                eta_str = f"约{eta//60}分{eta%60}秒" if eta > 0 else "<1分钟"
            else:
                eta_str = "计算中…"
            progress = {
                "tier": job["tier"],
                "done": job["done"],
                "target": job["target"],
                "elapsed": int(elapsed),
                "eta": eta_str,
            }
        elif job.get("status") == "done":
            progress = {"done": True, "tier": job["tier"]}
            with _job_lock:
                _jobs[market]["status"] = "idle"
        elif job.get("status") == "failed":
            progress = {
                "done": False, "failed": True,
                "tier": job["tier"],
                "exit_code": job.get("exit_code", 1),
            }
            with _job_lock:
                _jobs[market]["status"] = "idle"

        warnings = list(status_info.get("warnings", []))
        errors = list(status_info.get("errors", []))
        if not ts and not warnings and not errors:
            warnings.append(f"{cfg['label']}暂无筛选结果，请先运行选股")

        data = {
            "done": status_info.get("status") == "ready",
            "market": market,
            "latest_ts": ts,
            "latest_href": status_info.get("latest_href", ""),
            "stable_href": cfg["stable_name"],
            "progress": progress,
            "warnings": warnings,
            "errors": errors,
            "status": status_info.get("status"),
            "file_count": status_info.get("file_count", 0),
            "row_count": status_info.get("row_count", 0),
            "tier_counts": status_info.get("tier_counts", {}),
        }
        if status_info.get("latest_invalid_ts"):
            data["latest_invalid_ts"] = status_info.get("latest_invalid_ts")
            data["latest_invalid_href"] = status_info.get("latest_invalid_href")
        if market == "cn":
            data["deep_count"] = deep_count

        self.send_json(data)

    # ── JSON 响应工具 ────────────────────────────────

    def send_json(self, data: dict, status: int = 200):
        """发送 JSON 响应。"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        """抑制默认日志输出 (安静模式)。"""
        pass


# ── 启动入口 ──────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="多市场五层选股 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认 {DEFAULT_PORT})")
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), ScreenerHandler)
    except OSError as e:
        if "Address already in use" in str(e) or "Address already in use" in repr(e):
            print(f"❌ 端口 {args.port} 已被占用，尝试：lsof -ti:{args.port} | xargs kill")
        else:
            print(f"❌ 启动失败: {e}")
        sys.exit(1)

    print(f"🚀 多市场五层选股服务已启动: http://localhost:{args.port}")
    print(f"   全市场总览: http://localhost:{args.port}/screen.html")
    print()
    for m in ALL_MARKETS:
        cfg = MARKET_CONFIG[m]
        print(f"   {cfg['label']}:")
        print(f"     总表:   http://localhost:{args.port}/{cfg['stable_name']}")
        print(f"     状态:   http://localhost:{args.port}/api/status?market={m}")
        print(f"     刷新:   http://localhost:{args.port}/api/refresh?market={m}&mode=quotes")
    print()
    print(f"   全市场状态: http://localhost:{args.port}/api/status?market=all")
    print(f"   A股研报:    http://localhost:{args.port}/api/deep?market=cn&code=000423")
    print(f"   A股定性:    http://localhost:{args.port}/api/layer4?market=cn&tier=A")
    print("   Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
