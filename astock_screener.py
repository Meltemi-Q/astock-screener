#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股「五层选股流水线」自动化筛选器
================================================
顺序不可颠倒：第0层排雷 → 第1层质量 → 第2层估值 → 第3层毛估估+安全边际 →（第4层定性，人工把关）

数据源：东方财富公开数据接口（datacenter-web / push2delay），纯 Python 标准库，零第三方依赖。
  - 业绩报表  RPT_LICO_FN_CPD        → ROE/毛利率/净利/营收/净利同比/EPS/每股经营现金流/BPS
  - 资产负债表 RPT_DMSK_FN_BALANCE    → 资产负债率/净资产/行业
  - 商誉明细  RPT_GOODWILL_STOCKDETAILS → 商誉/净资产占比
  - 实时行情  push2delay clist        → PE(TTM)/PE(动)/PB/总市值/总股本/股价
  - 历史净利  RPT_LICO_FN_CPD（往年年报）→ 计算 3 年净利 CAGR

用法：
  python3 astock_screener.py            # 跑全市场（带缓存，重复跑很快）
  python3 astock_screener.py --fresh    # 忽略缓存，强制重新抓取
  python3 astock_screener.py --year 2025 --top 50
"""

import os, json, time, csv, ssl, argparse, hashlib
from urllib import request, parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 一、配置区（所有阈值/权重都在这里，方便调参）
# ============================================================
CONFIG = {
    "report_year": 2025,        # 主报告期（用最近一期年报）；脚本会自动回退到上一年若数据不足
    "cagr_years": 3,            # 复合增长率回看年数

    # —— 第0层 排雷 ——
    "min_ocf_to_profit": 0.8,   # 经营现金流 ÷ 净利润 ≥ 0.8（利润是真钱）
    "max_debt_ratio":    70.0,  # 资产负债率 < 70%
    "max_goodwill_ratio":30.0,  # 商誉 < 净资产 30%

    # —— 第1层 质量 ——
    "min_roe":          15.0,   # ROE ≥ 15%
    "min_gross_margin": 30.0,   # 毛利率 ≥ 30%
    "min_net_margin":   10.0,   # 净利率 ≥ 10%
    "min_growth":       10.0,   # 净利增速 ≥ 10%（同比 与 CAGR 都要≥）

    # —— 第2层 估值 ——
    "max_peg":          1.0,    # PEG < 1
    "min_earnings_yield":5.0,   # 盈利收益率 1/PE > 5%  ⇔ PE < 20

    # —— 第3层 毛估估 + 安全边际 ——
    "min_expected_return":10.0, # 预期年化 = 1/PE + 增长率 ≥ 10%
    "reasonable_pe_floor":12.0, # 合理PE下限
    "reasonable_pe_cap":  30.0, # 合理PE上限（合理PE = 增长率，限制在[下限,上限]）
    "margin_of_safety":   0.7,  # 当前市值 ≤ 合理市值 × 0.7（打7折才买）

    # —— 评分权重（满分100，质量55 + 估值/安全45）——
    "weights": {
        "roe": 12, "gross": 8, "net": 8, "growth": 12, "ocf": 8, "momentum": 7,   # 质量 55
        "eyield": 10, "peg": 10, "discount": 15, "pe_ind": 10,                     # 估值/安全 45
    },

    # —— 网络/缓存 ——
    "cache_hours": 6,           # 缓存有效期（小时）；财报是静态数据，重复跑直接读缓存
    "page_sleep": 0.12,         # 翻页间隔（礼貌延时，避免被限流）
    "timeout": 25,
    "retries": 4,
}

# A股普通股证券类型码（排除新三板/B股等）
A_SHARE_TYPE = "058001001"

# 东财行情接口按板块分开抓（push2 单查询会把每页截断到100行，分板块更稳）
SPOT_BOARDS = [
    ("m:0+t:6", "深主板"), ("m:0+t:80", "创业板"),
    ("m:1+t:2", "沪主板"), ("m:1+t:23", "科创板"),
    ("m:0+t:81+s:2048", "北交所"),
]
SPOT_FIELDS = "f12,f13,f14,f2,f9,f23,f20,f21,f38,f115"

WORKDIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(WORKDIR, "cache")
OUT_DIR   = os.path.join(WORKDIR, "results")
SSL_CTX   = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE  # 公开只读行情数据，免证书校验以规避 macOS 自带 python 证书问题

USE_CACHE = True  # 由命令行 --fresh 控制

# ============================================================
# 二、网络层（urllib + 重试 + 本地缓存）
# ============================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "identity",
}


def _http_get(url):
    last = None
    for i in range(CONFIG["retries"]):
        try:
            req = request.Request(url, headers=HEADERS)
            with request.urlopen(req, timeout=CONFIG["timeout"], context=SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * (i + 1))
    raise last



def _cache_uid(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()

def get_json(url, ttl_hours=None):
    """带本地缓存的 JSON GET。缓存键为 url 的 md5。"""
    ttl = (CONFIG["cache_hours"] if ttl_hours is None else ttl_hours) * 3600
    uid = _cache_uid(url)
    fp = os.path.join(CACHE_DIR, uid + ".json")
    if USE_CACHE and os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < ttl:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    raw = _http_get(url)
    d = json.loads(raw)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass
    return d


def fetch_datacenter(report_name, columns, filt, label):
    """东财 datacenter-web 通用分页抓取（pageSize 上限 500）。"""
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    out, page, pages = [], 1, 1
    while page <= pages:
        params = {
            "reportName": report_name, "columns": columns,
            "pageSize": 500, "pageNumber": page, "filter": filt,
            "sortColumns": "SECURITY_CODE", "sortTypes": 1,
            "source": "WEB", "client": "WEB",
        }
        url = base + "?" + parse.urlencode(params, quote_via=parse.quote)
        d = get_json(url)
        res = d.get("result") or {}
        if page == 1:
            pages = res.get("pages") or 1
            print(f"  [{label}] 记录数={res.get('count')} 页数={pages}")
        out.extend(res.get("data") or [])
        page += 1
        time.sleep(CONFIG["page_sleep"])
    return out


def fetch_spot():
    """push2delay 延时行情，按板块逐一分页抓取（pz=100 为服务器实际上限）。返回 {code: row}。"""
    host = "https://push2delay.eastmoney.com/api/qt/clist/get"
    pz = 100
    out = {}
    for fs, bname in SPOT_BOARDS:
        pn, total = 1, None
        while True:
            url = (f"{host}?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f12"
                   f"&fs={fs}&fields={SPOT_FIELDS}")
            data = (get_json(url, ttl_hours=CONFIG["cache_hours"]).get("data")) or {}
            if total is None:
                total = data.get("total") or 0
            diff = data.get("diff") or []
            # 空页重试一次，避免偶发限流导致漏数据
            if not diff and len(out) < 999999 and pn * pz < total:
                time.sleep(0.5)
                data = (get_json(url + "&_r=1", ttl_hours=0).get("data")) or {}
                diff = data.get("diff") or []
            for r in diff:
                out[str(r.get("f12"))] = r
            if pn * pz >= total or not diff:
                break
            pn += 1
            time.sleep(CONFIG["page_sleep"])
        print(f"  [行情·{bname}] {total} 只")
    print(f"  [行情] 合计 {len(out)} 只")
    return out


def fetch_spot_parallel():
    """并行抓取 5 大板块行情，大幅提速。返回 {code: row}。"""
    host = "https://push2delay.eastmoney.com/api/qt/clist/get"
    pz = 100
    all_out = {}

    def _fetch_board(fs, bname):
        out = {}
        pn, total = 1, None
        while True:
            url = (f"{host}?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f12"
                   f"&fs={fs}&fields={SPOT_FIELDS}")
            data = (get_json(url, ttl_hours=CONFIG["cache_hours"]).get("data")) or {}
            if total is None:
                total = data.get("total") or 0
            diff = data.get("diff") or []
            if not diff and pn * pz < total:
                time.sleep(0.5)
                data = (get_json(url + "&_r=1", ttl_hours=0).get("data")) or {}
                diff = data.get("diff") or []
            for r in diff:
                out[str(r.get("f12"))] = r
            if pn * pz >= total or not diff:
                break
            pn += 1
            time.sleep(CONFIG["page_sleep"])
        print(f"  [行情·{bname}] {total} 只")
        return out

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_board, fs, bname): bname for fs, bname in SPOT_BOARDS}
        for future in as_completed(futures):
            try:
                board_data = future.result()
                all_out.update(board_data)
            except Exception as e:
                bname = futures[future]
                print(f"  ⚠ [行情·{bname}] 抓取失败: {e}")

    print(f"  [行情] 合计 {len(all_out)} 只")
    return all_out


# ============================================================
# 三、工具函数
# ============================================================
def fnum(x):
    """安全转 float；'-'/None/'' → None。"""
    if x is None or x == "-" or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def scale(v, lo, hi):
    """把 v 线性映射到 0~100（v≤lo→0, v≥hi→100）。"""
    if v is None or hi == lo:
        return 0.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


def is_st(name):
    if not name:
        return False
    n = name.upper().replace(" ", "")
    return ("ST" in n) or ("退" in name) or n.startswith("PT")


# ============================================================
# 四、数据装配
# ============================================================
def build_records(year):
    rd = f"{year}-12-31"
    a_filter = f"(REPORTDATE='{rd}')(SECURITY_TYPE_CODE=\"{A_SHARE_TYPE}\")"

    print("抓取数据中（并行，首次较慢，之后走缓存秒级）...")

    # ── 第一波：并行抓取财报/资产负债表/商誉/历史净利（各数据源相互独立）──
    def _fetch_yjbb():
        return fetch_datacenter(
            "RPT_LICO_FN_CPD",
            "SECURITY_CODE,SECURITY_NAME_ABBR,WEIGHTAVG_ROE,XSMLL,PARENT_NETPROFIT,"
            "TOTAL_OPERATE_INCOME,SJLTZ,YSTZ,BASIC_EPS,DEDUCT_BASIC_EPS,MGJYXJJE,BPS,NOTICE_DATE",
            a_filter, "业绩报表")

    def _fetch_bal():
        return fetch_datacenter(
            "RPT_DMSK_FN_BALANCE",
            "SECURITY_CODE,SECURITY_NAME_ABBR,DEBT_ASSET_RATIO,TOTAL_EQUITY,INDUSTRY_NAME",
            f"(REPORT_DATE='{rd}')", "资产负债表")

    def _fetch_gw():
        return fetch_datacenter(
            "RPT_GOODWILL_STOCKDETAILS",
            "SECURITY_CODE,GOODWILL,SUMSHEQUITY,SUMSHEQUITY_RATIO",
            f"(REPORT_DATE='{rd}')", "商誉明细")

    def _fetch_hist(y):
        rows = fetch_datacenter(
            "RPT_LICO_FN_CPD", "SECURITY_CODE,PARENT_NETPROFIT",
            f"(REPORTDATE='{y}-12-31')(SECURITY_TYPE_CODE=\"{A_SHARE_TYPE}\")", f"历史净利{y}")
        return y, {r["SECURITY_CODE"]: fnum(r.get("PARENT_NETPROFIT")) for r in rows}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_fetch_yjbb): "yjbb",
            ex.submit(_fetch_bal): "bal",
            ex.submit(_fetch_gw): "gw",
        }
        for y in range(year - CONFIG["cagr_years"], year):
            futures[ex.submit(_fetch_hist, y)] = f"hist_{y}"

        results = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                print(f"  ⚠ [{key}] 抓取失败: {e}")
                raise

    yjbb = results["yjbb"]
    bal = results["bal"]
    gw = results["gw"]
    hist = {}
    for y in range(year - CONFIG["cagr_years"], year):
        _, data = results[f"hist_{y}"]  # unpack (year, {code: profit}) tuple
        hist[y] = data
    print(f"  第一波完成 ({time.time()-t0:.1f}s)")

    # ── 第二波：并行抓取各板块行情 ──
    t1 = time.time()
    spot = fetch_spot_parallel()
    print(f"  第二波行情完成 ({time.time()-t1:.1f}s)")

    # 建索引
    yjbb_i = {r["SECURITY_CODE"]: r for r in yjbb}
    bal_i  = {r["SECURITY_CODE"]: r for r in bal}
    gw_i   = {r["SECURITY_CODE"]: r for r in gw}

    records = []
    for code, sp in spot.items():
        price = fnum(sp.get("f2"))
        mktcap = fnum(sp.get("f20"))
        if price is None or mktcap is None:   # 停牌/无行情
            continue
        yb = yjbb_i.get(code)
        if not yb:                            # 无最新年报（次新股等）
            continue

        name   = sp.get("f14") or yb.get("SECURITY_NAME_ABBR")
        roe    = fnum(yb.get("WEIGHTAVG_ROE"))
        gm     = fnum(yb.get("XSMLL"))
        netp   = fnum(yb.get("PARENT_NETPROFIT"))
        rev    = fnum(yb.get("TOTAL_OPERATE_INCOME"))
        yoy    = fnum(yb.get("SJLTZ"))
        eps    = fnum(yb.get("BASIC_EPS"))
        deps   = fnum(yb.get("DEDUCT_BASIC_EPS"))
        ocf_ps = fnum(yb.get("MGJYXJJE"))

        nm = (netp / rev * 100.0) if (netp is not None and rev not in (None, 0)) else None
        ocf_to_profit = (ocf_ps / eps) if (ocf_ps is not None and eps not in (None, 0) and eps > 0) else None
        deduct_ratio = (deps / eps) if (deps is not None and eps not in (None, 0) and eps > 0) else None

        # 3年净利 CAGR
        base = hist.get(year - CONFIG["cagr_years"], {}).get(code)
        cagr = None
        if base is not None and base > 0 and netp is not None and netp > 0:
            cagr = ((netp / base) ** (1.0 / CONFIG["cagr_years"]) - 1.0) * 100.0

        ba = bal_i.get(code) or {}
        debt = fnum(ba.get("DEBT_ASSET_RATIO"))
        industry = ba.get("INDUSTRY_NAME") or "未分类"

        gwr = fnum((gw_i.get(code) or {}).get("SUMSHEQUITY_RATIO")) or 0.0  # 不在商誉表=无商誉=0

        pe_ttm = fnum(sp.get("f115"))
        pe_dyn = fnum(sp.get("f9"))
        pb     = fnum(sp.get("f23"))

        # 估值衍生量
        ttm_netp = (mktcap / pe_ttm) if (pe_ttm and pe_ttm > 0) else None
        # 增长率取保守值：同比与CAGR取小（都有时）
        gs = [g for g in (yoy, cagr) if g is not None]
        g = min(gs) if gs else None
        eyield = (100.0 / pe_ttm) if (pe_ttm and pe_ttm > 0) else None
        peg = (pe_ttm / g) if (pe_ttm and pe_ttm > 0 and g and g > 0) else None
        exp_ret = (eyield + g) if (eyield is not None and g is not None) else None
        reasonable_pe = None
        if g is not None:
            reasonable_pe = max(CONFIG["reasonable_pe_floor"], min(CONFIG["reasonable_pe_cap"], g))
        fair_mktcap = (ttm_netp * reasonable_pe) if (ttm_netp and reasonable_pe) else None
        discount = (1.0 - mktcap / fair_mktcap) if fair_mktcap else None  # 正=低于合理价

        records.append({
            "code": code, "name": name, "industry": industry,
            "roe": roe, "gross_margin": gm, "net_margin": nm,
            "net_profit": netp, "revenue": rev, "yoy": yoy, "cagr": cagr, "g": g,
            "eps": eps, "deduct_ratio": deduct_ratio, "ocf_ps": ocf_ps, "ocf_to_profit": ocf_to_profit,
            "debt_ratio": debt, "goodwill_ratio": gwr,
            "price": price, "mktcap": mktcap, "pe_ttm": pe_ttm, "pe_dyn": pe_dyn, "pb": pb,
            "ttm_netp": ttm_netp, "eyield": eyield, "peg": peg, "exp_ret": exp_ret,
            "reasonable_pe": reasonable_pe, "fair_mktcap": fair_mktcap, "discount": discount,
            "is_st": is_st(name),
        })
    return records


# ============================================================
# 五、五层流水线判定 + 评分
# ============================================================
def industry_median_pe(records):
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in records:
        if r["pe_ttm"] and r["pe_ttm"] > 0 and not r["is_st"]:
            buckets[r["industry"]].append(r["pe_ttm"])
    med = {}
    for ind, vals in buckets.items():
        vals.sort()
        n = len(vals)
        med[ind] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return med


def evaluate(r, ind_pe):
    """返回 (deepest_layer, tier, fails:list)。deepest_layer = 已完整通过的最高层(0~4)。"""
    C = CONFIG
    fails = []

    # —— 第0层 排雷 ——
    l0 = []
    if r["is_st"]:                                                  l0.append("ST/退市")
    if not (r["net_profit"] and r["net_profit"] > 0):              l0.append("亏损")
    if not (r["revenue"] and r["revenue"] > 0):                    l0.append("无营收")
    if r["ocf_to_profit"] is None or r["ocf_to_profit"] < C["min_ocf_to_profit"]:
        l0.append(f"现金流/利润<{C['min_ocf_to_profit']}")
    if r["debt_ratio"] is None or r["debt_ratio"] >= C["max_debt_ratio"]:
        l0.append(f"负债率≥{C['max_debt_ratio']}%")
    if r["goodwill_ratio"] >= C["max_goodwill_ratio"]:
        l0.append(f"商誉≥净资产{C['max_goodwill_ratio']}%")
    fails += l0

    # —— 第1层 质量 ——
    l1 = []
    if r["roe"] is None or r["roe"] < C["min_roe"]:                l1.append(f"ROE<{C['min_roe']}")
    if r["gross_margin"] is None or r["gross_margin"] < C["min_gross_margin"]: l1.append(f"毛利<{C['min_gross_margin']}")
    if r["net_margin"] is None or r["net_margin"] < C["min_net_margin"]:       l1.append(f"净利率<{C['min_net_margin']}")
    if r["yoy"] is None or r["yoy"] < C["min_growth"]:            l1.append(f"同比增速<{C['min_growth']}")
    if r["cagr"] is None or r["cagr"] < C["min_growth"]:         l1.append(f"CAGR<{C['min_growth']}")
    fails += l1

    # —— 第2层 估值 ——
    l2 = []
    if r["peg"] is None or r["peg"] >= C["max_peg"]:             l2.append(f"PEG≥{C['max_peg']}")
    if r["eyield"] is None or r["eyield"] <= C["min_earnings_yield"]: l2.append(f"盈利收益率≤{C['min_earnings_yield']}%")
    med = ind_pe.get(r["industry"])
    pe_le_peer = (r["pe_ttm"] is not None and r["pe_ttm"] > 0 and med is not None and r["pe_ttm"] <= med)
    if not pe_le_peer:                                           l2.append("PE高于行业中位")
    fails += l2

    # —— 第3层 毛估估 + 安全边际 ——
    l3 = []
    if r["exp_ret"] is None or r["exp_ret"] < C["min_expected_return"]:
        l3.append(f"预期年化<{C['min_expected_return']}%")
    if r["discount"] is None or r["discount"] < (1.0 - C["margin_of_safety"]):
        l3.append(f"未打{C['margin_of_safety']}折")
    fails += l3

    # 通过深度
    deepest = 0
    if not l0: deepest = 1
    if not l0 and not l1: deepest = 2
    if not l0 and not l1 and not l2: deepest = 3
    if not l0 and not l1 and not l2 and not l3: deepest = 4

    # 分层
    if deepest >= 4:
        tier = "A_可买入"
    elif not l0 and not l1:
        tier = "B_优质待跌"        # 质量确认，只是估值/买点未到
    elif not l0 and len(l1) == 1:
        tier = "C_接近合格"        # 排雷过关，质量仅差一项
    else:
        tier = "-"
    return deepest, tier, fails


def score(r, ind_pe):
    W = CONFIG["weights"]
    yoy, cagr = r["yoy"], r["cagr"]
    if yoy is not None and cagr is not None:
        momentum = 100.0 if yoy >= cagr else scale(yoy / cagr if cagr else 0, 0.5, 1.0)
    else:
        momentum = 50.0
    med = ind_pe.get(r["industry"])
    pe_ind = 50.0
    if med and r["pe_ttm"] and r["pe_ttm"] > 0:
        ratio = r["pe_ttm"] / med
        pe_ind = max(0.0, min(100.0, (2.0 - ratio) / 2.0 * 100.0))
    peg_s = max(0.0, min(100.0, (1.5 - r["peg"]) / 1.5 * 100.0)) if r["peg"] is not None else 0.0
    disc_s = scale(r["discount"], 0.0, 0.5) if r["discount"] is not None else 0.0

    subs = {
        "roe": scale(r["roe"], 15, 35),
        "gross": scale(r["gross_margin"], 30, 70),
        "net": scale(r["net_margin"], 10, 35),
        "growth": scale(r["g"], 10, 40),
        "ocf": scale(r["ocf_to_profit"], 0.8, 1.6),
        "momentum": momentum,
        "eyield": scale(r["eyield"], 5, 12.5),
        "peg": peg_s,
        "discount": disc_s,
        "pe_ind": pe_ind,
    }
    total = sum(W[k] * subs[k] for k in W) / 100.0  # 权重和=100 → 0~100
    return round(total, 2)


# ============================================================
# 六、输出
# ============================================================
def n(x, d=1):
    return "" if x is None else f"{x:.{d}f}"


def write_csv(records, path):
    cols = ["rank", "tier", "score", "code", "name", "price", "min_buy", "industry", "deepest_layer",
            "roe", "gross_margin", "net_margin", "yoy", "cagr",
            "deduct_ratio", "ocf_to_profit", "debt_ratio", "goodwill_ratio",
            "pe_ttm", "peg", "eyield", "exp_ret", "discount", "pb",
            "mktcap_yi", "risk_notes", "fail_reasons"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, r in enumerate(records, 1):
            min_buy = int((r["price"] or 0) * 100)
            w.writerow([
                i, r["tier"], r["score"], r["code"], r["name"], n(r["price"], 2), min_buy, r["industry"], r["deepest"],
                n(r["roe"]), n(r["gross_margin"]), n(r["net_margin"]), n(r["yoy"]), n(r["cagr"]),
                n(r["deduct_ratio"], 2), n(r["ocf_to_profit"], 2), n(r["debt_ratio"]), n(r["goodwill_ratio"]),
                n(r["pe_ttm"], 2), n(r["peg"], 2), n(r["eyield"], 2), n(r["exp_ret"]),
                n((r["discount"] or 0) * 100), n(r["pb"], 2),
                n((r["mktcap"] or 0) / 1e8), "; ".join(r.get("notes", [])), "; ".join(r["fails"]),
            ])


def md_table(rows):
    head = ("| 排名 | 代码 | 名称 | 现价 | 一手 | 行业 | 评分 | ROE% | 毛利% | 净利% | 同比% | CAGR% | "
            "PE(TTM) | PEG | 预期年化% | 折让% | 现金流/利润 | 负债% | 商誉% | 市值(亿) |")
    sep = "|" + "---|" * 20
    lines = [head, sep]
    for i, r in enumerate(rows, 1):
        nm_disp = ("⚠" + r["name"]) if r.get("notes") else r["name"]
        min_buy = int((r["price"] or 0) * 100)
        lines.append("| {i} | {code} | {name} | {px} | {mb} | {ind} | {sc} | {roe} | {gm} | {nm} | {yoy} | {cagr} | "
                     "{pe} | {peg} | {er} | {disc} | {ocf} | {debt} | {gw} | {cap} |".format(
            i=i, code=r["code"], name=nm_disp, px=n(r["price"], 2), mb=min_buy, ind=r["industry"], sc=n(r["score"]),
            roe=n(r["roe"]), gm=n(r["gross_margin"]), nm=n(r["net_margin"]),
            yoy=n(r["yoy"]), cagr=n(r["cagr"]), pe=n(r["pe_ttm"], 1), peg=n(r["peg"], 2),
            er=n(r["exp_ret"]), disc=n((r["discount"] or 0) * 100), ocf=n(r["ocf_to_profit"], 2),
            debt=n(r["debt_ratio"]), gw=n(r["goodwill_ratio"]), cap=n((r["mktcap"] or 0) / 1e8)))
    return "\n".join(lines)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股五层选股结果</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e8eb;font-size:13px}
header{padding:16px 20px;background:linear-gradient(135deg,#161a22,#0f1115);border-bottom:1px solid #232936}
h1{margin:0 0 4px;font-size:18px}
.sub{color:#8b93a1;font-size:12px}
/* Dashboard */
.dash{padding:16px 20px;background:#0d1117;border-bottom:1px solid #232936;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.dash-card{background:#131820;border:1px solid #1e2634;border-radius:10px;padding:14px;overflow:hidden}
.dash-card h3{margin:0 0 10px;font-size:13px;color:#8b93a1;font-weight:500}
.dash-card canvas{width:100%;height:170px}
.kpi-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.kpi{background:#1a1f29;border:1px solid #232936;border-radius:8px;padding:8px 14px;text-align:center;min-width:80px;flex:1}
.kpi .val{font-size:22px;font-weight:700;line-height:1.2}
.kpi .lbl{font-size:10px;color:#6b7380;margin-top:2px}
.kpi.A .val{color:#3ddc84}.kpi.B .val{color:#ffd166}.kpi.C .val{color:#9aa4b2}.kpi.X .val{color:#5a6270}
.leaderboard{display:flex;flex-direction:column;gap:4px;font-size:11px;max-height:170px;overflow-y:auto}
.lb-row{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:4px}
.lb-row:hover{background:#1a1f29}
.lb-rank{width:20px;text-align:center;font-weight:700;color:#6b7380}
.lb-rank.r1{color:#ffd166}.lb-rank.r2{color:#c7cdd6}.lb-rank.r3{color:#c79a4a}
.lb-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-code{color:#7fb3ff;font-variant-numeric:tabular-nums;width:64px}
.lb-score{font-weight:700;color:#3ddc84;width:40px;text-align:right}
.lb-tag{font-size:10px;padding:1px 5px;border-radius:3px;font-weight:700}
.lb-tag.a{background:#143524;color:#3ddc84}.lb-tag.b{background:#332a10;color:#ffd166}
.funnel{display:flex;gap:0;height:140px;align-items:flex-end;padding:0 6px}
.funnel-bar{flex:1;border-radius:5px 5px 0 0;position:relative;min-width:36px;margin:0 2px;transition:all .3s}
.funnel-bar:hover{filter:brightness(1.3)}
.funnel-val{position:absolute;top:-18px;left:50%;transform:translateX(-50%);font-size:11px;font-weight:700;white-space:nowrap}
.funnel-lbl{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:10px;color:#6b7380;text-align:center;white-space:nowrap}
/* Controls */
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 20px;background:#12151c;border-bottom:1px solid #232936;position:sticky;top:0;z-index:20}
input,select{background:#1a1f29;border:1px solid #2a3140;color:#e6e8eb;border-radius:7px;padding:7px 10px;font-size:13px;outline:none}
input:focus,select:focus{border-color:#3a86ff}
.btn{cursor:pointer;background:#1a1f29;border:1px solid #2a3140;color:#c7cdd6;border-radius:7px;padding:7px 12px;font-size:12px}
.btn.on{background:#3a86ff;border-color:#3a86ff;color:#fff}
.btn.refresh{background:#1d3320;border-color:#1e3e1e;color:#3ddc84}
.chk{display:flex;align-items:center;gap:5px;color:#c7cdd6;cursor:pointer;user-select:none}
.toast{position:fixed;bottom:20px;right:20px;background:#1a2a1a;border:1px solid #3ddc84;border-radius:8px;padding:10px 16px;color:#3ddc84;font-size:12px;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
.wrap{overflow:auto;height:calc(100vh - 52px)}
table{border-collapse:collapse;width:100%;white-space:nowrap}
th{position:sticky;top:0;background:#1a1f29;color:#c7cdd6;font-weight:600;padding:8px 10px;text-align:right;cursor:pointer;border-bottom:2px solid #2a3140;font-size:12px}
th.l,td.l{text-align:left}
th:hover{color:#fff}
td{padding:6px 10px;text-align:right;border-bottom:1px solid #1c212b}
tr:hover td{background:#161b24}
tr.warn td{background:#1f1a12}
tr.warn:hover td{background:#26200f}
.badge{display:inline-block;min-width:18px;text-align:center;border-radius:5px;padding:1px 6px;font-weight:700;font-size:11px}
.bA{background:#143524;color:#3ddc84}.bB{background:#332a10;color:#ffd166}.bC{background:#23282f;color:#9aa4b2}.b-{background:#1c1f26;color:#5a6270}
.pos{color:#3ddc84}.neg{color:#ff6b6b}
.code{color:#7fb3ff;font-variant-numeric:tabular-nums}
.code.pending{color:#8b93a1}
.note{color:#c79a4a;font-size:11px;text-align:left;max-width:320px;white-space:normal}
.cnt{color:#8b93a1;font-size:12px;margin-left:auto}
footer{padding:10px 20px;color:#5a6270;font-size:11px;border-top:1px solid #232936}
@media(max-width:768px){.dash{grid-template-columns:1fr}}
</style></head><body>
<header>
<h1>A股「五层选股流水线」结果 <span id="svrStatus" style="font-size:11px;color:#5a6270"></span></h1>
<div class="sub" id="sub"></div>
</header>
<!-- Dashboard -->
<div class="dash" id="dash">
<div class="dash-card">
<h3>📊 五层漏斗 (全市场 <span id="dtotal"></span> 只)</h3>
<div class="funnel" id="funnel"></div>
</div>
<div class="dash-card">
<h3>🏆 Tier A 优质榜 Top 10</h3>
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
<input id="q" placeholder="搜索代码/名称…" style="width:160px">
<button class="btn on" data-t="all">全部</button>
<button class="btn" data-t="A">🟢A 可买入</button>
<button class="btn" data-t="B">🟡B 优质待跌</button>
<button class="btn" data-t="C">⚪C 接近</button>
<button class="btn" data-t="x">未通过</button>
<select id="ind"></select>
<label class="chk"><input type="checkbox" id="warn">仅看⚠风险</label>
<label class="chk"><input type="checkbox" id="pass">仅通过排雷(第0层)</label>
<button class="btn refresh" id="refreshBtn" title="重新运行选股脚本，刷新价格、估值、财务和筛选评分">🔄 刷新指标</button>
<button class="btn refresh" id="layer4Btn" title="对 Tier A 标的运行 DeepSeek AI 定性分析" style="background:#1d2033;border-color:#1e2340;color:#7fb3ff">🧠 定性分析</button>
<span class="cnt" id="cnt"></span>
</div>
<div class="toast" id="toast"></div>
<div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<footer>数据来源：东方财富公开接口 · 第0层排雷→第1层质量→第2层估值→第3层安全边际 · 第4层定性需人工把关</footer>
<script>
var DATA=__DATA__, INDS=__INDS__, META=__META__;
var COLS=[["rk","#","n"],["tier","档","s"],["code","代码","s"],["name","名称","s"],
["px","现价","n"],["mb","一手","n"],["sc","评分","n"],["L","层","n"],["roe","ROE%","n"],["gm","毛利%","n"],
["nm","净利%","n"],["yoy","同比%","n"],["cagr","CAGR%","n"],["pe","PE","n"],["peg","PEG","n"],
["er","预期年化%","n"],["disc","折让%","n"],["ocf","现金流/利润","n"],["dd","扣非比","n"],
["debt","负债%","n"],["gw","商誉%","n"],["cap","市值亿","n"],["ind","行业","s"],["note","风险/落选原因","s"]];
var state={t:"all",q:"",ind:"",warn:false,pass:false,sk:"sc",sd:-1};
var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;
function fmt(v){return v===null||v===undefined?"":v}
document.getElementById("sub").innerHTML=META.year+"年报口径 · 生成于 "+META.ts+" · 全市场评估 "+META.total+" 只"
  +(META.hasDeep?' · <a href="deep_dives/index.html" style="color:#3ddc84">🔬 深度研报 ('+META.deepCount+'只)</a>':'');

// ---- Dashboard rendering ----
function drawScoreChart(){
 var cv=document.getElementById("cvScore");if(!cv)return;
 var W=cv.parentElement.clientWidth-28,H=140;
 cv.width=W*2;cv.height=H*2;cv.style.width=W+"px";cv.style.height=H+"px";
 var ctx=cv.getContext("2d");ctx.scale(2,2);
 var bins=META.scoreBins,keys=Object.keys(bins),vals=Object.values(bins);
 var max=Math.max.apply(null,vals),barW=(W-40)/keys.length;
 var colors=["#3a3f4b","#4a5160","#5a6270","#7fb3ff","#3a86ff","#3ddc84","#ffd166","#ff9f1c"];
 ctx.clearRect(0,0,W,H);
 for(var i=0;i<vals.length;i++){
  var bh=vals[i]/max*(H-30),x=20+i*barW,y=H-15-bh;
  ctx.fillStyle=colors[i];ctx.fillRect(x+2,y,barW-4,bh);
  ctx.fillStyle="#6b7380";ctx.font="10px sans-serif";ctx.textAlign="center";
  ctx.fillText(vals[i],x+barW/2,y-4);
  ctx.fillText(keys[i],x+barW/2,H-2);
 }
}
function drawIndChart(){
 var cv=document.getElementById("cvInd");if(!cv)return;
 var W=cv.parentElement.clientWidth-28,H=170,labelPad=54;
 cv.width=W*2;cv.height=H*2;cv.style.width=W+"px";cv.style.height=H+"px";
 var ctx=cv.getContext("2d");ctx.scale(2,2);
 var visibleCount=W<420?6:8;
 var inds=META.topInds.slice(0,visibleCount);
 if(!inds.length){ctx.fillStyle="#6b7380";ctx.font="12px sans-serif";ctx.textAlign="center";ctx.fillText("暂无 A+B 行业分布",W/2,H/2);return}
 var max=inds[0][1],barW=(W-70)/inds.length;
 var colors=["#3ddc84","#3a86ff","#ffd166","#7fb3ff","#ff9f1c","#c79a4a","#8b93a1","#5a6270"];
 ctx.clearRect(0,0,W,H);
 for(var i=0;i<inds.length;i++){
  var bh=inds[i][1]/max*(H-labelPad-12),x=50+i*barW,y=H-labelPad-bh;
  ctx.fillStyle=colors[i];ctx.fillRect(x+2,y,barW-4,bh);
  ctx.fillStyle="#e6e8eb";ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(inds[i][1],x+barW/2,y-4);
  ctx.save();ctx.translate(x+barW/2,H-labelPad+42);ctx.rotate(-0.5);
  ctx.fillStyle="#6b7380";ctx.font="10px sans-serif";ctx.fillText(inds[i][0],0,0);ctx.restore();
 }
}
function renderFunnel(){
 var layers=["第0排雷","第1质量","第2估值","第3安全","第4定性"],values=[META.funnel[0],META.funnel[1],META.funnel[2],META.funnel[3],META.funnel[4]],
  colors=["#5a6270","#3a86ff","#7fb3ff","#ffd166","#3ddc84"],
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
 var rows=a.slice(0,10),html1="";
 rows.forEach(function(r,i){
  var rc=i<3?"r"+(i+1):"";
  html1+='<div class="lb-row"><span class="lb-rank '+rc+'">'+(i+1)+'</span>'
    +'<span class="lb-code">'+r.code+'</span><span class="lb-name">'+r.name+'</span>'
    +'<span class="lb-tag a">A</span><span class="lb-score">'+r.sc+'</span></div>';
 });
 document.getElementById("lbA").innerHTML=html1||'<div style="color:#6b7380;font-size:12px;padding:20px;text-align:center">本期无A级标的</div>';
}
// init dashboard
renderFunnel();renderLB();drawScoreChart();drawIndChart();
window.addEventListener("resize",function(){drawScoreChart();drawIndChart()});

// ---- Server status ----
fetch(API+"/api/status").then(function(r){return r.json()}).then(function(d){
 var el=document.getElementById("svrStatus");
 if(d.latest_ts){
  el.innerHTML='<span style="color:#3ddc84">●</span> 已连接 ('+d.deep_count+'只研报)';
 }else{
  el.innerHTML='<span style="color:#ff6b6b">●</span> 离线';
 }
}).catch(function(){
 document.getElementById("svrStatus").innerHTML='<span style="color:#ff6b6b">●</span> 离线';
});

// ---- Refresh button ----
document.getElementById("refreshBtn").addEventListener("click",function(){
 var btn=document.getElementById("refreshBtn");
 btn.textContent="⏳ 刷新中...";btn.disabled=true;
 var toast=document.getElementById("toast");
 fetch(API+"/api/refresh?fresh=1").then(function(r){return r.json()}).then(function(d){
  if(d.done){
   toast.textContent="✅ 数据已刷新，正在重载页面…";
   toast.classList.add("show");
   setTimeout(function(){window.location.reload()},800);
  }else if(d.cached){
   toast.textContent="⏳ "+(d.msg||"冷却中，稍后再试");
   toast.classList.add("show");
   setTimeout(function(){toast.classList.remove("show")},3000);
  }else{
   toast.textContent="⚠️ 刷新失败: "+(d.error||"未知错误");
   toast.classList.add("show");
   setTimeout(function(){toast.classList.remove("show")},4000);
  }
  btn.textContent="🔄 刷新指标";btn.disabled=false;
 }).catch(function(e){
  toast.textContent="⚠️ 无法连接本地服务，请通过 ./run.sh 打开 HTTP 页面";
  toast.classList.add("show");
  setTimeout(function(){toast.classList.remove("show")},4000);
  btn.textContent="🔄 刷新指标";btn.disabled=false;
 });
});

// ---- Layer 4 定性分析按钮（根据当前筛选运行）----
document.getElementById("layer4Btn").addEventListener("click",function(){
 var btn=document.getElementById("layer4Btn");
 var toast=document.getElementById("toast");
 var tier=state.t==="all"?"A":(state.t==="x"?"A":state.t); // 全表/未通过默认跑 A
 btn.textContent="⏳ 启动中…";btn.disabled=true;
 toast.textContent="🧠 正在启动 Tier "+tier+" 定性分析…";
 toast.classList.add("show");
 fetch(API+"/api/layer4?tier="+tier).then(function(r){return r.json()}).then(function(d){
  if(d.msg){
   toast.textContent=d.msg+" 开始轮询进度…";
   pollProgress();
  }else if(d.done){
   toast.textContent="✅ 分析完成！正在刷新…";
   setTimeout(function(){window.location.reload()},1000);
  }else{
   toast.textContent="⚠️ "+(d.msg||d.error||"未知错误");
   setTimeout(function(){toast.classList.remove("show")},4000);
  }
  btn.textContent="🧠 定性分析";btn.disabled=false;
 }).catch(function(e){
  toast.textContent="⚠️ 无法连接本地服务，请通过 ./run.sh 打开 HTTP 页面";
  setTimeout(function(){toast.classList.remove("show")},4000);
  btn.textContent="🧠 定性分析";btn.disabled=false;
 });
});

var pollTimer=null;
function pollProgress(){
 if(pollTimer)clearInterval(pollTimer);
 var toast=document.getElementById("toast");
 var btn=document.getElementById("layer4Btn");
 btn.textContent="⏳ 分析中…";btn.disabled=true;
 pollTimer=setInterval(function(){
  fetch(API+"/api/status").then(function(r){return r.json()}).then(function(d){
   var p=d.progress;
   if(!p){clearInterval(pollTimer);btn.textContent="🧠 定性分析";btn.disabled=false;return}
   if(p.done===true){
    clearInterval(pollTimer);
    toast.textContent="✅ 定性分析完成！正在刷新…";
    toast.classList.add("show");
    btn.textContent="🧠 定性分析";btn.disabled=false;
    setTimeout(function(){window.location.reload()},1000);
   }else{
    var pct=p.target>0?Math.round(p.done/p.target*100):0;
    toast.textContent="🧠 Tier "+p.tier+" 分析中: "+p.done+"/"+p.target+" ("+pct+"%) · 已用"+p.elapsed+"秒 · "+p.eta;
    toast.classList.add("show");
   }
  });
 },2000);
}

// ---- Table ----
var sel=document.getElementById("ind");sel.innerHTML='<option value="">全部行业</option>'+INDS.map(function(x){return '<option>'+x+'</option>'}).join("");
function head(){document.getElementById("head").innerHTML=COLS.map(function(c){
 var ar=state.sk===c[0]?(state.sd<0?" ▼":" ▲"):"";
 var cl=c[2]==="s"?"l":"";return '<th class="'+cl+'" data-k="'+c[0]+'">'+c[1]+ar+'</th>'}).join("")}
function tierBadge(t){var k=t||"-";return '<span class="badge b'+k+'">'+(t||"-")+'</span>'}
function rowHTML(r){
 var cells=COLS.map(function(c){var k=c[0],v=r[k];
  if(k==="tier")return '<td>'+tierBadge(v)+'</td>';
  if(k==="code"){
   var dv = r.deep
    ? '<a href="deep_dives/report.html?code='+v+'" class="code" title="查看深度研报">'+v+'</a>'
    : '<a href="deep_dives/report.html?code='+v+'" class="code pending" title="生成深度研报">'+v+'</a>';
   return '<td class="l">'+dv+'</td>';
  }
  if(k==="name"){
   var nm = (r.warn?"⚠":"")+v;
   var dv2 = r.deep
    ? '<a href="deep_dives/report.html?code='+r.code+'" style="color:inherit;text-decoration:none" title="查看深度研报">'+nm+'</a>'
    : '<a href="deep_dives/report.html?code='+r.code+'" style="color:#8b93a1;text-decoration:none" title="生成深度研报">'+nm+'</a>';
   return '<td class="l">'+dv2+'</td>';
  }
  if(k==="ind")return '<td class="l">'+fmt(v)+'</td>';
  if(k==="note")return '<td class="note">'+fmt(v)+'</td>';
  if(k==="disc"){var cls=v>0?"pos":(v<0?"neg":"");return '<td class="'+cls+'">'+fmt(v)+'</td>'}
  return '<td>'+fmt(v)+'</td>'}).join("");
 return '<tr class="'+(r.warn?"warn":"")+'">'+cells+'</tr>'}
function render(){
 var q=state.q.trim().toLowerCase();
 var rows=DATA.filter(function(r){
  if(state.t==="A"&&r.tier!=="A")return false;
  if(state.t==="B"&&r.tier!=="B")return false;
  if(state.t==="C"&&r.tier!=="C")return false;
  if(state.t==="x"&&r.tier!=="")return false;
  if(state.ind&&r.ind!==state.ind)return false;
  if(state.warn&&!r.warn)return false;
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
document.querySelectorAll(".btn").forEach(function(b){b.addEventListener("click",function(){
 document.querySelectorAll(".btn").forEach(function(x){x.classList.remove("on")});b.classList.add("on");state.t=b.getAttribute("data-t");render()})});
document.getElementById("q").addEventListener("input",function(e){state.q=e.target.value;render()});
document.getElementById("ind").addEventListener("change",function(e){state.ind=e.target.value;render()});
document.getElementById("warn").addEventListener("change",function(e){state.warn=e.target.checked;render()});
document.getElementById("pass").addEventListener("change",function(e){state.pass=e.target.checked;render()});
head();render();
</script></body></html>"""


