#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convertible bond data sources.

The double-low screener needs two kinds of data:

- Eastmoney datacenter: full convertible-bond terms plus delayed quote columns
  for current bond price and conversion premium.
- Eastmoney quote board: active listed convertible bonds with market value,
  used to estimate remaining issue amount.

Jisilu is useful for a small cross-check sample, but its anonymous endpoint can
be capped, so the production universe is built from Eastmoney public endpoints.
"""

from __future__ import annotations

import time
from datetime import date, datetime
import re
from urllib import parse

from .http import get_json, get_text


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
QUOTE_BOARD_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
JISILU_CB_URL = "https://www.jisilu.cn/data/cbnew/cb_list_new/"

CB_QUOTE_COLUMNS = (
    "f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,"
    "f235~10~SECURITY_CODE~TRANSFER_PRICE,"
    "f236~10~SECURITY_CODE~TRANSFER_VALUE,"
    "f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,"
    "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO,"
    "f239~10~SECURITY_CODE~RESALE_TRIG_PRICE,"
    "f240~10~SECURITY_CODE~REDEEM_TRIG_PRICE,"
    "f23~01~CONVERT_STOCK_CODE~PBV_RATIO"
)

CB_QUOTE_FIELDS = "f2,f3,f12,f14,f15,f16,f17,f18,f20,f21,f22,f62"
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

HEADERS_EM = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/kzz/default.html",
}

HEADERS_QUOTE = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html#convertible_bond",
}

HEADERS_JISILU = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.jisilu.cn/web/data/cb/list",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def fnum(value):
    """Return float or None for Eastmoney/Jisilu placeholders."""
    if value in (None, "", "-", "None", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_ymd(value):
    """Parse Eastmoney datetime strings into date objects."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19 if " " in s else 10], fmt).date()
        except ValueError:
            continue
    return None


def years_until(value, today=None):
    """Return years from today until the given date string."""
    today = today or date.today()
    d = parse_ymd(value)
    if not d:
        return None
    return round((d - today).days / 365.25, 3)


def parse_maturity_redeem_price(*texts):
    """Extract the maturity redemption price per 100 par from clause text."""
    joined = "\n".join(str(t or "") for t in texts if t)
    if not joined:
        return None
    patterns = [
        r"(?:到期赎回条款|到期赎回|期满后|到期后)[\s\S]{0,120}?(?:票面面值|债券面值|面值)的?(\d+(?:\.\d+)?)%",
        r"到期赎回价(?:格)?(?:为|:|：)?\s*(\d+(?:\.\d+)?)\s*元",
        r"期满后[\s\S]{0,80}?(\d+(?:\.\d+)?)\s*元",
    ]
    for pattern in patterns:
        m = re.search(pattern, joined)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                return None
    return None


def has_conditional_resale_clause(text):
    """Return True for a normal holder put clause, excluding only change-of-use puts."""
    s = str(text or "")
    if not s:
        return False
    if "有条件回售" in s:
        return True
    return "低于当期转股" in s and ("回售" in s or "持有人有权" in s)


def fetch_eastmoney_cb_terms(page_size=200, ttl_hours=2, quote_type="0"):
    """Fetch full convertible-bond terms and quote columns from Eastmoney."""
    rows = []
    page = 1
    pages = None
    while pages is None or page <= pages:
        params = {
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "pageSize": str(page_size),
            "pageNumber": str(page),
            "reportName": "RPT_BOND_CB_LIST",
            "columns": "ALL",
            "quoteColumns": CB_QUOTE_COLUMNS,
            "quoteType": quote_type,
            "source": "WEB",
            "client": "WEB",
        }
        url = DATACENTER_URL + "?" + parse.urlencode(params)
        data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_EM)
        result = data.get("result") or {}
        if not data.get("success") and not result:
            raise RuntimeError(f"Eastmoney cb terms failed: {data}")
        pages = int(result.get("pages") or 0)
        chunk = result.get("data") or []
        rows.extend(chunk)
        if not chunk:
            break
        page += 1
        time.sleep(0.08)
    return rows


