"""Daily cron entry point — scrapes markets state by state.

Processes one state at a time with a cooldown between states to avoid
rate limiting from Legacy.com. Within each state, markets are processed
sequentially (one at a time) with the global rate limiter enforcing
minimum delay between requests.
"""

import json
import os
import sys
import time
from collections import defaultdict
from random import shuffle

from dotenv import load_dotenv

# Add project root to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from scraper.legacy_scraper import LegacyScraper
from scraper.db_writer import get_connection, batch_insert_obits, log_run, get_known_urls_for_site, flag_bad_and_dupes
from utils.logger import get_logger
from utils.rate_limiter import create_session

logger = get_logger("run_daily")

# Cooldown between states (seconds) — gives Legacy.com's rate limiter time to reset
# 60s default, configurable via env var. Total overhead: ~50 states * 60s = ~50 min
STATE_COOLDOWN = int(os.environ.get("STATE_COOLDOWN", "60"))

MARKETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "markets.json",
)


def load_markets():
    """Load the markets list from config/markets.json."""
    with open(MARKETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def group_by_state(markets):
    """Group markets by state, return as list of (state, markets) tuples.

    Randomizes state order so no state is always last (and most likely
    to hit accumulated rate limits).
    """
    by_state = defaultdict(list)
    for m in markets:
        state_abbr = m["site_id"].split("-")[0]
        by_state[state_abbr].append(m)
    states = list(by_state.items())
    shuffle(states)
    return states


def scrape_market(market, session):
    """Scrape a single market: fetch listing, dedup, fetch new details, write to DB.

    Returns:
        tuple (site_id, found_count, new_count, error_text_or_none)
    """
    site_id = market["site_id"]

    try:
        conn = get_connection()
        known_urls = get_known_urls_for_site(conn, site_id)
        conn.close()

        scraper = LegacyScraper(market, session=session)
        obits = scraper.scrape_today(known_urls=known_urls)
        found = len(obits)

        conn = get_connection()
        new_count = batch_insert_obits(conn, obits, site_id)
        log_run(conn, site_id, found, new_count)
        conn.close()

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
    """Main entry point: scrape all markets, state by state."""
    markets = load_markets()
    states = group_by_state(markets)
    logger.info(
        "Loaded %d markets across %d states, cooldown=%ds between states",
        len(markets), len(states), STATE_COOLDOWN,
    )

    session = create_session()
    total_found = 0
    total_new = 0
    errors = 0

    for i, (state_abbr, state_markets) in enumerate(states, 1):
        state_found = 0
        state_new = 0
        state_errors = 0

        for market in state_markets:
            site_id, found, new, error = scrape_market(market, session)
            state_found += found
            state_new += new
            if error:
                state_errors += 1

        total_found += state_found
        total_new += state_new
        errors += state_errors

        logger.info(
            "[%s] State %d/%d done — markets=%d, found=%d, new=%d, errors=%d",
            state_abbr.upper(), i, len(states), len(state_markets),
            state_found, state_new, state_errors,
        )

        # Cooldown between states (skip after last state)
        if i < len(states):
            time.sleep(STATE_COOLDOWN)

    # Post-scrape cleanup
    conn = get_connection()
    bad, duped = flag_bad_and_dupes(conn)
    conn.close()

    logger.info(
        "Run complete — markets=%d, found=%d, new=%d, errors=%d, flagged_bad=%d, flagged_dupes=%d",
        len(markets), total_found, total_new, errors, bad, duped,
    )


if __name__ == "__main__":
    run()
