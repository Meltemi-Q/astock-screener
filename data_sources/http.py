#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared HTTP utilities with caching for all data sources.

Replicates the http_get / get_json pattern from astock_screener.py
so every data source module can import from here.
"""

import os
import json
import time
import ssl
import hashlib
from urllib import request

# ── Paths ──────────────────────────────────────────────────
# data_sources/http.py → WORKDIR = project root (one level up)
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(WORKDIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── SSL ────────────────────────────────────────────────────
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE  # public read-only data, ok to skip cert verify

# ── Defaults ───────────────────────────────────────────────
DEFAULT_TIMEOUT = 25
DEFAULT_RETRIES = 4
DEFAULT_CACHE_HOURS = 6


def _http_get(url, headers=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
    """GET a URL with automatic retries on failure.

    Args:
        url: Full URL to fetch.
        headers: Optional dict of HTTP headers.
        timeout: Seconds before timing out each attempt.
        retries: Number of retry attempts.

    Returns:
        str: Decoded response body.

    Raises:
        The last exception after exhausting retries.
    """
    last = None
    for i in range(retries):
        try:
            req = request.Request(url, headers=headers or {})
            with request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def _cache_uid(url):
    """Return a deterministic cache key (md5 hex) for a URL."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def get_json(url, ttl_hours=None, headers=None):
    """GET JSON with local file cache (keyed by URL md5).

    On cache miss the raw response is fetched, parsed as JSON, and written
    to CACHE_DIR.  On cache hit the cached JSON is returned directly.

    Args:
        url: Full URL.
        ttl_hours: Cache lifetime in hours (default: DEFAULT_CACHE_HOURS).
        headers: Optional dict of HTTP headers.

    Returns:
        Parsed JSON object (dict, list, etc.).
    """
    ttl = (DEFAULT_CACHE_HOURS if ttl_hours is None else ttl_hours) * 3600
    uid = _cache_uid(url)
    fp = os.path.join(CACHE_DIR, uid + ".json")
    if os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < ttl:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    raw = _http_get(url, headers=headers)
    d = json.loads(raw)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass
    return d


def get_text(url, ttl_hours=None, headers=None):
    """GET plain text with local file cache (same pattern as get_json).

    Args:
        url: Full URL.
        ttl_hours: Cache lifetime in hours (default: DEFAULT_CACHE_HOURS).
        headers: Optional dict of HTTP headers.

    Returns:
        str: Response body.
    """
    ttl = (DEFAULT_CACHE_HOURS if ttl_hours is None else ttl_hours) * 3600
    uid = _cache_uid(url)
    fp = os.path.join(CACHE_DIR, uid + ".txt")
    if os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < ttl:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    raw = _http_get(url, headers=headers)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception:
        pass
    return raw


def _http_get_bytes(url, headers=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
    """GET raw bytes with automatic retries on failure.

    Args:
        url: Full URL to fetch.
        headers: Optional dict of HTTP headers.
        timeout: Seconds before timing out each attempt.
        retries: Number of retry attempts.

    Returns:
        bytes: Raw response body.

    Raises:
        The last exception after exhausting retries.
    """
    last = None
    for i in range(retries):
        try:
            req = request.Request(url, headers=headers or {})
            with request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def get_bytes(url, ttl_hours=None, headers=None):
    """GET raw bytes with local file cache (for binary files like xlsx).

    Args:
        url: Full URL.
        ttl_hours: Cache lifetime in hours (default: DEFAULT_CACHE_HOURS).
        headers: Optional dict of HTTP headers.

    Returns:
        bytes: Raw response body.
    """
    ttl = (DEFAULT_CACHE_HOURS if ttl_hours is None else ttl_hours) * 3600
    uid = _cache_uid(url)
    fp = os.path.join(CACHE_DIR, uid + ".bin")
    if os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < ttl:
        try:
            with open(fp, "rb") as f:
                return f.read()
        except Exception:
            pass
    raw = _http_get_bytes(url, headers=headers)
    try:
        with open(fp, "wb") as f:
            f.write(raw)
    except Exception:
        pass
    return raw
