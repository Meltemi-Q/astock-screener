# 港股/美股五层选股扩展 PRD

> 版本：v0.1 | 日期：2026-06-30 | 状态：待开发  
> 目标读者：接手开发的 AI / 工程师 / 回归测试执行者  
> 适用范围：在现有 A 股五层选股系统基础上，新增港股和美股完整全市场筛选，并通过按钮在 A 股、港股、美股之间切换。

---

## 1. 背景与目标

现有系统已经实现 A 股全市场五层选股流水线：

1. 第0层排雷：现金流、负债、商誉、ST/亏损过滤。
2. 第1层质量：ROE、毛利率、净利率、同比增长和 3 年 CAGR。
3. 第2层估值：PEG、盈利收益率、PE 对行业中位。
4. 第3层安全边际：预期年化和折价。
5. 第4层定性：DeepSeek AI / 人工对护城河、商业模式、管理层和行业进行把关。

本扩展的目标不是做一个静态港股/美股页面，而是把港股和美股做成与 A 股同等级的可刷新、可排序、可回测、可回归测试的全市场选股能力。

### 1.1 成功标准

- 用户访问固定入口后，可以通过市场切换按钮在 `A股 / 港股 / 美股` 三个市场间切换。
- 每个市场都有独立的全市场结果页、CSV、Markdown 榜单、状态和刷新按钮。
- 港股和美股的数据源可靠度明确，主源和备源边界明确，不能用不稳定数据源悄悄产出结果。
- 港股和美股都能跑完整五层筛选，无法满足字段要求的股票进入“数据不足”而不是错误进入 Tier A/B。
- 有完整回归测试：数据源可用性、字段映射、筛选逻辑、HTML 入口、按钮流程、回测反未来函数。

### 1.2 非目标

- 不做实时交易系统。
- 不提供投资建议或自动买卖。
- 不要求本期一次性实现全部深度研报和 AI 定性，但架构必须预留。
- 不使用 Yahoo Finance 或 Stooq 作为主源；它们在当前网络环境下实测不可稳定自动化。

---

## 2. 数据源可靠度矩阵

### 2.1 可靠度分级

| 等级 | 含义 | 是否可做主源 |
|---|---|---|
| S | 官方一手源，来自监管机构、交易所或上市公司申报系统 | 可以 |
| A | 非官方但工程上稳定，字段连续，已有实测，且可与官方源交叉校验 | 可用于行情/补充字段 |
| B | 可访问但字段或稳定性不足，需要人工抽样或二级校验 | 只能做备源 |
| C | 当前环境下不可稳定自动化，或有明显反爬/地区限制 | 不可做核心链路 |

### 2.2 美股数据源

| 数据 | 主源 | 等级 | 用途 | 验收条件 |
|---|---|---:|---|---|
| 证券主数据 | SEC `company_tickers_exchange.json` | S | ticker、CIK、公司名、交易所 | 返回字段必须含 `cik/name/ticker/exchange`，样本必须包含 `AAPL`、`MSFT`、`NVDA` |
| NASDAQ/NYSE/AMEX 可交易清单 | Nasdaq Trader `nasdaqlisted.txt` / `otherlisted.txt` | S | 过滤 ETF、测试证券、退市/异常证券 | 解析后普通股数量必须大于 3000，ETF 可识别并默认排除 |
| 财务基本面 | SEC XBRL `companyfacts` | S | 收入、毛利、净利、资产、负债、股东权益、经营现金流、EPS | Apple 样本必须解析出收入、毛利、净利、资产、负债、权益、经营现金流 |
| 行情/估值 | 东方财富 `push2/push2delay` 美股接口 | A | 价格、市值、PE/PB、成交量、币种 | `AAPL` 样本必须返回 USD、价格、市值；价格不能为 `-` |
| K 线/回测价格 | 东方财富 `push2his` | A | 日 K、复权价、回测净值 | `105.AAPL` 必须返回 2025 年日线，字段可解析为 date/open/close/high/low/volume |

