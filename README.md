# A股「五层选股流水线」自动化筛选器

把你的选股法（排雷 → 质量 → 估值 → 毛估估安全边际 → 定性把关）做成一键脚本，
对**全市场约 5500 只 A 股**自动打分排序，帮你把候选从 5000+ 缩到几十只。

## 一键运行

```bash
cd /Users/meltemi/Documents/yulong/economy
./run.sh            # 普通运行（6小时缓存，秒级出结果）
./run.sh --fresh    # 强制抓最新数据
./run.sh --deep --no-llm                    # 批量生成量化研报，不调用 AI
./run.sh --deep --ai-only --ai-concurrency=20 # 只给已有研报补 DeepSeek 分析
```

或直接：

```bash
python3 astock_screener.py            # 全市场
python3 astock_screener.py --fresh    # 忽略缓存
python3 astock_screener.py --year 2024 --top 60
```

**零第三方依赖**——只用 Python 标准库，数据来自东方财富公开接口，不需要装 akshare/tushare，也不需要 token。

## 性能与 AI 开关

- 全市场筛选：财报/资产负债/商誉/历史净利使用 6 路并行，行情按 5 个板块并行抓取，并带 6 小时本地 cache。
- 个股研报：默认 `--parallel 20` 按股票并发；全市场行情只抓一次并在线程间共享，避免每只股票重复开行情线程池。
- DeepSeek：默认 `DEEPSEEK_MODEL=deepseek-v4-flash`，可改为 `deepseek-v4-pro`；`--ai-concurrency` 单独控制 AI 并发，默认 20。
- 快速补 AI：已有 `results/deep_dives/data/XXXXXX.json` 时可用 `--ai-only`，只调用 DeepSeek，不重抓财务、行情、K 线。
- 跳过 AI：`--no-llm` 只生成量化数据；批量只看基本面时优先用这个。

## 输出

- `results/astock_screen_YYYYMMDD.html` —— **交互式网页（首选）**：全部 5500 只都在，可搜索代码/名称、按 A/B/C 档筛选、选行业、点表头排序，含**现价**。`run.sh` 会自动用浏览器打开。
- `results/deep_dives/report.html?code=XXXXXX` —— 个股深度研报共享页面壳；每只股票的数据存放在 `results/deep_dives/data/XXXXXX.json`，避免重复提交大量同构 HTML。
- `results/astock_shortlist_YYYYMMDD.md` —— 榜单，Tier A/B/C 三档表格，按评分排序，可直接看
- `results/astock_screen_YYYYMMDD.csv` —— 全量 5500 只，含每只的所有指标 + **现价** + **落选原因** + **风险备注**（用 Excel 打开可任意筛选/排序）

> 已封装为 skill：直接说"**跑一下选股**/选股/A股筛选"即可一键触发（见 `~/.claude/skills/astock-screener/`）。

## 五层逻辑（顺序不可颠倒）

| 层 | 名称 | 量化标准 |
|---|---|---|
| 0 | 排雷 | 经营现金流÷净利润≥0.8、负债率<70%、商誉<净资产30%、非ST、不亏损 |
| 1 | 质量 | ROE≥15%、毛利≥30%、净利率≥10%、净利同比&3年CAGR均≥10% |
| 2 | 估值 | PEG<1、盈利收益率(1/PE)>5%、PE≤行业中位 |
| 3 | 毛估估+安全边际 | 预期年化(1/PE+增速)≥10%、当前市值≤合理市值×0.7（打7折） |
| 4 | 定性把关 | 护城河/商业模式/能力圈/管理层/行业景气 —— **机器算不出，人工把关** |

## 三档结果

- **🟢 Tier A 可买入**：五层全过（含估值打7折）。好公司很少便宜，这档通常很少甚至为空，属正常。
- **🟡 Tier B 优质待跌**：排雷+质量全过的真·好生意，只是现在不够便宜 → **加自选，等回调到买点**。
- **⚪ Tier C 接近合格**：排雷过关，质量仅差一项，留作观察池。

## 风险备注（CSV 的 `risk_notes` 列 / 榜单里名称前的 ⚠）

量化指标好看也可能是陷阱，脚本会自动标记三类：
- **扣非比低**：利润含较多一次性收益（资产处置/政府补贴/投资收益），不可持续
- **单年爆发**：同比远高于3年CAGR，可能是周期顶 or 一次性高基数
- **动态PE远高于TTM**：最新季度盈利可能在下滑

看到 ⚠ 不代表淘汰，而是提醒你第4层定性时重点查。

## 调参

所有阈值/评分权重都在 `astock_screener.py` 顶部的 `CONFIG` 字典里，想松想紧直接改，例如：
- 嫌结果太少 → 调低 `min_roe` / `margin_of_safety`（如 0.8）
- 嫌结果太杂 → 调高 `min_roe` / `min_growth`

## 数据与口径说明

- 报告期默认用最近一期**年报**（更干净，季报 ROE/增速是部分期间值，会失真）。
- PE 用 **TTM**（滚动12个月），市值用总市值。
- 增长率取「净利同比」与「3年净利CAGR」的**较小值**（偏保守）。
- 合理PE = 增长率，限制在 [12, 30] 区间；合理市值 = TTM净利 × 合理PE。
- **银行/保险/券商**会被毛利率≥30%、负债率<70% 这两条自然过滤掉——这套估值框架本就不适用于金融股。
- 第2层「PE历史<30%分位」这一支暂用「PE≤行业中位」近似（满足其一即可），后续可接历史分位增强。

## 文件结构

```
economy/
├── astock_screener.py   主脚本（含 CONFIG 调参区）
├── run.sh               一键运行
├── README.md            本文件
├── cache/               接口数据缓存（财报静态，重复跑秒级）
├── templates/deep_dive/  个股研报共享页面模板
└── results/             输出榜单 + CSV + HTML/JSON 报告
    └── deep_dives/
        ├── index.html
        ├── report.html
        ├── assets/
        └── data/         每只股票一个 XXXXXX.json
```
