"""Backfill funeral_home_id and S3 photos for recent obituaries.

Combines both backfills into one script with a --days filter.
Run: python scripts/backfill_recent.py --days 7
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scraper.obit_parser import parse_funeral_home_detail
from scraper.db_writer import get_connection, upsert_funeral_home, ensure_schema
from utils.s3_uploader import upload_photo
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_recent")

UPDATE_FH_SQL = "UPDATE obituaries SET funeral_home_id = %s WHERE id = %s"
UPDATE_S3_SQL = "UPDATE obituaries SET s3_photo_url = %s WHERE id = %s"
DELETE_SQL = "UPDATE obituaries SET is_deleted = 1 WHERE id = %s"


def backfill_funeral_homes(rows, session):
    """Backfill funeral_home_id for rows missing it."""
    targets = [r for r in rows if r.get("funeral_home") and not r.get("funeral_home_id")]
    logger.info("Funeral homes backfill: %d obits to process", len(targets))

    linked = 0
    broken = 0
    no_data = 0

    for i, row in enumerate(targets, 1):
        resp = polite_get(session, row["legacy_url"])
        if not resp:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(DELETE_SQL, (row["id"],))
            conn.commit()
            cur.close()
            conn.close()
            broken += 1
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Check for broken redirect
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "SearchResultsPage":
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute(DELETE_SQL, (row["id"],))
                    conn.commit()
                    cur.close()
                    conn.close()
                    broken += 1
                    resp = None
                    break
            except Exception:
                pass
        if not resp:
            continue

        fh_detail = parse_funeral_home_detail(soup)
        if fh_detail.get("legacy_fh_id"):
            conn = get_connection()
            fh_id = upsert_funeral_home(conn, fh_detail)
            conn.commit()
            if fh_id:
                cur = conn.cursor()
                cur.execute(UPDATE_FH_SQL, (fh_id, row["id"]))
                conn.commit()
                cur.close()
                linked += 1
            conn.close()
        else:
            no_data += 1

        if i % 100 == 0:
            logger.info("FH progress: %d/%d (linked=%d, broken=%d)", i, len(targets), linked, broken)

    logger.info("FH backfill done: %d linked, %d broken, %d no data", linked, broken, no_data)
    return linked


def backfill_s3_photos(rows, session):
    """Upload photos to S3 for rows missing s3_photo_url."""
    targets = [r for r in rows if r.get("photo_url") and not r.get("s3_photo_url")]
    logger.info("S3 photos backfill: %d obits to process", len(targets))

    uploaded = 0
    failed = 0

    for i, row in enumerate(targets, 1):
        s3_url = upload_photo(session, row["photo_url"], row["site_id"], row["id"])
        if s3_url:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(UPDATE_S3_SQL, (s3_url, row["id"]))
            conn.commit()
            cur.close()
            conn.close()
            uploaded += 1
        else:
            failed += 1

        if i % 100 == 0:
            logger.info("S3 progress: %d/%d (uploaded=%d, failed=%d)", i, len(targets), uploaded, failed)

    logger.info("S3 backfill done: %d uploaded, %d failed", uploaded, failed)
    return uploaded


def run(days):
    # Ensure schema is up to date
    conn = get_connection()
    ensure_schema(conn)
    conn.close()

    # Fetch recent obits needing backfill
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, site_id, legacy_url, funeral_home, funeral_home_id, "
        "       photo_url, s3_photo_url "
        "FROM obituaries "
        "WHERE is_deleted = 0 AND scraped_at >= DATE_SUB(NOW(), INTERVAL %s DAY) "
        "ORDER BY id",
        (days,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    logger.info("Found %d obits from the past %d days", len(rows), days)

    session = create_session()

    # Phase 1: Funeral homes
    backfill_funeral_homes(rows, session)

    # Phase 2: S3 photos
    backfill_s3_photos(rows, session)

    logger.info("All backfills complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill recent obits")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back")
    args = parser.parse_args()
    run(args.days)
