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
from .http import get_json

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
    # 用于毛利回退计算：营业成本
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    # 流通股本口径（dei，非 us-gaap），供上层使用
    "shares_outstanding": ["EntityCommonStockSharesOutstanding"],
}

# 流量概念（利润表/现金流量表）：值对应一段期间(duration)，需整年
_FLOW_FIELDS = {"revenue", "gross_profit", "net_profit", "operating_cashflow",
                "cost_of_revenue"}
# 时点概念（资产负债表）：值对应某一时点(instant)的余额
_INSTANT_FIELDS = {"assets", "liabilities", "equity"}
# 每股/股本类：eps 为流量(整年)，shares_outstanding 为时点(dei)
# eps 归入流量校验，shares_outstanding 按时点处理
_FLOW_FIELDS_EPS = {"eps"}
_DEI_INSTANT_FIELDS = {"shares_outstanding"}

# Unit priority: try "USD" first (monetary), then "USD/shares" (EPS), then any.
_UNIT_ORDER = ["USD", "USD/shares", "shares"]

# 整年 duration 天数范围：财年可能 52/53 周，允许 340~380 天
_ANNUAL_MIN_DAYS = 340
_ANNUAL_MAX_DAYS = 380


def _parse_date(s):
    """Parse an ISO date string 'YYYY-MM-DD' into a date, or None."""
    if not s:
        return None
    try:
        from datetime import date as _date
        parts = str(s).split("-")
        return _date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError, TypeError):
        return None


