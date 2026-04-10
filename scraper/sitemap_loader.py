"""Legacy.com sitemap loader — pre-fetches obit URLs in bulk.

Legacy.com publishes a sitemap index at /sitemap.xml that lists obituary
sitemaps. Each child sitemap can contain up to 50,000 obituary URLs.

Loading from sitemaps is dramatically more efficient than crawling 2,659
county listing pages — one sitemap = thousands of obit URLs in a single
HTTP request, with much lower rate-limit pressure.

Usage:
    from scraper.sitemap_loader import load_sitemap_obit_urls
    urls = load_sitemap_obit_urls(session, since_date='2026-04-01')
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from utils.logger import get_logger
from utils.rate_limiter import polite_get

logger = get_logger(__name__)

SITEMAP_INDEX_URL = "https://www.legacy.com/sitemap.xml"
# Legacy.com obit URL pattern (handles both standard and publication-scoped):
#   /us/obituaries/name/john-smith-obituary
#   /us/obituaries/claxtonenterprise/name/jane-doe-obituary
_OBIT_URL_RE = re.compile(r"/us/obituaries/(?:[^/]+/)?name/", re.IGNORECASE)

# XML namespace for sitemaps
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _parse_xml(text):
    """Parse XML text, returning the root Element or None on error."""
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        logger.warning("Sitemap XML parse error: %s", e)
        return None


def _extract_sitemap_urls(text):
    """Parse a sitemap index, return list of child sitemap URLs."""
    root = _parse_xml(text)
    if root is None:
        return []
    urls = []
    for sitemap in root.findall("sm:sitemap", _NS):
        loc = sitemap.find("sm:loc", _NS)
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
    return urls


def _extract_obit_urls(text):
    """Parse a sitemap, return list of obituary URLs (filtered)."""
    root = _parse_xml(text)
    if root is None:
        return []
    urls = []
    for url_elem in root.findall("sm:url", _NS):
        loc = url_elem.find("sm:loc", _NS)
        if loc is not None and loc.text and _OBIT_URL_RE.search(loc.text):
            urls.append(loc.text.strip())
    return urls


def load_sitemap_obit_urls(session, max_sitemaps=20):
    """Fetch the Legacy.com sitemap index and return all obituary URLs.

    Args:
        session: HTTP session from rate_limiter.create_session().
        max_sitemaps: Maximum child sitemaps to fetch (each holds up to
            50K URLs). Default 20 = up to 1M URLs total.

    Returns:
        Set of obituary URLs found across all sitemaps. Empty set if
        the sitemap index is unreachable.
    """
    logger.info("Fetching sitemap index: %s", SITEMAP_INDEX_URL)
    resp = polite_get(session, SITEMAP_INDEX_URL)
    if not resp:
        logger.warning("Failed to fetch sitemap index — falling back to listing crawl")
        return set()

    child_sitemaps = _extract_sitemap_urls(resp.text)
    logger.info("Sitemap index contains %d child sitemaps", len(child_sitemaps))

    if not child_sitemaps:
        # Maybe the index URL itself returns obit URLs directly
        direct_obits = _extract_obit_urls(resp.text)
        if direct_obits:
            logger.info("Sitemap index returned %d direct obit URLs", len(direct_obits))
            return set(direct_obits)
        return set()

    all_obit_urls = set()
    for i, sitemap_url in enumerate(child_sitemaps[:max_sitemaps], 1):
        logger.info("Fetching child sitemap %d/%d: %s", i, min(len(child_sitemaps), max_sitemaps), sitemap_url)
        sitemap_resp = polite_get(session, sitemap_url)
        if not sitemap_resp:
            logger.warning("Failed to fetch child sitemap %s", sitemap_url)
            continue
        obit_urls = _extract_obit_urls(sitemap_resp.text)
        logger.info("  Found %d obit URLs", len(obit_urls))
        all_obit_urls.update(obit_urls)

    logger.info("Total obit URLs from sitemaps: %d", len(all_obit_urls))
    return all_obit_urls


def url_to_market_slug(url):
    """Extract the market slug (publication or city/county) from a Legacy.com obit URL.

    Examples:
        /us/obituaries/claxtonenterprise/name/jane-doe -> 'claxtonenterprise'
        /us/obituaries/name/john-smith -> None (geographic)

    Returns:
        The publication slug if URL is publication-scoped, else None.
    """
    m = re.search(r"/us/obituaries/([^/]+)/name/", url, re.IGNORECASE)
    if m:
        slug = m.group(1)
        # 'name' itself is not a publication slug
        if slug.lower() != "name":
            return slug
    return None
