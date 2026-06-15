"""One-time backfill: re-attribute existing obits to their Cherry Road town.

Cherry Road papers outsource obituaries to a publication-scoped Legacy.com page
(legacy.com/us/obituaries/{slug}/browse). Those same obits were often already
captured by the county listing scrape but stored with death_city null/county-
level, so the CR health check (which matches death_city = the paper's town) reads
the market as stale. INSERT IGNORE means re-scraping won't fix existing rows, so
this script walks each CR publication page, collects its obit URLs, and UPDATEs
the matching rows' death_city/death_state to the paper's town.

Run as a Render one-off against prod (DB creds via Secrets Manager):
    python -u scripts/backfill_cr_publication_cities.py
    python -u scripts/backfill_cr_publication_cities.py --state KS   # one state
    python -u scripts/backfill_cr_publication_cities.py --dry-run
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.legacy_scraper import LegacyScraper
from scraper.db_writer import get_connection
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("cr_backfill")

# Throwaway scraper instance just to reuse its obit-link extractor (which handles
# the standard, publication-scoped, and Next.js /person/ link formats).
_LINK_EXTRACTOR = LegacyScraper(
    {"site_id": "x-x", "state": "kansas", "legacy_slug": "x", "county": "x"}
)

CR_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "cherry_road_markets.json",
)


def publication_obit_urls(session, slug):
    """Return the set of full obit URLs listed on a CR publication browse page."""
    url = f"https://www.legacy.com/us/obituaries/{slug}/browse"
    resp = polite_get(session, url)
    if not resp or resp.status_code != 200:
        return set()
    return {u.split("?")[0].split("#")[0]
            for u in _LINK_EXTRACTOR._extract_obit_links(resp.text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="limit to one state (2-letter, e.g. KS)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CR_MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    pubs = [e for e in manifest if e.get("legacy_publication_slug")]
    if args.state:
        pubs = [e for e in pubs if (e.get("state") or "").upper() == args.state.upper()]
    logger.info("[OK] %d CR publications to backfill", len(pubs))

    session = create_session()
    conn = get_connection()
    cur = conn.cursor()

    total_updated = 0
    for e in pubs:
        slug, city, state = e["legacy_publication_slug"], e["city"], e["state"]
        urls = publication_obit_urls(session, slug)
        if not urls:
            logger.warning("[--] %s (%s, %s): no obit URLs on page", slug, city, state)
            continue
        url_list = list(urls)
        updated = 0
        # Update in chunks to keep the IN() list reasonable.
        for i in range(0, len(url_list), 200):
            chunk = url_list[i:i + 200]
            placeholders = ",".join(["%s"] * len(chunk))
            sql = (
                f"UPDATE obituaries SET death_city=%s, death_state=%s "
                f"WHERE legacy_url IN ({placeholders}) AND is_deleted=0 "
                f"AND (death_city IS NULL OR LOWER(death_city) <> LOWER(%s))"
            )
            params = [city, state] + chunk + [city]
            if args.dry_run:
                cur.execute(
                    f"SELECT COUNT(*) FROM obituaries WHERE legacy_url IN ({placeholders}) "
                    f"AND is_deleted=0 AND (death_city IS NULL OR LOWER(death_city) <> LOWER(%s))",
                    chunk + [city],
                )
                updated += cur.fetchone()[0]
            else:
                cur.execute(sql, params)
                updated += cur.rowcount
        if not args.dry_run:
            conn.commit()
        total_updated += updated
        logger.info(
            "[OK] %-26s %-18s %s: %d obits on page, %d rows %s",
            slug, f"{city}, {state}", "", len(urls), updated,
            "would update" if args.dry_run else "updated",
        )

    cur.close()
    conn.close()
    logger.info("[OK] DONE — %d rows %s", total_updated,
                "would be updated" if args.dry_run else "updated")


if __name__ == "__main__":
    main()
