#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个股深度研报生成器
================================
对选股结果中的优质标的，逐只抓取：
  - 5年营收/净利/ROE/毛利/净利率趋势
  - 资产负债表关键指标（负债率/流动比率/ROA）
  - 现金流量表（经营/投资/筹资现金流）
  - 同行业估值对比
  - DeepSeek AI 定性分析：生意模式、护城河、管理层、成长性、行业地位、风险

输出：一个共享 report.html 页面壳 + 每只股票一个 JSON 数据文件（可点开看完整分析）。

用法：
  python3 stock_deep_dive.py                    # 分析 Tier A+B 全部 ~108 只
  python3 stock_deep_dive.py --code 600519      # 单独分析一只
  python3 stock_deep_dive.py --tier A           # 仅分析 Tier A
  python3 stock_deep_dive.py --no-llm           # 跳过 LLM，仅出量化页
"""

import os, sys, json, time, csv, ssl, argparse, re, html as html_lib
from urllib import request, parse, error
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(WORKDIR, "cache")
OUT_DIR = os.path.join(WORKDIR, "results", "deep_dives")
TEMPLATE_DIR = os.path.join(WORKDIR, "templates", "deep_dive")
DATA_DIR_NAME = "data"
PREFETCH_THRESHOLD = int(os.environ.get("DEEP_PREFETCH_THRESHOLD", "50"))
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_RETRIES = int(os.environ.get("DEEPSEEK_RETRIES", "3"))
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

# DeepSeek API
DEEPSEEK_KEY = None
try:
    kf = os.path.expanduser("~/.config/deepseek/api_key")
    if os.path.exists(kf):
        with open(kf, encoding="utf-8") as f:
            DEEPSEEK_KEY = f.read().strip()
except Exception:
    pass

# ============================================================
# 一、数据获取
# ============================================================
_FINANCIAL_CACHE = None  # 批量预取的财务数据缓存


def prefetch_all_financials(codes=None):
    """批量抓取全市场股票的利润表/资产负债表/现金流量表。
    codes: 需要的股票代码集合，为 None 时取全部。
    存到模块级缓存，后续 fetch_stock_full 直接从内存读取。
    """
    global _FINANCIAL_CACHE
    if _FINANCIAL_CACHE is not None:
        return _FINANCIAL_CACHE

    print("批量预取全市场财务数据（约 2 分钟）...")
    t0 = time.time()

    SHARE_FILTER = '(SECURITY_TYPE_CODE="058001001")'

    def _fetch_all(report_name, columns, filt, label):
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
                print(f"  [{label}] {res.get('count')} 条, {pages} 页")
            out.extend(res.get("data") or [])
            page += 1
            time.sleep(0.03)  # 翻页间隔
        return out

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_inc = ex.submit(_fetch_all,
            "RPT_LICO_FN_CPD",
            "SECURITY_CODE,NOTICE_DATE,REPORTDATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,"
            "WEIGHTAVG_ROE,XSMLL,BASIC_EPS,SJLTZ,DEDUCT_BASIC_EPS,MGJYXJJE,BPS",
            SHARE_FILTER, "利润表")
        f_bal = ex.submit(_fetch_all,
            "RPT_DMSK_FN_BALANCE",
            "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,TOTAL_EQUITY,TOTAL_ASSETS,"
            "DEBT_ASSET_RATIO,CURRENT_RATIO",
            SHARE_FILTER, "资产负债表")
        f_cf = ex.submit(_fetch_all,
            "RPT_DMSK_FN_CASHFLOW",
            "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,NETCASH_OPERATE,NETCASH_INVEST,NETCASH_FINANCE",
            SHARE_FILTER, "现金流量表")
        income_all = f_inc.result()
        balance_all = f_bal.result()
        cashflow_all = f_cf.result()

    # 只索引需要的股票（codes），大幅减少内存占用和索引时间
    code_set = set(codes) if codes else None

    from collections import defaultdict
    def _build_index(rows, key_field="SECURITY_CODE"):
        idx = defaultdict(list)
        for r in rows:
            c = r.get(key_field, "")
            if code_set is None or c in code_set:
                idx[c].append(r)
        return dict(idx)

    _FINANCIAL_CACHE = {
        "income": _build_index(income_all),
        "balance": _build_index(balance_all),
        "cashflow": _build_index(cashflow_all),
    }
    print(f"  预取完成: {len(_FINANCIAL_CACHE['income'])} 只有利润表, "
          f"{len(_FINANCIAL_CACHE['balance'])} 只资产负债表, "
          f"{len(_FINANCIAL_CACHE['cashflow'])} 只现金流")
    print(f"  用时 {time.time()-t0:.1f}s · 后续每只股票从内存读取")
    return _FINANCIAL_CACHE


def should_prefetch_financials(mode, stock_count, is_single_code):
    """判断是否在主流程中预取财务三表。"""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return (not is_single_code) and stock_count >= PREFETCH_THRESHOLD


def http_get(url, retries=5):
    """带重试的 HTTP GET，自适应退避。"""
    last = None
    for i in range(retries):
        try:
            req = request.Request(url, headers=HEADERS)
            with request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            delay = 0.5 * (2 ** i)  # 0.5s, 1s, 2s, 4s, 8s
            time.sleep(delay)
    raise last


def get_json(url):
    raw = http_get(url)
    return json.loads(raw)


def fetch_datacenter(report_name, columns, filt, pagesize=500):
    """东财 datacenter-web 分页抓取。"""
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    out, page, pages = [], 1, 1
    while page <= pages:
        params = {
            "reportName": report_name, "columns": columns,
            "pageSize": pagesize, "pageNumber": page, "filter": filt,
            "sortColumns": "NOTICE_DATE", "sortTypes": -1,
            "source": "WEB", "client": "WEB",
        }
        url = base + "?" + parse.urlencode(params, quote_via=parse.quote)
        d = get_json(url)
        res = d.get("result") or {}
        if page == 1:
            pages = res.get("pages") or 1
        out.extend(res.get("data") or [])
        page += 1
        time.sleep(0.08)
    return out


def fnum(x):
    if x is None or x == "-" or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ============================================================
# 二、单只股票全面数据收集
# ============================================================
def fetch_stock_full(code, name=None, industry=None, csv_rows=None, no_kline=False, spot_cache=None):
    """
    抓取一只股票的全部深度数据。
    name/industry 优先从 CSV 传入（准确），回退到 API。
    csv_rows: 全部 CSV 行，用于同行对比和构建已有研报索引。
    返回 dict，包含财务趋势、行业对比、估值等。
    """
    print(f"  [{code}] 抓取深度数据...", end=" ", flush=True)

    # ── 财务数据：优先从批量预取缓存取（无需 API 调用）──
    if _FINANCIAL_CACHE is not None:
        income = _FINANCIAL_CACHE["income"].get(code, [])
        balance = _FINANCIAL_CACHE["balance"].get(code, [])
        cashflow = _FINANCIAL_CACHE["cashflow"].get(code, [])
    else:
        # 回退：逐只调 API（首次运行或未预取时）
        def _fetch_income():
            return fetch_datacenter(
                "RPT_LICO_FN_CPD",
                "SECURITY_CODE,NOTICE_DATE,REPORTDATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,"
                "WEIGHTAVG_ROE,XSMLL,BASIC_EPS,SJLTZ,DEDUCT_BASIC_EPS,MGJYXJJE,BPS",
                f'(SECURITY_CODE="{code}")', 50)
        def _fetch_balance():
            return fetch_datacenter(
                "RPT_DMSK_FN_BALANCE",
                "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,TOTAL_EQUITY,TOTAL_ASSETS,"
                "DEBT_ASSET_RATIO,CURRENT_RATIO",
                f'(SECURITY_CODE="{code}")', 100)
        def _fetch_cashflow():
            return fetch_datacenter(
                "RPT_DMSK_FN_CASHFLOW",
                "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,NETCASH_OPERATE,NETCASH_INVEST,NETCASH_FINANCE",
                f'(SECURITY_CODE="{code}")', 100)
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_inc, f_bal, f_cf = ex.submit(_fetch_income), ex.submit(_fetch_balance), ex.submit(_fetch_cashflow)
            income = f_inc.result()
            balance = f_bal.result()
            cashflow = f_cf.result()

    # 5) 行情数据：批量抓取全市场行情（clist f12 过滤有 bug 不可靠），
    #    利用 astock_screener.fetch_spot_parallel() + 缓存，秒级取到正确数据。
    if spot_cache is not None:
        spot = spot_cache.get(code, {})
    else:
        spot = {}
        try:
            from astock_screener import fetch_spot_parallel
            all_spot = fetch_spot_parallel()
            spot = all_spot.get(code, {})
        except Exception:
            pass
    # 回退：从 CSV 取上次筛选时的行情
    if not spot and csv_rows:
        my = next((r for r in csv_rows if r["code"] == code), {})
        if my:
            spot = {"f2": my.get("price"), "f115": my.get("pe_ttm"),
                    "f9": None, "f23": my.get("pb"),
                    "f20": str(float(my.get("mktcap_yi") or 0) * 1e8)}

    # 6) K线数据（日K、周K、月K）—— 腾讯财经接口，3个周期并行
    if no_kline:
        kline_data = {"day": [], "week": [], "month": []}
    else:
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
        kline_data = {}

        def _fetch_kline(period_name, period_key, count):
            parsed = []
            try:
                kurl = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                        f"param={prefix}{code},{period_key},,,{count},qfq")
                raw = http_get(kurl)
                kd = json.loads(raw)
                stock_key = f"{prefix}{code}"
                period_data = (kd.get("data") or {}).get(stock_key, {})
                klines = period_data.get(f"qfq{period_key}") or period_data.get(period_key) or []
                for parts in klines:
                    if len(parts) >= 6:
                        try:
                            parsed.append({
                                "date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
                                "high": float(parts[3]), "low": float(parts[4]), "volume": float(parts[5]),
                            })
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass
            return period_name, parsed

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_day, f_week, f_month = (
                ex.submit(_fetch_kline, "day", "day", 250),
                ex.submit(_fetch_kline, "week", "week", 100),
                ex.submit(_fetch_kline, "month", "month", 60),
            )
            for f in (f_day, f_week, f_month):
                try:
                    period, data = f.result()
                    kline_data[period] = data
                except Exception:
                    pass

    # 行情字段（与 astock_screener 的 push2delay clist 格式一致）：
    # f2=现价, f9=PE(动), f23=PB, f20=总市值, f115=PE(TTM)
    def _s(key):
        v = spot.get(key)
        if v is None or v == "-":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # 6) 行业 & 同行：优先用传入值，其次查 CSV
    if not industry or not csv_rows:
        if csv_rows:
            my_row = next((r for r in csv_rows if r["code"] == code), {})
            industry = industry or my_row.get("industry", "")
        else:
            csv_files = sorted([f for f in os.listdir(os.path.join(WORKDIR, "results"))
                               if f.startswith("astock_screen_") and f.endswith(".csv")],
                              reverse=True)
            if csv_files:
                with open(os.path.join(WORKDIR, "results", csv_files[0]), encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        if r["code"] == code:
                            industry = industry or r.get("industry", "")
                            break

    # 7) 同行公司 & 已有研报索引
    existing_deep = _existing_report_codes(OUT_DIR, include_legacy=True)

    peers = []
    if csv_rows and industry:
        for r in csv_rows:
            if r.get("industry") == industry and r["code"] != code:
                pcode = r["code"]
                peers.append({
                    "code": pcode, "name": r["name"],
                    "pe": fnum(r.get("pe_ttm")), "roe": fnum(r.get("roe")),
                    "gm": fnum(r.get("gross_margin")),
                    "mktcap": fnum(r.get("mktcap_yi")),
                    "tier": r.get("tier", ""),
                    "has_deep": pcode in existing_deep,
                })
        peers.sort(key=lambda x: -(x["roe"] or 0))

    result = {
        "code": code,
        "name": name or "",
        "price": _s("f2"),
        "pe_ttm": _s("f115"),
        "pe_dyn": _s("f9"),
        "pb": _s("f23"),
        "mktcap": _s("f20"),
        "industry": industry or "",
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "peers": peers[:10],
        "kline": kline_data,
    }
    print(f"OK ({len(income)}期财报)")
    return result


# ============================================================
# 三、财务指标计算
# ============================================================
def compute_financials(stock):
    """从原始数据中提取关键指标时间序列。"""
    inc = stock["income"]
    bal_data = stock["balance"]
    cf_data = stock["cashflow"]

    # 按报告期排序，取年报（12-31，日期可能含时间戳）
    annual_inc = sorted(
        [r for r in inc if "-12-31" in str(r.get("REPORTDATE", "")) and r.get("TOTAL_OPERATE_INCOME")],
        key=lambda r: str(r.get("REPORTDATE", "")))
    annual_bal = {}
    for r in bal_data:
        rd = str(r.get("REPORT_DATE", r.get("REPORTDATE", "")))
        if "-12-31" in rd:
            annual_bal[rd[:10]] = r
    annual_cf = {}
    for r in cf_data:
        rd = str(r.get("REPORT_DATE", r.get("REPORTDATE", "")))
        if "-12-31" in rd:
            annual_cf[rd[:10]] = r

    years_data = []
    for r in annual_inc[-5:]:  # 最近5年
        rd = str(r.get("REPORTDATE", ""))
        year = rd[:4]
        rev = fnum(r.get("TOTAL_OPERATE_INCOME"))
        netp = fnum(r.get("PARENT_NETPROFIT"))
        roe = fnum(r.get("WEIGHTAVG_ROE"))
        gm = fnum(r.get("XSMLL"))
        eps = fnum(r.get("BASIC_EPS"))
        ocf_ps = fnum(r.get("MGJYXJJE"))
        # 匹配资产负债表和现金流量表（用日期前10位匹配）
        rd_key = rd[:10]
        b = annual_bal.get(rd_key) or {}
        c = annual_cf.get(rd_key) or {}
        debt = fnum(b.get("DEBT_ASSET_RATIO"))
        cur_ratio = fnum(b.get("CURRENT_RATIO"))
        equity = fnum(b.get("TOTAL_EQUITY"))
        assets = fnum(b.get("TOTAL_ASSETS"))
        goodwill = fnum(b.get("GOODWILL"))
        cf_oper = fnum(c.get("NETCASH_OPERATE"))
        cf_invest = fnum(c.get("NETCASH_INVEST"))
        cf_finance = fnum(c.get("NETCASH_FINANCE"))

        nm = (netp / rev * 100) if (rev and netp and rev != 0) else None
        roa = (netp / assets * 100) if (netp and assets and assets != 0) else None
        ocf_ratio = (cf_oper / netp) if (cf_oper and netp and netp != 0) else None

        years_data.append({
            "year": year, "rev": rev, "netp": netp, "roe": roe, "gm": gm,
            "eps": eps, "nm": nm, "ocf_ps": ocf_ps, "ocf_ratio": ocf_ratio,
            "debt": debt, "cur_ratio": cur_ratio, "roa": roa,
            "equity": equity, "assets": assets, "goodwill": goodwill,
            "cf_oper": cf_oper, "cf_invest": cf_invest, "cf_finance": cf_finance,
        })

    # 计算增长率
    if len(years_data) >= 2:
        latest = years_data[-1]
        prev = years_data[-2]
        latest["rev_yoy"] = ((latest["rev"] - prev["rev"]) / prev["rev"] * 100) if (latest["rev"] and prev["rev"] and prev["rev"] != 0) else None
        latest["netp_yoy"] = ((latest["netp"] - prev["netp"]) / prev["netp"] * 100) if (latest["netp"] and prev["netp"] and prev["netp"] != 0) else None
        if len(years_data) >= 4:
            first = years_data[-4]
            n = 3
            cagr_rev = ((latest["rev"] / first["rev"]) ** (1/n) - 1) * 100 if (latest["rev"] and first["rev"] and first["rev"] > 0) else None
            cagr_netp = ((latest["netp"] / first["netp"]) ** (1/n) - 1) * 100 if (latest["netp"] and first["netp"] and first["netp"] > 0) else None
            latest["cagr_rev"] = cagr_rev
            latest["cagr_netp"] = cagr_netp

    return years_data


# ============================================================
# 四、DeepSeek 定性分析
# ============================================================
def deepseek_analyze(stock, financials):
    """调用 DeepSeek API 生成定性分析。"""
    if not DEEPSEEK_KEY:
        return None

    # 构建 prompt
    name = stock["name"]
    code = stock["code"]
    ind = stock["industry"]

    # 财务摘要
    fy = financials[-1] if financials else {}
    finfo = f"""
