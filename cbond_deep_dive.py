#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convertible-bond deep-dive report generator.

The double-low page answers "which bonds pass the mechanical filters".
This script answers "why this bond is or is not worth a basket slot" by
combining bond metrics, underlying A-share fundamentals, K-lines, and an
optional DeepSeek qualitative analysis.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error

from data_sources.convertible_bonds import (
    build_convertible_bond_universe,
    fetch_tencent_cb_kline,
    fnum,
)
from stock_deep_dive import (
    DEEPSEEK_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_RETRIES,
    SSL_CTX,
    compute_financials,
    fetch_stock_full,
    prefetch_all_financials,
)


WORKDIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORKDIR, "results")
OUT_DIR = os.path.join(RESULTS_DIR, "cbond_deep")
DATA_DIR = os.path.join(OUT_DIR, "data")
TEMPLATE_DIR = os.path.join(WORKDIR, "templates", "cbond_deep")


def latest_file(prefix: str, suffix: str) -> str:
    if not os.path.isdir(RESULTS_DIR):
        return ""
    files = sorted(
        [f for f in os.listdir(RESULTS_DIR) if f.startswith(prefix) and f.endswith(suffix)],
        reverse=True,
    )
    return os.path.join(RESULTS_DIR, files[0]) if files else ""


def read_csv(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_latest_cbond_rows() -> list[dict]:
    return read_csv(latest_file("cbond_double_low_", ".csv"))


def read_latest_astock_rows() -> list[dict]:
    return read_csv(latest_file("astock_screen_", ".csv"))


def payload_path(code: str) -> str:
    return os.path.join(DATA_DIR, f"{code}.json")


def ensure_app(out_dir: str = OUT_DIR):
    """Copy the shared report shell and static assets to results/."""
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    files = {
        "report.html": os.path.join(TEMPLATE_DIR, "report.html"),
        os.path.join("assets", "cbond_deep.css"): os.path.join(TEMPLATE_DIR, "assets", "cbond_deep.css"),
        os.path.join("assets", "cbond_deep.js"): os.path.join(TEMPLATE_DIR, "assets", "cbond_deep.js"),
    }
    for rel, src in files.items():
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)


def clean_code(code: str) -> str:
    code = str(code or "").strip()
    if not re.match(r"^\d{6}$", code):
        raise ValueError(f"无效可转债代码: {code}")
    return code


def first_nonempty(*values):
    for v in values:
        if v not in (None, "", "-", "None", "nan"):
            return v
    return None


def to_float(value):
    return fnum(value)


def rating_score(rating: str) -> float:
    order = {
        "AAA": 100, "AA+": 88, "AA": 78, "AA-": 68,
        "A+": 52, "A": 42, "A-": 30,
        "BBB+": 20, "BBB": 12, "BBB-": 8,
    }
    return order.get((rating or "").upper(), 5)


def clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def score_inverse(value, good, bad) -> float:
    if value is None:
        return 40
    if good == bad:
        return 50
    return clamp((bad - float(value)) / (bad - good) * 100)


def score_forward(value, bad, good) -> float:
    if value is None:
        return 40
    if good == bad:
        return 50
    return clamp((float(value) - bad) / (good - bad) * 100)


def latest_financial(financials: list[dict]) -> dict:
    return financials[-1] if financials else {}


