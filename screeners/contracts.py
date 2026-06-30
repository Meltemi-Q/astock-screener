"""
Data contracts for normalized cross-market stock screening.

All market-specific data sources must produce records conforming to
these contracts before entering the five-layer scoring pipeline.
"""

from __future__ import annotations
# ── Market identifiers ──
MARKET_CN = "cn"
MARKET_HK = "hk"
MARKET_US = "us"
VALID_MARKETS = frozenset({MARKET_CN, MARKET_HK, MARKET_US})

MARKET_LABELS = {
    MARKET_CN: "A股",
    MARKET_HK: "港股",
    MARKET_US: "美股",
}

# ── Security Master ──
SecurityMaster = dict  # keys: market, code, display_code, name, exchange, currency, lot_size, security_type, is_tradable

def make_security_master(
    market: str,
    code: str,
    name: str,
    exchange: str = "",
    currency: str = "",
    lot_size: int = 100,
    security_type: str = "common_stock",
    is_tradable: bool = True,
    display_code: str = "",
) -> SecurityMaster:
    return {
        "market": market,
        "code": code,
        "display_code": display_code or code,
        "name": name,
        "exchange": exchange,
        "currency": currency,
        "lot_size": lot_size,
        "security_type": security_type,
        "is_tradable": is_tradable,
    }

# ── Quote Snapshot ──
QuoteSnapshot = dict  # keys: market, code, price, pe_ttm, pb, market_cap, currency, quote_time, source

def make_quote_snapshot(
    market: str,
    code: str,
    price: float | None = None,
    pe_ttm: float | None = None,
    pb: float | None = None,
    market_cap: float | None = None,
    currency: str = "",
    quote_time: str | None = None,
    source: str = "",
) -> QuoteSnapshot:
    return {
        "market": market,
        "code": code,
        "price": price,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "market_cap": market_cap,
        "currency": currency,
        "quote_time": quote_time,
        "source": source,
    }

# ── Annual Financial ──
AnnualFinancial = dict  # keys per PRD section 4.2

def make_annual_financial(
    market: str,
    code: str,
    fiscal_year: int,
    report_date: str = "",
    filing_date: str | None = None,
    currency: str = "",
    revenue: float | None = None,
    gross_profit: float | None = None,
    net_profit: float | None = None,
    operating_cashflow: float | None = None,
    assets: float | None = None,
    liabilities: float | None = None,
    equity: float | None = None,
    eps: float | None = None,
    roe: float | None = None,
    gross_margin: float | None = None,
    net_margin: float | None = None,
    debt_ratio: float | None = None,
) -> AnnualFinancial:
    return {
        "market": market,
        "code": code,
        "fiscal_year": fiscal_year,
        "report_date": report_date,
        "filing_date": filing_date,
        "currency": currency,
        "revenue": revenue,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "operating_cashflow": operating_cashflow,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "eps": eps,
        "roe": roe,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "debt_ratio": debt_ratio,
    }

# ── Normalized Screening Record ──
# This is the canonical record that enters the five-layer pipeline.
# All market screeners must produce records with these keys.
NORMALIZED_FIELDS = [
    # identity
    "market", "code", "display_code", "name",
    # classification
    "industry", "exchange", "currency",
    # quote
    "price", "min_buy", "pe_ttm", "pe_dyn", "pb",
    "market_cap", "market_cap_cny",  # market_cap_cny for cross-market comparison
    # quality
    "roe", "gross_margin", "net_margin",
    # growth
    "yoy", "cagr",
    # financial health
    "ocf_to_profit", "debt_ratio", "goodwill_ratio",
    "deduct_ratio",
    # valuation derived
    "g",            # growth rate = min(yoy, cagr)
    "peg",          # pe_ttm / g
    "eyield",       # earnings yield = 100 / pe_ttm
    "fair_pe",      # = g capped to [12, 30]
    "fair_mktcap",  # = net_profit * fair_pe
    "discount",     # = market_cap / fair_mktcap
    "exp_ret",      # = fair_pe / pe_ttm * g (simplified)
    "ttm_netp",     # trailing net profit estimate
    # scoring
    "deepest", "tier", "score", "fails",
    # metadata
    "source_urls", "data_quality_flag",
]

