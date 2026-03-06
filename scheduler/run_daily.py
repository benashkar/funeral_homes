"""Daily cron entry point — loops all markets and scrapes obituaries."""

import json
import os
import sys

from dotenv import load_dotenv

# Add project root to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from scraper.legacy_scraper import LegacyScraper
from scraper.db_writer import _get_connection, upsert_obit, log_run, url_exists
from utils.logger import get_logger

logger = get_logger("run_daily")

MARKETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "markets.json",
)


def load_markets():
    """Load the markets list from config/markets.json."""
    with open(MARKETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_known_urls(conn):
    """Fetch all existing legacy_urls from the database for fast dedup.

    Returns:
        set of URL strings.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT legacy_url FROM obituaries")
    urls = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return urls


def run():
    """Main entry point: loop markets, scrape, write to DB."""
    markets = load_markets()
    logger.info("Loaded %d markets", len(markets))

    conn = _get_connection()
    known_urls = get_known_urls(conn)
    logger.info("DB has %d existing obituary URLs", len(known_urls))

    total_found = 0
    total_new = 0

    for market in markets:
        site_id = market["site_id"]
        scraper = LegacyScraper(market)
        errors = []

        try:
            obits = scraper.scrape_today(known_urls=known_urls)
        except Exception as e:
            logger.error("[%s] Scraper crashed: %s", site_id, e)
            log_run(conn, site_id, 0, 0, errors=str(e))
            continue

        found = len(obits)
        new = 0

        for obit in obits:
            try:
                inserted = upsert_obit(conn, obit, site_id)
                if inserted:
                    new += 1
                    known_urls.add(obit["legacy_url"])
            except Exception as e:
                logger.error("[%s] DB write error: %s", site_id, e)
                errors.append(str(e))

        error_text = "; ".join(errors) if errors else None
        log_run(conn, site_id, found, new, errors=error_text)

        total_found += found
        total_new += new
        logger.info("[%s] Done — found=%d, new=%d", site_id, found, new)

    conn.close()
    logger.info("Run complete — total found=%d, total new=%d", total_found, total_new)


if __name__ == "__main__":
    run()
