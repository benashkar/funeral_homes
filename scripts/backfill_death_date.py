"""Backfill missing death_date for existing obituaries.

Two-phase approach:
  Phase A — parse death dates from obit_text already in DB (fast, no HTTP)
  Phase B — re-fetch Legacy.com pages for rows still missing (slow, HTTP)

Run:
  python scripts/backfill_death_date.py --phase a --dry-run   # preview
  python scripts/backfill_death_date.py --phase a              # text-only
  python scripts/backfill_death_date.py --phase b              # re-scrape
  python scripts/backfill_death_date.py --phase both           # full run
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scraper.obit_parser import parse_death_date_from_text, parse_dates, parse_obit_text
from scraper.db_writer import get_connection, ensure_schema
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_death_date")

UPDATE_SQL = "UPDATE obituaries SET death_date = %s WHERE id = %s"
DELETE_SQL = "UPDATE obituaries SET is_deleted = 1 WHERE id = %s"

BATCH_SIZE = 500


def run_text_parse(limit=0, dry_run=False):
    """Phase A: parse death dates from obit_text already in DB."""
    conn = get_connection()
    ensure_schema(conn)
    cur = conn.cursor(dictionary=True)

    query = (
        "SELECT id, obit_text, published_date FROM obituaries "
        "WHERE death_date IS NULL AND is_deleted = 0 "
        "AND obit_text IS NOT NULL ORDER BY id"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Phase A: %d obits missing death_date with text available", total)

    updated = 0
    no_match = 0
    batch_conn = get_connection()
    batch_cur = batch_conn.cursor()

    for i, row in enumerate(rows, 1):
        death = parse_death_date_from_text(row["obit_text"], row["published_date"])
        if death:
            if dry_run:
                snippet = (row["obit_text"] or "")[:100].replace("\n", " ")
                logger.info(
                    "[DRY-RUN] id=%d -> %s | text: %s",
                    row["id"], death, snippet,
                )
            else:
                batch_cur.execute(UPDATE_SQL, (death, row["id"]))
            updated += 1
        else:
            no_match += 1

        if i % BATCH_SIZE == 0:
            if not dry_run:
                batch_conn.commit()
            logger.info(
                "Phase A progress: %d/%d (updated=%d, no_match=%d)",
                i, total, updated, no_match,
            )

    if not dry_run:
        batch_conn.commit()
    batch_cur.close()
    batch_conn.close()

    logger.info(
        "[OK] Phase A complete: %d updated, %d no match, %d total",
        updated, no_match, total,
    )
    return no_match


def run_rescrape(limit=0, dry_run=False):
    """Phase B: re-fetch pages for obits still missing death_date."""
    conn = get_connection()
    ensure_schema(conn)
    cur = conn.cursor(dictionary=True)

    query = (
        "SELECT id, legacy_url, published_date FROM obituaries "
        "WHERE death_date IS NULL AND is_deleted = 0 ORDER BY id"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Phase B: %d obits still missing death_date, re-scraping", total)

    session = create_session()
    updated = 0
    broken = 0
    no_data = 0

    for i, row in enumerate(rows, 1):
        resp = polite_get(session, row["legacy_url"])

        if not resp:
            if not dry_run:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(DELETE_SQL, (row["id"],))
                conn.commit()
                cur.close()
                conn.close()
            broken += 1
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Check for broken redirect (SearchResultsPage)
        is_broken = False
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "SearchResultsPage":
                    is_broken = True
                    break
            except Exception:
                pass

        if is_broken:
            if not dry_run:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(DELETE_SQL, (row["id"],))
                conn.commit()
                cur.close()
                conn.close()
            broken += 1
            continue

        # Try JSON-LD Person.deathDate first
        dates = parse_dates(soup)
        death = dates["death"]

        # Fallback: try text parser on fresh articleBody
        if not death:
            obit_text = parse_obit_text(soup)
            if obit_text:
                death = parse_death_date_from_text(obit_text, row["published_date"])

        if death:
            if dry_run:
                logger.info("[DRY-RUN] id=%d -> %s", row["id"], death)
            else:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(UPDATE_SQL, (death, row["id"]))
                conn.commit()
                cur.close()
                conn.close()
            updated += 1
        else:
            no_data += 1

        if i % 200 == 0:
            logger.info(
                "Phase B progress: %d/%d (updated=%d, broken=%d, no_data=%d)",
                i, total, updated, broken, no_data,
            )

    logger.info(
        "[OK] Phase B complete: %d updated, %d broken (deleted), %d no data, %d total",
        updated, broken, no_data, total,
    )
    return no_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill missing death dates")
    parser.add_argument("--phase", choices=["a", "b", "both"], default="both",
                        help="a=text parse, b=re-scrape, both=sequential (default: both)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to process per phase (0=all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and log results without updating DB")
    args = parser.parse_args()

    if args.phase in ("a", "both"):
        run_text_parse(limit=args.limit, dry_run=args.dry_run)

    if args.phase in ("b", "both"):
        run_rescrape(limit=args.limit, dry_run=args.dry_run)

    logger.info("[OK] Backfill finished (phase=%s, dry_run=%s)", args.phase, args.dry_run)
