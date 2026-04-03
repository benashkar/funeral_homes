"""Daily health check — sends consolidated Telegram report.

Checks that all 5 scrapers ran today, reports row counts,
stale markets, and pipeline health. Run as a Render cron
at 14:00 UTC (8 AM CT), 2 hours after scrapers start.

Run: python scripts/daily_health_telegram.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.db_writer import get_connection, ensure_schema
from utils.telegram import send_message
from utils.logger import get_logger

logger = get_logger("daily_health")


def run():
    conn = get_connection()
    ensure_schema(conn)
    cur = conn.cursor(dictionary=True)

    # Total row counts
    cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE is_deleted = 0")
    total_obits = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM funeral_homes")
    total_fh = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM funeral_homes WHERE address IS NOT NULL")
    fh_with_address = cur.fetchone()["cnt"]

    # Today's scrape stats
    cur.execute("""
        SELECT COUNT(DISTINCT site_id) as markets,
               SUM(obits_found) as found,
               SUM(obits_new) as new_obits,
               SUM(CASE WHEN errors IS NOT NULL AND errors != '' THEN 1 ELSE 0 END) as error_count
        FROM scrape_log
        WHERE run_date = CURDATE()
    """)
    today = cur.fetchone()
    today_markets = today["markets"] or 0
    today_found = today["found"] or 0
    today_new = today["new_obits"] or 0
    today_errors = today["error_count"] or 0

    # Yesterday's stats for comparison
    cur.execute("""
        SELECT SUM(obits_new) as new_obits
        FROM scrape_log
        WHERE run_date = CURDATE() - INTERVAL 1 DAY
    """)
    yesterday_new = (cur.fetchone()["new_obits"] or 0)

    # Check which state groups ran today (to verify all 5 scrapers)
    cur.execute("""
        SELECT DISTINCT SUBSTRING(site_id, 1, 2) as state_code
        FROM scrape_log
        WHERE run_date = CURDATE()
    """)
    states_today = sorted(r["state_code"] for r in cur.fetchall())

    # Expected states (all 51)
    expected_states = {
        "ak", "al", "az", "ar", "ca", "co", "ct", "de", "dc", "fl",
        "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me",
        "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh",
        "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
        "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
    }
    missing_states = expected_states - set(states_today)

    # Stale markets (no data in 3+ days)
    cur.execute("""
        SELECT COUNT(*) as cnt FROM (
            SELECT site_id FROM scrape_log
            GROUP BY site_id
            HAVING DATEDIFF(CURDATE(), MAX(run_date)) >= 3
        ) stale
    """)
    stale_count = cur.fetchone()["cnt"]

    # S3 photo stats
    cur.execute("SELECT COUNT(*) as cnt FROM obituaries WHERE s3_photo_url IS NOT NULL AND is_deleted = 0")
    s3_photos = cur.fetchone()["cnt"]

    cur.close()
    conn.close()

    # Build the message
    all_scrapers_ok = len(missing_states) == 0
    pipeline_status = "ALL OK" if all_scrapers_ok and today_errors == 0 else "ISSUES"

    lines = [
        f"<b>Daily Pipeline Report — {date.today()}</b>",
        "",
        f"<b>Status: {'[OK]' if pipeline_status == 'ALL OK' else '[!]'} {pipeline_status}</b>",
        "",
        "<b>Scrape Results</b>",
        f"  Markets scraped: {today_markets:,}",
        f"  Obits found: {today_found:,}",
        f"  New obits: {today_new:,} (yesterday: {yesterday_new:,})",
        f"  Errors: {today_errors}",
        f"  States covered: {len(states_today)}/51",
    ]

    if missing_states:
        lines.append(f"  Missing: {', '.join(sorted(missing_states)[:10])}")

    lines += [
        "",
        "<b>Database</b>",
        f"  Obituaries: {total_obits:,}",
        f"  Funeral homes: {total_fh:,} ({fh_with_address:,} with address)",
        f"  S3 photos: {s3_photos:,}",
    ]

    if stale_count > 0:
        lines += [
            "",
            f"<b>[!] {stale_count} stale markets</b> (no data 3+ days)",
        ]

    msg = "\n".join(lines)
    logger.info("Sending daily health report via Telegram")
    send_message(msg)
    logger.info("Done")


if __name__ == "__main__":
    run()
