#!/usr/bin/env python3
"""
Global multi-market five-layer stock screener.

Usage:
  python3 global_screener.py --market cn        # A-shares
  python3 global_screener.py --market hk        # Hong Kong
  python3 global_screener.py --market us        # US
  python3 global_screener.py --market all       # All markets
  python3 global_screener.py --market hk --fresh  # Force refresh
  python3 global_screener.py --market us --year 2024
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

# ── 路径：确保项目根在 sys.path ──
WORKDIR = os.path.dirname(os.path.abspath(__file__))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from screeners.cn import build_cn_records, write_cn_results
from screeners.hk import build_hk_records, write_hk_results
from screeners.us import build_us_records, write_us_results

# ── 市场注册表 ────────────────────────────────────────────

MARKET_BUILDERS = {
    "cn": build_cn_records,
    "hk": build_hk_records,
    "us": build_us_records,
}

MARKET_WRITERS = {
    "cn": lambda records, year, out_dir: write_cn_results(records, output_dir=out_dir, year=year),
    "hk": lambda records, year, out_dir: write_hk_results(records, output_dir=out_dir, year=year),
    "us": lambda records, year, out_dir: write_us_results(records, output_dir=out_dir, year=year),
}

MARKET_LABELS = {
    "cn": "A股",
    "hk": "港股",
    "us": "美股",
}

# Markets that support fresh / quotes-fresh flags
CN_FRESH_MARKETS = frozenset({"cn"})


def _run_one_market(
    market: str,
    year: int,
    fresh: bool = False,
    quotes_fresh: bool = False,
    no_html: bool = False,
    top_n: int = 0,
) -> dict | None:
    """Run the five-layer screener for a single market.

    Args:
        market: One of "cn", "hk", "us".
        year: Fiscal year.
        fresh: Disable all HTTP caching.
        quotes_fresh: Refresh quote cache only.
        no_html: Skip HTML output (CSV/MD only).
        top_n: If > 0, limit displayed records (testing mode).

    Returns:
        dict with keys market, label, total, tier_counts, elapsed, error
        or None if no records produced.
    """
    label = MARKET_LABELS.get(market, market.upper())
    print(f"\n{'#'*60}")
    print(f"#  {label} ({market.upper()}) 五层选股 — {year} 年报")
    print(f"{'#'*60}")

    t0 = time.time()

    builder = MARKET_BUILDERS[market]

    # Handle cn-specific fresh flags
    if market in CN_FRESH_MARKETS:
        records = builder(year, fresh=fresh, quotes_fresh=quotes_fresh)
    else:
        records = builder(year)

    if not records:
        elapsed = time.time() - t0
        print(f"  ⚠️ {label}: 未产出任何记录（{elapsed:.1f}s）")
        return {
            "market": market, "label": label, "total": 0,
            "tier_counts": {}, "elapsed": elapsed,
        }

    # ── Tier distribution ──
    tier_counts = dict(Counter(r.get("tier", "-") for r in records))
    total = len(records)
    elapsed = time.time() - t0

    # ── Display top N (if requested) ──
    tier_short = {"A_可买入": "A", "B_优质待跌": "B", "C_接近合格": "C"}

    if top_n > 0:
        sorted_records = sorted(records, key=lambda r: r.get("score", 0), reverse=True)
        show = sorted_records[:top_n]
        print(f"\n  Top {len(show)} ({label}):")
        print(f"  {'评分':>5} {'档':>2} {'代码':>10} {'名称':<12} {'ROE':>5} {'PE':>6}  {'行业'}")
        for r in show:
            t = tier_short.get(r.get("tier", ""), "-")
            pe = r.get("pe_ttm")
            print(f"  {r.get('score',0):>5.1f} {t:>2} {r.get('code',''):>10} "
                  f"{r.get('name','')[:12]:<12} {r.get('roe',''):>5} "
                  f"{f'{pe:.1f}' if pe else '':>6}  {r.get('industry','')}")

    # ── Summary ──
    print(f"\n  {label} 摘要:")
    print(f"    总记录:          {total}")
    for tier_full, short_lbl in tier_short.items():
        cnt = tier_counts.get(tier_full, 0)
        emoji = {"A": "🟢", "B": "🟡", "C": "⚪"}.get(short_lbl, "  ")
        print(f"    {emoji} Tier {short_lbl}:           {cnt}")
    print(f"    用时:            {elapsed:.1f}s")

    # ── Write output files ──
    if not no_html and records:
        writer = MARKET_WRITERS[market]
        writer(records, year, None)

    return {
        "market": market,
        "label": label,
        "total": total,
        "tier_counts": tier_counts,
        "elapsed": elapsed,
    }


def main():
    ap = argparse.ArgumentParser(
        description="全球多市场五层选股流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 global_screener.py --market cn
  python3 global_screener.py --market hk --fresh
  python3 global_screener.py --market us --year 2024
  python3 global_screener.py --market all
  python3 global_screener.py --market cn --quotes-fresh --top 20
        """,
    )
    ap.add_argument(
        "--market", required=True, choices=["cn", "hk", "us", "all"],
        help="目标市场: cn=A股, hk=港股, us=美股, all=全市场",
    )
    ap.add_argument(
        "--year", type=int, default=None,
        help="年报年份 (默认: 当前系统年份)",
    )
    ap.add_argument(
        "--fresh", action="store_true",
        help="禁用所有缓存，强制重新抓取全部数据",
    )
    ap.add_argument(
        "--quotes-fresh", action="store_true",
        help="仅刷新行情缓存，保留财报等基础数据缓存",
    )
    ap.add_argument(
        "--top", type=int, default=0,
        help="仅显示前 N 名摘要 (测试用，0=不限制)",
    )
    ap.add_argument(
        "--no-html", action="store_true",
        help="跳过 HTML 生成（仅输出 CSV/Markdown）",
    )
    args = ap.parse_args()

    # Default year = current system year
    year = args.year or time.localtime().tm_year

    # Determine which markets to run
    markets = ["cn", "hk", "us"] if args.market == "all" else [args.market]

    # ── Run each market ──
    t_total_start = time.time()
    results: list[dict] = []

    for m in markets:
        # For cn market, pass fresh/quotes_fresh flags through
        if m in CN_FRESH_MARKETS:
            # Ensure astock_screener sees the flags
            # (build_cn_records handles save/restore internally)
            pass

        result = _run_one_market(
            m, year,
            fresh=args.fresh,
            quotes_fresh=args.quotes_fresh,
            no_html=args.no_html,
            top_n=args.top,
        )
        if result:
            results.append(result)

    t_total = time.time() - t_total_start

    # ── Grand summary ──
    print(f"\n{'='*60}")
    print("  全球多市场选股汇总")
    print(f"{'='*60}")
    total_stocks = sum(r["total"] for r in results)
    print(f"  总股票数:  {total_stocks}")
    print(f"  总用时:    {t_total:.1f}s")
    print()

    for r in results:
        tc = r["tier_counts"]
        a_cnt = tc.get("A_可买入", 0)
        b_cnt = tc.get("B_优质待跌", 0)
        c_cnt = tc.get("C_接近合格", 0)
        print(f"  {r['label']} ({r['market'].upper()}): "
              f"{r['total']} 只 · {r['elapsed']:.0f}s "
              f"🟢A:{a_cnt} 🟡B:{b_cnt} ⚪C:{c_cnt}")

    print()
    print("✅ 全部完成")


if __name__ == "__main__":
    main()
