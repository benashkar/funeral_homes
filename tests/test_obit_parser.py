"""Tests for scraper.obit_parser — happy path and missing fields."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from bs4 import BeautifulSoup

from scraper.obit_parser import parse_name, parse_dates, parse_funeral_home, parse_obit_text


# --- Fixtures: realistic Legacy.com obit page HTML fragments ---

FULL_OBIT_HTML = """
<html>
<head>
    <meta property="article:published_time" content="2026-03-01T12:00:00Z">
</head>
<body>
    <h1 class="obit-name">John Michael Smith</h1>
    <p class="obit-dates">January 5, 1945 - February 28, 2026</p>
    <a data-component="FuneralHomeName">Greenfield Memorial Chapel</a>
    <div class="obit-text">
        <p>John Michael Smith, 81, of Springfield, passed away peacefully on February 28, 2026.</p>
        <p>He is survived by his wife, Mary, and three children.</p>
    </div>
</body>
</html>
"""

MINIMAL_OBIT_HTML = """
<html>
<head></head>
<body>
    <div class="obit-text">
        <p>A beloved member of the community has passed away.</p>
    </div>
</body>
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


def test_parse_dates_no_meta():
    html = """
    <html><head></head><body>
        <p class="obit-dates">March 10, 2026</p>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    dates = parse_dates(soup)
    assert dates["death"] == date(2026, 3, 10)
    assert dates["published"] is None


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