#### 美股主源 URL

- SEC API 文档：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC ticker 清单：https://www.sec.gov/files/company_tickers_exchange.json
- SEC companyfacts 示例：https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json
- Nasdaq Trader Symbol Directory：https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
- Nasdaq listed 文件：https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
- Nasdaq other listed 文件：https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

#### 美股实测证据

2026-06-30 实测：

- SEC ticker 清单曾返回 `10433` 行，字段为 `['cik', 'name', 'ticker', 'exchange']`，样本包含 `NVIDIA CORP / NVDA`、`Alphabet Inc. / GOOGL`、`Apple Inc. / AAPL`。
- SEC Apple `CIK0000320193` companyfacts 曾返回：
  - `RevenueFromContractWithCustomerExcludingAssessedTax`
  - `GrossProfit`
  - `NetIncomeLoss`
  - `Assets`
  - `Liabilities`
  - `StockholdersEquity`
  - `NetCashProvidedByUsedInOperatingActivities`
  - `EarningsPerShareDiluted`
- 当前本地网络对 SEC/Nasdaq 偶发 TLS 失败，因此开发必须实现 retry、缓存和失败报告。不能把一次 TLS 失败解释为“没有数据”。
- 东财 `105.AAPL` 行情实测返回 `f57=AAPL`、`f58=苹果`、`f172=USD`、价格、市值和估值字段。
- 东财 `105.AAPL` 日 K 实测返回 2025 年多条日线。

### 2.3 港股数据源

| 数据 | 主源 | 等级 | 用途 | 验收条件 |
|---|---|---:|---|---|
| 证券主数据 | HKEX List of Securities / HKEX 市场资料页 | S | 股票代码、英文名、类别、board lot、ISIN | 解析结果必须包含 `00700 / TENCENT`、`09988 / BABA-W`，普通股数量必须大于 2000 |
| 财务基本面 | 东方财富港股 F10 指标表 | A | 收入、毛利、净利、ROE、负债率、币种、报告期 | `00700.HK` 必须解析出最近 3 个年报的 ROE、毛利率、负债率、币种 |
| 行情/估值 | 东方财富 `push2/push2delay` 港股接口 | A | 价格、市值、PE/PB、币种、成交量 | `116.00700` 必须返回 HKD、价格、市值；价格不能为 `-` |
| K 线/回测价格 | 东方财富 `push2his` | A | 日 K、复权价、回测净值 | `116.00700` 必须返回 2025 年日线 |
| 年报公告交叉校验 | HKEXnews | S/B | 抽样核验港股财务字段 | MVP 不全量解析，但每次发布前必须抽样核验腾讯、汇丰、阿里等样本 |

#### 港股主源 URL

- HKEX List of Securities：https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx
- HKEX Equities 页面：https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities?sc_lang=en
- HKEXnews 入口：https://www.hkexnews.hk/

#### 港股实测证据

2026-06-30 实测：

- 东财 `RPT_HKF10_FN_GMAININDICATOR` 对 `00700.HK` 返回年报行，最近样本：
  - `2025-12-31`，币种人民币，ROE `21.1347`，毛利率 `56.2134`，负债率 `39.1332`
  - `2024-12-31`，ROE `21.7798`，毛利率 `52.8955`，负债率 `40.8254`
  - `2023-12-31`，ROE `15.0611`，毛利率 `48.1284`，负债率 `44.6072`
- 东财 `116.00700` 行情实测返回 `f57=00700`、`f58=腾讯控股`、`f172=HKD`、价格、市值和估值字段。
- 东财 `116.00700` 日 K 实测返回 2025 年多条日线。
- HKEX xlsx URL 在本地请求中存在只返回少量行的情况。实现时不能盲信下载成功，必须用记录数和关键样本做强校验；校验失败时应阻断港股构建并输出明确错误。

### 2.4 禁用为主源的数据源

