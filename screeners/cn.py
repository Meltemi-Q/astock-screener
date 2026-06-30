#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CN (A-share) screener — wraps astock_screener.py with the same interface as hk.py and us.py.

数据源:
  - 东方财富公开数据接口 (datacenter-web / push2delay)

归一化 + 打分:
  - screeners/contracts.py → 标准化记录 (make_screening_record)
  - screeners/scoring.py  → 五层流水线 evaluate + score (run_full_pipeline)

用法:
  from screeners.cn import build_cn_records, write_cn_results, test_cn_screener
  records = build_cn_records(2025)
  write_cn_results(records, year=2025)
  # 或直接运行: python3 screeners/cn.py
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter

# ── 路径：确保项目根在 sys.path ──
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

import astock_screener
from screeners.contracts import MARKET_CN, make_screening_record
from screeners.scoring import run_full_pipeline, DEFAULT_CONFIG

# ── 常量 ──
RESULTS_DIR = os.path.join(WORKDIR, "results")

# Module-level storage for raw A-stock records (needed by write functions for
# backward compatibility with astock_screener's HTML/CSV/MD output format).
_last_raw_records: list[dict] = []
_last_run_year: int = 2025


# ── 转换函数 ───────────────────────────────────────────────

def _convert_to_normalized(raw_record: dict) -> dict:
    """Convert an astock_screener raw record to a normalized screening record.

    The astock_screener raw format includes A-stock-specific fields (mktcap,
    deduct_ratio, notes, is_st, etc.).  We map them into the canonical
    make_screening_record shape, letting the scoring pipeline recompute
    derived fields (g, peg, eyield, fair_pe, etc.).
    """
    price = raw_record.get("price")
    mktcap = raw_record.get("mktcap")

    min_buy = (price * 100) if price else None

    return make_screening_record(
        market=MARKET_CN,
        code=raw_record.get("code", ""),
        display_code=raw_record.get("code", ""),
        name=raw_record.get("name", ""),
        industry=raw_record.get("industry", ""),
        exchange="SSE/SZSE",
        currency="CNY",
        price=price,
        min_buy=min_buy,
        pe_ttm=raw_record.get("pe_ttm"),
        pe_dyn=raw_record.get("pe_dyn"),
        pb=raw_record.get("pb"),
        market_cap=mktcap,
        market_cap_cny=mktcap,
        roe=raw_record.get("roe"),
        gross_margin=raw_record.get("gross_margin"),
        net_margin=raw_record.get("net_margin"),
        yoy=raw_record.get("yoy"),
        cagr=raw_record.get("cagr"),
        ocf_to_profit=raw_record.get("ocf_to_profit"),
        debt_ratio=raw_record.get("debt_ratio"),
        goodwill_ratio=raw_record.get("goodwill_ratio"),
        deduct_ratio=raw_record.get("deduct_ratio"),
        ttm_netp=raw_record.get("ttm_netp"),
        data_quality_flag="",
    )


# ── 核心：A 股记录装配 ─────────────────────────────────────

