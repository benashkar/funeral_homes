"""Builds Legacy.com listing URLs from market configuration dicts."""

# Legacy.com URL patterns (2026)
# County:      https://www.legacy.com/us/obituaries/local/{state}/{county-slug}
# City:        https://www.legacy.com/us/obituaries/local/{state}/{city-slug}
# Publication: https://www.legacy.com/us/obituaries/{publication-slug}
BASE_URL = "https://www.legacy.com/us/obituaries/local"
PUBLICATION_BASE_URL = "https://www.legacy.com/us/obituaries"


def build_listing_url(market):
    """Build the Legacy.com obituary listing URL for a market.

    Markets with a publication_slug use the publication-based URL, which
    lists obits by newspaper name (e.g. claxtonenterprise) instead of by
    geographic location. Some small-market newspapers only publish obits
    under their publication page, not the geographic listing.

    Args:
        market: Dict with keys 'state', 'legacy_slug', and optionally
                'publication_slug'.

    Returns:
        Full listing URL string.
    """
    pub_slug = market.get("publication_slug")
    if pub_slug:
        return f"{PUBLICATION_BASE_URL}/{pub_slug}"
    state = market["state"]
    slug = market["legacy_slug"]
    return f"{BASE_URL}/{state}/{slug}"