| 数据源 | 实测问题 | 结论 |
|---|---|---|
| Yahoo Finance | 当前网络返回 Yahoo 中国不可访问页 | 不可作为主源或备源 |
| Stooq | 出现 404、TLS 失败、browser verification | 不可作为主源 |
| 第三方博客/GitHub 端点说明 | 非一手来源，可能过期 | 只能用于发现线索，不能作为 PRD 证据 |

---

## 3. 产品形态

### 3.1 固定入口

新增统一入口：

- `results/screen.html`：市场总入口。
- `results/astock_screen.html`：A 股固定入口，保留兼容。
- `results/hkstock_screen.html`：港股固定入口。
- `results/usstock_screen.html`：美股固定入口。

统一入口顶部提供市场切换按钮：

```text
A股 | 港股 | 美股
```

按钮行为：

- 切换时不重新抓数据，只跳转到对应市场最新固定入口。
- 当前市场按钮为 active。
- 如果某市场暂无数据，显示空状态和“更新该市场筛选”按钮。
- URL 不使用日期硬编码；日期页只作为历史产物。

### 3.2 刷新按钮

每个市场独立提供：

| 按钮 | A 股 | 港股 | 美股 |
|---|---|---|---|
| 刷新行情 | 已有 | 新增 | 新增 |
| 更新五层筛选 | 已有 | 新增 | 新增 |
| 定性分析 | 已有 | 后续复用 | 后续复用 |

按钮文案必须包含市场：

- A 股：`更新 A 股筛选`
- 港股：`更新港股筛选`
- 美股：`更新美股筛选`

所有长任务必须显示进度条。没有精确百分比的同步任务显示 indeterminate progress；有 progress endpoint 的任务显示真实百分比。

### 3.3 输出文件命名

| 市场 | HTML | CSV | Markdown | 固定入口 |
|---|---|---|---|---|
| A 股 | `astock_screen_YYYYMMDD.html` | `astock_screen_YYYYMMDD.csv` | `astock_shortlist_YYYYMMDD.md` | `astock_screen.html` |
| 港股 | `hkstock_screen_YYYYMMDD.html` | `hkstock_screen_YYYYMMDD.csv` | `hkstock_shortlist_YYYYMMDD.md` | `hkstock_screen.html` |
| 美股 | `usstock_screen_YYYYMMDD.html` | `usstock_screen_YYYYMMDD.csv` | `usstock_shortlist_YYYYMMDD.md` | `usstock_screen.html` |

CSV 必须包含 `market` 字段，值为 `cn/hk/us`。

---

## 4. 技术架构

### 4.1 目标目录结构

```text
economy/
├── screeners/
│   ├── __init__.py
│   ├── contracts.py
│   ├── scoring.py
│   ├── html_renderer.py
│   ├── cn.py
│   ├── hk.py
│   └── us.py
├── data_sources/
│   ├── __init__.py
│   ├── http.py
│   ├── eastmoney.py
│   ├── sec_edgar.py
│   ├── nasdaq_trader.py
│   └── hkex.py
├── backtest/
│   ├── __init__.py
│   ├── point_in_time.py
│   ├── portfolio.py
│   └── metrics.py
├── astock_screener.py
├── global_screener.py
├── server.py
├── run.sh
└── tests/
```

### 4.2 数据合同

所有市场必须先归一化为同一数据合同，再进入五层评分。

```python
SecurityMaster = {
    "market": "cn" | "hk" | "us",
    "code": str,
    "display_code": str,
    "name": str,
    "exchange": str,
    "currency": str,
    "lot_size": int,
    "security_type": "common_stock" | "reit" | "etf" | "adr" | "unknown",
    "is_tradable": bool,
}
```

```python
QuoteSnapshot = {
    "market": "cn" | "hk" | "us",
    "code": str,
    "price": float | None,
    "pe_ttm": float | None,
    "pb": float | None,
    "market_cap": float | None,
    "currency": str,
    "quote_time": str | None,
    "source": str,
}
```