def fetch_eastmoney_cb_quote_board(page_size=100, ttl_hours=0.5):
    """Fetch active listed convertible-bond quotes from Eastmoney quote board."""
    out = {}
    # m:0 = Shenzhen, m:1 = Shanghai. b:MK0354 is the convertible-bond board.
    for fs in ("m:0+b:MK0354", "m:1+b:MK0354"):
        page = 1
        total = None
        while total is None or (page - 1) * page_size < total:
            params = {
                "pn": str(page),
                "pz": str(page_size),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": fs,
                "fields": CB_QUOTE_FIELDS,
            }
            url = QUOTE_BOARD_URL + "?" + parse.urlencode(params)
            data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_QUOTE, timeout=8, retries=2)
            result = data.get("data") or {}
            total = int(result.get("total") or 0)
            diff = result.get("diff") or []
            for r in diff:
                code = str(r.get("f12") or "").strip()
                if code:
                    out[code] = parse_quote_board_row(r)
            if not diff:
                break
            page += 1
            time.sleep(0.08)
    return out


def parse_quote_board_row(row):
    """Parse an Eastmoney quote-board row."""
    price = fnum(row.get("f2"))
    market_value = fnum(row.get("f20"))
    remaining_scale = None
    if price and market_value:
        # f20 is market value in yuan. Convertible quote price is per 100 par.
        remaining_scale = market_value / (price / 100.0) / 100_000_000
    return {
        "code": str(row.get("f12") or "").strip(),
        "name": str(row.get("f14") or "").strip(),
        "quote_price": price,
        "change_pct": fnum(row.get("f3")),
        "high": fnum(row.get("f15")),
        "low": fnum(row.get("f16")),
        "open": fnum(row.get("f17")),
        "prev_close": fnum(row.get("f18")),
        "market_value": market_value,
        "remaining_scale": remaining_scale,
        "turnover": fnum(row.get("f21")),
        "amplitude": fnum(row.get("f22")),
        "net_inflow": fnum(row.get("f62")),
    }


def cbond_market_id(code: str) -> str:
    """Return Eastmoney secid market prefix for a convertible bond code."""
    code = str(code or "").strip()
    # Shanghai convertible bonds commonly start with 110/111/113/118.
    return "1" if code.startswith(("110", "111", "113", "118")) else "0"


def fetch_eastmoney_cb_kline(code, klt="101", limit=260, ttl_hours=0.5):
    """Fetch convertible-bond K-line rows from Eastmoney push2his.

    klt: 101=daily, 102=weekly, 103=monthly.
    """
    secid = f"{cbond_market_id(code)}.{str(code).strip()}"
    params = {
        "secid": secid,
        "fields1": KLINE_FIELDS1,
        "fields2": KLINE_FIELDS2,
        "klt": str(klt),
        "fqt": "1",
        "beg": "19900101",
        "end": "20500101",
        "lmt": str(limit),
    }
    url = KLINE_URL + "?" + parse.urlencode(params)
    data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_QUOTE)
    rows = ((data.get("data") or {}).get("klines") or [])
    parsed = []
    for line in rows:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            parsed.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": fnum(parts[6]) if len(parts) > 6 else None,
                "amplitude": fnum(parts[7]) if len(parts) > 7 else None,
                "change_pct": fnum(parts[8]) if len(parts) > 8 else None,
                "change": fnum(parts[9]) if len(parts) > 9 else None,
                "turnover": fnum(parts[10]) if len(parts) > 10 else None,
            })
        except (TypeError, ValueError):
            continue
    return parsed


def fetch_tencent_cb_kline(code, period_key="day", limit=260, ttl_hours=0.5):
    """Fetch convertible-bond K-lines from Tencent as a fallback."""
    code = str(code).strip()
    prefix = "sh" if cbond_market_id(code) == "1" else "sz"
    params = {
        "param": f"{prefix}{code},{period_key},,,{limit},qfq",
    }
    url = TENCENT_KLINE_URL + "?" + parse.urlencode(params)
    raw = get_text(url, ttl_hours=ttl_hours, headers=HEADERS_QUOTE)
    import json
    data = json.loads(raw)
    stock_key = f"{prefix}{code}"
    period_data = (data.get("data") or {}).get(stock_key, {})
    rows = period_data.get(f"qfq{period_key}") or period_data.get(period_key) or []
    parsed = []
    for parts in rows:
        if len(parts) < 6:
            continue
        try:
            parsed.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": fnum(parts[6]) if len(parts) > 6 else None,
            })
        except (TypeError, ValueError):
            continue
    return parsed


