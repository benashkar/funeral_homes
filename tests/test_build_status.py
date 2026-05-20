"""Tests for scheduler.run_daily._build_status — Telegram status string logic.

Covers the silent-zero canary added after the 2026-05-17 Legacy.com Next.js
migration incident: 22 markets returned Found=0 / Blocked=0 / Errors=0 and
the Status:OK alert sat unnoticed for two days.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler.run_daily import _build_status


# --- Silent-zero canary: the high-value 2026-05-17 regression test ---

def test_silent_zero_22_markets_triggers_warning():
    """The exact 2026-05-17 alert shape: 22 markets, Found=0, no blocks, no errors."""
    s = _build_status(markets_count=22, total_found=0, blocked=0, errors=0)
    assert s.startswith("WARNING"), f"silent-zero should raise WARNING, got: {s}"
    assert "silent zero" in s.lower()


def test_silent_zero_threshold_5_markets():
    """Exactly at the threshold (5 markets) — must trigger."""
    s = _build_status(markets_count=5, total_found=0, blocked=0, errors=0)
    assert s.startswith("WARNING")


def test_silent_zero_below_threshold_4_markets_is_ok():
    """Below threshold, a zero day is plausible (small batch) — stay OK."""
    s = _build_status(markets_count=4, total_found=0, blocked=0, errors=0)
    assert s == "OK"


# --- Other status cases shouldn't regress ---

def test_blocked_50pct_takes_priority_over_silent_zero():
    """BLOCKED ≥ 50% wins even when found=0 (the cause is already named)."""
    s = _build_status(markets_count=22, total_found=0, blocked=11, errors=0)
    assert s.startswith("BLOCKED 50%")


def test_blocked_100pct_message_includes_proxy_hint():
    s = _build_status(markets_count=22, total_found=0, blocked=22, errors=0)
    assert "BLOCKED 100%" in s
    assert "PROXY_URL" in s


def test_errors_take_priority_when_below_blocked_threshold():
    s = _build_status(markets_count=22, total_found=0, blocked=3, errors=4)
    assert s == "ERRORS: 4"


def test_degraded_when_some_blocks_but_data_flowed():
    s = _build_status(markets_count=22, total_found=500, blocked=3, errors=0)
    assert s == "DEGRADED: 3 blocked"


def test_ok_when_data_flowed_no_blocks():
    s = _build_status(markets_count=22, total_found=500, blocked=0, errors=0)
    assert s == "OK"


def test_zero_markets_does_not_crash():
    """Edge case: empty market list shouldn't divide by zero."""
    s = _build_status(markets_count=0, total_found=0, blocked=0, errors=0)
    assert s == "OK"  # nothing to do, nothing to warn about


# --- Missing-funeral-home canary ---

def test_missing_fh_canary_fires_at_70pct():
    """500 obits, 350 missing FH (70%) → WARNING."""
    s = _build_status(markets_count=22, total_found=500, blocked=0, errors=0, obits_missing_fh=350)
    assert s.startswith("WARNING")
    assert "missing funeral_home" in s
    assert "70%" in s


def test_missing_fh_canary_fires_at_100pct():
    """All obits captured without FH — strongest signal."""
    s = _build_status(markets_count=22, total_found=200, blocked=0, errors=0, obits_missing_fh=200)
    assert s.startswith("WARNING")
    assert "100%" in s
    assert "missing funeral_home" in s


def test_missing_fh_canary_does_not_fire_below_50_obits():
    """Tiny batches are exempt — only fire when there's a meaningful sample."""
    s = _build_status(markets_count=22, total_found=40, blocked=0, errors=0, obits_missing_fh=40)
    assert s == "OK"  # 40 < threshold, even though 100% missing


def test_missing_fh_canary_does_not_fire_at_natural_rate():
    """A ~5% natural missing-FH rate must stay OK."""
    s = _build_status(markets_count=22, total_found=500, blocked=0, errors=0, obits_missing_fh=25)
    assert s == "OK"


def test_missing_fh_canary_below_70pct_threshold():
    """50% missing is concerning but not yet WARNING-loud."""
    s = _build_status(markets_count=22, total_found=500, blocked=0, errors=0, obits_missing_fh=250)
    assert s == "OK"  # below 70% threshold


def test_silent_zero_takes_priority_over_missing_fh():
    """If found=0 the silent-zero rule fires; missing_fh ratio is meaningless."""
    s = _build_status(markets_count=22, total_found=0, blocked=0, errors=0, obits_missing_fh=0)
    assert "silent zero" in s


def test_blocked_takes_priority_over_missing_fh():
    s = _build_status(markets_count=22, total_found=200, blocked=15, errors=0, obits_missing_fh=200)
    assert s.startswith("BLOCKED")
