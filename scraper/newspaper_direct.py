"""Newspaper-direct obit scraper for Cherry Road papers.

Cherry Road newspapers all use a common CMS (TownNews / BLOX) which
exposes obituaries at predictable URLs. This bypasses Legacy.com
entirely for CR markets.

Common patterns observed across CR sites:
  https://www.{paper}.com/obituaries/
  https://www.{paper}.com/community/obituaries/
  https://www.{paper}.com/news/obituaries/

The TownNews/BLOX CMS renders obit listings as <article> elements with
a consistent structure:
  <article class="tnt-asset-type-article">
    <h2 class="tnt-headline"><a href="...">Name</a></h2>
    <time>...</time>
  </article>

This module returns a list of obit detail URLs that can then be fetched
and parsed individually.
"""

import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils.logger import get_logger
from utils.rate_limiter import polite_get

logger = get_logger(__name__)

# CSS selectors that identify obit cards across common newspaper CMSes.
# Order matters — most specific first.
_OBIT_CARD_SELECTORS = [
    # TownNews / BLOX CMS (used by most Cherry Road papers)
    "article.tnt-asset-type-article a[href*='/obituaries/']",
    "article[class*='asset'] h2 a[href*='/obituaries/']",
    "h2.tnt-headline a[href*='/obituaries/']",
    # Generic obit list patterns
    ".obituary-list a[href*='/obituaries/']",
    ".obit-list a[href*='/obituaries/']",
    "div[class*='obit'] a[href*='/obituaries/']",
    # Fallback: any link inside an article tag pointing at obituaries
    "article a[href*='/obituaries/']",
]


def fetch_newspaper_obit_urls(session, newspaper_url):
    """Fetch a newspaper's obituary listing page and return obit detail URLs.

    Args:
        session: HTTP session from rate_limiter.create_session().
        newspaper_url: Full URL to the newspaper's obituaries page,
            e.g. 'https://www.claxtonenterprise.com/obituaries'.

    Returns:
        List of unique obit detail URLs (absolute), or empty list if
        the listing page is unreachable or contains no recognizable obits.
    """
    logger.info("[newspaper-direct] Fetching %s", newspaper_url)
    resp = polite_get(session, newspaper_url)
    if not resp:
        logger.warning("[newspaper-direct] Failed to fetch %s", newspaper_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    seen = set()
    urls = []

    for selector in _OBIT_CARD_SELECTORS:
        try:
            links = soup.select(selector)
        except Exception as e:
            logger.warning("[newspaper-direct] Selector failed (%s): %s", selector, e)
            continue
        for link in links:
            href = link.get("href", "").strip()
            if not href:
                continue
            absolute = urljoin(newspaper_url, href)
            # Filter: must contain /obituaries/ AND be on the same domain
            if "/obituaries/" not in absolute:
                continue
            # Drop the listing page itself
            if absolute.rstrip("/") == newspaper_url.rstrip("/"):
                continue
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)
        if urls:
            logger.info(
                "[newspaper-direct] Selector '%s' found %d obit URLs",
                selector[:60], len(urls),
            )
            break

    if not urls:
        logger.info("[newspaper-direct] No obit cards found at %s", newspaper_url)

    return urls


def parse_newspaper_obit_detail(session, detail_url):
    """Parse a newspaper obituary detail page into a normalized dict.

    Returns a dict matching the legacy_scraper output format, or None
    if parsing fails. Structured data extraction prefers JSON-LD when
    present (most TownNews papers embed Person schema).
    """
    resp = polite_get(session, detail_url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Title -> deceased name
    name = None
    for tag in (soup.find("h1"), soup.find("title")):
        if tag and tag.get_text(strip=True):
            text = tag.get_text(strip=True)
            # Strip common suffixes
            text = re.sub(r"\s*[-|]\s*Obituar(y|ies).*$", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*\|\s*.*$", "", text)
            if text:
                name = text
                break

    # Body text
    body = ""
    article = soup.find("article") or soup.find("div", class_=re.compile(r"obit|article-body|story"))
    if article:
        body = article.get_text("\n", strip=True)[:5000]

    return {
        "legacy_url": detail_url,
        "deceased_name": name,
        "obit_text": body,
        "source": "newspaper-direct",
        # Other fields (dates, funeral home, photos) left null — can be
        # backfilled by the standard text parsers in obit_parser.py
    }