def compute_scores(bond: dict, financials: list[dict]) -> dict:
    fy = latest_financial(financials)
    price = to_float(bond.get("price"))
    premium = to_float(bond.get("premium_rt"))
    double_low = to_float(bond.get("double_low"))
    scale = to_float(bond.get("remaining_scale"))
    years = to_float(bond.get("remaining_years"))
    turnover = to_float(bond.get("turnover"))
    maturity_yield = to_float(bond.get("maturity_yield_est"))
    call_gap = to_float(bond.get("call_gap_pct"))
    down_gap = to_float(bond.get("down_revision_gap_pct"))
    has_resale = str(bond.get("has_conditional_resale")).lower() in ("1", "true", "yes", "y")

    double_low_s = score_inverse(double_low, 120, 160)
    price_s = score_inverse(price, 105, 135)
    premium_s = score_inverse(premium, 5, 40)
    credit_s = rating_score(bond.get("rating"))
    scale_s = score_forward(scale, 1, 20)
    turnover_s = score_forward(turnover, 8_000_000, 120_000_000)
    years_s = 100 if years is not None and 0.8 <= years <= 4.5 else score_forward(years, 0.3, 2.0)
    roe_s = score_forward(to_float(fy.get("roe")), 5, 20)
    debt_s = score_inverse(to_float(fy.get("debt")), 75, 35)
    cash_s = score_forward(to_float(fy.get("ocf_ratio")), 0.2, 1.2)
    growth_s = score_forward(to_float(fy.get("netp_yoy")), -20, 30)
    stock_quality_s = clamp(roe_s * 0.35 + debt_s * 0.2 + cash_s * 0.25 + growth_s * 0.2)
    event_s = 70
    if maturity_yield is not None:
        event_s += clamp(maturity_yield, -20, 12) * 1.2
    if call_gap is not None and call_gap <= 8:
        event_s -= 18 if call_gap > 0 else 28
    if down_gap is not None and down_gap <= -12:
        event_s += 8
    if not has_resale:
        event_s -= 8
    event_s = clamp(event_s)
    total = clamp(
        double_low_s * 0.24
        + price_s * 0.14
        + premium_s * 0.14
        + credit_s * 0.16
        + scale_s * 0.08
        + turnover_s * 0.06
        + years_s * 0.06
        + stock_quality_s * 0.08
        + event_s * 0.04
    )
    basic_status = bond.get("basic_status") or bond.get("status")
    final_action = bond.get("final_action")
    if basic_status == "剔除" or final_action == "排除":
        total = min(total, 45)
    elif basic_status == "观察" or final_action == "观察":
        total = min(total, 72)
    return {
        "total": round(total, 1),
        "double_low": round(double_low_s, 1),
        "price_safety": round(price_s, 1),
        "premium": round(premium_s, 1),
        "credit": round(credit_s, 1),
        "scale_liquidity": round((scale_s * 0.6 + turnover_s * 0.4), 1),
        "maturity": round(years_s, 1),
        "stock_quality": round(stock_quality_s, 1),
        "event_risk": round(event_s, 1),
    }


def action_label(bond: dict, scores: dict) -> str:
    final_action = bond.get("final_action")
    if final_action in ("排除", "观察", "小仓试跑"):
        return final_action
    basic_status = bond.get("basic_status") or bond.get("status")
    if basic_status == "剔除":
        return "排除"
    if scores.get("total", 0) >= 78 and basic_status in ("买入候选", "基础候选"):
        return "篮子候选"
    if basic_status in ("买入候选", "基础候选"):
        return "小仓试跑"
    return "观察"


def fetch_bond_klines(code: str, no_kline: bool = False) -> dict:
    if no_kline:
        return {"day": [], "week": [], "month": []}
    periods = {"day": ("101", 260), "week": ("102", 120), "month": ("103", 80)}
    out = {"day": [], "week": [], "month": []}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            ex.submit(fetch_tencent_cb_kline, code, period_key=name, limit=limit): name
            for name, (_klt, limit) in periods.items()
        }
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                out[name] = fut.result()
            except Exception:
                out[name] = []
    return out


def normalize_bond_record(record: dict) -> dict:
    keys = [
        "rank", "status", "basic_status", "enhanced_status", "final_action",
        "code", "name", "stock_code", "stock_name", "price",
        "premium_rt", "double_low", "rating", "remaining_scale", "remaining_years",
        "change_pct", "maturity_date", "convert_price", "convert_value",
        "stock_price", "pb", "turnover", "risk_reasons", "source",
        "enhanced_reasons", "maturity_redeem_price", "maturity_yield_est",
        "call_gap_pct", "put_gap_pct", "down_revision_gap_pct", "has_conditional_resale",
        "resale_trigger_price", "redeem_trigger_price", "is_redeem", "redeem_type",
        "execute_reason_sh", "execute_reason_hs", "notice_date_sh", "notice_date_hs",
        "execute_start_date_sh", "execute_start_date_hs", "execute_end_date", "record_date_sh",
    ]
    out = {k: record.get(k) for k in keys}
    for k in (
        "price", "premium_rt", "double_low", "remaining_scale", "remaining_years",
        "change_pct", "convert_price", "convert_value", "stock_price", "pb",
        "turnover", "resale_trigger_price", "redeem_trigger_price",
        "maturity_redeem_price", "maturity_yield_est", "call_gap_pct", "put_gap_pct",
        "down_revision_gap_pct",
    ):
        out[k] = to_float(out.get(k))
    out["has_conditional_resale"] = str(out.get("has_conditional_resale")).lower() in (
        "1", "true", "yes", "y", "有"
    )
    return out


