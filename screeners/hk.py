#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股「五层选股流水线」自动化筛选器
================================================
对应 A 股 astock_screener.py 的港股版本。

数据源:
  - 证券主表: data_sources/hkex.py  → HKEX 证券名单 + 每手股数
  - 实时行情: data_sources/eastmoney.py → 批量港股快照 (push2delay clist)
  - 财务数据: data_sources/hkex.py  → 东财港股财务指标 (RPT_HKF10_FN_GMAININDICATOR)
  - 历史K线:  data_sources/eastmoney.py → 日K线 (push2his)

归一化 + 打分:
  - screeners/contracts.py → 标准化记录 + Tier A/B 资格检查
  - screeners/scoring.py  → 五层流水线 evaluate + score

用法:
  from screeners.hk import build_hk_records, write_hk_results, test_hk_screener
  records = build_hk_records(2025)
  write_hk_results(records, year=2025)
  # 或直接运行: python3 screeners/hk.py
"""

from __future__ import annotations

import os
import sys
import json
import time
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# ── 路径：确保项目根在 sys.path ──
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data_sources.eastmoney import (
    fetch_hk_spot_snapshot,
)
from data_sources.hkex import (
    fetch_hkex_security_master,
    fetch_eastmoney_hk_financials,
    fetch_eastmoney_hk_cashflow,
    validate_hkex_master,
)
from screeners.contracts import (
    MARKET_HK,
    make_screening_record,
    check_currency_match,
)
from screeners.scoring import run_full_pipeline, DEFAULT_CONFIG

# ── 常量 ───────────────────────────────────────────────────
# 金融股关键词（港股排除银行/保险/券商）
_FINANCIAL_KEYWORDS = frozenset({"银行", "保险", "券商", "金融", "Bank", "Insurance", "Securities"})

# 财务数据抓取并发数 & 间隔
FIN_WORKERS = 10
FIN_API_SLEEP = 0.05
DEFAULT_HK_FIN_MAX_FETCHES = 500

# 输出目录
RESULTS_DIR = os.path.join(WORKDIR, "results")

# 稳定入口文件名
HK_STABLE_SCREEN_NAME = "hkstock_screen.html"


# ── 辅助函数 ───────────────────────────────────────────────

def _fnum(x):
    """安全转 float；None / '-' / '' → None。"""
    if x is None or x == "-" or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_financial_stock(name: str, category: str) -> bool:
    """判断是否为金融股（银行/保险/券商）。

    通过 stock name 或 category 匹配关键词。
    """
    combined = f"{name or ''} {category or ''}"
    for kw in _FINANCIAL_KEYWORDS:
        if kw in combined:
            return True
    # H 股常见金融代码模式
    if name and any(
        k in name for k in ("银行", "Bank", "HSBC", "Hang Seng", "BOC", "ICBC", "CCB", "ABC")
    ):
        return True
    return False


def _master_from_quote_universe(spot: dict[str, dict]) -> list[dict]:
    """Build a degraded HK master from the quote universe when HKEX is unavailable."""
    rows = []
    for code, quote in sorted(spot.items()):
        if not str(code).isdigit():
            continue
        rows.append({
            "code": str(code).zfill(5),
            "name": quote.get("name") or str(code).zfill(5),
            "board_lot": 100,
            "category": "Eastmoney quote universe",
            "isin": "",
        })
    return rows


def _hk_fin_max_fetches() -> int:
    """Return max HK financial API fetches for one full run.

    ``HK_FIN_MAX_FETCHES=0`` means skip financial APIs. ``-1`` means unlimited.
    """
    raw = os.environ.get("HK_FIN_MAX_FETCHES", str(DEFAULT_HK_FIN_MAX_FETCHES))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HK_FIN_MAX_FETCHES


def _currency_code(value: str) -> str:
    aliases = {
        "人民币": "CNY",
        "人民幣": "CNY",
        "元": "CNY",
        "¥": "CNY",
        "港元": "HKD",
        "港币": "HKD",
        "港幣": "HKD",
        "美元": "USD",
    }
    raw = (value or "").upper().strip()
    return aliases.get(raw, raw)


# ── 增长率计算 ─────────────────────────────────────────────

def compute_growth(financials: list[dict], year: int) -> tuple[float | None, float | None]:
    """从财务数据列表中计算 yoy（同比净利增速）和 3 年 CAGR。

    Args:
        financials: fetch_eastmoney_hk_financials() 返回的列表，
                   按 report_date 降序排列，每年一条 12-31 记录。
        year: 目标年度。

    Returns:
        (yoy, cagr): 单位均为百分比。无法计算时对应项为 None。

        - yoy = (net_profit_current - net_profit_prior) / abs(net_profit_prior) * 100
        - cagr = ((latest / first) ** (1/3) - 1) * 100  （3 年）
    """
    # 建立年份 → net_profit 映射
    yr_np = {}
    for r in financials:
        rd = r.get("report_date", "")
        if rd.endswith("-12-31"):
            y = int(rd[:4])
            np_ = r.get("net_profit")
            if np_ is not None and y not in yr_np:
                yr_np[y] = np_

    # 同比：当年 vs 前一年
    yoy = None
    np_cur = yr_np.get(year)
    np_prior = yr_np.get(year - 1)
    if np_cur is not None and np_prior is not None and np_prior != 0:
        yoy = (np_cur - np_prior) / abs(np_prior) * 100.0

    # 3 年 CAGR：当年 vs (year-3)
    cagr = None
    np_first = yr_np.get(year - 3)
    if np_cur is not None and np_first is not None and np_cur > 0 and np_first > 0:
        cagr = ((np_cur / np_first) ** (1.0 / 3.0) - 1.0) * 100.0

    return yoy, cagr


# ── 核心：港股记录装配 ─────────────────────────────────────

def build_hk_records(year: int = 2025) -> list[dict]:
    """港股五层选股主装配函数。

    顺序:
      1. 获取 HKEX 证券主表（普通股 + 每手股数）
      2. 获取港股实时行情快照
      3. 并行获取每只股票的财务数据
      4. 组装标准化记录（screeners/contracts 格式）
      5. 排除金融股
      6. 调用五层流水线打分（screeners/scoring）

    Args:
        year: 年报口径年份，默认 2025。

    Returns:
        list[dict]: 已打分、排序（评分降序）的标准化记录列表。
    """
    print(f"\n{'='*60}")
    print(f"  港股五层选股 — {year} 年报口径")
    print(f"{'='*60}\n")

    t_start = time.time()

    # ── 1. 证券主表 ─────────────────────────────────────────
    print("[1/4] 获取 HKEX 证券主表...")
    spot = None
    master_source = "HKEX"
    try:
        master = fetch_hkex_security_master()
        valid, msg = validate_hkex_master(master)
        if not valid:
            raise RuntimeError(f"HKEX security master validation failed: {msg}")
        print(f"  ✅ 证券主表校验通过 ({len(master)} 只)")
    except Exception as exc:
        print(f"  ⚠ HKEX 证券主表不可用: {exc}")
        print("  ⚠ 改用 Eastmoney 港股行情 universe；缺失每手股数/ISIN 的行会标记数据质量")
        print("\n[2/4] 获取港股实时行情...")
        spot = fetch_hk_spot_snapshot()
        print(f"  ✅ 行情快照: {len(spot)} 只")
        master = _master_from_quote_universe(spot)
        master_source = "Eastmoney quote universe"
        print(f"  ✅ 兜底证券主表: {len(master)} 只")
    t1 = time.time()

    # 建立 code → master 映射
    master_map = {r["code"]: r for r in master}

    # ── 2. 实时行情快照 ─────────────────────────────────────
    if spot is None:
        print("\n[2/4] 获取港股实时行情...")
        spot = fetch_hk_spot_snapshot()
        print(f"  ✅ 行情快照: {len(spot)} 只")
    t2 = time.time()

    # ── 3. 财务数据（并行） ─────────────────────────────────
    print(f"\n[3/4] 获取财务数据（并行, {FIN_WORKERS}线程）...")
    # 只对有行情且在主表中的股票拉财务
    all_codes_to_fetch = [
        code for code in spot
        if code in master_map
    ]

    def _cap_key(code):
        return _fnum((spot.get(code) or {}).get("market_cap")) or 0.0

    all_codes_to_fetch.sort(key=_cap_key, reverse=True)
    fetch_limit = _hk_fin_max_fetches()
    if fetch_limit < 0:
        codes_to_fetch = all_codes_to_fetch
    else:
        codes_to_fetch = all_codes_to_fetch[:fetch_limit]
    skipped_by_budget = max(0, len(all_codes_to_fetch) - len(codes_to_fetch))
    print(
        f"  待拉取财务数据: {len(codes_to_fetch)} 只 "
        f"(预算跳过 {skipped_by_budget} 只)"
    )

    financials_map: dict[str, list[dict]] = {}
    cashflow_map: dict[str, dict] = {}
    done = 0
    total = len(codes_to_fetch)

    def _fetch_one(code):
        """单只股票财务数据抓取（财报 + 现金流并行）。"""
        try:
            time.sleep(FIN_API_SLEEP)  # 限速
            fin = fetch_eastmoney_hk_financials(code)
            cf = fetch_eastmoney_hk_cashflow(code)
            return code, fin, cf
        except Exception as e:
            print(f"  ⚠️ [{code}] 财务数据抓取失败: {e}")
            return code, [], {}

    with ThreadPoolExecutor(max_workers=FIN_WORKERS) as ex:
        futures = {ex.submit(_fetch_one, code): code for code in codes_to_fetch}
        for future in as_completed(futures):
            try:
                code, fin, cf = future.result()
                if fin:
                    financials_map[code] = fin
                    cashflow_map[code] = cf
            except Exception as e:
                code = futures[future]
                print(f"  ⚠️ [{code}] 线程异常: {e}")
            done += 1
            if done % 100 == 0 or done == total:
                print(f"  财务数据进度: {done}/{total} ({done*100//total}%)")

    print(f"  ✅ 财务数据: 成功 {len(financials_map)} 只, 用时 {time.time()-t2:.1f}s")
    t3 = time.time()

    # ── 4. 组装标准化记录 ──────────────────────────────────
    print("\n[4/4] 组装标准化记录 + 打分...")
    records = []
    skipped_missing_master = 0
    skipped_financial = 0
    skipped_no_price = 0

    for code, sp in spot.items():
        # 必须在主表中
        master_entry = master_map.get(code)
        if not master_entry:
            skipped_missing_master += 1
            continue

        # 必须有有效价格
        price = sp.get("price")
        if price is None or price <= 0:
            skipped_no_price += 1
            continue

        # 获取基本属性
        name = sp.get("name") or master_entry.get("name", code)
        board_lot = master_entry.get("board_lot", 0)
        if not board_lot or board_lot <= 0:
            board_lot = 100  # 默认 100 股一手
        min_buy = price * board_lot

        # 行情字段
        pe_ttm = sp.get("pe_ttm")
        pe_dyn = sp.get("pe_dyn")
        pb = sp.get("pb")
        market_cap = sp.get("market_cap")

        # 财务数据
        fin_list = financials_map.get(code, [])
        target_fin = None  # 目标年度财务记录

        # 尝试获取目标年度财务数据
        for r in fin_list:
            rd = r.get("report_date", "")
            if rd.startswith(str(year)):
                target_fin = r
                break

        # 回退：目标年度无数据，尝试上一年
        fallback_year = year
        if target_fin is None and fin_list:
            for r in fin_list:
                rd = r.get("report_date", "")
                if rd.startswith(str(year - 1)):
                    target_fin = r
                    fallback_year = year - 1
                    break

        missing_financials = target_fin is None
        if missing_financials:
            skipped_financial += 1
            roe = None
            gross_margin = None
            net_margin = None
            debt_ratio = None
            net_profit = None
            report_currency = "HKD"
        else:
            # 提取财务指标
            roe = target_fin.get("roe")
            gross_margin = target_fin.get("gross_margin")
            net_margin = target_fin.get("net_margin")
            debt_ratio = target_fin.get("debt_ratio")
            net_profit = target_fin.get("net_profit")
            report_currency = target_fin.get("currency", "CNY")

        # 经营现金流：从批量预取结果读取
        cf_year_map = cashflow_map.get(code, {})
        operating_cashflow = cf_year_map.get(fallback_year) if cf_year_map else None

        # 行业分类：当前 API 未提供，默认 "港股"
        # 后续可通过东财行业分类 API 或港股 GICS 分类补充
        industry = "港股"

        # 增长率
        yoy, cagr = (None, None) if missing_financials else compute_growth(fin_list, fallback_year)

        # TTM 净利（估值用）
        ttm_netp = None
        if market_cap is not None and pe_ttm is not None and pe_ttm > 0:
            ttm_netp = market_cap / pe_ttm

        # OCF / 净利
        ocf_to_profit = None
        if operating_cashflow is not None and net_profit is not None and net_profit != 0:
            ocf_to_profit = operating_cashflow / net_profit

        # 数据质量标记
        data_quality_flag = ""
        if missing_financials:
            data_quality_flag = "missing_financials"
        elif master_source != "HKEX":
            data_quality_flag = "quote_universe_master"
        quote_currency = "HKD"
        report_currency_code = _currency_code(report_currency)
        if not missing_financials and report_currency_code not in ("HKD", "CNY", "RMB"):
            # 非 HKD/CNY 报表货币
            if not check_currency_match(quote_currency, report_currency_code):
                data_quality_flag = "currency_mismatch"

        # ── 金融股过滤 ──
        if _is_financial_stock(name, master_entry.get("category", "")):
            continue

        # ── 调用标准化记录工厂 ──
        rec = make_screening_record(
            market=MARKET_HK,
            code=code,
            display_code=code,  # 5-digit HK code, 可后续加 .HK 后缀
            name=name,
            industry=industry,
            exchange="HKEX",
            currency="HKD",
            price=price,
            min_buy=min_buy,
            pe_ttm=pe_ttm,
            pe_dyn=pe_dyn,
            pb=pb,
            market_cap=market_cap,
            market_cap_cny=None,  # 暂无 HKD→CNY 汇率转换
            roe=roe,
            gross_margin=gross_margin,
            net_margin=net_margin,
            yoy=yoy,
            cagr=cagr,
            ocf_to_profit=ocf_to_profit,
            debt_ratio=debt_ratio,
            goodwill_ratio=None,  # HK 股票商誉 N/A
            deduct_ratio=None,    # HK 股票扣非 N/A
            ttm_netp=ttm_netp,
            data_quality_flag=data_quality_flag,
        )
        records.append(rec)

    t4 = time.time()

    # ── 5. 五层流水线打分 ──────────────────────────────────
    records, total_eval, tier_counts = run_full_pipeline(
        records, config=DEFAULT_CONFIG, market=MARKET_HK
    )

    # 排序：评分降序
    records.sort(key=lambda r: r.get("score", 0), reverse=True)

    # ── 摘要 ───────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  装配统计:")
    print(f"    证券主表:          {len(master)} 只")
    print(f"    主表来源:          {master_source}")
    print(f"    行情快照:          {len(spot)} 只")
    print(f"    财务数据覆盖:      {len(financials_map)} 只")
    print(f"    跳过-不在主表:      {skipped_missing_master}")
    print(f"    跳过-无有效价格:    {skipped_no_price}")
    print(f"    跳过-无财务数据:    {skipped_financial}")
    print(f"    有效记录:          {len(records)} 只")
    print(f"    其中评估通过:      {total_eval} 只")
    print("  Tier 分布:")
    for tier in ("A_可买入", "B_优质待跌", "C_接近合格", "-"):
        cnt = tier_counts.get(tier, 0)
        emoji = {"A_可买入": "🟢", "B_优质待跌": "🟡", "C_接近合格": "⚪", "-": "⚫"}.get(tier, "")
        print(f"    {emoji} {tier}: {cnt}")
    print(f"{'─'*60}")

    total_elapsed = time.time() - t_start
    print(f"\n  ⏱️ 总用时: {total_elapsed:.1f}s")
    print(f"     步骤1(主表): {t1-t_start:.1f}s")
    print(f"     步骤2(行情): {t2-t1:.1f}s")
    print(f"     步骤3(财务): {t3-t2:.1f}s")
    print(f"     步骤4(装配+打分): {t4-t3:.1f}s")

    return records


# ── 输出函数 ───────────────────────────────────────────────

def _n(x, d=1):
    """格式化数值，None → 空字符串。"""
    return "" if x is None else f"{x:.{d}f}"


def _md_table_hk(rows: list[dict]) -> str:
    """生成港股 Markdown 表格。"""
    head = (
        "| 排名 | 代码 | 名称 | 现价 | 一手 | 行业 | 评分 | "
        "ROE% | 毛利% | 净利% | 同比% | CAGR% | "
        "PE(TTM) | PEG | 预期年化% | 折让% | 负债% | 市值(亿·HKD) |"
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


def write_hk_results(
    records: list[dict],
    output_dir: str | None = None,
    year: int = 2025,
) -> str:
    """输出港股五层筛选结果到 HTML / CSV / Markdown。

    输出文件:
      - results/hkstock_screen_{date}.html
      - results/hkstock_screen_{date}.csv
      - results/hkstock_shortlist_{date}.md
      - results/hkstock_screen.html  (稳定入口 → 最新日期版)

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

    # ── HTML ─────────────────────────────────────────────
    html_path = os.path.join(out_dir, f"hkstock_screen_{ts}.html")
    _write_hk_html(records, html_path, year, total_eval, (len(tier_a), len(tier_b), len(tier_c)))

    # ── CSV ──────────────────────────────────────────────
    csv_path = os.path.join(out_dir, f"hkstock_screen_{ts}.csv")
    _write_hk_csv(records, csv_path)

    # ── Markdown ─────────────────────────────────────────
    md_path = os.path.join(out_dir, f"hkstock_shortlist_{ts}.md")
    _write_hk_md(tier_a, tier_b, tier_c, md_path, year, total_eval)

    # ── 稳定入口 ─────────────────────────────────────────
    _write_hk_stable_entry(out_dir, ts)

    print(f"\n✅ 输出完成 ({year} 年报):")
    print(f"   HTML: {html_path}")
    print(f"   CSV:  {csv_path}")
    print(f"   MD:   {md_path}")
    print(f"   固定入口: {os.path.join(out_dir, HK_STABLE_SCREEN_NAME)}")

    return ts


