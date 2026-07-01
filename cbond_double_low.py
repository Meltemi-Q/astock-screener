#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convertible-bond double-low screener.

Strategy summary:
    double_low = bond price + conversion premium ratio

The script builds a full-market convertible-bond universe from Eastmoney public
endpoints, applies conservative risk filters, then writes repeatable CSV/HTML/MD
artifacts under results/.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
from datetime import date, datetime

from data_sources.convertible_bonds import (
    build_convertible_bond_universe,
    fetch_jisilu_low_sample,
    fnum,
    parse_ymd,
)


WORKDIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORKDIR, "results")
CBOND_DEEP_DIR = os.path.join(RESULTS_DIR, "cbond_deep")
CBOND_DEEP_TEMPLATE_DIR = os.path.join(WORKDIR, "templates", "cbond_deep")

RATING_ORDER = {
    "AAA": 9,
    "AA+": 8,
    "AA": 7,
    "AA-": 6,
    "A+": 5,
    "A": 4,
    "A-": 3,
    "BBB+": 2,
    "BBB": 1,
    "BBB-": 0,
    "BB": -1,
    "B": -2,
    "CCC": -3,
    "CC": -4,
    "C": -5,
}

CSV_FIELDS = [
    "rank",
    "status",
    "code",
    "name",
    "stock_code",
    "stock_name",
    "price",
    "premium_rt",
    "double_low",
    "rating",
    "remaining_scale",
    "remaining_years",
    "change_pct",
    "maturity_date",
    "convert_price",
    "convert_value",
    "stock_price",
    "pb",
    "turnover",
    "risk_reasons",
    "source",
]


def rating_value(rating: str) -> int:
    return RATING_ORDER.get((rating or "").upper(), -99)


def money2(value):
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def pct(value):
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_date(value):
    d = parse_ymd(value)
    return d.isoformat() if d else "-"


def ensure_cbond_deep_shell():
    """Copy the shared convertible-bond detail shell next to results."""
    files = {
        "report.html": os.path.join(CBOND_DEEP_TEMPLATE_DIR, "report.html"),
        os.path.join("assets", "cbond_deep.css"): os.path.join(CBOND_DEEP_TEMPLATE_DIR, "assets", "cbond_deep.css"),
        os.path.join("assets", "cbond_deep.js"): os.path.join(CBOND_DEEP_TEMPLATE_DIR, "assets", "cbond_deep.js"),
    }
    for rel, src in files.items():
        if not os.path.exists(src):
            continue
        dst = os.path.join(CBOND_DEEP_DIR, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)


def is_future_date(value, today: date) -> bool:
    d = parse_ymd(value)
    return bool(d and d >= today)


def has_redeem_execution(record, today: date) -> bool:
    """Return True when a future redemption execution/record date is visible."""
    for key in (
        "execute_start_date_sh",
        "execute_start_date_hs",
        "execute_end_date",
        "record_date_sh",
    ):
        if is_future_date(record.get(key), today):
            return True
    return False


def risk_reasons(record, args, today: date) -> list[str]:
    """Hard exclusion reasons for the double-low basket."""
    reasons = []
    name = (record.get("name") or "") + (record.get("stock_name") or "")
    price = fnum(record.get("price"))
    premium = fnum(record.get("premium_rt"))
    remaining_scale = fnum(record.get("remaining_scale"))
    remaining_years = fnum(record.get("remaining_years"))
    rating = record.get("rating") or ""

    if not record.get("has_quote_board"):
        reasons.append("不在实时转债行情板块/可能未正常交易")
    if price is None or price <= 0:
        reasons.append("无有效现价")
    if premium is None:
        reasons.append("无转股溢价率")
    if parse_ymd(record.get("listing_date")) is None or parse_ymd(record.get("listing_date")) > today:
        reasons.append("未上市/待上市")
    delist_date = parse_ymd(record.get("delist_date"))
    if delist_date and delist_date <= today:
        reasons.append("已退市/摘牌")
    if "退" in name or name.upper().startswith("R") or "ST" in name.upper():
        reasons.append("正股或转债含 ST/退/R 标记")
    if rating_value(rating) < rating_value(args.min_rating):
        reasons.append(f"评级低于{args.min_rating}")
    if remaining_scale is None:
        reasons.append("无剩余规模")
    elif remaining_scale < args.min_scale:
        reasons.append(f"剩余规模<{args.min_scale:g}亿")
    if remaining_years is None:
        reasons.append("无剩余期限")
    elif remaining_years < args.min_years:
        reasons.append(f"剩余期限<{args.min_years:g}年")
    if has_redeem_execution(record, today):
        reasons.append("存在未来赎回/摘牌执行日")
    return reasons