def bond_record_map(fresh: bool = False) -> dict[str, dict]:
    rows = {r.get("code"): dict(r) for r in read_latest_cbond_rows() if r.get("code")}
    try:
        universe = build_convertible_bond_universe(ttl_hours=0 if fresh else 2, quote_ttl_hours=0 if fresh else 0.5)
    except Exception:
        universe = []
    for r in universe:
        code = r.get("code")
        if not code:
            continue
        merged = dict(r)
        if code in rows:
            merged.update({k: v for k, v in rows[code].items() if v not in (None, "")})
        rows[code] = merged
    return rows


def find_bond_record(code: str, records: dict[str, dict]) -> dict:
    record = records.get(code)
    if not record:
        raise RuntimeError(f"未找到可转债 {code}，请先运行可转债双低筛选")
    return normalize_bond_record(record)


def build_payload(
    code: str,
    records: dict[str, dict] | None = None,
    no_kline: bool = False,
    fresh: bool = False,
    spot_cache: dict | None = None,
) -> dict:
    code = clean_code(code)
    records = records or bond_record_map(fresh=fresh)
    bond = find_bond_record(code, records)
    astock_rows = read_latest_astock_rows()
    astock_row = next((r for r in astock_rows if r.get("code") == bond.get("stock_code")), {})

    stock_code = bond.get("stock_code") or ""
    stock_name = first_nonempty(bond.get("stock_name"), astock_row.get("name"), "")
    industry = first_nonempty(astock_row.get("industry"), "")

    stock = fetch_stock_full(
        stock_code,
        name=stock_name,
        industry=industry,
        csv_rows=astock_rows,
        no_kline=no_kline,
        spot_cache=spot_cache,
    ) if stock_code else {"kline": {"day": [], "week": [], "month": []}}
    financials = compute_financials(stock) if stock_code else []
    scores = compute_scores(bond, financials)
    payload = {
        "meta": {
            "kind": "convertible_bond",
            "code": code,
            "name": bond.get("name") or "",
            "stock_code": stock_code,
            "stock_name": stock_name or "",
            "industry": stock.get("industry") or industry or "",
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        },
        "bond": bond,
        "stock_quote": {
            "price": stock.get("price"),
            "pe_ttm": stock.get("pe_ttm"),
            "pe_dyn": stock.get("pe_dyn"),
            "pb": stock.get("pb"),
            "mktcap": stock.get("mktcap"),
            "mktcap_yi": (stock.get("mktcap") or 0) / 1e8 if stock.get("mktcap") else None,
        },
        "financials": financials,
        "stock_peers": stock.get("peers", []),
        "kline": {
            "bond": fetch_bond_klines(code, no_kline=no_kline),
            "stock": stock.get("kline") or {"day": [], "week": [], "month": []},
        },
        "scores": scores,
        "action": action_label(bond, scores),
        "analysis": None,
    }
    return payload


