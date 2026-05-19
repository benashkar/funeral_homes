"""Main scraper for Legacy.com obituary listing and detail pages."""

import json
import re

from bs4 import BeautifulSoup

from scraper.url_builder import build_listing_url, build_listing_urls
from scraper.obit_parser import (
    parse_name, parse_dates, parse_funeral_home, parse_funeral_home_detail,
    parse_obit_text, parse_photo_url, parse_death_place,
    parse_death_date_from_text, parse_funeral_home_from_text,
)
from utils.logger import get_logger
from utils.rate_limiter import create_session, polite_get

logger = get_logger(__name__)

# Matches Legacy.com obituary detail URLs — three formats:
#   /us/obituaries/name/john-smith-obituary             (standard, pre-2026)
#   /us/obituaries/claxtonenterprise/name/judy-anderson (publication-scoped)
#   /person/Janet-S.-Bick-61392356                       (Next.js / RSC, 2026 H2+)
_LEGACY_OBIT_URL_RE = re.compile(
    r"/us/obituaries/(?:[^/]+/)?name/|/person/[A-Za-z0-9._'-]+-\d+\b",
    re.IGNORECASE,
)

# Listing pages embed the canonical URL on each result card via aria-label /
# href attribute. Selectors below cover the old anchor patterns and the new
# Next.js <a aria-label="View details for ..." href="/person/..."> link.
OBIT_CARD_LINK_SELECTOR = (
    "a[href*='/us/obituaries/'][href*='/name/'],"   # standard + publication-scoped
    "a.personalization-link,"                        # personalization-style links
    "div[data-component='ObituaryCard'] a[href],"    # component-based cards (legacy)
    "a[href^='/person/'][aria-label^='View details']"  # Next.js RSC cards (2026 H2+)
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

    def __init__(self, market, session=None, max_retries=None):
        self.market = market
        self.site_id = market["site_id"]
        self.listing_urls = build_listing_urls(market)
        self.listing_url = self.listing_urls[0]  # Primary URL
        self.session = session or create_session()
        self.max_retries = max_retries  # None = use polite_get default

    @staticmethod
    def _extract_listing_metadata(html):
        """Build a {url: {funeral_home, snippet, name}} map from listing-page cards.

        Legacy.com's Next.js (2026 H2+) detail pages at /person/{slug}-{id} no
        longer expose funeral_home or obit_text in JSON-LD — that data is only
        on the listing-page result cards. We harvest it here once per listing
        page so each obit can be enriched without an extra detail fetch.

        Card structure:
          <article data-testid="result-card-N">
            <a aria-label="View details for {NAME}" href="/person/...">
              <div>{NAME}</div><span>YYYY - YYYY</span>
            </a>
            <p title="{snippet}">{snippet}</p>
            <a><span data-slot="badge">{FUNERAL HOME}</span></a>
          </article>

        Args:
            html: Raw HTML string of a listing page.

        Returns:
            Dict keyed by absolute URL. Each value is a dict with optional keys
            funeral_home, snippet, name. Missing keys mean we couldn't extract
            that field for that card.
        """
        soup = BeautifulSoup(html, "lxml")
        metadata = {}
        for card in soup.select("article[data-testid^='result-card-']"):
            main_link = card.select_one(
                "a[aria-label^='View details for '][href^='/person/']"
            )
            if not main_link:
                continue
            href = main_link.get("href") or ""
            if not href:
                continue
            full_url = href if href.startswith("http") else LEGACY_DOMAIN + href

            entry = {}
            aria = main_link.get("aria-label") or ""
            if aria.startswith("View details for "):
                entry["name"] = aria[len("View details for "):].strip()

            snippet_el = card.select_one("p[title]")
            if snippet_el is not None:
                snippet = (snippet_el.get("title") or snippet_el.get_text(strip=True) or "").strip()
                if snippet:
                    entry["snippet"] = snippet

            fh_el = card.select_one("span[data-slot='badge']")
            if fh_el is not None:
                fh = fh_el.get_text(strip=True)
                if fh:
                    entry["funeral_home"] = fh

            if entry:
                metadata[full_url] = entry

        return metadata

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

    def _parse_detail_page(self, html, url, listing_meta=None):
        """Parse a single obituary detail page into a structured dict.

        Args:
            html: Raw HTML string of the detail page.
            url: The URL of this page (stored for dedup).
            listing_meta: Optional dict harvested from the listing page card
                ({name, snippet, funeral_home}). Used to backfill fields the
                new Next.js /person/ detail pages no longer expose in JSON-LD.

        Returns:
            Dict with keys: legacy_url, deceased_name, published_date,
            death_date, funeral_home, funeral_home_detail, obit_text, etc.
        """
        soup = BeautifulSoup(html, "lxml")
        dates = parse_dates(soup)
        place = parse_death_place(soup)
        obit_text = parse_obit_text(soup)
        listing_meta = listing_meta or {}

        # On /person/ pages, the detail page has no obit body — use the
        # listing-page snippet (the only place it still lives).
        if not obit_text and listing_meta.get("snippet"):
            obit_text = listing_meta["snippet"]

        # Fallback: parse death date from text if JSON-LD has no deathDate
        death_date = dates["death"]
        if not death_date and obit_text:
            death_date = parse_death_date_from_text(obit_text, dates["published"])

        # Fallback: parse funeral home from text if JSON-LD has no FH data
        funeral_home = parse_funeral_home(soup)
        if not funeral_home and obit_text:
            funeral_home = parse_funeral_home_from_text(obit_text)
        # Last resort: the funeral-home badge harvested from the listing card.
        if not funeral_home and listing_meta.get("funeral_home"):
            funeral_home = listing_meta["funeral_home"]

        # Name fallback chain: detail-page JSON-LD → listing-card aria-label.
        deceased_name = parse_name(soup) or listing_meta.get("name") or ""

        return {
            "legacy_url": url,
            "deceased_name": deceased_name,
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
        # Per-URL metadata harvested from listing-page cards (funeral_home,
        # snippet, name). Used by _parse_detail_page to backfill fields the
        # new /person/ pages no longer expose.
        listing_metadata = {}

        # Try each listing URL in order (primary + fallbacks).
        # Markets with a publication_slug have both publication and geographic URLs.
        active_listing_url = self.listing_url
        for listing_url_idx, candidate_url in enumerate(self.listing_urls):
            if listing_url_idx > 0:
                logger.info("[%s] Trying fallback listing URL: %s", self.site_id, candidate_url)
            active_listing_url = candidate_url
            first_page_worked = False

            for page_num in range(1, MAX_LISTING_PAGES + 1):
                page_url = active_listing_url if page_num == 1 else f"{active_listing_url}?page={page_num}"
                logger.info("[%s] Fetching listing page %d: %s", self.site_id, page_num, page_url)

                resp = polite_get(self.session, page_url, max_retries=self.max_retries)
                if not resp:
                    logger.error("[%s] Failed to fetch listing page %d (blocked/timeout)", self.site_id, page_num)
                    self._last_listing_diag = "listing_fetch_failed"
                    break

                page_urls = self._extract_obit_links(resp.text)
                # Harvest funeral_home + snippet from result-card HTML before
                # we move on; new /person/ pages have no other source for these.
                for url, meta in self._extract_listing_metadata(resp.text).items():
                    if url not in listing_metadata:
                        listing_metadata[url] = meta
                if not page_urls:
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

                first_page_worked = True
                all_obit_urls.extend(page_urls)

                new_on_page = [u for u in page_urls if u not in known_urls]
                if not new_on_page:
                    logger.info("[%s] Page %d had 0 new URLs — stopping pagination", self.site_id, page_num)
                    break

            # If this URL worked, don't try fallbacks
            if first_page_worked or all_obit_urls:
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
                obit = self._parse_detail_page(
                    detail_resp.text, url, listing_meta=listing_metadata.get(url),
                )
                results.append(obit)
                logger.info("[%s] Parsed: %s", self.site_id, obit.get("deceased_name", "unknown"))
            except Exception as e:
                logger.error("[%s] Parse error for %s: %s", self.site_id, url, e)

        return results
