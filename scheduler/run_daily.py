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
from scraper.newspaper_direct import fetch_newspaper_obit_urls, parse_newspaper_obit_detail
from scraper.db_writer import get_connection, release_connection, pooled_connection, batch_insert_obits, log_run, get_known_urls_for_site, flag_bad_and_dupes, upsert_funeral_home, enrich_funeral_home, ensure_schema
from utils.logger import get_logger
from utils.rate_limiter import create_session, MAX_RETRIES
from utils.s3_uploader import upload_photo
from utils.telegram import send_message as telegram_send

logger = get_logger("run_daily")

# Cooldown between states (seconds) — gives Legacy.com's rate limiter time to reset
# 60s default, configurable via env var. Total overhead: ~50 states * 60s = ~50 min
STATE_COOLDOWN = int(os.environ.get("STATE_COOLDOWN", "60"))

# Optional: only scrape specific states (comma-separated 2-letter codes)
# e.g. SCRAPE_STATES=mn,wi,il,ia,in  — if unset, scrapes all states
SCRAPE_STATES = os.environ.get("SCRAPE_STATES", "")

# PRIORITY_ONLY mode: when "true", skip all non-priority markets.
# Used by the CR rescue scraper to only hit Cherry Road counties on a fresh IP.
PRIORITY_ONLY = os.environ.get("PRIORITY_ONLY", "").lower() in ("true", "1", "yes")

MARKETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "markets.json",
)
CR_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "cherry_road_markets.json",
)


def _load_cr_newspaper_urls():
    """Return {site_id: [newspaper_url, ...]} from the Cherry Road manifest.

    Multiple newspapers can serve the same county (different cities), so
    each site_id may have multiple URLs to try.
    """
    try:
        with open(CR_MANIFEST_PATH, "r", encoding="utf-8") as f:
            cr = json.load(f)
    except FileNotFoundError:
        return {}
    by_site = {}
    for entry in cr:
        url = entry.get("newspaper_url")
        if url:
            by_site.setdefault(entry["site_id"], []).append(url)
    return by_site


