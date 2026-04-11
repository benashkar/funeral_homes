"""Analyze Cherry Road market failures by scraper assignment."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_HOST"] = "10.10.0.8"

from dotenv import load_dotenv
load_dotenv()

from utils.aws_secrets import get_db_credentials
import mysql.connector

creds = get_db_credentials()
conn = mysql.connector.connect(
    host=creds["DB_HOST"], port=int(creds["DB_PORT"]),
    user=creds["DB_USER"], password=creds["DB_PASSWORD"],
    database=creds["DB_NAME"], connect_timeout=10
)
cur = conn.cursor()

# 1. Claxton
print("=== GA-CLAXTON STATUS ===")
cur.execute("""
    SELECT run_date, obits_found, obits_new, errors
    FROM scrape_log WHERE site_id = 'ga-claxton'
    ORDER BY run_date DESC LIMIT 10
""")
for r in cur.fetchall():
    err = (r[3] or "")[:60]
    print(f"  {r[0]} f={r[1]} n={r[2]} {err}")
cur.execute("SELECT MAX(scraped_at) FROM obituaries WHERE site_id = 'ga-claxton' AND is_deleted = 0")
print(f"  Latest obit: {cur.fetchone()[0]}")

# 2. Apr 10 vs 11
print()
print("=== APR 10 vs APR 11 ===")
for d in ["2026-04-10", "2026-04-11"]:
    cur.execute("""
        SELECT COUNT(*), SUM(CASE WHEN obits_found > 0 THEN 1 ELSE 0 END),
               SUM(obits_found), SUM(obits_new),
               SUM(CASE WHEN errors LIKE '%%listing_fetch_failed%%' THEN 1 ELSE 0 END)
        FROM scrape_log WHERE run_date = %s
    """, (d,))
    row = cur.fetchone()
    if row[0]:
        pct = (row[1] / row[0] * 100) if row[0] else 0
        print(f"  {d}: {row[0]} runs | {int(row[1] or 0)} found ({pct:.0f}%) | "
              f"{int(row[2] or 0)} total | {int(row[3] or 0)} new | {int(row[4] or 0)} fail")
    else:
        print(f"  {d}: no data yet")

# 3. Per-scraper CR analysis
print()
print("=== CHERRY ROAD FAILURE BY SCRAPER (Apr 10-11 combined) ===")

SCRAPER_STATES = {
    "scraper-1": ["va", "id", "wa", "nm", "ak", "ut"],
    "scraper-2": ["ky", "ne", "md", "wy", "oh", "nv"],
    "scraper-3": ["mo", "mi", "or", "me", "az", "vt"],
    "scraper-4": ["ks", "ar", "nj", "nh", "ri", "hi"],
    "scraper-5": ["il", "sd", "tx", "ny"],
    "scraper-6": ["nc", "al", "ok"],
    "scraper-7": ["ia", "mn", "ma", "ct", "de", "dc"],
    "scraper-8": ["tn", "in", "ga"],
    "scraper-9": ["ms", "pa", "co"],
    "scraper-10": ["la", "ca", "fl", "wi", "mt", "wv", "nd", "sc"],
}

CR_SITES = [
    "al-barbour", "ar-fulton", "ar-clay", "ar-searcy", "ar-randolph", "co-bent", "co-otero",
    "co-las-animas", "id-valley", "il-cass", "in-dearborn", "in-madison", "in-tipton",
    "ia-fremont", "ks-mcpherson", "ks-pratt", "ks-kiowa", "ks-stafford", "ks-miami", "ks-brown",
    "ks-atchison", "ks-butler", "ks-leavenworth", "ks-ford", "ks-harvey", "ks-finney", "ks-ellis",
    "ks-sumner", "ks-ottawa", "ma-worcester", "ma-grafton", "ma-holden", "ma-millbury",
    "mi-arenac", "mi-ogemaw", "mi-oscoda", "mn-st-louis", "mn-cook", "mn-meeker",
    "mn-koochiching", "mn-brown", "mn-redwood", "mn-chippewa", "mn-lac-qui-parle", "mn-lake",
    "mn-watonwan", "mn-stearns", "mo-pike", "mo-lincoln", "mo-audrain", "mo-montgomery",
    "mo-monroe", "mo-boone", "mo-cooper", "mo-linn", "mo-randolph", "mo-andrew", "mo-clay",
    "mo-daviess", "mo-jackson", "mo-saline", "mo-livingston", "mo-oregon", "ne-otoe",
    "ny-yates", "oh-allen", "oh-monroe", "oh-paulding", "oh-putnam", "oh-van-wert",
    "oh-madison-twp", "oh-grove-city", "ok-pottawatomie", "ok-carter", "tx-jim-wells",
    "tx-brown", "tx-erath", "tx-somervell", "tx-grayson", "tx-ellis", "tx-palo-pinto",
    "tx-runnels", "ut-uintah", "ut-duchesne", "ut-sevier", "ga-claxton",
]

for name, states in sorted(SCRAPER_STATES.items()):
    cr_in = [s for s in CR_SITES if s.split("-")[0] in states]
    if not cr_in:
        continue
    ph = ",".join(["%s"] * len(cr_in))
    cur.execute(f"""
        SELECT COUNT(*),
               SUM(CASE WHEN obits_found > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN errors LIKE '%%listing_fetch_failed%%' THEN 1 ELSE 0 END)
        FROM scrape_log
        WHERE run_date >= '2026-04-10' AND site_id IN ({ph})
    """, cr_in)
    row = cur.fetchone()
    if not row[0]:
        continue
    ok = int(row[1] or 0)
    fail = int(row[2] or 0)
    pct = ok / row[0] * 100

    # Get consistently failing markets
    cur.execute(f"""
        SELECT site_id, SUM(CASE WHEN obits_found > 0 THEN 1 ELSE 0 END) as ok_cnt,
               SUM(CASE WHEN errors LIKE '%%listing_fetch_failed%%' THEN 1 ELSE 0 END) as fail_cnt
        FROM scrape_log
        WHERE run_date >= '2026-04-10' AND site_id IN ({ph})
        GROUP BY site_id
        HAVING fail_cnt > 0 AND ok_cnt = 0
    """, cr_in)
    always_fail = [r[0] for r in cur.fetchall()]

    states_str = ",".join(states)
    print(f"  {name} ({states_str}): {row[0]} CR runs | {ok} OK ({pct:.0f}%) | {fail} fail")
    if always_fail:
        print(f"    ALWAYS FAIL: {', '.join(always_fail)}")

# 4. Overall market count per scraper (total markets, not just CR)
print()
print("=== TOTAL MARKETS PER SCRAPER ===")
import json
with open("config/markets.json") as f:
    markets = json.load(f)
for name, states in sorted(SCRAPER_STATES.items()):
    count = sum(1 for m in markets if m["site_id"].split("-")[0] in states)
    cr_count = sum(1 for s in CR_SITES if s.split("-")[0] in states)
    print(f"  {name}: {count} total markets, {cr_count} CR priority")

cur.close()
conn.close()
