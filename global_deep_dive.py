#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-market deep-dive report generator for HK and US stocks.

This script writes the same JSON-backed payload shape used by the A-share
deep-dive report shell:

  results/deep_dives/report.html
  results/deep_dives/assets/*
  results/deep_dives/data/hk_01530.json
  results/deep_dives/data/us_CALM.json

It intentionally reuses the shared report shell instead of generating one HTML
file per stock.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib import error, parse, request

from data_sources.eastmoney import fetch_global_quote
from data_sources.http import get_json
from data_sources.hkex import fetch_eastmoney_hk_cashflow, fetch_eastmoney_hk_financials
from data_sources.sec_edgar import fetch_sec_ticker_master
from screeners.us import _safe_api_call as fetch_us_financial_bundle
from stock_deep_dive import (
    DEEPSEEK_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_RETRIES,
    SSL_CTX,
    atomic_write_json,
    ensure_deep_dive_app,
)


WORKDIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORKDIR, "results")
OUT_DIR = os.path.join(RESULTS_DIR, "deep_dives")
DATA_DIR_NAME = "data"

MARKETS = {
    "hk": {
        "label": "港股",
        "csv_prefix": "hkstock_screen",
        "stable_href": "hkstock_screen.html",
        "code_pattern": re.compile(r"^\d{5}$"),
        "currency": "HKD",
        "cap_key": "market_cap_yi_hkd",
        "default_min_buy_lot": 100,
    },
    "us": {
        "label": "美股",
        "csv_prefix": "usstock_screen",
        "stable_href": "usstock_screen.html",
        "code_pattern": re.compile(r"^[A-Za-z]{1,5}$"),
        "currency": "USD",
        "cap_key": "market_cap_yi_usd",
        "default_min_buy_lot": 1,
    },
}


def fnum(value):
    if value in (None, "", "-", "None", "nan"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_market_csv(market: str) -> tuple[str, str]:
    prefix = MARKETS[market]["csv_prefix"]
    files = sorted(
        f for f in os.listdir(RESULTS_DIR)
        if f.startswith(prefix + "_") and f.endswith(".csv")
    )
    if not files:
        raise FileNotFoundError(f"未找到 {MARKETS[market]['label']} 五层筛选 CSV")
    name = files[-1]
    ts = name.replace(prefix + "_", "").replace(".csv", "")
    return os.path.join(RESULTS_DIR, name), ts


def read_market_rows(market: str) -> tuple[list[dict], str]:
    csv_path, ts = latest_market_csv(market)
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows, ts


def normalize_tier(tier: str) -> str:
    return {
        "A": "A_可买入",
        "B": "B_优质待跌",
        "C": "C_接近合格",
    }.get(tier or "", tier or "-")


def short_tier(tier: str) -> str:
    return {
        "A_可买入": "A",
        "B_优质待跌": "B",
        "C_接近合格": "C",
    }.get(normalize_tier(tier), "-")


def payload_filename(market: str, code: str) -> str:
    return f"{market}_{code.upper() if market == 'us' else code}.json"


def payload_path(market: str, code: str) -> str:
    return os.path.join(OUT_DIR, DATA_DIR_NAME, payload_filename(market, code))


def payload_exists(market: str, code: str) -> bool:
    return os.path.exists(payload_path(market, code))


def _parse_day(row: dict) -> date:
    return date.fromisoformat(str(row["date"])[:10])


def aggregate_kline(rows: list[dict], period: str, limit: int) -> list[dict]:
    """Aggregate daily OHLCV rows to week/month rows."""
    if period == "day":
        return rows[-limit:]
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for row in rows:
        try:
            d = _parse_day(row)
        except Exception:
            continue
        if period == "week":
            iso = d.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        elif period == "month":
            key = f"{d.year}-{d.month:02d}"
        else:
            raise ValueError(f"unsupported period: {period}")
        groups.setdefault(key, []).append(row)

    out = []
    for chunk in groups.values():
        if not chunk:
            continue
        out.append({
            "date": chunk[-1]["date"],
            "open": fnum(chunk[0].get("open")),
            "close": fnum(chunk[-1].get("close")),
            "high": max(fnum(r.get("high")) or 0 for r in chunk),
            "low": min(fnum(r.get("low")) or 0 for r in chunk),
            "volume": sum(fnum(r.get("volume")) or 0 for r in chunk),
            "amount": sum(fnum(r.get("amount")) or 0 for r in chunk),
        })
    return out[-limit:]


def _tencent_symbols(market: str, code: str) -> list[str]:
    if market == "hk":
        return [f"hk{code}"]
    # Tencent's US K-line endpoint needs exchange suffixes for most tickers.
    code = code.upper()
    return [f"us{code}.OQ", f"us{code}.N", f"us{code}.A", f"us{code}"]


def _parse_tencent_klines(rows: list) -> list[dict]:
    parsed = []
    for parts in rows:
        if len(parts) < 6:
            continue
        try:
            parsed.append({
                "date": str(parts[0]),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except (TypeError, ValueError):
            continue
    return parsed


def fetch_tencent_kline(market: str, code: str, period: str, count: int) -> list[dict]:
    best: list[dict] = []
    for symbol in _tencent_symbols(market, code):
        param = f"{symbol},{period},,,{count},qfq"
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            + parse.urlencode({"param": param})
        )
        try:
            data = get_json(url, ttl_hours=24)
        except Exception:
            continue
        period_data = (data.get("data") or {}).get(symbol, {})
        raw = period_data.get(f"qfq{period}") or period_data.get(period) or []
        parsed = _parse_tencent_klines(raw)
        if len(parsed) > len(best):
            best = parsed
        if len(best) >= min(count, 30):
            break
    return best[-count:]


def fetch_kline_bundle(market: str, code: str, no_kline: bool = False) -> dict:
    if no_kline:
        return {"day": [], "week": [], "month": []}
    return {
        "day": fetch_tencent_kline(market, code, "day", 250),
        "week": fetch_tencent_kline(market, code, "week", 100),
        "month": fetch_tencent_kline(market, code, "month", 60),
    }


def row_for_code(rows: list[dict], market: str, code: str) -> dict:
    code = code.upper() if market == "us" else code
    for row in rows:
        row_code = (row.get("code") or "").upper() if market == "us" else row.get("code")
        if row_code == code:
            return row
    raise ValueError(f"{MARKETS[market]['label']}总表中未找到 {code}")


def choose_stocks(rows: list[dict], market: str, code: str | None, tier: str | None) -> list[dict]:
    if code:
        return [row_for_code(rows, market, code)]
    if tier:
        wanted = tier.upper()
        return [r for r in rows if short_tier(r.get("tier")) == wanted]
    return [r for r in rows if short_tier(r.get("tier")) in ("A", "B")]


def year_from_hk_report(row: dict) -> int | None:
    rd = row.get("report_date") or ""
    if len(rd) >= 4 and rd[:4].isdigit():
        return int(rd[:4])
    return None


def add_growth_fields(financials: list[dict]) -> list[dict]:
    by_year = {r["year"]: r for r in financials if r.get("year")}
    for row in financials:
        year = row.get("year")
        netp = row.get("netp")
        prev = by_year.get(year - 1) if year else None
        base = by_year.get(year - 3) if year else None
        row["netp_yoy"] = None
        row["cagr_netp"] = None
        if prev and netp is not None and prev.get("netp") not in (None, 0):
            row["netp_yoy"] = (netp - prev["netp"]) / abs(prev["netp"]) * 100.0
        if base and netp is not None and netp > 0 and base.get("netp") and base["netp"] > 0:
            row["cagr_netp"] = ((netp / base["netp"]) ** (1.0 / 3.0) - 1.0) * 100.0
    return financials


def convert_hk_financials(raw: list[dict], cashflow: dict[int, float]) -> list[dict]:
    out = []
    for row in raw:
        year = year_from_hk_report(row)
        if not year:
            continue
        netp = fnum(row.get("net_profit"))
        cf_oper = cashflow.get(year)
        out.append({
            "year": year,
            "rev": fnum(row.get("revenue")),
            "netp": netp,
            "roe": fnum(row.get("roe")),
            "gm": fnum(row.get("gross_margin")),
            "nm": fnum(row.get("net_margin")),
            "roa": None,
            "debt": fnum(row.get("debt_ratio")),
            "eps": None,
            "cf_oper": cf_oper,
            "ocf_ratio": (cf_oper / netp) if (cf_oper is not None and netp not in (None, 0)) else None,
        })
    out.sort(key=lambda r: r["year"])
    return add_growth_fields(out)[-6:]


def convert_us_financials(raw: list[dict]) -> list[dict]:
    out = []
    for row in raw:
        year = row.get("fiscal_year")
        netp = fnum(row.get("net_profit"))
        assets = fnum(row.get("assets"))
        cf_oper = fnum(row.get("operating_cashflow"))
        out.append({
            "year": year,
            "rev": fnum(row.get("revenue")),
            "netp": netp,
            "roe": fnum(row.get("roe")),
            "gm": fnum(row.get("gross_margin")),
            "nm": fnum(row.get("net_margin")),
            "roa": (netp / assets * 100.0) if (netp is not None and assets) else None,
            "debt": fnum(row.get("debt_ratio")),
            "eps": fnum(row.get("eps")),
            "cf_oper": cf_oper,
            "ocf_ratio": fnum(row.get("ocf_to_profit")),
        })
    out.sort(key=lambda r: r["year"] or 0)
    return add_growth_fields(out)[-6:]


def peers_from_rows(rows: list[dict], market: str, current: dict, limit: int = 10) -> list[dict]:
    industry = current.get("industry") or ""
    code = current.get("code") or ""
    candidates = []
    for row in rows:
        if row.get("code") == code:
            continue
        # HK industry coverage is still coarse; in that case use top ranked pool.
        if industry and industry != "港股" and row.get("industry") != industry:
            continue
        candidates.append(row)
    candidates.sort(key=lambda r: (fnum(r.get("score")) or 0, fnum(r.get("roe")) or 0), reverse=True)
    out = []
    for row in candidates[:limit]:
        pcode = row.get("code", "")
        out.append({
            "market": market,
            "code": pcode,
            "name": row.get("name", ""),
            "pe": fnum(row.get("pe_ttm")),
            "roe": fnum(row.get("roe")),
            "gm": fnum(row.get("gross_margin")),
            "mktcap": fnum(row.get(MARKETS[market]["cap_key"])),
            "tier": normalize_tier(row.get("tier")),
            "has_deep": payload_exists(market, pcode),
        })
    return out


def _quote_with_csv_fallback(market: str, code: str, row: dict) -> dict:
    quote = fetch_global_quote(market, code) or {}
    price = quote.get("price") or fnum(row.get("price"))
    market_cap = quote.get("market_cap")
    if market_cap is None:
        cap_yi = fnum(row.get(MARKETS[market]["cap_key"]))
        market_cap = cap_yi * 1e8 if cap_yi is not None else None
    return {
        "price": price,
        "pe_ttm": quote.get("pe_ttm") or fnum(row.get("pe_ttm")),
        "pe_dyn": fnum(row.get("pe_dyn")),
        "pb": quote.get("pb") or fnum(row.get("pb")),
        "mktcap": market_cap,
        "name": quote.get("name") or row.get("name") or code,
        "currency": quote.get("currency") or MARKETS[market]["currency"],
    }


def _min_buy(market: str, row: dict, price: float | None) -> float | None:
    if market == "us":
        return price
    value = fnum(row.get("min_buy"))
    if value is not None and value > 0:
        return value
    return price * MARKETS[market]["default_min_buy_lot"] if price is not None else None


def fetch_hk_deep(row: dict, rows: list[dict], screen_ts: str, no_kline: bool) -> tuple[dict, list[dict]]:
    code = row["code"]
    quote = _quote_with_csv_fallback("hk", code, row)
    raw_fin = fetch_eastmoney_hk_financials(code)
    cashflow = fetch_eastmoney_hk_cashflow(code)
    financials = convert_hk_financials(raw_fin, cashflow)
    stock = {
        "market": "hk",
        "code": code,
        "name": quote["name"],
        "industry": row.get("industry") or "港股",
        "currency": quote["currency"],
        "price": quote["price"],
        "min_buy": _min_buy("hk", row, quote["price"]),
        "pe_ttm": quote["pe_ttm"],
        "pe_dyn": quote["pe_dyn"],
        "pb": quote["pb"],
        "mktcap": quote["mktcap"],
        "screen_ts": screen_ts,
        "peers": peers_from_rows(rows, "hk", row),
        "kline": fetch_kline_bundle("hk", code, no_kline=no_kline),
    }
    return stock, financials


def _us_cik_for_code(code: str) -> tuple[int | None, str]:
    try:
        for row in fetch_sec_ticker_master():
            if row.get("ticker") == code.upper():
                return row.get("cik"), row.get("exchange", "")
    except Exception:
        return None, ""
    return None, ""


def fetch_us_deep(row: dict, rows: list[dict], screen_ts: str, year: int, no_kline: bool) -> tuple[dict, list[dict]]:
    code = row["code"].upper()
    quote = _quote_with_csv_fallback("us", code, row)
    cik, exchange = _us_cik_for_code(code)
    bundle = fetch_us_financial_bundle(cik, code, year) if cik else None
    financials = convert_us_financials(bundle.get("financials") if bundle else [])
    stock = {
        "market": "us",
        "code": code,
        "name": row.get("name") or quote["name"],
        "industry": row.get("industry") or exchange or "美股",
        "currency": "USD",
        "price": quote["price"],
        "min_buy": _min_buy("us", row, quote["price"]),
        "pe_ttm": quote["pe_ttm"],
        "pe_dyn": quote["pe_dyn"],
        "pb": quote["pb"],
        "mktcap": quote["mktcap"],
        "screen_ts": screen_ts,
        "peers": peers_from_rows(rows, "us", row),
        "kline": fetch_kline_bundle("us", code, no_kline=no_kline),
    }
    return stock, financials


def analyze_with_deepseek(stock: dict, financials: list[dict]):
    if not DEEPSEEK_KEY:
        return None
    market_label = MARKETS[stock["market"]]["label"]
    fy = financials[-1] if financials else {}
    prompt = f"""你是资深价值投资者，请对以下{market_label}上市公司做简洁、谨慎的定性分析。

股票: {stock.get('name')} ({stock.get('code')}) | 行业: {stock.get('industry')}
最新年报: {fy.get('year','?')}年
营收: {fy.get('rev','?')} | 净利润: {fy.get('netp','?')} | ROE: {fy.get('roe','?')}% | 毛利率: {fy.get('gm','?')}%
净利率: {fy.get('nm','?')}% | PE(TTM): {stock.get('pe_ttm','?')} | PB: {stock.get('pb','?')}
负债率: {fy.get('debt','?')}% | ROA: {fy.get('roa','?')}% | 现金流/净利: {fy.get('ocf_ratio','?')}
总市值: {(stock.get('mktcap') or 0)/1e8:.1f}亿 {stock.get('currency','')}

近三年财务趋势:
{chr(10).join(f"  {d.get('year')}: 营收{d.get('rev','?')} 净利{d.get('netp','?')} ROE{d.get('roe','?')}% 毛利{d.get('gm','?')}%" for d in financials[-3:])}

请按以下 JSON 格式输出，不要 Markdown，不要代码块：
{{"business_model":"...","moat":"...","moat_score":8,"growth":"...","industry_position":"...","management":"...","risks":"...","thesis":"...","value_trap_risk":"低/中/高","confidence":"高/中/低","qual_score":85}}"""
    req_payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是资深价值投资者，回答用中文，简洁、审慎，不构成投资建议。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 1500,
        "user_id": f"economy_deep_dive_{stock['market']}",
    }
    if DEEPSEEK_MODEL.startswith("deepseek-v4-"):
        req_payload["thinking"] = {"type": "disabled"}

    last_err = None
    for attempt in range(max(1, DEEPSEEK_RETRIES)):
        try:
            api_req = request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps(req_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp = json.loads(request.urlopen(api_req, timeout=60, context=SSL_CTX).read())
            content = resp["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```\w*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            return json.loads(content)
        except error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            last_err = str(exc)
            time.sleep(1.0 * (attempt + 1))
    print(f"    DeepSeek API 错误: {last_err}")
    return None


def build_payload(stock: dict, financials: list[dict], analysis) -> dict:
    mktcap = stock.get("mktcap")
    now = time.strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {
            "market": stock["market"],
            "market_label": MARKETS[stock["market"]]["label"],
            "code": stock["code"],
            "name": stock.get("name") or stock["code"],
            "industry": stock.get("industry") or "",
            "currency": stock.get("currency") or MARKETS[stock["market"]]["currency"],
            "screen_ts": stock.get("screen_ts") or time.strftime("%Y%m%d"),
            # generated_at 保留作向后兼容；量化数据与 AI 分析各自独立时间戳
            "generated_at": now,
            "data_generated_at": now,
            "analysis_generated_at": now if analysis else None,
        },
        "quote": {
            "price": stock.get("price"),
            "min_buy": stock.get("min_buy"),
            "pe_ttm": stock.get("pe_ttm"),
            "pe_dyn": stock.get("pe_dyn"),
            "pb": stock.get("pb"),
            "mktcap": mktcap,
            "mktcap_yi": mktcap / 1e8 if mktcap else None,
            "currency": stock.get("currency"),
        },
        "financials": financials,
        "peers": stock.get("peers") or [],
        "kline": stock.get("kline") or {"day": [], "week": [], "month": []},
        "analysis": analysis,
    }


def write_payload(stock: dict, financials: list[dict], analysis) -> str:
    ensure_deep_dive_app(OUT_DIR)
    path = payload_path(stock["market"], stock["code"])
    atomic_write_json(path, build_payload(stock, financials, analysis))
    return path


def run_ai_only(market: str, code: str) -> bool:
    path = payload_path(market, code)
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    stock = {
        "market": market,
        "code": payload.get("meta", {}).get("code") or code,
        "name": payload.get("meta", {}).get("name") or code,
        "industry": payload.get("meta", {}).get("industry") or "",
        "currency": payload.get("meta", {}).get("currency") or MARKETS[market]["currency"],
        **(payload.get("quote") or {}),
    }
    analysis = analyze_with_deepseek(stock, payload.get("financials") or [])
    if not analysis:
        return False
    payload["analysis"] = analysis
    # --ai-only 只刷新 AI 分析时间戳，量化数据抓取时间 data_generated_at 保持不变
    meta = payload.setdefault("meta", {})
    meta["analysis_generated_at"] = time.strftime("%Y-%m-%d %H:%M")
    meta.setdefault("data_generated_at", meta.get("generated_at"))
    atomic_write_json(path, payload)
    ensure_deep_dive_app(OUT_DIR)
    return True


def process_one(market: str, row: dict, rows: list[dict], screen_ts: str, year: int,
                no_llm: bool, no_kline: bool, ai_only: bool) -> tuple[str, bool]:
    code = row["code"].upper() if market == "us" else row["code"]
    print(f"  [{market}:{code}] 抓取深度数据...", flush=True)
    if ai_only:
        ok = run_ai_only(market, code) if not no_llm else payload_exists(market, code)
        return code, ok
    if market == "hk":
        stock, financials = fetch_hk_deep(row, rows, screen_ts, no_kline)
    elif market == "us":
        stock, financials = fetch_us_deep(row, rows, screen_ts, year, no_kline)
    else:
        raise ValueError(f"unsupported market: {market}")
    analysis = None if no_llm else analyze_with_deepseek(stock, financials)
    path = write_payload(stock, financials, analysis)
    print(f"    写入 {path} ({len(financials)}期财报)")
    return code, True


def main() -> int:
    ap = argparse.ArgumentParser(description="港股/美股个股深度研报生成器")
    ap.add_argument("--market", required=True, choices=sorted(MARKETS), help="市场: hk/us")
    ap.add_argument("--code", help="单独分析一只股票")
    ap.add_argument("--tier", choices=["A", "B", "C"], help="仅分析指定评级")
    ap.add_argument("--year", type=int, default=2025, help="年报口径年份")
    ap.add_argument("--parallel", type=int, default=6, help="并行生成线程数")
    ap.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek AI 分析")
    ap.add_argument("--ai-only", action="store_true", help="只对已有 JSON 补 AI")
    ap.add_argument("--no-kline", action="store_true", help="跳过 K线抓取")
    args = ap.parse_args()

    market = args.market
    code = args.code.upper() if market == "us" and args.code else args.code
    if code and not MARKETS[market]["code_pattern"].match(code):
        ap.error(f"{MARKETS[market]['label']}代码格式不正确: {code}")

    os.makedirs(OUT_DIR, exist_ok=True)
    rows, screen_ts = read_market_rows(market)
    stocks = choose_stocks(rows, market, code, args.tier)
    if not stocks:
        print("❌ 未找到符合条件的股票")
        return 1

    mode = "AI-only" if args.ai_only else ("含 AI" if not args.no_llm and DEEPSEEK_KEY else "仅量化")
    print(f"\n{'=' * 60}")
    print(f"{MARKETS[market]['label']}深度研报生成 | {len(stocks)} 只 | {mode}")
    print(f"{'=' * 60}\n")

    ok = 0
    if len(stocks) == 1 or args.parallel <= 1:
        for row in stocks:
            _, done = process_one(market, row, rows, screen_ts, args.year, args.no_llm, args.no_kline, args.ai_only)
            ok += int(done)
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            futures = {
                ex.submit(process_one, market, row, rows, screen_ts, args.year,
                          args.no_llm, args.no_kline, args.ai_only): row
                for row in stocks
            }
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    _, done = fut.result()
                    ok += int(done)
                except Exception as exc:
                    print(f"  ⚠ {row.get('code')} 失败: {exc}")

    print(f"\n完成: {ok}/{len(stocks)}")
    return 0 if ok == len(stocks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
