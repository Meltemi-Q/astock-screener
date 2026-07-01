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
    "basic_status",
    "enhanced_status",
    "final_action",
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
    "enhanced_reasons",
    "maturity_redeem_price",
    "maturity_yield_est",
    "call_gap_pct",
    "put_gap_pct",
    "down_revision_gap_pct",
    "has_conditional_resale",
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


def round_or_none(value, digits=2):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def metric_calcs(record) -> dict:
    """Convertible-bond event/risk proxy metrics used by enhanced filters."""
    price = fnum(record.get("price"))
    years = fnum(record.get("remaining_years"))
    stock_price = fnum(record.get("stock_price"))
    convert_price = fnum(record.get("convert_price"))
    redeem_trigger = fnum(record.get("redeem_trigger_price"))
    resale_trigger = fnum(record.get("resale_trigger_price"))
    maturity_redeem = fnum(record.get("maturity_redeem_price"))

    # 说明：东方财富公开接口没有结构化的逐年票息现金流字段（只有自由文本
    # INTEREST_RATE_EXPLAIN，且未并入记录），无法可靠还原真实 YTM。
    # 这里只算“价格年化(不含票息)”：单纯用到期赎回价相对现价的价差做单利年化，
    # 用于粗略衡量债性拖累，绝非真实到期收益率，标签/页面已明确标注避免误导。
    price_annualized = None
    if price and years and years > 0:
        maturity_base = maturity_redeem if maturity_redeem else 100.0
        price_annualized = (maturity_base - price) / price / years * 100

    call_gap = None
    if stock_price and stock_price > 0 and redeem_trigger:
        call_gap = (redeem_trigger - stock_price) / stock_price * 100

    put_gap = None
    if stock_price and stock_price > 0 and resale_trigger:
        put_gap = (stock_price - resale_trigger) / stock_price * 100

    down_revision_gap = None
    if stock_price and convert_price and convert_price > 0:
        # A common下修触发区间 is around 85% of conversion price, but clauses vary.
        down_revision_line = convert_price * 0.85
        down_revision_gap = (stock_price - down_revision_line) / down_revision_line * 100

    return {
        # 键名沿用 maturity_yield_est 以兼容 CSV/深度分析/前端契约，
        # 但其含义是“价格年化(不含票息，非真实YTM)”，展示层已据此改标签。
        "maturity_yield_est": round_or_none(price_annualized),
        "call_gap_pct": round_or_none(call_gap),
        "put_gap_pct": round_or_none(put_gap),
        "down_revision_gap_pct": round_or_none(down_revision_gap),
    }


def enhanced_reasons(record, args) -> list[str]:
    """Second-layer convertible-specific review after the basic double-low pass."""
    reasons = []
    price = fnum(record.get("price"))
    premium = fnum(record.get("premium_rt"))
    years = fnum(record.get("remaining_years"))
    maturity_redeem = fnum(record.get("maturity_redeem_price"))
    metrics = metric_calcs(record)
    # 注意：该值是“价格年化(不含票息，非真实YTM)”，不能当作真实到期收益率解读。
    price_ann = metrics["maturity_yield_est"]
    call_gap = metrics["call_gap_pct"]
    put_gap = metrics["put_gap_pct"]
    down_gap = metrics["down_revision_gap_pct"]
    has_resale = str(record.get("has_conditional_resale")).lower() in ("1", "true", "yes", "y")

    if price_ann is not None:
        # 价格年化仅衡量价差拖累、不含票息，故文案强调"含票息后可能仍为正"；
        # 但在 enhanced_status_for 的档位决策里，价格年化为负仍作为"降级观察"的严重信号之一。
        if price_ann < -8:
            reasons.append(f"价格年化(不含票息){price_ann:.2f}%/年，价高债性保护偏弱")
        elif price_ann < 0:
            reasons.append(f"价格年化(不含票息){price_ann:.2f}%/年，为负（含票息后可能仍为正）")
        elif price_ann >= 4:
            reasons.append(f"价格年化(不含票息){price_ann:.2f}%/年，价差留有债性缓冲")

    if price is not None and maturity_redeem is not None and years is not None:
        if years <= 1.0 and price > maturity_redeem + 3:
            reasons.append(f"剩余期限{years:.2f}年且价格高于到期赎回价{maturity_redeem:.2f}")

    if call_gap is not None:
        # 仅基于单日快照价，未反映 15/30 日连续触发计数，接近强赎识别偏弱。
        if call_gap <= 0:
            reasons.append("正股已高于强赎触发价(仅单日快照，未计连续触发天数)，需核对连续交易日与公告")
        elif call_gap <= 8:
            reasons.append(f"距强赎触发价约{call_gap:.2f}%(仅单日快照，未计连续触发天数)，上涨空间可能被封顶")

    if has_resale:
        if put_gap is not None and put_gap <= 0:
            reasons.append("正股已低于回售触发价，需核对是否进入可回售年度及公告")
        elif put_gap is not None and put_gap <= 8 and years is not None and years <= 2.2:
            reasons.append(f"接近回售触发线({put_gap:.2f}%)，可关注回售公告窗口")
    else:
        reasons.append("无普通有条件回售条款，不能按回售保护估值")

    if down_gap is not None:
        if down_gap <= -12:
            reasons.append(f"低于常见下修观察线约{abs(down_gap):.2f}%，有下修压力但需股东大会/董事会落地")
        elif down_gap <= 5:
            reasons.append(f"接近常见下修观察线({down_gap:.2f}%)，仅作为事件催化")

    if premium is not None and premium > 25:
        reasons.append(f"溢价率{premium:.2f}%偏高，正股跟涨弹性不足")
    if price is not None and price > 125:
        reasons.append(f"价格{price:.2f}偏高，债底保护变弱")

    return reasons