def build_cn_records(
    year: int = 2025,
    fresh: bool = False,
    quotes_fresh: bool = False,
) -> list[dict]:
    """Build CN (A-share) normalized screening records.

    Wraps astock_screener.build_records(), converts to normalized format,
    then runs the five-layer scoring pipeline.

    Args:
        year: Fiscal year for annual reports (default 2025).
        fresh: If True, disable all HTTP caching (--fresh).
        quotes_fresh: If True, only refresh quote cache; keep fundamental
                      data cache (--quotes-fresh).

    Returns:
        list[dict]: Scored, normalized screening records sorted by score desc.
    """
    global _last_raw_records, _last_run_year

    # ── 缓存标志：保存/恢复，避免污染模块级全局 ──
    _saved_use_cache = astock_screener.USE_CACHE
    _saved_force_spot = astock_screener.FORCE_SPOT_REFRESH
    try:
        if fresh:
            astock_screener.USE_CACHE = False
        if quotes_fresh:
            astock_screener.FORCE_SPOT_REFRESH = True

        print(f"\n{'='*60}")
        print(f"  A股五层选股 — {year} 年报口径")
        print(f"{'='*60}\n")

        t_start = time.time()

        # ── 1. Build raw records via astock_screener ──
        raw_records = astock_screener.build_records(year)

        # Handle fallback (astock_screener main() retries year-1)
        effective_year = year
        if not raw_records:
            print(f"  ⚠️  {year} 年报数据为空，尝试回退到 {year-1} ...")
            raw_records = astock_screener.build_records(year - 1)
            effective_year = year - 1

        if not raw_records:
            print("  ⚠️ 未能获取任何 A 股数据，请检查网络或数据源")
            return []

        print(f"  装配完成: {len(raw_records)} 只 A 股，用时 {time.time()-t_start:.1f}s")

        # ── 2. Convert to normalized records ──
        normalized = [_convert_to_normalized(r) for r in raw_records]

        # ── 3. Run five-layer scoring pipeline ──
        normalized, total_eval, tier_counts = run_full_pipeline(
            normalized, config=DEFAULT_CONFIG, market=MARKET_CN,
        )

        # ── 4. Map scoring results back to raw records ──
        code_to_scored: dict[str, dict] = {r["code"]: r for r in normalized}
        for raw in raw_records:
            scored = code_to_scored.get(raw.get("code", ""))
            if scored:
                raw["deepest"] = scored.get("deepest", 0)
                raw["tier"] = scored.get("tier", "-")
                raw["score"] = scored.get("score", 0.0)
                raw["fails"] = scored.get("fails", [])

        # ── 5. Ensure raw records have notes for HTML output ──
        for raw in raw_records:
            raw.setdefault("notes", [])

        _last_raw_records = raw_records
        _last_run_year = effective_year

        # Sort normalized records by score desc
        normalized.sort(key=lambda r: r.get("score", 0), reverse=True)

        # ── 摘要 ────────────────────────────────────────────
        total_elapsed = time.time() - t_start
        print(f"\n{'─'*60}")
        print("  Tier 分布:")
        for tier in ("A_可买入", "B_优质待跌", "C_接近合格", "-"):
            cnt = tier_counts.get(tier, 0)
            emoji = {
                "A_可买入": "🟢", "B_优质待跌": "🟡",
                "C_接近合格": "⚪", "-": "⚫",
            }.get(tier, "")
            print(f"    {emoji} {tier}: {cnt}")
        print(f"  其中评估通过: {total_eval} 只")
        print(f"{'─'*60}")
        print(f"\n  ⏱️ 总用时: {total_elapsed:.1f}s")

        return normalized

    finally:
        astock_screener.USE_CACHE = _saved_use_cache
        astock_screener.FORCE_SPOT_REFRESH = _saved_force_spot


# ── 输出函数 ───────────────────────────────────────────────

