"""Daily cron entry point — scrapes all markets concurrently.

Uses a thread pool for parallel market processing. Rate limiting is
global (shared lock in rate_limiter.py) so Legacy.com sees a steady
stream of requests regardless of worker count.

At 1,200 markets this brings runtime from 40+ hours (sequential) to
under 2 hours, while staying under 1 req/sec to Legacy.com.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

# Add project root to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from scraper.legacy_scraper import LegacyScraper
from scraper.db_writer import get_connection, batch_insert_obits, log_run, get_known_urls_for_site, flag_bad_and_dupes
from utils.logger import get_logger
from utils.rate_limiter import create_session

logger = get_logger("run_daily")

# Thread pool size — controls how many markets process in parallel.
# The global rate limiter caps actual request throughput, so more workers
# just means less idle time between markets (not more requests/sec).
MAX_WORKERS = int(os.environ.get("SCRAPE_WORKERS", "4"))

MARKETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "markets.json",
)

# Shared HTTP session across workers (thread-safe with rate limiter)
_shared_session = None


def _get_session():
    """Lazily create a shared session."""
    global _shared_session
    if _shared_session is None:
        _shared_session = create_session()
    return _shared_session


def load_markets():
    """Load the markets list from config/markets.json."""
    with open(MARKETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def scrape_market(market):
    """Scrape a single market: fetch listing, dedup, fetch new details, write to DB.

    Returns:
        tuple (site_id, found_count, new_count, error_text_or_none)
    """
    site_id = market["site_id"]

    try:
        # Get known URLs for THIS market only (not entire table)
        conn = get_connection()
        known_urls = get_known_urls_for_site(conn, site_id)
        conn.close()

        # Scrape
        scraper = LegacyScraper(market, session=_get_session())
        obits = scraper.scrape_today(known_urls=known_urls)
        found = len(obits)

        # Batch insert
        conn = get_connection()
        new_count = batch_insert_obits(conn, obits, site_id)
        log_run(conn, site_id, found, new_count)
        conn.close()

        logger.info("[%s] Done — found=%d, new=%d", site_id, found, new_count)
        return (site_id, found, new_count, None)

    except Exception as e:
        logger.error("[%s] Failed: %s", site_id, e)
        try:
            conn = get_connection()
            log_run(conn, site_id, 0, 0, errors=str(e))
            conn.close()
        except Exception:
            pass
        return (site_id, 0, 0, str(e))


def run():
    """Main entry point: scrape all markets using a thread pool."""
    markets = load_markets()
    logger.info("Loaded %d markets, using %d workers", len(markets), MAX_WORKERS)

    total_found = 0
    total_new = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scrape_market, m): m for m in markets}

        for future in as_completed(futures):
            site_id, found, new, error = future.result()
            total_found += found
            total_new += new
            if error:
                errors += 1

    # Post-scrape cleanup: flag bad names and cross-market duplicates
    conn = get_connection()
    bad, duped = flag_bad_and_dupes(conn)
    conn.close()

    logger.info(
        "Run complete — markets=%d, found=%d, new=%d, errors=%d, flagged_bad=%d, flagged_dupes=%d",
        len(markets), total_found, total_new, errors, bad, duped,
    )


if __name__ == "__main__":
    run()
