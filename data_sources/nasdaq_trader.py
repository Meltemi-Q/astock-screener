#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nasdaq Trader Symbol Directory data source for US stock universe.

Downloads the official Nasdaq-listed and other-exchange-listed symbol files
(nasdaqlisted.txt, otherlisted.txt), parses them, filters out ETFs / funds /
non-common-stock instruments, and cross-references with the SEC CIK master.
"""

import re

from .http import get_text

# ── Constants ──────────────────────────────────────────────
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
NASDAQ_TTL_HOURS = 24


def _is_excluded_us_security(name, ticker):
    """Return True for instruments outside the ordinary-stock screener scope."""
    upper_name = (name or "").upper()
    upper_ticker = (ticker or "").upper()

    if upper_ticker.endswith("$"):
        return True
    if "TEST" in upper_name:
        return True

    if re.search(r"\b(ETF|ETN|FUND)\b", upper_name):
        return True

    non_common_pattern = (
        r"\b(UNIT|UNITS|WARRANT|WARRANTS|RIGHT|RIGHTS|"
        r"PREFERRED|PREFERENCE|PFD)\b|"
        r"\b(CONVERTIBLE|SENIOR)\s+NOTE\b"
    )
    if re.search(non_common_pattern, upper_name):
        return True

    spac_keywords = (
        "ACQUISITION CORP",
        "ACQUISITION CORPORATION",
        "BLANK CHECK",
        "SPAC",
    )
    if any(kw in upper_name for kw in spac_keywords):
        return True

    return False


def _parse_nasdaq_tsv(text):
    """Parse a Nasdaq pipe-delimited symbol file.

    The last line is a ``File Creation Time: ...`` footer and is skipped.
    Returns a list of dicts, one per symbol row.

    Args:
        text: Raw text content of the file.

    Returns:
        list[dict]: Each dict has keys matching the header row.
    """
    lines = text.strip().split("\n")
    if not lines:
        return []

    # Drop the trailing file-creation footer line
    if lines[-1].startswith("File Creation Time:"):
        lines = lines[:-1]

    # Header is the first line; remaining lines are data
    header = [h.strip() for h in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = [v.strip() for v in line.split("|")]
        if len(values) >= len(header):
            row = dict(zip(header, values))
            rows.append(row)
            # Pad with empty strings if some trailing columns are missing
            for h in header[len(values):]:
                row[h] = ""

    return rows


def fetch_nasdaq_listed():
    """Fetch and parse the Nasdaq-listed symbol directory.

    Returns:
        list[str]: Ticker symbols listed on Nasdaq.
    """
    text = get_text(NASDAQ_LISTED_URL, ttl_hours=NASDAQ_TTL_HOURS)
    rows = _parse_nasdaq_tsv(text)
    tickers = [r["Symbol"] for r in rows if r.get("Symbol")]
    print(f"  [Nasdaq Listed] {len(tickers)} symbols")
    return tickers


def fetch_other_listed():
    """Fetch and parse the other-exchange-listed symbol directory.

    Returns:
        list[str]: Ticker symbols listed on NYSE / NYSE ARCA.
    """
    text = get_text(OTHER_LISTED_URL, ttl_hours=NASDAQ_TTL_HOURS)
    rows = _parse_nasdaq_tsv(text)
    # otherlisted.txt uses "ACT Symbol" as the header
    ticker_key = "ACT Symbol" if "ACT Symbol" in (rows[0] if rows else {}) else "Symbol"
    tickers = [r.get(ticker_key, "") for r in rows if r.get(ticker_key)]
    print(f"  [Other Listed] {len(tickers)} symbols")
    return tickers


def build_us_stock_universe():
    """Build the full US stock universe from Nasdaq Trader symbol files.

    Downloads both nasdaqlisted.txt and otherlisted.txt, parses them,
    combines the results, and filters out:

    * ETFs / ETNs / funds
    * Test issues
    * Units / warrants / rights / preferred shares / SPAC shells

    Returns:
        list[dict]: Each dict has keys:
                    ``ticker`` (str), ``name`` (str), ``exchange`` (str),
                    ``market_category`` (str).

    Raises:
        AssertionError: If the result count is outside 3000-12000 or if
                        key tickers (AAPL, MSFT, NVDA, GOOGL) are missing.
    """
    # ── Nasdaq-listed ──
    try:
        nasdaq_text = get_text(NASDAQ_LISTED_URL, ttl_hours=NASDAQ_TTL_HOURS)
    except OSError as e:
        raise RuntimeError(
            f"Nasdaq Trader API 不可达: {e}\n"
            "  大陆用户请将 nasdaqtrader.com 加入代理直连规则，或设置 HTTPS_PROXY"
        ) from e
    nasdaq_rows = _parse_nasdaq_tsv(nasdaq_text)

    # ── Other-listed ──
    other_text = get_text(OTHER_LISTED_URL, ttl_hours=NASDAQ_TTL_HOURS)
    other_rows = _parse_nasdaq_tsv(other_text)

    # ── Build combined list ──
    universe = []

    for r in nasdaq_rows:
        ticker = r.get("Symbol", "").strip()
        name = r.get("Security Name", "").strip()
        if not ticker:
            continue
        exchange = "NASDAQ"
        market_cat = r.get("Market Category", "").strip()
        universe.append({
            "ticker": ticker,
            "name": name,
            "exchange": exchange,
            "market_category": market_cat,
        })

    # otherlisted.txt uses "ACT Symbol" and "Exchange" columns
    for r in other_rows:
        ticker = r.get("ACT Symbol", "").strip() or r.get("Symbol", "").strip()
        name = r.get("Security Name", "").strip()
        exchange_code = r.get("Exchange", "").strip()
        if not ticker:
            continue
        # Map exchange codes to readable names
        exchange_map = {
            "N": "NYSE",
            "A": "NYSE ARCA",
            "P": "NYSE ARCA",
            "Q": "NASDAQ",
            "Z": "BATS",
        }
        exchange = exchange_map.get(exchange_code, exchange_code)
        universe.append({
            "ticker": ticker,
            "name": name,
            "exchange": exchange,
            "market_category": "",
        })

    filtered = []
    removed = 0
    for r in universe:
        if _is_excluded_us_security(r["name"], r["ticker"]):
            removed += 1
            continue
        filtered.append(r)

    print(
        f"  [Universe] {len(filtered)} common-stock candidates "
        f"(removed {removed} funds/tests/non-common instruments)"
    )

    # ── Validation ──
    assert 3000 <= len(filtered) <= 12000, \
        f"US stock universe count {len(filtered)} outside expected range [3000, 12000]"

    tickers = {r["ticker"] for r in filtered}
    for required in ("AAPL", "MSFT", "NVDA", "GOOGL"):
        assert required in tickers, f"US stock universe missing {required}"

    return filtered


def merge_universe_with_sec(universe, sec_master):
    """Cross-reference Nasdaq universe with SEC ticker master by ticker.

    Adds the ``cik`` field from SEC to each universe entry.  Entries
    without a CIK match are kept but flagged with ``cik=None`` and a
    warning is printed.

    Args:
        universe: List of dicts from ``build_us_stock_universe``.
        sec_master: List of dicts from ``fetch_sec_ticker_master``.

    Returns:
        list[dict]: Each dict has an additional ``cik`` key (int or None)
                    and a ``has_cik`` key (bool).
    """
    # Build ticker → CIK lookup
    cik_map = {r["ticker"].upper(): r["cik"] for r in sec_master}

    merged = []
    missing = 0
    for r in universe:
        t = r["ticker"].upper()
        cik = cik_map.get(t)
        if cik is None:
            missing += 1
        merged.append({
            **r,
            "cik": cik,
            "has_cik": cik is not None,
        })

    print(f"  [Merge] {len(merged)} stocks, {missing} without CIK match "
          f"({missing / max(1, len(merged)) * 100:.1f}%)")
    return merged


# ── Quick self-test ────────────────────────────────────────
if __name__ == "__main__":
    print("=== Nasdaq Trader self-test ===\n")

    print("[1] Fetching Nasdaq-listed symbols...")
    nasdaq_tickers = fetch_nasdaq_listed()
    print(f"    Sample: {nasdaq_tickers[:5]}...")

    print("\n[2] Fetching other-listed symbols...")
    other_tickers = fetch_other_listed()
    print(f"    Sample: {other_tickers[:5]}...")

    print("\n[3] Building full US stock universe...")
    universe = build_us_stock_universe()
    for t in ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"):
        found = next((r for r in universe if r["ticker"] == t), None)
        if found:
            print(f"    {t}: name={found['name']}, exchange={found['exchange']}")
        else:
            print(f"    {t}: NOT FOUND ⚠")

    print("\n[4] Merging with SEC ticker master...")
    from .sec_edgar import fetch_sec_ticker_master
    master = fetch_sec_ticker_master()
    merged = merge_universe_with_sec(universe, master)
    with_cik = sum(1 for r in merged if r["has_cik"])
    print(f"    With CIK: {with_cik} / {len(merged)}")
    # Show a few examples
    for t in ("AAPL", "MSFT", "NVDA"):
        found = next((r for r in merged if r["ticker"] == t), None)
        if found:
            print(f"    {t}: CIK={found['cik']}, matched={found['has_cik']}")

    print("\n✅ Nasdaq Trader self-test complete")
