"""Tests for /api/scrape-health and /scrape-health (mocked DB)."""

import sys
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_app_with_mock_conn(rows_by_query):
    """Create a Flask test app with `_get_conn` and `ensure_schema` patched.

    rows_by_query: a callable that takes the SQL string and returns the
    list of rows that cur.fetchall() should yield for that query.
    """
    # Patch ensure_schema to a no-op so create_app() doesn't try to connect
    # during startup.
    with patch("scraper.db_writer.ensure_schema", lambda conn: None):
        with patch("dashboard.app._get_conn") as mock_get_conn:
            cur = MagicMock()

            def execute(sql, params=None):
                cur._last_rows = rows_by_query(sql)

            def fetchall():
                return getattr(cur, "_last_rows", []) or []

            cur.execute.side_effect = execute
            cur.fetchall.side_effect = fetchall
            cur.fetchone.side_effect = lambda: (getattr(cur, "_last_rows", []) or [None])[0]

            conn = MagicMock()
            conn.cursor.return_value = cur
            mock_get_conn.return_value = conn

            from dashboard.app import create_app
            app = create_app()
            app.config["TESTING"] = True
            yield app, cur


def _no_rows(sql):
    return []


def test_api_scrape_health_shape_empty():
    """Endpoint returns the documented shape even when DB has no data."""
    for app, _cur in _build_app_with_mock_conn(_no_rows):
        client = app.test_client()
        resp = client.get("/api/scrape-health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "as_of" in data
        assert isinstance(data["days"], list) and len(data["days"]) == 7
        assert data["states"] == []
        assert data["quiet_markets"] == []
        # Days are newest-first.
        assert data["days"][0] > data["days"][-1]


def test_api_scrape_health_groups_by_state_prefix():
    """site_ids like 'tx-brown', 'tx-runnels', 'oh-pickaway' group by 2-letter prefix."""
    today = date.today()
    rows = [
        {"site_id": "tx-brown", "run_date": today, "found": 5},
        {"site_id": "tx-runnels", "run_date": today, "found": 3},
        {"site_id": "oh-pickaway", "run_date": today, "found": 7},
        {"site_id": "tx-brown", "run_date": today - timedelta(days=1), "found": 4},
    ]

    def rows_for(sql):
        s = sql.strip()
        if s.startswith("SELECT site_id, run_date"):
            return rows
        if "MAX(run_date) AS last_active" in s:
            return [
                {"site_id": "tx-brown", "last_active": today},
                {"site_id": "tx-runnels", "last_active": today},
                {"site_id": "oh-pickaway", "last_active": today},
            ]
        return []

    for app, _cur in _build_app_with_mock_conn(rows_for):
        with patch("scraper.db_writer.find_quiet_markets", lambda conn, site_ids, today=None: []):
            client = app.test_client()
            resp = client.get("/api/scrape-health")
            assert resp.status_code == 200
            data = resp.get_json()
            states = {s["state"]: s["daily_counts"] for s in data["states"]}
            assert set(states.keys()) == {"TX", "OH"}
            # Day 0 = today (newest first). TX today = 5 + 3 = 8.
            assert states["TX"][0] == 8
            assert states["OH"][0] == 7
            # Day 1 = yesterday. TX = 4, OH = 0.
            assert states["TX"][1] == 4
            assert states["OH"][1] == 0


def test_api_scrape_health_quiet_markets_populated():
    """find_quiet_markets results are surfaced into the JSON payload."""
    today = date.today()

    def rows_for(sql):
        if "MAX(run_date) AS last_active" in sql:
            return [{"site_id": "wi-brown", "last_active": today - timedelta(days=2)}]
        return []

    for app, _cur in _build_app_with_mock_conn(rows_for):
        with patch(
            "scraper.db_writer.find_quiet_markets",
            lambda conn, site_ids, today=None: [("wi-brown", 12.5, 5)],
        ):
            client = app.test_client()
            resp = client.get("/api/scrape-health")
            data = resp.get_json()
            assert len(data["quiet_markets"]) == 1
            q = data["quiet_markets"][0]
            assert q["site_id"] == "wi-brown"
            assert q["last_7d_avg"] == 12.5
            assert q["active_days"] == 5
            assert q["last_active"] == (today - timedelta(days=2)).isoformat()


def test_scrape_health_view_renders_empty():
    """HTML view renders without crashing on empty data."""
    for app, _cur in _build_app_with_mock_conn(_no_rows):
        with patch("scraper.db_writer.find_quiet_markets", lambda conn, site_ids, today=None: []):
            client = app.test_client()
            resp = client.get("/scrape-health")
            assert resp.status_code == 200
            body = resp.get_data(as_text=True)
            assert "Scrape Health" in body
            assert "Quiet markets" in body
            assert "No scrape data in last 7 days" in body


def test_scrape_health_view_renders_with_data():
    """HTML view renders heatmap cells colored by per-state median."""
    today = date.today()
    rows = [
        {"site_id": "tx-brown", "run_date": today, "found": 10},
        {"site_id": "tx-brown", "run_date": today - timedelta(days=1), "found": 0},
    ]

    def rows_for(sql):
        if sql.strip().startswith("SELECT site_id, run_date"):
            return rows
        return []

    for app, _cur in _build_app_with_mock_conn(rows_for):
        with patch("scraper.db_writer.find_quiet_markets", lambda conn, site_ids, today=None: []):
            client = app.test_client()
            resp = client.get("/scrape-health")
            assert resp.status_code == 200
            body = resp.get_data(as_text=True)
            assert ">TX<" in body
            # 0 cell should be classed red.
            assert "cell-red" in body
            assert "cell-green" in body
