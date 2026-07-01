"""
Minimal point-in-time backtest framework for multi-market five-layer screening.

Phase 3 v0: Fixed-sample smoke test with MOCK data. 占位实现，非真实回测。

⚠️ 严重局限（绝对不可用于策略评估）：
  - 价格是 random-walk 合成数据，不是真实行情。
  - 财报是硬编码假数据，选股规则只是玩具 (roe>15)，未复用五层选股逻辑。
  - 样本极小：仅 7 只大盘股 / 5 个年度调仓期。
  - 不处理分红、停牌、涨跌停、交易成本、滑点。
  - 存在幸存者偏差（只用当前 universe）。
  - 没有真实基准数据，不输出 alpha/超额收益。
本模块仅用于验证 signal → equity curve → metrics 管道结构是否跑通，
所有数值均为 mock，绝不可用于任何真实策略评估或对外展示。
"""

from __future__ import annotations
import json
import os
from collections import defaultdict
from typing import Any


def fixed_sample_v0(
    output_dir: str = "results/backtest",
    rebalance_frequency: str = "annual",
) -> dict[str, Any]:
    """Run MOCK backtest v0 on a fixed sample of 7 stocks.

    ⚠️ 全 mock 占位实现：价格为合成 random-walk，财报为硬编码，选股规则为玩具。
    返回的所有指标 (mock_cagr_pct / mock_sharpe 等) 均为 mock，仅验证管道结构，
    绝不可用于策略评估。meta.is_mock=True。不输出真实基准/alpha。
    """
    import time as _time

    os.makedirs(output_dir, exist_ok=True)

    # Fixed sample as per PRD section 7.4
    samples = [
        # (market, code, name)
        ("hk", "00700", "Tencent"),
        ("hk", "00005", "HSBC"),
        ("hk", "09988", "Alibaba HK"),
        ("us", "AAPL", "Apple"),
        ("us", "MSFT", "Microsoft"),
        ("us", "NVDA", "NVIDIA"),
        ("us", "GOOGL", "Alphabet"),
    ]

    results: dict[str, Any] = {
        "meta": {
            "version": "v0-fixed-sample-mock",
            "is_mock": True,
            "survivorship_bias": True,
            "warning": "MOCK 占位实现：价格为合成 random-walk、财报硬编码、选股为玩具规则、样本仅 7 股/5 期，"
                       "不处理分红/停牌/涨跌停/交易成本，且仅用当前 universe（幸存者偏差）。"
                       "所有 mock_* 指标绝不可用于策略评估。",
            "limitations": [
                "价格为合成 random-walk，非真实行情",
                "财报硬编码，选股规则未复用五层选股逻辑",
                "样本极小：7 只大盘股 / 5 个年度调仓期",
                "不处理分红、停牌、涨跌停、交易成本、滑点",
                "存在幸存者偏差（仅当前 universe）",
                "无真实基准数据，不输出 alpha/超额收益",
            ],
            "generated": _time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "sample": samples,
        "signals": {},
        "equity_curve": [],
        "metrics": {},
    }

    # ── Mock data: simple price + fundamental sequence ──
    # In production, this would fetch from push2his + SEC/HKEX
    mock_prices = _build_mock_price_series(samples)
    mock_financials = _build_mock_financial_series(samples)

    # ── Generate signals at each rebalance date ──
    rebalance_dates = _get_rebalance_dates(mock_prices, rebalance_frequency)
    signals, holdings_log = _generate_signals(rebalance_dates, samples, mock_financials)

    # ── Compute equity curve ──
    equity_curve, daily_returns = _compute_equity_curve(mock_prices, signals, holdings_log)

    # ── Compute metrics ──
    metrics = _compute_metrics(equity_curve, daily_returns, signals)

    results["signals"] = signals
    results["equity_curve"] = equity_curve[:120]  # first 120 days sample
    results["metrics"] = metrics
    results["report_path"] = os.path.join(output_dir, "backtest_v0.json")

    with open(results["report_path"], "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    return results


def _build_mock_price_series(samples):
    """Build a mock price series: flat prices for simplicity.

    Returns: dict[code, list[dict]] with date, open, close, high, low, volume.
    """
    import datetime

    prices = {}
    base_date = datetime.date(2020, 1, 2)
    base_prices = {"00700": 380, "00005": 58, "09988": 210,
                   "AAPL": 75, "MSFT": 160, "NVDA": 6, "GOOGL": 67}

    for market, code, _name in samples:
        series = []
        base = base_prices.get(code, 100)
        for i in range(1500):  # ~6 years of trading days
            date = base_date + datetime.timedelta(days=i)
            if date.weekday() >= 5:  # skip weekends
                continue
            # Simple random walk with drift
            drift = 1.0 + (i * 0.0001)  # slight upward trend
            noise = 1.0 + (hash(f"{code}{i}") % 100 - 50) / 500.0
            price = base * drift * noise
            series.append({
                "date": date.isoformat(),
                "open": round(price, 2),
                "close": round(price * 1.001, 2),
                "high": round(price * 1.005, 2),
                "low": round(price * 0.995, 2),
                "volume": 1000000,
            })
        prices[code] = series
    return prices


def _build_mock_financial_series(samples):
    """Build mock annual financial data."""
    return {code: [{"year": y, "net_profit": 100, "revenue": 500, "equity": 400,
                    "roe": 20 + (y - 2020), "debt_ratio": 30}
                   for y in range(2020, 2026)]
            for _m, code, _n in samples}


def _get_rebalance_dates(price_data, frequency="annual"):
    """Get list of rebalance dates from price data."""
    all_dates = set()
    for series in price_data.values():
        for row in series:
            all_dates.add(row["date"])
    dates = sorted(all_dates)

    if frequency == "annual":
        # Take first trading day of each year
        by_year = defaultdict(list)
        for d in dates:
            by_year[d[:4]].append(d)
        return [min(v) for v in by_year.values()]
    else:
        # Quarterly: first day of each quarter
        return dates[::63]


def _generate_signals(rebalance_dates, samples, financials):
    """Generate buy/sell signals at each rebalance date using MOCK screening.

    ⚠️ 玩具规则：仅 roe>15 就买，未复用五层选股逻辑（排雷/质量/估值/毛估估/定性）。
    生产版本应在 point-in-time 数据上跑完整五层管道。
    """
    signals = {}
    holdings_log = defaultdict(list)

    for date in rebalance_dates:
        year = int(date[:4])
        date_signals = []
        for market, code, name in samples:
            fin = financials.get(code, [])
            # Simple mock signal: buy if ROE > 15%
            latest_fin = next((f for f in fin if abs(f["year"] - year) <= 1), None)
            if latest_fin and latest_fin.get("roe", 0) > 15:
                date_signals.append({
                    "code": code, "name": name, "market": market,
                    "action": "buy", "weight": 1.0 / len(samples),
                })
                holdings_log[code].append((date, "buy"))
        signals[date] = date_signals
    return signals, dict(holdings_log)


def _compute_equity_curve(price_data, signals, holdings_log, initial_capital=1000000):
    """Compute equity curve from price series and signals."""
    all_dates = set()
    for series in price_data.values():
        for row in series:
            all_dates.add(row["date"])
    dates = sorted(all_dates)

    equity_curve = []
    daily_returns = []
    portfolio = {}  # code → shares
    cash = initial_capital

    for i, date in enumerate(dates):
        # Check for rebalance
        if date in signals:
            # Sell everything
            for code, shares in list(portfolio.items()):
                price = _get_price(price_data, code, date)
                if price:
                    cash += shares * price
            portfolio.clear()

            # Buy new positions
            for sig in signals[date]:
                code = sig["code"]
                price = _get_price(price_data, code, date)
                if price and cash > 0:
                    allocation = cash * sig["weight"]
                    shares = int(allocation / price)
                    if shares > 0:
                        portfolio[code] = shares
                        cash -= shares * price

        # Compute portfolio value
        stock_value = 0
        for code, shares in portfolio.items():
            price = _get_price(price_data, code, date)
            if price:
                stock_value += shares * price
        total = cash + stock_value

        equity_curve.append({"date": date, "value": round(total, 2), "cash": round(cash, 2)})

        if i > 0:
            prev = equity_curve[-2]["value"]
            if prev > 0:
                daily_returns.append((total - prev) / prev)

    return equity_curve, daily_returns


def _get_price(price_data, code, date):
    """Get close price for a stock on a given date."""
    series = price_data.get(code, [])
    for row in series:
        if row["date"] == date:
            return row["close"]
    # Nearest earlier date
    best = None
    for row in series:
        if row["date"] <= date:
            best = row["close"]
    return best


def _compute_metrics(equity_curve, daily_returns, signals):
    """Compute backtest performance metrics."""
    import math

    if not equity_curve or not daily_returns:
        return {
            "is_mock": True,
            "mock_cagr_pct": 0.0, "mock_max_drawdown_pct": 0.0, "mock_sharpe": 0.0,
            "mock_win_rate_pct": 0.0, "mock_turnover_per_year": 0.0,
            "portfolio_values": {},
            "note": "insufficient data — mock 管道占位，无真实数据",
        }

    start_val = equity_curve[0]["value"]
    end_val = equity_curve[-1]["value"]
    days = len(equity_curve)
    years = days / 252.0

    cagr = ((end_val / start_val) ** (1.0 / years) - 1.0) * 100 if years > 0 and start_val > 0 else 0

    # Max drawdown
    peak = start_val
    max_dd = 0.0
    for point in equity_curve:
        v = point["value"]
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)

    # Sharpe (assuming 0% risk-free rate for simplicity)
    if daily_returns:
        avg_daily = sum(daily_returns) / len(daily_returns)
        std_daily = math.sqrt(sum((r - avg_daily) ** 2 for r in daily_returns) / len(daily_returns))
        sharpe = (avg_daily / std_daily * math.sqrt(252)) if std_daily > 0 else 0
    else:
        sharpe = 0.0

    # Win rate
    wins = sum(1 for r in daily_returns if r > 0)
    win_rate = wins / len(daily_returns) * 100 if daily_returns else 0

    # Turnover (number of rebalances / years)
    n_rebalances = len(signals)
    turnover = n_rebalances / years if years > 0 else 0

    # 期末组合绝对市值（按年）——注意：这是市值不是收益率，故命名 portfolio_values
    portfolio_values = defaultdict(float)
    for point in equity_curve:
        year = point["date"][:4]
        portfolio_values[year] = point["value"]

    # 全 mock：所有指标加 mock_ 前缀，避免被误当真；无真实基准数据，不输出 alpha/benchmark。
    return {
        "is_mock": True,
        "mock_cagr_pct": round(cagr, 2),
        "mock_max_drawdown_pct": round(max_dd * 100, 2),
        "mock_sharpe": round(sharpe, 2),
        "mock_win_rate_pct": round(win_rate, 2),
        "mock_turnover_per_year": round(turnover, 2),
        "portfolio_values": {k: round(v, 2) for k, v in sorted(portfolio_values.items())},
        "note": "MOCK 数据 — v0 管道占位，绝不可用于策略评估。无真实基准，不输出 alpha/超额。",
    }
