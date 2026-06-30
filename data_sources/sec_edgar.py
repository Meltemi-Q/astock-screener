#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC EDGAR API data source for US stock fundamentals.

Provides:
- SEC ticker → CIK mapping (company_tickers_exchange.json)
- Company facts XBRL data (companyfacts/CIKXXXXXXXXXX.json)
- Annual (10-K) financial extraction with derived ratios
- Acceptance test against Apple Inc.

Rate limit: SEC allows 10 requests/second; a simple 0.1 s floor is enforced.
"""

import time
from .http import get_json, _http_get, CACHE_DIR

# ── SEC-specific constants ─────────────────────────────────
SEC_USER_AGENT = "InvestmentScreener/1.0 (investment.screener@example.com)"
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}
SEC_TTL_HOURS = 24  # cache SEC data for 24 hours
SEC_MIN_INTERVAL = 0.1  # 10 req/s max

_SEC_HELP_URLS = (
    "\n  SEC/Nasdaq API 不可达。可能原因：\n"
    "  1. 大陆网络需代理。若使用 ClashX/Clash Verge，在配置里加：\n"
    "       - DOMAIN-SUFFIX,sec.gov,DIRECT\n"
    "       - DOMAIN-SUFFIX,nasdaqtrader.com,DIRECT\n"
    "  2. 或设置 HTTPS_PROXY 环境变量指向你的代理。\n"
    "  3. 也可以跳过美股筛选，仅使用 A 股和港股。\n"
)

_last_sec_request = 0.0


def _sec_rate_limit():
    """Enforce SEC's 10 requests/second rate limit."""
    global _last_sec_request
    elapsed = time.time() - _last_sec_request
    if elapsed < SEC_MIN_INTERVAL:
        time.sleep(SEC_MIN_INTERVAL - elapsed)
    _last_sec_request = time.time()


def _sec_get_json(url, ttl_hours=None):
    """GET JSON from an SEC endpoint with rate limiting and cache.

    Args:
        url: Full SEC API URL.
        ttl_hours: Cache TTL in hours (default: SEC_TTL_HOURS).

    Returns:
        Parsed JSON (dict/list).
    Raises:
        RuntimeError with proxy/config help message on network failure.
    """
    _sec_rate_limit()
    ttl = SEC_TTL_HOURS if ttl_hours is None else ttl_hours
    try:
        return get_json(url, ttl_hours=ttl, headers=SEC_HEADERS)
    except OSError as e:
        raise RuntimeError(f"SEC API 请求失败: {e}{_SEC_HELP_URLS}") from e


def _sec_get_text(url, ttl_hours=None):
    """GET plain text from an SEC endpoint with rate limiting and cache."""
    from .http import get_text
    _sec_rate_limit()
    ttl = SEC_TTL_HOURS if ttl_hours is None else ttl_hours
    return get_text(url, ttl_hours=ttl, headers=SEC_HEADERS)


# ── XBRL tag mapping ───────────────────────────────────────
# Each key maps to a list of SEC us-gaap tag names tried in order
# (first match wins).  Arrows in the PRD mean "try A → fallback B".
TAG_MAP = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ],
    "gross_profit": ["GrossProfit"],
    "net_profit": ["NetIncomeLoss"],
    "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "eps": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ],
}

# Unit priority: try "USD" first (monetary), then "USD/shares" (EPS), then any.
_UNIT_ORDER = ["USD", "USD/shares"]


def _tag_annual_entries(facts, field_name):
    """Extract annual (10-K / FY) entries for *field_name* from company facts.

    Tries each XBRL tag listed in TAG_MAP[field_name] in order and returns
    the first set of annual entries found.  Returns an empty list if none of
    the tags exist in the facts.

    Args:
        facts: The ``facts`` dict from a companyfacts JSON response.
        field_name: One of the keys in TAG_MAP.

    Returns:
        list[dict]: Annual entries, each containing at least ``val``, ``fy``,
                    ``form``, ``filed``, ``end``.
    """
    tags = TAG_MAP.get(field_name, [])
    us_gaap = facts.get("us-gaap") or {}
    for tag in tags:
        tag_data = us_gaap.get(tag)
        if not tag_data:
            continue
        units = tag_data.get("units") or {}
        # Try preferred unit order, then fall back to any available unit
        for unit in _UNIT_ORDER:
            entries = units.get(unit)
            if entries:
                break
        else:
            # No preferred unit; pick the first available unit
            if units:
                entries = next(iter(units.values()), [])
            else:
                continue
        if not entries:
            continue
        # Filter to annual (10-K) entries only
        annual = [
            e for e in entries
            if (e.get("form") == "10-K" or e.get("fp") == "FY")
            and e.get("fy")
        ]
        if annual:
            return annual
    return []


