"""Polite rate limiting and HTTP session for Legacy.com scraping.

Thread-safe: uses a global lock so concurrent workers share one rate limit.
Treats 403/429/503 as rate limiting (Legacy.com's response to too many
requests) and retries with exponential backoff up to MAX_RETRIES times.

Uses curl_cffi to impersonate Chrome 120's TLS fingerprint (JA3/JA4),
HTTP/2 framing, and header order — bypasses Legacy.com's bot detection
that targets the standard `requests` library's signature.
"""

import os
import random
import threading
import time

# curl_cffi is a drop-in replacement for `requests` that mimics real
# browser TLS fingerprints. Falls back to stdlib requests if unavailable.
try:
    from curl_cffi import requests as cffi_requests
    _USE_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    _USE_CURL_CFFI = False

import requests as _stdlib_requests

from utils.logger import get_logger

logger = get_logger(__name__)

# Rotating User-Agents to look less like a bot
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]
USER_AGENT = USER_AGENTS[0]  # Default for create_session

# Aggressive politeness settings — Legacy.com rate-limits Render IPs hard.
# Original 3s/120s/1-retry config caused 84% of listing fetches to fail.
MIN_DELAY = 6.0       # seconds between ANY two requests (global, direct mode)
MIN_DELAY_PROXY = 1.0 # rotating-gateway IPs change per request; no per-IP politeness needed
MAX_RETRIES = 4       # total attempts including first try
PROXY_MIN_RETRIES = 12 # measured per-IP CF-block rate is 0.74-0.88 on half the fleet (not 0.6); 8 attempts left 9-37% of bad-shard markets blocked. 12 cuts p=0.74 miss 9%->2.7%, p=0.82 20%->6.5%. Extra GB only spent on already-blocked markets.
INITIAL_BACKOFF = 60  # seconds to wait on first 403/429/503

# Residential proxy support — set PROXY_URL env var to route requests
# through a proxy service (Bright Data, SmartProxy, etc.).
# Example: PROXY_URL=http://user:pass@proxy.brightdata.com:22225
# When set, ALL requests go through the proxy. When unset, direct.
PROXY_URL = os.environ.get("PROXY_URL", "")

# Proxies dict passed EXPLICITLY to every request. Setting `session.proxies`
# as a bare attribute is a `requests` idiom that curl_cffi does NOT honor —
# doing only that silently sends traffic direct from the datacenter IP, which
# Cloudflare blocks on sight. Always pass this to session.get(proxies=...).
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Global rate limiter — ensures all threads share one delay
_lock = threading.Lock()
_last_request_time = 0.0
# Track consecutive 403s to increase delay dynamically
_consecutive_blocks = 0


def _rate_limit():
    """Block until the minimum delay has elapsed since the last request.

    Adds dynamic extra delay if we've been getting blocked recently. In proxy
    mode the per-request IP rotates, so we use a much smaller base delay.
    """
    global _last_request_time, _consecutive_blocks
    with _lock:
        now = time.monotonic()
        base = MIN_DELAY_PROXY if PROXY_URL else MIN_DELAY
        # Add extra delay proportional to recent blocks (up to ~30s extra in direct mode)
        block_penalty = min(_consecutive_blocks * 3, 30)
        if PROXY_URL:
            block_penalty = min(block_penalty, 3)  # cap proxy-mode penalty
        delay = base + block_penalty
        # Add small random jitter to avoid thundering-herd patterns
        delay += random.uniform(0, 1.0)
        elapsed = now - _last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        _last_request_time = time.monotonic()


# Cloudflare/anti-bot challenge markers that appear in the first few KB of the body.
_CHALLENGE_MARKERS = (
    "just a moment",       # Cloudflare 5s challenge title
    "cf-challenge",        # Cloudflare challenge platform
    "challenge-platform",  # Cloudflare challenge JS bundle path
    "/cdn-cgi/challenge",  # Cloudflare challenge endpoint
    "checking your browser",  # Cloudflare interstitial
    "attention required",  # Cloudflare block page
)


