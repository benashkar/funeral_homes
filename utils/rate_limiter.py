"""Polite rate limiting and HTTP session for Legacy.com scraping.

Thread-safe: uses a global lock so concurrent workers share one rate limit.
At scale (1,200+ markets), multiple threads scrape in parallel but total
request rate stays capped to avoid 429s from Legacy.com.
"""

import threading
import time

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; CR-NewsBot/1.0; +https://crcommunity.news)"
MIN_DELAY = 1.0  # minimum seconds between ANY two requests (global)
RETRY_DELAY = 60  # seconds to wait on 429/503 before one retry

# Global rate limiter — ensures all threads share one delay
_lock = threading.Lock()
_last_request_time = 0.0


def _rate_limit():
    """Block until MIN_DELAY has elapsed since the last request."""
    global _last_request_time
    with _lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < MIN_DELAY:
            time.sleep(MIN_DELAY - elapsed)
        _last_request_time = time.monotonic()


def create_session():
    """Return a requests.Session with the polite User-Agent header."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def polite_get(session, url):
    """GET a URL with global rate limiting and retry logic for 429/503.

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

    if resp.status_code in (429, 503):
        logger.warning("Got %d for %s — waiting %ds and retrying once", resp.status_code, url, RETRY_DELAY)
        time.sleep(RETRY_DELAY)
        _rate_limit()
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            logger.error("Retry failed for %s: %s", url, e)
            return None

    if resp.status_code != 200:
        logger.warning("Non-200 status %d for %s", resp.status_code, url)
        return None

    return resp
