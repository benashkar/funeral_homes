"""Check which Cherry Road newspapers have valid Legacy.com publication pages."""

import re
import requests
import time

NEWSPAPERS = [
    ("Clayton Record", "Clayton", "AL"),
    ("Areawide News", "Salem", "AR"),
    ("Clay County Times-Democrat", "Piggott", "AR"),
    ("Marshall Mountain Wave", "Marshall", "AR"),
    ("Pocahontas Star Herald", "Pocahontas", "AR"),
    ("Clay County Courier", "Corning", "AR"),
    ("Bent County Democrat", "Las Animas", "CO"),
    ("La Junta Tribune-Democrat", "La Junta", "CO"),
    ("The Star News", "McCall", "ID"),
    ("Cass County Star-Gazette", "Virginia", "IL"),
    ("Register Publications", "Lawrenceburg", "IN"),
    ("Tipton County Tribune", "Tipton", "IN"),
    ("The Call-Leader", "Elwood", "IN"),
    ("The Alexandria Times-Tribune", "Alexandria", "IN"),
    ("The Hamburg Reporter", "Hamburg", "IA"),
    ("The Lindsborg News-Record", "Lindsbourg", "KS"),
    ("Tri-County Tribune", "Pratt", "KS"),
    ("The Miami County Republic", "Paola", "KS"),
    ("Hiawatha World", "Hiawatha", "KS"),
    ("Green Acres Publications", "", "KS"),
    ("Atchison Globe", "Atchison", "KS"),
    ("Butler County Times-Gazette", "El Dorado", "KS"),
    ("McPherson Sentinel", "McPherson", "KS"),
    ("Leavenworth Times", "Leavenworth", "KS"),
    ("Dodge City Daily Globe", "Dodge City", "KS"),
    ("The Newton Kansan", "Newton", "KS"),
    ("The Garden City Telegram", "Garden City", "KS"),
    ("The Hays Daily News", "Hays", "KS"),
    ("Ottawa Herald", "Ottawa", "KS"),
    ("The Wellington Daily News", "Wellington", "KS"),
    ("The Clinton Item", "Clinton", "MA"),
    ("The Grafton News", "Grafton", "MA"),
    ("The Landmark", "Holden", "MA"),
    ("The Millbury-Sutton Chronicle", "Millbury", "MA"),
    ("The Arenac County Independent", "Standish", "MI"),
    ("The Ogemaw County Herald", "West Branch", "MI"),
    ("The Oscoda County Herald", "Mio", "MI"),
    ("The Tower News", "Tower", "MN"),
    ("The Cook County News Herald", "Grand Marais", "MN"),
    ("The Hutchinson Station", "Hutchinson", "MN"),
    ("The Litchfield Rail", "Litchfield", "MN"),
    ("The Rainy Lake Gazette", "International Falls", "MN"),
    ("The Sleepy Eye Herald-Dispatch", "Sleepy Eye", "MN"),
    ("The Redwood Falls Gazette", "Redwood Falls", "MN"),
    ("The Granite Falls Advocate-Tribune", "Granite Falls", "MN"),
    ("Montevideo American News", "Montevideo", "MN"),
    ("Lake County Press", "Two Harbors", "MN"),
    ("St. James Plaindealer", "St. James", "MN"),
    ("Tri-County News", "Eden Valley", "MN"),
    ("Pike County News", "Bowling Green", "MO"),
    ("The Elsberry Democrat", "Elsberry", "MO"),
    ("The Hermann Advertiser-Courier", "Hermann", "MO"),
    ("The Lake Gazette", "Monroe City", "MO"),
    ("Centralia Fireside Guard", "Centralia", "MO"),
    ("The Vandalia Leader", "Vandalia", "MO"),
    ("The Lincoln County Journal", "Troy", "MO"),
    ("Troy Free Press", "Troy", "MO"),
    ("Savannah Reporter", "Savannah", "MO"),
    ("Moberly Monitor-Index", "Moberly", "MO"),
    ("Gladstone Dispatch", "Gladstone", "MO"),
    ("Courier-Tribune", "Liberty", "MO"),
    ("South Missourian News", "Alton", "MO"),
    ("The Marshall Democrat-News", "Marshall", "MO"),
    ("The Examiner", "Independence", "MO"),
    ("Constitution-Tribune", "Chillicothe", "MO"),
    ("Boonville Daily-News", "Boonville", "MO"),
    ("Linn County Leader", "Linneus", "MO"),
    ("Nebraska City News Press", "Nebraska City", "NE"),
    ("Syracuse Journal-Democrat", "Syracuse", "NE"),
    ("The Chronicle-News", "Trinidad", "NM"),
    ("The Chronicle-Express", "Penn Yan", "NY"),
    ("Southwest Messenger", "Madison Township", "OH"),
    ("Southeast Messenger", "Grove City", "OH"),
    ("Times Bulletin Media", "Van Wert", "OH"),
    ("Putnam County Sentinel", "Ottawa", "OH"),
    ("Paulding Progress", "Paulding", "OH"),
    ("The Delphos Herald", "Delphos", "OH"),
    ("Monroe County Beacon", "Woodsfield", "OH"),
    ("The Shawnee News-Star", "Shawnee", "OK"),
    ("The Ardmoreite", "Ardmore", "OK"),
    ("Alice Echo News-Journal", "Alice", "TX"),
    ("Brownwood Bulletin", "Brownwood", "TX"),
    ("Stephenville Empire-Tribune", "Stephenville", "TX"),
    ("Glen Rose Reporter", "Glen Rose", "TX"),
    ("Sherman Herald-Democrat", "Sherman", "TX"),
    ("Midlothian Mirror", "Midlothian", "TX"),
    ("Palo Pinto Press", "Mineral Wells", "TX"),
    ("Runnels County Register", "Ballinger", "TX"),
    ("Waxahachie Daily Light", "Waxahachie", "TX"),
    ("Vernal Express", "Vernal", "UT"),
    ("Unitah Basin Standard", "Duchesne", "UT"),
    ("The Richfield Reaper", "Richfield", "UT"),
]