def load_markets():
    """Load the markets list from config/markets.json.

    Augments each market with `newspaper_urls` (list) from the Cherry
    Road manifest when applicable. If SCRAPE_STATES env var is set,
    filters to only those states.
    """
    with open(MARKETS_PATH, "r", encoding="utf-8") as f:
        markets = json.load(f)

    cr_urls = _load_cr_newspaper_urls()
    for m in markets:
        urls = cr_urls.get(m["site_id"])
        if urls:
            m["newspaper_urls"] = urls

    if SCRAPE_STATES:
        allowed = {s.strip().lower() for s in SCRAPE_STATES.split(",")}
        markets = [m for m in markets if m["site_id"].split("-")[0] in allowed]
        logger.info("Filtered to %d markets for states: %s", len(markets), SCRAPE_STATES)

    if PRIORITY_ONLY:
        markets = [m for m in markets if m.get("priority")]
        logger.info("PRIORITY_ONLY mode: filtered to %d priority markets", len(markets))

    return markets


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

    # A pooled connection that is not returned is lost for the life of the
    # process (POOL_SIZE losses and every later market fails with PoolError),
    # and its db99 session sits Sleep for the eight-hour wait_timeout. The
    # window below spans network I/O -- enrich_funeral_home and upload_photo
    # both make HTTP calls while holding the connection -- so the `finally`
    # is what actually keeps the pool alive across a few hundred markets.
    conn = None
    try:
        conn = get_connection()
        known_urls = get_known_urls_for_site(conn, site_id)
        release_connection(conn)
        conn = None

        # Priority markets (Cherry Road) get full retries. Non-priority get
        # 1 attempt only to avoid burning 8+ min per failure and timing out
        # the 12-hour Render cron limit before reaching all markets.
        is_priority = market.get("priority", False)
        listing_retries = MAX_RETRIES if is_priority else 1

        scraper = LegacyScraper(market, session=session, max_retries=listing_retries)
        scraper._last_listing_diag = None
        obits = scraper.scrape_today(known_urls=known_urls)
        found = len(obits)

        # Newspaper-direct fallback: if Legacy.com gave us nothing AND this
        # market has a configured newspaper website, try fetching obits
        # from there. This bypasses Legacy.com entirely for Cherry Road papers.
        newspaper_urls = market.get("newspaper_urls") or []
        if found == 0 and newspaper_urls:
            for np_url in newspaper_urls:
                np_obit_urls = fetch_newspaper_obit_urls(session, np_url)
                np_obit_urls = [u for u in np_obit_urls if u not in known_urls]
                if not np_obit_urls:
                    continue
                logger.info(
                    "[%s] newspaper-direct yielded %d new URLs from %s",
                    site_id, len(np_obit_urls), np_url,
                )
                for u in np_obit_urls:
                    detail = parse_newspaper_obit_detail(session, u)
                    if detail:
                        # Match LegacyScraper output schema
                        detail.setdefault("published_date", None)
                        detail.setdefault("death_date", None)
                        detail.setdefault("death_city", None)
                        detail.setdefault("death_state", None)
                        detail.setdefault("funeral_home", None)
                        detail.setdefault("funeral_home_detail", None)
                        detail.setdefault("photo_url", None)
                        obits.append(detail)
            found = len(obits)

        conn = get_connection()

        # Upsert funeral homes, enrich addresses, and attach IDs to obit dicts
        for obit in obits:
            fh_detail = obit.pop("funeral_home_detail", None) or {}
            if fh_detail.get("legacy_fh_id"):
                fh_id = upsert_funeral_home(conn, fh_detail)
                obit["funeral_home_id"] = fh_id
                conn.commit()
                # Fetch full address if this FH doesn't have one yet
                if fh_id:
                    enrich_funeral_home(conn, fh_id, session)
            else:
                obit["funeral_home_id"] = None
        conn.commit()

        # Upload photos to S3 (original + 16:9 version)
        for obit in obits:
            photo_url = obit.get("photo_url")
            if photo_url:
                temp_id = abs(hash(obit["legacy_url"])) % 10**10
                result = upload_photo(session, photo_url, site_id, temp_id)
                if result:
                    obit["s3_photo_url"] = result["original"]
                    obit["s3_photo_url_16x9"] = result["16x9"]
                else:
                    obit["s3_photo_url"] = None
                    obit["s3_photo_url_16x9"] = None
            else:
                obit["s3_photo_url"] = None
                obit["s3_photo_url_16x9"] = None

        new_count = batch_insert_obits(conn, obits, site_id)
        # Log diagnostic info when listing page returned 0 links
        diag = scraper._last_listing_diag if found == 0 else None
        log_run(conn, site_id, found, new_count, errors=diag)
        release_connection(conn)
        conn = None

        # Surface a blocked listing as an error so the in-memory counter
        # (used by the per-scraper Telegram summary) doesn't report "0 errors"
        # when every market was IP-blocked.
        return (site_id, found, new_count, diag)

    except Exception as e:
        logger.error("[%s] Failed: %s", site_id, e)
        err_conn = None
        try:
            err_conn = get_connection()
            log_run(err_conn, site_id, 0, 0, errors=str(e))
        except Exception:
            pass
        finally:
            release_connection(err_conn)
        return (site_id, 0, 0, str(e))

    finally:
        # Returns the connection on every path the body did not already
        # release it on -- including the exception path above.
        release_connection(conn)


def _run_auto_backfill():
    """Run text-based backfill for missing death dates and funeral homes.

    Fast pass: regex on existing obit_text in DB, no HTTP requests.
    Returns a short summary string, or None if nothing to fix.
    """
    from scraper.obit_parser import parse_death_date_from_text, parse_funeral_home_from_text

    # Pooled connection returned on every path, including the mid-loop
    # exception that used to strand it (see db_writer.release_connection).
    with pooled_connection() as conn:
        return _auto_backfill_with_conn(
            conn, parse_death_date_from_text, parse_funeral_home_from_text
        )


