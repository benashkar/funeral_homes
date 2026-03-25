"""Backfill death_city/death_state for existing obits missing it.

Re-fetches detail pages with rate limiting. Uses the full parse_death_place
fallback chain (Person.deathPlace -> og:title). Marks broken URLs as is_deleted.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bs4 import BeautifulSoup
from scraper.obit_parser import parse_death_place
from scraper.db_writer import get_connection
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_city")

UPDATE_SQL = "UPDATE obituaries SET death_city = %s, death_state = %s WHERE id = %s"
DELETE_SQL = "UPDATE obituaries SET is_deleted = 1 WHERE id = %s"

BATCH_SIZE = 500


def run():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, legacy_url FROM obituaries "
        "WHERE death_city IS NULL AND is_deleted = 0 ORDER BY id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Found %d obits missing death_city", total)

    session = create_session()
    updated = 0
    broken = 0
    no_data = 0

    for i, row in enumerate(rows, 1):
        resp = polite_get(session, row["legacy_url"])

        if not resp:
            # URL completely failed
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(DELETE_SQL, (row["id"],))
            conn.commit()
            cur.close()
            conn.close()
            broken += 1
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Check if this is a broken redirect (SearchResultsPage instead of obit)
        blocks = {}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                data = json.loads(script.string or "")
                if isinstance(data, dict) and "@type" in data:
                    blocks[data["@type"]] = data
            except Exception:
                pass

        if "SearchResultsPage" in blocks and "NewsArticle" not in blocks:
            # Broken URL — redirected to search page
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(DELETE_SQL, (row["id"],))
            conn.commit()
            cur.close()
            conn.close()
            broken += 1
            continue

        place = parse_death_place(soup)

        if place["city"]:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(UPDATE_SQL, (place["city"], place["state"], row["id"]))
            conn.commit()
            cur.close()
            conn.close()
            updated += 1
        else:
            no_data += 1

        if i % 200 == 0:
            logger.info(
                "Progress: %d/%d (updated=%d, broken=%d, no_data=%d)",
                i, total, updated, broken, no_data,
            )

    logger.info(
        "Backfill complete: %d updated, %d broken (deleted), %d no city available, %d total",
        updated, broken, no_data, total,
    )


if __name__ == "__main__":
    run()
