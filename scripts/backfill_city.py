"""One-time backfill: fetch death_city/death_state for existing obits missing it.

Reads all obituaries where death_city IS NULL, re-fetches the detail page,
parses the Person.deathPlace, and updates the row. Uses polite rate limiting.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scraper.obit_parser import parse_death_place
from scraper.db_writer import get_connection
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_city")

UPDATE_SQL = """
    UPDATE obituaries SET death_city = %s, death_state = %s WHERE id = %s
"""


def run():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute(
        "SELECT id, legacy_url FROM obituaries WHERE death_city IS NULL ORDER BY id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Found %d obits missing death_city — starting backfill", total)

    session = create_session()
    updated = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        resp = polite_get(session, row["legacy_url"])
        if not resp:
            skipped += 1
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        place = parse_death_place(soup)

        if place["city"]:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(UPDATE_SQL, (place["city"], place["state"], row["id"]))
            conn.commit()
            cur.close()
            conn.close()
            updated += 1

        if i % 100 == 0:
            logger.info(
                "Progress: %d/%d (updated=%d, skipped=%d)",
                i, total, updated, skipped,
            )

    logger.info(
        "Backfill complete: %d/%d updated, %d skipped", updated, total, skipped
    )


if __name__ == "__main__":
    run()
