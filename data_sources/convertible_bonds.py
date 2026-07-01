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
from urllib import parse

from .http import get_json


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
QUOTE_BOARD_URL = "https://push2.eastmoney.com/api/qt/clist/get"
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
            data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_QUOTE)
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


def fetch_jisilu_low_sample(ttl_hours=0.25):
    """Fetch Jisilu's anonymous low double-low sample for sanity checks."""
    url = JISILU_CB_URL + "?___jsl=LST___t=" + str(int(time.time() * 1000))
    data = get_json(url, ttl_hours=ttl_hours, headers=HEADERS_JISILU)
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