股票: {name}({code}) | 行业: {ind}
最新年报: {fy.get('year','?')}年
营收: {fy.get('rev','?')} | 净利润: {fy.get('netp','?')} | ROE: {fy.get('roe','?')}% | 毛利率: {fy.get('gm','?')}%
净利率: {fy.get('nm','?')}% | PE(TTM): {stock.get('pe_ttm','?')} | PB: {stock.get('pb','?')}
负债率: {fy.get('debt','?')}% | ROA: {fy.get('roa','?')}%
现金流/净利: {fy.get('ocf_ratio','?')} | 流动比率: {fy.get('cur_ratio','?')}
总市值: {(stock.get('mktcap') or 0)/1e8:.1f}亿

近三年财务趋势:
{chr(10).join(f"  {d['year']}: 营收{d.get('rev','?')} 净利{d.get('netp','?')} ROE{d.get('roe','?')}% 毛利{d.get('gm','?')}%" for d in financials[-3:])}

同行公司（同行业 {ind} 的优质标的）:
{chr(10).join(f"  {p['code']} {p['name']}: PE={p.get('pe','?')} ROE={p.get('roe','?')}% 毛利={p.get('gm','?')}%" for p in stock.get('peers', [])[:5])}
"""

    prompt = f"""你是资深价值投资者，请对以下A股上市公司进行简洁深刻的定性分析。

{finfo}

请按以下结构分析（每项2-3句话，直接讲核心观点，不要铺垫）：

1. 生意模式：这门生意怎么赚钱？轻资产还是重资产？客户粘性如何？定价权强弱？
2. 护城河：竞争优势是什么？品牌/规模/网络效应/技术壁垒/特许经营？壁垒有多深？
3. 成长性：成长驱动力是什么？天花板在哪？是周期性成长还是结构性成长？
4. 行业地位：在行业中排名如何？份额趋势？上下游议价能力？
5. 管理层与治理：股权结构（国资/民企/管理层持股）？历史资本配置能力？分红/回购记录？
6. 风险点：最大的风险是什么？（竞争/政策/技术替代/周期/财务风险），哪些可能形成价值陷阱？
7. 一句话投资逻辑：用一句话概括这只股票的核心投资逻辑。

