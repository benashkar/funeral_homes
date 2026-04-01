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
