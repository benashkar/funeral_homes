"""Stateless parsing functions for individual Legacy.com obituary pages.

Each function takes a BeautifulSoup object of a single obit page and
returns structured data. Missing fields return None, never raise.
"""

import re
from datetime import datetime

from utils.logger import get_logger

logger = get_logger(__name__)

# --- CSS selectors for Legacy.com obit detail pages (2026) ---
# These target the structured data in the obit detail view.
# If Legacy changes their HTML, update these selectors first.
NAME_SELECTOR = "h1.obit-name, h1[data-component='ObituaryName']"
DATES_CONTAINER_SELECTOR = "p.obit-dates, span[data-component='ObituaryDates']"
FUNERAL_HOME_SELECTOR = "a[data-component='FuneralHomeName'], div.fh-name a, span.funeral-home-name"
OBIT_TEXT_SELECTOR = "div.obit-text, div[data-component='ObituaryText'], section.obituary-text"

# Date patterns commonly seen in Legacy.com date strings
# e.g. "January 15, 2026" or "Jan 15, 2026"
DATE_PATTERN = re.compile(
    r"([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE
)

DATE_FORMATS = [
    "%B %d, %Y",   # January 15, 2026
    "%B %d %Y",    # January 15 2026
    "%b %d, %Y",   # Jan 15, 2026
    "%b. %d, %Y",  # Jan. 15, 2026
]


def _parse_date_string(text):
    """Try to parse a date string using known Legacy.com formats.

    Returns:
        datetime.date or None.
    """
    if not text:
        return None
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_name(soup):
    """Extract the deceased's name from the obit page.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    # Try the primary selectors for the name heading
    el = soup.select_one(NAME_SELECTOR)
    if el:
        return el.get_text(strip=True)
    logger.warning("Could not find name element on page")
    return None


def parse_dates(soup):
    """Extract published and death dates from the obit page.

    Legacy.com typically shows dates as a range like:
    "January 5, 1945 - March 1, 2026"
    or just a published date near the byline.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        dict with keys 'published' and 'death', values are date or None.
    """
    result = {"published": None, "death": None}

    # Look for the dates container (birth-death range)
    el = soup.select_one(DATES_CONTAINER_SELECTOR)
    if el:
        text = el.get_text(strip=True)
        matches = DATE_PATTERN.findall(text)
        if len(matches) >= 2:
            # First date is birth/earlier, second is death
            result["death"] = _parse_date_string(matches[-1])
        elif len(matches) == 1:
            result["death"] = _parse_date_string(matches[0])

    # Look for a published/posted date in meta tags
    # Legacy often uses <meta property="article:published_time">
    meta = soup.find("meta", property="article:published_time")
    if meta and meta.get("content"):
        try:
            result["published"] = datetime.fromisoformat(
                meta["content"].replace("Z", "+00:00")
            ).date()
        except (ValueError, TypeError):
            pass

    return result


def parse_funeral_home(soup):
    """Extract the funeral home name from the obit page.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    el = soup.select_one(FUNERAL_HOME_SELECTOR)
    if el:
        return el.get_text(strip=True)
    return None


def parse_obit_text(soup):
    """Extract the full obituary text, stripped of HTML tags.

    Args:
        soup: BeautifulSoup object of a single obituary page.

    Returns:
        str or None.
    """
    el = soup.select_one(OBIT_TEXT_SELECTOR)
    if el:
        # get_text with separator to preserve paragraph breaks
        text = el.get_text(separator="\n", strip=True)
        if text:
            return text
    logger.warning("Could not find obituary text on page")
    return None