```python
AnnualFinancial = {
    "market": "cn" | "hk" | "us",
    "code": str,
    "fiscal_year": int,
    "report_date": str,
    "filing_date": str | None,
    "currency": str,
    "revenue": float | None,
    "gross_profit": float | None,
    "net_profit": float | None,
    "operating_cashflow": float | None,
    "assets": float | None,
    "liabilities": float | None,
    "equity": float | None,
    "eps": float | None,
    "roe": float | None,
    "gross_margin": float | None,
    "net_margin": float | None,
    "debt_ratio": float | None,
}
```

### 4.3 字段映射要求

#### 美股 SEC 字段映射

| 目标字段 | SEC XBRL tag 优先级 |
|---|---|
| revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues` |
| gross_profit | `GrossProfit` |
| net_profit | `NetIncomeLoss` |
| operating_cashflow | `NetCashProvidedByUsedInOperatingActivities` |
| assets | `Assets` |
| liabilities | `Liabilities` |
| equity | `StockholdersEquity` |
| eps | `EarningsPerShareDiluted` → `EarningsPerShareBasic` |

派生字段：

- `gross_margin = gross_profit / revenue * 100`
- `net_margin = net_profit / revenue * 100`
- `debt_ratio = liabilities / assets * 100`
- `roe = net_profit / average_equity * 100`
- `ocf_to_profit = operating_cashflow / net_profit`

#### 港股东财字段映射

优先使用 `RPT_HKF10_FN_GMAININDICATOR` 的年报行：

| 目标字段 | 东财字段 |
|---|---|
| revenue | `OPERATE_INCOME` |
| gross_profit | `GROSS_PROFIT` |
| net_profit | `PARENT_HOLDER_NETPROFIT` 或 `HOLDER_PROFIT` |
| roe | `ROE_AVG` |
| gross_margin | `GROSS_PROFIT_RATIO` |
| net_margin | `NET_PROFIT_RATIO` |
| debt_ratio | `DEBT_ASSET_RATIO` |
| currency | `CURRENCY` |

经营现金流缺失时，从 `RPT_HKSK_FN_CASHFLOW` 取 `经营活动产生的现金流量净额` 或等价 `ITEM_NAME`。解析必须用 item code 或受控中文名称白名单，不允许模糊包含误配。

### 4.4 五层评分调整

五层逻辑保持一致，但不同市场有口径差异：

| 项 | A 股 | 港股 | 美股 |
|---|---|---|---|
| 一手金额 | 100 股 | HKEX board lot | 1 股或券商最小交易单位 |
| 币种 | CNY | HKD / CNY / USD 报表币种 | USD |
| 财年 | 多数 12-31 | 可能非 12-31 | 常见非自然年 |
| ST 过滤 | 适用 | 不适用 | 不适用 |
| 商誉过滤 | 当前 A 股已有 | MVP 可缺失则不作为硬排雷 | MVP 可缺失则不作为硬排雷 |
| 金融股 | 默认自然过滤 | 默认排除银行/保险/券商 | 默认排除 Financials |

硬规则：

- 任一股票缺少 `price`、`market_cap`、`revenue`、`net_profit`、`equity`、`operating_cashflow` 中任意字段，不能进入 Tier A/B。
- 缺少 `gross_profit` 的股票不能进入 Tier A/B，但可进入“数据不足”。
- 港股/美股的行业 PE 中位必须按本市场单独计算，不能跨市场混用。
- 市值、净利、合理市值的币种必须一致。若行情币种与报表币种不一致，MVP 先标记“币种不一致/数据不足”，不做自动汇率换算。

---

## 5. API 与 CLI

### 5.1 CLI

新增统一命令：

```bash
python3 global_screener.py --market cn
python3 global_screener.py --market hk
python3 global_screener.py --market us
python3 global_screener.py --market all
python3 global_screener.py --market hk --fresh
python3 global_screener.py --market us --quotes-fresh
```

`astock_screener.py` 保留，但内部可以逐步迁移到 `global_screener.py --market cn`。

### 5.2 HTTP API

| Endpoint | 参数 | 行为 |
|---|---|---|
| `/api/status` | `market=cn/hk/us/all` | 返回各市场最新数据时间、运行状态、进度 |
| `/api/refresh` | `market=cn/hk/us`, `mode=quotes/full` | 刷新对应市场 |
| `/api/layer4` | `market=cn/hk/us`, `tier=A/B/C` | 对对应市场运行 AI 定性 |
| `/api/deep` | `market=cn/hk/us`, `code=...` | 生成对应市场单股研报 |

响应必须包含：

```json
{
  "done": true,
  "market": "us",
  "latest_ts": "20260630",
  "latest_href": "usstock_screen_20260630.html",
  "stable_href": "usstock_screen.html",
  "progress": null,
  "warnings": []
}
```

错误响应不得吞掉数据源错误：

```json
{
  "done": false,
  "market": "hk",
  "error": "HKEX security master validation failed: missing 00700",
  "source": "hkex",
  "retryable": true
}
```

---

## 6. 开发路线

### Phase 0: 数据源探针

目标：不改现有 UI，先把数据源探针做成可重复测试。

交付：

- `data_sources/sec_edgar.py`
- `data_sources/nasdaq_trader.py`
- `data_sources/hkex.py`
- `data_sources/eastmoney.py`
- `tests/test_global_data_sources.py`

验收：

- 美股 ticker 清单解析出 `AAPL`、`MSFT`、`NVDA`。
- Apple SEC companyfacts 解析出收入、毛利、净利、资产、负债、权益、经营现金流。
- 港股主数据解析出 `00700`、`09988`；如果 HKEX 官方下载文件不完整，测试必须失败并给出明确错误。
- 东财 `116.00700`、`105.AAPL` 行情和 K 线探针通过。

### Phase 1: 数据合同与评分内核

目标：把 A 股强绑定逻辑拆成市场无关 scoring core。

交付：

- `screeners/contracts.py`
- `screeners/scoring.py`
- `tests/test_global_scoring.py`

验收：

- 给定同一组 normalized records，A/HK/US 都能产出一致结构的 `ScreeningResult`。
- 缺少关键字段的股票进入 `data_insufficient`，不会进入 Tier A/B。
- 行业 PE 中位按市场内分组计算。
- 现有 A 股回归测试全部通过。

### Phase 2: 港股全市场筛选

目标：先完成港股，因为港股东财财务指标和现有 A 股架构更接近。

交付：

- `screeners/hk.py`
- `results/hkstock_screen.html`
- `results/hkstock_screen_YYYYMMDD.html`
- `results/hkstock_screen_YYYYMMDD.csv`
- `results/hkstock_shortlist_YYYYMMDD.md`

验收：

- 全市场港股普通股数量合理，低于 1000 或高于 5000 均视为异常。
- `00700`、`00005`、`09988` 在 universe 中可定位。
- `00700` 最近 3 个年报指标可解析。
- 港股页面搜索、排序、Tier 筛选、行业筛选、风险筛选可用。
- 港股固定入口不硬编码过期日期。

### Phase 3: 美股全市场筛选

目标：完成美股 SEC + 东财行情链路。

交付：

- `screeners/us.py`
- `results/usstock_screen.html`
- `results/usstock_screen_YYYYMMDD.html`
- `results/usstock_screen_YYYYMMDD.csv`
- `results/usstock_shortlist_YYYYMMDD.md`

验收：

- SEC universe 中普通股数量合理，低于 3000 或高于 12000 均视为异常。
- `AAPL`、`MSFT`、`NVDA`、`GOOGL` 可定位。
- `AAPL` 最近 3 个年报核心字段可解析。
- 东财行情中 `AAPL` 的价格、市值、币种可解析。
- 美股页面搜索、排序、Tier 筛选、行业筛选、风险筛选可用。

### Phase 4: 统一入口和按钮切换

目标：把三个市场放到同一个使用体验里。

交付：

- `results/screen.html`
- 市场切换组件
- `server.py` market 参数
- `run.sh --market cn/hk/us/all`

验收：

- `http://127.0.0.1:8899/screen.html` 打开统一入口。
- 三个市场按钮能跳到对应固定入口。
- 刷新某市场不会误刷新其他市场。
- `/api/status?market=all` 返回三个市场状态。

