"""Stateless parsing functions for individual Legacy.com obituary pages.

Legacy.com detail pages are JS-rendered, so CSS selectors on the raw HTML
return nothing. Instead, all structured data is in JSON-LD script blocks:
  - @type "NewsArticle" → articleBody, datePublished
  - @type "Person" → name, deathDate
  - Funeral home name in the BreadcrumbList (funeral-homes path segment)

Each function takes a BeautifulSoup object and returns structured data.
Missing fields return None, never raise.
"""

import json
import re
from datetime import datetime

from utils.logger import get_logger

logger = get_logger(__name__)

# Pattern for headline fallback: "Name Obituary YYYY - Funeral Home"
# or "Name YYYY - Funeral Home" (no "Obituary" keyword)
_HEADLINE_SUFFIX_RE = re.compile(
    r'\s*(?:Obituary\s*)?(\d{4})?\s*-\s*.+$'
)


def _extract_jsonld(soup):
    """Extract all JSON-LD blocks from the page, keyed by @type.

    Returns:
        dict mapping @type string to the parsed JSON dict.
    """
    result = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "@type" in data:
            result[data["@type"]] = data
    return result


def parse_name(soup):
    """Extract the deceased's name from JSON-LD Person block.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    blocks = _extract_jsonld(soup)

    # Primary: Person schema has the canonical name
    person = blocks.get("Person")
    if person:
        name = person.get("name") or ""
        if name:
            return name.strip()

    # Fallback: NewsArticle headline — may contain year + funeral home
    # e.g. "Leon G Kober Obituary 2026 - Phillip Funeral Home"
    article = blocks.get("NewsArticle")
    if article:
        headline = article.get("headline") or ""
        if headline:
            # Strip " Obituary YYYY - Funeral Home" or " YYYY - Funeral Home"
            cleaned = _HEADLINE_SUFFIX_RE.sub("", headline).strip()
            # Also strip trailing " Obituary" if it wasn't caught above
            cleaned = re.sub(r'\s+Obituary$', '', cleaned).strip()
            if cleaned:
                return cleaned

    logger.warning("Could not find name in JSON-LD")
    return None


def parse_dates(soup):
    """Extract published and death dates from JSON-LD.

    - Death date comes from Person.deathDate (e.g. "2026-3-21")
    - Published date comes from NewsArticle.datePublished (ISO 8601)

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        dict with keys 'published' and 'death', values are date or None.
    """
    blocks = _extract_jsonld(soup)
    result = {"published": None, "death": None}

    # Death date from Person schema
    person = blocks.get("Person")
    if person:
        death_str = person.get("deathDate") or ""
        if death_str:
            try:
                # Format can be "2026-3-21" (no zero-padding)
                parts = death_str.split("-")
                if len(parts) == 3:
                    result["death"] = datetime(
                        int(parts[0]), int(parts[1]), int(parts[2])
                    ).date()
            except (ValueError, IndexError):
                logger.warning("Could not parse deathDate: %s", death_str)

    # Published date from NewsArticle schema
    article = blocks.get("NewsArticle")
    if article:
        pub_str = article.get("datePublished") or ""
        if pub_str:
            try:
                result["published"] = datetime.fromisoformat(
                    pub_str.replace("Z", "+00:00")
                ).date()
            except (ValueError, TypeError):
                pass

    return result


_FH_URL_RE = re.compile(r'/funeral-homes/([^/]+)/([^/]+)/[^/]+/(fh-\d+)')


def parse_funeral_home_detail(soup):
    """Extract structured funeral home data from JSON-LD BreadcrumbList.

    Parses the BreadcrumbList URL pattern:
      /funeral-homes/{state}/{city}/{slug}/fh-{ID}

    Returns:
        dict with keys: legacy_fh_id, name, city, state, legacy_url.
        All values are None if no FH data found.
    """
    result = {"legacy_fh_id": None, "name": None, "city": None, "state": None, "legacy_url": None}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "BreadcrumbList":
            continue
        for item in data.get("itemListElement", []):
            item_data = item.get("item", {})
            item_id = item_data.get("@id") or ""
            if "/funeral-homes/" in item_id and "/fh-" in item_id:
                result["name"] = (item_data.get("name") or "").strip() or None
                result["legacy_url"] = item_id
                match = _FH_URL_RE.search(item_id)
                if match:
                    result["state"] = match.group(1).replace("-", " ").title()
                    result["city"] = match.group(2).replace("-", " ").title()
                    result["legacy_fh_id"] = match.group(3)
                break

    return result


def parse_funeral_home(soup):
    """Extract the funeral home name from JSON-LD BreadcrumbList.

    Legacy.com embeds a BreadcrumbList with a path through funeral-homes.
    The last item in that list is the funeral home name.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "BreadcrumbList":
            continue
        items = data.get("itemListElement", [])
        # The specific funeral home entry has /fh-XXXXX in the URL
        # (not /listing/ which is the category, not /obituaries/ which is the obit itself)
        for item in items:
            item_data = item.get("item", {})
            item_id = item_data.get("@id") or ""
            if "/funeral-homes/" in item_id and "/fh-" in item_id:
                name = item_data.get("name") or ""
                if name:
                    return name.strip()

    # Fallback: extract from headline "Name YYYY - Funeral Home"
    blocks = _extract_jsonld(soup)
    article = blocks.get("NewsArticle")
    if article:
        headline = article.get("headline") or ""
        match = re.search(r'\d{4}\s*-\s*(.+)$', headline)
        if match:
            return match.group(1).strip()

    return None


