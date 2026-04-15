"""CR Obituaries Dashboard — browse scraped obituary data."""

import json
import os
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, render_template, request
import mysql.connector

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

# Central Time offset (UTC-6 standard, UTC-5 daylight)
CT = timezone(timedelta(hours=-5))  # CDT (March-November)

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


def _pagination_pages(current_page, total_pages, window=2):
    """Compute a compact list of page numbers for pagination display.

    Returns a list like [1, 2, 3, None, 8, 9, 10, None, 98, 99, 100]
    where None represents an ellipsis gap.
    """
    pages = set()
    # Always show first 3 and last 3
    for p in range(1, min(4, total_pages + 1)):
        pages.add(p)
    for p in range(max(1, total_pages - 2), total_pages + 1):
        pages.add(p)
    # Window around current page
    for p in range(max(1, current_page - window), min(total_pages, current_page + window) + 1):
        pages.add(p)

    sorted_pages = sorted(pages)
    result = []
    for i, p in enumerate(sorted_pages):
        if i > 0 and p > sorted_pages[i - 1] + 1:
            result.append(None)
        result.append(p)
    return result


def create_app():
    app = Flask(__name__)

    # Auto-migrate on startup (idempotent)
    try:
        from scraper.db_writer import ensure_schema as _ensure
        conn = _get_conn()
        _ensure(conn)
        conn.close()
    except Exception as e:
        logger.warning("Auto-migration skipped: %s", e)

    @app.template_filter("to_ct")
    def to_ct_filter(dt):
        """Convert a UTC datetime to Central Time string."""
        if dt is None:
            return ""
        if isinstance(dt, datetime):
            utc_dt = dt.replace(tzinfo=timezone.utc)
            ct_dt = utc_dt.astimezone(CT)
            return ct_dt.strftime("%Y-%m-%d %I:%M %p CT")
        return str(dt)

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

            pagination_pages = _pagination_pages(page, total_pages)

            return render_template(
                "index.html",
                obits=obits,
                page=page,
                total_pages=total_pages,
                total=total,
                pagination_pages=pagination_pages,
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

    @app.route("/funeral-homes")
    def funeral_homes_list():
        try:
            page = request.args.get("page", 1, type=int)
            search = request.args.get("search", "").strip()
            state = request.args.get("state", "").strip()

            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            where_clauses = []
            params = []
            if search:
                where_clauses.append("fh.name LIKE %s")
                params.append(f"%{search}%")
            if state:
                where_clauses.append("fh.state = %s")
                params.append(state)

            where = ""
            if where_clauses:
                where = "WHERE " + " AND ".join(where_clauses)

            cur.execute(f"SELECT COUNT(*) as cnt FROM funeral_homes fh {where}", params)
            total = cur.fetchone()["cnt"]
            total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

            offset = (page - 1) * PER_PAGE
            cur.execute(
                f"""SELECT fh.*, COUNT(o.id) as obit_count
                    FROM funeral_homes fh
                    LEFT JOIN obituaries o ON o.funeral_home_id = fh.id AND o.is_deleted = 0
                    {where}
                    GROUP BY fh.id
                    ORDER BY obit_count DESC, fh.name
                    LIMIT %s OFFSET %s""",
                params + [PER_PAGE, offset],
            )
            fh_rows = cur.fetchall()

            # Distinct states for filter
            cur.execute("SELECT DISTINCT state FROM funeral_homes WHERE state IS NOT NULL ORDER BY state")
            states = [r["state"] for r in cur.fetchall()]

            cur.close()
            conn.close()

            pagination_pages = _pagination_pages(page, total_pages)

            return render_template(
                "funeral_homes.html",
                funeral_homes=fh_rows,
                page=page,
                total_pages=total_pages,
                total=total,
                pagination_pages=pagination_pages,
                states=states,
                search=search,
                state=state,
            )
        except Exception as e:
            logger.error("Funeral homes route error: %s", e)
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
            for table in ["obituaries", "scrape_log", "funeral_homes"]:
                cur.execute(f"DESCRIBE {table}")
                tables[table] = cur.fetchall()

            # Get index info
            indexes = {}
            for table in ["obituaries", "scrape_log", "funeral_homes"]:
                cur.execute(f"SHOW INDEX FROM {table}")
                raw = cur.fetchall()
                indexes[table] = [
                    {"name": r["Key_name"], "column": r["Column_name"], "unique": r["Non_unique"] == 0}
                    for r in raw
                ]

            # Get row counts
            counts = {}
            for table in ["obituaries", "scrape_log", "funeral_homes"]:
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

    @app.route("/health-cherry-road")
    def health_cherry_road():
        """Per-city health for Cherry Road customer markets.

        Status: green (data within 48h), yellow (3-5 days), red (>5 days or never).
        """
        manifest_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "cherry_road_markets.json",
        )
        try:
            with open(manifest_path, encoding="utf-8") as f:
                cr_cities = json.load(f)
        except FileNotFoundError:
            return jsonify({"error": "cherry_road_markets.json not found"}), 500

        try:
            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            results = []
            counts = {"green": 0, "yellow": 0, "red": 0}

            for entry in cr_cities:
                city = entry["city"]
                state = entry["state"]
                site_id = entry["site_id"]
                newspaper = entry["newspaper"]

                cur.execute(
                    "SELECT MAX(scraped_at) as last_scraped, MAX(published_date) as last_pub, "
                    "COUNT(*) as total, "
                    "SUM(CASE WHEN scraped_at >= NOW() - INTERVAL 7 DAY THEN 1 ELSE 0 END) as last_7d "
                    "FROM obituaries "
                    "WHERE LOWER(death_city) = LOWER(%s) AND LOWER(death_state) = LOWER(%s) "
                    "AND is_deleted = 0",
                    (city, state),
                )
                row = cur.fetchone()
                last_scraped = row["last_scraped"]
                days_stale = None
                if last_scraped:
                    days_stale = (datetime.now() - last_scraped).days

                if days_stale is None:
                    status = "red"
                elif days_stale <= 2:
                    status = "green"
                elif days_stale <= 5:
                    status = "yellow"
                else:
                    status = "red"

                counts[status] += 1
                results.append({
                    "newspaper": newspaper,
                    "city": city,
                    "state": state,
                    "site_id": site_id,
                    "total_obits": int(row["total"] or 0),
                    "obits_last_7d": int(row["last_7d"] or 0),
                    "last_scraped": last_scraped.isoformat() if last_scraped else None,
                    "last_published": row["last_pub"].isoformat() if row["last_pub"] else None,
                    "days_stale": days_stale,
                    "status": status,
                })

            cur.close()
            conn.close()

            results.sort(key=lambda r: ({"red": 0, "yellow": 1, "green": 2}[r["status"]], r["state"], r["city"]))

            return jsonify({
                "summary": {
                    "total_cities": len(results),
                    "green": counts["green"],
                    "yellow": counts["yellow"],
                    "red": counts["red"],
                },
                "cities": results,
            })
        except Exception as e:
            logger.error("Cherry Road health error: %s", e)
            return jsonify({"error": str(e)}), 503

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

            # Check: markets with no new data in 3+ days
            cur.execute("""
                SELECT sl.site_id,
                       MAX(sl.run_date) as last_run,
                       DATEDIFF(CURDATE(), MAX(sl.run_date)) as days_stale
                FROM scrape_log sl
                GROUP BY sl.site_id
                HAVING days_stale >= 3
                ORDER BY days_stale DESC
                LIMIT 50
            """)
            stale_markets = cur.fetchall()
            if stale_markets:
                stale_list = "; ".join(
                    f"{r['site_id']} ({r['days_stale']}d)"
                    for r in stale_markets[:10]
                )
                more = f" (+{len(stale_markets) - 10} more)" if len(stale_markets) > 10 else ""
                alerts.append({
                    "level": "warning",
                    "title": f"{len(stale_markets)} market(s) have no new data in 3+ days",
                    "detail": stale_list + more,
                })

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
                stale_markets=stale_markets,
            )
        except Exception as e:
            logger.error("Health route error: %s", e)
            return f"<h1>Database Error</h1><p>{e}</p>", 503

    @app.route("/health-status.json")
    def health_status_json():
        """Structured health snapshot for the Layer-3 diagnostic agent.

        Splits IP-blocked markets (errors LIKE 'listing_fetch_failed%') from
        real errors so the agent can pick the right Fix Recipe.
        """
        try:
            conn = _get_conn()
            cur = conn.cursor(dictionary=True)

            cur.execute("""
                SELECT COUNT(DISTINCT site_id) AS markets,
                       SUM(obits_found) AS found,
                       SUM(obits_new) AS new_obits,
                       SUM(CASE WHEN errors LIKE 'listing_fetch_failed%' THEN 1 ELSE 0 END) AS blocked,
                       SUM(CASE WHEN errors IS NOT NULL AND errors != ''
                                  AND errors NOT LIKE 'listing_fetch_failed%' THEN 1 ELSE 0 END) AS errors
                FROM scrape_log WHERE run_date = CURDATE()
            """)
            today = cur.fetchone() or {}
            markets = today.get("markets") or 0
            blocked = today.get("blocked") or 0

            cur.execute("SELECT SUM(obits_new) AS n FROM scrape_log WHERE run_date = CURDATE() - INTERVAL 1 DAY")
            yesterday_new = (cur.fetchone() or {}).get("n") or 0

            cur.execute("""
                SELECT site_id, errors FROM scrape_log
                WHERE run_date = CURDATE() AND errors LIKE 'listing_fetch_failed%'
                ORDER BY site_id LIMIT 20
            """)
            blocked_sample = [r["site_id"] for r in cur.fetchall()]

            cur.execute("""
                SELECT site_id, errors FROM scrape_log
                WHERE run_date = CURDATE() AND errors IS NOT NULL AND errors != ''
                  AND errors NOT LIKE 'listing_fetch_failed%'
                ORDER BY run_at DESC LIMIT 20
            """)
            error_sample = [{"site_id": r["site_id"], "error": (r["errors"] or "")[:200]} for r in cur.fetchall()]

            cur.execute("""
                SELECT site_id, DATEDIFF(CURDATE(), MAX(run_date)) AS days_stale
                FROM scrape_log GROUP BY site_id
                HAVING days_stale >= 3 ORDER BY days_stale DESC LIMIT 50
            """)
            stale = [{"site_id": r["site_id"], "days_stale": r["days_stale"]} for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) AS c FROM obituaries WHERE is_deleted=0 AND death_date IS NULL")
            missing_dd = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM obituaries WHERE is_deleted=0 AND funeral_home IS NULL")
            missing_fh = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM obituaries WHERE is_deleted=0")
            total_obits = cur.fetchone()["c"]

            cur.close()
            conn.close()

            blocked_pct = (blocked * 100 // markets) if markets else 0
            if blocked_pct >= 50:
                status = "BLOCKED"
            elif (today.get("errors") or 0) > 0 or blocked > 0 or stale:
                status = "ISSUES"
            else:
                status = "OK"

            return jsonify({
                "status": status,
                "scrape_today": {
                    "markets": markets,
                    "found": today.get("found") or 0,
                    "new": today.get("new_obits") or 0,
                    "blocked": blocked,
                    "blocked_pct": blocked_pct,
                    "errors": today.get("errors") or 0,
                    "yesterday_new": yesterday_new,
                },
                "blocked_sample": blocked_sample,
                "error_sample": error_sample,
                "stale_markets": stale,
                "backfill": {
                    "missing_death_date": missing_dd,
                    "missing_funeral_home": missing_fh,
                    "total_obits": total_obits,
                },
            })
        except Exception as e:
            logger.error("health_status_json error: %s", e)
            return jsonify({"error": str(e)}), 503

    return app