请严格按以下 JSON 格式输出（不要 Markdown，不要代码块标记）：
{{"business_model":"...","moat":"...","moat_score":8,"growth":"...","industry_position":"...","management":"...","risks":"...","thesis":"...","value_trap_risk":"低/中/高","confidence":"高/中/低","qual_score":85}}"""

    req_payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是资深价值投资者，擅长用第一性原理分析企业基本面。回答用中文，简洁深刻，不做铺垫。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
        "max_tokens": 1500,
        "user_id": "economy_deep_dive",
    }
    if DEEPSEEK_MODEL.startswith("deepseek-v4-"):
        req_payload["thinking"] = {"type": "disabled"}

    last_err = None
    retries = max(1, DEEPSEEK_RETRIES)
    for attempt in range(retries):
        try:
            req_body = json.dumps(req_payload).encode("utf-8")
            api_req = request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=req_body,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_KEY}",
                    "Content-Type": "application/json",
                }
            )
            resp = json.loads(request.urlopen(api_req, timeout=60, context=SSL_CTX).read())
            content = resp["choices"][0]["message"]["content"]

            # 尝试解析 JSON
            content_clean = content.strip()
            if content_clean.startswith("```"):
                content_clean = re.sub(r"^```\w*\n?", "", content_clean)
                content_clean = re.sub(r"\n?```$", "", content_clean)
            return json.loads(content_clean)
        except error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last_err = str(e)
            if attempt == retries - 1:
                break
            time.sleep(1.0 * (attempt + 1))
    print(f"    DeepSeek API 错误: {last_err}")
    return None


def run_deepseek_with_limiter(stock, financials, ai_limiter=None):
    """可选地通过 semaphore 限制 DeepSeek 并发。"""
    if ai_limiter is None:
        return deepseek_analyze(stock, financials)
    ai_limiter.acquire()
    try:
        return deepseek_analyze(stock, financials)
    finally:
        ai_limiter.release()


# ============================================================
# 五、HTML 生成
# ============================================================
def _generate_html_legacy(stock, financials, analysis, output_path, screen_ts=None):
    """旧版自包含 HTML renderer。保留作对照，不再由主流程调用。"""
    name = stock["name"]
    code = stock["code"]
    ind = stock["industry"]
    if not screen_ts:
        screen_ts = sorted([f for f in os.listdir(os.path.join(WORKDIR, "results"))
                           if f.startswith("astock_screen_") and f.endswith(".html")],
                          reverse=True)
        screen_ts = screen_ts[0].replace("astock_screen_", "").replace(".html", "") if screen_ts else time.strftime("%Y%m%d")

    # 最新一期数据
    fy = financials[-1] if financials else {}

    # 数据单位换算
    def r(v, d=1):
        return "" if v is None else f"{v:.{d}f}"

    def rmb(v):
        if v is None: return ""
        if abs(v) >= 1e8: return f"{v/1e8:.2f}亿"
        if abs(v) >= 1e4: return f"{v/1e4:.0f}万"
        return f"{v:.0f}"

    mktcap_yi = (stock.get("mktcap") or 0) / 1e8
    min_buy = int((stock.get("price") or 0) * 100)

    # 财务趋势 JSON（用于前端图表）
    trend_json = json.dumps([{
        "year": d["year"], "rev": d.get("rev"), "netp": d.get("netp"),
        "roe": d.get("roe"), "gm": d.get("gm"), "nm": d.get("nm"),
        "debt": d.get("debt"), "roa": d.get("roa"),
        "ocf": rmb(d.get("cf_oper")), "ocf_ratio": d.get("ocf_ratio"),
    } for d in financials], ensure_ascii=False)

    # K线数据 JSON
    kline_day = stock.get("kline", {}).get("day", [])
    kline_week = stock.get("kline", {}).get("week", [])
    kline_month = stock.get("kline", {}).get("month", [])
    kline_json = json.dumps({"day": kline_day, "week": kline_week, "month": kline_month}, ensure_ascii=False)

    # 同行表格（全部可点击：有研报直接跳转，无研报一键生成）
    peer_rows = ""
    for i, p in enumerate(stock.get("peers", [])[:8]):
        tlabel = {"A_可买入": "🟢A", "B_优质待跌": "🟡B", "C_接近合格": "⚪C"}.get(p["tier"], "")
        pcode = p["code"]
        if p.get("has_deep"):
            name_cell = f'<a href="{pcode}.html" class="code">{pcode}</a> <a href="{pcode}.html" title="查看深度研报">{p["name"]}</a>'
        else:
            name_cell = f'<a href="#" class="code deep-gen" data-code="{pcode}" title="点击生成研报">{pcode}</a> <a href="#" class="deep-gen" data-code="{pcode}" title="点击一键生成研报" style="color:#7fb3ff">{p["name"]}</a> <span style="font-size:10px;color:#5a6270">⚡一键</span>'
        peer_rows += f"""
        <tr>
            <td>{i+1}</td>
            <td>{name_cell}</td>
            <td>{r(p['pe'], 1)}</td>
            <td>{r(p['roe'], 1)}%</td>
            <td>{r(p['gm'], 1)}%</td>
            <td>{r(p['mktcap'], 0)}亿</td>
            <td>{tlabel}</td>
        </tr>"""

    # AI 分析区块
    ai_html = ""
    # 量化速览（零 LLM，自动生成）
    q_roe = fy.get('roe')
    q_gm = fy.get('gm')
    q_debt = fy.get('debt')
    q_ocf = fy.get('ocf_ratio')
    q_yoy = fy.get('netp_yoy')
    q_cagr = fy.get('cagr_netp')
    def judge(v, thresholds, labels):
        if v is None: return ("数据不足", "#5a6270")
        for t, l in zip(thresholds, labels):
            if v >= t: return (l[0], l[1])
        return (labels[-1][0], labels[-1][1])
    roe_j = judge(q_roe, [25, 20, 15], [("卓越", "#3ddc84"), ("优秀", "#7fb3ff"), ("良好", "#ffd166"), ("一般", "#9aa4b2")])
    gm_j  = judge(q_gm, [60, 40, 30], [("强定价权", "#3ddc84"), ("较强", "#7fb3ff"), ("合理", "#ffd166"), ("偏低", "#9aa4b2")])
    debt_j = judge(100 - q_debt, [70, 50, 30], [("极稳健", "#3ddc84"), ("稳健", "#7fb3ff"), ("适中", "#ffd166"), ("偏高", "#ff6b6b")]) if q_debt is not None else ("数据不足", "#5a6270")
    ocf_j = judge(q_ocf, [1.5, 1.0, 0.8], [("现金流充沛", "#3ddc84"), ("健康", "#7fb3ff"), ("合格", "#ffd166"), ("需关注", "#ff6b6b")])

    quant_summary = f"""<div class="quant-summary">
    <div class="qs-item"><span class="qs-label">ROE</span><span class="qs-val" style="color:{roe_j[1]}">{r(q_roe)}%</span><span class="qs-tag" style="color:{roe_j[1]}">{roe_j[0]}</span></div>
    <div class="qs-item"><span class="qs-label">毛利率</span><span class="qs-val" style="color:{gm_j[1]}">{r(q_gm)}%</span><span class="qs-tag" style="color:{gm_j[1]}">{gm_j[0]}</span></div>
    <div class="qs-item"><span class="qs-label">负债率</span><span class="qs-val" style="color:{debt_j[1]}">{r(q_debt)}%</span><span class="qs-tag" style="color:{debt_j[1]}">{debt_j[0]}</span></div>
    <div class="qs-item"><span class="qs-label">现金流</span><span class="qs-val" style="color:{ocf_j[1]}">{r(q_ocf, 2)}x</span><span class="qs-tag" style="color:{ocf_j[1]}">{ocf_j[0]}</span></div>
    <div class="qs-item"><span class="qs-label">净利增速</span><span class="qs-val">{r(q_yoy)}%</span><span class="qs-tag" style="color:{'#3ddc84' if (q_yoy or 0) >= 20 else '#ffd166' if (q_yoy or 0) >= 10 else '#9aa4b2'}">{'高增长' if (q_yoy or 0) >= 30 else '稳健' if (q_yoy or 0) >= 10 else '平缓'}</span></div>
    <div class="qs-item"><span class="qs-label">3年CAGR</span><span class="qs-val">{r(q_cagr)}%</span><span class="qs-tag">{'高成长' if (q_cagr or 0) >= 25 else '稳定' if (q_cagr or 0) >= 10 else '低速'}</span></div>