### Phase 5: 深度研报复用

目标：港股/美股单股研报复用共享 shell。

交付：

- `results/deep_dives/data/hk/00700.json`
- `results/deep_dives/data/us/AAPL.json`
- `deep_dives/report.html?market=hk&code=00700`
- `deep_dives/report.html?market=us&code=AAPL`

验收：

- 同一 code 在不同市场不会冲突。
- 返回按钮回到对应市场总表。
- 研报索引可按市场筛选。

---

## 7. 回测路线

### 7.1 回测目标

验证五层筛选在港股和美股上的历史表现，并防止未来函数。

必须输出：

- 年化收益率 CAGR
- 最大回撤
- Sharpe
- 胜率
- 换手率
- 年度收益
- 相对 benchmark 超额收益

### 7.2 数据要求

| 数据 | 港股 | 美股 |
|---|---|---|
| 历史价格 | 东财 `push2his`，secid `116.CODE` | 东财 `push2his`，secid `105.TICKER` |
| 财报可用日期 | `NOTICE_DATE` 优先；没有则 `REPORT_DATE + 90天` | SEC `filed` 日期优先；没有则 `REPORT_DATE + 90天` |
| benchmark | `02800.HK` 或恒指 ETF | `SPY` / `QQQ` |
| 调仓频率 | 年度、季度 | 年度、季度 |

