"""Find Cherry Road counties missing from markets.json and add them."""

import json
import sys

MARKETS_PATH = "config/markets.json"

with open(MARKETS_PATH) as f:
    markets = json.load(f)

all_sids = {m["site_id"] for m in markets}

# Cherry Road city -> county mapping (verified)
cherry_road_counties = [
    ("al-barbour", "alabama", "barbour-county", "county"),
    ("ar-fulton", "arkansas", "fulton-county", "county"),
    ("ar-clay", "arkansas", "clay-county", "county"),
    ("ar-searcy", "arkansas", "searcy-county", "county"),
    ("ar-randolph", "arkansas", "randolph-county", "county"),
    ("co-bent", "colorado", "bent-county", "county"),
    ("co-otero", "colorado", "otero-county", "county"),
    ("co-las-animas", "colorado", "las-animas-county", "county"),
    ("id-valley", "idaho", "valley-county", "county"),
    ("il-cass", "illinois", "cass-county", "county"),
    ("in-dearborn", "indiana", "dearborn-county", "county"),
    ("in-madison", "indiana", "madison-county", "county"),
    ("ia-fremont", "iowa", "fremont-county", "county"),
    ("ks-brown", "kansas", "brown-county", "county"),
    ("ks-butler", "kansas", "butler-county", "county"),
    ("ks-ford", "kansas", "ford-county", "county"),
    ("ks-harvey", "kansas", "harvey-county", "county"),
    ("ks-finney", "kansas", "finney-county", "county"),
    ("ks-ellis", "kansas", "ellis-county", "county"),
    ("ks-miami", "kansas", "miami-county", "county"),
    ("ks-sumner", "kansas", "sumner-county", "county"),
    ("ks-kiowa", "kansas", "kiowa-county", "county"),
    ("ks-stafford", "kansas", "stafford-county", "county"),
    ("ma-worcester", "massachusetts", "worcester-county", "county"),
    ("mi-arenac", "michigan", "arenac-county", "county"),
    ("mi-ogemaw", "michigan", "ogemaw-county", "county"),
    ("mi-oscoda", "michigan", "oscoda-county", "county"),
    ("mn-st-louis", "minnesota", "st-louis-county", "county"),
    ("mn-cook", "minnesota", "cook-county", "county"),
    ("mn-meeker", "minnesota", "meeker-county", "county"),
    ("mn-koochiching", "minnesota", "koochiching-county", "county"),
    ("mn-brown", "minnesota", "brown-county", "county"),
    ("mn-redwood", "minnesota", "redwood-county", "county"),
    ("mn-chippewa", "minnesota", "chippewa-county", "county"),
    ("mn-yellow-medicine", "minnesota", "yellow-medicine-county", "county"),
    ("mn-lac-qui-parle", "minnesota", "lac-qui-parle-county", "county"),
    ("mn-lake", "minnesota", "lake-county", "county"),
    ("mn-watonwan", "minnesota", "watonwan-county", "county"),
    ("mn-stearns", "minnesota", "stearns-county", "county"),
    ("mo-pike", "missouri", "pike-county", "county"),
    ("mo-lincoln", "missouri", "lincoln-county", "county"),
    ("mo-audrain", "missouri", "audrain-county", "county"),
    ("mo-montgomery", "missouri", "montgomery-county", "county"),
    ("mo-monroe", "missouri", "monroe-county", "county"),
    ("mo-boone", "missouri", "boone-county", "county"),
    ("mo-cooper", "missouri", "cooper-county", "county"),
    ("mo-linn", "missouri", "linn-county", "county"),
    ("mo-randolph", "missouri", "randolph-county", "county"),
    ("mo-andrew", "missouri", "andrew-county", "county"),
    ("mo-clay", "missouri", "clay-county", "county"),
    ("mo-daviess", "missouri", "daviess-county", "county"),
    ("mo-jackson", "missouri", "jackson-county", "county"),
    ("mo-saline", "missouri", "saline-county", "county"),
    ("mo-livingston", "missouri", "livingston-county", "county"),
    ("mo-oregon", "missouri", "oregon-county", "county"),
    ("ne-otoe", "nebraska", "otoe-county", "county"),
    ("ny-yates", "new-york", "yates-county", "county"),
    ("oh-allen", "ohio", "allen-county", "county"),
    ("oh-monroe", "ohio", "monroe-county", "county"),
    ("oh-paulding", "ohio", "paulding-county", "county"),
    ("oh-putnam", "ohio", "putnam-county", "county"),
    ("oh-van-wert", "ohio", "van-wert-county", "county"),
    ("ok-pottawatomie", "oklahoma", "pottawatomie-county", "county"),
    ("ok-carter", "oklahoma", "carter-county", "county"),
    ("tx-jim-wells", "texas", "jim-wells-county", "county"),
    ("tx-grayson", "texas", "grayson-county", "county"),
    ("tx-ellis", "texas", "ellis-county", "county"),
    ("tx-somervell", "texas", "somervell-county", "county"),
    ("tx-runnels", "texas", "runnels-county", "county"),
    ("ut-uintah", "utah", "uintah-county", "county"),
    ("ut-sevier", "utah", "sevier-county", "county"),
]

missing = []
for sid, state, slug, mtype in cherry_road_counties:
    if sid not in all_sids:
        missing.append({
            "site_id": sid,
            "state": state,
            "legacy_slug": slug,
            "type": mtype,
        })

print(f"Already in config: {len(cherry_road_counties) - len(missing)}")
print(f"Need to add: {len(missing)}")
print()
for m in sorted(missing, key=lambda x: x["site_id"]):
    print(f"  {m['site_id']:<25} {m['state']}/{m['legacy_slug']}")

if "--add" in sys.argv:
    markets.extend(missing)
    with open(MARKETS_PATH, "w", encoding="utf-8") as f:
        json.dump(markets, f, indent=2, ensure_ascii=False)
    print(f"\nAdded {len(missing)} markets to {MARKETS_PATH}")
    print(f"Total markets now: {len(markets)}")