def fetch_sec_ticker_master():
    """Fetch the full SEC ticker→CIK mapping and filter to major US exchanges.

    Returns:
        list[dict]: Each dict has keys ``cik`` (int), ``name`` (str),
                    ``ticker`` (str), ``exchange`` (str).
                    Only NASDAQ, NYSE, and NYSE ARCA entries are kept.

    Raises:
        AssertionError: If the result fails validation (too few entries or
                        missing key tickers).
    """
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    data = _sec_get_json(url)
    fields = data.get("fields", [])
    rows = data.get("data", [])

    # Build list of dicts
    cik_idx = fields.index("cik") if "cik" in fields else 0
    name_idx = fields.index("name") if "name" in fields else 1
    ticker_idx = fields.index("ticker") if "ticker" in fields else 2
    exchange_idx = fields.index("exchange") if "exchange" in fields else 3

    allowed = {"NASDAQ", "NYSE", "NYSE ARCA"}
    out = []
    for row in rows:
        exchange = str(row[exchange_idx]).strip().upper() if len(row) > exchange_idx else ""
        if exchange in allowed:
            out.append({
                "cik": int(row[cik_idx]),
                "name": str(row[name_idx]).strip(),
                "ticker": str(row[ticker_idx]).strip().upper(),
                "exchange": exchange,
            })

    # Validation
    assert len(out) >= 3000, f"SEC ticker master too small: {len(out)} entries"
    tickers = {r["ticker"] for r in out}
    for required in ("AAPL", "MSFT", "NVDA"):
        assert required in tickers, f"SEC ticker master missing {required}"

    return out


def fetch_sec_company_facts(cik):
    """Fetch the full XBRL companyfacts JSON for a given CIK.

    Args:
        cik: CIK as int or str (will be zero-padded to 10 digits).

    Returns:
        dict: The full companyfacts JSON response, or ``{}`` on error.
    """
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    try:
        return _sec_get_json(url)
    except Exception as e:
        print(f"  ⚠ SEC companyfacts CIK={cik_padded}: {e}")
        return {}


def extract_annual_financials(companyfacts):
    """Extract annual (10-K) financial data from a companyfacts JSON dict.

    Maps SEC XBRL tags to standard fields, groups by fiscal year, and
    picks the most recent filing per year.

    Args:
        companyfacts: The dict returned by ``fetch_sec_company_facts``.

    Returns:
        list[dict]: One dict per fiscal year (sorted ascending by fiscal_year),
                    with keys: ``fiscal_year``, ``report_date`` (end date),
                    ``filing_date``, ``revenue``, ``gross_profit``,
                    ``net_profit``, ``operating_cashflow``, ``assets``,
                    ``liabilities``, ``equity``, ``eps``.
    """
    facts = companyfacts.get("facts") or {}
    if not facts:
        return []

    # Collect entries for each field
    field_entries = {}
    for field_name in TAG_MAP:
        entries = _tag_annual_entries(facts, field_name)
        if entries:
            field_entries[field_name] = entries

    if not field_entries:
        return []

    # Group all entries by fiscal year: fy → {field_name: best_entry}
    def _entry_val(e):
        """Return numeric value (handle None / missing val)."""
        v = e.get("val")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Collect all fiscal years
    all_years = set()
    for entries in field_entries.values():
        for e in entries:
            all_years.add(e["fy"])

    records = []
    for fy in sorted(all_years):
        rec = {
            "fiscal_year": fy,
            "report_date": None,
            "filing_date": None,
            "revenue": None,
            "gross_profit": None,
            "net_profit": None,
            "operating_cashflow": None,
            "assets": None,
            "liabilities": None,
            "equity": None,
            "eps": None,
        }
        for field_name, entries in field_entries.items():
            # Take the most recent filing for this fiscal year
            fy_entries = [e for e in entries if e["fy"] == fy]
            if not fy_entries:
                continue
            # Sort by filed date descending, pick the latest
            fy_entries.sort(key=lambda e: e.get("filed", ""), reverse=True)
            best = fy_entries[0]
            rec[field_name] = _entry_val(best)
            # Use the latest filing dates across all fields
            if best.get("end") and (rec["report_date"] is None or best["end"] > rec["report_date"]):
                rec["report_date"] = best["end"]
            if best.get("filed") and (rec["filing_date"] is None or best["filed"] > rec["filing_date"]):
                rec["filing_date"] = best["filed"]

        records.append(rec)

    return records


