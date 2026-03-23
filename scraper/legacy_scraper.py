"""Main scraper for Legacy.com obituary listing and detail pages."""

import json

from bs4 import BeautifulSoup

from scraper.url_builder import build_listing_url
from scraper.obit_parser import parse_name, parse_dates, parse_funeral_home, parse_obit_text, parse_photo_url
from utils.logger import get_logger
from utils.rate_limiter import create_session, polite_get

logger = get_logger(__name__)

# CSS selectors for the listing page — fallback if JSON-LD is missing.
# Legacy.com listing pages use personalization-link anchors inside result cards.
OBIT_CARD_LINK_SELECTOR = (
    "a[href*='/us/obituaries/name/'],"           # standard obit detail links
    "a.personalization-link,"                      # personalization-style links
    "div[data-component='ObituaryCard'] a[href]"   # component-based cards
)

# Base domain for resolving relative URLs
LEGACY_DOMAIN = "https://www.legacy.com"


class LegacyScraper:
    """Scrapes obituaries from Legacy.com for a single market.

    Args:
        market: Dict from markets.json with keys site_id, state, legacy_slug, type.
    """

    def __init__(self, market, session=None):
        self.market = market
        self.site_id = market["site_id"]
        self.listing_url = build_listing_url(market)
        self.session = session or create_session()

    def _extract_obit_links(self, html):
        """Parse the listing page HTML and return unique obit detail URLs.

        Legacy.com embeds obituary URLs in Schema.org JSON-LD (application/ld+json)
        as an ItemList. We extract from there first, then fall back to CSS selectors
        for anchor tags.

        Args:
            html: Raw HTML string of the listing page.

        Returns:
            List of absolute URL strings.
        """
        soup = BeautifulSoup(html, "lxml")
        urls = set()

        # Primary: extract from JSON-LD structured data.
        # Detail URLs require the ?id= param to resolve (422 without it).
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            main_entity = data.get("mainEntity") if isinstance(data, dict) else None
            if not main_entity:
                continue
            for item in main_entity.get("itemListElement", []):
                item_url = item.get("url") or ""
                if "/us/obituaries/name/" in item_url.lower():
                    urls.add(item_url)

        if urls:
            logger.info("[%s] Extracted %d URLs from JSON-LD", self.site_id, len(urls))
            return list(urls)

        # Fallback: CSS selectors on anchor tags
        links = soup.select(OBIT_CARD_LINK_SELECTOR)
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = LEGACY_DOMAIN + href
            if "/us/obituaries/name/" in href.lower():
                urls.add(href.split("?")[0])

        return list(urls)

    def _parse_detail_page(self, html, url):
        """Parse a single obituary detail page into a structured dict.

        Args:
            html: Raw HTML string of the detail page.
            url: The URL of this page (stored for dedup).

        Returns:
            Dict with keys: legacy_url, deceased_name, published_date,
            death_date, funeral_home, obit_text.
        """
        soup = BeautifulSoup(html, "lxml")
        dates = parse_dates(soup)

        return {
            "legacy_url": url,
            "deceased_name": parse_name(soup),
            "published_date": dates["published"],
            "death_date": dates["death"],
            "funeral_home": parse_funeral_home(soup),
            "photo_url": parse_photo_url(soup),
            "obit_text": parse_obit_text(soup),
        }

    def scrape_today(self, known_urls=None):
        """Scrape today's obituaries for this market.

        Args:
            known_urls: Optional set of URLs already in DB to skip fetching.

        Returns:
            List of obit dicts ready for db_writer.upsert_obit.
        """
        known_urls = known_urls or set()

        logger.info("[%s] Fetching listing: %s", self.site_id, self.listing_url)
        resp = polite_get(self.session, self.listing_url)
        if not resp:
            logger.error("[%s] Failed to fetch listing page", self.site_id)
            return []

        obit_urls = self._extract_obit_links(resp.text)
        logger.info("[%s] Found %d obit links on listing page", self.site_id, len(obit_urls))

        # Filter out already-known URLs
        new_urls = [u for u in obit_urls if u not in known_urls]
        logger.info("[%s] %d new URLs to fetch", self.site_id, len(new_urls))

        results = []
        for url in new_urls:
            detail_resp = polite_get(self.session, url)
            if not detail_resp:
                logger.warning("[%s] Failed to fetch detail: %s", self.site_id, url)
                continue

            try:
                obit = self._parse_detail_page(detail_resp.text, url)
                results.append(obit)
                logger.info("[%s] Parsed: %s", self.site_id, obit.get("deceased_name", "unknown"))
            except Exception as e:
                logger.error("[%s] Parse error for %s: %s", self.site_id, url, e)

        return results
