"""CR Obituaries Dashboard — browse scraped obituary data."""

from flask import Flask, render_template, request
import mysql.connector

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

PER_PAGE = 50

STATE_MAP = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "dc": "DC", "fl": "Florida", "ga": "Georgia", "hi": "Hawaii",
    "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine",
    "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico",
    "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island",
    "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas",
    "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
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
    return site_id.split("-")[0] if site_id else ""


def _county_from_site_id(site_id):
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

            where_clauses = ["is_deleted = 0"]
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
                where_clauses.append("death_city = %s")
                params.append(city)

            where = ""
            if where_clauses:
                where = "WHERE " + " AND ".join(where_clauses)

            cur.execute(f"SELECT COUNT(*) as cnt FROM obituaries {where}", params)
            total = cur.fetchone()["cnt"]
            total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

            offset = (page - 1) * PER_PAGE
            cur.execute(
                f"SELECT * FROM obituaries {where} ORDER BY published_date DESC, id DESC LIMIT %s OFFSET %s",
                params + [PER_PAGE, offset],
            )
            obits = cur.fetchall()

            # Filters: states, counties, cities
            cur.execute("SELECT DISTINCT site_id FROM obituaries WHERE is_deleted = 0 ORDER BY site_id")
            all_site_ids = [r["site_id"] for r in cur.fetchall()]

            states = sorted({_state_from_site_id(s) for s in all_site_ids})
            states_display = [(s, STATE_MAP.get(s, s.upper())) for s in states]

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

            # City dropdown — distinct death_city values, filtered by state if selected
            city_where = "WHERE is_deleted = 0 AND death_city IS NOT NULL AND death_city != ''"
            city_params = []
            if state:
                city_where += " AND site_id LIKE %s"
                city_params.append(f"{state}-%")
            if site_id:
                city_where += " AND site_id = %s"
                city_params.append(site_id)
            cur.execute(
                f"SELECT DISTINCT death_city FROM obituaries {city_where} ORDER BY death_city",
                city_params,
            )
            cities = [r["death_city"] for r in cur.fetchall()]

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
                cities=cities,
                search=search,
                state=state,
                site_id=site_id,
                city=city,
            )
        except Exception as e:
            logger.error("Index route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    @app.route("/obit/<int:obit_id>")
    def obit_detail(obit_id):
        try:
            conn = _get_conn()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM obituaries WHERE id = %s", (obit_id,))
            obit = cur.fetchone()
            cur.close()
            conn.close()

            if not obit:
                return "<h1>Not Found</h1>", 404

            return render_template("detail.html", obit=obit)
        except Exception as e:
            logger.error("Detail route error: %s", e)
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
                WHERE is_deleted = 0
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

    @app.route("/schema")
    def schema():
        try:
            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            # Get table schemas
            tables = {}
            for table in ["obituaries", "scrape_log"]:
                cur.execute(f"DESCRIBE {table}")
                tables[table] = cur.fetchall()

            # Get index info
            indexes = {}
            for table in ["obituaries", "scrape_log"]:
                cur.execute(f"SHOW INDEX FROM {table}")
                raw = cur.fetchall()
                indexes[table] = [
                    {"name": r["Key_name"], "column": r["Column_name"], "unique": r["Non_unique"] == 0}
                    for r in raw
                ]

            # Get row counts
            counts = {}
            for table in ["obituaries", "scrape_log"]:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
                counts[table] = cur.fetchone()["cnt"]

            cur.close()
            conn.close()

            return render_template(
                "schema.html",
                tables=tables,
                indexes=indexes,
                counts=counts,
            )
        except Exception as e:
            logger.error("Schema route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    @app.route("/erd")
    def erd():
        return app.send_static_file("erd.html")

    @app.route("/health-status")
    def health_status():
        try:
            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            alerts = []

            # Check: did the scraper run today?
            cur.execute("""
                SELECT COUNT(*) as cnt FROM scrape_log
                WHERE run_date = CURDATE()
            """)
            today_runs = cur.fetchone()["cnt"]
            if today_runs == 0:
                alerts.append({
                    "level": "danger",
                    "title": "Daily scrape did NOT run today",
                    "detail": "No scrape_log entries for today. Check Render cron job.",
                })

            # Check: did the scraper run yesterday?
            cur.execute("""
                SELECT COUNT(*) as cnt FROM scrape_log
                WHERE run_date = CURDATE() - INTERVAL 1 DAY
            """)
            yesterday_runs = cur.fetchone()["cnt"]

            # Check: any errors in the last 24h?
            cur.execute("""
                SELECT site_id, errors FROM scrape_log
                WHERE errors IS NOT NULL AND errors != ''
                  AND run_at >= NOW() - INTERVAL 24 HOUR
                ORDER BY run_at DESC LIMIT 20
            """)
            recent_errors = cur.fetchall()
            if recent_errors:
                alerts.append({
                    "level": "warning",
                    "title": f"{len(recent_errors)} market(s) had errors in last 24h",
                    "detail": "; ".join(f"{r['site_id']}: {r['errors'][:80]}" for r in recent_errors[:5]),
                })

            # Stats
            cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE is_deleted = 0")
            total_obits = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE is_deleted = 1")
            deleted_obits = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE death_city IS NOT NULL AND is_deleted = 0")
            with_city = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE photo_url IS NOT NULL AND is_deleted = 0")
            with_photo = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(DISTINCT SUBSTRING(site_id, 1, 2)) as cnt FROM obituaries WHERE is_deleted = 0")
            states = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(DISTINCT site_id) as cnt FROM obituaries WHERE is_deleted = 0")
            markets = cur.fetchone()["cnt"]

            # Last 7 days scrape summary
            cur.execute("""
                SELECT run_date, COUNT(*) as markets_scraped,
                       SUM(obits_found) as total_found,
                       SUM(obits_new) as total_new,
                       SUM(CASE WHEN errors IS NOT NULL AND errors != '' THEN 1 ELSE 0 END) as error_count
                FROM scrape_log
                WHERE run_date >= CURDATE() - INTERVAL 7 DAY
                GROUP BY run_date
                ORDER BY run_date DESC
            """)
            daily_summary = cur.fetchall()

            if not alerts and today_runs > 0:
                alerts.append({
                    "level": "success",
                    "title": "All systems healthy",
                    "detail": f"Daily scrape ran today ({today_runs} markets logged).",
                })

            cur.close()
            conn.close()

            return render_template(
                "health.html",
                alerts=alerts,
                total_obits=total_obits,
                deleted_obits=deleted_obits,
                with_city=with_city,
                with_photo=with_photo,
                states=states,
                markets=markets,
                today_runs=today_runs,
                yesterday_runs=yesterday_runs,
                daily_summary=daily_summary,
            )
        except Exception as e:
            logger.error("Health route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    return app