def write_cn_results(
    records: list[dict],
    output_dir: str | None = None,
    year: int = 2025,
) -> str:
    """Write CN (A-share) screening results to HTML / CSV / Markdown.

    Delegates to astock_screener's write_html / write_csv / write_md for
    backward compatibility with existing run.sh and the HTML dashboard.

    输出文件:
      - results/astock_screen_{date}.html
      - results/astock_screen_{date}.csv
      - results/astock_shortlist_{date}.md
      - results/astock_screen.html  (稳定入口 → 最新日期版)

    Args:
        records: Scored normalized screening records (from build_cn_records).
        output_dir: Output directory, defaults to results/.
        year: Fiscal year for display.

    Returns:
        str: Date timestamp string (YYYYMMDD).
    """
    out_dir = output_dir or RESULTS_DIR
    os.makedirs(out_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d")

    # ── Reconstruct raw records for astock_screener write functions ──
    # Prefer stored raw records (post-scoring) for full fidelity.
    raw = _last_raw_records if _last_raw_records else _reconstruct_raw(records)

    # Ensure raw records carry the latest scoring from normalized records
    code_to_norm: dict[str, dict] = {r["code"]: r for r in records}
    for rw in raw:
        norm = code_to_norm.get(rw.get("code", ""))
        if norm:
            rw["deepest"] = norm.get("deepest", 0)
            rw["tier"] = norm.get("tier", "-")
            rw["score"] = norm.get("score", 0.0)
            rw["fails"] = norm.get("fails", [])
        rw.setdefault("notes", [])

    # Sort raw by score desc
    raw.sort(key=lambda r: r.get("score", 0), reverse=True)

    # ── Classify into tiers ──
    tier_a = [r for r in raw if r.get("tier") == "A_可买入"]
    tier_b = [r for r in raw if r.get("tier") == "B_优质待跌"]
    tier_c = [r for r in raw if r.get("tier") == "C_接近合格"]
    total_eval = len(raw)

    # ── Write CSV ──
    csv_path = os.path.join(out_dir, f"astock_screen_{ts}.csv")
    astock_screener.write_csv(raw, csv_path)

    # ── Write HTML (interactive dashboard) ──
    html_path = os.path.join(out_dir, f"astock_screen_{ts}.html")
    astock_screener.write_html(
        raw, html_path, year, total_eval,
        (len(tier_a), len(tier_b), len(tier_c)),
    )

    # ── Write Markdown shortlist ──
    md_path = os.path.join(out_dir, f"astock_shortlist_{ts}.md")
    astock_screener.write_md(tier_a, tier_b, tier_c, md_path, year, total_eval)

    # ── Stable entry: astock_screen.html → latest dated HTML ──
    astock_screener.write_latest_entrypoints(ts)

    print(f"\n✅ 输出完成 ({year} 年报):")
    print(f"   HTML: {html_path}")
    print(f"   CSV:  {csv_path}")
    print(f"   MD:   {md_path}")
    print(f"   固定入口: {os.path.join(out_dir, astock_screener.STABLE_SCREEN_NAME)}")

    return ts


def _reconstruct_raw(records: list[dict]) -> list[dict]:
    """Fallback: reconstruct astock_screener raw format from normalized records.

    Used when _last_raw_records is empty (e.g., records passed from another
    source).  Some A-stock-specific fields (notes, is_st, deduct_ratio) are
    best-effort populated from the normalized record.
    """
    raw = []
    for r in records:
        raw.append({
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "industry": r.get("industry", ""),
            "roe": r.get("roe"),
            "gross_margin": r.get("gross_margin"),
            "net_margin": r.get("net_margin"),
            "net_profit": None,  # not preserved in normalized
            "revenue": None,
            "yoy": r.get("yoy"),
            "cagr": r.get("cagr"),
            "g": r.get("g"),
            "eps": None,
            "deduct_ratio": r.get("deduct_ratio"),
            "ocf_ps": None,
            "ocf_to_profit": r.get("ocf_to_profit"),
            "debt_ratio": r.get("debt_ratio"),
            "goodwill_ratio": r.get("goodwill_ratio"),
            "price": r.get("price"),
            "mktcap": r.get("market_cap"),
            "pe_ttm": r.get("pe_ttm"),
            "pe_dyn": r.get("pe_dyn"),
            "pb": r.get("pb"),
            "ttm_netp": r.get("ttm_netp"),
            "eyield": r.get("eyield"),
            "peg": r.get("peg"),
            "exp_ret": r.get("exp_ret"),
            "reasonable_pe": r.get("fair_pe"),
            "fair_mktcap": r.get("fair_mktcap"),
            "discount": r.get("discount"),
            "deepest": r.get("deepest", 0),
            "tier": r.get("tier", "-"),
            "score": r.get("score", 0.0),
            "fails": r.get("fails", []),
            "notes": [],
            "is_st": False,
        })
    return raw


# ── 测试/便利函数 ──────────────────────────────────────────

def test_cn_screener(year: int = 2025) -> tuple[list[dict], dict[str, int]]:
    """Convenience test function: run build_cn_records and print summary.

    Args:
        year: Fiscal year.

    Returns:
        (records, tier_counts): Normalized records and tier distribution.
    """
    records = build_cn_records(year)

    tier_counts = dict(Counter(r.get("tier", "-") for r in records))

    print(f"\n{'='*60}")
    print(f"  A股五层选股 — {year}年报 测试摘要")
    print(f"{'='*60}")
    print(f"  总记录数:         {len(records)}")
    print(f"  Tier A (可买入):  {tier_counts.get('A_可买入', 0)}")
    print(f"  Tier B (优质待跌): {tier_counts.get('B_优质待跌', 0)}")
    print(f"  Tier C (接近合格): {tier_counts.get('C_接近合格', 0)}")
    print(f"  未通过:            {tier_counts.get('-', 0)}")

    if records:
        print("\n  Top 5:")
        for i, r in enumerate(records[:5], 1):
            pe_ttm = r.get("pe_ttm")
            print(
                f"  {i}. {r['code']} {r.get('name','')} "
                f"评分={r.get('score')} "
                f"档={r.get('tier','-')} "
                f"PE={f'{pe_ttm:.1f}' if pe_ttm else ''} "
                f"ROE={r.get('roe')}"
            )

    return records, tier_counts


# ── CLI 入口 ───────────────────────────────────────────────

def main():
    """命令行入口：python3 screeners/cn.py [--year 2025]"""
    import argparse

    ap = argparse.ArgumentParser(description="A股五层选股流水线 (normalized)")
    ap.add_argument(
        "--year", type=int, default=2025, help="年报口径年份 (默认 2025)"
    )
    ap.add_argument(
        "--fresh", action="store_true", help="忽略缓存，强制重新抓取"
    )
    ap.add_argument(
        "--quotes-fresh", action="store_true",
        help="仅强制刷新行情缓存，财报/基础数据继续使用缓存",
    )
    ap.add_argument(
        "--no-html", action="store_true", help="跳过 HTML 输出"
    )
    ap.add_argument(
        "--output-dir", type=str, default=None, help="输出目录 (默认 results/)"
    )
    args = ap.parse_args()

    records, _ = test_cn_screener(year=args.year)

    if records and not args.no_html:
        write_cn_results(records, output_dir=args.output_dir, year=args.year)
    elif not records:
        print("\n⚠️ 未产出记录，请检查数据源或网络连接。")


if __name__ == "__main__":
    main()
