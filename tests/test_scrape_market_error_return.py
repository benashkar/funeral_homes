"""Tests that scrape_market surfaces listing-fetch failures as errors.

Regression guard: prior to 2026-04-15, blocked markets (403/429/503 from
Legacy.com) returned error=None, so the per-scraper Telegram counter
under-reported them as "0 errors" — misleading "OK" when all IPs were banned.
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_conn():
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()
    return conn


@patch("scheduler.run_daily.enrich_funeral_home")
@patch("scheduler.run_daily.upsert_funeral_home")
@patch("scheduler.run_daily.log_run")
@patch("scheduler.run_daily.batch_insert_obits", return_value=0)
@patch("scheduler.run_daily.get_known_urls_for_site", return_value=set())
@patch("scheduler.run_daily.get_connection")
@patch("scheduler.run_daily.LegacyScraper")
def test_blocked_market_returns_diag_string(scraper_cls, get_conn, *_mocks):
    from scheduler.run_daily import scrape_market

    get_conn.return_value = _fake_conn()
    instance = scraper_cls.return_value

    # scrape_market sets _last_listing_diag=None after construction; the real
    # scrape_today sets it to "listing_fetch_failed" when the listing fetch is
    # blocked. Simulate that via side_effect.
    def _simulate_blocked(**_kw):
        instance._last_listing_diag = "listing_fetch_failed"
        return []

    instance.scrape_today.side_effect = _simulate_blocked

    market = {"site_id": "tx-harris", "priority": False}
    site_id, found, new, error, missing_fh = scrape_market(market, session=MagicMock())

    assert site_id == "tx-harris"
    assert found == 0
    assert new == 0
    assert error is not None, "blocked market must surface an error, not None"
    assert error.startswith("listing_fetch_failed")
    assert missing_fh == 0  # nothing fetched, so nothing missing


@patch("scheduler.run_daily.enrich_funeral_home")
@patch("scheduler.run_daily.upsert_funeral_home")
@patch("scheduler.run_daily.log_run")
@patch("scheduler.run_daily.batch_insert_obits", return_value=5)
@patch("scheduler.run_daily.get_known_urls_for_site", return_value=set())
@patch("scheduler.run_daily.get_connection")
@patch("scheduler.run_daily.LegacyScraper")
def test_successful_market_returns_no_error(scraper_cls, get_conn, *_mocks):
    from scheduler.run_daily import scrape_market

    get_conn.return_value = _fake_conn()
    instance = scraper_cls.return_value

    def _simulate_success(**_kw):
        instance._last_listing_diag = None
        return [{"legacy_url": "https://legacy.com/us/obituaries/name/x", "deceased_name": "X"}]

    instance.scrape_today.side_effect = _simulate_success

    market = {"site_id": "ca-los-angeles", "priority": False}
    site_id, found, new, error, missing_fh = scrape_market(market, session=MagicMock())

    assert found == 1
    assert error is None
    # The mocked obit dict has no funeral_home key, so it counts as missing.
    # This isn't a fixture bug — it documents the fact that scrape_market
    # exposes the missing-FH count for the run-level canary in run().
    assert missing_fh == 1