</div>"""

    if analysis:
        moat_color = {"高": "#3ddc84", "中": "#ffd166", "低": "#ff6b6b"}.get(
            analysis.get("moat_score", 0) >= 7 and "高" or (analysis.get("moat_score", 0) >= 5 and "中" or "低"), "#5a6270")
        trap_color = {"低": "#3ddc84", "中": "#ffd166", "高": "#ff6b6b"}.get(analysis.get("value_trap_risk", "中"), "#ffd166")
        conf_color = {"高": "#3ddc84", "中": "#ffd166", "低": "#ff6b6b"}.get(analysis.get("confidence", "中"), "#ffd166")

        ai_html = f"""
    <div class="section">
        <h2>🤖 AI 定性分析 (DeepSeek)</h2>
        <div class="ai-meta">
            <span>护城河评分: <b style="color:{moat_color}">{analysis.get('moat_score', '?')}/10</b></span>
            <span>综合定性分: <b style="color:#3a86ff">{analysis.get('qual_score', '?')}/100</b></span>
            <span>价值陷阱风险: <b style="color:{trap_color}">{analysis.get('value_trap_risk', '?')}</b></span>
            <span>分析信心: <b style="color:{conf_color}">{analysis.get('confidence', '?')}</b></span>
        </div>
        <div class="ai-grid">
            <div class="ai-card">
                <h4>🏗️ 生意模式</h4>
                <p>{analysis.get('business_model', '暂无')}</p>
            </div>
            <div class="ai-card">
                <h4>🛡️ 护城河</h4>
                <p>{analysis.get('moat', '暂无')}</p>
            </div>
            <div class="ai-card">
                <h4>📈 成长性</h4>
                <p>{analysis.get('growth', '暂无')}</p>
            </div>
            <div class="ai-card">
                <h4>🏭 行业地位</h4>
                <p>{analysis.get('industry_position', '暂无')}</p>
            </div>
            <div class="ai-card">
                <h4>👥 管理层与治理</h4>
                <p>{analysis.get('management', '暂无')}</p>
            </div>
            <div class="ai-card risk">
                <h4>⚠️ 风险点</h4>
                <p>{analysis.get('risks', '暂无')}</p>
            </div>
        </div>
        <div class="thesis">
            <h4>💡 一句话投资逻辑</h4>
            <p>「{analysis.get('thesis', '暂无')}」</p>
        </div>
    </div>"""
    else:
        ai_html = f"""
    <div class="section">
        <h2>🤖 AI 定性分析</h2>
        <div id="aiPlaceholder" style="background:#131820;border:1px dashed #2a3140;border-radius:10px;padding:20px;text-align:center;color:#6b7380">
            <p style="font-size:15px;margin:0 0 8px">AI 定性分析未运行</p>
            <p style="font-size:12px;margin:0 0 12px">DeepSeek 将对生意模式、护城河、管理层、成长性、行业地位、风险做深度分析（约 5-10 秒）</p>
            <button id="aiAnalyzeBtn" style="background:#1d2033;border:1px solid #3a86ff;color:#7fb3ff;padding:8px 20px;border-radius:7px;cursor:pointer;font-size:14px"
                    onclick="runAiAnalysis('{code}')">🤖 开始 AI 分析</button>
            <div id="aiProgress" style="margin-top:10px;display:none;color:#7fb3ff;font-size:12px">⏳ 分析中…</div>
        </div>
    </div>
    <script>
    var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;
    function runAiAnalysis(code){{
     var btn=document.getElementById('aiAnalyzeBtn');
     var prog=document.getElementById('aiProgress');
     if(!btn)return;
     btn.disabled=true;btn.textContent='⏳ 分析中…';prog.style.display='block';
     fetch(API+'/api/deep?code='+code)
      .then(function(r){{return r.json()}})
      .then(function(d){{
       if(d.done){{prog.textContent='✅ 完成！正在刷新…';setTimeout(function(){{location.reload()}},600)}}
       else{{prog.textContent='⚠️ 失败: '+(d.error||'未知');btn.disabled=false;btn.textContent='🤖 重试'}}
      }})
      .catch(function(e){{prog.textContent='⚠️ 无法连接服务，请通过 ./run.sh 打开 HTTP 页面';btn.disabled=false;btn.textContent='🤖 重试'}});
    }}
    </script>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}({code}) 深度研报 | 五层选股</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e8eb;font-size:14px;line-height:1.6}}