def _duration_days(entry):
    """Return the duration in days between start/end, or None if missing."""
    start = _parse_date(entry.get("start"))
    end = _parse_date(entry.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _is_annual_form(entry):
    """True if the entry originates from an annual report (10-K/10-K/A)."""
    form = str(entry.get("form") or "")
    return form == "10-K" or form == "10-K/A"


def _raw_tag_entries(facts, field_name):
    """Return raw XBRL entries (first matching tag) for *field_name*.

    us-gaap 概念在 facts["us-gaap"]，dei 概念（如 shares_outstanding）在
    facts["dei"]。按 TAG_MAP 顺序取第一个存在的 tag，选优先单位。
    每条追加 ``_tag`` 便于调试；不做期间过滤，交由上层按流量/时点处理。
    """
    tags = TAG_MAP.get(field_name, [])
    ns = facts.get("dei") if field_name in _DEI_INSTANT_FIELDS else facts.get("us-gaap")
    ns = ns or {}
    for tag in tags:
        tag_data = ns.get(tag)
        if not tag_data:
            continue
        units = tag_data.get("units") or {}
        entries = None
        for unit in _UNIT_ORDER:
            if units.get(unit):
                entries = units[unit]
                break
        if entries is None and units:
            entries = next(iter(units.values()), None)
        if entries:
            return list(entries)
    return []


def _tag_annual_entries(facts, field_name):
    """Extract annual (整年/年末) entries for *field_name* from company facts.

    流量概念(_FLOW_FIELDS/eps)：只保留 duration≈整年(340~380 天)的条目，
      拒绝季度/半年 stub；来源需为 10-K/10-K/A 年报。
    时点概念(_INSTANT_FIELDS/dei)：instant 条目(无 start)，取年报口径。

    返回按 end 归组前的候选年度条目列表，每条至少含 ``val``、``end``、
    ``filed``、``form``。保留此函数名与签名以兼容既有测试。
    """
    entries = _raw_tag_entries(facts, field_name)
    out = []
    is_flow = field_name in _FLOW_FIELDS or field_name in _FLOW_FIELDS_EPS
    for e in entries:
        if not e.get("end"):
            continue
        if is_flow:
            # 流量：必须是整年期间，且来自年报
            dur = _duration_days(e)
            if dur is None or dur < _ANNUAL_MIN_DAYS or dur > _ANNUAL_MAX_DAYS:
                continue
            if not _is_annual_form(e):
                # 允许 fp==FY 的整年条目作为兜底（部分公司 form 标注不规范）
                if e.get("fp") != "FY":
                    continue
        else:
            # 时点：应为 instant（无 start）；优先年报口径
            if e.get("start"):
                continue
        out.append(e)
    return out


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


def _entry_val(e):
    """Return numeric value of an entry (handle None / missing val)."""
    v = e.get("val")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pick_by_period(entries):
    """按 end 期间归组，同期间取 filed 最新(拿重述后最新值)。

    返回 dict: end(str) → 该期间选定的条目。
    去重规则：同一 end 优先 10-K/10-K/A，再按 filed 降序取首条。
    """
    by_end = {}
    for e in entries:
        end = e.get("end")
        if not end:
            continue
        cur = by_end.get(end)
        if cur is None:
            by_end[end] = e
            continue
        # 优先年报表单，其次 filed 更晚者
        cur_annual = _is_annual_form(cur)
        e_annual = _is_annual_form(e)
        if e_annual and not cur_annual:
            by_end[end] = e
        elif e_annual == cur_annual and str(e.get("filed") or "") > str(cur.get("filed") or ""):
            by_end[end] = e
    return by_end


def extract_annual_financials(companyfacts):
    """Extract annual financial data from a companyfacts JSON dict.

    以**流量概念的整年 end 期间**为主键组装记录，保证同一条记录里
    revenue/net_profit/gross_profit/operating_cashflow 都来自同一年度期间；
    资产负债表时点概念(assets/liabilities/equity)按财年末 instant 对齐到
    最接近该 end 的时点值；report_date 即该条自身的 end，绝不跨字段取 max。

    Args:
        companyfacts: The dict returned by ``fetch_sec_company_facts``.

    Returns:
        list[dict]: One dict per fiscal year-end (sorted ascending by
                    fiscal_year), keys: ``fiscal_year``, ``report_date``
                    (period end), ``filing_date``, ``revenue``,
                    ``gross_profit``, ``net_profit``, ``operating_cashflow``,
                    ``assets``, ``liabilities``, ``equity``, ``eps``,
                    ``shares_outstanding``.
    """
    facts = companyfacts.get("facts") or {}
    if not facts:
        return []

    # 各字段按 end 归组后的选定条目
    picked = {}
    for field_name in TAG_MAP:
        entries = _tag_annual_entries(facts, field_name)
        if entries:
            picked[field_name] = _pick_by_period(entries)

    # 流量主键：以 net_profit 优先，其次 revenue 的整年 end 期间作为年度锚点
    anchor_fields = ["net_profit", "revenue", "operating_cashflow"]
    period_ends = set()
    for f in anchor_fields:
        period_ends.update((picked.get(f) or {}).keys())
    if not period_ends:
        return []

    # 时点概念：所有可用 instant end，供最近日期匹配
    instant_ends = {}
    for f in _INSTANT_FIELDS | _DEI_INSTANT_FIELDS:
        instant_ends[f] = picked.get(f) or {}

    def _nearest_instant(field_name, target_end):
        """取字段中 end 最接近 target_end(且不晚于其后 5 天)的时点值。"""
        candidates = instant_ends.get(field_name) or {}
        te = _parse_date(target_end)
        if te is None:
            return candidates.get(target_end)
        best_e, best_gap = None, None
        for end, e in candidates.items():
            d = _parse_date(end)
            if d is None:
                continue
            gap = abs((d - te).days)
            # 财年末时点应与流量 end 基本重合，允许 ±5 天差异
            if gap > 5:
                continue
            if best_gap is None or gap < best_gap:
                best_e, best_gap = e, gap
        return best_e

    def _fy_label(end, end_date):
        """按锚点流量条目的期间中点年份定 fiscal_year。

        避免财年切换公司(如财年末从 1 月初迁到 12 月底)出现两条不同年度记录
        共享同一 end.year、被下游 extract_roe 的 by_year 去重静默丢弃。
        中点年份对正常公司与 end.year 一致，对切换公司则天然分开。
        """
        if end_date is None:
            return None
        for f in anchor_fields:
            e = (picked.get(f) or {}).get(end)
            if e and e.get("start"):
                sd = _parse_date(e.get("start"))
                if sd:
                    return (sd + (end_date - sd) / 2).year
        return end_date.year

    records = []
    for end in sorted(period_ends):
        end_date = _parse_date(end)
        fiscal_year = _fy_label(end, end_date)
        rec = {
            "fiscal_year": fiscal_year,
            "report_date": end,
            "filing_date": None,
            "revenue": None,
            "gross_profit": None,
            "net_profit": None,
            "operating_cashflow": None,
            "assets": None,
            "liabilities": None,
            "equity": None,
            "eps": None,
            "shares_outstanding": None,
        }
        filed_dates = []
        # 流量字段：严格按同一 end 取值
        for field_name in (_FLOW_FIELDS | _FLOW_FIELDS_EPS):
            e = (picked.get(field_name) or {}).get(end)
            if e is not None and field_name in rec:
                rec[field_name] = _entry_val(e)
                if e.get("filed"):
                    filed_dates.append(str(e["filed"]))
        # 时点字段：匹配同财年末
        for field_name in (_INSTANT_FIELDS | _DEI_INSTANT_FIELDS):
            e = _nearest_instant(field_name, end)
            if e is not None:
                rec[field_name] = _entry_val(e)
                if e.get("filed"):
                    filed_dates.append(str(e["filed"]))
        # 毛利回退：GrossProfit 缺失时用 Revenues - CostOfRevenue
        if rec.get("gross_profit") is None:
            cor_e = (picked.get("cost_of_revenue") or {}).get(end)
            cor = _entry_val(cor_e) if cor_e is not None else None
            if rec.get("revenue") is not None and cor is not None:
                rec["gross_profit"] = rec["revenue"] - cor
        # 该条自身的申报日：取参与本记录各字段 filed 的最大值
        if filed_dates:
            rec["filing_date"] = max(filed_dates)
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