def _write_hk_csv(records: list[dict], path: str):
    """写港股 CSV（含 market 列）。"""
    cols = [
        "rank", "tier", "score", "market", "code", "name", "price", "min_buy",
        "industry", "deepest_layer",
        "roe", "gross_margin", "net_margin", "yoy", "cagr",
        "ocf_to_profit", "debt_ratio",
        "pe_ttm", "pe_dyn", "peg", "eyield", "exp_ret", "discount", "pb",
        "market_cap_yi_hkd", "fail_reasons",
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
                r.get("market", "hk"),
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
                _n(r.get("pe_dyn"), 2),
                _n(r.get("peg"), 2),
                _n(r.get("eyield"), 2),
                _n(r.get("exp_ret")),
                _n((r.get("discount") or 0) * 100, 1),
                _n(r.get("pb"), 2),
                _n((r.get("market_cap") or 0) / 1e8, 1),
                "; ".join(r.get("fails", [])),
            ])


def _write_hk_md(
    tier_a: list[dict],
    tier_b: list[dict],
    tier_c: list[dict],
    path: str,
    year: int,
    total_eval: int,
):
    """写港股 Markdown 短名单。"""
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"# 港股五层选股结果（{year}年报 · 生成于 {ts}）\n",
        f"全市场参与筛选 **{total_eval}** 只港股。按评分（满分100）从高到低排序。\n",
        "> 数据来源：东方财富公开接口 + HKEX 证券主表。第0-3层为量化筛选，"
        "**第4层定性（护城河/商业模式/能力圈/管理层/行业景气）需人工把关**。\n",
        f"## 🟢 Tier A · 可买入（五层全过，估值已到买点）— {len(tier_a)} 只\n",
        "通过排雷+质量+估值，且当前市值已打到合理价 7 折以内、预期年化≥10%。\n",
        _md_table_hk(tier_a) if tier_a else (
            "_本期无标的同时满足「优质 + 打7折」。"
            "这很正常——好公司很少便宜。见下方 Tier B 候选池。_"
        ),
        f"\n\n## 🟡 Tier B · 优质待跌（质量确认，估值/买点未到）— {len(tier_b)} 只\n",
        "排雷与质量层全部过关的真·好生意，只是现在不够便宜。**加自选，等回调到买点**。\n",
        _md_table_hk(tier_b[:60]),
        f"\n\n## ⚪ Tier C · 接近合格（排雷过关，质量仅差一项）— {len(tier_c)} 只\n",
        "仅供观察，差一口气，可留意基本面是否改善。\n",
        _md_table_hk(tier_c[:40]),
        "\n\n---\n### 字段说明\n",
        "- **现金流/利润**：经营现金流 ÷ 净利润，>=0.8 说明利润质量较稳\n",
        "- **折让%**：(合理市值−当前市值)/合理市值，正数=低于合理价；≥30% 才算到买点\n",
        "- **预期年化**：盈利收益率(1/PE) + 增长率\n",
        "- **PEG**：PE ÷ 增长率（同比与3年CAGR取小，偏保守）\n",
        "- 评分权重：质量55（ROE/毛利/净利/增速/现金流/动能）+ 估值安全45（盈利收益率/PEG/折让/行业相对PE）\n",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _write_hk_stable_entry(out_dir: str, ts: str):
    """写稳定入口 HTML（重定向到最新日期页）。"""
    latest_name = f"hkstock_screen_{ts}.html"
    target_js = json.dumps(latest_name, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={latest_name}">
<title>港股五层选股固定入口</title>
<style>
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#172033}}
.box{{max-width:560px;margin:14vh auto;padding:24px;background:#fff;border:1px solid #dbe4f0;border-radius:8px}}
a{{color:#2563eb}}
</style></head><body>
<div class="box">
<h1>港股五层选股固定入口</h1>
<p>正在打开最新总表：<a href="{latest_name}">{latest_name}</a></p>
<p>日期页继续作为后台历史产物保留；日常请访问 <code>hkstock_screen.html</code>。</p>
</div>
<script>location.replace({target_js})</script>
</body></html>"""
    for name in (HK_STABLE_SCREEN_NAME,):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(html)


# ── HTML 模板（港股专用） ──────────────────────────────────

HK_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>港股五层选股结果</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:light;--bg:#f6f8fb;--text:#172033;--heading:#0f172a;--muted:#64748b;--surface:#fff;--surface-soft:#f8fafc;--band:#eef4f8;--header-a:#ffffff;--header-b:#eef4ff;--border:#dbe4f0;--border-strong:#cbd5e1;--head:#eef2f7;--link:#2563eb;--green:#16a34a;--yellow:#b45309;--red:#dc2626;--hover:#f8fbff;--warn:#fff7ed;--warn-hover:#ffedd5;--tag:#f1f5f9;--toast-bg:#ecfdf5;--toast-border:#86efac;--shadow:0 1px 2px rgba(15,23,42,.04)}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0f1115;--text:#e6e8eb;--heading:#f8fafc;--muted:#9aa4b2;--surface:#131820;--surface-soft:#1a1f29;--band:#0d1117;--header-a:#161a22;--header-b:#0f1115;--border:#232936;--border-strong:#2a3140;--head:#1a1f29;--link:#7fb3ff;--green:#3ddc84;--yellow:#ffd166;--red:#ff6b6b;--hover:#161b24;--warn:#1f1a12;--warn-hover:#26200f;--tag:#1c1f26;--toast-bg:#1a2a1a;--toast-border:#3ddc84;--shadow:none}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:13px}
header{padding:16px 20px;background:linear-gradient(135deg,var(--header-a),var(--header-b));border-bottom:1px solid var(--border)}
.head-main{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{margin:0 0 4px;font-size:18px}
.sub{color:var(--muted);font-size:12px}
/* Dashboard */
.dash{padding:16px 20px;background:var(--band);border-bottom:1px solid var(--border);display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:stretch}
.dash-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;overflow:hidden;box-shadow:var(--shadow)}
.dash-card h3{margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:600}
.dash-card canvas{width:100%;height:210px}
.kpi-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.kpi{background:var(--surface-soft);border:1px solid var(--border);border-radius:8px;padding:8px 14px;text-align:center;min-width:80px;flex:1}
.kpi .val{font-size:22px;font-weight:700;line-height:1.2}
.kpi .lbl{font-size:10px;color:var(--muted);margin-top:2px}
.kpi.A .val{color:var(--green)}.kpi.B .val{color:var(--yellow)}.kpi.C .val{color:var(--muted)}.kpi.X .val{color:var(--muted)}
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
.detail-link{appearance:none;background:none;border:0;padding:0;margin:0;color:var(--link);font:inherit;cursor:pointer;text-align:left}
.detail-link:hover{text-decoration:underline}.detail-link.code{font-variant-numeric:tabular-nums}.detail-link.name-link{color:var(--text)}
.note{color:var(--yellow);font-size:11px;text-align:left;max-width:320px;white-space:normal}
.cnt{color:var(--muted);font-size:12px;margin-left:auto}
.hint{color:var(--muted);font-size:12px;white-space:nowrap}
.detail-panel{position:fixed;inset:0;z-index:80;background:var(--bg);display:none;overflow:auto}
.detail-panel.open{display:block}
.detail-head{position:sticky;top:0;z-index:2;background:var(--surface);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:14px;box-shadow:var(--shadow)}
.detail-title h2{margin:0 0 4px;font-size:18px;color:var(--heading)}.detail-title h2 span{font-size:13px;color:var(--muted);font-weight:600;margin-left:8px}
.detail-sub{display:flex;gap:10px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:12px}
.detail-body{padding:16px 20px 28px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.detail-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;box-shadow:var(--shadow)}
.detail-card.full{grid-column:1/-1}.detail-card h3{margin:0 0 10px;font-size:13px;color:var(--muted)}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.detail-metric{border:1px solid var(--border);background:var(--surface-soft);border-radius:7px;padding:9px 10px;min-height:58px}
.detail-metric span{display:block;color:var(--muted);font-size:11px;margin-bottom:4px}.detail-metric b{font-size:18px;color:var(--heading);word-break:break-word}
.detail-note{white-space:normal;line-height:1.6;color:var(--yellow);font-size:13px}
footer{padding:10px 20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border)}
@media(max-width:720px){.dash{grid-template-columns:1fr;padding:12px}.controls{align-items:stretch}.controls .btn,.controls select,.controls .chk{flex:1 1 auto}.controls input#q{max-width:none;flex-basis:100%}.cnt{flex-basis:100%;margin-left:0}.head-main{align-items:flex-start}.theme-btn{margin-left:0}}
</style></head><body>
<header>
<div class="head-main">
<div><h1>港股「五层选股流水线」结果</h1>
<div class="sub" id="sub"></div></div>
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
<div class="detail-panel" id="detailPanel" aria-hidden="true">
<div class="detail-head">
<button class="btn" id="detailBack">← 返回总表</button>
<div class="detail-title"><h2 id="detailTitle"></h2><div class="detail-sub" id="detailSub"></div></div>
</div>
<div class="detail-body" id="detailBody"></div>
</div>
<div class="toast" id="toast"></div>
<div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<footer>港股数据来源：东方财富公开接口 + HKEX 证券主表 · 第0层排雷→第1层质量→第2层估值→第3层安全边际 · 第4层定性需人工把关 · 市场: 港股</footer>
<script>
var DATA=__DATA__, INDS=__INDS__, META=__META__;
var COLS=[["rk","#","n"],["tier","档","s"],["code","代码","s"],["name","名称","s"],
["px","现价","n"],["mb","一手","n"],["sc","评分","n"],["L","层","n"],["roe","ROE%","n"],["gm","毛利%","n"],
["nm","净利%","n"],["yoy","同比%","n"],["cagr","CAGR%","n"],["pe","PE","n"],["peg","PEG","n"],
["er","预期年化%","n"],["disc","折让%","n"],["debt","负债%","n"],["cap","市值亿·HKD","n"],["ind","行业","s"],["note","落选原因","s"]];
var state={t:"all",q:"",ind:"",pass:false,sk:"sc",sd:-1};
function fmt(v){return v===null||v===undefined?"":v}
function esc(v){return String(fmt(v)).replace(/[&<>"']/g,function(ch){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]})}
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
document.getElementById("sub").innerHTML=META.year+"年报口径 · 市场: 港股 · 生成于 "+META.ts+" · 全市场评估 "+META.total+" 只";

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
function detailValue(v,suffix){return v===null||v===undefined||v===""?"-":esc(v)+(suffix||"")}
function detailMetric(label,value,suffix){return '<div class="detail-metric"><span>'+esc(label)+'</span><b>'+detailValue(value,suffix)+'</b></div>'}
function findRecord(code){return DATA.find(function(r){return String(r.code)===String(code)})}
function openDetail(code,push){
 var r=findRecord(code);if(!r)return;
 document.getElementById("detailTitle").innerHTML=esc(r.code)+' <span>'+esc(r.name)+'</span>';
 document.getElementById("detailSub").innerHTML=tierBadge(r.tier)+'<span>评分 '+detailValue(r.sc)+'</span><span>第 '+detailValue(r.L)+' 层</span><span>'+esc(r.ind)+'</span>';
 var body='';
 body+='<section class="detail-card"><h3>交易与评级</h3><div class="detail-grid">'
   +detailMetric("现价",r.px)+detailMetric("一手",r.mb)+detailMetric("评分",r.sc)+detailMetric("市值(亿 HKD)",r.cap)+'</div></section>';
 body+='<section class="detail-card"><h3>质量指标</h3><div class="detail-grid">'
   +detailMetric("ROE",r.roe,"%")+detailMetric("毛利率",r.gm,"%")+detailMetric("净利率",r.nm,"%")+detailMetric("负债率",r.debt,"%")+'</div></section>';
 body+='<section class="detail-card"><h3>成长与估值</h3><div class="detail-grid">'
   +detailMetric("同比",r.yoy,"%")+detailMetric("CAGR",r.cagr,"%")+detailMetric("PE",r.pe)+detailMetric("PEG",r.peg)+detailMetric("预期年化",r.er,"%")+detailMetric("折让",r.disc,"%")+'</div></section>';
 body+='<section class="detail-card full"><h3>落选原因/风险</h3><div class="detail-note">'+(r.note?esc(r.note):"无")+'</div></section>';
 document.getElementById("detailBody").innerHTML=body;
 document.getElementById("detailPanel").classList.add("open");
 document.getElementById("detailPanel").setAttribute("aria-hidden","false");
 if(push)location.hash="code="+encodeURIComponent(r.code);
}
function closeDetail(push){
 document.getElementById("detailPanel").classList.remove("open");
 document.getElementById("detailPanel").setAttribute("aria-hidden","true");
 if(push&&location.hash.indexOf("code=")>=0)history.pushState("",document.title,location.pathname+location.search);
}
function syncDetailFromHash(){
 var m=location.hash.match(/(?:^#|&)code=([^&]+)/);
 if(m)openDetail(decodeURIComponent(m[1]),false);else closeDetail(false);
}
function rowHTML(r){
 var cells=COLS.map(function(c){var k=c[0],v=r[k];
  if(k==="tier")return '<td>'+tierBadge(v)+'</td>';
  if(k==="code")return '<td class="l"><button class="detail-link code" data-code="'+esc(r.code)+'">'+esc(v)+'</button></td>';
  if(k==="name")return '<td class="l"><button class="detail-link name-link" data-code="'+esc(r.code)+'">'+esc(v)+'</button></td>';
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
document.getElementById("body").addEventListener("click",function(e){var el=e.target.closest("[data-code]");if(el)openDetail(el.getAttribute("data-code"),true)});
document.getElementById("detailBack").addEventListener("click",function(){closeDetail(true)});
window.addEventListener("hashchange", syncDetailFromHash);
head();render();syncDetailFromHash();
</script></body></html>"""


def _write_hk_html(
    records: list[dict],
    path: str,
    year: int,
    total_eval: int,
    tier_counts: tuple[int, int, int],
):
    """生成港股自包含交互式 HTML 页面。"""
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
        if s < 20: score_bins["0-20"] += 1
        elif s < 30: score_bins["20-30"] += 1
        elif s < 40: score_bins["30-40"] += 1
        elif s < 50: score_bins["40-50"] += 1
        elif s < 60: score_bins["50-60"] += 1
        elif s < 70: score_bins["60-70"] += 1
        elif s < 80: score_bins["70-80"] += 1
        else: score_bins["80-100"] += 1

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
    }

    html = HK_HTML_TEMPLATE.replace(
        "__DATA__", json.dumps(data, ensure_ascii=False)
    ).replace(
        "__INDS__", json.dumps(inds, ensure_ascii=False)
    ).replace(
        "__META__", json.dumps(meta, ensure_ascii=False)
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ── 测试/便利函数 ──────────────────────────────────────────

def test_hk_screener(year: int = 2025) -> tuple[list[dict], dict[str, int]]:
    """便捷测试函数：运行 build_hk_records 并打印摘要。

    Args:
        year: 年报口径年份。

    Returns:
        (records, tier_counts): 标准化记录列表 和 Tier 分布。
    """
    records = build_hk_records(year)

    tier_counts = dict(Counter(r.get("tier", "-") for r in records))

    print(f"\n{'='*60}")
    print(f"  港股五层选股 — {year}年报 测试摘要")
    print(f"{'='*60}")
    print(f"  总记录数:       {len(records)}")
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
    """命令行入口：python3 screeners/hk.py [--year 2025]"""
    import argparse

    ap = argparse.ArgumentParser(description="港股五层选股流水线")
    ap.add_argument(
        "--year", type=int, default=2025, help="年报口径年份 (默认 2025)"
    )
    ap.add_argument(
        "--no-html", action="store_true", help="跳过 HTML 输出"
    )
    ap.add_argument(
        "--output-dir", type=str, default=None, help="输出目录 (默认 results/)"
    )
    args = ap.parse_args()

    records = test_hk_screener(year=args.year)[0]

    if not args.no_html and records:
        write_hk_results(records, output_dir=args.output_dir, year=args.year)
    elif not records:
        print("\n⚠️ 未产出记录，请检查数据源或网络连接。")


if __name__ == "__main__":
    main()