def fetch_jisilu_low_sample(ttl_hours=0.25):
    """Fetch Jisilu's anonymous low double-low sample for sanity checks."""
    url = JISILU_CB_URL + "?___jsl=LST"
    data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_JISILU, timeout=8, retries=2)
    return [(r.get("cell") or {}) for r in (data.get("rows") or [])]


def build_convertible_bond_universe(ttl_hours=2, quote_ttl_hours=0.5, today=None):
    """Merge Eastmoney terms and quote-board data into normalized records."""
    today = today or date.today()
    terms = fetch_eastmoney_cb_terms(ttl_hours=ttl_hours)
    quotes = fetch_eastmoney_cb_quote_board(ttl_hours=quote_ttl_hours)
    records = []
    for row in terms:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not code:
            continue
        q = quotes.get(code, {})
        price = q.get("quote_price")
        if price is None:
            price = fnum(row.get("CURRENT_BOND_PRICE"))
        if price is None:
            price = fnum(row.get("CURRENT_BOND_PRICENEW"))
        premium = fnum(row.get("TRANSFER_PREMIUM_RATIO"))
        remaining_years = years_until(row.get("EXPIRE_DATE"), today=today)
        remaining_scale = q.get("remaining_scale")
        original_scale = fnum(row.get("ACTUAL_ISSUE_SCALE"))
        if remaining_scale is None:
            remaining_scale = original_scale
        redeem_clause = row.get("REDEEM_CLAUSE") or ""
        resale_clause = row.get("RESALE_CLAUSE") or ""
        records.append({
            "code": code,
            "name": str(row.get("SECURITY_NAME_ABBR") or q.get("name") or "").strip(),
            "stock_code": str(row.get("CONVERT_STOCK_CODE") or "").strip(),
            "stock_name": str(row.get("SECURITY_SHORT_NAME") or "").strip(),
            "price": price,
            "change_pct": q.get("change_pct"),
            "premium_rt": premium,
            "double_low": round(price + premium, 3) if price is not None and premium is not None else None,
            "rating": normalize_rating(row.get("RATING")),
            "rating_raw": row.get("RATING"),
            "remaining_scale": remaining_scale,
            "original_scale": original_scale,
            "remaining_years": remaining_years,
            "maturity_date": row.get("EXPIRE_DATE"),
            "listing_date": row.get("LISTING_DATE"),
            "delist_date": row.get("DELIST_DATE"),
            "convert_price": fnum(row.get("TRANSFER_PRICE")),
            "convert_value": fnum(row.get("TRANSFER_VALUE")),
            "stock_price": fnum(row.get("CONVERT_STOCK_PRICE")),
            "pb": fnum(row.get("PBV_RATIO")),
            "resale_trigger_price": fnum(row.get("RESALE_TRIG_PRICE")),
            "redeem_trigger_price": fnum(row.get("REDEEM_TRIG_PRICE")),
            "maturity_redeem_price": parse_maturity_redeem_price(
                redeem_clause,
                row.get("INTEREST_RATE_EXPLAIN"),
            ),
            "has_conditional_resale": has_conditional_resale_clause(resale_clause),
            "is_redeem": row.get("IS_REDEEM"),
            "redeem_type": row.get("REDEEM_TYPE"),
            "execute_reason_sh": row.get("EXECUTE_REASON_SH"),
            "execute_reason_hs": row.get("EXECUTE_REASON_HS"),
            "notice_date_sh": row.get("NOTICE_DATE_SH"),
            "notice_date_hs": row.get("NOTICE_DATE_HS"),
            "execute_start_date_sh": row.get("EXECUTE_START_DATESH"),
            "execute_start_date_hs": row.get("EXECUTE_START_DATEHS"),
            "execute_end_date": row.get("EXECUTE_END_DATE"),
            "record_date_sh": row.get("RECORD_DATE_SH"),
            "has_quote_board": code in quotes,
            "turnover": q.get("turnover"),
            "source": "eastmoney_datacenter+quote_board",
        })
    return records


def normalize_rating(value):
    """Normalize rating strings such as AA+sti -> AA+."""
    if not value:
        return ""
    s = str(value).strip().upper()
    for suffix in ("STI", "（稳定）", "(稳定)"):
        s = s.replace(suffix, "")
    return s.strip()
