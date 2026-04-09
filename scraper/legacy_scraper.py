"""Main scraper for Legacy.com obituary listing and detail pages."""

import json
import re

from bs4 import BeautifulSoup

from scraper.url_builder import build_listing_url
from scraper.obit_parser import (
    parse_name, parse_dates, parse_funeral_home, parse_funeral_home_detail,
    parse_obit_text, parse_photo_url, parse_death_place,
    parse_death_date_from_text, parse_funeral_home_from_text,
)
from utils.logger import get_logger
from utils.rate_limiter import create_session, polite_get

logger = get_logger(__name__)

# Matches Legacy.com obituary detail URLs — both standard and publication-scoped:
#   /us/obituaries/name/john-smith-obituary
#   /us/obituaries/claxtonenterprise/name/judy-anderson-obituary
_LEGACY_OBIT_URL_RE = re.compile(r'/us/obituaries/(?:[^/]+/)?name/', re.IGNORECASE)

# CSS selectors for the listing page — fallback if JSON-LD is missing.
# Legacy.com listing pages use personalization-link anchors inside result cards.
OBIT_CARD_LINK_SELECTOR = (
    "a[href*='/us/obituaries/'][href*='/name/'],"  # standard + publication-scoped
    "a.personalization-link,"                       # personalization-style links
    "div[data-component='ObituaryCard'] a[href]"    # component-based cards
)

# Base domain for resolving relative URLs
LEGACY_DOMAIN = "https://www.legacy.com"

# Safety limit for listing page pagination
MAX_LISTING_PAGES = 5


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

        Handles both standard and publication-scoped Legacy.com URLs:
          - /us/obituaries/name/john-smith-obituary
          - /us/obituaries/claxtonenterprise/name/judy-anderson-obituary

        External funeral home URLs (e.g. moodysfuneralhome.com) are logged but
        skipped — our parser relies on Legacy.com JSON-LD structure.

        Args:
            html: Raw HTML string of the listing page.

        Returns:
            List of absolute URL strings.
        """
        soup = BeautifulSoup(html, "lxml")
        urls = set()
        external_count = 0

        # Primary: extract from JSON-LD structured data.
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
                if _LEGACY_OBIT_URL_RE.search(item_url):
                    urls.add(item_url)
                elif item_url.startswith("http"):
                    external_count += 1

        if urls or external_count:
            logger.info(
                "[%s] JSON-LD: %d Legacy URLs, %d external URLs (skipped)",
                self.site_id, len(urls), external_count,
            )
            if urls:
                return list(urls)

        # Fallback: CSS selectors on anchor tags
        links = soup.select(OBIT_CARD_LINK_SELECTOR)
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = LEGACY_DOMAIN + href
            if _LEGACY_OBIT_URL_RE.search(href):
                urls.add(href.split("?")[0])

        return list(urls)

    def _parse_detail_page(self, html, url):
        """Parse a single obituary detail page into a structured dict.

        Args:
            html: Raw HTML string of the detail page.
            url: The URL of this page (stored for dedup).

        Returns:
            Dict with keys: legacy_url, deceased_name, published_date,
            death_date, funeral_home, funeral_home_detail, obit_text, etc.
        """
        soup = BeautifulSoup(html, "lxml")
        dates = parse_dates(soup)
        place = parse_death_place(soup)
        obit_text = parse_obit_text(soup)

        # Fallback: parse death date from text if JSON-LD has no deathDate
        death_date = dates["death"]
        if not death_date and obit_text:
            death_date = parse_death_date_from_text(obit_text, dates["published"])

        # Fallback: parse funeral home from text if JSON-LD has no FH data
        funeral_home = parse_funeral_home(soup)
        if not funeral_home and obit_text:
            funeral_home = parse_funeral_home_from_text(obit_text)

        return {
            "legacy_url": url,
            "deceased_name": parse_name(soup),
            "published_date": dates["published"],
            "death_date": death_date,
            "death_city": place["city"],
            "death_state": place["state"],
            "funeral_home": funeral_home,
            "funeral_home_detail": parse_funeral_home_detail(soup),
            "photo_url": parse_photo_url(soup),
            "obit_text": obit_text,
        }

    def scrape_today(self, known_urls=None):
        """Scrape today's obituaries for this market.

        Fetches multiple listing pages (paginated via ?page=N) until no more
        new URLs are found or MAX_LISTING_PAGES is reached.

        Args:
            known_urls: Optional set of URLs already in DB to skip fetching.

        Returns:
            List of obit dicts ready for db_writer.upsert_obit.
        """
        known_urls = known_urls or set()
        all_obit_urls = []

        for page_num in range(1, MAX_LISTING_PAGES + 1):
            page_url = self.listing_url if page_num == 1 else f"{self.listing_url}?page={page_num}"
            logger.info("[%s] Fetching listing page %d: %s", self.site_id, page_num, page_url)

            resp = polite_get(self.session, page_url)
            if not resp:
                logger.error("[%s] Failed to fetch listing page %d (blocked/timeout)", self.site_id, page_num)
                # Store diagnostic info so scrape_log captures the failure reason
                self._last_listing_diag = "listing_fetch_failed"
                break

            page_urls = self._extract_obit_links(resp.text)
            if not page_urls:
                # Diagnostic: log response details to understand WHY 0 links
                resp_len = len(resp.text)
                title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text[:2000], re.IGNORECASE | re.DOTALL)
                title = title_match.group(1).strip()[:80] if title_match else "no-title"
                has_captcha = "captcha" in resp.text[:5000].lower() or "challenge" in resp.text[:5000].lower()
                has_blocked = "blocked" in resp.text[:5000].lower() or "denied" in resp.text[:5000].lower()
                diag = f"status={resp.status_code},len={resp_len},title={title}"
                if has_captcha:
                    diag += ",CAPTCHA_DETECTED"
                if has_blocked:
                    diag += ",BLOCKED_DETECTED"
                logger.info("[%s] Page %d returned 0 Legacy URLs — %s", self.site_id, page_num, diag)
                self._last_listing_diag = diag
                break

            all_obit_urls.extend(page_urls)

            # If every URL on this page is already known, no point continuing
            new_on_page = [u for u in page_urls if u not in known_urls]
            if not new_on_page:
                logger.info("[%s] Page %d had 0 new URLs — stopping pagination", self.site_id, page_num)
                break

        # Deduplicate across pages while preserving order
        all_obit_urls = list(dict.fromkeys(all_obit_urls))
        logger.info("[%s] Found %d total obit links across pages", self.site_id, len(all_obit_urls))

        # Filter out already-known URLs
        new_urls = [u for u in all_obit_urls if u not in known_urls]
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
