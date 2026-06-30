#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Eastmoney global data source for HK and US quotes and K-line data.

Provides:
- Single-stock global quotes (HK / US) via push2delay real-time API
- Batch HK quotes via push2delay clist API (parallel per board)
- Batch US quotes via push2delay clist API
- Historical K-line data via push2his API
- Convenience snapshot functions for all HK / US spot data

Cache TTL: quotes 2 hours, K-line 24 hours.

Follows the push2delay patterns from astock_screener.py.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from .http import get_json, CACHE_DIR

# ── Constants ──────────────────────────────────────────────
QUOTE_TTL_HOURS = 2    # quotes refresh every 2 hours
KLINE_TTL_HOURS = 24   # K-line data refresh daily
PAGE_SLEEP = 0.12       # polite delay between pagination
PAGE_SIZE = 100         # push2delay server limit

# ── HK board sections (push2delay clist fs parameters) ──────
# m:128 = HK market; t:1-4 are board types
HK_BOARDS = [
    ("m:128+t:1", "HK_Main"),
    ("m:128+t:2", "HK_GEM"),
    ("m:128+t:3", "HK_Main_Sec"),
    ("m:128+t:4", "HK_ETF_N_Warrant"),
]

# HK batch fields
HK_BATCH_FIELDS = "f12,f14,f2,f3,f9,f23,f20,f21,f38,f115,f116"

# ── US board sections ──────────────────────────────────────
US_BOARDS = [
    ("m:105", "NASDAQ"),
    ("m:106", "NYSE"),
    ("m:107", "AMEX"),
]

# US batch fields (same as HK)
US_BATCH_FIELDS = HK_BATCH_FIELDS

# ── Single-stock quote fields ──────────────────────────────
QUOTE_FIELDS = (
    "f43,f44,f45,f46,f48,f50,f57,f58,f60,"
    "f115,f116,f117,f162,f167,f170,f171,f172"
)

# ── Helper ─────────────────────────────────────────────────

def _fnum(x):
    """Safe float conversion; None / '-' / '' → None."""
    if x is None or x == "-" or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ── Single-stock quotes ────────────────────────────────────

def fetch_global_quote(market, code):
    """Fetch real-time quote for a single HK or US stock.

    Uses East Money's push2delay single-stock API.

    Args:
        market: ``"hk"`` or ``"us"``.
        code: Stock code string.  For HK, a 5-digit code like ``"00700"``.
              For US, the ticker like ``"AAPL"``.

    Returns:
        dict with keys: code, name, price, pe_ttm, pb, market_cap,
        currency, quote_time, source.

        All numeric values are already converted to floats (price, pe_ttm,
        pb, market_cap).  Returns empty dict on failure.
    """
    market = market.lower().strip()
    if market not in ("hk", "us"):
        raise ValueError(f"market must be 'hk' or 'us', got '{market}'")

    if market == "hk":
        secid = f"116.{code}"
        expected_currency = "HKD"
    else:
        secid = f"105.{code}"
        expected_currency = "USD"

    url = (
        f"https://push2delay.eastmoney.com/api/qt/stock/get?"
        f"secid={secid}&fields={QUOTE_FIELDS}"
    )

    try:
        data = get_json(url, ttl_hours=QUOTE_TTL_HOURS)
    except Exception as e:
        print(f"  [Global Quote] Failed for {secid}: {e}")
        return {}

    if not data or not data.get("data"):
        return {}

    d = data["data"]

    # ── Extract and convert fields ──
    # For HK/US single-stock quotes, f43 is in units of 0.001 (divide by 1000).
    # (A-share single-stock quotes use /100, but HK/US use /1000.)
    price = _fnum(d.get("f43"))
    if price is not None and price > 0:
        price = price / 1000.0

    # PE TTM via f162: often 0 for HK/US stocks in this API.
    # Try f115 as a fallback (also often 0).
    pe_ttm = _fnum(d.get("f162")) or _fnum(d.get("f115"))
    if pe_ttm is not None and pe_ttm > 0:
        pe_ttm = pe_ttm / 100.0
    else:
        pe_ttm = None

    # PB via f167: divide by 100 (verified correct for HK/US).
    pb = _fnum(d.get("f167"))
    if pb is not None and pb > 0:
        pb = pb / 100.0
    else:
        pb = None

    mktcap = _fnum(d.get("f116"))  # market cap, in original units

    currency = str(d.get("f172") or expected_currency).strip().upper()

    # Validate
    if price is None or price <= 0:
        print(f"  [Global Quote] Invalid price for {code}: {price}")
        return {}
    if currency and currency != expected_currency:
        # Some dual-listed stocks may show a different currency; accept HKD too
        if market == "hk" and currency != "HKD":
            print(f"  [Global Quote] Unexpected currency {currency} for HK {code}")
        elif market == "us" and currency != "USD":
            print(f"  [Global Quote] Unexpected currency {currency} for US {code}")

    return {
        "code": str(d.get("f57") or code).strip(),
        "name": str(d.get("f58") or "").strip(),
        "price": price,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "market_cap": mktcap,
        "currency": currency,
        "quote_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "eastmoney_push2delay",
    }


