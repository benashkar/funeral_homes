"""Tests for scraper.db_writer — uses mocked MySQL connections."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch
from datetime import date

from scraper.db_writer import upsert_obit, batch_insert_obits, log_run, url_exists, get_known_urls_for_site


def _mock_conn(rowcount=1, fetchone_result=None, fetchall_result=None):
    """Create a mock MySQL connection with a cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = rowcount
    cursor.fetchone.return_value = fetchone_result
    cursor.fetchall.return_value = fetchall_result or []
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


def test_batch_insert_obits():
    conn, cursor = _mock_conn(rowcount=1)
    obits = [
        {"legacy_url": "https://example.com/obit/1", "deceased_name": "A"},
        {"legacy_url": "https://example.com/obit/2", "deceased_name": "B"},
        {"legacy_url": "https://example.com/obit/3", "deceased_name": "C"},
    ]
    new_count = batch_insert_obits(conn, obits, "tx-brown")
    assert new_count == 3
    assert cursor.execute.call_count == 3
    # Single commit for the whole batch
    conn.commit.assert_called_once()


def test_batch_insert_empty():
    conn, cursor = _mock_conn()
    assert batch_insert_obits(conn, [], "tx-brown") == 0
    cursor.execute.assert_not_called()


def test_url_exists_true():
    conn, cursor = _mock_conn(fetchone_result=(1,))
    assert url_exists(conn, "https://example.com/obit/1") is True


def test_url_exists_false():
    conn, cursor = _mock_conn(fetchone_result=None)
    assert url_exists(conn, "https://example.com/obit/999") is False


def test_get_known_urls_for_site():
    conn, cursor = _mock_conn(fetchall_result=[
        ("https://example.com/obit/1",),
        ("https://example.com/obit/2",),
    ])
    urls = get_known_urls_for_site(conn, "oh-franklin")
    assert urls == {"https://example.com/obit/1", "https://example.com/obit/2"}
    # Verify it queries by site_id
    cursor.execute.assert_called_once()
    args = cursor.execute.call_args[0]
    assert "site_id" in args[0]


def test_log_run():
    conn, cursor = _mock_conn()
    log_run(conn, "oh-franklin", found=5, new=3, errors=None)
    cursor.execute.assert_called_once()
    conn.commit.assert_called_once()