def make_screening_record(
    market: str, code: str, display_code: str, name: str,
    industry: str = "", exchange: str = "", currency: str = "",
    price: float | None = None,
    min_buy: float | None = None,
    pe_ttm: float | None = None,
    pe_dyn: float | None = None,
    pb: float | None = None,
    market_cap: float | None = None,
    market_cap_cny: float | None = None,
    roe: float | None = None,
    gross_margin: float | None = None,
    net_margin: float | None = None,
    yoy: float | None = None,
    cagr: float | None = None,
    ocf_to_profit: float | None = None,
    debt_ratio: float | None = None,
    goodwill_ratio: float | None = None,
    deduct_ratio: float | None = None,
    ttm_netp: float | None = None,
    data_quality_flag: str = "",
) -> dict:
    """Create a normalized screening record with derived fields computed."""
    # derived fields
    g = None
    if yoy is not None and cagr is not None:
        g = min(yoy, cagr)
    elif yoy is not None:
        g = yoy
    elif cagr is not None:
        g = cagr

    peg = None
    if pe_ttm is not None and pe_ttm > 0 and g is not None and g > 0:
        peg = pe_ttm / g

    eyield = None
    if pe_ttm is not None and pe_ttm > 0:
        eyield = 100.0 / pe_ttm

    fair_pe = None
    if g is not None and g > 0:
        fair_pe = max(12.0, min(g, 30.0))

    fair_mktcap = None
    if ttm_netp is not None and fair_pe is not None:
        fair_mktcap = ttm_netp * fair_pe

    discount = None
    if market_cap is not None and fair_mktcap is not None and fair_mktcap > 0:
        discount = market_cap / fair_mktcap

    exp_ret = None
    if pe_ttm is not None and pe_ttm > 0 and fair_pe is not None and g is not None and g > 0:
        exp_ret = (fair_pe / pe_ttm) * g

    return {
        "market": market, "code": code, "display_code": display_code, "name": name,
        "industry": industry, "exchange": exchange, "currency": currency,
        "price": price, "min_buy": min_buy,
        "pe_ttm": pe_ttm, "pe_dyn": pe_dyn, "pb": pb,
        "market_cap": market_cap, "market_cap_cny": market_cap_cny,
        "roe": roe, "gross_margin": gross_margin, "net_margin": net_margin,
        "yoy": yoy, "cagr": cagr,
        "ocf_to_profit": ocf_to_profit, "debt_ratio": debt_ratio,
        "goodwill_ratio": goodwill_ratio, "deduct_ratio": deduct_ratio,
        "g": g, "peg": peg, "eyield": eyield,
        "fair_pe": fair_pe, "fair_mktcap": fair_mktcap,
        "discount": discount, "exp_ret": exp_ret,
        "ttm_netp": ttm_netp,
        "deepest": 0, "tier": "", "score": 0.0, "fails": [],
        "source_urls": "", "data_quality_flag": data_quality_flag,
    }

# ── Validation ──
REQUIRED_FOR_TIER_AB = frozenset({
    "price", "market_cap",  # quote fields
    "revenue", "net_profit", "equity", "operating_cashflow",  # from raw financials
})

def check_tier_ab_eligibility(record: dict, raw_fin: AnnualFinancial | None = None) -> tuple[bool, list[str]]:
    """Check if a record has the minimum data to be eligible for Tier A/B.
    
    Returns (is_eligible, missing_fields).
    """
    missing = []
    # Check direct record fields
    for f in ("price", "market_cap"):
        if record.get(f) is None:
            missing.append(f)

    # Check raw financial fields if provided
    if raw_fin:
        for f in ("revenue", "net_profit", "equity", "operating_cashflow"):
            if raw_fin.get(f) is None:
                missing.append(f"fin.{f}")

    # Without raw financials, check computed fields
    if not raw_fin:
        if record.get("roe") is None and record.get("gross_margin") is None:
            missing.append("financial_data")

    return len(missing) == 0, missing


def check_currency_match(quote_currency: str, report_currency: str) -> bool:
    """Check if quote and report currencies match (case-insensitive, CNY/RMB equivalence)."""
    q = quote_currency.upper().strip()
    r = report_currency.upper().strip()
    if q == r:
        return True
    # CNY/RMB equivalence
    cny_set = {"CNY", "RMB", "¥"}
    if q in cny_set and r in cny_set:
        return True
    return False