def extract_roe(financials):
    """Compute ROE for each year using average equity of T and T-1.

    ROE = net_profit / ((equity_T + equity_T-1) / 2) × 100

    Years where either T or T-1 equity is missing are skipped (ROE stays None).

    Args:
        financials: List of dicts from ``extract_annual_financials``.

    Returns:
        list[dict]: The same list with an ``roe`` key added to each dict.
    """
    # Sort by fiscal year for T-1 lookup
    sorted_fin = sorted(financials, key=lambda r: r["fiscal_year"])
    by_year = {r["fiscal_year"]: r for r in sorted_fin}

    for r in sorted_fin:
        r["roe"] = None
        fy = r["fiscal_year"]
        prev = by_year.get(fy - 1)
        equity_t = r.get("equity")
        equity_t1 = prev.get("equity") if prev else None
        net = r.get("net_profit")
        if equity_t is not None and equity_t1 is not None and net is not None:
            avg_equity = (equity_t + equity_t1) / 2.0
            if avg_equity != 0:
                r["roe"] = (net / avg_equity) * 100.0

    return sorted_fin


def compute_derived_ratios(financials):
    """Add derived financial ratios to each year's record.

    Adds: gross_margin, net_margin, debt_ratio, ocf_to_profit.

    Args:
        financials: List of dicts (from ``extract_annual_financials`` or
                    ``extract_roe``).

    Returns:
        list[dict]: The same list with derived ratio keys added.
    """
    for r in financials:
        rev = r.get("revenue")
        gp = r.get("gross_profit")
        np_ = r.get("net_profit")
        lia = r.get("liabilities")
        ast = r.get("assets")
        ocf = r.get("operating_cashflow")

        r["gross_margin"] = (gp / rev * 100.0) if (gp is not None and rev) else None
        r["net_margin"] = (np_ / rev * 100.0) if (np_ is not None and rev) else None
        r["debt_ratio"] = (lia / ast * 100.0) if (lia is not None and ast) else None
        r["ocf_to_profit"] = (ocf / np_) if (ocf is not None and np_ and np_ != 0) else None

    return financials


def fetch_apple_test():
    """Acceptance test: fetch Apple Inc. (CIK 0000320193) fundamentals.

    Fetches company facts, extracts annual financials, computes ROE and
    derived ratios, and returns the latest 3 fiscal years.

    Returns:
        dict: Keys ``ticker``, ``company_name``, ``cik``, ``latest_years``
              (list of the 3 most recent annual records with all derived
              fields), ``all_years`` (full list).
    """
    AAPL_CIK = "0000320193"
    facts = fetch_sec_company_facts(AAPL_CIK)
    company_name = facts.get("entityName", "Apple Inc.")

    financials = extract_annual_financials(facts)
    financials = extract_roe(financials)
    financials = compute_derived_ratios(financials)

    # Latest 3 years
    sorted_fin = sorted(financials, key=lambda r: r["fiscal_year"], reverse=True)
    latest_years = sorted_fin[:3]

    print(f"\n  === Apple Inc. (CIK {AAPL_CIK}) ===")
    print(f"  Company: {company_name}")
    print(f"  Total annual records: {len(financials)}")
    for r in latest_years:
        print(f"  FY{r['fiscal_year']}: rev={_fmtb(r['revenue'])} "
              f"net={_fmtb(r['net_profit'])} "
              f"roe={_fmtpct(r['roe'])} "
              f"gm={_fmtpct(r['gross_margin'])} "
              f"nm={_fmtpct(r['net_margin'])} "
              f"eps={_fmtn(r['eps'], 2)}")

    return {
        "ticker": "AAPL",
        "company_name": company_name,
        "cik": AAPL_CIK,
        "latest_years": latest_years,
        "all_years": financials,
    }


# ── Formatting helpers ─────────────────────────────────────
def _fmtb(v):
    """Format a large number in billions (one decimal)."""
    if v is None:
        return "N/A"
    return f"${v / 1e9:.1f}B"


def _fmtpct(v):
    """Format a percentage value."""
    if v is None:
        return "N/A"
    return f"{v:.1f}%"


def _fmtn(v, d=1):
    """Format a number with *d* decimal places."""
    if v is None:
        return "N/A"
    return f"{v:.{d}f}"


# ── Quick self-test ────────────────────────────────────────
if __name__ == "__main__":
    print("=== SEC EDGAR self-test ===")

    print("\n[1] Fetching SEC ticker master...")
    master = fetch_sec_ticker_master()
    print(f"  Tickers on major exchanges: {len(master)}")
    for t in ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"):
        found = next((r for r in master if r["ticker"] == t), None)
        if found:
            print(f"    {t}: CIK={found['cik']}, exchange={found['exchange']}")

    print("\n[2] Apple acceptance test...")
    result = fetch_apple_test()
    assert result["latest_years"], "Apple acceptance test failed: no annual data"
    print("  ✅ Apple acceptance test passed")
