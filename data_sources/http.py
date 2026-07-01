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
# 默认启用证书校验。确需自签代理/内网 host 时，通过环境变量显式放开：
#   HTTP_INSECURE_SSL=1              → 全局关闭校验（不推荐，仅调试用）
#   HTTP_INSECURE_HOSTS=a.com,b.com  → 仅对白名单 host 关闭校验
SSL_CTX = ssl.create_default_context()

_INSECURE_ALL = os.environ.get("HTTP_INSECURE_SSL", "").strip().lower() in ("1", "true", "yes")
_INSECURE_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("HTTP_INSECURE_HOSTS", "").split(",")
    if h.strip()
}
if _INSECURE_ALL:
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE

# 为白名单 host 预建一个不校验的 context（按需使用）
_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE


def _ctx_for(url):
    """按 host 白名单选择 SSL context。"""
    if _INSECURE_ALL:
        return SSL_CTX
    if _INSECURE_HOSTS:
        try:
            from urllib.parse import urlsplit
            host = (urlsplit(url).hostname or "").lower()
            if host in _INSECURE_HOSTS:
                return _INSECURE_CTX
        except Exception:
            pass
    return SSL_CTX


# ── Defaults ───────────────────────────────────────────────
DEFAULT_TIMEOUT = 25
DEFAULT_RETRIES = 4
DEFAULT_CACHE_HOURS = 6
# 网络失败兜底读旧缓存时，超过该时长的缓存视为过旧
STALE_CACHE_MAX_HOURS = 24 * 30  # 30 天


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
            with request.urlopen(req, timeout=timeout, context=_ctx_for(url)) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def _cache_uid(url):
    """Return a deterministic cache key (md5 hex) for a URL."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _atomic_write(fp, data, mode="w"):
    """原子写：先写同目录 .tmp 再 os.replace，避免半写文件污染缓存。

    ``data`` 为 str(mode="w") 或 bytes(mode="wb")。写失败静默忽略（缓存非关键）。
    """
    tmp = fp + ".tmp"
    try:
        if "b" in mode:
            with open(tmp, "wb") as f:
                f.write(data)
        else:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
        os.replace(tmp, fp)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _read_stale_cache(fp, loader):
    """网络失败时兜底读旧缓存，损坏或过旧则视为缺失(返回 (False, None))。

    - 用 ``loader(f)`` 解析文件；半写/损坏文件抛异常时不外泄，视为缓存缺失。
    - 校验 mtime：超过 STALE_CACHE_MAX_HOURS 视为过旧，打警告并拒用。

    返回 (ok, value)：ok=True 时 value 为缓存内容。
    """
    if not os.path.exists(fp):
        return (False, None)
    age_hours = (time.time() - os.path.getmtime(fp)) / 3600.0
    if age_hours > STALE_CACHE_MAX_HOURS:
        print(f"  ⚠ 缓存过旧({age_hours/24:.0f} 天)，拒用: {os.path.basename(fp)}")
        return (False, None)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            value = loader(f)
    except Exception:
        # 半写/损坏文件：视为缓存缺失，让上层继续抛原始网络异常
        return (False, None)
    print(f"  ⚠ 网络失败，使用过期缓存({age_hours:.1f}h): {os.path.basename(fp)}")
    return (True, value)


def get_json(url, ttl_hours=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
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
    try:
        raw = _http_get(url, headers=headers, timeout=timeout, retries=retries)
    except Exception:
        # 兜底读旧缓存：损坏(半写)视为缺失、过旧拒用，不因半写文件整链崩溃
        ok, value = _read_stale_cache(fp, json.load)
        if ok:
            return value
        raise
    d = json.loads(raw)
    _atomic_write(fp, json.dumps(d, ensure_ascii=False))
    return d


def get_text(url, ttl_hours=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):
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
    try:
        raw = _http_get(url, headers=headers, timeout=timeout, retries=retries)
    except Exception:
        # 兜底读旧缓存：损坏视为缺失、过旧拒用
        ok, value = _read_stale_cache(fp, lambda f: f.read())
        if ok:
            return value
        raise
    _atomic_write(fp, raw)
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
            with request.urlopen(req, timeout=timeout, context=_ctx_for(url)) as r:
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
    _atomic_write(fp, raw, mode="wb")
    return raw