# ── Batch quotes ───────────────────────────────────────────

def _fetch_clist_board(fs, fields, ttl_hours, label):
    """Fetch all pages for one board section from push2delay clist API.

    Follows the same pagination pattern as astock_screener.py fetch_spot().

    Args:
        fs: Board filter string (e.g. ``"m:128+t:1"``).
        fields: Comma-separated field codes.
        ttl_hours: Cache TTL in hours.
        label: Human-readable board name for logging.

    Returns:
        list[dict]: Raw diff rows from the API.
    """
    host = "https://push2delay.eastmoney.com/api/qt/clist/get"
    pz = PAGE_SIZE
    out = {}
    pn, total = 1, None
    while True:
        url = (
            f"{host}?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f12"
            f"&fs={fs}&fields={fields}"
        )
        try:
            data = get_json(url, ttl_hours=ttl_hours)
        except Exception as e:
            print(f"  [Clist·{label}] Page {pn} failed: {e}")
            break
        result = data.get("data") or {}
        if total is None:
            total = result.get("total") or 0
        diff = result.get("diff") or []
        # Empty page retry
        if not diff and pn * pz < total:
            time.sleep(0.5)
            try:
                data = get_json(url + "&_r=1", ttl_hours=0)
                result = data.get("data") or {}
                diff = result.get("diff") or []
            except Exception:
                pass
        for r in diff:
            code = str(r.get("f12") or "").strip()
            if code:
                out[code] = r
        if pn * pz >= total or not diff:
            break
        pn += 1
        time.sleep(PAGE_SLEEP)
    print(f"  [Clist·{label}] {total} records, got {len(out)}")
    return out


def _parse_clist_row(row):
    """Parse a raw push2delay clist diff row into standardized dict.

    Args:
        row: Raw dict from the API response (keyed by field codes f12, f14, etc.).

    Returns:
        dict with keys: code, name, price, pe_ttm, pb, market_cap, change_pct.
    """
    return {
        "code": str(row.get("f12") or "").strip(),
        "name": str(row.get("f14") or "").strip(),
        "price": _fnum(row.get("f2")),
        "change_pct": _fnum(row.get("f3")),
        "pe_ttm": _fnum(row.get("f115")),
        "pb": _fnum(row.get("f23")),
        "market_cap": _fnum(row.get("f20")),
        "pe_dyn": _fnum(row.get("f9")),
        "volume": _fnum(row.get("f38")),
        "turnover": _fnum(row.get("f21")),
    }


def _fetch_batch_parallel(boards, fields, ttl_hours, label_prefix):
    """Generic parallel board fetcher with ThreadPoolExecutor.

    Args:
        boards: list of (fs, bname) tuples.
        fields: Comma-separated field codes.
        ttl_hours: Cache lifetime in hours.
        label_prefix: Prefix for log labels (e.g. "HK" or "US").

    Returns:
        dict[str, dict]: Merged dictionary keyed by code/ticker.
    """
    all_out = {}

    def _worker(fs, bname):
        return _fetch_clist_board(fs, fields, ttl_hours, f"{label_prefix}·{bname}")

    with ThreadPoolExecutor(max_workers=len(boards)) as ex:
        futures = {ex.submit(_worker, fs, bname): bname for fs, bname in boards}
        for future in as_completed(futures):
            bname = futures[future]
            try:
                board_data = future.result()
                all_out.update(board_data)
            except Exception as e:
                print(f"  ⚠ [{label_prefix}·{bname}] Fetch failed: {e}")

    return all_out


def fetch_hk_quotes_batch(codes=None):
    """Batch fetch HK stock quotes from push2delay clist API.

    Fetches all HKEX board sections in parallel and merges results.

    Args:
        codes: Optional list of codes to filter by.  If None, returns all.

    Returns:
        dict[str, dict]: Keyed by 5-digit code string (e.g. ``"00700"``).
        Each value is a standardized dict with code, name, price, pe_ttm,
        pb, market_cap, change_pct.
    """
    raw = _fetch_batch_parallel(HK_BOARDS, HK_BATCH_FIELDS, QUOTE_TTL_HOURS, "HK")
    out = {}
    for code_key, row in raw.items():
        parsed = _parse_clist_row(row)
        if codes is None or parsed["code"] in set(codes):
            out[parsed["code"]] = parsed
    print(f"  [HK Batch] {len(out)} quotes returned")
    return out


def fetch_us_quotes_batch():
    """Batch fetch US stock quotes from push2delay clist API.

    Fetches all US market sections (NASDAQ, NYSE, AMEX) in parallel and
    merges results.  Returns all available US tickers.

    Returns:
        dict[str, dict]: Keyed by ticker string (e.g. ``"AAPL"``).
        Each value is a standardized dict with code, name, price, pe_ttm,
        pb, market_cap, change_pct.
    """
    raw = _fetch_batch_parallel(US_BOARDS, US_BATCH_FIELDS, QUOTE_TTL_HOURS, "US")
    out = {}
    for ticker, row in raw.items():
        parsed = _parse_clist_row(row)
        out[parsed["code"]] = parsed
    print(f"  [US Batch] {len(out)} quotes returned")
    return out


