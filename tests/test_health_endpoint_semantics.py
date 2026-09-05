"""`/health` must not answer "ok" when the database is unreachable.

The pre-2026-09-05 exception handler was:

    except Exception as e:
        return {"status": "ok", "db": f"error: {e}"}

-- literally the word **"ok"** while reporting that the database was down, with
an implicit HTTP 200. Both machine-readable signals said healthy. This is the
one endpoint whose entire job is to be believed, which makes it the worst place
in the codebase for that.

This was the third dashboard found with a version of this defect (crime's
/healthz returned {"status":"ok"} off a dead socket; church_scrapes returned
HTTP 200 alongside status "error"). It is a pattern, not an oversight.

The contract:

    ok        200   the database answered
    faulted   503   it did not

No `degraded` case here -- this probe tests exactly one thing. `degraded`
(which is 200) is for "wrong in a way a restart cannot fix", and this endpoint
has no such state to report.

Returning 503 is only safe because `/livez` exists and is DB-free. Render
restarts a container whose health check fails, so a database probe must never
be the restart trigger: db99 is shared, and another project exhausting the pool
would restart-loop this service for a fault a restart cannot fix. Verified
2026-09-05: cr-obituaries-dashboard has healthCheckPath="" (unset), so nothing
currently restarts on either endpoint.
"""
import pytest
from flask import Flask


def _make_app(monkeypatch, conn_factory):
    from dashboard import app as app_mod

    monkeypatch.setattr(app_mod, "_get_conn", conn_factory)
    return app_mod


class _OkConn:
    def __init__(self):
        self.closed = False

    def cursor(self, *a, **kw):
        class _Cur:
            def execute(self, *a, **kw):
                return None

            def fetchone(self):
                return (1,)

            def close(self):
                return None

        return _Cur()

    def close(self):
        self.closed = True


def test_health_is_faulted_and_503_when_the_db_is_unreachable(monkeypatch):
    """Against the pre-fix code this FAILS twice: status was "ok", code was 200."""
    def _boom():
        raise RuntimeError("simulated db99 outage")

    app_mod = _make_app(monkeypatch, _boom)
    app = app_mod.create_app()
    resp = app.test_client().get("/health")

    assert resp.status_code == 503, (
        "/health returned HTTP %s while the database was unreachable; every "
        "monitor that checks the status code reads that as healthy"
        % resp.status_code
    )
    assert resp.get_json()["status"] == "faulted", (
        'status was %r -- the body must not say the service is fine when it '
        "cannot reach its database" % resp.get_json()["status"]
    )


def test_health_is_ok_and_200_when_the_db_answers(monkeypatch):
    """Do not over-correct: a working database must still read as healthy."""
    app_mod = _make_app(monkeypatch, _OkConn)
    app = app_mod.create_app()
    resp = app.test_client().get("/health")

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_health_closes_its_connection_on_the_error_path(monkeypatch):
    """A probe polled forever is the last place an unguarded close belongs."""
    opened = []

    class _RaisingCursorConn(_OkConn):
        def cursor(self, *a, **kw):
            raise RuntimeError("query blew up after connecting")

    def _factory():
        c = _RaisingCursorConn()
        opened.append(c)
        return c

    app_mod = _make_app(monkeypatch, _factory)
    app = app_mod.create_app()
    app.test_client().get("/health")

    assert opened, "no connection was opened"
    assert opened[0].closed, (
        "the connection was not closed when the query raised; on a URL polled "
        "around the clock that parks a db99 slot every single poll"
    )


def test_livez_exists_and_never_touches_the_database(monkeypatch):
    """Liveness must answer without a DB, or it cannot survive a db99 outage."""
    def _boom():
        raise AssertionError("/livez must not open a database connection")

    app_mod = _make_app(monkeypatch, _boom)
    app = app_mod.create_app()
    resp = app.test_client().get("/livez")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "alive"
    assert "db" not in body, "/livez reported on the database; it must not ask"
