"""Discover newspaper website URLs for Cherry Road papers.

For each entry in cherry_road_markets.json without a `newspaper_url`
field, tries common URL patterns for the paper's likely domain. Reports
which patterns return 200s with obit content.

Run: python scripts/discover_newspaper_urls.py [--update]
  --update: Write discovered URLs back to cherry_road_markets.json
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.rate_limiter import create_session, polite_get
from utils.logger import get_logger

logger = get_logger("discover_urls")

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "cherry_road_markets.json",
)

# URL path suffixes to try (in order of preference)
PATH_PATTERNS = [
    "/obituaries",
    "/community/obituaries",
    "/news/obituaries",
    "/obits",
    "/lifestyles/obituaries",
]


def slugify_newspaper(name):
    """Generate candidate domain slugs from newspaper name.

    Examples:
        'Claxton Enterprise' -> ['claxtonenterprise', 'theclaxtonenterprise']
        'The Hays Daily News' -> ['haysdailynews', 'thehaysdailynews']
    """
    clean = name.strip()
    no_the = re.sub(r"^The\s+", "", clean, flags=re.IGNORECASE)
    slugs = []

    # 1. Lowercase, alphanumeric only, no spaces
    s1 = re.sub(r"[^a-z0-9]", "", no_the.lower())
    if s1:
        slugs.append(s1)

    # 2. With "the" prefix
    if no_the != clean:
        s2 = re.sub(r"[^a-z0-9]", "", clean.lower())
        if s2 and s2 not in slugs:
            slugs.append(s2)

    # 3. Hyphenated
    s3 = re.sub(r"[^a-z0-9-]", "", no_the.lower().replace(" ", "-")).strip("-")
    if s3 and s3 not in slugs:
        slugs.append(s3)

    return slugs


def probe_newspaper(session, name):
    """Try common URL patterns for a newspaper. Return first working URL or None."""
    slugs = slugify_newspaper(name)
    for slug in slugs:
        for path in PATH_PATTERNS:
            url = f"https://www.{slug}.com{path}"
            resp = polite_get(session, url)
            if resp and resp.status_code == 200:
                # Check the response actually contains obit-like content
                text_lower = resp.text[:50000].lower()
                if "obituar" in text_lower and len(resp.text) > 5000:
                    logger.info("[OK] %s -> %s", name, url)
                    return url
            time.sleep(0.5)
    logger.info("[--] %s -> no URL found", name)
    return None


def main():
    update = "--update" in sys.argv

    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)

    # Dedupe by newspaper name (some appear multiple times for different cities)
    seen_papers = {}
    for entry in manifest:
        name = entry["newspaper"]
        if name not in seen_papers:
            seen_papers[name] = entry.get("newspaper_url")

    session = create_session()
    discovered = {}

    for name, existing in seen_papers.items():
        if existing:
            logger.info("[skip] %s already has URL: %s", name, existing)
            continue
        url = probe_newspaper(session, name)
        if url:
            discovered[name] = url

    print()
    print(f"Discovered {len(discovered)} new newspaper URLs:")
    for name, url in discovered.items():
        print(f"  {name:<40} {url}")

    if update and discovered:
        for entry in manifest:
            if entry["newspaper"] in discovered and not entry.get("newspaper_url"):
                entry["newspaper_url"] = discovered[entry["newspaper"]]
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"\nUpdated {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
