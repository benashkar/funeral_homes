"""Backfill funeral_home_id for existing obituaries.

Re-fetches detail pages for obits with funeral_home but no funeral_home_id,
extracts the funeral home via parse_funeral_home_detail(), upserts into the
funeral_homes table, and links via the FK.

Safe to re-run: a fetch failure is treated as retryable and never mutates the
row. Only a served SearchResultsPage -- a positive signal that the obituary URL
is dead -- soft-deletes.

Run: python -u scripts/backfill_funeral_homes.py [--limit N] [--dry-run]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import json
from bs4 import BeautifulSoup
from scraper.obit_parser import parse_funeral_home_detail
from scraper.db_writer import get_connection, upsert_funeral_home
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_funeral_homes")

UPDATE_FK_SQL = "UPDATE obituaries SET funeral_home_id = %s WHERE id = %s"
DELETE_SQL = "UPDATE obituaries SET is_deleted = 1 WHERE id = %s"

BATCH_SIZE = 500


def run(limit=None, dry_run=False):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, legacy_url FROM obituaries "
        "WHERE funeral_home IS NOT NULL AND funeral_home_id IS NULL "
        "AND is_deleted = 0 ORDER BY id DESC" + (" LIMIT %d" % int(limit) if limit else "")
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info(
        "Found %d obits needing funeral_home_id backfill (limit=%s, dry_run=%s)",
        total, limit, dry_run,
    )

    session = create_session()
    linked = 0
    broken = 0
    no_fh_data = 0
    fetch_failed = 0

    for i, row in enumerate(rows, 1):
        resp = polite_get(session, row["legacy_url"])

        if not resp:
            # A failed fetch is almost always transient (Cloudflare block, proxy
            # rotation, timeout) -- NOT a dead obituary. Soft-deleting here would
            # silently destroy good rows on every backfill run. Skip and retry
            # on a later pass; only a positive SearchResultsPage signal below
            # proves the URL is really gone.
            fetch_failed += 1
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Check for broken redirect (SearchResultsPage)
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
            if dry_run:
                logger.info(
                    "[DRY] obit %s -> %s (%s)",
                    row["id"], fh_detail["legacy_fh_id"], fh_detail["name"],
                )
                linked += 1
            else:
                conn = get_connection()
                fh_id = upsert_funeral_home(conn, fh_detail)
                conn.commit()
                if fh_id:
                    cur = conn.cursor()
                    cur.execute(UPDATE_FK_SQL, (fh_id, row["id"]))
                    conn.commit()
                    cur.close()
                    linked += 1
                conn.close()
        else:
            # Legacy.com reports no funeral home for this obit ("funeralHome":null).
            # A NULL FK is the honest answer -- leave the row alone.
            no_fh_data += 1

        if i % 200 == 0:
            logger.info(
                "Progress: %d/%d (linked=%d, broken=%d, no_fh_data=%d, fetch_failed=%d)",
                i, total, linked, broken, no_fh_data, fetch_failed,
            )

    logger.info(
        "Backfill complete: %d linked, %d broken (deleted), %d no FH data, "
        "%d fetch_failed (retryable), %d total",
        linked, broken, no_fh_data, fetch_failed, total,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the N most recent NULL rows (bounds memory + runtime).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be linked without writing to the DB.")
    a = ap.parse_args()
    run(limit=a.limit, dry_run=a.dry_run)
