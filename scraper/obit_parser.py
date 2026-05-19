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
from datetime import date, datetime, timedelta

from utils.logger import get_logger

logger = get_logger(__name__)

# Pattern for headline fallback: "Name Obituary YYYY - Funeral Home"
# or "Name YYYY - Funeral Home" (no "Obituary" keyword)
_HEADLINE_SUFFIX_RE = re.compile(
    r'\s*(?:Obituary\s*)?(\d{4})?\s*-\s*.+$'
)

# Universal trailing-junk cleanup: strip from the first 19YY/20YY token onward,
# whether or not a "Obituary" keyword or " - Funeral Home" tail follows it.
# A real person name never contains a 4-digit year, so the year marks the
# boundary between the actual name and any over-grabbed pollution.
# Caught patterns:
#   "Margaret Janet Morss Herren Obituary 2026"           → "Margaret Janet Morss Herren"
#   "Glenn Curran McCabe 2026 - Brantley Phillips FH"     → "Glenn Curran McCabe"
#   "Fred \"Butch\" Paxton, Jr. Obituary 2026 - Mem. Gard." → "Fred \"Butch\" Paxton, Jr."
_NAME_TRAILING_JUNK_RE = re.compile(
    r"\s+(?:Obituary\s+)?(?:19|20)\d{2}\b.*$",
    re.IGNORECASE,
)
_TRAILING_OBITUARY_RE = re.compile(r"\s+Obituary\s*$", re.IGNORECASE)


def _clean_extracted_name(name):
    """Strip year + funeral-home pollution from a name string.

    Applied to every name we extract — JSON-LD Person.name as well as
    NewsArticle.headline — so the same junk pattern can't sneak in via
    whichever code path Legacy.com happens to populate today.

    Args:
        name: Raw name string (or None).

    Returns:
        Cleaned name with whitespace trimmed. Empty string for None/empty.
    """
    if not name:
        return ""
    cleaned = _NAME_TRAILING_JUNK_RE.sub("", name)
    cleaned = _TRAILING_OBITUARY_RE.sub("", cleaned)
    return cleaned.strip()


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


def _creative_work_person(blocks):
    """Return the Person object nested under CreativeWork.about, or None.

    Next.js /person/ pages (2026 H2+) wrap the Person schema inside a
    CreativeWork.about field rather than emitting a top-level Person block.
    """
    cw = blocks.get("CreativeWork")
    if not cw:
        return None
    about = cw.get("about")
    if isinstance(about, dict) and about.get("@type") == "Person":
        return about
    return None