def _is_challenge_response(resp):
    """True if a 200 response is actually an anti-bot challenge page.

    Detects two variants the rate_limiter would otherwise treat as success:
    1) Cloudflare 200 challenge: body contains "Just a moment" / cf-challenge markers
    2) Silent 200 challenge: large body but no <title> tag anywhere — Legacy.com's
       fingerprint-block returns a sizable interstitial with no <title>.

    Real obit listing/detail pages always have a <title data-react-helmet> tag.
    """
    if resp.status_code != 200:
        return False
    body = resp.text or ""
    if len(body) < 1000:
        return False  # too small to be a meaningful response either way
    head = body[:5000].lower()
    if any(m in head for m in _CHALLENGE_MARKERS):
        return True
    # Silent challenge: >50KB body with no <title> tag is not a real Legacy page.
    if len(body) > 50_000 and "<title" not in body.lower():
        return True
    return False


def _record_success():
    """Reset consecutive block counter on success."""
    global _consecutive_blocks
    with _lock:
        _consecutive_blocks = 0


def _record_block():
    """Increment block counter and return current count."""
    global _consecutive_blocks
    with _lock:
        _consecutive_blocks = min(_consecutive_blocks + 1, 10)
        return _consecutive_blocks


def _make_stdlib_session():
    """A plain `requests` session with browser-like headers."""
    session = _stdlib_requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def create_session():
    """Return an HTTP session tuned for the current egress mode.

    Both modes now use curl_cffi Chrome-120 impersonation, because as of
    2026-06 Legacy.com's Cloudflare fingerprints the TLS/HTTP client itself:
    plain `requests` gets a hard 403 "Just a moment..." 100% of the time —
    direct AND through a residential proxy. Only an impersonated browser
    ClientHello passes.

    - PROXY mode (PROXY_URL set): curl_cffi impersonation + residential exit
      IP. Verified 2026-06-13 (curl_cffi 0.15.0 + 711proxy): 7/7 markets
      200 OK with full obituary data, incl. big counties. This REVERSES the
      old assumption that curl_cffi-through-an-HTTP-proxy is blocked — with
      curl_cffi 0.15.0 the impersonated ClientHello now survives the proxy
      CONNECT, and it is the ONLY path that gets past Legacy.com today.
      (Plain-requests-via-proxy now measures 0/8 — hard 403 + RST.)

    - DIRECT mode (no PROXY_URL): curl_cffi Chrome-120 impersonation. Works
      from a residential IP; a datacenter IP still gets blocked, so prod
      should always run with PROXY_URL set.

    If curl_cffi is unavailable we fall back to stdlib requests, but that
    path is expected to be ~100% blocked by Cloudflare now — install
    curl_cffi.
    """
    if PROXY_URL:
        if _USE_CURL_CFFI:
            session = cffi_requests.Session(impersonate="chrome120")
            session._impersonated = True
            proxy_host = PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL
            logger.info(
                "[OK] HTTP session: curl_cffi (Chrome 120) via proxy %s", proxy_host
            )
        else:
            session = _make_stdlib_session()
            session._impersonated = False
            logger.warning(
                "curl_cffi not installed — proxy session falling back to stdlib "
                "requests, which Cloudflare now blocks ~100%%. Install curl_cffi."
            )
        _proxy_self_test(session)
        return session

    if _USE_CURL_CFFI:
        session = cffi_requests.Session(impersonate="chrome120")
        session._impersonated = True
        logger.info("[OK] HTTP session using curl_cffi (Chrome 120 impersonation)")
    else:
        session = _make_stdlib_session()
        session._impersonated = False
        logger.warning("curl_cffi not installed — falling back to stdlib requests")

    return session


