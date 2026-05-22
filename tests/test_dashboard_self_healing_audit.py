"""Tests for /self-healing-audit (mocked GitHub + Render API calls)."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.self_healing_audit import (  # noqa: E402
    fetch_auto_merges,
    fetch_pause_status,
    fetch_suspended_scrapers,
    parse_git_log_output,
)


def _build_app():
    """Boot create_app() without touching the real DB or running migrations."""
    with patch("scraper.db_writer.ensure_schema", lambda conn: None):
        with patch("dashboard.app._get_conn") as mock_get_conn:
            cur = MagicMock()
            cur.fetchall.return_value = []
            cur.fetchone.return_value = None
            conn = MagicMock()
            conn.cursor.return_value = cur
            mock_get_conn.return_value = conn
            from dashboard.app import create_app
            app = create_app()
            app.config["TESTING"] = True
            return app


def test_endpoint_200_when_everything_empty():
    """Page must render even when GitHub + Render APIs return nothing."""
    app = _build_app()
    with patch("dashboard.self_healing_audit.fetch_auto_merges", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_suspended_scrapers", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_pause_status",
               return_value={"paused": False, "known": True}):
        client = app.test_client()
        resp = client.get("/self-healing-audit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Self-Healing Audit" in body
        assert "No [AUTO-MERGE] commits" in body or "No <code>[AUTO-MERGE]</code>" in body
        assert "No scrapers currently suspended" in body
        assert "ACTIVE" in body
        assert "trig_01EzPjGTvjG6cK9BW5NDY3bz" in body


def test_parse_git_log_output_filters_to_auto_merge_only():
    """The legacy git-log parser must keep `[AUTO-MERGE]` rows and drop others."""
    raw = "\n".join([
        "abc1234deadbeef|2026-05-21 22:00:00 +0000|[AUTO-MERGE] Recipe B slug fix|self-healer-bot",
        "def5678cafebabe|2026-05-20 14:00:00 +0000|fix: typo in README|ben",
        "9999000aaaa1111|2026-05-19 22:00:00 +0000|[AUTO-MERGE] Recipe D orphan removal|self-healer-bot",
    ])
    rows = parse_git_log_output(raw)
    assert len(rows) == 2
    assert rows[0]["short_sha"] == "abc1234"
    assert rows[0]["subject"].startswith("[AUTO-MERGE]")
    assert rows[0]["url"].endswith("/abc1234deadbeef")
    assert rows[0]["author"] == "self-healer-bot"
    assert rows[1]["short_sha"] == "9999000"


def test_fetch_auto_merges_via_mocked_github():
    """Mock the GitHub commits API and ensure we extract [AUTO-MERGE] entries."""
    listing = [
        {"sha": "aaa" * 13 + "a",
         "commit": {"message": "[AUTO-MERGE] Recipe B: fix slug rot for tx-brown\n\nBody",
                    "author": {"name": "self-healer-bot", "date": "2026-05-21T22:00:00Z"}}},
        {"sha": "bbb" * 13 + "b",
         "commit": {"message": "chore: bump deps",
                    "author": {"name": "ben", "date": "2026-05-20T10:00:00Z"}}},
    ]
    files_payload = {"files": [{"filename": "config/markets.json"}]}

    def fake_http(url, headers=None, timeout=10):
        if "/commits?" in url:
            return 200, listing
        if "/commits/" in url:
            return 200, files_payload
        return 404, None

    out = fetch_auto_merges(http_get=fake_http)
    assert len(out) == 1
    assert out[0]["subject"].startswith("[AUTO-MERGE]")
    assert out[0]["author"] == "self-healer-bot"
    assert out[0]["files"] == ["config/markets.json"]
    assert out[0]["url"].endswith(out[0]["sha"])


def test_fetch_suspended_scrapers_filters_by_name_and_state():
    """Only services matching the scraper name prefixes AND suspended count."""
    payload = [
        {"service": {"id": "srv-1", "name": "funeral-homes-scraper-tx",
                     "suspended": "suspended"}},
        {"service": {"id": "srv-2", "name": "funeral-homes-scraper-ga",
                     "suspended": "not_suspended"}},
        {"service": {"id": "srv-3", "name": "cr-rescue-scraper-pickaway",
                     "suspended": "suspended", "suspendedAt": "2026-05-22T03:00:00Z"}},
        {"service": {"id": "srv-4", "name": "cr-obituaries-dashboard",
                     "suspended": "suspended"}},
    ]

    def fake_http(url, headers=None, timeout=10):
        assert "api.render.com" in url
        return 200, payload

    with patch.dict(os.environ, {"RENDER_API_KEY": "test-key"}):
        out = fetch_suspended_scrapers(http_get=fake_http)
    names = {s["name"] for s in out}
    assert names == {"funeral-homes-scraper-tx", "cr-rescue-scraper-pickaway"}
    cr = [s for s in out if s["name"] == "cr-rescue-scraper-pickaway"][0]
    assert cr["resume_url"].endswith("srv-3")


def test_fetch_suspended_scrapers_returns_empty_without_key():
    """No RENDER_API_KEY → return [] (page must still render)."""
    env = {k: v for k, v in os.environ.items() if k != "RENDER_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        out = fetch_suspended_scrapers(http_get=lambda *a, **k: (200, []))
    assert out == []


def test_pause_status_paused_when_github_returns_200():
    """200 from contents API = pause file present = paused."""
    assert fetch_pause_status(http_get=lambda *a, **k: (200, {"name": ".self_healing_paused"})) \
        == {"paused": True, "known": True}


def test_pause_status_active_when_github_returns_404():
    """404 = file absent = self-healing armed."""
    assert fetch_pause_status(http_get=lambda *a, **k: (404, None)) \
        == {"paused": False, "known": True}


def test_pause_status_unknown_on_network_error():
    """Network failure (None status) = unknown; UI shows yellow banner."""
    res = fetch_pause_status(http_get=lambda *a, **k: (None, None))
    assert res["paused"] is False
    assert res["known"] is False


def test_endpoint_renders_paused_banner():
    """When pause API says paused, the red PAUSED banner must appear."""
    app = _build_app()
    with patch("dashboard.self_healing_audit.fetch_auto_merges", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_suspended_scrapers", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_pause_status",
               return_value={"paused": True, "known": True}):
        client = app.test_client()
        body = client.get("/self-healing-audit").get_data(as_text=True)
        assert "PAUSED" in body
        assert "sh-banner-paused" in body
        assert "ACTIVE" not in body or body.count("PAUSED") >= 1


def test_endpoint_renders_active_banner_on_404():
    """404 from GitHub contents API → green ACTIVE banner."""
    app = _build_app()
    with patch("dashboard.self_healing_audit.fetch_auto_merges", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_suspended_scrapers", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_pause_status",
               return_value={"paused": False, "known": True}):
        client = app.test_client()
        body = client.get("/self-healing-audit").get_data(as_text=True)
        assert "ACTIVE" in body
        assert "sh-banner-active" in body


def test_endpoint_renders_unknown_banner_on_network_error():
    """Unknown pause state → yellow UNKNOWN banner, not a 500."""
    app = _build_app()
    with patch("dashboard.self_healing_audit.fetch_auto_merges", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_suspended_scrapers", return_value=[]), \
         patch("dashboard.self_healing_audit.fetch_pause_status",
               return_value={"paused": False, "known": False}):
        client = app.test_client()
        resp = client.get("/self-healing-audit")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "UNKNOWN" in body
        assert "sh-banner-unknown" in body