BASE = "https://www.legacy.com/us/obituaries"


def make_slugs(name):
    """Generate candidate slugs from newspaper name."""
    clean = re.sub(r"^The\s+", "", name)
    slugs = []

    # Slug 1: all lowercase, remove all non-alpha (claxtonenterprise style)
    s1 = re.sub(r"[^a-z]", "", clean.lower())
    slugs.append(s1)

    # Slug 2: hyphenated (clay-county-times-democrat style)
    s2 = re.sub(r"[^a-z0-9\s-]", "", clean.lower())
    s2 = re.sub(r"\s+", "-", s2).strip("-")
    slugs.append(s2)

    # Slug 3: remove "the" and punctuation, keep hyphens
    s3 = re.sub(r"[^a-z-]", "", clean.lower())
    slugs.append(s3)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    hits = []
    misses = []

    for name, city, state in NEWSPAPERS:
        slugs = make_slugs(name)
        found = False

        for slug in slugs:
            url = f"{BASE}/{slug}"
            try:
                resp = session.get(url, timeout=10, allow_redirects=True)
                # Check for 200 + actual obituary content
                if resp.status_code == 200:
                    text_sample = resp.text[:80000].lower()
                    if "/name/" in text_sample or "obituar" in text_sample:
                        hits.append((name, city, state, slug))
                        print(f"[HIT]  {name:<45} {city}, {state}  ->  {slug}")
                        found = True
                        break
            except Exception as e:
                print(f"[ERR]  {name:<45} {slug}: {e}")
            time.sleep(0.4)

        if not found:
            misses.append((name, city, state, slugs))
            print(f"[MISS] {name:<45} {city}, {state}  tried: {slugs}")

        time.sleep(0.5)

    print()
    print(f"=== RESULTS: {len(hits)} hits, {len(misses)} misses ===")
    print()
    if hits:
        print("VALID PUBLICATION SLUGS:")
        for name, city, state, slug in hits:
            print(f"  {slug:<45} <- {name} ({city}, {state})")
    print()
    if misses:
        print("NO MATCH FOUND:")
        for name, city, state, slugs in misses:
            print(f"  {name:<45} {city}, {state}  (tried: {', '.join(slugs)})")


if __name__ == "__main__":
    main()