.container{{max-width:1100px;margin:0 auto;padding:24px 20px}}
header{{padding:24px 0;border-bottom:1px solid #232936;margin-bottom:24px}}
header h1{{margin:0;font-size:26px;color:#fff}}
header .sub{{color:#8b93a1;font-size:14px;margin-top:6px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:28px}}
.kpi{{background:#131820;border:1px solid #1e2634;border-radius:10px;padding:14px;text-align:center}}
.kpi .val{{font-size:24px;font-weight:700}}
.kpi .lbl{{font-size:11px;color:#6b7380;margin-top:4px}}
.kpi.green .val{{color:#3ddc84}}.kpi.yellow .val{{color:#ffd166}}.kpi.blue .val{{color:#3a86ff}}.kpi.red .val{{color:#ff6b6b}}
/* 量化速览 */
.quant-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:28px}}
.qs-item{{background:#131820;border:1px solid #1e2634;border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:8px}}
.qs-label{{color:#6b7380;font-size:11px;min-width:42px}}
.qs-val{{font-size:18px;font-weight:700}}
.qs-tag{{font-size:10px;padding:2px 6px;border-radius:3px;margin-left:auto;background:#1a1f29}}
/* K线周期按钮 */
.kline-bar{{display:flex;gap:6px;margin-bottom:12px}}
.kline-bar button{{background:#1a1f29;border:1px solid #2a3140;color:#8b93a1;padding:5px 14px;border-radius:5px;cursor:pointer;font-size:12px}}
.kline-bar button.on{{background:#3a86ff;border-color:#3a86ff;color:#fff}}
.section{{margin:28px 0}}
.section h2{{font-size:18px;border-bottom:1px solid #1e2634;padding-bottom:8px;margin-bottom:16px}}
canvas{{width:100%;height:280px;border-radius:8px;background:#131820}}
.peer-table{{width:100%;border-collapse:collapse;font-size:12px}}
.peer-table th{{background:#1a1f29;color:#8b93a1;padding:8px 12px;text-align:left;border-bottom:1px solid #2a3140}}
.peer-table td{{padding:6px 12px;border-bottom:1px solid #1c212b}}
.peer-table tr:hover td{{background:#161b24}}
.code{{color:#7fb3ff;font-variant-numeric:tabular-nums}}
/* AI 区块 */
.ai-meta{{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:16px;font-size:13px}}
.ai-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.ai-card{{background:#131820;border:1px solid #1e2634;border-radius:10px;padding:16px}}
.ai-card h4{{margin:0 0 8px;font-size:14px;color:#c7cdd6}}
.ai-card p{{margin:0;font-size:13px;color:#9aa4b2;line-height:1.7}}
.ai-card.risk{{border-color:#3d1f1f}}
.thesis{{background:linear-gradient(135deg,#1a2a1a,#131820);border:1px solid #1e3e1e;border-radius:10px;padding:18px;margin-top:16px}}
.thesis h4{{margin:0 0 6px;color:#3ddc84}}
.thesis p{{margin:0;font-size:15px;color:#e6e8eb;font-style:italic}}
/* 财务表格 */
.fin-table{{width:100%;border-collapse:collapse;font-size:11px}}
.fin-table th{{background:#1a1f29;color:#8b93a1;padding:6px 10px;text-align:right;border-bottom:1px solid #2a3140}}
.fin-table th.l{{text-align:left}}
.fin-table td{{padding:5px 10px;text-align:right;border-bottom:1px solid #1c212b}}
.fin-table td.l{{text-align:left}}
.fin-table tr:hover td{{background:#161b24}}
.pos{{color:#3ddc84}}.neg{{color:#ff6b6b}}
footer{{margin-top:40px;padding:16px 0;border-top:1px solid #232936;color:#5a6270;font-size:11px}}
.back{{color:#7fb3ff;text-decoration:none;font-size:13px}}
.back:hover{{text-decoration:underline}}
@media(max-width:768px){{.container{{padding:12px}} .ai-grid{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="container">
<header>
    <a href="../astock_screen_{screen_ts}.html" class="back">← 回到选股总表</a>
    <h1>{name} <span style="font-size:16px;color:#8b93a1">{code}</span></h1>
    <div class="sub">{ind} · {'  |  '.join(str(d['year']) for d in financials[-3:])}年</div>
</header>

<!-- KPI 卡片 -->
<div class="kpis">
    <div class="kpi green"><div class="val">{r(stock.get('price'), 2)}</div><div class="lbl">现价(元)</div></div>
    <div class="kpi yellow"><div class="val">{min_buy}</div><div class="lbl">一手(元)</div></div>
    <div class="kpi blue"><div class="val">{r(stock.get('pe_ttm'), 1)}</div><div class="lbl">PE(TTM)</div></div>
    <div class="kpi blue"><div class="val">{r(stock.get('pb'), 1)}</div><div class="lbl">PB</div></div>
    <div class="kpi green"><div class="val">{r(fy.get('roe'), 1)}%</div><div class="lbl">ROE</div></div>
    <div class="kpi green"><div class="val">{r(fy.get('gm'), 1)}%</div><div class="lbl">毛利率</div></div>
    <div class="kpi green"><div class="val">{r(fy.get('nm'), 1)}%</div><div class="lbl">净利率</div></div>
    <div class="kpi yellow"><div class="val">{mktcap_yi:.0f}亿</div><div class="lbl">总市值</div></div>
    <div class="kpi"><div class="val">{r(fy.get('debt'), 1)}%</div><div class="lbl">负债率</div></div>
    <div class="kpi"><div class="val">{r(fy.get('roa'), 1)}%</div><div class="lbl">ROA</div></div>
</div>

<!-- 量化速览 -->
{quant_summary}

<!-- K线图 -->
<div class="section">
    <h2>📈 股价走势 (前复权)</h2>
    <div class="kline-bar">
        <button class="on" onclick="switchKline('day', this)">日K</button>
        <button onclick="switchKline('week', this)">周K</button>
        <button onclick="switchKline('month', this)">月K</button>
        <span style="color:#6b7380;font-size:11px;margin-left:12px;line-height:28px">
            MA5 <span style="color:#ffe066">──</span>
            MA10 <span style="color:#ff9f1c">──</span>
            MA20 <span style="color:#e15554">──</span>
            MA60 <span style="color:#4e9f3d">──</span>
        </span>
    </div>
    <canvas id="cvKline"></canvas>
</div>

<!-- 财务趋势图表 -->
<div class="section">
    <h2>📊 5年财务趋势</h2>
    <canvas id="cvTrend"></canvas>
</div>

<!-- 详细财务数据表 -->
<div class="section">
    <h2>📋 历年财务数据</h2>
    <div style="overflow:auto">
    <table class="fin-table">
    <thead><tr>
        <th class="l">年度</th><th>营收(亿)</th><th>净利(亿)</th><th>ROE%</th><th>毛利%</th><th>净利率%</th>
        <th>ROA%</th><th>负债%</th><th>EPS</th><th>经营现金流</th><th>现金流/净利</th>
    </tr></thead>
    <tbody>
    {''.join(f'''<tr>
        <td class="l">{d['year']}</td>
        <td>{rmb(d.get('rev'))}</td>
        <td>{rmb(d.get('netp'))}</td>
        <td class="{'pos' if d.get('roe',0) and d['roe']>=15 else ''}">{r(d.get('roe'))}%</td>
        <td>{r(d.get('gm'))}%</td>
        <td class="{'pos' if d.get('nm',0) and d['nm']>=10 else ''}">{r(d.get('nm'))}%</td>
        <td>{r(d.get('roa'))}%</td>
        <td>{r(d.get('debt'))}%</td>
        <td>{r(d.get('eps'), 2)}</td>
        <td class="{'pos' if d.get('cf_oper',0) and d['cf_oper']>0 else 'neg'}">{rmb(d.get('cf_oper'))}</td>
        <td class="{'pos' if d.get('ocf_ratio',0) and d['ocf_ratio']>=0.8 else 'neg'}">{r(d.get('ocf_ratio'), 2)}</td>
    </tr>''' for d in reversed(financials))}
    </tbody></table></div>
</div>

<!-- 同行对比 -->
<div class="section">
    <h2>🏭 同行对比（同行业 {ind} 优质标的）</h2>
    <table class="peer-table">
    <thead><tr>
        <th>#</th><th>代码/名称</th><th>PE</th><th>ROE%</th><th>毛利%</th><th>市值(亿)</th><th>评级</th>
    </tr></thead>
    <tbody>{peer_rows}</tbody></table>
</div>

<!-- AI 分析 -->
{ai_html}

<footer>
    数据来源：东方财富公开接口 · AI分析由 DeepSeek 生成，仅供参考不构成投资建议 · 生成于 {time.strftime('%Y-%m-%d %H:%M')}
</footer>
</div>

<script>
// K线数据
var KDATA={kline_json};
var curPeriod='day';
var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;

function switchKline(p, btn){{
 curPeriod=p;
 document.querySelectorAll('.kline-bar button').forEach(function(b){{b.classList.remove('on')}});
 if(btn)btn.classList.add('on');
 drawKline();
}}

function calcMA(data, n){{
 var result=[];
 for(var i=0;i<data.length;i++){{
  if(i<n-1){{result.push(null);continue}}
  var sum=0;for(var j=i-n+1;j<=i;j++) sum+=data[j].close;
  result.push(sum/n);
 }}
 return result;
}}

function drawKline(){{
 var data=KDATA[curPeriod];
 if(!data||data.length===0) return;
 var cv=document.getElementById('cvKline');
 var W=cv.parentElement.clientWidth-4,H=380;
 cv.width=W*2;cv.height=H*2;
 cv.style.width=W+'px';cv.style.height=H+'px';
 var ctx=cv.getContext('2d');
 ctx.scale(2,2);

 var n=data.length;
 var chartH=H*0.68,volH=H*0.18,padL=50,padR=10,padT=10;

 // 计算价格范围
 var maxH=data[0].high,minL=data[0].low;
 for(var i=0;i<n;i++){{
  if(data[i].high>maxH) maxH=data[i].high;
  if(data[i].low<minL) minL=data[i].low;
 }}
 var maxV=0;for(var i=0;i<n;i++){{if(data[i].volume>maxV)maxV=data[i].volume;}}
 maxH=maxH*1.02;minL=minL*0.98;
 var priceRange=maxH-minL;

 // MAs
 var ma5=calcMA(data,5),ma10=calcMA(data,10),ma20=calcMA(data,20),ma60=calcMA(data,60);
 var barW=(W-padL-padR)/n*0.7;
 var gap=(W-padL-padR)/n;
 var candleW=Math.max(1,barW*0.6);

 // X轴标签（只显示部分日期）
 var skip=Math.max(1,Math.floor(n/8));

 // 清除
 ctx.clearRect(0,0,W,H);

 // 网格线
 ctx.strokeStyle='#1a1f29';ctx.lineWidth=0.5;
 for(var i=0;i<=4;i++){{
  var y=padT+chartH*i/4;
  ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(W-padR,y);ctx.stroke();
  ctx.fillStyle='#5a6270';ctx.font='9px sans-serif';ctx.textAlign='right';
  var p=maxH-priceRange*i/4;
  ctx.fillText(p.toFixed(2),padL-4,y+3);
 }}

 // 成交量柱
 for(var i=0;i<n;i++){{
  var x=padL+i*gap;
  var vh=data[i].volume/maxV*volH;
  var vy=padT+chartH+10+volH-vh;
  var c=data[i].close>=data[i].open?'rgba(61,220,132,0.35)':'rgba(255,107,107,0.35)';
  ctx.fillStyle=c;ctx.fillRect(x-candleW/2,vy,candleW,vh);
 }}

 // 成交量标签
 ctx.fillStyle='#5a6270';ctx.font='9px sans-serif';ctx.textAlign='right';
 ctx.fillText('VOL',padL-4,padT+chartH+20);

 // 蜡烛图
 for(var i=0;i<n;i++){{
  var x=padL+i*gap;
  var oy=padT+(maxH-data[i].open)/priceRange*chartH;
  var cy=padT+(maxH-data[i].close)/priceRange*chartH;
  var hy=padT+(maxH-data[i].high)/priceRange*chartH;
  var ly=padT+(maxH-data[i].low)/priceRange*chartH;
  var up=data[i].close>=data[i].open;
  ctx.strokeStyle=up?'#3ddc84':'#ff6b6b';
  ctx.fillStyle=up?'#3ddc84':(data[i].close<data[i].open?'#ff6b6b':'#3ddc84');
  // 影线
  ctx.beginPath();ctx.moveTo(x,hy);ctx.lineTo(x,ly);ctx.stroke();
  // 实体
  var bodyH=Math.max(1,Math.abs(cy-oy));
  var bodyY=Math.min(oy,cy);
  ctx.fillRect(x-candleW/2,bodyY,candleW,bodyH);
 }}

 // MA线
 function drawMA(ma,color,lw){{
  ctx.strokeStyle=color;ctx.lineWidth=lw;
  ctx.beginPath();var started=false;
  for(var i=0;i<n;i++){{
   if(ma[i]===null) continue;
   var x=padL+i*gap,y=padT+(maxH-ma[i])/priceRange*chartH;
   if(!started){{ctx.moveTo(x,y);started=true;}}else ctx.lineTo(x,y);
  }}
  ctx.stroke();
 }}
 drawMA(ma5,'#ffe066',1.2);
 drawMA(ma10,'#ff9f1c',1.2);
 drawMA(ma20,'#e15554',1.2);
 drawMA(ma60,'#4e9f3d',1.2);

 // X轴日期
 ctx.fillStyle='#6b7380';ctx.font='9px sans-serif';ctx.textAlign='center';
 for(var i=0;i<n;i+=skip){{
  var x=padL+i*gap;
  ctx.fillText(data[i].date.slice(5),x,padT+chartH+volH+22);
 }}
}}

// 初始化K线
drawKline();

// K线交互：十字线直接画在主canvas上（零图层），tooltip在canvas下方
var crossIdx=-1;
var tipK=document.createElement('div');
tipK.style.cssText='margin-top:4px;padding:6px 10px;background:#131820;border:1px solid #232936;border-radius:6px;font-size:11px;color:#8b93a1;text-align:center;line-height:1.5;min-height:20px';
cvKline.parentElement.appendChild(tipK);
tipK.innerHTML='在K线上点击查看详情';

function klineHit(e){{
 var data=KDATA[curPeriod];if(!data||!data.length)return-1;
 var rect=cvKline.getBoundingClientRect();
 var W=cvKline.parentElement.clientWidth-4,padL=50,padR=10;
 var n=data.length,gap=(W-padL-padR)/n;
 var idx=Math.round((e.clientX-rect.left-padL)/gap);
 return (idx>=0&&idx<n)?idx:-1;
}}

function showTip(idx,pinned){{
 if(idx<0||!KDATA[curPeriod]||!KDATA[curPeriod][idx]){{tipK.innerHTML='在K线上点击查看详情';return}}
 var d=KDATA[curPeriod][idx];
 var up=d.close>=d.open,chg=((d.close-d.open)/d.open*100).toFixed(2);
 tipK.innerHTML='<b style=\"color:#fff\">'+d.date+'</b>'
  +' 开<b>'+d.open.toFixed(2)+'</b> 收<b style=\"color:'+(up?'#3ddc84':'#ff6b6b')+'\">'+d.close.toFixed(2)+'</b>'
  +' 高'+d.high.toFixed(2)+' 低'+d.low.toFixed(2)
  +' <span style=\"color:'+(up?'#3ddc84':'#ff6b6b')+'\">'+(up?'+':'')+chg+'%</span>'
  +' 量'+(d.volume/10000).toFixed(0)+'万手'
  +(pinned?' <span style=\"color:#6b7380;font-size:10px\">·已固定·点击取消</span>':'');
}}

// 扩展drawKline：在尾部追加十字线
// 注意：_origDraw 内部会 cv.width=W*2 清空canvas 并 ctx.scale(2,2) 绘制，结束后不 restore
// 所以这里需要重置变换再画十字线
var _origDraw=drawKline;
drawKline=function(){{
 _origDraw();
 if(crossIdx<0||!KDATA[curPeriod]||!KDATA[curPeriod][crossIdx])return;
 var data=KDATA[curPeriod],H=380,W=cvKline.parentElement.clientWidth-4;
 var ctx=cvKline.getContext('2d');
 // 重置变换矩阵后重新设置 2x HiDPI
 ctx.setTransform(1,0,0,1,0,0);
 ctx.scale(2,2);
 var chartH=H*0.68,padL=50,padR=10,padT=10;
 var n=data.length,gap=(W-padL-padR)/n;
 var x=padL+crossIdx*gap;
 var maxH=data[0].high,minL=data[0].low;
 for(var i=0;i<n;i++){{if(data[i].high>maxH)maxH=data[i].high;if(data[i].low<minL)minL=data[i].low;}}
 maxH*=1.02;minL*=0.98;
 var cy=padT+(maxH-data[crossIdx].close)/(maxH-minL)*chartH;
 // 极淡竖虚线 + 小圆点
 ctx.strokeStyle='rgba(58,134,255,0.22)';ctx.lineWidth=1;ctx.setLineDash([3,5]);
 ctx.beginPath();ctx.moveTo(x,padT);ctx.lineTo(x,padT+chartH);ctx.stroke();
 ctx.setLineDash([]);
 ctx.fillStyle='rgba(58,134,255,0.7)';ctx.beginPath();ctx.arc(x,cy,3,0,Math.PI*2);ctx.fill();
}};

cvKline.addEventListener('click',function(e){{
 var idx=klineHit(e);
 if(idx===crossIdx){{crossIdx=-1;showTip(-1,false);drawKline();return}}
 crossIdx=idx;showTip(idx,true);drawKline();
}});
cvKline.addEventListener('mousemove',function(e){{
 if(crossIdx>=0)return;var idx=klineHit(e);showTip(idx,false);
}});
cvKline.addEventListener('mouseleave',function(){{if(crossIdx<0)tipK.innerHTML='在K线上点击查看详情'}});

// ---- 一键生成研报：所有 .deep-gen 链接 ----
document.querySelectorAll('.deep-gen').forEach(function(el){{
 el.addEventListener('click',function(e){{
  e.preventDefault();
  var code=el.getAttribute('data-code');
  el.textContent='⏳';el.style.pointerEvents='none';
  fetch(API+'/api/deep?code='+code)
   .then(function(r){{return r.json()}})
   .then(function(d){{
    if(d.done){{location.href=code+'.html';}}
    else{{el.textContent=code;el.style.pointerEvents='auto';alert('生成失败');}}
   }})
   .catch(function(e){{el.textContent=code;el.style.pointerEvents='auto';alert('无法连接本地服务，请通过 ./run.sh 打开 HTTP 页面');}});
 }});
}});

// 财务趋势图表
(function(){{
var data = {trend_json};
var cv = document.getElementById('cvTrend');
var W = cv.parentElement.clientWidth - 4, H = 280;
cv.width = W * 2; cv.height = H * 2;
cv.style.width = W + 'px'; cv.style.height = H + 'px';
var ctx = cv.getContext('2d');
ctx.scale(2, 2);

// 绘制双轴图：营收+净利柱状图, ROE+毛利率折线
var years = data.map(function(d){{return d.year}});
var revs = data.map(function(d){{return d.rev ? d.rev/1e8 : 0}});
var netps = data.map(function(d){{return d.netp ? d.netp/1e8 : 0}});
var roes = data.map(function(d){{return d.roe || 0}});
var gms = data.map(function(d){{return d.gm || 0}});

var maxRev = Math.max.apply(null, revs.concat([1]));
var maxPct = Math.max.apply(null, roes.concat(gms).concat([50]));

var n = data.length;
var barW = (W - 100) / n * 0.35;
var groupW = (W - 100) / n;

// 营收柱
for(var i = 0; i < n; i++){{
    var bh = revs[i] / maxRev * (H - 80), x = 60 + i * groupW, y = H - 40 - bh;
    ctx.fillStyle = 'rgba(58,134,255,0.7)';
    ctx.fillRect(x - barW, y, barW, bh);
    ctx.fillStyle = '#7fb3ff'; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText((revs[i]||0).toFixed(1), x - barW/2, y - 4);
}}
// 净利柱
for(var i = 0; i < n; i++){{
    var bh = netps[i] / maxRev * (H - 80), x = 60 + i * groupW, y = H - 40 - bh;
    ctx.fillStyle = 'rgba(61,220,132,0.7)';
    ctx.fillRect(x, y, barW, bh);
    ctx.fillStyle = '#3ddc84'; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText((netps[i]||0).toFixed(1), x + barW/2, y - 4);
}}

// ROE 折线
ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 2;
ctx.beginPath();
for(var i = 0; i < n; i++){{
    var x = 60 + i * groupW, y = H - 40 - (roes[i] / maxPct * (H - 80));
    if(i == 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
}}
ctx.stroke();
// ROE 点
for(var i = 0; i < n; i++){{
    var x = 60 + i * groupW, y = H - 40 - (roes[i] / maxPct * (H - 80));
    ctx.fillStyle = '#ffd166'; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = '#ffd166'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(roes[i]+'%', x, y - 8);
}}

// 毛利率折线
ctx.strokeStyle = '#9aa4b2'; ctx.lineWidth = 2; ctx.setLineDash([4,3]);
ctx.beginPath();
for(var i = 0; i < n; i++){{
    var x = 60 + i * groupW, y = H - 40 - (gms[i] / maxPct * (H - 80));
    if(i == 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
}}
ctx.stroke();
ctx.setLineDash([]);

// 图例
ctx.fillStyle = '#7fb3ff'; ctx.fillRect(60, H-20, 10, 10);
ctx.fillStyle = '#8b93a1'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
ctx.fillText('营收(亿)', 74, H-11);
ctx.fillStyle = '#3ddc84'; ctx.fillRect(130, H-20, 10, 10);
ctx.fillText('净利(亿)', 144, H-11);
ctx.strokeStyle = '#ffd166'; ctx.beginPath(); ctx.moveTo(210, H-15); ctx.lineTo(230, H-15); ctx.stroke();
ctx.fillText('ROE%', 234, H-11);
ctx.strokeStyle = '#9aa4b2'; ctx.setLineDash([4,3]); ctx.beginPath(); ctx.moveTo(290, H-15); ctx.lineTo(310, H-15); ctx.stroke();
ctx.setLineDash([]);
ctx.fillText('毛利率%', 314, H-11);

// X轴标签
ctx.fillStyle = '#6b7380'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
for(var i = 0; i < n; i++){{
    ctx.fillText(years[i], 60 + i * groupW, H - 4);
}}
}})();
</script>
</body></html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _base_dir_from_output_path(output_path):
    """兼容旧调用签名：传入 XXXXXX.html 或 data/XXXXXX.json 都归一到 deep_dives 目录。"""
    base_dir = os.path.dirname(os.path.abspath(output_path))
    if os.path.basename(base_dir) == DATA_DIR_NAME:
        base_dir = os.path.dirname(base_dir)
    return base_dir


def _write_text_if_changed(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == content:
                return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def latest_screen_ts(results_dir=None):
    """返回当前 results 目录里最新的选股总表日期。"""
    results_dir = results_dir or os.path.join(WORKDIR, "results")
    screens = sorted(
        [
            f for f in os.listdir(results_dir)
            if f.startswith("astock_screen_") and f.endswith(".html")
        ],
        reverse=True,
    ) if os.path.isdir(results_dir) else []
    return screens[0].replace("astock_screen_", "").replace(".html", "") if screens else time.strftime("%Y%m%d")


def ensure_deep_dive_app(base_dir):
    """把共享 report shell 和静态 assets 写入 deep_dives 目录。"""
    os.makedirs(os.path.join(base_dir, DATA_DIR_NAME), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "assets"), exist_ok=True)
    files = {
        "report.html": os.path.join(TEMPLATE_DIR, "report.html"),
        os.path.join("assets", "deep_dive.css"): os.path.join(TEMPLATE_DIR, "assets", "deep_dive.css"),
        os.path.join("assets", "deep_dive.js"): os.path.join(TEMPLATE_DIR, "assets", "deep_dive.js"),
    }
    screen_href = f"astock_screen_{latest_screen_ts(os.path.dirname(base_dir))}.html"
    for rel, src in files.items():
        with open(src, encoding="utf-8") as f:
            content = f.read().replace("__SCREEN_HREF__", screen_href)
            _write_text_if_changed(os.path.join(base_dir, rel), content)


def _existing_report_codes(base_dir, include_legacy=False):
    codes = set()
    data_dir = os.path.join(base_dir, DATA_DIR_NAME)
    if os.path.isdir(data_dir):
        for f in os.listdir(data_dir):
            if len(f) == 11 and f.endswith(".json") and f[:6].isdigit():
                codes.add(f[:6])
    if include_legacy and os.path.isdir(base_dir):
        for f in os.listdir(base_dir):
            if len(f) == 11 and f.endswith(".html") and f[:6].isdigit():
                codes.add(f[:6])
    return codes


def _report_has_analysis(base_dir, code):
    path = os.path.join(base_dir, DATA_DIR_NAME, f"{code}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return bool(payload.get("analysis"))
    except (OSError, json.JSONDecodeError):
        return False


def build_deep_dive_payload(stock, financials, analysis, screen_ts=None):
    """构建前端 report shell 使用的纯数据 payload。"""
    code = stock["code"]
    if not screen_ts:
        screen_ts = sorted([f for f in os.listdir(os.path.join(WORKDIR, "results"))
                           if f.startswith("astock_screen_") and f.endswith(".html")],
                          reverse=True)
        screen_ts = screen_ts[0].replace("astock_screen_", "").replace(".html", "") if screen_ts else time.strftime("%Y%m%d")

    price = stock.get("price") or 0
    mktcap = stock.get("mktcap") or 0
    return {
        "meta": {
            "code": code,
            "name": stock.get("name") or "",
            "industry": stock.get("industry") or "",
            "screen_ts": screen_ts,
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        },
        "quote": {
            "price": stock.get("price"),
            "min_buy": int(price * 100),
            "pe_ttm": stock.get("pe_ttm"),
            "pe_dyn": stock.get("pe_dyn"),
            "pb": stock.get("pb"),
            "mktcap": mktcap,
            "mktcap_yi": mktcap / 1e8 if mktcap else None,
        },
        "financials": financials,
        "peers": stock.get("peers", []),
        "kline": stock.get("kline", {"day": [], "week": [], "month": []}),
        "analysis": analysis,
    }


def _payload_path_for_code(code, base_dir=None):
    return os.path.join(base_dir or OUT_DIR, DATA_DIR_NAME, f"{code}.json")


def _stock_from_payload(payload):
    meta = payload.get("meta") or {}
    quote = payload.get("quote") or {}
    return {
        "code": meta.get("code") or "",
        "name": meta.get("name") or "",
        "industry": meta.get("industry") or "",
        "price": quote.get("price"),
        "pe_ttm": quote.get("pe_ttm"),
        "pe_dyn": quote.get("pe_dyn"),
        "pb": quote.get("pb"),
        "mktcap": quote.get("mktcap"),
        "peers": payload.get("peers") or [],
        "kline": payload.get("kline") or {"day": [], "week": [], "month": []},
    }


def run_ai_only_for_existing_report(code, ai_limiter=None):
    """只对已有 JSON 研报补 DeepSeek 分析，不重新抓财务/行情/K线。"""
    data_path = _payload_path_for_code(code)
    if not os.path.exists(data_path):
        print(f"    [{code}] 无已有 JSON 研报，跳过 AI-only")
        return False
    with open(data_path, encoding="utf-8") as f:
        payload = json.load(f)
    stock = _stock_from_payload(payload)
    financials = payload.get("financials") or []
    analysis = run_deepseek_with_limiter(stock, financials, ai_limiter)
    if not analysis:
        return False
    payload["analysis"] = analysis
    payload.setdefault("meta", {})["generated_at"] = time.strftime("%Y-%m-%d %H:%M")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    ensure_deep_dive_app(OUT_DIR)
    return True


def generate_html(stock, financials, analysis, output_path, screen_ts=None):
    """生成 JSON-backed 个股研报：共享 report.html + data/XXXXXX.json。"""
    base_dir = _base_dir_from_output_path(output_path)
    ensure_deep_dive_app(base_dir)

    payload = build_deep_dive_payload(stock, financials, analysis, screen_ts)
    data_path = os.path.join(base_dir, DATA_DIR_NAME, f"{stock['code']}.json")
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    # 旧版按 XXXXXX.html 输出；新架构不再保留每只股票一份 HTML。
    if output_path.endswith(".html") and os.path.basename(output_path) not in ("index.html", "report.html"):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
    return data_path


# ============================================================
# 六、生成索引页
# ============================================================
def generate_index(stocks_done, base_dir, screen_ts=None):
    """生成个股研报索引页面，按评分排序，可点击跳转。"""
    ensure_deep_dive_app(base_dir)
    if not screen_ts:
        screen_ts = sorted([f for f in os.listdir(os.path.join(WORKDIR, "results"))
                           if f.startswith("astock_screen_") and f.endswith(".html")],
                          reverse=True)
        screen_ts = screen_ts[0].replace("astock_screen_", "").replace(".html", "") if screen_ts else time.strftime("%Y%m%d")

    def first_value(stock, *keys):
        for key in keys:
            value = stock.get(key)
            if value not in (None, "", "None", "nan"):
                return value
        return ""

    def fmt_plain(value):
        if value in (None, "", "None", "nan"):
            return ""
        return html_lib.escape(str(value))

    def fmt_number(value, digits=None):
        if value in (None, "", "None", "nan"):
            return ""
        try:
            num = float(value)
            if digits is None:
                text = f"{num:g}"
            else:
                text = f"{num:.{digits}f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            text = str(value)
        return html_lib.escape(text)

    def fmt_unit(value, unit, digits=None):
        if value in (None, "", "None", "nan"):
            return ""
        return f"{fmt_number(value, digits)}{unit}"

    def fmt_market_cap(stock):
        value = first_value(stock, "mktcap", "mktcap_yi", "market_cap_yi")
        if value in (None, "", "None", "nan"):
            return ""
        try:
            num = float(value)
            if abs(num) > 1_000_000:
                num = num / 1e8
            text = f"{num:.1f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            text = str(value)
        return f"{html_lib.escape(text)}亿"

    rows_html = ""
    for i, s in enumerate(stocks_done):
        tier_label = {"A_可买入": "A", "B_优质待跌": "B", "C_接近合格": "C"}.get(s.get("tier", ""), "")
        badge_class = {"A": "bA", "B": "bB", "C": "bC"}.get(tier_label, "")
        llm_tag = " 🤖" if s.get("has_llm") else ""
        rows_html += f"""
        <tr>
            <td>{i+1}</td>
            <td><span class="badge {badge_class}">{tier_label}</span></td>
            <td class="code">{fmt_plain(s['code'])}</td>
            <td><a href="report.html?code={fmt_plain(s['code'])}">{fmt_plain(s['name'])}{llm_tag}</a></td>
            <td>{fmt_number(first_value(s, 'price'), 2)}</td>
            <td>{fmt_unit(first_value(s, 'roe'), '%', 1)}</td>
            <td>{fmt_number(first_value(s, 'pe', 'pe_ttm'), 2)}</td>
            <td>{fmt_unit(first_value(s, 'gm', 'gross_margin'), '%', 1)}</td>
            <td>{fmt_market_cap(s)}</td>
            <td>{fmt_plain(first_value(s, 'industry'))}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
	<title>个股深度研报索引 | 五层选股</title>
	<style>
	*{{box-sizing:border-box}}
	body{{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#172033;font-size:13px}}
	.container{{max-width:1120px;margin:0 auto;padding:24px 20px}}
	.topbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:22px}}
	.nav-link{{display:inline-flex;align-items:center;min-height:34px;padding:6px 12px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;color:#2563eb;text-decoration:none;font-weight:600}}
	.nav-link.muted{{color:#64748b}}
	.nav-link:hover{{background:#eff6ff;border-color:#2563eb;text-decoration:none}}
	h1{{font-size:24px;margin:0 0 4px;color:#0f172a}}
	.sub{{color:#64748b;font-size:12px;margin-bottom:20px}}
	.table-wrap{{overflow:auto;background:#fff;border:1px solid #dbe4f0;border-radius:8px}}
	table{{width:100%;border-collapse:collapse}}
	th{{background:#eef2f7;color:#475569;padding:9px 12px;text-align:left;border-bottom:2px solid #cbd5e1;position:sticky;top:0;font-size:12px}}
	td{{padding:7px 12px;border-bottom:1px solid #e2e8f0}}
	tr:hover td{{background:#f8fbff}}
	a{{color:#2563eb;text-decoration:none}}
	a:hover{{text-decoration:underline}}
	.code{{color:#2563eb;font-variant-numeric:tabular-nums}}
	.badge{{display:inline-block;min-width:20px;text-align:center;border-radius:4px;padding:1px 6px;font-weight:700;font-size:11px}}
	.bA{{background:#dcfce7;color:#166534}}.bB{{background:#fef3c7;color:#92400e}}.bC{{background:#e2e8f0;color:#475569}}
	footer{{margin-top:30px;color:#64748b;font-size:11px;border-top:1px solid #dbe4f0;padding-top:12px}}
	</style></head><body>
	<div class="container">
	<nav class="topbar">
	    <a id="backLink" href="../astock_screen_{screen_ts}.html" class="nav-link">← 选股总表</a>
	    <a href="report.html?code={stocks_done[0]['code'] if stocks_done else ''}" class="nav-link muted">首只研报</a>
	</nav>
	<h1>🔬 个股深度研报</h1>
	<div class="sub">共 {len(stocks_done)} 只 · 🤖 = DeepSeek AI 定性分析 · 点击名称查看详情</div>
	<div class="table-wrap">
	<table>
<thead><tr>
    <th>#</th><th>评级</th><th>代码</th><th>名称</th><th>现价</th><th>ROE%</th><th>PE</th><th>毛利%</th><th>市值(亿)</th><th>行业</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table></div>
	<footer>数据来源：东方财富公开接口 · AI分析由 DeepSeek 生成 · 仅供参考不构成投资建议</footer>
	</div>
	<script>
	var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;
	if(window.location.protocol!=="file:"){{
	  fetch(API+"/api/status").then(function(r){{return r.json()}}).then(function(d){{
	    if(d.latest_ts)document.getElementById("backLink").href="../astock_screen_"+d.latest_ts+".html";
	  }}).catch(function(){{}});
	}}
	</script>
	</body></html>"""
    idx_path = os.path.join(base_dir, "index.html")
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write(html)
    return idx_path


def _process_one(idx, s, total, csv_rows, screen_ts, no_llm, deepseek_key, no_kline,
                 stocks_done, lock, spot_cache=None, ai_limiter=None, ai_only=False):
    """单只股票的分析+生成（线程安全）。"""
    code = s["code"]
    with lock:
        print(f"[{idx+1}/{total}] {code} {s['name']}")

    if ai_only:
        ok = False
        if not no_llm and deepseek_key:
            with lock:
                print(f"    [{code}] 🤖 AI-only 分析中...", end=" ", flush=True)
            ok = run_ai_only_for_existing_report(code, ai_limiter)
            with lock:
                print("OK" if ok else "失败")
        s["has_llm"] = ok or _report_has_analysis(OUT_DIR, code)
        with lock:
            stocks_done.append(s)
        return

    stock = fetch_stock_full(
        code,
        name=s["name"],
        industry=s.get("industry"),
        csv_rows=csv_rows,
        no_kline=no_kline,
        spot_cache=spot_cache,
    )
    financials = compute_financials(stock)

    analysis = None
    if not no_llm and deepseek_key:
        with lock:
            print(f"    [{code}] 🤖 DeepSeek 分析中...", end=" ", flush=True)
        analysis = run_deepseek_with_limiter(stock, financials, ai_limiter)
        with lock:
            print("OK" if analysis else "失败")

    out_path = os.path.join(OUT_DIR, DATA_DIR_NAME, f"{code}.json")
    generate_html(stock, financials, analysis, out_path, screen_ts)

    s["has_llm"] = bool(analysis)
    with lock:
        stocks_done.append(s)
        print(f"    📄 {out_path}  ({len(stocks_done)}/{total})")


# ============================================================
# 七、主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="个股深度研报生成器")
    ap.add_argument("--code", type=str, help="单独分析一只股票（6位代码）")
    ap.add_argument("--tier", type=str, choices=["A", "B", "C"], help="仅分析指定评级的股票")
    ap.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek AI 分析")
    ap.add_argument("--parallel", type=int, default=20, help="并行生成研报的线程数（默认20）")
    ap.add_argument("--ai-concurrency", type=int, default=int(os.environ.get("DEEPSEEK_CONCURRENCY", "20")),
                    help="DeepSeek API 并发数（默认20，可独立于研报线程数调低）")
    ap.add_argument("--ai-only", action="store_true", help="只对已有 JSON 研报补 AI，不重抓财务/行情/K线")
    ap.add_argument("--prefetch-financials", choices=["auto", "always", "never"], default="auto",
                    help="批量预取财务三表：auto>=50只启用，always强制，never禁用")
    ap.add_argument("--no-parallel", action="store_true", help="禁用并行，逐只生成")
    ap.add_argument("--no-kline", action="store_true", help="跳过 K 线图（提速 ~40%%，适合批量跑）")
    args = ap.parse_args()

    if args.parallel < 1:
        ap.error("--parallel 必须 >= 1")
    if args.ai_concurrency < 1:
        ap.error("--ai-concurrency 必须 >= 1")

    os.makedirs(OUT_DIR, exist_ok=True)

    # 读取选股结果 CSV
    csv_path = sorted([f for f in os.listdir(os.path.join(WORKDIR, "results"))
                       if f.startswith("astock_screen_") and f.endswith(".csv")],
                      reverse=True)
    if not csv_path:
        print("❌ 未找到选股结果 CSV，请先运行 astock_screener.py")
        sys.exit(1)

    stocks = []
    csv_rows = []
    csv_full = os.path.join(WORKDIR, "results", csv_path[0])
    with open(csv_full, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            csv_rows.append(r)
            code = r["code"]
            tier = r["tier"]

            if args.code:
                if code == args.code:
                    stocks.append({"code": code, "tier": tier, "name": r["name"],
                                   "price": r.get("price"), "roe": r.get("roe"),
                                   "pe": r.get("pe_ttm"), "gm": r.get("gross_margin"),
                                   "mktcap": r.get("mktcap_yi"), "industry": r.get("industry"),
                                   "score": r.get("score")})
                    # 不 break，继续读完 CSV 以填充 csv_rows（同行对比需要）
                    pass
            elif args.tier:
                tier_map = {"A": "A_可买入", "B": "B_优质待跌", "C": "C_接近合格"}
                if tier == tier_map.get(args.tier, ""):
                    stocks.append({"code": code, "tier": tier, "name": r["name"],
                                   "price": r.get("price"), "roe": r.get("roe"),
                                   "pe": r.get("pe_ttm"), "gm": r.get("gross_margin"),
                                   "mktcap": r.get("mktcap_yi"), "industry": r.get("industry"),
                                   "score": r.get("score")})
            else:
                if tier in ("A_可买入", "B_优质待跌"):
                    stocks.append({"code": code, "tier": tier, "name": r["name"],
                                   "price": r.get("price"), "roe": r.get("roe"),
                                   "pe": r.get("pe_ttm"), "gm": r.get("gross_margin"),
                                   "mktcap": r.get("mktcap_yi"), "industry": r.get("industry"),
                                   "score": r.get("score")})

    if not stocks:
        print("❌ 未找到符合条件的股票")
        sys.exit(1)

    print(f"\n{'='*60}")
    mode_label = "AI-only" if args.ai_only else ("含 AI 定性分析" if not args.no_llm and DEEPSEEK_KEY else "仅量化数据")
    print(f"个股深度研报生成 | {len(stocks)} 只 | {mode_label}")
    if not args.no_parallel and len(stocks) > 1:
        print(f"并行模式：{args.parallel} 线程")
    if not args.no_llm and DEEPSEEK_KEY:
        print(f"AI并发：{args.ai_concurrency} | 模型：{DEEPSEEK_MODEL}")
    print(f"{'='*60}\n")

    # 从 CSV 文件名提取日期，用于动态返回链接
    screen_ts = csv_path[0].replace("astock_screen_", "").replace(".csv", "")

    codes = [s["code"] for s in stocks]
    if not args.ai_only and should_prefetch_financials(args.prefetch_financials, len(stocks), bool(args.code)):
        prefetch_all_financials(codes)

    spot_cache = None
    if not args.ai_only:
        # 只抓一次全市场行情，线程间共享，避免每只股票重复创建行情线程池。
        try:
            from astock_screener import fetch_spot_parallel
            spot_cache = fetch_spot_parallel()
        except Exception:
            spot_cache = None

    stocks_done = []
    thread_lock = threading.Lock()
    ai_limiter = threading.BoundedSemaphore(args.ai_concurrency) if (not args.no_llm and DEEPSEEK_KEY) else None

    if args.no_parallel or len(stocks) <= 1:
        for i, s in enumerate(stocks):
            _process_one(i, s, len(stocks), csv_rows, screen_ts,
                        args.no_llm, DEEPSEEK_KEY, args.no_kline, stocks_done, thread_lock,
                        spot_cache=spot_cache, ai_limiter=ai_limiter, ai_only=args.ai_only)
    else:
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futures = {}
            for i, s in enumerate(stocks):
                fut = ex.submit(_process_one, i, s, len(stocks), csv_rows,
                               screen_ts, args.no_llm, DEEPSEEK_KEY, args.no_kline, stocks_done, thread_lock,
                               spot_cache, ai_limiter, args.ai_only)
                futures[fut] = s
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    s = futures[fut]
                    with thread_lock:
                        print(f"  ⚠ [{s['code']}] 失败: {e}")

    # 合并已有研报（运行单股时不丢失其他研报的索引）
    existing_codes = _existing_report_codes(OUT_DIR)
    for code in existing_codes:
        if code not in {s["code"] for s in stocks_done}:
            csv_info = next((r for r in csv_rows if r["code"] == code), {})
            stocks_done.append({
                "code": code, "name": csv_info.get("name", code),
                "tier": csv_info.get("tier", ""), "price": csv_info.get("price", ""),
                "roe": csv_info.get("roe", ""), "pe": csv_info.get("pe_ttm", ""),
                "gm": csv_info.get("gross_margin", ""), "mktcap": csv_info.get("mktcap_yi", ""),
                "industry": csv_info.get("industry", ""),
                "score": float(csv_info.get("score", 0)),
                "has_llm": _report_has_analysis(OUT_DIR, code),
            })

    # 按评分排序
    stocks_done.sort(key=lambda s: -float(s.get("score", 0) or 0))

    # 生成索引页
    idx_path = generate_index(stocks_done, OUT_DIR, screen_ts)
    print(f"\n✅ 索引页: {idx_path} ({len(stocks_done)}只)")
    print(f"✅ 全部研报: {OUT_DIR}/")
    llm_count = sum(1 for s in stocks_done if s.get("has_llm"))
    print(f"   AI分析: {llm_count}/{len(stocks_done)} 只")


if __name__ == "__main__":
    main()
