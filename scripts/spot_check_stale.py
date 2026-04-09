"""Spot-check stale Cherry Road cities against Legacy.com.

For each stale city:
  1. Check the county scrape_log (are we scraping the county at all?)
  2. Hit Legacy.com city page to see if there are recent obits we're missing
"""

import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_HOST"] = "10.10.0.8"

from dotenv import load_dotenv
load_dotenv()

from utils.aws_secrets import get_db_credentials
import mysql.connector

STALE_CITIES = [
    # (city, state_abbr, state_full, last_scraped)
    ("Lawrenceburg", "IN", "indiana", "2026-03-25"),
    ("Clinton", "MA", "massachusetts", "2026-03-26"),
    ("Standish", "MI", "michigan", "2026-03-27"),
    ("Eden Valley", "MN", "minnesota", "2026-03-24"),
    ("Watkins", "MN", "minnesota", "2026-03-24"),
    ("Redwood Falls", "MN", "minnesota", "2026-04-02"),
    ("Sleepy Eye", "MN", "minnesota", "2026-04-03"),
    ("Granite Falls", "MN", "minnesota", "2026-04-02"),
    ("Grand Marais", "MN", "minnesota", "2026-04-02"),
    ("Kimball", "MN", "minnesota", "2026-04-02"),
    ("Bowling Green", "MO", "missouri", "2026-03-27"),
    ("Gladstone", "MO", "missouri", "2026-03-27"),
    ("Linneus", "MO", "missouri", "2026-03-27"),
    ("Boonville", "MO", "missouri", "2026-04-03"),
    ("Elsberry", "MO", "missouri", "2026-04-03"),
    ("Hermann", "MO", "missouri", "2026-04-03"),
    ("Moberly", "MO", "missouri", "2026-04-03"),
    ("Monroe City", "MO", "missouri", "2026-04-03"),
    ("Troy", "MO", "missouri", "2026-04-03"),
    ("Vandalia", "MO", "missouri", "2026-04-03"),
    ("Delphos", "OH", "ohio", "2026-03-31"),
    ("Ottawa", "OH", "ohio", "2026-03-29"),
    ("Paulding", "OH", "ohio", "2026-03-28"),
    ("Van Wert", "OH", "ohio", "2026-03-29"),
    ("Woodsfield", "OH", "ohio", "never"),
    ("Alice", "TX", "texas", "2026-03-25"),
    ("Vernal", "UT", "utah", "2026-04-01"),
    ("Duchesne", "UT", "utah", "2026-03-26"),
    # Zero coverage
    ("Lindsbourg", "KS", "kansas", "never"),
    ("Tower", "MN", "minnesota", "never"),
    ("Trinidad", "NM", "new-mexico", "never"),
]

BASE = "https://www.legacy.com/us/obituaries/local"


def get_county_info(cur, city, state_abbr):
    """Find which county site_id this city's obits came from."""
    cur.execute(
        "SELECT site_id, COUNT(*) as cnt FROM obituaries "
        "WHERE LOWER(death_city) = LOWER(%s) AND LOWER(death_state) = LOWER(%s) "
        "AND is_deleted = 0 GROUP BY site_id ORDER BY cnt DESC LIMIT 1",
        (city, state_abbr)
    )
    row = cur.fetchone()
    return row if row else (None, 0)


def get_county_scrape_log(cur, site_id):
    """Get recent scrape_log for a county."""
    cur.execute(
        "SELECT run_date, obits_found, obits_new FROM scrape_log "
        "WHERE site_id = %s AND run_date >= '2026-04-05' "
        "ORDER BY run_date DESC LIMIT 5",
        (site_id,)
    )
    return cur.fetchall()


def check_legacy_page(session, state_full, city_slug):
    """Check if Legacy.com has obits for this city."""
    url = f"{BASE}/{state_full}/{city_slug}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return resp.status_code, 0, []

        soup = BeautifulSoup(resp.text, "html.parser")
        # Count obit links
        obit_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/us/obituaries/" in href and "/name/" in href:
                obit_links.append(href)

        # Also check JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    main = data.get("mainEntity", {})
                    for item in main.get("itemListElement", []):
                        item_url = item.get("url", "")
                        if "/name/" in item_url and item_url not in obit_links:
                            obit_links.append(item_url)
            except (json.JSONDecodeError, TypeError):
                pass

        unique_links = list(dict.fromkeys(obit_links))
        return 200, len(unique_links), unique_links[:5]

    except Exception as e:
        return -1, 0, [str(e)]


def main():
    creds = get_db_credentials()
    conn = mysql.connector.connect(
        host=creds["DB_HOST"], port=int(creds["DB_PORT"]),
        user=creds["DB_USER"], password=creds["DB_PASSWORD"],
        database=creds["DB_NAME"], connect_timeout=10
    )
    cur = conn.cursor()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    print(f"{'City':<22} {'ST':<4} {'Last Scraped':<14} {'County':<20} {'County Log':<25} {'Legacy Status':<15} {'Obit Links'}")
    print("=" * 130)

    for city, state_abbr, state_full, last_scraped in STALE_CITIES:
        # 1. County info from DB
        county_sid, county_cnt = get_county_info(cur, city, state_abbr)
        county_label = f"{county_sid}({county_cnt})" if county_sid else "none"

        # 2. County scrape log
        if county_sid:
            logs = get_county_scrape_log(cur, county_sid)
            if logs:
                log_summary = "; ".join(f"{r[0]}:f={r[1]},n={r[2]}" for r in logs[:3])
            else:
                log_summary = "no recent runs"
        else:
            log_summary = "n/a"

        # 3. Check Legacy.com
        city_slug = city.lower().replace(" ", "-").replace(".", "")
        status, count, links = check_legacy_page(session, state_full, city_slug)

        if status == 200:
            legacy_status = f"200 ({count} obits)"
        else:
            legacy_status = f"HTTP {status}"

        print(f"{city:<22} {state_abbr:<4} {last_scraped:<14} {county_label:<20} {log_summary:<25} {legacy_status:<15} {links[:2]}")

        # Rate limit - 2 seconds between requests
        time.sleep(2)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
