"""CR Obituaries Dashboard — browse scraped obituary data."""

from flask import Flask, render_template, request
import mysql.connector

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

PER_PAGE = 50


def _get_conn():
    creds = get_db_credentials()
    return mysql.connector.connect(
        host=creds["DB_HOST"],
        port=int(creds["DB_PORT"]),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        database=creds["DB_NAME"],
    )


def create_app():
    app = Flask(__name__)

    @app.route("/health")
    def health():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            conn.close()
            return {"status": "ok", "db": "connected"}
        except Exception as e:
            return {"status": "ok", "db": f"error: {e}"}

    @app.route("/")
    def index():
        page = request.args.get("page", 1, type=int)
        search = request.args.get("search", "").strip()
        site_id = request.args.get("site_id", "").strip()

        conn = _get_conn()
        cur = conn.cursor(dictionary=True)

        # Build query
        where_clauses = []
        params = []
        if search:
            where_clauses.append("(deceased_name LIKE %s OR funeral_home LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        if site_id:
            where_clauses.append("site_id = %s")
            params.append(site_id)

        where = ""
        if where_clauses:
            where = "WHERE " + " AND ".join(where_clauses)

        # Count
        cur.execute(f"SELECT COUNT(*) as cnt FROM obituaries {where}", params)
        total = cur.fetchone()["cnt"]
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

        # Fetch page
        offset = (page - 1) * PER_PAGE
        cur.execute(
            f"SELECT * FROM obituaries {where} ORDER BY published_date DESC, id DESC LIMIT %s OFFSET %s",
            params + [PER_PAGE, offset],
        )
        obits = cur.fetchall()

        # Get distinct site_ids for filter
        cur.execute("SELECT DISTINCT site_id FROM obituaries ORDER BY site_id")
        sites = [r["site_id"] for r in cur.fetchall()]

        cur.close()
        conn.close()

        return render_template(
            "index.html",
            obits=obits,
            page=page,
            total_pages=total_pages,
            total=total,
            sites=sites,
            search=search,
            site_id=site_id,
        )

    @app.route("/stats")
    def stats():
        conn = _get_conn()
        cur = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT site_id, COUNT(*) as total_obits,
                   MAX(published_date) as latest_obit,
                   MIN(published_date) as earliest_obit
            FROM obituaries
            GROUP BY site_id
            ORDER BY total_obits DESC
        """)
        site_stats = cur.fetchall()

        cur.execute("""
            SELECT site_id, run_date, obits_found, obits_new, errors
            FROM scrape_log
            ORDER BY run_date DESC, id DESC
            LIMIT 100
        """)
        recent_runs = cur.fetchall()

        cur.close()
        conn.close()

        return render_template(
            "stats.html",
            site_stats=site_stats,
            recent_runs=recent_runs,
        )

    return app
