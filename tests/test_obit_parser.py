"""Tests for scraper.obit_parser — happy path and missing fields.

The parser extracts data from JSON-LD script blocks (not CSS selectors)
because Legacy.com detail pages are JS-rendered.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from bs4 import BeautifulSoup

from scraper.obit_parser import parse_name, parse_dates, parse_funeral_home, parse_funeral_home_detail, parse_obit_text, parse_photo_url, parse_death_place, parse_death_date_from_text, parse_funeral_home_from_text


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


# --- parse_funeral_home_detail tests ---

def test_parse_funeral_home_detail_happy():
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    fh = parse_funeral_home_detail(soup)
    assert fh["legacy_fh_id"] == "fh-1234"
    assert fh["name"] == "Greenfield Memorial Chapel"
    assert fh["city"] == "Springfield"
    assert fh["state"] == "Ohio"
    assert "/fh-1234" in fh["legacy_url"]


def test_parse_funeral_home_detail_missing():
    soup = BeautifulSoup(EMPTY_HTML, "lxml")
    fh = parse_funeral_home_detail(soup)
    assert fh["legacy_fh_id"] is None
    assert fh["name"] is None
    assert fh["city"] is None
    assert fh["state"] is None
    assert fh["legacy_url"] is None


def test_parse_funeral_home_detail_no_breadcrumb():
    """When only NewsArticle exists (no BreadcrumbList), all fields are None."""
    soup = BeautifulSoup(MINIMAL_OBIT_HTML, "lxml")
    fh = parse_funeral_home_detail(soup)
    assert fh["legacy_fh_id"] is None
    assert fh["name"] is None


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


# --- parse_death_date_from_text tests ---

def test_death_date_passed_away_on():
    """Standard 'passed away on' phrasing."""
    text = "John Michael Smith, 81, of Springfield, passed away peacefully on February 28, 2026. He is survived by..."
    assert parse_death_date_from_text(text) == date(2026, 2, 28)


def test_death_date_range_dash():
    """Birth-death date range with dash."""
    text = "Jane Doe (January 5, 1945 - February 28, 2026) Jane was born in Columbus..."
    assert parse_death_date_from_text(text, date(2026, 3, 1)) == date(2026, 2, 28)


def test_death_date_range_tilde():
    """Birth-death date range with tilde."""
    text = "Robert James Lee October 12, 1950 ~ March 3, 2026 Robert was a loving father..."
    assert parse_death_date_from_text(text, date(2026, 3, 5)) == date(2026, 3, 3)


def test_death_date_born_died():
    """Explicit 'Born...died' phrasing."""
    text = "Born March 1, 1940 in Columbus, she died on February 15, 2026 at the age of 85."
    assert parse_death_date_from_text(text, date(2026, 2, 17)) == date(2026, 2, 15)


def test_death_date_abbreviated_month():
    """Abbreviated month name."""
    text = "Mary Anne Wilson, 73, passed away on Feb. 28, 2026 at her home."
    assert parse_death_date_from_text(text) == date(2026, 2, 28)


def test_death_date_numeric_range():
    """Numeric MM/DD/YYYY format in date range."""
    text = "Thomas Ray Johnson (3/15/1948 - 2/28/2026) Thomas was a veteran..."
    assert parse_death_date_from_text(text, date(2026, 3, 1)) == date(2026, 2, 28)


def test_death_date_no_match():
    """Text with no parseable death date."""
    text = "A beloved member of the community has passed away."
    assert parse_death_date_from_text(text) is None


def test_death_date_empty_text():
    """Empty or None text."""
    assert parse_death_date_from_text("") is None
    assert parse_death_date_from_text(None) is None


def test_death_date_future_rejected():
    """Future dates should be rejected."""
    text = "John Smith passed away on December 31, 2099."
    assert parse_death_date_from_text(text) is None


def test_death_date_validation_published_bound():
    """Death date near published_date is valid."""
    text = "John Smith, born January 5, 1945, passed away on February 28, 2026."
    assert parse_death_date_from_text(text, date(2026, 3, 1)) == date(2026, 2, 28)


def test_death_date_birth_only_rejected():
    """Only a birth date visible should return None when published_date filters it."""
    text = "John Smith was born on January 5, 1945 in Columbus, Ohio. " + ("x " * 300)
    assert parse_death_date_from_text(text, date(2026, 3, 1)) is None


def test_death_date_departed_this_life():
    """Alternative death phrasing."""
    text = "Sarah Johnson departed this life on March 10, 2026, surrounded by family."
    assert parse_death_date_from_text(text, date(2026, 3, 12)) == date(2026, 3, 10)


def test_death_date_went_to_be_with_lord():
    """Religious phrasing."""
    text = "James Lee went to be with the Lord on March 5, 2026."
    assert parse_death_date_from_text(text, date(2026, 3, 7)) == date(2026, 3, 5)


def test_death_date_cross_validate_fixture():
    """The text in the FULL_OBIT_HTML fixture should yield the same death date as JSON-LD."""
    soup = BeautifulSoup(FULL_OBIT_HTML, "lxml")
    obit_text = parse_obit_text(soup)
    dates = parse_dates(soup)
    text_death = parse_death_date_from_text(obit_text, dates["published"])
    assert text_death == dates["death"]


# --- parse_funeral_home_from_text tests ---

def test_fh_text_arrangements_by():
    text = "John passed away on Feb 28, 2026. Arrangements by Smith Funeral Home of Springfield."
    assert parse_funeral_home_from_text(text) == "Smith Funeral Home"


def test_fh_text_entrusted_to():
    text = "She was 81. Arrangements entrusted to Brown Funeral Chapel, Madison."
    assert parse_funeral_home_from_text(text) == "Brown Funeral Chapel"


def test_fh_text_in_care_of():
    text = "In care of Williams Memorial Funeral Home."
    assert parse_funeral_home_from_text(text) == "Williams Memorial Funeral Home"


def test_fh_text_visitation_at():
    text = "Visitation at Green Funeral Services on Saturday from 2-4 PM."
    assert parse_funeral_home_from_text(text) == "Green Funeral Services"


def test_fh_text_keyword_only():
    """Keyword-only fallback when no context phrase."""
    text = "Jackson Mortuary is in charge of arrangements."
    assert parse_funeral_home_from_text(text) == "Jackson Mortuary"


def test_fh_text_cremation_society():
    text = "Services provided by Northwest Cremation Society."
    assert parse_funeral_home_from_text(text) == "Northwest Cremation Society"


def test_fh_text_multi_word_hyphenated():
    text = "Arrangements under the direction of Jones-Smith Memorial Funeral Home."
    assert parse_funeral_home_from_text(text) == "Jones-Smith Memorial Funeral Home"


def test_fh_text_near_end():
    """Funeral home mentioned past 500 chars (near end of long obituary)."""
    text = ("He was a beloved father and grandfather. " * 20) + "Arrangements by Oak Hill Funeral Home."
    assert parse_funeral_home_from_text(text) == "Oak Hill Funeral Home"


def test_fh_text_memorial_contributions_skipped():
    """Must NOT match funeral home in 'memorial contributions' context."""
    text = "Memorial contributions may be sent to First Baptist Church. Arrangements by Smith Funeral Home."
    assert parse_funeral_home_from_text(text) == "Smith Funeral Home"


def test_fh_text_donations_skipped():
    """Donations context should not trigger false match."""
    text = "Donations may be made to Memorial Hospital Foundation. Services by Valley Mortuary."
    assert parse_funeral_home_from_text(text) == "Valley Mortuary"


def test_fh_text_no_match():
    text = "A beloved member of the community has passed away."
    assert parse_funeral_home_from_text(text) is None


def test_fh_text_empty():
    assert parse_funeral_home_from_text("") is None
    assert parse_funeral_home_from_text(None) is None
