"""Polite rate limiting and HTTP session for Legacy.com scraping."""

import time

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; CR-NewsBot/1.0; +https://crcommunity.news)"
DEFAULT_DELAY = 2  # seconds between requests
RETRY_DELAY = 60   # seconds to wait on 429/503 before one retry


def create_session():
    """Return a requests.Session with the polite User-Agent header."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def polite_get(session, url, delay=DEFAULT_DELAY):
    """GET a URL with polite delay and retry logic for 429/503.

    Args:
        session: requests.Session with headers configured.
        url: Target URL.
        delay: Seconds to sleep before the request.

    Returns:
        requests.Response on success, None on failure.
    """
    time.sleep(delay)

    try:
        resp = session.get(url, timeout=30)
    except requests.RequestException as e:
        logger.error("Request failed for %s: %s", url, e)
        return None

    if resp.status_code in (429, 503):
        logger.warning("Got %d for %s — waiting %ds and retrying once", resp.status_code, url, RETRY_DELAY)
        time.sleep(RETRY_DELAY)
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            logger.error("Retry failed for %s: %s", url, e)
            return None

    if resp.status_code != 200:
        logger.warning("Non-200 status %d for %s", resp.status_code, url)
        return None

    return resp
