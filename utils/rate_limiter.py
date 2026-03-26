"""Polite rate limiting and HTTP session for Legacy.com scraping.

Thread-safe: uses a global lock so concurrent workers share one rate limit.
Treats 403 as rate limiting (Legacy.com's response to too many requests)
and backs off with exponential retry.
"""

import threading
import time

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
MIN_DELAY = 2.0   # seconds between ANY two requests (global)
BACKOFF_DELAY = 90  # seconds to wait on 403/429/503 before retry

# Global rate limiter — ensures all threads share one delay
_lock = threading.Lock()
_last_request_time = 0.0
# Track consecutive 403s to increase delay dynamically
_consecutive_blocks = 0


def _rate_limit():
    """Block until MIN_DELAY has elapsed since the last request."""
    global _last_request_time, _consecutive_blocks
    with _lock:
        now = time.monotonic()
        # Add extra delay if we've been getting blocked
        delay = MIN_DELAY + (_consecutive_blocks * 2)
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
    """Increment block counter and return backoff time."""
    global _consecutive_blocks
    with _lock:
        _consecutive_blocks = min(_consecutive_blocks + 1, 10)
        return BACKOFF_DELAY + (_consecutive_blocks * 30)


def create_session():
    """Return a requests.Session with browser-like headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def polite_get(session, url):
    """GET a URL with global rate limiting and retry on rate-limit responses.

    Retries on 403, 429, and 503 with exponential backoff.

    Args:
        session: requests.Session with headers configured.
        url: Target URL.

    Returns:
        requests.Response on success, None on failure.
    """
    _rate_limit()

    try:
        resp = session.get(url, timeout=30)
    except requests.RequestException as e:
        logger.error("Request failed for %s: %s", url, e)
        return None

    if resp.status_code in (403, 429, 503):
        backoff = _record_block()
        logger.warning(
            "Got %d for %s — backing off %ds before retry",
            resp.status_code, url, backoff,
        )
        time.sleep(backoff)
        _rate_limit()
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            logger.error("Retry failed for %s: %s", url, e)
            return None
        if resp.status_code in (403, 429, 503):
            logger.warning("Still blocked (%d) after retry for %s", resp.status_code, url)
            return None

    if resp.status_code != 200:
        logger.warning("Non-200 status %d for %s", resp.status_code, url)
        return None

    _record_success()
    return resp
