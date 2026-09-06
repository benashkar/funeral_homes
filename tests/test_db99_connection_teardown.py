"""Regression tests for the db99 connection leak (2026-09-02).

db99 is a shared MySQL instance: max_connections=1289, wait_timeout=28800.
An unclosed connection holds its slot for eight hours, so a leak in this
dashboard starves every other project on the box. On 2026-09-02 the
funeral_homes database held 96 connections, 95 of them Sleep.

Two bugs are covered here:
  1. dashboard/app.py `_get_conn()` opened a raw connection per call with no
     `finally` anywhere in the module, so every route leaked whenever a query
     raised -- and each route's own `except` swallowed the error, hiding it.
  2. dashboard/db.py `close_db()` was defined but never registered with Flask,
     so `get_db()` leaked one connection per request.

Each test fails against the pre-fix code and passes after it.
"""

import sys
import types

import pytest
from flask import Flask, g


class FakeConn:
    """Stands in for a mysql.connector connection."""

    def __init__(self, registry):
        self.closed = False
        registry.append(self)

    def close(self):
        self.closed = True

    def cursor(self, *a, **kw):  # pragma: no cover - not exercised here
        raise AssertionError("cursor() not expected in these tests")


def test_get_conn_registers_on_g_and_teardown_closes(monkeypatch):
    """A connection opened inside a request is closed even if the view raises.

    This is the bug: the view catches its own exception and returns a 503, so
    nothing ever reached the `conn.close()` further down the try block.
    """
    from dashboard import app as app_mod

    opened = []
    monkeypatch.setattr(app_mod, "_connect_with_retry", lambda: FakeConn(opened))

    app = Flask(__name__)

    @app.teardown_appcontext
    def _close(exc=None):
        for conn in g.pop(app_mod._G_CONNS, None) or []:
            try:
                conn.close()
            except Exception:
                pass

    @app.route("/boom")
    def boom():
        try:
            app_mod._get_conn()          # opened, never closed by the view
            raise RuntimeError("query blew up")
        except Exception as e:           # the swallow that caused the leak
            return f"error: {e}", 503

    client = app.test_client()
    assert client.get("/boom").status_code == 503

    assert len(opened) == 1, "expected exactly one connection to be opened"
    assert opened[0].closed, "connection leaked: teardown did not close it"


def test_real_app_registers_a_teardown_that_closes(monkeypatch):
    """create_app() must wire the teardown, not just define it."""
    from dashboard import app as app_mod

    opened = []
    monkeypatch.setattr(app_mod, "_connect_with_retry", lambda: FakeConn(opened))
    # ensure_schema runs at startup against a FakeConn; make it a no-op.
    fake_writer = types.ModuleType("scraper.db_writer")
    fake_writer.ensure_schema = lambda conn: None
    monkeypatch.setitem(sys.modules, "scraper.db_writer", fake_writer)

    app = app_mod.create_app()

    # The startup migration connection must be closed by its own finally.
    assert opened and opened[0].closed, "startup migration leaked a connection"

    with app.test_request_context("/"):
        conn = app_mod._get_conn()
        assert not conn.closed
    # Leaving the context pops it and runs teardown_appcontext.
    assert conn.closed, "create_app did not register a working teardown"


def test_close_db_is_registered_by_init_app():
    """dashboard.db.close_db was orphaned: defined, never registered.

    Skipped when pymysql is absent, which is its NORMAL state here and not an
    oversight: `dashboard/db.py` imports pymysql, pymysql is in neither
    requirements.txt nor requirements-web.txt, and `dashboard/app.py` guards its
    own import of this module for exactly that reason -- the blueprint is not
    mounted, so a missing driver must never be fatal at boot.

    Adding pymysql to requirements to make this test pass would install a driver
    for unreachable code and change the deployed image, which is the wrong
    trade. Mirroring the application's own guard is the honest one. If the
    blueprint is ever mounted, add pymysql to requirements-web.txt and this skip
    stops firing on its own.

    Broke CI on 2026-09-02 (`ModuleNotFoundError: No module named 'pymysql'`,
    1 failed / 163 passed) -- green on master before that, red every run since.
    """
    pytest.importorskip("pymysql", reason="dashboard/db.py's driver is deliberately not installed")

    from dashboard import db as db_mod

    app = Flask(__name__)
    before = len(app.teardown_appcontext_funcs)
    db_mod.init_app(app)
    assert len(app.teardown_appcontext_funcs) == before + 1
    assert db_mod.close_db in app.teardown_appcontext_funcs

    closed = {"n": 0}

    class FakePyMySQL:
        def close(self):
            closed["n"] += 1

    with app.app_context():
        g.db = FakePyMySQL()
    assert closed["n"] == 1, "close_db did not run at teardown"


def test_retry_only_fires_on_errno_1040(monkeypatch):
    """Retry ER_CON_COUNT_ERROR only. Retrying everything hides real faults."""
    import mysql.connector

    from dashboard import app as app_mod

    monkeypatch.setattr(app_mod.time, "sleep", lambda s: None)

    # A non-1040 error must propagate on the FIRST attempt, no retries.
    calls = {"n": 0}

    def bad_credentials():
        calls["n"] += 1
        raise mysql.connector.Error(msg="Access denied", errno=1045)

    monkeypatch.setattr(app_mod, "_connect_raw", bad_credentials)
    with pytest.raises(mysql.connector.Error):
        app_mod._connect_with_retry()
    assert calls["n"] == 1, "a non-1040 error must not be retried"

    # 1040 retries, then succeeds.
    state = {"n": 0}
    opened = []

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise mysql.connector.Error(
                msg="Too many connections", errno=app_mod.ER_CON_COUNT_ERROR
            )
        return FakeConn(opened)

    monkeypatch.setattr(app_mod, "_connect_raw", flaky)
    conn = app_mod._connect_with_retry()
    assert state["n"] == 3
    assert conn is opened[0]

    # 1040 that never clears must eventually raise, not spin forever.
    def always_1040():
        raise mysql.connector.Error(
            msg="Too many connections", errno=app_mod.ER_CON_COUNT_ERROR
        )

    monkeypatch.setattr(app_mod, "_connect_raw", always_1040)
    with pytest.raises(mysql.connector.Error):
        app_mod._connect_with_retry(attempts=2, base_delay=0)