def write_html(records, path, year, total_eval, tierN):
    """生成自包含交互式网页：全部股票 + Dashboard 仪表盘，可搜索/按档筛选/点表头排序。"""
    rnd = lambda x, d=1: (None if x is None else round(x, d))
    tshort = {"A_可买入": "A", "B_优质待跌": "B", "C_接近合格": "C"}
    data = []
    # 检查哪些股票已有深度研报
    deep_dir = os.path.join(OUT_DIR, "deep_dives")
    deep_data_dir = os.path.join(deep_dir, "data")
    existing_deep = set()
    if os.path.isdir(deep_data_dir):
        for f in os.listdir(deep_data_dir):
            if len(f) == 11 and f.endswith(".json") and f[:6].isdigit():
                existing_deep.add(f[:6])
    for i, r in enumerate(records, 1):
        data.append({
            "rk": i, "code": r["code"], "name": r["name"],
            "px": rnd(r["price"], 2), "mb": int((r["price"] or 0) * 100),
            "ind": r["industry"], "tier": tshort.get(r["tier"], ""),
            "sc": r["score"], "L": r["deepest"],
            "deep": 1 if r["code"] in existing_deep else 0,
            "roe": rnd(r["roe"]), "gm": rnd(r["gross_margin"]), "nm": rnd(r["net_margin"]),
            "yoy": rnd(r["yoy"]), "cagr": rnd(r["cagr"]),
            "pe": rnd(r["pe_ttm"], 2), "peg": rnd(r["peg"], 2),
            "er": rnd(r["exp_ret"]), "disc": rnd((r["discount"] or 0) * 100, 1),
            "ocf": rnd(r["ocf_to_profit"], 2), "dd": rnd(r["deduct_ratio"], 2),
            "debt": rnd(r["debt_ratio"]), "gw": rnd(r["goodwill_ratio"]),
            "cap": rnd((r["mktcap"] or 0) / 1e8, 1),
            "warn": 1 if r.get("notes") else 0,
            "note": "; ".join(r.get("notes", [])) or "; ".join(r["fails"]),
        })
    inds = sorted({r["industry"] for r in records})

    # Dashboard 数据：评分分布区间
    score_bins = {"0-20":0,"20-30":0,"30-40":0,"40-50":0,"50-60":0,"60-70":0,"70-80":0,"80-100":0}
    for r in records:
        s = r["score"]
        if s < 20: score_bins["0-20"] += 1
        elif s < 30: score_bins["20-30"] += 1
        elif s < 40: score_bins["30-40"] += 1
        elif s < 50: score_bins["40-50"] += 1
        elif s < 60: score_bins["50-60"] += 1
        elif s < 70: score_bins["60-70"] += 1
        elif s < 80: score_bins["70-80"] += 1
        else: score_bins["80-100"] += 1

    # 行业分布（A+B 通过的行业 Top 8）
    from collections import Counter
    ind_count = Counter(r["industry"] for r in records if r["tier"] in ("A_可买入", "B_优质待跌"))
    top_inds = ind_count.most_common(8)

    # 五层漏斗：第0层剩余 → 第1层剩余 → ...
    l0_pass = sum(1 for r in records if r["deepest"] >= 1)
    l1_pass = sum(1 for r in records if r["deepest"] >= 2)
    l2_pass = sum(1 for r in records if r["deepest"] >= 3)
    l3_pass = sum(1 for r in records if r["deepest"] >= 4)
    funnel = [total_eval, l0_pass, l1_pass, l2_pass, l3_pass]

    meta = {"year": year, "total": total_eval, "ts": time.strftime("%Y-%m-%d %H:%M"),
            "A": tierN[0], "B": tierN[1], "C": tierN[2],
            "scoreBins": score_bins, "topInds": top_inds, "funnel": funnel,
            "hasDeep": len(existing_deep) > 0, "deepCount": len(existing_deep)}
    html = HTML_TEMPLATE \
        .replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
        .replace("__INDS__", json.dumps(inds, ensure_ascii=False)) \
        .replace("__META__", json.dumps(meta, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def write_md(tierA, tierB, tierC, path, year, total_eval):
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"# A股五层选股结果（{year}年报 · 生成于 {ts}）\n",
        f"全市场参与筛选 **{total_eval}** 只 A 股。按评分（满分100）从高到低排序。\n",
        "> 数据来源：东方财富公开接口。第0-3层为量化筛选，**第4层定性（护城河/商业模式/能力圈/管理层/行业景气）需人工把关**——",
        "> 量化能帮你把范围从 5000+ 缩到几十只，但最后一道防价值陷阱的关，机器替不了。\n",
        f"## 🟢 Tier A · 可买入（五层全过，估值已到买点）— {len(tierA)} 只\n",
        "通过排雷+质量+估值，且当前市值已打到合理价 7 折以内、预期年化≥10%。\n",
        md_table(tierA) if tierA else "_本期无标的同时满足「优质 + 打7折」。这很正常——好公司很少便宜。见下方 Tier B 候选池。_",
        f"\n\n## 🟡 Tier B · 优质待跌（质量确认，估值/买点未到）— {len(tierB)} 只\n",
        "排雷与质量层全部过关的真·好生意，只是现在不够便宜。**加自选，等回调到买点**。\n",
        md_table(tierB[:60]),
        f"\n\n## ⚪ Tier C · 接近合格（排雷过关，质量仅差一项）— {len(tierC)} 只\n",
        "仅供观察，差一口气，可留意基本面是否改善。\n",
        md_table(tierC[:40]),
        "\n\n---\n### 字段说明\n",
        "- **⚠ 名称前缀**：该股有风险备注（扣非占比低/单年爆发增长/动态PE异常），第4层定性需重点核查，详见 CSV 的 risk_notes 列\n",
        "- **现金流/利润**：经营现金流÷净利润，≥0.8 说明利润是真金白银\n",
        "- **折让%**：(合理市值−当前市值)/合理市值，正数=低于合理价；≥30% 才算到买点\n",
        "- **预期年化**：盈利收益率(1/PE) + 增长率\n",
        "- **PEG**：PE ÷ 增长率（同比与3年CAGR取小，偏保守）\n",
        "- 评分权重：质量55（ROE/毛利/净利/增速/现金流/动能）+ 估值安全45（盈利收益率/PEG/折让/行业相对PE）\n",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


# ============================================================
# 七、主流程
# ============================================================
def main():
    global USE_CACHE
    ap = argparse.ArgumentParser(description="A股五层选股流水线")
    ap.add_argument("--year", type=int, default=CONFIG["report_year"], help="主报告期年份（年报）")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存，强制重新抓取")
    ap.add_argument("--top", type=int, default=50, help="终端打印前 N 名")
    args = ap.parse_args()
    if args.fresh:
        USE_CACHE = False
    CONFIG["report_year"] = args.year

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    t0 = time.time()
    records = build_records(args.year)
    if not records:
        print(f"⚠️  {args.year} 年报数据为空，尝试回退到 {args.year-1} ...")
        CONFIG["report_year"] = args.year - 1
        records = build_records(args.year - 1)
        args.year -= 1
    print(f"装配完成：{len(records)} 只可评估 A 股，用时 {time.time()-t0:.1f}s\n")

    ind_pe = industry_median_pe(records)
    for r in records:
        deepest, tier, fails = evaluate(r, ind_pe)
        r["deepest"], r["tier"], r["fails"] = deepest, tier, fails
        r["score"] = score(r, ind_pe)
        # 风险备注（不改变通过判定，仅供第4层人工核查）
        notes = []
        if r["deduct_ratio"] is not None and r["deduct_ratio"] < 0.7:
            notes.append(f"扣非比{r['deduct_ratio']*100:.0f}%(含较多一次性收益)")
        if r["yoy"] and r["cagr"] and r["cagr"] > 0 and r["yoy"] > 2.2 * r["cagr"]:
            notes.append("增速或为单年爆发(同比远高于CAGR)")
        if r["pe_ttm"] and r["pe_dyn"] and r["pe_dyn"] > 0 and r["pe_ttm"] > 0 and r["pe_dyn"] > 2 * r["pe_ttm"]:
            notes.append("动态PE远高于TTM(盈利或下滑)")
        r["notes"] = notes

    # 排序：评分降序
    records.sort(key=lambda r: r["score"], reverse=True)

    tierA = [r for r in records if r["tier"] == "A_可买入"]
    tierB = [r for r in records if r["tier"] == "B_优质待跌"]
    tierC = [r for r in records if r["tier"] == "C_接近合格"]

    ts = time.strftime("%Y%m%d")
    csv_path  = os.path.join(OUT_DIR, f"astock_screen_{ts}.csv")
    md_path   = os.path.join(OUT_DIR, f"astock_shortlist_{ts}.md")
    html_path = os.path.join(OUT_DIR, f"astock_screen_{ts}.html")
    write_csv(records, csv_path)
    write_md(tierA, tierB, tierC, md_path, args.year, len(records))
    write_html(records, html_path, args.year, len(records), (len(tierA), len(tierB), len(tierC)))

    # 终端摘要
    print("=" * 70)
    print(f"全A股评估 {len(records)} 只 → Tier A 可买入 {len(tierA)} | Tier B 优质待跌 {len(tierB)} | Tier C 接近 {len(tierC)}")
    print("=" * 70)
    show = (tierA + tierB)[:args.top]
    print(f"\n【优质榜 Top {len(show)}】(A=可买入 / B=优质待跌)")
    print(f"{'评分':>5} {'层':>2} {'代码':>7} {'名称':<9} {'ROE':>5} {'毛利':>5} {'净利':>5} "
          f"{'同比':>6} {'PE':>6} {'PEG':>5} {'折让%':>6}  {'行业'}")
    for r in show:
        tag = "A" if r["tier"].startswith("A") else "B"
        print(f"{r['score']:>5} {tag:>2} {r['code']:>7} {r['name']:<9} "
              f"{n(r['roe']):>5} {n(r['gross_margin']):>5} {n(r['net_margin']):>5} "
              f"{n(r['yoy']):>6} {n(r['pe_ttm'],1):>6} {n(r['peg'],2):>5} "
              f"{n((r['discount'] or 0)*100):>6}  {r['industry']}")
    print("\n✅ 完整结果：")
    print(f"   网页 (全部可筛选排序): {html_path}")
    print(f"   CSV  (全量+落选原因): {csv_path}")
    print(f"   榜单 (Markdown 表格):  {md_path}")
    print("\n第4层定性把关请人工完成：护城河 / 商业模式 / 能力圈 / 管理层 / 行业朝阳or夕阳。")


if __name__ == "__main__":
    main()
