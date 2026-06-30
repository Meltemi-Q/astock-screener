#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股「五层选股流水线」自动化筛选器
================================================
对应 A 股 astock_screener.py / 港股 screeners/hk.py 的美股版本。

数据源:
  - 证券主表:    data_sources/nasdaq_trader.py → Nasdaq Symbol Directory + SEC ticker master 合并
  - 实时行情:    data_sources/eastmoney.py       → 批量美股快照 (push2delay clist)
  - 财务数据:    data_sources/sec_edgar.py       → SEC EDGAR XBRL companyfacts (10-K annual)

归一化 + 打分:
  - screeners/contracts.py → 标准化记录 (make_screening_record)
  - screeners/scoring.py  → 五层流水线 evaluate + score (run_full_pipeline)

SEC API 的严格速率限制 (10 req/s)，内部使用 RateLimiter 类强制 0.15 s 最小间隔，
并以 ThreadPoolExecutor(max_workers=3) 温和并发抓取。

用法:
  from screeners.us import build_us_records, write_us_results, test_us_screener
  records = build_us_records(2025)
  write_us_results(records, year=2025)
  # 或直接运行: python3 screeners/us.py
"""

from __future__ import annotations

import os
import sys
import json
import time
import threading
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# ── 路径：确保项目根在 sys.path ──
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data_sources.eastmoney import (
    fetch_us_spot_snapshot,
)
from data_sources.sec_edgar import (
    fetch_sec_ticker_master,
    fetch_sec_company_facts,
    extract_annual_financials,
    extract_roe,
    compute_derived_ratios,
)
from data_sources.nasdaq_trader import (
    build_us_stock_universe,
    merge_universe_with_sec,
)
from screeners.contracts import (
    MARKET_US,
    make_screening_record,
)
from screeners.scoring import run_full_pipeline, DEFAULT_CONFIG

# ── 常量 ───────────────────────────────────────────────────
# SEC 速率限制
SEC_MIN_INTERVAL = 0.15      # 最小请求间隔 (秒)，对应 ~6.7 req/s，低于 SEC 的 10 req/s 上限
SEC_FETCH_WORKERS = 3        # 线程池并发数 (保守，避免触发限流)

# 缓存目录：SEC companyfacts JSON 文件
US_FACTS_CACHE_DIR = os.path.join(WORKDIR, "cache", "us_facts")
US_FACTS_CACHE_TTL = 24 * 3600
DEFAULT_US_SEC_MAX_FRESH_FETCHES = 250

# 输出目录
RESULTS_DIR = os.path.join(WORKDIR, "results")

# 稳定入口文件名
US_STABLE_SCREEN_NAME = "usstock_screen.html"


# ── SEC 速率限制器 ────────────────────────────────────────

class RateLimiter:
    """简单的令牌桶式速率限制器，线程安全。

    强制两次 acquire() 调用之间至少间隔 min_interval 秒。
    适用于 SEC EDGAR API (10 req/s)。
    """

    def __init__(self, min_interval: float = SEC_MIN_INTERVAL):
        self._min_interval = min_interval
        self._last_time = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """阻塞直至可以发出下一个请求。"""
        with self._lock:
            now = time.time()
            wait = self._last_time + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_time = time.time()


# 全局速率限制器实例
_rate_limiter = RateLimiter(SEC_MIN_INTERVAL)


# ── 辅助函数 ───────────────────────────────────────────────

def _fnum(x):
    """安全转 float；None / '-' / '' → None。"""
    if x is None or x == "-" or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_financial_stock(industry_name: str = "", sic_code: str | int | None = None) -> bool:
    """判断是否为金融股（银行/保险/券商/投行等）。

    规则：
      - SIC 代码以 "6" 开头 (Finance, Insurance, Real Estate 大类)
      - 行业名称含金融关键词

    Args:
        industry_name: 行业名称（如 SEC SIC 描述）。
        sic_code: SIC 标准行业分类代码。

    Returns:
        bool: 是金融股返回 True。
    """
    # SIC 代码检查
    if sic_code is not None:
        sic_str = str(sic_code).strip()
        if sic_str.startswith("6"):
            return True

    # 行业名称关键词匹配
    if industry_name:
        upper = industry_name.upper()
        financial_kw = (
            "BANK", "INSURANCE", "SECURITIES", "BROKER", "INVESTMENT",
            "FINANCIAL", "FINANCE", "REAL ESTATE", "REIT", "TRUST",
            "MORTGAGE", "LENDER", "BANKING",
        )
        for kw in financial_kw:
            if kw in upper:
                return True

    return False


def compute_growth(financials: list[dict], year: int) -> tuple[float | None, float | None]:
    """从年度财务数据计算净利润同比增长率 (yoy) 和 3 年 CAGR。

    取 fiscal_year 匹配 target year 的记录，计算：
      - yoy = (net_profit_T - net_profit_T-1) / |net_profit_T-1| × 100
      - cagr = ((net_profit_T / net_profit_T-3) ^ (1/3) - 1) × 100

    Args:
        financials: 排序后的年度财务记录列表 (extract_annual_financials 输出)。
        year: 目标年报年份。

    Returns:
        (yoy, cagr): 百分比值，任一不可计算时为 None。
    """
    # 按 fiscal_year 建立索引
    by_year = {r["fiscal_year"]: r for r in financials}

    current = by_year.get(year)
    if not current:
        return None, None

    net_t = current.get("net_profit")
    if net_t is None or net_t == 0:
        return None, None

    # 同比 (year-over-year)
    prev = by_year.get(year - 1)
    yoy = None
    if prev:
        net_t1 = prev.get("net_profit")
        if net_t1 is not None and net_t1 != 0:
            yoy = ((net_t - net_t1) / abs(net_t1)) * 100.0

    # 3 年 CAGR
    base = by_year.get(year - 3)
    cagr = None
    if base:
        net_base = base.get("net_profit")
        if net_base is not None and net_base > 0 and net_t > 0:
            cagr = ((net_t / net_base) ** (1.0 / 3) - 1.0) * 100.0

    return yoy, cagr


def _fetch_cached_company_facts(cik: int | str) -> dict:
    """带本地缓存的 SEC companyfacts 抓取。

    缓存位于 cache/us_facts/CIK{zero_padded}.json，TTL 24 小时。
    抓取前先通过全局速率限制器排队。

    Args:
        cik: SEC CIK 编号。

    Returns:
        dict: companyfacts JSON 对象；失败时返回 {}。
    """
    os.makedirs(US_FACTS_CACHE_DIR, exist_ok=True)

    cache_path = _company_facts_cache_path(cik)

    if _has_fresh_company_facts_cache(cik):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # 速率限制 → 实际请求
    _rate_limiter.acquire()
    facts = fetch_sec_company_facts(cik)

    # 写入缓存
    if facts:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(facts, f, ensure_ascii=False)
        except Exception:
            pass

    return facts


def _company_facts_cache_path(cik: int | str) -> str:
    cik_padded = str(int(cik)).zfill(10)
    return os.path.join(US_FACTS_CACHE_DIR, f"CIK{cik_padded}.json")


def _has_fresh_company_facts_cache(cik: int | str) -> bool:
    cache_path = _company_facts_cache_path(cik)
    return (
        os.path.exists(cache_path)
        and (time.time() - os.path.getmtime(cache_path)) < US_FACTS_CACHE_TTL
    )


def _sec_max_fresh_fetches() -> int:
    """Return max uncached SEC companyfacts fetches for one full run.

    ``US_SEC_MAX_FRESH_FETCHES=0`` means cache-only. ``-1`` means unlimited.
    """
    raw = os.environ.get("US_SEC_MAX_FRESH_FETCHES", str(DEFAULT_US_SEC_MAX_FRESH_FETCHES))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_US_SEC_MAX_FRESH_FETCHES


def _safe_api_call(cik: int | str, ticker: str, year: int) -> dict | None:
    """对单个美股执行一次完整的 SEC API 调用链路，带缓存与重试。

    调用链:
      fetch_sec_company_facts → extract_annual_financials → extract_roe → compute_derived_ratios

    Args:
        cik: SEC CIK。
        ticker: 股票代码 (仅用于日志)。
        year: 目标年报年份。

    Returns:
        dict 包含 keys: financials (list), latest (最近一年 dict), growth (yoy, cagr), sic。
        失败返回 None。
    """
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            facts = _fetch_cached_company_facts(cik)
            if not facts or not facts.get("facts"):
                if attempt < max_attempts:
                    time.sleep(0.3 * attempt)
                    continue
                return None

            financials = extract_annual_financials(facts)
            if not financials:
                if attempt < max_attempts:
                    time.sleep(0.3 * attempt)
                    continue
                return None

            financials = extract_roe(financials)
            financials = compute_derived_ratios(financials)

            # 按 fiscal_year 排序
            financials.sort(key=lambda r: r["fiscal_year"])

            # 提取 SIC 信息
            sic = facts.get("sic") or facts.get("sicDescription") or ""

            # 最近一期年报
            by_year = {r["fiscal_year"]: r for r in financials}
            latest = by_year.get(year)

            yoy, cagr = compute_growth(financials, year)

            return {
                "financials": financials,
                "latest": latest,
                "yoy": yoy,
                "cagr": cagr,
                "sic": str(sic).strip(),
            }

        except Exception as e:
            if attempt < max_attempts:
                time.sleep(0.6 * attempt)
            else:
                print(f"  ⚠ SEC API 3 次尝试均失败 CIK={cik} ({ticker}): {e}")
                return None

    return None


# ── 主函数：build_us_records ───────────────────────────────

def build_us_records(year: int = 2025) -> list[dict]:
    """构建美股五层选股归一化记录。

    步骤:
      1. 构建美股证券主表 (Nasdaq Trader)
      2. 合并 SEC CIK 映射
      3. 抓取全市场实时行情 (Eastmoney)
      4. 并发抓取 SEC 财务数据
      5. 组装归一化筛选记录

    Args:
        year: 年报口径年份 (默认 2025)。

    Returns:
        list[dict]: 标准化筛选记录列表，每条含所有归一化字段。
    """
    print("=" * 60)
    print(f"  美股五层选股流水线 — {year} 年报")
    print("=" * 60)

    # ── Step 1: 构建美股证券主表 ──
    print("\n[1/5] 构建美股证券主表...")
    t0 = time.time()
    try:
        universe = build_us_stock_universe()
    except Exception as e:
        print(f"  ⚠ Nasdaq Trader 主表获取失败: {e}")
        return []
    print(f"  主表: {len(universe)} 只美股 ({time.time() - t0:.1f}s)")

    # ── Step 2: 合并 SEC CIK 映射 ──
    print("\n[2/5] 合并 SEC ticker→CIK 映射...")
    t0 = time.time()
    try:
        sec_master = fetch_sec_ticker_master()
    except Exception as e:
        print(f"  ⚠ SEC ticker master 获取失败: {e}")
        return []
    merged = merge_universe_with_sec(universe, sec_master)
    with_cik = sum(1 for r in merged if r["has_cik"])
    print(f"  合并后: {len(merged)} 只, 含 CIK: {with_cik} ({time.time() - t0:.1f}s)")

    # ── Step 3: 抓取全市场实时行情 ──
    print("\n[3/5] 抓取美股全市场实时行情...")
    t0 = time.time()
    try:
        spot_data = fetch_us_spot_snapshot()
    except Exception as e:
        print(f"  ⚠ 美股行情获取失败: {e}")
        return []
    print(f"  行情: {len(spot_data)} 只 ({time.time() - t0:.1f}s)")

    # ── Step 4: 并发抓取 SEC 财务数据 ──
    print(f"\n[4/5] 抓取 SEC 财务数据 (并发={SEC_FETCH_WORKERS})...")
    t0 = time.time()

    # 仅对有 CIK 的股票抓取财务数据；默认只补一批 uncached SEC facts，
    # 其余股票保留行情行并标记 missing_financials，避免全量运行卡数小时。
    stocks_with_cik = [r for r in merged if r["has_cik"]]
    fetch_limit = _sec_max_fresh_fetches()
    cached_fetches = []
    fresh_candidates = []
    for r in stocks_with_cik:
        if _has_fresh_company_facts_cache(r["cik"]):
            cached_fetches.append(r)
        else:
            fresh_candidates.append(r)

    def _cap_key(row):
        quote = spot_data.get(row["ticker"]) or {}
        return _fnum(quote.get("market_cap")) or 0.0

    fresh_candidates.sort(key=_cap_key, reverse=True)
    if fetch_limit < 0:
        fresh_fetches = fresh_candidates
    else:
        fresh_fetches = fresh_candidates[:fetch_limit]
    skipped_by_budget = max(0, len(fresh_candidates) - len(fresh_fetches))
    stocks_to_fetch = cached_fetches + fresh_fetches
    print(
        f"  待处理 SEC 财务: {len(stocks_to_fetch)} 只 "
        f"(缓存 {len(cached_fetches)}, 新抓 {len(fresh_fetches)}, "
        f"预算跳过 {skipped_by_budget})"
    )

    fin_results: dict[str, dict | None] = {}  # ticker → financial data
    completed = 0
    skipped_no_cik = len(merged) - len(stocks_with_cik)
    skipped_no_fin = 0

    with ThreadPoolExecutor(max_workers=SEC_FETCH_WORKERS) as ex:
        futures = {}
        for r in stocks_to_fetch:
            ticker = r["ticker"]
            cik = r["cik"]
            futures[ex.submit(_safe_api_call, cik, ticker, year)] = ticker

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
                fin_results[ticker] = result
            except Exception as e:
                fin_results[ticker] = None
                print(f"  ⚠ {ticker}: 财务数据抓取异常: {e}")

            completed += 1
            if completed % 200 == 0 or completed == len(stocks_to_fetch):
                elapsed = time.time() - t0
                rate = completed / max(1, elapsed)
                eta = (len(stocks_to_fetch) - completed) / max(0.01, rate)
                print(f"    进度: {completed}/{len(stocks_to_fetch)} "
                      f"({rate:.1f} req/s, 预计剩余 {eta:.0f}s)")

    valid_fin = sum(1 for v in fin_results.values() if v is not None)
    print(f"  财务数据: {valid_fin}/{len(stocks_to_fetch)} 成功 "
          f"({time.time() - t0:.1f}s)")

    # ── Step 5: 组装归一化记录 ──
    print("\n[5/5] 组装归一化筛选记录...")
    t0 = time.time()

    records = []
    skipped_no_spot = 0

    for r in merged:
        ticker = r["ticker"]

        # 检查行情
        spot = spot_data.get(ticker)
        if not spot:
            skipped_no_spot += 1
            continue

        price = _fnum(spot.get("price"))
        mktcap = _fnum(spot.get("market_cap"))
        if price is None or mktcap is None:
            skipped_no_spot += 1
            continue

        # 财务数据缺失时仍保留行情行，避免正式总表退化成少量样本。
        fin = fin_results.get(ticker)
        if fin is None:
            skipped_no_fin += 1

        # 提取行业与 SIC
        sic = fin.get("sic", "") if fin else ""
        industry = str(sic).strip() if sic else ""
        # 尝试从 Nasdaq 名称或 SIC 描述提取更好的行业名
        if not industry:
            industry = r.get("name", "美股")

        # 过滤金融股
        if _is_financial_stock(industry_name=industry, sic_code=sic):
            print(f"  ⚠ 过滤金融股: {ticker} (SIC={sic})")
            continue

        latest = fin.get("latest") if fin else None
        yoy = fin.get("yoy") if fin else None
        cagr = fin.get("cagr") if fin else None

        # 从行情提取估值
        pe_ttm = _fnum(spot.get("pe_ttm"))
        pe_dyn = _fnum(spot.get("pe_dyn"))
        pb = _fnum(spot.get("pb"))

        # 构建归一化记录
        record = make_screening_record(
            market=MARKET_US,
            code=ticker,
            display_code=ticker,
            name=r.get("name", ticker),
            industry=industry,
            exchange=r.get("exchange", ""),
            currency="USD",
            price=price,
            min_buy=price,  # 美股 1 股起买
            pe_ttm=pe_ttm,
            pe_dyn=pe_dyn,
            pb=pb,
            market_cap=mktcap,
            roe=latest.get("roe") if latest else None,
            gross_margin=latest.get("gross_margin") if latest else None,
            net_margin=latest.get("net_margin") if latest else None,
            yoy=yoy,
            cagr=cagr,
            ocf_to_profit=latest.get("ocf_to_profit") if latest else None,
            debt_ratio=latest.get("debt_ratio") if latest else None,
            goodwill_ratio=None,   # 美股不计算商誉
            deduct_ratio=None,      # 美股不计算扣非比
            ttm_netp=latest.get("net_profit") if latest else None,
            data_quality_flag="" if latest else (
                "missing_financials" if r.get("has_cik") else "missing_cik"
            ),
        )

        records.append(record)

    elapsed = time.time() - t0
    print(f"  组装: {len(records)} 只归一化记录 ({elapsed:.1f}s)")

    # 汇总
    print("\n  跳过统计:")
    print(f"    无 SEC CIK: {skipped_no_cik}")
    print(f"    无行情数据: {skipped_no_spot}")
    print(f"    无财务数据: {skipped_no_fin}")

    return records


# ── 输出函数 ──────────────────────────────────────────────

def _n(x, d=1):
    """格式化数值，None → 空字符串。"""
    return "" if x is None else f"{x:.{d}f}"


def _write_us_csv(records: list[dict], path: str):
    """写美股 CSV（含 market 列）。"""
    cols = [
        "rank", "tier", "score", "market", "code", "name", "price", "min_buy",
        "industry", "deepest_layer",
        "roe", "gross_margin", "net_margin", "yoy", "cagr",
        "ocf_to_profit", "debt_ratio",
        "pe_ttm", "peg", "eyield", "exp_ret", "discount", "pb",
        "market_cap_yi_usd", "fail_reasons",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for i, r in enumerate(records, 1):
            min_buy = int((r.get("price") or 0) * 100)
            tier_short = {"A_可买入": "A", "B_优质待跌": "B", "C_接近合格": "C"}.get(
                r.get("tier", ""), "-"
            )
            w.writerow([
                i,
                tier_short,
                r.get("score"),
                r.get("market", MARKET_US),
                r.get("code"),
                r.get("name"),
                _n(r.get("price"), 2),
                min_buy,
                r.get("industry"),
                r.get("deepest"),
                _n(r.get("roe")),
                _n(r.get("gross_margin")),
                _n(r.get("net_margin")),
                _n(r.get("yoy")),
                _n(r.get("cagr")),
                _n(r.get("ocf_to_profit"), 2),
                _n(r.get("debt_ratio")),
                _n(r.get("pe_ttm"), 2),
                _n(r.get("peg"), 2),
                _n(r.get("eyield"), 2),
                _n(r.get("exp_ret")),
                _n((r.get("discount") or 0) * 100, 1),
                _n(r.get("pb"), 2),
                _n((r.get("market_cap") or 0) / 1e8, 1),
                "; ".join(r.get("fails", [])),
            ])


def _md_table_us(rows: list[dict]) -> str:
    """生成美股 Markdown 表格。"""
    head = (
        "| 排名 | 代码 | 名称 | 现价 | 一手 | 行业 | 评分 | "
        "ROE% | 毛利% | 净利% | 同比% | CAGR% | "
        "PE(TTM) | PEG | 预期年化% | 折让% | 负债% | 市值(亿·USD) |"
    )
    sep = "|" + "---|" * 17
    lines = [head, sep]
    for i, r in enumerate(rows, 1):
        name_disp = r.get("name", "")
        min_buy = int((r.get("price") or 0) * 100)
        lines.append(
            f"| {i} | {r.get('code','')} | {name_disp} | "
            f"{_n(r.get('price'),2)} | {min_buy} | {r.get('industry','')} | "
            f"{_n(r.get('score'))} | "
            f"{_n(r.get('roe'))} | {_n(r.get('gross_margin'))} | {_n(r.get('net_margin'))} | "
            f"{_n(r.get('yoy'))} | {_n(r.get('cagr'))} | "
            f"{_n(r.get('pe_ttm'),1)} | {_n(r.get('peg'),2)} | "
            f"{_n(r.get('exp_ret'))} | {_n((r.get('discount') or 0)*100)} | "
            f"{_n(r.get('debt_ratio'))} | {_n((r.get('market_cap') or 0)/1e8)} |"
        )
    return "\n".join(lines)


def _write_us_md(
    tier_a: list[dict],
    tier_b: list[dict],
    tier_c: list[dict],
    path: str,
    year: int,
    total_eval: int,
):
    """写美股 Markdown 短名单。"""
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"# 美股五层选股结果（{year}年报 · 生成于 {ts}）\n",
        f"全市场参与筛选 **{total_eval}** 只美股。按评分（满分100）从高到低排序。\n",
        "> 数据来源：SEC EDGAR (XBRL 10-K) + Nasdaq Trader + 东方财富行情。"
        "第0-3层为量化筛选，**第4层定性（护城河/商业模式/能力圈/管理层/行业景气）需人工把关**。\n",
        f"## 🟢 Tier A · 可买入（五层全过，估值已到买点）— {len(tier_a)} 只\n",
        "通过排雷+质量+估值，且当前市值已打到合理价 7 折以内、预期年化≥10%。\n",
        _md_table_us(tier_a) if tier_a else (
            "_本期无标的同时满足「优质 + 打7折」。"
            "这很正常——好公司很少便宜。见下方 Tier B 候选池。_"
        ),
        f"\n\n## 🟡 Tier B · 优质待跌（质量确认，估值/买点未到）— {len(tier_b)} 只\n",
        "排雷与质量层全部过关的真·好生意，只是现在不够便宜。**加自选，等回调到买点**。\n",
        _md_table_us(tier_b[:60]),
        f"\n\n## ⚪ Tier C · 接近合格（排雷过关，质量仅差一项）— {len(tier_c)} 只\n",
        "仅供观察，差一口气，可留意基本面是否改善。\n",
        _md_table_us(tier_c[:40]),
        "\n\n---\n### 字段说明\n",
        "- **现金流/利润**：经营现金流 ÷ 净利润，≥0.8 说明利润是真金白银\n",
        "- **折让%**：(合理市值−当前市值)/合理市值，正数=低于合理价；≥30% 才算到买点\n",
        "- **预期年化**：盈利收益率(1/PE) + 增长率\n",
        "- **PEG**：PE ÷ 增长率（同比与3年CAGR取小，偏保守）\n",
        "- 评分权重：质量55（ROE/毛利/净利/增速/现金流/动能）+ 估值安全45（盈利收益率/PEG/折让/行业相对PE）\n",
        "- 美股不计算商誉与扣非比，相关字段留空\n",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _write_us_stable_entry(out_dir: str, ts: str):
    """写稳定入口 HTML（重定向到最新日期页）。"""
    latest_name = f"usstock_screen_{ts}.html"
    target_js = json.dumps(latest_name, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={latest_name}">
<title>美股五层选股固定入口</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#172033}}
.box{{max-width:560px;margin:14vh auto;padding:24px;background:#fff;border:1px solid #dbe4f0;border-radius:8px}}
a{{color:#2563eb}}
</style></head><body>
<div class="box">
<h1>美股五层选股固定入口</h1>
<p>正在打开最新总表：<a href="{latest_name}">{latest_name}</a></p>
<p>日期页继续作为后台历史产物保留；日常请访问 <code>usstock_screen.html</code>。</p>
</div>
<script>location.replace({target_js})</script>
</body></html>"""
    for name in (US_STABLE_SCREEN_NAME,):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(html)


