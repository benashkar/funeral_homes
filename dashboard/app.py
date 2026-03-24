"""CR Obituaries Dashboard — browse scraped obituary data."""

from flask import Flask, render_template, request
import mysql.connector

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

PER_PAGE = 50

# Map state abbreviations to full names (for display)
STATE_MAP = {
    "tx": "Texas", "oh": "Ohio", "ma": "Massachusetts", "ga": "Georgia",
    "mn": "Minnesota", "wi": "Wisconsin", "il": "Illinois",
}


def _get_conn():
    creds = get_db_credentials()
    return mysql.connector.connect(
        host=creds["DB_HOST"],
        port=int(creds["DB_PORT"]),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        database=creds["DB_NAME"],
        connect_timeout=10,
    )


def _state_from_site_id(site_id):
    """Extract state abbreviation from site_id (e.g. 'mn-hennepin' -> 'mn')."""
    return site_id.split("-")[0] if site_id else ""


def _county_from_site_id(site_id):
    """Extract county name from site_id (e.g. 'mn-hennepin' -> 'Hennepin')."""
    parts = site_id.split("-")[1:]
    return " ".join(p.capitalize() for p in parts) if parts else ""


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
        try:
            page = request.args.get("page", 1, type=int)
            search = request.args.get("search", "").strip()
            state = request.args.get("state", "").strip()
            site_id = request.args.get("site_id", "").strip()
            city = request.args.get("city", "").strip()

            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            # Build query
            where_clauses = []
            params = []
            if search:
                where_clauses.append("(deceased_name LIKE %s OR funeral_home LIKE %s)")
                params.extend([f"%{search}%", f"%{search}%"])
            if state:
                where_clauses.append("site_id LIKE %s")
                params.append(f"{state}-%")
            if site_id:
                where_clauses.append("site_id = %s")
                params.append(site_id)
            if city:
                where_clauses.append("(obit_text LIKE %s OR funeral_home LIKE %s)")
                params.extend([f"%{city}%", f"%{city}%"])

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

            # Get distinct states and site_ids for filters
            cur.execute("SELECT DISTINCT site_id FROM obituaries ORDER BY site_id")
            all_site_ids = [r["site_id"] for r in cur.fetchall()]

            # Build state list from site_ids
            states = sorted({_state_from_site_id(s) for s in all_site_ids})
            states_display = [(s, STATE_MAP.get(s, s.upper())) for s in states]

            # If state is selected, filter counties to that state
            if state:
                counties = sorted(
                    [(s, _county_from_site_id(s)) for s in all_site_ids if s.startswith(f"{state}-")],
                    key=lambda x: x[1],
                )
            else:
                counties = sorted(
                    [(s, _county_from_site_id(s)) for s in all_site_ids],
                    key=lambda x: x[1],
                )

            cur.close()
            conn.close()

            return render_template(
                "index.html",
                obits=obits,
                page=page,
                total_pages=total_pages,
                total=total,
                states=states_display,
                counties=counties,
                search=search,
                state=state,
                site_id=site_id,
                city=city,
            )
        except Exception as e:
            logger.error("Index route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    @app.route("/stats")
    def stats():
        try:
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
        except Exception as e:
            logger.error("Stats route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    return app
