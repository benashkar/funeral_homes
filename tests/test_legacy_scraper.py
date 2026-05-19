"""Tests for scraper.legacy_scraper — URL extraction and filtering."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.legacy_scraper import LegacyScraper


# --- Fixture: JSON-LD ItemList with mixed URL types ---

LISTING_HTML = """
<html>
<head>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "WebPage",
        "mainEntity": {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "url": "https://www.legacy.com/us/obituaries/name/john-smith-obituary?id=111"},
                {"@type": "ListItem", "position": 2, "url": "https://www.legacy.com/us/obituaries/claxtonenterprise/name/judy-anderson-obituary?id=222"},
                {"@type": "ListItem", "position": 3, "url": "https://www.legacy.com/us/obituaries/cassville-democrat/name/bob-jones-obituary?id=333"},
                {"@type": "ListItem", "position": 4, "url": "https://www.moodysfuneralhome.com/obituary/Rebecca-Futch"},
                {"@type": "ListItem", "position": 5, "url": "https://www.goodshepherdfh.net/obituary/Roman-Holloway"},
                {"@type": "ListItem", "position": 6, "url": "https://www.legacy.com/us/obituaries/local/georgia/claxton"}
            ]
        }
    }
    </script>
</head>
<body></body>
</html>
"""

EMPTY_LISTING_HTML = "<html><head></head><body></body></html>"

# CSS fallback fixture — no JSON-LD
CSS_LISTING_HTML = """
<html>
<head></head>
<body>
    <a href="/us/obituaries/name/alice-walker-obituary?id=444">Alice Walker</a>
    <a href="/us/obituaries/nytimes/name/carol-davis-obituary?id=555">Carol Davis</a>
    <a class="personalization-link" href="/us/obituaries/name/dave-brown-obituary?id=666">Dave Brown</a>
    <a href="https://www.somefuneralhome.com/obit/123">External</a>
</body>
</html>
"""


def _make_scraper():
    market = {"site_id": "ga-claxton", "state": "georgia", "legacy_slug": "claxton", "type": "city"}
    return LegacyScraper(market)


class TestExtractObitLinks:
    """Tests for _extract_obit_links URL filtering."""

    def test_extracts_standard_legacy_url(self):
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(LISTING_HTML)
        assert any("john-smith-obituary" in u for u in urls)

    def test_extracts_publication_scoped_url(self):
        """Publication-scoped URLs like /obituaries/claxtonenterprise/name/ must be captured."""
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(LISTING_HTML)
        assert any("judy-anderson-obituary" in u for u in urls)
        assert any("bob-jones-obituary" in u for u in urls)

    def test_excludes_external_funeral_home_urls(self):
        """External funeral home URLs should NOT be extracted."""
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(LISTING_HTML)
        assert not any("moodysfuneralhome" in u for u in urls)
        assert not any("goodshepherdfh" in u for u in urls)

    def test_excludes_listing_page_urls(self):
        """Legacy.com listing/category URLs should NOT be extracted."""
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(LISTING_HTML)
        assert not any(u.endswith("/claxton") for u in urls)

    def test_returns_exactly_three_legacy_urls(self):
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(LISTING_HTML)
        assert len(urls) == 3

    def test_empty_listing_returns_empty(self):
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(EMPTY_LISTING_HTML)
        assert urls == []

    def test_css_fallback_extracts_urls(self):
        """When JSON-LD is missing, CSS selectors should work."""
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(CSS_LISTING_HTML)
        assert len(urls) >= 2
        assert any("alice-walker" in u for u in urls)
        assert any("carol-davis" in u for u in urls)

    def test_css_fallback_excludes_external(self):
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(CSS_LISTING_HTML)
        assert not any("somefuneralhome" in u for u in urls)


# --- Fixture: Next.js (2026 H2+) RSC result cards ---

NEXTJS_LISTING_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"WebPage","mainEntity":{"@type":"ItemList","itemListElement":[
  {"@type":"ListItem","position":1,"url":"https://www.legacy.com/person/Janet-S.-Bick-61392356","name":"Janet S. Bick Obituary"},
  {"@type":"ListItem","position":2,"url":"https://www.legacy.com/person/David-Eyre-61357971","name":"David Eyre Obituary"}
]}}
</script>
</head>
<body>
<article data-testid="result-card-0">
  <a aria-label="View details for Janet S. Bick" href="/person/Janet-S.-Bick-61392356">
    <div>Janet S. Bick</div>
    <span>1931 - 2026</span>
  </a>
  <p title="Janet S. Bick passed away on Saturday, May 16, 2026. The family is being cared for by E C Nurre Funeral Home.">snippet</p>
  <a href="https://www.ecnurre.com/obituaries/Janet-S-Bick"><span data-slot="badge">E C Nurre Funeral Home</span></a>
</article>
<article data-testid="result-card-1">
  <a aria-label="View details for David Eyre" href="/person/David-Eyre-61357971">
    <div>David Eyre</div>
  </a>
  <p title="David L. Eyre, age 77, of Sardinia, Ohio, passed away on Monday, May 11, 2026.">snippet2</p>
  <a><span data-slot="badge">Edgington Funeral Home - Mowrystown</span></a>
</article>
</body></html>
"""


class TestExtractObitLinksNextJs:
    """Regression tests for the Next.js / RSC listing format (2026 H2+)."""

    def test_extracts_person_urls_from_jsonld(self):
        scraper = _make_scraper()
        urls = scraper._extract_obit_links(NEXTJS_LISTING_HTML)
        assert "https://www.legacy.com/person/Janet-S.-Bick-61392356" in urls
        assert "https://www.legacy.com/person/David-Eyre-61357971" in urls

    def test_person_urls_match_filter_regex(self):
        from scraper.legacy_scraper import _LEGACY_OBIT_URL_RE
        assert _LEGACY_OBIT_URL_RE.search("https://www.legacy.com/person/Janet-S.-Bick-61392356")
        assert _LEGACY_OBIT_URL_RE.search("/person/Jane-Doe-12345")
        # Must NOT match listing/category paths
        assert not _LEGACY_OBIT_URL_RE.search("https://www.legacy.com/us/obituaries/local/ohio")
        assert not _LEGACY_OBIT_URL_RE.search("/person/")


class TestListingMetadataHarvest:
    """Tests for _extract_listing_metadata (funeral_home + snippet harvesting)."""

    def test_harvests_funeral_home_and_snippet(self):
        meta = LegacyScraper._extract_listing_metadata(NEXTJS_LISTING_HTML)
        assert "https://www.legacy.com/person/Janet-S.-Bick-61392356" in meta
        entry = meta["https://www.legacy.com/person/Janet-S.-Bick-61392356"]
        assert entry["name"] == "Janet S. Bick"
        assert entry["funeral_home"] == "E C Nurre Funeral Home"
        assert "passed away" in entry["snippet"]

    def test_returns_empty_when_no_cards(self):
        meta = LegacyScraper._extract_listing_metadata("<html><body></body></html>")
        assert meta == {}

    def test_old_format_listing_returns_empty(self):
        # Old listing pages don't have data-testid="result-card-*", so this
        # harvester is a no-op there — the existing JSON-LD path still works.
        meta = LegacyScraper._extract_listing_metadata(LISTING_HTML)
        assert meta == {}