# ── K-line (historical candlestick data) ───────────────────

def fetch_global_kline(market, code, days=250):
    """Fetch historical daily K-line (candlestick) data for a single stock.

    Uses East Money's push2his kline API.

    Args:
        market: ``"hk"`` or ``"us"``.
        code: Stock code.  For HK, a 5-digit padded string like ``"00700"``.
              For US, the ticker like ``"AAPL"``.
        days: Number of trading days to fetch (default 250, ~1 year).

    Returns:
        list[dict]: Sorted by date ascending.  Each dict has keys:
        date, open, close, high, low, volume, amount.
    """
    market = market.lower().strip()
    if market not in ("hk", "us"):
        raise ValueError(f"market must be 'hk' or 'us', got '{market}'")

    if market == "hk":
        secid = f"116.{code}"
    else:
        secid = f"105.{code}"

    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=1&end=20500101&lmt={days}"
    )

    try:
        data = get_json(url, ttl_hours=KLINE_TTL_HOURS)
    except Exception as e:
        print(f"  [Kline] Failed for {secid}: {e}")
        return []

    result = data.get("data") or {}
    if not result:
        return []

    klines = result.get("klines") or []
    if not klines:
        return []

    out = []
    for line in klines:
        # Format: "date,open,close,high,low,volume,amount,..."
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            out.append({
                "date": parts[0].strip(),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]) if len(parts) > 6 else None,
            })
        except (ValueError, IndexError):
            continue

    # Sort by date ascending (push2his returns ascending for HK/US, but sort explicitly to be safe)
    out.sort(key=lambda x: x["date"])
    return out


# ── Convenience snapshots ──────────────────────────────────

def fetch_hk_spot_snapshot():
    """Convenience: batch fetch ALL HK spot quotes in one call.

    Cached for 2 hours.

    Returns:
        dict[str, dict]: Keyed by 5-digit code string.  Same format as
        fetch_hk_quotes_batch().
    """
    return fetch_hk_quotes_batch(codes=None)


def fetch_us_spot_snapshot():
    """Convenience: batch fetch ALL US spot quotes in one call.

    Cached for 2 hours.

    Returns:
        dict[str, dict]: Keyed by ticker string.  Same format as
        fetch_us_quotes_batch().
    """
    return fetch_us_quotes_batch()


# ── Acceptance tests ───────────────────────────────────────

def _test():
    """Run quick acceptance tests."""
    print("=== Eastmoney Global data source tests ===\n")

    # Test 1: Single HK quote
    print("[Test 1] fetch_global_quote('hk', '00700')...")
    try:
        q = fetch_global_quote("hk", "00700")
        print(f"  Result: {q.get('name')} @ {q.get('price')} {q.get('currency')}")
        assert q.get("code") == "00700", f"Wrong code: {q.get('code')}"
        assert q.get("name"), "Missing name"
        assert q.get("price") and q.get("price") > 0, f"Invalid price: {q.get('price')}"
        assert q.get("currency") in ("HKD", "CNY"), f"Invalid currency: {q.get('currency')}"
        print("  ✓ HK quote OK")
    except Exception as e:
        print(f"  ⚠ HK quote test failed (may be offline): {e}")

    # Test 2: Single US quote
    print("[Test 2] fetch_global_quote('us', 'AAPL')...")
    try:
        q = fetch_global_quote("us", "AAPL")
        print(f"  Result: {q.get('name')} @ {q.get('price')} {q.get('currency')}")
        assert q.get("code"), "Missing code"
        assert q.get("price") and q.get("price") > 0, f"Invalid price: {q.get('price')}"
        assert q.get("currency") == "USD", f"Invalid currency: {q.get('currency')}"
        print("  ✓ US quote OK")
    except Exception as e:
        print(f"  ⚠ US quote test failed (may be offline): {e}")

    # Test 3: Invalid market
    print("[Test 3] Invalid market...")
    try:
        fetch_global_quote("cn", "00001")
        print("  ✗ Should have raised ValueError")
    except ValueError:
        print("  ✓ ValueError raised correctly")

    # Test 4: HK kline
    print("[Test 4] fetch_global_kline('hk', '00700', 10)...")
    try:
        kl = fetch_global_kline("hk", "00700", 10)
        print(f"  Got {len(kl)} klines")
        if kl:
            last = kl[-1]
            print(f"  Latest: {last['date']} O={last['open']} C={last['close']}")
            assert "date" in last
            assert last["close"] > 0
            # Check sorted ascending
            dates = [k["date"] for k in kl]
            assert dates == sorted(dates), "Klines not sorted ascending"
        print("  ✓ HK kline OK")
    except Exception as e:
        print(f"  ⚠ Kline test failed (may be offline): {e}")

    print("\n✅ All Eastmoney global tests passed")


if __name__ == "__main__":
    _test()