def deepseek_analyze_cbond(payload: dict) -> dict | None:
    if not DEEPSEEK_KEY:
        return None
    meta = payload.get("meta") or {}
    bond = payload.get("bond") or {}
    quote = payload.get("stock_quote") or {}
    financials = payload.get("financials") or []
    scores = payload.get("scores") or {}
    fy = latest_financial(financials)
    fin_lines = "\n".join(
        f"  {d.get('year')}: 营收{d.get('rev')} 净利{d.get('netp')} ROE{d.get('roe')}% "
        f"毛利{d.get('gm')}% 负债{d.get('debt')}% 现金流/净利{d.get('ocf_ratio')}"
        for d in financials[-3:]
    ) or "  无完整财务序列"
    prompt = f"""你是可转债双低策略研究员，请分析这只可转债是否适合进入分散轮动篮子。

转债: {meta.get('name')}({meta.get('code')})
正股: {meta.get('stock_name')}({meta.get('stock_code')}) | 行业: {meta.get('industry')}

转债指标:
- 现价: {bond.get('price')}
- 转股溢价率: {bond.get('premium_rt')}%
- 双低值: {bond.get('double_low')}
- 评级: {bond.get('rating')}
- 剩余规模: {bond.get('remaining_scale')} 亿
- 剩余年限: {bond.get('remaining_years')}
- 到期日: {bond.get('maturity_date')}
- 转股价: {bond.get('convert_price')}
- 转股价值: {bond.get('convert_value')}
- 赎回触发价: {bond.get('redeem_trigger_price')}
- 回售触发价: {bond.get('resale_trigger_price')}
- 到期赎回价: {bond.get('maturity_redeem_price')}
- 到期收益估算: {bond.get('maturity_yield_est')}%/年
- 距强赎触发价: {bond.get('call_gap_pct')}%
- 距回售触发价: {bond.get('put_gap_pct')}%
- 常见下修压力线距离: {bond.get('down_revision_gap_pct')}%
- 是否有普通有条件回售: {bond.get('has_conditional_resale')}
- 基础筛选状态: {bond.get('basic_status') or bond.get('status')}；增强风控: {bond.get('enhanced_status')}；最终动作: {bond.get('final_action')}
- 排雷/观察原因: {bond.get('risk_reasons') or '无'}
- 增强风控原因: {bond.get('enhanced_reasons') or '无'}

正股与财务:
- 正股现价: {quote.get('price')} | PE(TTM): {quote.get('pe_ttm')} | PB: {quote.get('pb')} | 市值: {quote.get('mktcap_yi')} 亿
- 最新年报: {fy.get('year')} | ROE: {fy.get('roe')}% | 毛利率: {fy.get('gm')}% | 净利率: {fy.get('nm')}%
- 负债率: {fy.get('debt')}% | 现金流/净利: {fy.get('ocf_ratio')} | 净利增速: {fy.get('netp_yoy')}%
近三年财务:
{fin_lines}

量化评分:
- 总分: {scores.get('total')}/100
- 双低性: {scores.get('double_low')} | 价格安全: {scores.get('price_safety')} | 溢价率: {scores.get('premium')}
- 信用: {scores.get('credit')} | 规模流动性: {scores.get('scale_liquidity')} | 正股质量: {scores.get('stock_quality')} | 事件风控: {scores.get('event_risk')}

请按可转债双低策略的真实交易纪律分析：下跌保护、正股弹性、信用/强赎/回售/下修/退市风险、是否适合小仓试跑或篮子持有、何时止盈/轮动。
注意：下修或回售公告只是事件催化，不能一票买入；必须结合转债价格、到期赎回价、回售价格、正股质量、公司偿付能力和公告落地概率。
不要说空话，不要给保证收益，不要建议重仓。

严格输出 JSON，不要 Markdown，不要代码块：
{{"bond_thesis":"一句话结论","double_low_view":"双低性评价","downside_protection":"债底/价格/评级/规模带来的下跌保护","equity_optionality":"正股上涨弹性与溢价率评价","underlying_quality":"正股质量评价","call_put_risk":"强赎/回售/到期风险","rotation_plan":"买入、持有、止盈、轮动纪律","key_risks":"关键风险","action":"篮子候选/小仓试跑/观察/排除","confidence":"高/中/低","cbond_score":78}}"""
    req_payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是可转债双低策略研究员，回答用中文，简洁、克制、重视风险和交易纪律。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 1500,
        "user_id": "economy_cbond_deep_dive",
    }
    if DEEPSEEK_MODEL.startswith("deepseek-v4-"):
        req_payload["thinking"] = {"type": "disabled"}

    last_err = None
    for attempt in range(max(1, DEEPSEEK_RETRIES)):
        try:
            req = request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps(req_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp = json.loads(request.urlopen(req, timeout=70, context=SSL_CTX).read())
            content = resp["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```\w*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            return json.loads(content)
        except error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504) or attempt == DEEPSEEK_RETRIES - 1:
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last_err = str(e)
            if attempt == DEEPSEEK_RETRIES - 1:
                break
            time.sleep(1.0 * (attempt + 1))
    print(f"    DeepSeek API 错误: {last_err}")
    return None


def write_payload(payload: dict) -> str:
    ensure_app()
    os.makedirs(DATA_DIR, exist_ok=True)
    path = payload_path(payload["meta"]["code"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return path


def existing_analysis(code: str) -> dict | None:
    path = payload_path(code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("analysis")
    except Exception:
        return None


def run_one(code: str, args, records: dict[str, dict] | None = None, spot_cache: dict | None = None) -> bool:
    code = clean_code(code)
    if args.ai_only:
        path = payload_path(code)
        if not os.path.exists(path):
            print(f"[{code}] 无已有详情 JSON，无法 AI-only")
            return False
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = build_payload(
            code,
            records=records,
            no_kline=args.no_kline,
            fresh=args.fresh,
            spot_cache=spot_cache,
        )
        if not args.fresh and not args.refresh_ai:
            old_analysis = existing_analysis(code)
            if old_analysis:
                payload["analysis"] = old_analysis

    if not args.no_llm and DEEPSEEK_KEY and (args.ai_only or args.refresh_ai or not payload.get("analysis")):
        print(f"[{code}] DeepSeek 可转债分析中...", end=" ", flush=True)
        analysis = deepseek_analyze_cbond(payload)
        print("OK" if analysis else "失败")
        if analysis:
            payload["analysis"] = analysis
            payload.setdefault("meta", {})["generated_at"] = time.strftime("%Y-%m-%d %H:%M")
    elif not DEEPSEEK_KEY and not args.no_llm:
        print(f"[{code}] 未配置 DeepSeek API Key，生成量化详情")

    path = write_payload(payload)
    print(f"[{code}] 写入 {path}")
    return True


def select_rows(rows: list[dict], status: str, limit: int) -> list[dict]:
    if status != "all":
        rows = [r for r in rows if r.get("status") == status or r.get("basic_status") == status]
    rows.sort(key=lambda r: (
        999999 if to_float(r.get("rank")) is None else to_float(r.get("rank")),
        999999 if to_float(r.get("double_low")) is None else to_float(r.get("double_low")),
    ))
    return rows[:limit] if limit else rows


def generate_index(rows: list[dict]):
    ensure_app()
    body_rows = []
    for r in rows:
        code = r.get("code", "")
        name = r.get("name", "")
        stock = r.get("stock_name", "")
        status = r.get("basic_status") or r.get("status", "")
        final_action = r.get("final_action", "")
        body_rows.append(
            f'<tr><td>{r.get("rank","")}</td><td><a href="report.html?code={code}">{code}</a></td>'
            f'<td><a href="report.html?code={code}">{name}</a></td><td>{stock}</td>'
            f'<td>{r.get("price","")}</td><td>{r.get("premium_rt","")}%</td>'
            f'<td>{r.get("double_low","")}</td><td>{r.get("rating","")}</td><td>{status}</td><td>{final_action}</td></tr>'
        )
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>可转债深度分析索引</title><link rel="stylesheet" href="assets/cbond_deep.css"></head>
<body><div class="container"><header><nav class="report-nav"><a class="back" href="../cbond_double_low.html">← 可转债双低</a></nav>
<h1>可转债深度分析</h1><div class="sub">点击转债进入详情，缺失数据时会通过本地/线上 API 自动生成。</div></header>
<div class="section"><div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>转债</th><th>正股</th><th>现价</th><th>溢价率</th><th>双低值</th><th>评级</th><th>基础状态</th><th>最终动作</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div></div></div></body></html>"""
    path = os.path.join(OUT_DIR, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="可转债深度分析页生成器")
    ap.add_argument("--code", help="单只可转债代码")
    ap.add_argument("--from-screen", action="store_true", help="从最新双低筛选结果批量生成")
    ap.add_argument("--status", default="基础候选", choices=["基础候选", "买入候选", "观察", "剔除", "all"], help="批量生成的筛选状态")
    ap.add_argument("--limit", type=int, default=30, help="批量上限，默认30")
    ap.add_argument("--fresh", action="store_true", help="重新抓取可转债公开数据")
    ap.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek，只生成量化详情")
    ap.add_argument("--ai-only", action="store_true", help="只对已有详情 JSON 补 AI，不重抓数据")
    ap.add_argument("--refresh-ai", action="store_true", help="重建详情并强制刷新 DeepSeek 分析")
    ap.add_argument("--no-kline", action="store_true", help="跳过转债/正股 K线")
    ap.add_argument("--parallel", type=int, default=4, help="批量生成并发数，默认4")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    ensure_app()
    if args.code:
        return 0 if run_one(args.code, args) else 1
    if not args.from_screen:
        print("请指定 --code 或 --from-screen")
        return 2

    rows = select_rows(read_latest_cbond_rows(), args.status, args.limit)
    if not rows:
        print("未找到可生成的可转债行，请先运行 cbond_double_low.py")
        return 1
    generate_index(rows)
    records = bond_record_map(fresh=args.fresh)
    spot_cache = None
    if not args.ai_only:
        stock_codes = [records.get(r.get("code"), {}).get("stock_code") for r in rows]
        stock_codes = sorted({c for c in stock_codes if c})
        if len(stock_codes) >= 20:
            try:
                prefetch_all_financials(codes=stock_codes)
            except Exception as e:
                print(f"  ⚠ 正股财务预取失败，将逐只回退: {e}")
        try:
            from astock_screener import fetch_spot_parallel
            spot_cache = fetch_spot_parallel()
        except Exception as e:
            print(f"  ⚠ 正股行情预取失败，将逐只回退: {e}")
    ok = 0
    if args.parallel <= 1:
        for r in rows:
            ok += 1 if run_one(r["code"], args, records=records, spot_cache=spot_cache) else 0
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            futs = {ex.submit(run_one, r["code"], args, records, spot_cache): r for r in rows}
            for fut in as_completed(futs):
                ok += 1 if fut.result() else 0
    print(f"完成: {ok}/{len(rows)} 只")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