def _proxy_self_test(session):
    """Verify traffic actually exits through the proxy, not the datacenter IP.

    Fetches an IP-echo endpoint through the session. If the request goes
    direct (curl_cffi ignoring a misconfigured proxy), this is the only
    place we'd catch it before a full run silently 100%-blocks.
    """
    try:
        resp = session.get(
            "https://api.ipify.org", proxies=PROXIES, timeout=15
        )
        exit_ip = (resp.text or "").strip()
        if resp.status_code == 200 and exit_ip:
            logger.info("[OK] Proxy self-test passed — exit IP %s", exit_ip)
        else:
            logger.warning(
                "[WARN] Proxy self-test odd response: status=%s body=%r",
                resp.status_code, exit_ip[:80],
            )
    except Exception as e:
        logger.error("[ERR] Proxy self-test FAILED — traffic may be going "
                     "direct from the datacenter IP: %s", e)


def polite_get(session, url, max_retries=None):
    """GET a URL with global rate limiting and retry on rate-limit responses.

    Two modes:
    - Direct (PROXY_URL unset): exponential 60/120/240/480s backoff on
      403/429/503. Caller-provided max_retries respected (1 for non-priority
      to avoid 12h cron timeout; 4 for priority).
    - Proxy (PROXY_URL set): each retry grabs a fresh exit IP from the
      gateway, so old-school backoff is obsolete. Forces at least 3 attempts
      regardless of caller's max_retries, with tiny 1-2s sleeps between —
      block rate drops from ~76% to ~20% at the cost of ~2-3x bandwidth
      on blocked markets only.

    Rotates User-Agent on each retry to evade fingerprint-based blocks.

    Returns:
        requests.Response on success, None on failure.
    """
    caller_retries = max_retries if max_retries is not None else MAX_RETRIES
    # With a rotating proxy, low retry counts waste the proxy's whole purpose.
    # ~60% of 711proxies US residential IPs are Cloudflare-flagged today; 8 attempts
    # → 0.6^8 = 1.7% miss-rate. Each retry pulls a fresh exit IP from the gateway.
    retries = max(caller_retries, PROXY_MIN_RETRIES) if PROXY_URL else caller_retries

    for attempt in range(1, retries + 1):
        _rate_limit()

        # Rotate User-Agent on retries (only useful for stdlib requests path;
        # curl_cffi sessions manage their own headers via impersonation).
        if attempt > 1 and not getattr(session, "_impersonated", False):
            session.headers["User-Agent"] = random.choice(USER_AGENTS)

        try:
            # Pass proxies= explicitly — curl_cffi does not reliably honor
            # the session.proxies attribute, and a silent direct-connection
            # fallback gets 100%-blocked by Cloudflare from the datacenter IP.
            resp = session.get(url, timeout=30, proxies=PROXIES)
        except (_stdlib_requests.RequestException, Exception) as e:
            # curl_cffi raises curl_cffi.CurlError, not requests.RequestException
            logger.error("Request failed (attempt %d) for %s: %s", attempt, url, e)
            if attempt < retries:
                # Proxy: retry near-immediately (fresh IP). Direct: full backoff.
                time.sleep(1.0 + random.uniform(0, 1)) if PROXY_URL \
                    else time.sleep(INITIAL_BACKOFF * (2 ** (attempt - 1)))
                continue
            return None

        is_status_block = resp.status_code in (403, 429, 503)
        is_challenge = (resp.status_code == 200 and _is_challenge_response(resp))
        if is_status_block or is_challenge:
            blocks = _record_block()
            block_kind = "challenge-200" if is_challenge else str(resp.status_code)
            if attempt >= retries:
                logger.warning(
                    "Still blocked (%s) after %d attempts for %s",
                    block_kind, attempt, url,
                )
                return None
            if PROXY_URL:
                # Proxy gateway rotates on next request; no need to cool down
                # a specific IP. Tiny jitter only.
                backoff = 1.0 + random.uniform(0, 1.5)
            else:
                # Direct: exponential 60/120/240/480s
                backoff = INITIAL_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 10)
            logger.warning(
                "Got %s for %s (attempt %d/%d, blocks=%d) — backing off %.1fs",
                block_kind, url, attempt, retries, blocks, backoff,
            )
            time.sleep(backoff)
            continue

        if resp.status_code != 200:
            logger.warning("Non-200 status %d for %s", resp.status_code, url)
            return None

        _record_success()
        return resp

    return None
