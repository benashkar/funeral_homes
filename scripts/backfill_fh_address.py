"""Backfill address and lat/lon for funeral homes.

Fetches each funeral home's Legacy.com detail page and extracts
structured address data from JSON-LD (FuneralHome/LocalBusiness schema).

Run: python scripts/backfill_fh_address.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import json
from bs4 import BeautifulSoup
from scraper.db_writer import get_connection
from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("backfill_fh_address")

UPDATE_SQL = """
    UPDATE funeral_homes
    SET address = %s, city = COALESCE(%s, city), state = COALESCE(%s, state),
        zip = %s, lat = %s, lon = %s
    WHERE id = %s
"""


def parse_fh_page(html):
    """Extract address and coordinates from a funeral home detail page.

    Looks for FuneralHome, LocalBusiness, or Organization JSON-LD types
    with PostalAddress and/or GeoCoordinates.

    Returns:
        dict with keys: address, city, state, zip, lat, lon (all nullable).
    """
    result = {"address": None, "city": None, "state": None, "zip": None, "lat": None, "lon": None}
    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict):
            continue

        dtype = data.get("@type") or ""
        if dtype.lower() not in ("funeralhome", "localbusiness", "organization"):
            continue

        # Address
        addr = data.get("address") or {}
        street = addr.get("streetAddress") or ""
        city = addr.get("addressLocality") or ""
        state = addr.get("addressRegion") or ""
        zip_code = addr.get("postalCode") or ""

        if street:
            result["address"] = street.strip()
        if city:
            result["city"] = city.strip()
        if state:
            result["state"] = state.strip()
        if zip_code:
            result["zip"] = zip_code.strip()

        # Geo coordinates
        geo = data.get("geo") or {}
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        if lat is not None and lon is not None:
            try:
                result["lat"] = float(lat)
                result["lon"] = float(lon)
            except (ValueError, TypeError):
                pass

        break

    return result


def run():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, legacy_fh_id, name, legacy_url FROM funeral_homes "
        "WHERE address IS NULL AND legacy_url IS NOT NULL ORDER BY id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Found %d funeral homes needing address backfill", total)

    session = create_session()
    enriched = 0
    failed = 0
    no_data = 0

    for i, row in enumerate(rows, 1):
        resp = polite_get(session, row["legacy_url"])

        if not resp:
            failed += 1
            continue

        fh_data = parse_fh_page(resp.text)

        if fh_data["address"] or fh_data["lat"]:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(UPDATE_SQL, (
                fh_data["address"], fh_data["city"], fh_data["state"],
                fh_data["zip"], fh_data["lat"], fh_data["lon"],
                row["id"],
            ))
            conn.commit()
            cur.close()
            conn.close()
            enriched += 1
        else:
            no_data += 1

        if i % 200 == 0:
            logger.info(
                "Progress: %d/%d (enriched=%d, failed=%d, no_data=%d)",
                i, total, enriched, failed, no_data,
            )

    logger.info(
        "Address backfill complete: %d enriched, %d failed, %d no data, %d total",
        enriched, failed, no_data, total,
    )


if __name__ == "__main__":
    run()
