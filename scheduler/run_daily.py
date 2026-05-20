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
from scraper.db_writer import get_connection, batch_insert_obits, log_run, get_known_urls_for_site, flag_bad_and_dupes, upsert_funeral_home, enrich_funeral_home, ensure_schema, find_quiet_markets
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

# Silent-zero canary: when this many markets all report 0 obits without
# blocks or errors, treat the run as suspect (almost always a parser break).
SILENT_ZERO_MARKETS_THRESHOLD = 5

# Missing-funeral-home canary: the listing-metadata harvest is the *only*
# source of funeral_home + obit_text for /person/ URLs on Legacy.com's new
# (2026 H2) Next.js layout. If a future markup change strips the result-card
# data we'd silently capture URLs+names but lose funeral_home for everyone.
# This threshold catches that pattern without false-firing on tiny batches
# or on the natural ~5% missing-FH rate of normal runs.
MISSING_FH_OBITS_THRESHOLD = 50
MISSING_FH_RATIO_THRESHOLD = 70  # percent


def _build_status(markets_count, total_found, blocked, errors, obits_missing_fh=0):
    """Build the Telegram status string from run counters.

    Priority order:
      1. BLOCKED — proxy/IP issue dominates (>=50% block rate)
      2. ERRORS  — exceptions raised during the run
      3. WARNING:silent zero — high-value canary for parser breaks
      4. WARNING:missing funeral_home — canary for listing-metadata regression
      5. DEGRADED — some blocks but data still flowed
      6. OK

    The silent-zero check is the lesson from the 2026-05-17 incident: 22
    markets returned Found=0 with Errors=0 and Blocked=0 for two days
    before anyone noticed, because Status: OK looked benign. Now that
    same pattern produces a loud WARNING line.

    The missing-funeral-home check guards the 2026-05-19 follow-up:
    /person/ URLs depend on listing-page result-card HTML for
    funeral_home + obit_text. If Legacy.com tweaks the result-card
    markup, URLs still flow but those fields silently drop to NULL.

    Pure function so it can be unit-tested without standing up the
    whole scraper.
    """
    blocked_pct = (blocked * 100 // markets_count) if markets_count else 0
    if blocked_pct >= 50:
        return f"BLOCKED {blocked_pct}% — IPs likely banned, set PROXY_URL"
    if errors:
        return f"ERRORS: {errors}"
    if total_found == 0 and markets_count >= SILENT_ZERO_MARKETS_THRESHOLD:
        return "WARNING: silent zero — possible parser break"
    if total_found >= MISSING_FH_OBITS_THRESHOLD:
        missing_pct = obits_missing_fh * 100 // total_found
        if missing_pct >= MISSING_FH_RATIO_THRESHOLD:
            return (
                f"WARNING: {missing_pct}% missing funeral_home — "
                "listing-metadata harvest may be broken"
            )
    if blocked:
        return f"DEGRADED: {blocked} blocked"
    return "OK"

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

    try:
        conn = get_connection()
        known_urls = get_known_urls_for_site(conn, site_id)
        conn.close()

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
        # Count obits where the listing-metadata harvest + detail-page
        # parsing both came back without a funeral_home. Used by the
        # missing-funeral-home canary to catch a future listing-format
        # change that silently strips funeral_home data.
        missing_fh = sum(1 for o in obits if not o.get("funeral_home"))
        # Log diagnostic info when listing page returned 0 links
        diag = scraper._last_listing_diag if found == 0 else None
        log_run(conn, site_id, found, new_count, errors=diag)
        conn.close()

        # Surface a blocked listing as an error so the in-memory counter
        # (used by the per-scraper Telegram summary) doesn't report "0 errors"
        # when every market was IP-blocked.
        return (site_id, found, new_count, diag, missing_fh)

    except Exception as e:
        logger.error("[%s] Failed: %s", site_id, e)
        try:
            conn = get_connection()
            log_run(conn, site_id, 0, 0, errors=str(e))
            conn.close()
        except Exception:
            pass
        return (site_id, 0, 0, str(e), 0)


def _run_auto_backfill():
    """Run text-based backfill for missing death dates and funeral homes.

    Fast pass: regex on existing obit_text in DB, no HTTP requests.
    Returns a short summary string, or None if nothing to fix.
    """
    from scraper.obit_parser import parse_death_date_from_text, parse_funeral_home_from_text

    conn = get_connection()
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
        conn.close()
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
    conn.close()

    if death_fixed or fh_fixed:
        summary = f"death_date={death_fixed}, funeral_home={fh_fixed}"
        logger.info("[OK] Auto-backfill: %s", summary)
        return summary

    return None


def run():
    """Main entry point: scrape all markets, state by state."""
    # Ensure funeral_homes table + FK exist before scraping
    conn = get_connection()
    ensure_schema(conn)
    conn.close()

    markets = load_markets()
    states = group_by_state(markets)
    logger.info(
        "Loaded %d markets across %d states, cooldown=%ds between states",
        len(markets), len(states), STATE_COOLDOWN,
    )

    session = create_session()
    total_found = 0
    total_new = 0
    total_missing_fh = 0
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
            site_id, found, new, error, missing_fh = scrape_market(market, session)
            state_found += found
            state_new += new
            total_missing_fh += missing_fh
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
    conn = get_connection()
    bad, duped = flag_bad_and_dupes(conn)
    # Per-market silent-zero check — markets that scraped 0 today but had
    # >=3 active days in the prior week. Catches single-market regressions
    # the batch-level silent-zero canary can't see.
    site_ids = [m["site_id"] for m in markets]
    try:
        quiet_markets = find_quiet_markets(conn, site_ids)
    except Exception as e:
        logger.warning("[--] quiet-market query failed: %s", e)
        quiet_markets = []
    conn.close()

    # Auto-backfill: parse missing death dates and funeral homes from text
    # Fast text-only parsing (~30s), no HTTP requests
    backfill_fixed = _run_auto_backfill()

    logger.info(
        "Run complete — markets=%d, found=%d, new=%d, blocked=%d, errors=%d, flagged_bad=%d, flagged_dupes=%d",
        len(markets), total_found, total_new, blocked, errors, bad, duped,
    )

    # Send Telegram summary
    states_label = SCRAPE_STATES if SCRAPE_STATES else "ALL"
    status = _build_status(
        markets_count=len(markets),
        total_found=total_found,
        blocked=blocked,
        errors=errors,
        obits_missing_fh=total_missing_fh,
    )
    msg = (
        f"<b>Obituary Scraper — {states_label}</b>\n"
        f"Status: {status}\n"
        f"Markets: {len(markets)} | Found: {total_found} | New: {total_new}\n"
        f"Blocked: {blocked} | Errors: {errors} | Bad: {bad} | Dupes: {duped}"
    )
    if backfill_fixed:
        msg += f"\nBackfill: {backfill_fixed}"
    if quiet_markets:
        # Show up to 5 in-line; full list (capped at 25) goes in scrape_log
        # for later inspection. Format: "site_id(avg=N.N)" so the reader
        # can quickly see which markets matter (high avg = real regression).
        preview = ", ".join(
            f"{sid}(avg={avg})" for sid, avg, _ in quiet_markets[:5]
        )
        suffix = "" if len(quiet_markets) <= 5 else f" +{len(quiet_markets) - 5} more"
        msg += f"\nQuiet markets: {preview}{suffix}"
    telegram_send(msg)


if __name__ == "__main__":
    run()
