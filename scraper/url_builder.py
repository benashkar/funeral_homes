"""Builds Legacy.com listing URLs from market configuration dicts."""

# Legacy.com URL patterns (2026)
# County: https://www.legacy.com/us/obituaries/local/{state}/{county-slug}
# City:   https://www.legacy.com/us/obituaries/local/{state}/{city-slug}
BASE_URL = "https://www.legacy.com/us/obituaries/local"


def build_listing_url(market):
    """Build the Legacy.com obituary listing URL for a market.

    Args:
        market: Dict with keys 'state' and 'legacy_slug'.

    Returns:
        Full listing URL string.
    """
    state = market["state"]
    slug = market["legacy_slug"]
    return f"{BASE_URL}/{state}/{slug}"