### 7.3 反未来函数规则

任意回测日期 `T`，只能使用：

- `filing_date <= T` 的财报。
- `quote_date <= T` 的行情。
- 当日收盘后生成的信号，只能按下一个交易日开盘或收盘成交。
- 不允许使用当前最新 universe 直接回测历史，除非明确标记为 survivorship-biased exploratory backtest。

### 7.4 回测阶段

#### Backtest v0: 固定样本冒烟

样本：

- 港股：`00700`、`00005`、`09988`
- 美股：`AAPL`、`MSFT`、`NVDA`、`GOOGL`

目标：

- 验证价格序列、财报序列、信号生成、净值计算不报错。

#### Backtest v1: 当前 universe 历史回测

目标：

- 先用当前 universe 跑历史价格和历史财报。
- 文档必须标注 survivorship bias。
- 仅用于工程验收，不作为策略有效性结论。

#### Backtest v2: Point-in-time universe

目标：

- 美股用 SEC/Nasdaq 可追溯清单或本项目每日快照重建历史 universe。
- 港股用 HKEX 每日/定期快照或本项目每日快照重建历史 universe。
- 只有 v2 结果可以作为策略研究依据。

### 7.5 回测验收阈值

- 回测不能因为单只股票缺数据中断全局任务。
- 缺价格的股票当期剔除，并在报告中列出。
- 每个调仓日必须输出入选股票数、剔除股票数、缺数据股票数。
- 任一调仓日持仓数为 0 时，净值应保持现金，不得除以 0。
- 测试必须包含一个人工构造的未来函数样本，确认系统不会读取 `filing_date > T` 的财报。

---

## 8. 回归测试清单

### 8.1 数据源测试

必须新增 `tests/test_global_data_sources.py`。

测试项：

- `test_sec_ticker_master_contains_required_fields`
- `test_sec_companyfacts_maps_apple_core_fields`
- `test_nasdaq_symbol_directory_filters_etfs_and_tests`
- `test_hkex_security_master_contains_core_hk_names`
- `test_hkex_security_master_fails_on_too_few_rows`
- `test_eastmoney_hk_quote_maps_currency_price_market_cap`
- `test_eastmoney_us_quote_maps_currency_price_market_cap`
- `test_eastmoney_global_kline_parses_hk_and_us_daily_rows`

网络测试必须支持 `--live` 开关。默认单元测试使用 fixture，不直接打外网。

