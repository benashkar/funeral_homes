"""Polite rate limiting and HTTP session for Legacy.com scraping.

Thread-safe: uses a global lock so concurrent workers share one rate limit.
Treats 403/429/503 as rate limiting (Legacy.com's response to too many
requests) and retries with exponential backoff up to MAX_RETRIES times.

Uses curl_cffi to impersonate Chrome 120's TLS fingerprint (JA3/JA4),
HTTP/2 framing, and header order — bypasses Legacy.com's bot detection
that targets the standard `requests` library's signature.
"""

import os
import random
import threading
import time

# curl_cffi is a drop-in replacement for `requests` that mimics real
# browser TLS fingerprints. Falls back to stdlib requests if unavailable.
try:
    from curl_cffi import requests as cffi_requests
    _USE_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    _USE_CURL_CFFI = False

import requests as _stdlib_requests

from utils.logger import get_logger

logger = get_logger(__name__)

# Rotating User-Agents to look less like a bot
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]
USER_AGENT = USER_AGENTS[0]  # Default for create_session

# Aggressive politeness settings — Legacy.com rate-limits Render IPs hard.
# Original 3s/120s/1-retry config caused 84% of listing fetches to fail.
MIN_DELAY = 6.0       # seconds between ANY two requests (global)
MAX_RETRIES = 4       # total attempts including first try
INITIAL_BACKOFF = 60  # seconds to wait on first 403/429/503

# Residential proxy support — set PROXY_URL env var to route requests
# through a proxy service (Bright Data, SmartProxy, etc.).
# Example: PROXY_URL=http://user:pass@proxy.brightdata.com:22225
# When set, ALL requests go through the proxy. When unset, direct.
PROXY_URL = os.environ.get("PROXY_URL", "")

# Global rate limiter — ensures all threads share one delay
_lock = threading.Lock()
_last_request_time = 0.0
# Track consecutive 403s to increase delay dynamically
_consecutive_blocks = 0


def _rate_limit():
    """Block until MIN_DELAY has elapsed since the last request.

    Adds dynamic extra delay if we've been getting blocked recently.
    """
    global _last_request_time, _consecutive_blocks
    with _lock:
        now = time.monotonic()
        # Add extra delay proportional to recent blocks (up to ~30s extra)
        delay = MIN_DELAY + min(_consecutive_blocks * 3, 30)
        # Add small random jitter to avoid thundering-herd patterns
        delay += random.uniform(0, 1.0)
        elapsed = now - _last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        _last_request_time = time.monotonic()


def _record_success():
    """Reset consecutive block counter on success."""
    global _consecutive_blocks
    with _lock:
        _consecutive_blocks = 0


def _record_block():
    """Increment block counter and return current count."""
    global _consecutive_blocks
    with _lock:
        _consecutive_blocks = min(_consecutive_blocks + 1, 10)
        return _consecutive_blocks


def create_session():
    """Return a session with browser-like headers and TLS fingerprint.

    When curl_cffi is available, the session impersonates Chrome 120 —
    matching its TLS handshake, HTTP/2 framing, and header order. This
    is critical to bypass Legacy.com's bot fingerprinting which blocks
    the default `requests` library on sight.

    If PROXY_URL env var is set, all requests are routed through the
    residential proxy (Bright Data, SmartProxy, IPRoyal, etc.).
    """
    if _USE_CURL_CFFI:
        session = cffi_requests.Session(impersonate="chrome120")
        logger.info("[OK] HTTP session using curl_cffi (Chrome 120 impersonation)")
    else:
        session = cffi_requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        logger.warning("curl_cffi not installed — falling back to stdlib requests")

    if PROXY_URL:
        session.proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL,
        }
        # Log proxy host only (not credentials)
        proxy_host = PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL
        logger.info("[OK] Proxy configured: %s", proxy_host)

    return session


def polite_get(session, url, max_retries=None):
    """GET a URL with global rate limiting and retry on rate-limit responses.

    Two modes:
    - Direct (PROXY_URL unset): exponential 60/120/240/480s backoff on
      403/429/503. Caller-provided max_retries respected (1 for non-priority
      to avoid 12h cron timeout; 4 for priority).
    - Proxy (PROXY_URL set): each retry grabs a fresh exit IP from the
      gateway, so old-school backoff is obsolete. Forces at least 3 attempts
      regardless of caller's max_retries, with tiny 1-2s sleeps between —
      block rate drops from ~76% to ~20% at the cost of ~2-3x bandwidth
      on blocked markets only.

    Rotates User-Agent on each retry to evade fingerprint-based blocks.

    Returns:
        requests.Response on success, None on failure.
    """
    caller_retries = max_retries if max_retries is not None else MAX_RETRIES
    # With a rotating proxy, low retry counts waste the proxy's whole purpose.
    # Bump non-priority markets from 1 -> 3 so each gets 3 distinct exit IPs.
    retries = max(caller_retries, 3) if PROXY_URL else caller_retries

    for attempt in range(1, retries + 1):
        _rate_limit()

        # Rotate User-Agent on retries (only useful for stdlib requests path;
        # curl_cffi sessions manage their own headers via impersonation).
        if attempt > 1 and not _USE_CURL_CFFI:
            session.headers["User-Agent"] = random.choice(USER_AGENTS)

        try:
            resp = session.get(url, timeout=30)
        except (_stdlib_requests.RequestException, Exception) as e:
            # curl_cffi raises curl_cffi.CurlError, not requests.RequestException
            logger.error("Request failed (attempt %d) for %s: %s", attempt, url, e)
            if attempt < retries:
                # Proxy: retry near-immediately (fresh IP). Direct: full backoff.
                time.sleep(1.0 + random.uniform(0, 1)) if PROXY_URL \
                    else time.sleep(INITIAL_BACKOFF * (2 ** (attempt - 1)))
                continue
            return None

        if resp.status_code in (403, 429, 503):
            blocks = _record_block()
            if attempt >= retries:
                logger.warning(
                    "Still blocked (%d) after %d attempts for %s",
                    resp.status_code, attempt, url,
                )
                return None
            if PROXY_URL:
                # Proxy gateway rotates on next request; no need to cool down
                # a specific IP. Tiny jitter only.
                backoff = 1.0 + random.uniform(0, 1.5)
            else:
                # Direct: exponential 60/120/240/480s
                backoff = INITIAL_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 10)
            logger.warning(
                "Got %d for %s (attempt %d/%d, blocks=%d) — backing off %.1fs",
                resp.status_code, url, attempt, retries, blocks, backoff,
            )
            time.sleep(backoff)
            continue

        if resp.status_code != 200:
            logger.warning("Non-200 status %d for %s", resp.status_code, url)
            return None

        _record_success()
        return resp

    return None