def enhanced_status_for(record, basic_status: str) -> tuple[str, str, list[str]]:
    """Return enhanced status, final action and reasons."""
    if basic_status == "剔除":
        reasons = [r for r in str(record.get("risk_reasons") or "").split("；") if r]
        return "增强排除", "排除", reasons
    if basic_status == "观察":
        reasons = [r for r in str(record.get("risk_reasons") or "").split("；") if r]
        return "增强观察", "观察", reasons

    reasons = enhanced_reasons(record, None)

    # 用结构化数值阈值判严重度，替代脆弱的中文关键词 in 匹配。
    metrics = metric_calcs(record)
    price = fnum(record.get("price"))
    price_ann = metrics["maturity_yield_est"]  # 价格年化(不含票息)
    call_gap = metrics["call_gap_pct"]
    put_gap = metrics["put_gap_pct"]
    down_gap = metrics["down_revision_gap_pct"]

    # 硬排除：正股已站上/贴近强赎触发价且转债价高——强赎砸盘风险实打实。
    # call_gap<=0 表示正股已高于触发价；配合价格>=118 判定为不可持有。
    above_call_trigger = call_gap is not None and call_gap <= 0
    high_price = price is not None and price >= 118
    # 价格年化深度为负且转债价高，价差侧几乎没有债性保护垫。
    deep_negative_ann = price_ann is not None and price_ann <= -8
    if (above_call_trigger and high_price) or (deep_negative_ann and high_price):
        return "增强排除", "排除", reasons

    # 严重（降级观察）：贴近强赎触发线，或价格年化为负，或价格偏高。
    severe = (
        (call_gap is not None and call_gap <= 8)
        or (price_ann is not None and price_ann < 0)
        or (price is not None and price > 125)
    )
    # 支撑（可小仓试跑）：价差留有债性缓冲、临近回售窗口、或有下修压力催化。
    supportive = (
        (price_ann is not None and price_ann >= 4)
        or (put_gap is not None and 0 < put_gap <= 8)
        or (down_gap is not None and down_gap <= -12)
    )
    if severe:
        return "增强观察", "观察", reasons
    if supportive or not reasons:
        return "增强通过", "小仓试跑", reasons
    return "增强观察", "观察", reasons


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
            basic_status = "剔除"
            reasons = hard
        elif soft:
            basic_status = "观察"
            reasons = soft
        else:
            basic_status = "基础候选"
            reasons = []
        record.update(metric_calcs(record))
        record["basic_status"] = basic_status
        record["status"] = basic_status
        record["risk_reasons"] = "；".join(reasons)
        enhanced_status, final_action, extra_reasons = enhanced_status_for(record, basic_status)
        record["enhanced_status"] = enhanced_status
        record["final_action"] = final_action
        record["enhanced_reasons"] = "；".join(extra_reasons)
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
    # basic_status 仅取值 基础候选/观察/剔除；不存在“买入候选”这一状态，故不再匹配它。
    # 增强风控硬升级为“排除”的基础候选不再算作可买候选。
    buy = [r for r in records if r["status"] == "基础候选" and r.get("final_action") != "排除"]
    final = [r for r in records if r.get("final_action") == "小仓试跑"]
    watch = [r for r in records if r["status"] == "观察"]
    # 增强风控可把基础候选硬升级为“排除”，这些应计入剔除口径展示。
    reject = [r for r in records if r["status"] == "剔除" or r.get("final_action") == "排除"]
    low_pool = [r for r in records if r.get("double_low") is not None][:30]
    return {
        "total": total,
        "buy_count": len(buy),
        "basic_count": len(buy),
        "final_count": len(final),
        "watch_count": len(watch),
        "reject_count": len(reject),
        "low_pool_count": len(low_pool),
        "buy": buy,
        "final": final,
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
        # 增强风控把基础候选硬升级为“排除”时，行样式跟随最终动作显示为剔除，避免误导。
        if r.get("final_action") == "排除":
            cls = "reject"
        else:
            cls = {"基础候选": "buy", "观察": "watch", "剔除": "reject"}.get(r.get("status"), "")
        reason = r.get("enhanced_reasons") or r.get("risk_reasons") or ""
        out.append(
            "<tr class=\"%s\">"
            "<td>%s</td><td><a href=\"cbond_deep/report.html?code=%s\">%s</a></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=\"reason\">%s</td>"
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
                html.escape(str(r.get("enhanced_status") or r.get("status") or "")),
                html.escape(str(r.get("final_action") or "")),
                html.escape(str(reason)),
            )
        )
    return "\n".join(out)