def parse_obit_text(soup):
    """Extract the full obituary text from JSON-LD NewsArticle.articleBody.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    blocks = _extract_jsonld(soup)
    article = blocks.get("NewsArticle")
    if article:
        body = article.get("articleBody") or ""
        if body:
            return body.strip()

    logger.warning("Could not find obituary text in JSON-LD")
    return None


def parse_photo_url(soup):
    """Extract the deceased's photo URL from JSON-LD Person block.

    Legacy.com hosts photos on cache.legacy.net CDN. We store the URL,
    not the image itself.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str (URL) or None.
    """
    blocks = _extract_jsonld(soup)
    person = blocks.get("Person")
    if person:
        image = person.get("image") or ""
        if image:
            return image.strip()
    return None


_OG_TITLE_RE = re.compile(
    r'Obituary\s*\(\d{4}\)\s*-\s*([^,]+),\s*([A-Z]{2})\b'
)


def parse_death_place(soup):
    """Extract death city and state from JSON-LD Person.deathPlace.

    Fallback chain:
      1. Person.deathPlace.address (structured data)
      2. og:title meta tag: "Name Obituary (YYYY) - City, ST - Funeral Home"

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        dict with keys 'city' and 'state', values are str or None.
    """
    result = {"city": None, "state": None}

    # Primary: Person.deathPlace
    blocks = _extract_jsonld(soup)
    person = blocks.get("Person")
    if person:
        death_place = person.get("deathPlace") or {}
        address = death_place.get("address") or {}
        city = address.get("addressLocality") or ""
        state = address.get("addressRegion") or ""
        if city:
            result["city"] = city.strip()
        if state:
            result["state"] = state.strip()

    if result["city"]:
        return result

    # Fallback: og:title "Name Obituary (2026) - City, ST - Funeral Home"
    og = soup.find("meta", property="og:title")
    if og:
        content = og.get("content") or ""
        match = _OG_TITLE_RE.search(content)
        if match:
            result["city"] = match.group(1).strip()
            result["state"] = match.group(2).strip()

    return result


_STREET_TYPES = (
    "St", "Ave", "Blvd", "Dr", "Rd", "Ln", "Ct", "Pl", "Way", "Cir",
    "Ter", "Pkwy", "Hwy", "Loop", "Trl", "Run", "Pass", "Pike", "Sq",
    "Street", "Avenue", "Boulevard", "Drive", "Road", "Lane", "Court",
    "Place", "Circle", "Terrace", "Parkway", "Highway", "Trail", "Square",
)
_STREET_TYPE_RE = re.compile(
    r'^(\d+[-\w]*)\s+'           # street number (e.g. 123, 123-A)
    r'([NSEW]{1,2}\b\.?\s+)?'    # optional direction (N, SW, etc.)
    r'(.+?)\s+'                   # street name (greedy minimal)
    r'(' + '|'.join(_STREET_TYPES) + r')\.?\s*$',
    re.IGNORECASE,
)


def parse_street_address(address_str):
    """Parse a US street address into granular components.

    Args:
        address_str: Full street address like "123 N Main St".

    Returns:
        dict with keys: street_number, street_direction, street_name, street_type.
        All values may be None if parsing fails.
    """
    result = {"street_number": None, "street_direction": None, "street_name": None, "street_type": None}
    if not address_str:
        return result
    match = _STREET_TYPE_RE.match(address_str.strip())
    if match:
        result["street_number"] = match.group(1)
        direction = (match.group(2) or "").strip().rstrip(".")
        result["street_direction"] = direction if direction else None
        result["street_name"] = match.group(3).strip()
        result["street_type"] = match.group(4).strip()
    return result


def parse_fh_detail_page(html):
    """Extract address and coordinates from a Legacy.com funeral home page.

    Legacy.com FH pages embed FuneralHome/LocalBusiness JSON-LD with
    PostalAddress and GeoCoordinates.

    Args:
        html: Raw HTML string of the funeral home detail page.

    Returns:
        dict with keys: address, city, state, zip, lat, lon (all nullable).
    """
    from bs4 import BeautifulSoup as _BS
    result = {"address": None, "city": None, "state": None, "zip": None, "lat": None, "lon": None}
    soup = _BS(html, "lxml")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        dtype = (data.get("@type") or "").lower()
        if dtype not in ("funeralhome", "localbusiness", "organization"):
            continue

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