def _auto_backfill_with_conn(conn, parse_death_date_from_text, parse_funeral_home_from_text):
    """Body of the auto-backfill, on a caller-owned connection."""
    cur = conn.cursor(dictionary=True)

    # Find records missing death_date or funeral_home
    cur.execute(
        "SELECT id, obit_text, published_date, death_date, funeral_home "
        "FROM obituaries "
        "WHERE is_deleted = 0 AND obit_text IS NOT NULL "
        "AND (death_date IS NULL OR funeral_home IS NULL) "
        "ORDER BY id"
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return None

    death_fixed = 0
    fh_fixed = 0
    update_cur = conn.cursor()

    for row in rows:
        if row["death_date"] is None:
            d = parse_death_date_from_text(row["obit_text"], row["published_date"])
            if d:
                update_cur.execute(
                    "UPDATE obituaries SET death_date = %s WHERE id = %s",
                    (d, row["id"]),
                )
                death_fixed += 1

        if row["funeral_home"] is None:
            fh = parse_funeral_home_from_text(row["obit_text"])
            if fh:
                update_cur.execute(
                    "UPDATE obituaries SET funeral_home = %s WHERE id = %s",
                    (fh, row["id"]),
                )
                fh_fixed += 1

    conn.commit()
    update_cur.close()

    if death_fixed or fh_fixed:
        summary = f"death_date={death_fixed}, funeral_home={fh_fixed}"
        logger.info("[OK] Auto-backfill: %s", summary)
        return summary

    return None


def run():
    """Main entry point: scrape all markets, state by state."""
    # Ensure funeral_homes table + FK exist before scraping
    with pooled_connection() as conn:
        ensure_schema(conn)

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
    blocked = 0

    for i, (state_abbr, state_markets) in enumerate(states, 1):
        state_found = 0
        state_new = 0
        state_errors = 0
        state_blocked = 0

        # Sort priority markets first so Cherry Road counties are scraped
        # before any rate-limit accumulation hits later in the run.
        state_markets = sorted(state_markets, key=lambda m: not m.get("priority", False))

        for market in state_markets:
            site_id, found, new, error = scrape_market(market, session)
            state_found += found
            state_new += new
            if error and error.startswith("listing_fetch_failed"):
                state_blocked += 1
            elif error:
                state_errors += 1

        total_found += state_found
        total_new += state_new
        errors += state_errors
        blocked += state_blocked

        logger.info(
            "[%s] State %d/%d done — markets=%d, found=%d, new=%d, blocked=%d, errors=%d",
            state_abbr.upper(), i, len(states), len(state_markets),
            state_found, state_new, state_blocked, state_errors,
        )

        # Cooldown between states (skip after last state)
        if i < len(states):
            time.sleep(STATE_COOLDOWN)

    # Post-scrape cleanup
    with pooled_connection() as conn:
        bad, duped = flag_bad_and_dupes(conn)

    # Auto-backfill: parse missing death dates and funeral homes from text
    # Fast text-only parsing (~30s), no HTTP requests
    backfill_fixed = _run_auto_backfill()

    logger.info(
        "Run complete — markets=%d, found=%d, new=%d, blocked=%d, errors=%d, flagged_bad=%d, flagged_dupes=%d",
        len(markets), total_found, total_new, blocked, errors, bad, duped,
    )

    # Send Telegram summary
    states_label = SCRAPE_STATES if SCRAPE_STATES else "ALL"
    blocked_pct = (blocked * 100 // len(markets)) if markets else 0
    if blocked_pct >= 50:
        status = f"BLOCKED {blocked_pct}% — IPs likely banned, set PROXY_URL"
    elif errors:
        status = f"ERRORS: {errors}"
    elif blocked:
        status = f"DEGRADED: {blocked} blocked"
    else:
        status = "OK"
    msg = (
        f"<b>Obituary Scraper — {states_label}</b>\n"
        f"Status: {status}\n"
        f"Markets: {len(markets)} | Found: {total_found} | New: {total_new}\n"
        f"Blocked: {blocked} | Errors: {errors} | Bad: {bad} | Dupes: {duped}"
    )
    if backfill_fixed:
        msg += f"\nBackfill: {backfill_fixed}"
    telegram_send(msg)


if __name__ == "__main__":
    run()
