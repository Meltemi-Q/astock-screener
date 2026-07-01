"""
Market-agnostic five-layer scoring engine.

Takes normalized screening records (per screeners/contracts.py) and
applies the five-layer pass/fail gating + 100-point weighted scoring.

This is a refactored, market-independent version of the evaluate()
and score() functions from astock_screener.py.
"""

from __future__ import annotations
from collections import Counter

from screeners.contracts import check_tier_ab_eligibility, REQUIRED_FOR_TIER_AB


def scale(v, lo, hi):
    """Linear scale: maps minimum input to 0, maximum to 100."""
    if v is None or hi <= lo:
        return 50.0
    if v >= hi:
        return 100.0
    if v <= lo:
        return 0.0
    return (v - lo) / (hi - lo) * 100.0


def median(vals):
    """Compute median of a list of numeric values, ignoring None."""
    clean = sorted(v for v in vals if v is not None)
    if not clean:
        return None
    n = len(clean)
    if n % 2:
        return clean[n // 2]
    return (clean[n // 2 - 1] + clean[n // 2]) / 2.0


def industry_median_pe(records, market=None):
    """Compute per-industry median PE(TTM).
    
    If market is specified, only consider records from that market.
    """
    ind_pe = {}
    for r in records:
        if market and r.get("market") != market:
            continue
        pe = r.get("pe_ttm")
        ind = r.get("industry", "")
        if pe is not None and pe > 0 and ind:
            ind_pe.setdefault(ind, []).append(pe)
    return {ind: median(vals) for ind, vals in ind_pe.items()}


def evaluate(record, ind_pe, config=None):
    """Five-layer pass/fail gating.
    
    Args:
        record: normalized screening record
        ind_pe: per-industry median PE dict
        config: optional dict overriding default thresholds
    
    Returns:
        (deepest_layer, tier, fails)
    """
    if config is None:
        config = DEFAULT_CONFIG

    C = config
    market = record.get("market", "cn")
    fails = []

    # ── Layer 0: Mine-sweeping ──
    # 口径与 astock_screener 一致：关键排雷数据缺失一律 fail-closed(视为不通过)，不静默放行
    l0 = []
    # ST / delisting check (CN only)
    if market == "cn":
        name = record.get("name", "")
        if name and ("ST" in name or "*ST" in name):
            l0.append("ST")
    # Loss-making：优先用真实归母净利判亏损（与 astock 内联引擎一致），
    # 缺失时才回退 ttm_netp(=市值/PE) 代理，避免仅缺 PE 的盈利股被误杀；两者皆缺 fail-closed
    npr = record.get("net_profit")
    if npr is None:
        npr = record.get("ttm_netp")
    if npr is None or npr <= 0:
        l0.append("亏损")
    # Debt too high：缺失也 fail-closed
    dr = record.get("debt_ratio")
    if dr is None or dr >= C["max_debt_ratio"]:
        l0.append(f"负债率≥{C['max_debt_ratio']}%")
    # Goodwill risk (CN only, skip for HK/US)；有商誉字段才判，缺失视为无商誉=0（CN 走商誉表默认 0）
    if market == "cn":
        gw = record.get("goodwill_ratio")
        if gw is not None and gw >= C["max_goodwill_ratio"]:
            l0.append(f"商誉≥净资产{C['max_goodwill_ratio']}%")
    # Cashflow quality：缺失也 fail-closed
    ocf = record.get("ocf_to_profit")
    if ocf is None or ocf < C["min_ocf_to_profit"]:
        l0.append(f"OCF/净利<{C['min_ocf_to_profit']}")
    fails += l0

    # ── Layer 1: Quality ──
    l1 = []
    if record.get("roe") is None or record.get("roe", 0) < C["min_roe"]:
        l1.append(f"ROE<{C['min_roe']}%")
    if record.get("gross_margin") is None or record.get("gross_margin", 0) < C["min_gross_margin"]:
        l1.append(f"毛利<{C['min_gross_margin']}%")
    if record.get("net_margin") is None or record.get("net_margin", 0) < C["min_net_margin"]:
        l1.append(f"净利率<{C['min_net_margin']}%")
    if record.get("yoy") is None or record.get("yoy", 0) < C["min_growth"]:
        l1.append(f"同比<{C['min_growth']}%")
    if record.get("cagr") is None or record.get("cagr", 0) < C["min_growth"]:
        l1.append(f"CAGR<{C['min_growth']}%")
    fails += l1

    # ── Layer 2: Valuation ──
    l2 = []
    if record.get("peg") is None or record.get("peg", float("inf")) >= C["max_peg"]:
        l2.append(f"PEG≥{C['max_peg']}")
    if record.get("eyield") is None or record.get("eyield", 0) <= C["min_earnings_yield"]:
        l2.append(f"盈收率≤{C['min_earnings_yield']}%")
    med = ind_pe.get(record.get("industry"))
    pe_ttm = record.get("pe_ttm")
    pe_le_peer = (pe_ttm is not None and pe_ttm > 0 and med is not None and pe_ttm <= med)
    if not pe_le_peer:
        l2.append("PE高于行业中位")
    fails += l2

    # ── Layer 3: Safety margin ──
    l3 = []
    if record.get("exp_ret") is None or record.get("exp_ret", 0) < C["min_expected_return"]:
        l3.append(f"预期年化<{C['min_expected_return']}%")
    # discount 为正值口径(1-市值/合理市值)：买点需 discount ≥ 1-margin_of_safety(=0.3)，缺失 fail-closed
    disc = record.get("discount")
    if disc is None or disc < (1.0 - C["margin_of_safety"]):
        l3.append(f"安全边际不足{int((1.0 - C['margin_of_safety']) * 100)}%(市值>合理市值×{C['margin_of_safety']})")
    fails += l3

    # Determine deepest layer passed
    deepest = 0
    if not l0:
        deepest = 1
    if not l0 and not l1:
        deepest = 2
    if not l0 and not l1 and not l2:
        deepest = 3
    if not l0 and not l1 and not l2 and not l3:
        deepest = 4

    # Tier assignment
    if deepest >= 4:
        tier = "A_可买入"
    elif not l0 and not l1:
        tier = "B_优质待跌"
    elif not l0 and len(l1) == 1:
        tier = "C_接近合格"
    else:
        tier = "-"

    return deepest, tier, fails


def score(record, ind_pe, config=None):
    """Weighted 100-point scoring for a normalized record.
    
    Returns a float 0-100.
    """
    if config is None:
        config = DEFAULT_CONFIG

    W = config["weights"]
    yoy = record.get("yoy")
    cagr = record.get("cagr")

    # Growth momentum
    if yoy is not None and cagr is not None:
        if cagr and cagr != 0:
            momentum = 100.0 if yoy >= cagr else scale(yoy / cagr, 0.5, 1.0)
        else:
            momentum = 50.0
    elif yoy is not None:
        momentum = scale(yoy, 5, 25)
    else:
        momentum = 25.0

    # Quality sub-score
    roe_s = scale(record.get("roe"), 10, 30) if record.get("roe") is not None else 20
    gm_s = scale(record.get("gross_margin"), 20, 60) if record.get("gross_margin") is not None else 20
    nm_s = scale(record.get("net_margin"), 5, 25) if record.get("net_margin") is not None else 20

    quality = (W["roe"] * roe_s + W["gm"] * gm_s + W["nm"] * nm_s +
               W["growth"] * momentum) / (W["roe"] + W["gm"] + W["nm"] + W["growth"])

    # Valuation & safety sub-score
    pe_ttm = record.get("pe_ttm")
    med = ind_pe.get(record.get("industry"))

    if pe_ttm is not None and pe_ttm > 0 and med is not None and med > 0:
        pe_s = scale(med / pe_ttm, 0.5, 1.2)
    else:
        pe_s = 50.0

    ocf_s = scale(record.get("ocf_to_profit"), 0.5, 1.5) if record.get("ocf_to_profit") is not None else 30
    debt_s = scale(100 - (record.get("debt_ratio") or 50), 30, 70) if record.get("debt_ratio") is not None else 30
    peg_s = scale(record.get("peg"), 2.0, 0.5) if record.get("peg") is not None and record.get("peg") > 0 else 25
    exp_s = scale(record.get("exp_ret"), 5, 20) if record.get("exp_ret") is not None else 25

    val_safety = (W["pe_vs_peer"] * pe_s + W["ocf"] * ocf_s +
                  W["debt"] * debt_s + W["peg"] * peg_s +
                  W["exp_ret"] * exp_s) / (W["pe_vs_peer"] + W["ocf"] + W["debt"] +
                                            W["peg"] + W["exp_ret"])

    total = W["quality_weight"] * quality + (1.0 - W["quality_weight"]) * val_safety
    return round(total, 2)


def resolve_config(config, market):
    """按 market 覆盖阈值：返回合并后的 config（浅拷贝 + 覆盖项）。

    A股(cn)沿用现有阈值不变；港美股(hk/us)可在 MARKET_THRESHOLD_OVERRIDES 里挂钩后续调参，
    留 hook 但当前默认空覆盖，保持既有行为。weights 也支持整体覆盖。
    """
    if config is None:
        config = DEFAULT_CONFIG
    overrides = MARKET_THRESHOLD_OVERRIDES.get(market) if market else None
    if not overrides:
        return config
    merged = dict(config)
    for k, v in overrides.items():
        if k == "weights" and isinstance(v, dict):
            w = dict(config.get("weights", {}))
            w.update(v)
            merged["weights"] = w
        else:
            merged[k] = v
    return merged


def run_full_pipeline(records, config=None, market=None):
    """Run evaluate + score on all records, mutating them in place.

    Returns:
        (records, total_eval, tier_counts)
    """
    config = resolve_config(config, market)  # 按 market 覆盖阈值（A股无覆盖，保持原值）
    ind_pe = industry_median_pe(records, market=market)

    total_eval = 0
    for r in records:
        r["deepest"], r["tier"], r["fails"] = evaluate(r, ind_pe, config)
        r["score"] = score(r, ind_pe, config)

        # 死代码护栏落地：进入 Tier A/B 前校验关键排雷/行情数据齐全，缺失则阻止升档并记 data_quality_flag
        if r["tier"] in ("A_可买入", "B_优质待跌"):
            eligible, missing = check_tier_ab_eligibility(r)
            # 补充关键排雷数据(对应 ocf/debt/assets/liab 的归一化产物)——缺失同样 fail-closed
            for f in ("ocf_to_profit", "debt_ratio"):
                if r.get(f) is None:
                    missing.append(f)
                    eligible = False
            if not eligible:
                r["data_quality_flag"] = "排雷数据不完整:" + ",".join(missing)
                r["fails"] = list(r.get("fails") or []) + ["数据不足(阻止进A/B)"]
                r["tier"] = "-"
                if r.get("deepest", 0) >= 1:
                    r["deepest"] = 0

        if r.get("deepest", 0) >= 1:
            total_eval += 1

    tier_counts = Counter(r["tier"] for r in records)
    return records, total_eval, tier_counts


# ── Default Configuration ──
DEFAULT_CONFIG = {
    # Layer 0: Mine-sweeping
    "min_ocf_to_profit": 0.8,
    "max_debt_ratio": 70.0,
    "max_goodwill_ratio": 30.0,  # 百分数口径(商誉/净资产≥30% 排雷)，与 astock_screener 一致
    # Layer 1: Quality
    "min_roe": 15.0,
    "min_gross_margin": 30.0,
    "min_net_margin": 10.0,
    "min_growth": 10.0,
    # Layer 2: Valuation
    "max_peg": 1.0,
    "min_earnings_yield": 5.0,
    "max_pe_absolute": 80.0,
    # Layer 3: Safety margin
    "min_expected_return": 10.0,
    # margin_of_safety=0.7：当前市值 ≤ 合理市值×0.7 才买 ⇔ discount ≥ 1-0.7 = 0.3（与 astock_screener 一致）
    "margin_of_safety": 0.70,
    # Scoring weights
    "weights": {
        "quality_weight": 0.55,
        "roe": 1.5, "gm": 1.0, "nm": 1.0, "growth": 1.5,
        "pe_vs_peer": 1.5, "ocf": 1.0, "debt": 0.5,
        "peg": 1.0, "exp_ret": 1.0,
    },
}


# ── Per-market 阈值覆盖 hook ──
# 键为 market ("cn"/"hk"/"us")，值为要覆盖的阈值子集（可含 "weights" 子字典）。
# 现阶段全部留空（沿用 DEFAULT_CONFIG，A股阈值数值不变），供港美股后续按市场特性单独调参。
# 示例：MARKET_THRESHOLD_OVERRIDES["hk"] = {"min_roe": 12.0}
MARKET_THRESHOLD_OVERRIDES = {
    "cn": {},
    "hk": {},
    "us": {},
}
