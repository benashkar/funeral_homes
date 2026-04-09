"""Quick test of the rate limiter against Legacy.com.

Prints the rate limiter constants AND attempts a few requests to
verify behavior.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.rate_limiter as rl
from utils.rate_limiter import polite_get, create_session

print("=" * 60)
print("RATE LIMITER CONFIG")
print("=" * 60)
print(f"MAX_RETRIES:    {rl.MAX_RETRIES}")
print(f"MIN_DELAY:      {rl.MIN_DELAY}s")
print(f"INITIAL_BACKOFF:{rl.INITIAL_BACKOFF}s")
print(f"USER_AGENTS:    {len(rl.USER_AGENTS)} configured")
print()

# Test 3 different counties
TEST_URLS = [
    "https://www.legacy.com/us/obituaries/local/ohio/allen-county",
    "https://www.legacy.com/us/obituaries/local/missouri/cooper-county",
    "https://www.legacy.com/us/obituaries/local/texas/jim-wells-county",
]

session = create_session()
print("=" * 60)
print("TEST REQUESTS")
print("=" * 60)
for url in TEST_URLS:
    start = time.monotonic()
    print(f"\nFetching: {url}")
    resp = polite_get(session, url)
    elapsed = time.monotonic() - start
    if resp:
        print(f"  SUCCESS: {resp.status_code} in {elapsed:.1f}s, body={len(resp.text)} bytes")
    else:
        print(f"  FAILED after {elapsed:.1f}s")

print("\nDone.")
