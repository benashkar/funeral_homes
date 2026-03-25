"""Tests for scraper.obit_parser — happy path and missing fields.

The parser extracts data from JSON-LD script blocks (not CSS selectors)
because Legacy.com detail pages are JS-rendered.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from bs4 import BeautifulSoup

from scraper.obit_parser import parse_name, parse_dates, parse_funeral_home, parse_obit_text, parse_photo_url, parse_death_place


# --- Fixtures: realistic Legacy.com JSON-LD structured data ---

FULL_OBIT_HTML = """
<html>
<head>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "NewsArticle",
        "articleBody": "John Michael Smith, 81, of Springfield, passed away peacefully on February 28, 2026. He is survived by his wife, Mary, and three children.",
        "datePublished": "2026-03-01T12:00:00.000Z",
        "headline": "John Michael Smith Obituary"
    }
    </script>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "Person",
        "name": "John Michael Smith",
        "givenName": "John",
        "familyName": "Smith",
        "additionalName": "Michael",
        "deathDate": "2026-2-28",
        "birthDate": "1945-1-5",
        "image": "https://cache.legacy.net/photos/12345.jpg",
        "deathPlace": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Springfield", "addressRegion": "OH"}}
    }
    </script>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "item": {"@id": "https://www.legacy.com", "name": "Home"}},
            {"@type": "ListItem", "position": 2, "item": {"@id": "https://www.legacy.com/funeral-homes/listing/ohio", "name": "Ohio"}},
            {"@type": "ListItem", "position": 3, "item": {"@id": "https://www.legacy.com/funeral-homes/listing/ohio/springfield", "name": "Springfield"}},
            {"@type": "ListItem", "position": 4, "item": {"@id": "https://www.legacy.com/funeral-homes/ohio/springfield/greenfield-memorial-chapel/fh-1234", "name": "Greenfield Memorial Chapel"}}
        ]
    }
    </script>
</head>
<body></body>
</html>
"""

MINIMAL_OBIT_HTML = """
<html>
<head>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "NewsArticle",
        "articleBody": "A beloved member of the community has passed away.",
        "datePublished": "2026-03-15T00:00:00.000Z",
        "headline": "Community Member Obituary"
    }
    </script>
</head>
<body></body>
</html>
"""

EMPTY_HTML = "<html><head></head><body></body></html>"


# --- parse_name tests ---

def test_parse_name_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    assert parse_name(soup) == "John Michael Smith"


def test_parse_name_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    assert parse_name(soup) is None


def test_parse_name_fallback_headline():
    """When Person block is missing, falls back to NewsArticle headline."""
    soup = BeautifulSoup(MINIMAL_OBIT_HTML, "lxml")
    assert parse_name(soup) == "Community Member"


def test_parse_name_strips_year_and_funeral_home():
    """Headline like 'Leon G Kober Obituary 2026 - Phillip Funeral Home' should clean to just name."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"http://schema.org","@type":"NewsArticle",
     "headline":"Leon G Kober Obituary 2026 - Phillip Funeral Home",
     "articleBody":"Pending.","datePublished":"2026-03-24T00:00:00.000Z"}
    </script></head><body></body></html>"""
    soup = BeautifulSoup(html, "lxml")
    assert parse_name(soup) == "Leon G Kober"


def test_parse_funeral_home_from_headline():
    """When no BreadcrumbList /fh-, extract funeral home from headline."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"http://schema.org","@type":"NewsArticle",
     "headline":"Leon G Kober Obituary 2026 - Phillip Funeral Home",
     "articleBody":"Pending.","datePublished":"2026-03-24T00:00:00.000Z"}
    </script></head><body></body></html>"""
    soup = BeautifulSoup(html, "lxml")
    assert parse_funeral_home(soup) == "Phillip Funeral Home"


# --- parse_dates tests ---

def test_parse_dates_full():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    dates = parse_dates(soup)
    assert dates["death"] == date(2026, 2, 28)
    assert dates["published"] == date(2026, 3, 1)


def test_parse_dates_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    dates = parse_dates(soup)
    assert dates["death"] is None
    assert dates["published"] is None


def test_parse_dates_no_person():
    """Published date works even without Person schema."""
    soup = BeautifulSoup(MINIMAL_OBIT_HTML, "lxml")
    dates = parse_dates(soup)
    assert dates["death"] is None
    assert dates["published"] is not None


# --- parse_funeral_home tests ---

def test_parse_funeral_home_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    assert parse_funeral_home(soup) == "Greenfield Memorial Chapel"


def test_parse_funeral_home_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    assert parse_funeral_home(soup) is None


# --- parse_obit_text tests ---

def test_parse_obit_text_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    text = parse_obit_text(soup)
    assert "John Michael Smith, 81" in text
    assert "survived by his wife" in text


def test_parse_obit_text_minimal():
    soup = BeautifulSoup(MINIMAL_OBIT_HTML, "lxml")
    text = parse_obit_text(soup)
    assert "beloved member" in text


def test_parse_obit_text_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    assert parse_obit_text(soup) is None


# --- parse_photo_url tests ---

def test_parse_photo_url_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    assert parse_photo_url(soup) == "https://cache.legacy.net/photos/12345.jpg"


def test_parse_photo_url_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    assert parse_photo_url(soup) is None


def test_parse_photo_url_no_person():
    soup = BeautifulSoup(MINIMAL_OBIT_HTML, "lxml")
    assert parse_photo_url(soup) is None


# --- parse_death_place tests ---

def test_parse_death_place_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    place = parse_death_place(soup)
    assert place["city"] == "Springfield"
    assert place["state"] == "OH"


def test_parse_death_place_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    place = parse_death_place(soup)
    assert place["city"] is None
    assert place["state"] is None


def test_parse_death_place_og_title_fallback():
    """When Person block is missing, extract city from og:title meta tag."""
    html = """<html><head>
    <meta property="og:title" content="John Smith Obituary (2026) - Madison, WI - Smith Funeral Home">
    <script type="application/ld+json">
    {"@context":"http://schema.org","@type":"NewsArticle","articleBody":"John passed away.","datePublished":"2026-03-24T00:00:00.000Z"}
    </script>
    </head><body></body></html>"""
    soup = BeautifulSoup(html, "lxml")
    place = parse_death_place(soup)
    assert place["city"] == "Madison"
    assert place["state"] == "WI"