def write_html(path, records, summary, args, generated_at):
    buy = summary["buy"]
    final = summary["final"]
    watch = summary["watch"]
    low_pool = summary["low_pool"]
    conclusion = f"基础双低规则筛出 {len(buy)} 只基础候选；增强风控后 {len(final)} 只进入小仓试跑。"
    if not final:
        conclusion += " 当前没有同时通过价格年化(不含票息)、强赎/回售/下修复核的最终候选，建议只观察，不强行建仓。"
    elif len(final) < 10:
        conclusion += " 数量不足 10 只，暂不适合一次性做完整 15-30 只篮子。"
    else:
        conclusion += " 数量已足够构建分散篮子，仍建议单只不超过 5-8%。"

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
table{{width:100%;border-collapse:collapse;min-width:1120px}}th,td{{padding:9px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}}th{{background:var(--soft);color:var(--muted);font-size:12px;position:sticky;top:0}}td:nth-child(2),td:nth-child(3),td:nth-child(4),td.reason{{text-align:left}}a{{color:var(--link)}}tr.buy td:first-child{{color:var(--green);font-weight:800}}tr.watch td:first-child{{color:var(--yellow);font-weight:800}}tr.reject{{color:var(--muted)}}.reason{{white-space:normal;min-width:260px}}footer{{padding:16px 28px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}}
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
<div class="metric"><div class="v">{summary['basic_count']}</div><div class="l">基础候选</div></div>
<div class="metric"><div class="v">{summary['final_count']}</div><div class="l">小仓试跑</div></div>
<div class="metric"><div class="v">{summary['watch_count']}</div><div class="l">观察池</div></div>
<div class="metric"><div class="v">{summary['reject_count']}</div><div class="l">排雷剔除</div></div>
</div>
<div class="notice">
<strong>当前结论：</strong>{html.escape(conclusion)}
<br>基础规则：评级 ≥ {html.escape(args.min_rating)}，剩余规模 ≥ {args.min_scale:g} 亿，剩余期限 ≥ {args.min_years:g} 年，价格 ≤ {args.max_price:g}，溢价率 ≤ {args.max_premium:g}%，双低值 ≤ {args.max_double_low:g}，剔除 ST/退债/待上市/强赎执行风险。
<br>增强规则：复核到期赎回价/价格年化(不含票息，非真实YTM)、距强赎触发线(仅单日快照，未计连续触发天数)、普通有条件回售、距回售线、常见下修压力线；有事件催化不等于直接买入。价格站上强赎触发价且价高者硬排除。
</div>
<section>
<h2>基础候选 Top {min(args.basket_size, len(buy))}</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>增强风控</th><th>最终动作</th><th>说明</th></tr></thead><tbody>
{rows_html(buy, args.basket_size) or '<tr><td colspan="13" class="reason">暂无基础候选。</td></tr>'}
</tbody></table></div>
</section>
<section>
<h2>观察池 Top 30</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>增强风控</th><th>最终动作</th><th>观察原因</th></tr></thead><tbody>
{rows_html(watch, 30) or '<tr><td colspan="13" class="reason">暂无观察项。</td></tr>'}
</tbody></table></div>
</section>
<section>
<h2>双低值最低 30 只及排雷结果</h2>
<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>剩余规模(亿)</th><th>剩余年限</th><th>增强风控</th><th>最终动作</th><th>剔除/观察原因</th></tr></thead><tbody>
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
    final = summary["final"][:args.basket_size]
    lines = [
        f"# 可转债双低策略筛选 ({generated_at})",
        "",
        "## 当前结论",
    ]
    if summary["buy"]:
        lines.append(f"- 基础双低规则筛出 **{len(summary['buy'])}** 只基础候选。")
        lines.append(f"- 增强风控后 **{len(summary['final'])}** 只进入小仓试跑。")
        if len(summary["final"]) < 10:
            lines.append("- 最终候选不足 10 只，不建议一次性做完整 15-30 只篮子。")
        else:
            lines.append("- 最终候选数量可支持分散篮子，仍需单只限仓。")
    else:
        lines.append("- 基础规则下暂无候选，建议观察。")
    lines += [
        "",
        "## 基础规则",
        f"- 评级 >= {args.min_rating}",
        f"- 剩余规模 >= {args.min_scale:g} 亿",
        f"- 剩余期限 >= {args.min_years:g} 年",
        f"- 价格 <= {args.max_price:g}",
        f"- 溢价率 <= {args.max_premium:g}%",
        f"- 双低值 <= {args.max_double_low:g}",
        "- 剔除 ST/退债/待上市/强赎执行风险",
        "",
        "## 增强规则",
        "- 复核到期赎回价/价格年化(不含票息，非真实YTM)、距强赎触发线(仅单日快照，未计连续触发天数)、普通有条件回售、距回售线、常见下修压力线",
        "- 价格站上强赎触发价且价高的转债硬排除，不进任何篮子",
        "- 下修/回售公告是事件催化，不是一票买入条件",
        "",
        "## 小仓试跑",
        "|排名|代码|转债|正股|现价|溢价率|双低值|评级|最终动作|说明|",
        "|---:|---|---|---|---:|---:|---:|---|---|---|",
    ]
    if final:
        for r in final:
            lines.append(
                f"|{r['rank']}|{r['code']}|{r['name']}|{r['stock_name']}|"
                f"{money2(r.get('price'))}|{pct(r.get('premium_rt'))}|"
                f"{money2(r.get('double_low'))}|{r.get('rating') or '-'}|"
                f"{r.get('final_action') or '-'}|{r.get('enhanced_reasons') or '无'}|"
            )
    else:
        lines.append("|-|-|暂无|-|-|-|-|-|-|-|")
    lines += [
        "",
        "## 基础候选",
        "|排名|代码|转债|正股|现价|溢价率|双低值|评级|增强风控|最终动作|说明|",
        "|---:|---|---|---|---:|---:|---:|---|---|---|---|",
    ]
    if buy:
        for r in buy:
            lines.append(
                f"|{r['rank']}|{r['code']}|{r['name']}|{r['stock_name']}|"
                f"{money2(r.get('price'))}|{pct(r.get('premium_rt'))}|"
                f"{money2(r.get('double_low'))}|{r.get('rating') or '-'}|"
                f"{r.get('enhanced_status') or '-'}|{r.get('final_action') or '-'}|"
                f"{r.get('enhanced_reasons') or '无'}|"
            )
    else:
        lines.append("|-|-|暂无|-|-|-|-|-|-|-|-|")
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
    print(f"基础候选:   {summary['basic_count']} 只")
    print(f"小仓试跑:   {summary['final_count']} 只")
    print(f"观察池:     {summary['watch_count']} 只")
    print(f"排雷剔除:   {summary['reject_count']} 只")
    if args.jisilu_check:
        print(f"集思录抽样: {jsl_count} 条，低双低前60重合 {jsl_overlap} 条")
    print("")
    print("Top 基础候选:")
    for r in summary["buy"][:args.basket_size]:
        print(
            f"  {r['rank']:>3}. {r['code']} {r['name']} "
            f"价{money2(r.get('price'))} 溢价{pct(r.get('premium_rt'))} "
            f"双低{money2(r.get('double_low'))} 评级{r.get('rating') or '-'} "
            f"规模{money2(r.get('remaining_scale'))}亿 -> {r.get('enhanced_status')} / {r.get('final_action')}"
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
    ap.add_argument("--max-price", type=float, default=130.0, help="基础候选最高价格，默认130")
    ap.add_argument("--max-premium", type=float, default=30.0, help="基础候选最高转股溢价率，默认30")
    ap.add_argument("--max-double-low", type=float, default=150.0, help="基础候选最高双低值，默认150")
    ap.add_argument("--basket-size", type=int, default=30, help="页面展示的基础候选上限，默认30")
    ap.add_argument("--jisilu-check", action="store_true", help="用集思录匿名低双低样本做交叉校验")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
