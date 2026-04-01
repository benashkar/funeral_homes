"""Tests for dashboard.app — pagination helper."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.app import _pagination_pages


def test_small_page_count():
    """With few pages, all should be listed."""
    assert _pagination_pages(1, 5) == [1, 2, 3, 4, 5]


def test_single_page():
    assert _pagination_pages(1, 1) == [1]


def test_large_page_count_start():
    pages = _pagination_pages(1, 20000)
    assert 1 in pages
    assert 2 in pages
    assert 3 in pages
    assert 20000 in pages
    assert None in pages
    assert len(pages) < 15


def test_large_page_count_middle():
    pages = _pagination_pages(500, 20000)
    assert 1 in pages
    assert 500 in pages
    assert 498 in pages
    assert 502 in pages
    assert 20000 in pages
    assert None in pages
    assert len(pages) < 20


def test_large_page_count_end():
    pages = _pagination_pages(20000, 20000)
    assert 1 in pages
    assert 20000 in pages
    assert 19999 in pages
    assert None in pages


def test_ellipsis_placement():
    """None markers should appear between non-consecutive pages."""
    pages = _pagination_pages(50, 1000)
    for i in range(len(pages) - 1):
        if pages[i] is None:
            # After ellipsis should be a number
            assert pages[i + 1] is not None
        elif pages[i + 1] is not None and pages[i] is not None:
            # Consecutive numbers should be sequential or have ellipsis between
            assert pages[i + 1] == pages[i] + 1 or pages[i + 1] is None


def test_current_page_always_included():
    for current in [1, 50, 500, 9999, 10000]:
        pages = _pagination_pages(current, 10000)
        assert current in pages