def soft_reasons(record, args) -> list[str]:
    """Reasons a clean bond is only watchlist, not buy-candidate."""
    reasons = []
    price = fnum(record.get("price"))
    premium = fnum(record.get("premium_rt"))
    double_low = fnum(record.get("double_low"))
    if price is not None and price > args.max_price:
        reasons.append(f"价格>{args.max_price:g}")
    if premium is not None and premium > args.max_premium:
        reasons.append(f"溢价率>{args.max_premium:g}%")
    if double_low is not None and double_low > args.max_double_low:
        reasons.append(f"双低值>{args.max_double_low:g}")
    return reasons


def classify_records(records, args, today: date):
    """Attach rank/status/reasons and sort by double-low value."""
    usable = []
    for record in records:
        record = dict(record)
        hard = risk_reasons(record, args, today)
        soft = soft_reasons(record, args) if not hard else []
        if hard:
            status = "剔除"
            reasons = hard
        elif soft:
            status = "观察"
            reasons = soft
        else:
            status = "买入候选"
            reasons = []
        record["status"] = status
        record["risk_reasons"] = "；".join(reasons)
        usable.append(record)

    usable.sort(key=lambda r: (
        9999 if r.get("double_low") is None else float(r.get("double_low")),
        9999 if r.get("price") is None else float(r.get("price")),
        r.get("code", ""),
    ))
    for i, record in enumerate(usable, 1):
        record["rank"] = i
    return usable


def summarize(records):
    total = len(records)
    buy = [r for r in records if r["status"] == "买入候选"]
    watch = [r for r in records if r["status"] == "观察"]
    reject = [r for r in records if r["status"] == "剔除"]
    low_pool = [r for r in records if r.get("double_low") is not None][:30]
    return {
        "total": total,
        "buy_count": len(buy),
        "watch_count": len(watch),
        "reject_count": len(reject),
        "low_pool_count": len(low_pool),
        "buy": buy,
        "watch": watch,
        "reject": reject,
        "low_pool": low_pool,
    }


def write_csv(path, records):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in records:
            w.writerow(r)


def rows_html(rows, limit=None):
    rows = rows[:limit] if limit else rows
    out = []
    for r in rows:
        cls = {"买入候选": "buy", "观察": "watch", "剔除": "reject"}.get(r.get("status"), "")
        out.append(
            "<tr class=\"%s\">"
            "<td>%s</td><td><a href=\"cbond_deep/report.html?code=%s\">%s</a></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td class=\"reason\">%s</td>"
            "</tr>" % (
                cls,
                r.get("rank", ""),
                html.escape(str(r.get("code", ""))),
                html.escape(str(r.get("code", ""))),
                html.escape(str(r.get("name", ""))),
                html.escape(str(r.get("stock_name", ""))),
                money2(r.get("price")),
                pct(r.get("premium_rt")),
                money2(r.get("double_low")),
                html.escape(str(r.get("rating") or "-")),
                money2(r.get("remaining_scale")),
                money2(r.get("remaining_years")),
                html.escape(str(r.get("risk_reasons") or "")),
            )
        )
    return "\n".join(out)