def parse_name(soup):
    """Extract the deceased's name from JSON-LD.

    Source order:
      1. Person schema (legacy detail pages)
      2. CreativeWork.about Person (new /person/ pages)
      3. NewsArticle.headline with " YYYY - Funeral Home" cleanup

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    blocks = _extract_jsonld(soup)

    # Primary: Person schema has the canonical name. Legacy.com sometimes
    # writes the headline format ("Name Obituary 2026") into Person.name,
    # so apply the universal cleaner before returning.
    person = blocks.get("Person") or _creative_work_person(blocks)
    if person:
        name = _clean_extracted_name(person.get("name") or "")
        if name:
            return name

    # Fallback: NewsArticle headline — may contain year + funeral home
    # e.g. "Leon G Kober Obituary 2026 - Phillip Funeral Home"
    article = blocks.get("NewsArticle")
    if article:
        headline = article.get("headline") or ""
        if headline:
            # Step 1: strip " Obituary YYYY - Funeral Home" / " YYYY - FH"
            cleaned = _HEADLINE_SUFFIX_RE.sub("", headline).strip()
            # Step 2: universal trailing-junk cleanup catches what HEADLINE_SUFFIX
            # missed (no dash, e.g. "Name Obituary 2026" or just "Name 2026").
            cleaned = _clean_extracted_name(cleaned)
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

    # Death date from Person schema (legacy detail pages OR
    # CreativeWork.about on new /person/ pages).
    person = blocks.get("Person") or _creative_work_person(blocks)
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


# --- Death date extraction from obituary text ---

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = "|".join(_MONTH_NAMES.keys())

# "February 28, 2026" / "Feb. 28, 2026" / "February 28th, 2026"
_DATE_WRITTEN_RE = re.compile(
    r'(' + _MONTH_ALT + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
    re.IGNORECASE,
)

# "28 February 2026" (European order)
_DATE_EURO_RE = re.compile(
    r'(\d{1,2})(?:st|nd|rd|th)?\s+(' + _MONTH_ALT + r')\.?,?\s+(\d{4})',
    re.IGNORECASE,
)

# "2/28/2026" or "02-28-2026"
_DATE_NUMERIC_RE = re.compile(
    r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})'
)

# Two dates separated by dash or tilde (birth - death)
_DATE_RANGE_RE = re.compile(
    r'(?:'
    # Written dates: "Month DD, YYYY - Month DD, YYYY"
    r'(?:' + _MONTH_ALT + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}'
    r'|'
    # Numeric dates: "MM/DD/YYYY"
    r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}'
    r')'
    r'\s*[\-\u2013\u2014~]\s*'  # dash, en-dash, em-dash, or tilde
    r'('
    # Second date (captured) — written
    r'(?:' + _MONTH_ALT + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}'
    r'|'
    # Second date (captured) — numeric
    r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}'
    r')',
    re.IGNORECASE,
)

# Death phrases followed by a written date
_DEATH_PHRASE_RE = re.compile(
    r'(?:passed\s+away|died|departed(?:\s+this\s+life)?|'
    r'went\s+to\s+be\s+with\s+(?:the\s+)?(?:lord|god|his\s+lord|her\s+lord)|'
    r'entered\s+(?:eternal|into\s+eternal)\s+rest|'
    r'went\s+(?:home\s+)?to\s+(?:be\s+with\s+)?(?:the\s+)?lord|'
    r'was\s+called\s+home)'
    r'[^.]*?'  # optional words like "peacefully", "suddenly", "on"
    r'(?:on\s+)?'
    r'(' + _MONTH_ALT + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})',
    re.IGNORECASE,
)

# Death phrase with numeric date
_DEATH_PHRASE_NUMERIC_RE = re.compile(
    r'(?:passed\s+away|died|departed(?:\s+this\s+life)?|'
    r'went\s+to\s+be\s+with\s+(?:the\s+)?(?:lord|god)|'
    r'entered\s+(?:eternal|into\s+eternal)\s+rest|'
    r'was\s+called\s+home)'
    r'[^.]*?'
    r'(?:on\s+)?'
    r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})',
    re.IGNORECASE,
)


def _parse_date_str(date_str):
    """Parse a date string into a datetime.date, or None.

    Handles written ("February 28, 2026") and numeric ("2/28/2026") formats.
    """
    if not date_str:
        return None

    # Try written format: "Month DD, YYYY"
    m = _DATE_WRITTEN_RE.search(date_str)
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower().rstrip("."))
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass

    # Try European format: "28 February 2026"
    m = _DATE_EURO_RE.search(date_str)
    if m:
        month = _MONTH_NAMES.get(m.group(2).lower().rstrip("."))
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                pass

    # Try numeric format: "2/28/2026"
    m = _DATE_NUMERIC_RE.search(date_str)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    return None


def _validate_death_date(d, published_date=None):
    """Check whether a parsed date is a plausible death date."""
    if d is None:
        return False
    today = date.today()
    if d > today:
        return False
    if d.year < 1900:
        return False
    if published_date:
        # Death should be within 30 days before published, or at most 3 days after
        if d > published_date + timedelta(days=3):
            return False
        if d < published_date - timedelta(days=30):
            return False
    return True


def parse_death_date_from_text(obit_text, published_date=None):
    """Extract death date from obituary text.

    Searches the first 500 characters using a regex cascade:
      1. Date range (birth - death): take the second date
      2. Death phrase ("passed away on", "died on", etc.) + date
      3. First plausible date in text (lowest confidence fallback)

    Args:
        obit_text: Full obituary body text.
        published_date: Optional date for validation upper bound.

    Returns:
        datetime.date or None.
    """
    if not obit_text:
        return None

    snippet = obit_text[:500]

    # Priority 1: Date range (birth - death) — take the second date
    m = _DATE_RANGE_RE.search(snippet)
    if m:
        d = _parse_date_str(m.group(1))
        if _validate_death_date(d, published_date):
            return d

    # Priority 2: Death phrase with written date
    m = _DEATH_PHRASE_RE.search(snippet)
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower().rstrip("."))
        if month:
            try:
                d = date(int(m.group(3)), month, int(m.group(2)))
                if _validate_death_date(d, published_date):
                    return d
            except ValueError:
                pass

    # Priority 2b: Death phrase with numeric date
    m = _DEATH_PHRASE_NUMERIC_RE.search(snippet)
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            if _validate_death_date(d, published_date):
                return d
        except ValueError:
            pass

    # Priority 3: First plausible written date in snippet
    for m in _DATE_WRITTEN_RE.finditer(snippet):
        month = _MONTH_NAMES.get(m.group(1).lower().rstrip("."))
        if month:
            try:
                d = date(int(m.group(3)), month, int(m.group(2)))
                if _validate_death_date(d, published_date):
                    return d
            except ValueError:
                continue

    # Priority 3b: First plausible numeric date in snippet
    for m in _DATE_NUMERIC_RE.finditer(snippet):
        try:
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            if _validate_death_date(d, published_date):
                return d
        except ValueError:
            continue

    return None


# --- Funeral home extraction from obituary text ---

_FH_KEYWORDS = (
    r"Funeral\s+Home",
    r"Funeral\s+Chapel",
    r"Funeral\s+Services?",
    r"Mortuary",
    r"Memorial\s+Chapel",
    r"Memorial\s+Funeral",
    r"Cremation\s+Society",
    r"Crematory",
    r"Cremation\s+Services?",
)
_FH_KEYWORD_ALT = "|".join(_FH_KEYWORDS)

# Context phrase + keyword anchor (highest confidence)
_FH_CONTEXT_RE = re.compile(
    r'(?:arrangements?\s+(?:entrusted\s+to|(?:are\s+)?(?:by|under\s+the\s+direction\s+of|in\s+(?:the\s+)?care\s+of))|'
    r'(?:services?\s+(?:entrusted\s+to|provided\s+by|will\s+be\s+(?:held\s+at|at)))|'
    r'in\s+(?:the\s+)?care\s+of|'
    r'under\s+the\s+direction\s+of|'
    r'(?:visitation|viewing)\s+(?:will\s+be\s+)?at)'
    r'\s+'
    r'((?:[A-Za-z][A-Za-z\'\-]+\s+){0,5}'
    r'(?:' + _FH_KEYWORD_ALT + r'))',
    re.IGNORECASE,
)

# Keyword anchor alone — [1-5 Title-Case words] + keyword
# No IGNORECASE: name words must start uppercase (proper nouns) to avoid
# grabbing common words like "services by" as part of the name.
_FH_KEYWORD_RE = re.compile(
    r'((?:[A-Z][A-Za-z\'\-]+\s+){1,5}'
    r'(?:' + _FH_KEYWORD_ALT + r'))',
)

# Exclusion: memorial contributions / donations context
_FH_EXCLUSION_RE = re.compile(
    r'(?:memorial\s+contributions?\s+(?:may\s+be\s+)?(?:sent|made|directed)|'
    r'donations?\s+(?:may\s+be\s+)?(?:sent|made|directed)|'
    r'in\s+lieu\s+of\s+flowers)',
    re.IGNORECASE,
)


def _validate_fh_name(name):
    """Check if a candidate funeral home name is plausible."""
    if not name or len(name) < 5:
        return False
    words = name.split()
    if len(words) < 2:
        return False
    return True


def _clean_fh_name(name):
    """Normalize whitespace and punctuation in a funeral home name."""
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.rstrip('.,;:')
    return name


def parse_funeral_home_from_text(obit_text):
    """Extract funeral home name from obituary body text.

    Searches the FULL text (funeral home mentions are often near the end).
    Uses a regex cascade:
      1. Context phrase + keyword anchor (highest confidence)
      2. Keyword anchor alone with name prefix

    Args:
        obit_text: Full obituary body text.

    Returns:
        str or None.
    """
    if not obit_text:
        return None

    # Priority 1: Context phrase + keyword
    for m in _FH_CONTEXT_RE.finditer(obit_text):
        candidate = m.group(1).strip()
        # Check same sentence for exclusion (back to last period/newline)
        sent_start = max(obit_text.rfind(".", 0, m.start()), obit_text.rfind("\n", 0, m.start())) + 1
        preceding_sent = obit_text[sent_start:m.start()]
        if _FH_EXCLUSION_RE.search(preceding_sent):
            continue
        if _validate_fh_name(candidate):
            return _clean_fh_name(candidate)

    # Priority 2: Keyword anchor alone
    for m in _FH_KEYWORD_RE.finditer(obit_text):
        candidate = m.group(1).strip()
        sent_start = max(obit_text.rfind(".", 0, m.start()), obit_text.rfind("\n", 0, m.start())) + 1
        preceding_sent = obit_text[sent_start:m.start()]
        if _FH_EXCLUSION_RE.search(preceding_sent):
            continue
        if _validate_fh_name(candidate):
            return _clean_fh_name(candidate)

    return None
