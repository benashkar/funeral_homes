"""Tests for scraper.db_writer — uses mocked MySQL connections."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch
from datetime import date

from scraper.db_writer import upsert_obit, log_run, url_exists


def _mock_conn(rowcount=1, fetchone_result=None):
    """Create a mock MySQL connection with a cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = rowcount
    cursor.fetchone.return_value = fetchone_result
    conn.cursor.return_value = cursor
    return conn, cursor


def test_upsert_obit_new_row():
    conn, cursor = _mock_conn(rowcount=1)
    obit = {
        "legacy_url": "https://www.legacy.com/us/obituaries/name/john-smith",
        "deceased_name": "John Smith",
        "published_date": date(2026, 3, 1),
        "death_date": date(2026, 2, 28),
        "funeral_home": "Chapel Hill",
        "obit_text": "John Smith passed away peacefully.",
    }
    result = upsert_obit(conn, obit, "tx-brown")
    assert result is True
    cursor.execute.assert_called_once()
    conn.commit.assert_called_once()


def test_upsert_obit_duplicate():
    conn, cursor = _mock_conn(rowcount=0)
    obit = {
        "legacy_url": "https://www.legacy.com/us/obituaries/name/john-smith",
        "deceased_name": "John Smith",
    }
    result = upsert_obit(conn, obit, "tx-brown")
    assert result is False


def test_url_exists_true():
    conn, cursor = _mock_conn(fetchone_result=(1,))
    assert url_exists(conn, "https://example.com/obit/1") is True


def test_url_exists_false():
    conn, cursor = _mock_conn(fetchone_result=None)
    assert url_exists(conn, "https://example.com/obit/999") is False


def test_log_run():
    conn, cursor = _mock_conn()
    log_run(conn, "oh-franklin", found=5, new=3, errors=None)
    cursor.execute.assert_called_once()
    conn.commit.assert_called_once()