# ── HTML 模板 (美股专用) ───────────────────────────────────

US_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股五层选股结果</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:light;--bg:#f6f8fb;--text:#172033;--heading:#0f172a;--muted:#64748b;--surface:#fff;--surface-soft:#f8fafc;--band:#eef4f8;--header-a:#ffffff;--header-b:#eef4ff;--border:#dbe4f0;--border-strong:#cbd5e1;--head:#eef2f7;--link:#2563eb;--green:#16a34a;--yellow:#b45309;--red:#dc2626;--hover:#f8fbff;--warn:#fff7ed;--warn-hover:#ffedd5;--tag:#f1f5f9;--toast-bg:#ecfdf5;--toast-border:#86efac;--shadow:0 1px 2px rgba(15,23,42,.04)}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0f1115;--text:#e6e8eb;--heading:#f8fafc;--muted:#9aa4b2;--surface:#131820;--surface-soft:#1a1f29;--band:#0d1117;--header-a:#161a22;--header-b:#0f1115;--border:#232936;--border-strong:#2a3140;--head:#1a1f29;--link:#7fb3ff;--green:#3ddc84;--yellow:#ffd166;--red:#ff6b6b;--hover:#161b24;--warn:#1f1a12;--warn-hover:#26200f;--tag:#1c1f26;--toast-bg:#1a2a1a;--toast-border:#3ddc84;--shadow:none}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:13px}
header{padding:16px 20px;background:linear-gradient(135deg,var(--header-a),var(--header-b));border-bottom:1px solid var(--border)}
.head-main{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{margin:0 0 4px;font-size:18px}
.sub{color:var(--muted);font-size:12px}
.market-links{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.market-link{font-size:11px;color:var(--link);text-decoration:none;padding:2px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-soft)}
.market-link.active{background:var(--link);color:#fff;border-color:var(--link)}
/* Dashboard */
.dash{padding:16px 20px;background:var(--band);border-bottom:1px solid var(--border);display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:stretch}
.dash-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;overflow:hidden;box-shadow:var(--shadow)}
.dash-card h3{margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:600}
.dash-card canvas{width:100%;height:210px}
.leaderboard{display:flex;flex-direction:column;gap:6px;font-size:12px;overflow:visible}
.lb-row{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:4px}
.lb-row:hover{background:var(--hover)}
.lb-rank{width:20px;text-align:center;font-weight:700;color:var(--muted)}
.lb-rank.r1{color:var(--yellow)}.lb-rank.r2{color:var(--muted)}.lb-rank.r3{color:#92400e}
.lb-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-code{color:var(--link);font-variant-numeric:tabular-nums;width:64px;flex:0 0 64px}
.lb-score{font-weight:700;color:var(--green);width:42px;text-align:right;flex:0 0 42px}
.lb-tag{font-size:10px;padding:1px 5px;border-radius:3px;font-weight:700}
.lb-tag.a{background:#dcfce7;color:#166534}.lb-tag.b{background:#fef3c7;color:#92400e}
.funnel{display:flex;gap:0;height:170px;align-items:flex-end;padding:18px 6px 40px}
.funnel-bar{flex:1;border-radius:5px 5px 0 0;position:relative;min-width:36px;margin:0 2px;transition:all .3s}
.funnel-bar:hover{filter:brightness(1.3)}
.funnel-val{position:absolute;top:-18px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:700;white-space:nowrap}
.funnel-lbl{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:10px;color:var(--muted);text-align:center;white-space:nowrap}
/* Controls */
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:20;box-shadow:var(--shadow)}
.controls input#q{flex:1 1 160px;min-width:140px;max-width:220px}
input,select{background:var(--surface);border:1px solid var(--border-strong);color:var(--text);border-radius:7px;padding:7px 10px;font-size:13px;outline:none;min-height:34px}
input[type=checkbox]{min-height:auto;width:auto;padding:0}
input:focus,select:focus{border-color:var(--link)}
.btn{cursor:pointer;background:var(--surface-soft);border:1px solid var(--border-strong);color:var(--text);border-radius:7px;padding:7px 12px;font-size:12px;min-height:34px;white-space:nowrap}
.btn.on{background:var(--link);border-color:var(--link);color:#fff}
.btn.primary{background:#eff6ff;border-color:#bfdbfe;color:#2563eb}
:root[data-theme="dark"] .btn.primary{background:#1d2033;border-color:#1e2340;color:#7fb3ff}
.chk{display:flex;align-items:center;gap:5px;color:var(--muted);cursor:pointer;user-select:none;min-height:34px}
.toast{position:fixed;bottom:20px;right:20px;background:var(--toast-bg);border:1px solid var(--toast-border);border-radius:8px;padding:10px 16px;color:var(--green);font-size:12px;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none;max-width:min(520px,calc(100vw - 32px))}
.toast.show{opacity:1}
.wrap{overflow:auto;height:calc(100vh - 52px)}
table{border-collapse:collapse;width:100%;white-space:nowrap}
th{position:sticky;top:0;background:var(--head);color:var(--text);font-weight:600;padding:8px 10px;text-align:right;cursor:pointer;border-bottom:2px solid var(--border-strong);font-size:12px}
th.l,td.l{text-align:left}
th:hover{color:var(--heading)}
td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--border)}
tr:hover td{background:var(--hover)}
tr.warn td{background:var(--warn)}
tr.warn:hover td{background:var(--warn-hover)}
.badge{display:inline-block;min-width:18px;text-align:center;border-radius:5px;padding:1px 6px;font-weight:700;font-size:11px}
.bA{background:#dcfce7;color:#166534}.bB{background:#fef3c7;color:#92400e}.bC{background:#e2e8f0;color:#475569}.b-{background:var(--tag);color:var(--muted)}
.pos{color:var(--green)}.neg{color:var(--red)}
.code{color:var(--link);font-variant-numeric:tabular-nums}
.note{color:var(--yellow);font-size:11px;text-align:left;max-width:320px;white-space:normal}
.cnt{color:var(--muted);font-size:12px;margin-left:auto}
.hint{color:var(--muted);font-size:12px;white-space:nowrap}
footer{padding:10px 20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border)}
@media(max-width:720px){.dash{grid-template-columns:1fr;padding:12px}.controls{align-items:stretch}.controls .btn,.controls select,.controls .chk{flex:1 1 auto}.controls input#q{max-width:none;flex-basis:100%}.cnt{flex-basis:100%;margin-left:0}.head-main{align-items:flex-start}.theme-btn{margin-left:0}}
</style></head><body>
<header>
<div class="head-main">
<div>
<h1>美股「五层选股流水线」结果</h1>
<div class="sub" id="sub"></div>
<div class="market-links">
<a class="market-link" href="screen.html">← 全市场总览</a>
<a class="market-link" href="astock_screen.html">A股</a>
<a class="market-link" href="hkstock_screen.html">港股</a>
<a class="market-link active" href="usstock_screen.html">美股</a>
</div>
</div>
<button class="btn theme-btn" id="themeToggle" title="切换暗色/亮色主题">🌙 暗色</button>
</div>
</header>
<!-- Dashboard -->
<div class="dash" id="dash">
<div class="dash-card">
<h3>📊 五层漏斗 (全市场 <span id="dtotal"></span> 只)</h3>
<div class="funnel" id="funnel"></div>
</div>
<div class="dash-card">
<h3>🏆 候选榜 Top 10 (A+B)</h3>
<div class="leaderboard" id="lbA"></div>
</div>
<div class="dash-card">
<h3>📈 评分分布</h3>
<canvas id="cvScore"></canvas>
</div>
<div class="dash-card">
<h3>🏭 行业分布 (A+B 通过数)</h3>
<canvas id="cvInd"></canvas>
</div>
</div>
<!-- Controls -->
<div class="controls">
<input id="q" placeholder="搜索代码/名称…">
<button class="btn filter-btn on" data-t="all">全部</button>
<button class="btn filter-btn" data-t="A">🟢A 可买入</button>
<button class="btn filter-btn" data-t="B">🟡B 优质待跌</button>
<button class="btn filter-btn" data-t="C">⚪C 接近</button>
<button class="btn filter-btn" data-t="x">未通过</button>
<select id="ind"></select>
<label class="chk"><input type="checkbox" id="pass">仅通过排雷(第0层)</label>
<button class="btn primary" onclick="window.location.href='screen.html'">← 返回全市场总览</button>
<button class="btn primary" onclick="window.location.reload()">↻ 重新加载</button>
<span class="cnt" id="cnt"></span>
</div>
<div class="toast" id="toast"></div>
<div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<footer>美股数据来源：SEC EDGAR (XBRL 10-K) + Nasdaq Trader + 东方财富行情 · 第0层排雷→第1层质量→第2层估值→第3层安全边际 · 第4层定性需人工把关 · 市场: 美股</footer>
<script>
var DATA=__DATA__, INDS=__INDS__, META=__META__;
var COLS=[["rk","#","n"],["tier","档","s"],["code","代码","s"],["name","名称","s"],
["px","现价","n"],["mb","一手","n"],["sc","评分","n"],["L","层","n"],["roe","ROE%","n"],["gm","毛利%","n"],
["nm","净利%","n"],["yoy","同比%","n"],["cagr","CAGR%","n"],["pe","PE","n"],["peg","PEG","n"],
["er","预期年化%","n"],["disc","折让%","n"],["debt","负债%","n"],["cap","市值亿·USD","n"],["ind","行业","s"],["note","落选原因","s"]];
var state={t:"all",q:"",ind:"",pass:false,sk:"sc",sd:-1};
function fmt(v){return v===null||v===undefined?"":v}
function cssVar(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()}
function setTheme(theme){
 document.documentElement.setAttribute("data-theme",theme);
 localStorage.setItem("theme",theme);
 var btn=document.getElementById("themeToggle");
 if(btn)btn.textContent=theme==="dark"?"☀️ 亮色":"🌙 暗色";
 setTimeout(function(){drawScoreChart();drawIndChart()},0);
}
function initTheme(){
 var saved=localStorage.getItem("theme");
 if(!saved)saved=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
 setTheme(saved);
 document.getElementById("themeToggle").addEventListener("click",function(){
  setTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark");
 });
}
initTheme();
document.getElementById("sub").innerHTML=META.year+"年报口径 · 市场: 美股 · 生成于 "+META.ts+" · 全市场评估 "+META.total+" 只";

// ---- Dashboard rendering ----
function drawScoreChart(){
 var cv=document.getElementById("cvScore");if(!cv)return;
 var W=cv.parentElement.clientWidth-28,H=140;
 cv.width=W*2;cv.height=H*2;cv.style.width=W+"px";cv.style.height=H+"px";
 var ctx=cv.getContext("2d");ctx.scale(2,2);
 var bins=META.scoreBins,keys=Object.keys(bins),vals=Object.values(bins);
 var max=Math.max.apply(null,vals),barW=(W-40)/keys.length;
 var colors=["#cbd5e1","#94a3b8","#64748b","#93c5fd",cssVar("--link"),cssVar("--green"),"#facc15","#f97316"];
 ctx.clearRect(0,0,W,H);
 for(var i=0;i<vals.length;i++){
  var bh=vals[i]/max*(H-30),x=20+i*barW,y=H-15-bh;
  ctx.fillStyle=colors[i];ctx.fillRect(x+2,y,barW-4,bh);
  ctx.fillStyle=cssVar("--muted");ctx.font="10px sans-serif";ctx.textAlign="center";
  ctx.fillText(vals[i],x+barW/2,y-4);
  ctx.fillText(keys[i],x+barW/2,H-2);
 }
}
function drawIndChart(){
 var cv=document.getElementById("cvInd");if(!cv)return;
 var W=cv.parentElement.clientWidth-28,H=210,labelPad=76;
 cv.width=W*2;cv.height=H*2;cv.style.width=W+"px";cv.style.height=H+"px";
 var ctx=cv.getContext("2d");ctx.scale(2,2);
 var visibleCount=W<420?6:8;
 var inds=META.topInds.slice(0,visibleCount);
 if(!inds.length){ctx.fillStyle=cssVar("--muted");ctx.font="12px sans-serif";ctx.textAlign="center";ctx.fillText("暂无 A+B 行业分布",W/2,H/2);return}
 var max=inds[0][1],barW=(W-70)/inds.length;
 var colors=["#22c55e","#3b82f6","#facc15","#60a5fa","#f97316","#c084fc","#94a3b8","#64748b"];
 ctx.clearRect(0,0,W,H);
 for(var i=0;i<inds.length;i++){
  var bh=inds[i][1]/max*(H-labelPad-12),x=50+i*barW,y=H-labelPad-bh;
  ctx.fillStyle=colors[i];ctx.fillRect(x+2,y,barW-4,bh);
  ctx.fillStyle=cssVar("--text");ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(inds[i][1],x+barW/2,y-4);
  ctx.save();ctx.translate(x+barW/2,H-labelPad+58);ctx.rotate(-0.58);
  ctx.fillStyle=cssVar("--muted");ctx.font="10px sans-serif";ctx.fillText(inds[i][0],0,0);ctx.restore();
 }
}
function renderFunnel(){
 var layers=["第0排雷","第1质量","第2估值","第3安全","第4定性"],values=[META.funnel[0],META.funnel[1],META.funnel[2],META.funnel[3],META.funnel[4]],
  colors=["#94a3b8",cssVar("--link"),"#60a5fa","#facc15","#22c55e"],
  max=META.total,html="";
 document.getElementById("dtotal").textContent=META.total;
 for(var i=0;i<layers.length;i++){
  var pct=values[i]/max*100,h=pct*0.85+15;
  html+='<div class="funnel-bar" style="height:'+h+'px;background:'+colors[i]+'">'
    +'<div class="funnel-val">'+values[i]+'</div>'
    +'<div class="funnel-lbl">'+layers[i]+'<br>'+pct.toFixed(1)+'%</div></div>'
 }
 document.getElementById("funnel").innerHTML=html;
}
function renderLB(){
 var a=[],b=[];
 DATA.forEach(function(r){if(r.tier==="A")a.push(r);else if(r.tier==="B")b.push(r)});
 a.sort(function(x,y){return y.sc-x.sc});b.sort(function(x,y){return y.sc-x.sc});
 var rows=a.concat(b).slice(0,10),html1="";
 rows.forEach(function(r,i){
  var rc=i<3?"r"+(i+1):"";
  var tg=r.tier==="A"?"a":"b";
  html1+='<div class="lb-row"><span class="lb-rank '+rc+'">'+(i+1)+'</span>'
    +'<span class="lb-code">'+r.code+'</span><span class="lb-name">'+r.name+'</span>'
    +'<span class="lb-tag '+tg+'">'+r.tier+'</span><span class="lb-score">'+r.sc+'</span></div>';
 });
 document.getElementById("lbA").innerHTML=html1||'<div style="color:#64748b;font-size:12px;padding:20px;text-align:center">本期无 A/B 候选</div>';
}
// init dashboard
renderFunnel();renderLB();drawScoreChart();drawIndChart();
window.addEventListener("resize",function(){drawScoreChart();drawIndChart()});

// ---- Table ----
var sel=document.getElementById("ind");sel.innerHTML='<option value="">全部行业</option>'+INDS.map(function(x){return '<option>'+x+'</option>'}).join("");
function head(){document.getElementById("head").innerHTML=COLS.map(function(c){
 var ar=state.sk===c[0]?(state.sd<0?" ▼":" ▲"):"";
 var cl=c[2]==="s"?"l":"";return '<th class="'+cl+'" data-k="'+c[0]+'">'+c[1]+ar+'</th>'}).join("")}
function tierBadge(t){var k=t||"-";return '<span class="badge b'+k+'">'+(t||"-")+'</span>'}
function rowHTML(r){
 var cells=COLS.map(function(c){var k=c[0],v=r[k];
  if(k==="tier")return '<td>'+tierBadge(v)+'</td>';
  if(k==="code")return '<td class="l"><span class="code">'+fmt(v)+'</span></td>';
  if(k==="name")return '<td class="l">'+fmt(v)+'</td>';
  if(k==="ind")return '<td class="l">'+fmt(v)+'</td>';
  if(k==="note")return '<td class="note">'+fmt(v)+'</td>';
  if(k==="disc"){var cls=v>0?"pos":(v<0?"neg":"");return '<td class="'+cls+'">'+fmt(v)+'</td>'}
  return '<td>'+fmt(v)+'</td>'}).join("");
 return '<tr>'+cells+'</tr>'}
function render(){
 var q=state.q.trim().toLowerCase();
 var rows=DATA.filter(function(r){
  if(state.t==="A"&&r.tier!=="A")return false;
  if(state.t==="B"&&r.tier!=="B")return false;
  if(state.t==="C"&&r.tier!=="C")return false;
  if(state.t==="x"&&r.tier!=="")return false;
  if(state.ind&&r.ind!==state.ind)return false;
  if(state.pass&&r.L<1)return false;
  if(q&&r.code.indexOf(q)<0&&r.name.toLowerCase().indexOf(q)<0)return false;
  return true});
 var sk=state.sk,sd=state.sd,typ=(COLS.find(function(c){return c[0]===sk})||[])[2];
 rows.sort(function(a,b){var x=a[sk],y=b[sk];
  if(x===null||x===undefined)x=typ==="n"?-1e18:"";if(y===null||y===undefined)y=typ==="n"?-1e18:"";
  if(typ==="n")return (x-y)*sd; return (x<y?-1:x>y?1:0)*sd});
 document.getElementById("body").innerHTML=rows.map(rowHTML).join("");
 document.getElementById("cnt").textContent="显示 "+rows.length+" / "+DATA.length+" 只";}
document.getElementById("head").addEventListener("click",function(e){var k=e.target.getAttribute("data-k");if(!k)return;
 if(state.sk===k)state.sd=-state.sd;else{state.sk=k;state.sd=(COLS.find(function(c){return c[0]===k})[2]==="n")?-1:1}head();render()});
document.querySelectorAll(".filter-btn").forEach(function(b){b.addEventListener("click",function(){
 document.querySelectorAll(".filter-btn").forEach(function(x){x.classList.remove("on")});b.classList.add("on");state.t=b.getAttribute("data-t");render()})});
document.getElementById("q").addEventListener("input",function(e){state.q=e.target.value;render()});
document.getElementById("ind").addEventListener("change",function(e){state.ind=e.target.value;render()});
document.getElementById("pass").addEventListener("change",function(e){state.pass=e.target.checked;render()});
head();render();
</script></body></html>"""


def _write_us_html(
    records: list[dict],
    path: str,
    year: int,
    total_eval: int,
    tier_counts: tuple[int, int, int],
):
    """生成美股自包含交互式 HTML 页面。"""
    rnd = lambda x, d=1: (None if x is None else round(x, d))
    tshort = {"A_可买入": "A", "B_优质待跌": "B", "C_接近合格": "C"}

    data = []
    for i, r in enumerate(records, 1):
        data.append({
            "rk": i,
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "px": rnd(r.get("price"), 2),
            "mb": int((r.get("price") or 0) * 100),
            "ind": r.get("industry", ""),
            "tier": tshort.get(r.get("tier", ""), ""),
            "sc": r.get("score"),
            "L": r.get("deepest"),
            "roe": rnd(r.get("roe")),
            "gm": rnd(r.get("gross_margin")),
            "nm": rnd(r.get("net_margin")),
            "yoy": rnd(r.get("yoy")),
            "cagr": rnd(r.get("cagr")),
            "pe": rnd(r.get("pe_ttm"), 2),
            "peg": rnd(r.get("peg"), 2),
            "er": rnd(r.get("exp_ret")),
            "disc": rnd((r.get("discount") or 0) * 100, 1),
            "debt": rnd(r.get("debt_ratio")),
            "cap": rnd((r.get("market_cap") or 0) / 1e8, 1),
            "note": "; ".join(r.get("fails", [])),
        })

    inds = sorted({r.get("industry", "") for r in records})

    # 评分分布
    score_bins = {
        "0-20": 0, "20-30": 0, "30-40": 0, "40-50": 0,
        "50-60": 0, "60-70": 0, "70-80": 0, "80-100": 0,
    }
    for r in records:
        s = r.get("score", 0)
        if s < 20:
            score_bins["0-20"] += 1
        elif s < 30:
            score_bins["20-30"] += 1
        elif s < 40:
            score_bins["30-40"] += 1
        elif s < 50:
            score_bins["40-50"] += 1
        elif s < 60:
            score_bins["50-60"] += 1
        elif s < 70:
            score_bins["60-70"] += 1
        elif s < 80:
            score_bins["70-80"] += 1
        else:
            score_bins["80-100"] += 1

    # 行业分布
    ind_count = Counter(
        r.get("industry", "")
        for r in records
        if r.get("tier") in ("A_可买入", "B_优质待跌")
    )
    top_inds = ind_count.most_common(8)

    # 五层漏斗
    l0_pass = sum(1 for r in records if r.get("deepest", 0) >= 1)
    l1_pass = sum(1 for r in records if r.get("deepest", 0) >= 2)
    l2_pass = sum(1 for r in records if r.get("deepest", 0) >= 3)
    l3_pass = sum(1 for r in records if r.get("deepest", 0) >= 4)
    funnel = [total_eval, l0_pass, l1_pass, l2_pass, l3_pass]

    meta = {
        "year": year,
        "total": total_eval,
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "A": tier_counts[0],
        "B": tier_counts[1],
        "C": tier_counts[2],
        "scoreBins": score_bins,
        "topInds": top_inds,
        "funnel": funnel,
        "market": MARKET_US,
    }

    html = US_HTML_TEMPLATE.replace(
        "__DATA__", json.dumps(data, ensure_ascii=False)
    ).replace(
        "__INDS__", json.dumps(inds, ensure_ascii=False)
    ).replace(
        "__META__", json.dumps(meta, ensure_ascii=False)
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def write_us_results(
    records: list[dict],
    output_dir: str | None = None,
    year: int = 2025,
) -> str:
    """输出美股五层筛选结果到 HTML / CSV / Markdown。

    输出文件:
      - results/usstock_screen_{date}.html
      - results/usstock_screen_{date}.csv
      - results/usstock_shortlist_{date}.md
      - results/usstock_screen.html  (稳定入口 → 最新日期版)

    Args:
        records: 已打分的标准化记录列表。
        output_dir: 输出目录，默认项目根下的 results/。
        year: 年报口径年份。

    Returns:
        str: 日期时间戳字符串 (YYYYMMDD)。
    """
    out_dir = output_dir or RESULTS_DIR
    os.makedirs(out_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d")

    # ── 分类 ──
    tier_a = [r for r in records if r.get("tier") == "A_可买入"]
    tier_b = [r for r in records if r.get("tier") == "B_优质待跌"]
    tier_c = [r for r in records if r.get("tier") == "C_接近合格"]
    total_eval = len(records)

    # ── HTML ──
    html_path = os.path.join(out_dir, f"usstock_screen_{ts}.html")
    _write_us_html(records, html_path, year, total_eval, (len(tier_a), len(tier_b), len(tier_c)))

    # ── CSV ──
    csv_path = os.path.join(out_dir, f"usstock_screen_{ts}.csv")
    _write_us_csv(records, csv_path)

    # ── Markdown ──
    md_path = os.path.join(out_dir, f"usstock_shortlist_{ts}.md")
    _write_us_md(tier_a, tier_b, tier_c, md_path, year, total_eval)

    # ── 稳定入口 ──
    _write_us_stable_entry(out_dir, ts)

    print(f"\n✅ 输出完成 ({year} 年报):")
    print(f"   HTML: {html_path}")
    print(f"   CSV:  {csv_path}")
    print(f"   MD:   {md_path}")
    print(f"   固定入口: {os.path.join(out_dir, US_STABLE_SCREEN_NAME)}")

    return ts


# ── 测试/便利函数 ──────────────────────────────────────────

def test_us_screener(
    year: int = 2025,
    max_stocks: int = 100,
) -> tuple[list[dict], dict[str, int]]:
    """便捷测试函数：运行 build_us_records 并打印摘要 (限制数量)。

    Args:
        year: 年报口径年份。
        max_stocks: 最大处理股票数 (加快测试)。

    Returns:
        (records, tier_counts): 标准化记录列表 和 Tier 分布。
    """
    # 构建完整的 build_us_records 的轻量版
    print(f"=== 美股五层选股 测试模式 (最多 {max_stocks} 只) ===\n")

    # Step 1-2: 构建主表 (全量，但后续只取前 max_stocks)
    print("[1/2] 构建美股证券主表...")
    try:
        universe = build_us_stock_universe()
        sec_master = fetch_sec_ticker_master()
        merged = merge_universe_with_sec(universe, sec_master)
    except Exception as e:
        print(f"  ⚠ 主表构建失败: {e}")
        return [], {}

    # 限制数量
    merged = [r for r in merged if r["has_cik"]][:max_stocks]
    print(f"  测试范围: {len(merged)} 只有 CIK 的美股")

    # Step 3: 行情
    print("\n[2/2] 抓取行情 + 财务数据...")
    spot_data = fetch_us_spot_snapshot()

    fin_results: dict[str, dict | None] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=SEC_FETCH_WORKERS) as ex:
        futures = {}
        for r in merged:
            ticker = r["ticker"]
            futures[ex.submit(_safe_api_call, r["cik"], ticker, year)] = ticker

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                fin_results[ticker] = future.result()
            except Exception:
                fin_results[ticker] = None
            completed += 1

    # 组装
    records_raw = []
    for r in merged:
        ticker = r["ticker"]
        spot = spot_data.get(ticker)
        if not spot:
            continue
        price = _fnum(spot.get("price"))
        mktcap = _fnum(spot.get("market_cap"))
        if price is None or mktcap is None:
            continue
        fin = fin_results.get(ticker)
        if fin is None:
            continue
        sic = fin.get("sic", "")
        industry = str(sic) if sic else r.get("name", "美股")
        if _is_financial_stock(industry_name=industry, sic_code=sic):
            continue
        latest = fin.get("latest")
        yoy = fin.get("yoy")
        cagr = fin.get("cagr")
        record = make_screening_record(
            market=MARKET_US, code=ticker, display_code=ticker,
            name=r.get("name", ticker),
            industry=industry, exchange=r.get("exchange", ""), currency="USD",
            price=price, min_buy=price,
            pe_ttm=_fnum(spot.get("pe_ttm")), pe_dyn=_fnum(spot.get("pe_dyn")),
            pb=_fnum(spot.get("pb")), market_cap=mktcap,
            roe=latest.get("roe") if latest else None,
            gross_margin=latest.get("gross_margin") if latest else None,
            net_margin=latest.get("net_margin") if latest else None,
            yoy=yoy, cagr=cagr,
            ocf_to_profit=latest.get("ocf_to_profit") if latest else None,
            debt_ratio=latest.get("debt_ratio") if latest else None,
            goodwill_ratio=None, deduct_ratio=None,
            ttm_netp=latest.get("net_profit") if latest else None,
            data_quality_flag="" if latest else "missing_financials",
        )
        records_raw.append(record)

    # 评分
    records, total_eval, _ = run_full_pipeline(records_raw, config=DEFAULT_CONFIG)
    tier_counts = dict(Counter(r.get("tier", "-") for r in records))

    print(f"\n{'='*60}")
    print(f"  美股五层选股 — {year}年报 测试摘要")
    print(f"{'='*60}")
    print(f"  处理标的:       {len(merged)}")
    print(f"  产出记录:       {len(records)}")
    print(f"  Tier A (可买入): {tier_counts.get('A_可买入', 0)}")
    print(f"  Tier B (优质待跌): {tier_counts.get('B_优质待跌', 0)}")
    print(f"  Tier C (接近合格): {tier_counts.get('C_接近合格', 0)}")
    print(f"  未通过:          {tier_counts.get('-', 0)}")

    if records:
        print("\n  Top 5:")
        for i, r in enumerate(records[:5], 1):
            print(
                f"  {i}. {r['code']} {r.get('name','')} "
                f"评分={r.get('score')} "
                f"档={r.get('tier','-')} "
                f"PE={_n(r.get('pe_ttm'),1)} "
                f"ROE={_n(r.get('roe'))}"
            )

    return records, tier_counts


# ── CLI 入口 ───────────────────────────────────────────────

def main():
    """命令行入口：python3 screeners/us.py [--year 2025] [--test N]"""
    import argparse

    ap = argparse.ArgumentParser(description="美股五层选股流水线")
    ap.add_argument(
        "--year", type=int, default=2025, help="年报口径年份 (默认 2025)"
    )
    ap.add_argument(
        "--test", type=int, default=0, metavar="N",
        help="限制测试数量；测试模式默认不写正式 results/ 产物"
    )
    ap.add_argument(
        "--no-html", action="store_true", help="跳过 HTML 输出"
    )
    ap.add_argument(
        "--output-dir", type=str, default=None, help="输出目录 (默认 results/)"
    )
    args = ap.parse_args()

    sample_mode = args.test > 0
    if sample_mode:
        records, tier_counts = test_us_screener(year=args.year, max_stocks=args.test)
    else:
        records = build_us_records(args.year)
        if records:
            records, total_eval, tier_counts = run_full_pipeline(records, config=DEFAULT_CONFIG)
        else:
            return

    if records and not args.no_html and sample_mode and args.output_dir is None:
        print(
            "\nℹ️ 测试模式不会写入正式 results/ 产物。"
            "如需保存样本，请显式传 --output-dir results/samples。"
        )
    elif records and not args.no_html:
        write_us_results(records, output_dir=args.output_dir, year=args.year)
    elif not records:
        print("\n⚠️ 未产出记录，请检查数据源或网络连接。")


if __name__ == "__main__":
    main()
