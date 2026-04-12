"""Analyze how many markets succeed per scraper before rate limits kick in."""

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

# Rescue-1 had 20 priority markets and got 17/20 (85%) on Apr 12
# Let's check exactly what happened per run
print("=== RESCUE-1 (ia,mn,ma,in,ga) - PRIORITY ONLY ===")
cur.execute("""
    SELECT site_id, run_at, obits_found, obits_new, LEFT(errors, 30) as err
    FROM scrape_log
    WHERE run_at >= '2026-04-12 04:15:00' AND run_at <= '2026-04-12 06:00:00'
    AND (site_id LIKE 'ia-%%' OR site_id LIKE 'mn-%%' OR site_id LIKE 'ma-%%'
         OR site_id LIKE 'in-%%' OR site_id LIKE 'ga-%%')
    ORDER BY run_at
""")
rows = cur.fetchall()
ok = sum(1 for r in rows if r[2] > 0)
fail = sum(1 for r in rows if r[4] and "listing_fetch_failed" in r[4])
print(f"Total: {len(rows)} | OK: {ok} | Fail: {fail}")
for r in rows:
    status = "OK" if r[2] > 0 else "FAIL"
    print(f"  {r[0]:<22} {r[1]} f={r[2]:<4} n={r[3]:<4} {status}")

# Check Apr 10 (best day) - how many markets succeeded per scraper
print()
print("=== APR 10: SUCCESS COUNT PER SCRAPER (scheduled crons) ===")
cur.execute("""
    SELECT SUBSTRING_INDEX(site_id, '-', 1) as state_code,
           COUNT(*) as total,
           SUM(CASE WHEN obits_found > 0 THEN 1 ELSE 0 END) as ok
    FROM scrape_log
    WHERE run_date = '2026-04-10'
    GROUP BY state_code
    ORDER BY ok DESC
""")
total_ok = 0
total_all = 0
for r in cur.fetchall():
    pct = (r[2] / r[1] * 100) if r[1] else 0
    print(f"  {r[0]:<4} {r[1]:>4} total | {r[2]:>4} OK ({pct:>3.0f}%)")
    total_ok += r[2]
    total_all += r[1]
print(f"\n  TOTAL: {total_all} runs, {total_ok} OK ({total_ok/total_all*100:.0f}%)")

# Key: how many CR priority markets have NEVER succeeded in the last 3 days?
print()
print("=== CR MARKETS THAT HAVE SUCCEEDED AT LEAST ONCE (Apr 10-12) ===")
cr_sites = [
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
ph = ",".join(["%s"] * len(cr_sites))
cur.execute(f"""
    SELECT site_id,
           MAX(CASE WHEN obits_found > 0 THEN 1 ELSE 0 END) as ever_ok,
           MAX(obits_found) as best_found,
           SUM(obits_new) as total_new
    FROM scrape_log
    WHERE run_date >= '2026-04-10' AND site_id IN ({ph})
    GROUP BY site_id
""", cr_sites)
results = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

ever_ok = []
never_ok = []
no_run = []
for sid in sorted(set(cr_sites)):
    if sid in results:
        ok, best, new = results[sid]
        if ok:
            ever_ok.append((sid, best, new))
        else:
            never_ok.append(sid)
    else:
        no_run.append(sid)

print(f"Succeeded at least once: {len(ever_ok)}")
print(f"NEVER succeeded (always blocked): {len(never_ok)}")
print(f"No run data: {len(no_run)}")
print()
print("NEVER OK (need proxies):")
for sid in never_ok:
    print(f"  {sid}")
print()
print("NO RUN DATA:")
for sid in no_run:
    print(f"  {sid}")

cur.close()
conn.close()
