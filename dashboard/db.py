"""Per-request MySQL connection for Flask dashboard.

db99 is a SHARED instance with wait_timeout=28800 (eight hours). `close_db`
below was defined but NEVER registered with Flask, so every request that
called `get_db()` stranded a connection until db99 timed it out. Registration
now happens in `init_app`, which any app using this module MUST call.
"""

import pymysql
import pymysql.cursors
from flask import current_app, g


def get_db():
    """Get or create a MySQL connection for the current request."""
    if "db" not in g:
        cfg = current_app.config
        g.db = pymysql.connect(
            host=cfg["DB_HOST"],
            port=cfg["DB_PORT"],
            user=cfg["DB_USER"],
            password=cfg["DB_PASSWORD"],
            database=cfg["DB_NAME"],
            charset="utf8mb4",
            connect_timeout=10,
        )
    return g.db


def close_db(exc=None):
    """Close the DB connection at end of request."""
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def init_app(app):
    """Register the teardown handler on a Flask app.

    Without this, `close_db` never runs and `get_db()` leaks one db99
    connection per request. Call this from every factory that uses `get_db`.
    """
    app.teardown_appcontext(close_db)
    return app