### 8.2 合同测试

必须新增 `tests/test_global_contracts.py`。

测试项：

- 所有市场 normalized record 字段一致。
- 缺关键字段不能进入 Tier A/B。
- currency mismatch 会进入数据不足。
- lot size 计算：
  - A 股一手金额 = price * 100
  - 港股一手金额 = price * board_lot
  - 美股最小买入金额 = price

### 8.3 UI 回归测试

必须扩展 `tests/test_regressions.py`。

测试项：

- 页面存在 `A股 / 港股 / 美股` 市场切换按钮。
- active 市场状态正确。
- 港股/美股固定入口不硬编码过期日期。
- 刷新按钮带 market 参数。
- 进度条在港股/美股刷新任务中出现。
- 深度研报返回按钮按 market 返回。

### 8.4 API 测试

测试项：

- `/api/status?market=all` 返回 `cn/hk/us` 三个状态。
- `/api/refresh?market=hk&mode=quotes` 只调用港股行情刷新。
- `/api/refresh?market=us&mode=full` 只调用美股五层筛选。
- 无效 market 返回 400。
- 数据源验证失败返回结构化错误，不生成错误 HTML。

### 8.5 回测测试

必须新增 `tests/test_backtest.py`。

测试项：

- `test_backtest_uses_only_filings_available_on_trade_date`
- `test_backtest_empty_portfolio_stays_cash`
- `test_backtest_missing_price_excludes_stock_and_reports_warning`
- `test_backtest_rebalance_uses_next_trade_day_execution`
- `test_backtest_outputs_required_metrics`

---

## 9. 数据质量红线

以下情况必须失败，不允许降级为静默通过：

1. 官方证券清单下载成功但记录数低于阈值。
2. 关键样本缺失：港股缺 `00700` 或美股缺 `AAPL`。
3. 行情价格为 `-`、`0`、负数，或市值缺失。
4. 报表币种和行情币种不一致且没有显式换算。
5. 财报行无法区分年报/季报。
6. 美股 SEC tag 同时缺收入、净利或现金流核心字段。
7. HTML 固定入口指向不存在的日期页。
8. 港股/美股结果复用了 A 股 `deep_dives/data/XXXXXX.json` 路径导致 code 冲突。

---

## 10. AI 开发者交接要求

接手开发的 AI 必须按以下顺序推进：

1. 先实现数据源探针和 fixture，不改 UI。
2. 再实现 normalized contract 和 scoring core。
3. 再接港股全市场。
4. 再接美股全市场。
5. 最后做统一入口、按钮切换和深度研报复用。

每个阶段必须：

- 先写 failing test，再实现。
- 不把 live network 测试放进默认单元测试。
- 每个 live source 都要有 fixture 样本。
- 每次生成 HTML 后用 HTTP 服务打开检查。
- 每个 commit 只覆盖一个阶段。

推荐提交顺序：

```text
test: add global market data source contracts
feat: add SEC and Eastmoney source probes
feat: add normalized scoring core
feat: add HK market screener
feat: add US market screener
feat: add market switcher and API market routing
feat: add global market backtest harness
```

---

## 11. Definition of Done

本项目完成时必须满足：

- `python3 -m unittest tests.test_regressions tests.test_global_contracts tests.test_global_data_sources tests.test_backtest -v` 通过。
- `python3 -m py_compile astock_screener.py global_screener.py server.py stock_deep_dive.py` 通过。
- `python3 -m flake8 --select=F,E9 ...` 通过。
- `./run.sh --market cn --serve-only`、`./run.sh --market hk --serve-only`、`./run.sh --market us --serve-only` 可启动并打开对应页面。
- 三个市场的固定入口都能访问。
- 港股、美股各自至少生成一个完整日期页、CSV、Markdown 榜单。
- 统一入口市场切换按钮可用。
- 回测 v0 和 v1 可运行并产出报告。
- PRD 中列出的数据质量红线都有自动测试覆盖。

