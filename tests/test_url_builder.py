"""Tests for scraper.url_builder."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.url_builder import build_listing_url


def test_county_url():
    market = {"site_id": "tx-brown", "state": "texas", "legacy_slug": "brown-county", "type": "county"}
    url = build_listing_url(market)
    assert url == "https://www.legacy.com/us/obituaries/local/texas/brown-county"


def test_city_url():
    market = {"site_id": "oh-hilliard", "state": "ohio", "legacy_slug": "hilliard", "type": "city"}
    url = build_listing_url(market)
    assert url == "https://www.legacy.com/us/obituaries/local/ohio/hilliard"


def test_multi_word_state():
    market = {"site_id": "ma-grafton", "state": "massachusetts", "legacy_slug": "grafton", "type": "city"}
    url = build_listing_url(market)
    assert url == "https://www.legacy.com/us/obituaries/local/massachusetts/grafton"


def test_hyphenated_slug():
    market = {"site_id": "oh-canal-winch", "state": "ohio", "legacy_slug": "canal-winchester", "type": "city"}
    url = build_listing_url(market)
    assert url == "https://www.legacy.com/us/obituaries/local/ohio/canal-winchester"
