#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validation helpers for generated market screener artifacts.

The rules here are intentionally strict enough to prevent sample/test output
from being shown as a production-ready full-market screen.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter


DATE_RE = re.compile(r"^\d{8}$")
TIER_ALIASES = {
    "A": "A_可买入",
    "B": "B_优质待跌",
    "C": "C_接近合格",
}

MARKET_RULES = {
    "cn": {
        "label": "A股",
        "prefix": "astock_screen",
        "shortlist_prefix": "astock_shortlist",
        "min_rows": 4000,
        "max_rows": 8000,
        "required_codes": (),
        "min_tier_signals": 1,
    },
    "hk": {
        "label": "港股",
        "prefix": "hkstock_screen",
        "shortlist_prefix": "hkstock_shortlist",
        "min_rows": 1000,
        "max_rows": 5000,
        "required_codes": ("00700", "09988"),
        "min_tier_signals": 1,
    },
    "us": {
        "label": "美股",
        "prefix": "usstock_screen",
        "shortlist_prefix": "usstock_shortlist",
        "min_rows": 3000,
        "max_rows": 12000,
        "required_codes": ("AAPL", "MSFT", "NVDA", "GOOGL"),
        "min_tier_signals": 1,
    },
}


def _csv_path(results_dir: str, market: str, ts: str) -> str:
    return os.path.join(results_dir, f"{MARKET_RULES[market]['prefix']}_{ts}.csv")


def _html_path(results_dir: str, market: str, ts: str) -> str:
    return os.path.join(results_dir, f"{MARKET_RULES[market]['prefix']}_{ts}.html")


def _md_path(results_dir: str, market: str, ts: str) -> str:
    return os.path.join(results_dir, f"{MARKET_RULES[market]['shortlist_prefix']}_{ts}.md")


def _iter_csv_timestamps(results_dir: str, market: str) -> list[str]:
    if market not in MARKET_RULES or not os.path.isdir(results_dir):
        return []
    prefix = MARKET_RULES[market]["prefix"]
    timestamps = []
    for name in os.listdir(results_dir):
        if not name.startswith(f"{prefix}_") or not name.endswith(".csv"):
            continue
        ts = name.replace(f"{prefix}_", "").replace(".csv", "")
        if DATE_RE.match(ts):
            timestamps.append(ts)
    return sorted(set(timestamps), reverse=True)


def validate_market_result(results_dir: str, market: str, ts: str) -> dict:
    """Validate one dated result bundle.

    Returns a JSON-serializable dict with ``valid``, row counts, tier counts,
    and exact reasons. Missing/invalid bundles are not deleted here; callers
    decide whether to hide or expose them.
    """
    if market not in MARKET_RULES:
        return {"valid": False, "errors": [f"unknown market: {market}"]}
    rule = MARKET_RULES[market]
    errors: list[str] = []
    warnings: list[str] = []
    csv_path = _csv_path(results_dir, market, ts)
    html_path = _html_path(results_dir, market, ts)
    md_path = _md_path(results_dir, market, ts)

    rows: list[dict] = []
    codes: set[str] = set()
    tier_counts: Counter = Counter()

    if not os.path.exists(csv_path):
        errors.append(f"missing csv: {os.path.basename(csv_path)}")
    else:
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
                    code = str(row.get("code", "")).strip().upper()
                    if code:
                        codes.add(code)
                    raw_tier = str(row.get("tier", "-")).strip() or "-"
                    tier_counts[TIER_ALIASES.get(raw_tier, raw_tier)] += 1
        except Exception as exc:
            errors.append(f"csv unreadable: {exc}")

    row_count = len(rows)
    if row_count < rule["min_rows"]:
        errors.append(
            f"row_count {row_count} below {rule['min_rows']} minimum for {rule['label']}"
        )
    if row_count > rule["max_rows"]:
        errors.append(
            f"row_count {row_count} above {rule['max_rows']} maximum for {rule['label']}"
        )

    for code in rule["required_codes"]:
        if code.upper() not in codes:
            errors.append(f"missing required code {code}")

    tier_signal_count = sum(
        tier_counts.get(tier, 0)
        for tier in ("A_可买入", "B_优质待跌", "C_接近合格")
    )
    if tier_signal_count < rule.get("min_tier_signals", 0):
        errors.append(
            f"tier_signal_count {tier_signal_count} below "
            f"{rule['min_tier_signals']} minimum for {rule['label']}"
        )

    if not os.path.exists(html_path):
        errors.append(f"missing html: {os.path.basename(html_path)}")
    if not os.path.exists(md_path):
        warnings.append(f"missing shortlist: {os.path.basename(md_path)}")

    return {
        "market": market,
        "label": rule["label"],
        "ts": ts,
        "valid": not errors,
        "row_count": row_count,
        "tier_counts": dict(tier_counts),
        "tier_signal_count": tier_signal_count,
        "required_codes": list(rule["required_codes"]),
        "errors": errors,
        "warnings": warnings,
        "csv": os.path.basename(csv_path),
        "html": os.path.basename(html_path),
        "md": os.path.basename(md_path),
    }


def latest_market_result(results_dir: str, market: str) -> dict:
    """Return latest valid result plus latest invalid evidence for a market."""
    timestamps = _iter_csv_timestamps(results_dir, market)
    latest_any = None
    for ts in timestamps:
        status = validate_market_result(results_dir, market, ts)
        if latest_any is None:
            latest_any = status
        if status["valid"]:
            return {
                "status": "ready",
                "latest": status,
                "latest_invalid": None,
                "checked": len(timestamps),
            }
    if latest_any is not None:
        return {
            "status": "invalid",
            "latest": None,
            "latest_invalid": latest_any,
            "checked": len(timestamps),
        }
    return {
        "status": "not_generated",
        "latest": None,
        "latest_invalid": None,
        "checked": 0,
    }