def write_html(path, records, summary, args, generated_at):
    buy = summary["buy"]
    watch = summary["watch"]
    low_pool = summary["low_pool"]
    conclusion = (
        f"当前按默认保守规则筛出 {len(buy)} 只买入候选。"
        if buy else
        "当前默认保守规则下没有完整通过的买入候选，建议只观察，不强行建仓。"
    )
    if 0 < len(buy) < 10:
        conclusion += " 数量不足 10 只，暂不适合一次性做完整 15-30 只篮子。"
    elif len(buy) >= 10:
        conclusion += " 候选数量已足够构建分散篮子，仍建议单只不超过 5-8%。"

    payload = {
        "generated_at": generated_at,
        "rules": vars(args),
        "summary": {k: v for k, v in summary.items() if not isinstance(v, list)},
    }
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>可转债双低策略筛选</title>
<style>
*{{box-sizing:border-box}}:root{{--bg:#f6f8fb;--text:#172033;--heading:#0f172a;--muted:#64748b;--surface:#fff;--soft:#f8fafc;--border:#dbe4f0;--green:#16a34a;--yellow:#b45309;--red:#dc2626;--link:#2563eb;--shadow:0 1px 2px rgba(15,23,42,.05)}}
:root[data-theme="dark"]{{--bg:#0f1115;--text:#e6e8eb;--heading:#f8fafc;--muted:#9aa4b2;--surface:#131820;--soft:#1a1f29;--border:#232936;--green:#3ddc84;--yellow:#ffd166;--red:#ff6b6b;--link:#7fb3ff;--shadow:none}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px}}
header{{padding:22px 28px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}}
h1{{margin:0 0 6px;color:var(--heading);font-size:22px}}.sub{{color:var(--muted);font-size:12px;line-height:1.7}}
main{{max-width:1280px;margin:0 auto;padding:22px 18px 40px}}.toolbar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.btn{{display:inline-flex;align-items:center;gap:6px;text-decoration:none;border:1px solid var(--border);background:var(--soft);color:var(--text);border-radius:7px;padding:7px 12px;min-height:34px;white-space:nowrap;cursor:pointer}}
.btn.primary{{background:var(--link);border-color:var(--link);color:#fff}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}
.metric{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;box-shadow:var(--shadow)}}.metric .v{{font-size:26px;font-weight:800;color:var(--heading)}}.metric .l{{font-size:12px;color:var(--muted);margin-top:4px}}
.notice{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:16px;line-height:1.8}}
.notice strong{{color:var(--heading)}}section{{margin:18px 0 26px}}h2{{font-size:17px;margin:0 0 10px;color:var(--heading)}}.table-wrap{{overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px}}
table{{width:100%;border-collapse:collapse;min-width:980px}}th,td{{padding:9px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}}th{{background:var(--soft);color:var(--muted);font-size:12px;position:sticky;top:0}}td:nth-child(2),td:nth-child(3),td:nth-child(4),td.reason{{text-align:left}}a{{color:var(--link)}}tr.buy td:first-child{{color:var(--green);font-weight:800}}tr.watch td:first-child{{color:var(--yellow);font-weight:800}}tr.reject{{color:var(--muted)}}.reason{{white-space:normal;min-width:220px}}footer{{padding:16px 28px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}}
@media(max-width:720px){{header{{padding:16px}}h1{{font-size:18px}}main{{padding:14px 10px}}.summary{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
</style></head><body>
<header>
<div>
<h1>可转债双低策略筛选</h1>
<div class="sub">双低值 = 现价 + 转股溢价率；生成于 {html.escape(generated_at)} · 数据源：东方财富公开接口，集思录抽样交叉校验</div>
</div>
<div class="toolbar">
<a class="btn" href="/screen.html">← 全市场首页</a>
<a class="btn primary" href="{os.path.basename(path).replace('.html', '.csv')}">下载 CSV</a>
<button class="btn" id="themeToggle" type="button">🌙 暗色</button>
</div>
</header>
<main>
<div class="summary">
<div class="metric"><div class="v">{summary['total']}</div><div class="l">全市场转债记录</div></div>
<div class="metric"><div class="v">{summary['buy_count']}</div><div class="l">买入候选</div></div>
<div class="metric"><div class="v">{summary['watch_count']}</div><div class="l">观察池</div></div>
<div class="metric"><div class="v">{summary['reject_count']}</div><div class="l">排雷剔除</div></div>
</div>
<div class="notice">
<strong>当前结论：</strong>{html.escape(conclusion)}
<br>默认规则：评级 ≥ {html.escape(args.min_rating)}，剩余规模 ≥ {args.min_scale:g} 亿，剩余期限 ≥ {args.min_years:g} 年，价格 ≤ {args.max_price:g}，溢价率 ≤ {args.max_premium:g}%，双低值 ≤ {args.max_double_low:g}，剔除 ST/退债/待上市/强赎执行风险。
</div>
<section>
<h2>买入候选 Top {min(args.basket_size, len(buy))}</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>说明</th></tr></thead><tbody>
{rows_html(buy, args.basket_size) or '<tr><td colspan="11" class="reason">暂无完整通过候选。</td></tr>'}
</tbody></table></div>
</section>
<section>
<h2>观察池 Top 30</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>观察原因</th></tr></thead><tbody>
{rows_html(watch, 30) or '<tr><td colspan="11" class="reason">暂无观察项。</td></tr>'}
</tbody></table></div>
</section>
<section>
<h2>双低值最低 30 只及排雷结果</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>剔除/观察原因</th></tr></thead><tbody>
{rows_html(low_pool, 30)}
</tbody></table></div>
</section>
<script id="payload" type="application/json">{html.escape(json.dumps(payload, ensure_ascii=False))}</script>
<script>
function setTheme(t){{document.documentElement.setAttribute("data-theme",t);localStorage.setItem("theme",t);document.getElementById("themeToggle").textContent=t==="dark"?"☀️ 亮色":"🌙 暗色"}}
(function(){{var s=localStorage.getItem("theme")||"light";setTheme(s);document.getElementById("themeToggle").onclick=function(){{setTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark")}}}})();
</script>
</main>
<footer>这是策略筛选与风控工具，不构成投资建议；可转债已存在违约和退市案例，需分散、限仓、复核公告。</footer>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)


def write_md(path, summary, args, generated_at):
    buy = summary["buy"][:args.basket_size]
    lines = [
        f"# 可转债双低策略筛选 ({generated_at})",
        "",
        "## 当前结论",
    ]
    if buy:
        lines.append(f"- 默认保守规则筛出 **{len(summary['buy'])}** 只买入候选。")
        if len(summary["buy"]) < 10:
            lines.append("- 候选不足 10 只，不建议一次性做完整 15-30 只篮子。")
        else:
            lines.append("- 候选数量可支持分散篮子，仍需单只限仓。")
    else:
        lines.append("- 默认保守规则下暂无完整通过候选，建议观察。")
    lines += [
        "",
        "## 默认规则",
        f"- 评级 >= {args.min_rating}",
        f"- 剩余规模 >= {args.min_scale:g} 亿",
        f"- 剩余期限 >= {args.min_years:g} 年",
        f"- 价格 <= {args.max_price:g}",
        f"- 溢价率 <= {args.max_premium:g}%",
        f"- 双低值 <= {args.max_double_low:g}",
        "- 剔除 ST/退债/待上市/强赎执行风险",
        "",
        "## 买入候选",
        "|排名|代码|转债|正股|现价|溢价率|双低值|评级|剩余规模|剩余年限|",
        "|---:|---|---|---|---:|---:|---:|---|---:|---:|",
    ]
    if buy:
        for r in buy:
            lines.append(
                f"|{r['rank']}|{r['code']}|{r['name']}|{r['stock_name']}|"
                f"{money2(r.get('price'))}|{pct(r.get('premium_rt'))}|"
                f"{money2(r.get('double_low'))}|{r.get('rating') or '-'}|"
                f"{money2(r.get('remaining_scale'))}|{money2(r.get('remaining_years'))}|"
            )
    else:
        lines.append("|-|-|暂无|-|-|-|-|-|-|-|")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_stable_alias(path, target, title="可转债双低策略固定入口"):
    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={html.escape(target)}">
<title>{html.escape(title)}</title>
<style>body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#172033}}.box{{max-width:560px;margin:14vh auto;padding:24px;background:#fff;border:1px solid #dbe4f0;border-radius:8px}}a{{color:#2563eb}}</style>
</head><body><div class="box"><h1>{html.escape(title)}</h1>
<p>正在打开最新页面：<a href="{html.escape(target)}">{html.escape(target)}</a></p>
<p>日常请访问 <code>cbond_double_low.html</code>，日期页作为历史产物保留。</p>
</div><script>location.replace({json.dumps(target)})</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def run(args):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ensure_cbond_deep_shell()
    today = date.today()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    ttl = 0 if args.fresh else args.cache_hours
    records = build_convertible_bond_universe(ttl_hours=ttl, quote_ttl_hours=0 if args.fresh else 0.5, today=today)
    classified = classify_records(records, args, today)
    summary = summarize(classified)

    ts = today.strftime("%Y%m%d")
    base = os.path.join(RESULTS_DIR, f"cbond_double_low_{ts}")
    csv_path = base + ".csv"
    html_path = base + ".html"
    md_path = base + ".md"
    stable_path = os.path.join(RESULTS_DIR, "cbond_double_low.html")

    write_csv(csv_path, classified)
    write_html(html_path, classified, summary, args, generated_at)
    write_md(md_path, summary, args, generated_at)
    write_stable_alias(stable_path, os.path.basename(html_path))

    jsl_count = 0
    jsl_overlap = 0
    if args.jisilu_check:
        try:
            sample = fetch_jisilu_low_sample(ttl_hours=0 if args.fresh else 0.25)
            jsl_count = len(sample)
            em_codes = {r["code"] for r in classified[:60]}
            jsl_overlap = len({str(r.get("bond_id") or "") for r in sample} & em_codes)
        except Exception as e:
            print(f"  ⚠ 集思录抽样校验失败: {e}")

    print("=" * 60)
    print("  可转债双低策略筛选")
    print("=" * 60)
    print(f"全市场记录: {summary['total']} 只")
    print(f"买入候选:   {summary['buy_count']} 只")
    print(f"观察池:     {summary['watch_count']} 只")
    print(f"排雷剔除:   {summary['reject_count']} 只")
    if args.jisilu_check:
        print(f"集思录抽样: {jsl_count} 条，低双低前60重合 {jsl_overlap} 条")
    print("")
    print("Top 买入候选:")
    for r in summary["buy"][:args.basket_size]:
        print(
            f"  {r['rank']:>3}. {r['code']} {r['name']} "
            f"价{money2(r.get('price'))} 溢价{pct(r.get('premium_rt'))} "
            f"双低{money2(r.get('double_low'))} 评级{r.get('rating') or '-'} "
            f"规模{money2(r.get('remaining_scale'))}亿"
        )
    if not summary["buy"]:
        print("  暂无")
    print("")
    print(f"HTML: {html_path}")
    print(f"CSV:  {csv_path}")
    print(f"MD:   {md_path}")
    print(f"稳定入口: {stable_path}")
    return classified, summary


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="可转债双低策略自动筛选工具")
    ap.add_argument("--fresh", action="store_true", help="跳过缓存，重新抓取公开数据")
    ap.add_argument("--cache-hours", type=float, default=2, help="东方财富条款缓存小时数")
    ap.add_argument("--min-rating", default="AA-", help="最低评级，默认 AA-")
    ap.add_argument("--min-scale", type=float, default=2.0, help="最低剩余规模(亿)，默认2")
    ap.add_argument("--min-years", type=float, default=0.5, help="最低剩余期限(年)，默认0.5")
    ap.add_argument("--max-price", type=float, default=130.0, help="买入候选最高价格，默认130")
    ap.add_argument("--max-premium", type=float, default=30.0, help="买入候选最高转股溢价率，默认30")
    ap.add_argument("--max-double-low", type=float, default=150.0, help="买入候选最高双低值，默认150")
    ap.add_argument("--basket-size", type=int, default=30, help="页面展示的买入篮子上限，默认30")
    ap.add_argument("--jisilu-check", action="store_true", help="用集思录匿名低双低样本做交叉校验")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
