"""Symptom-based 711proxy health probe.

711proxy exposes NO balance/usage API (only a proxy-list generation API), so we
cannot read remaining GB directly. But the failure mode we care about — the GB
balance hitting 0 (or the account being suspended) — manifests as the gateway
refusing ALL traffic: ProxyError / 407 auth-reject / connection reset on every
request. That is distinct from a *target* site's Cloudflare challenge (HTTP 403
"Just a moment"), which is normal and not a proxy problem.

This probe sends a few requests through PROXY_URL to stable, proxy-friendly
canary endpoints (NOT Cloudflare-walled). If none succeed — and it's not a
Cloudflare wall — the proxy account is almost certainly out of GB / suspended,
and we Telegram an alert before the whole scraper fleet silently goes dark.

Run daily (dedicated cron). Needs PROXY_URL in env (already set on the fleet).
Telegram creds via utils.telegram (Biscotcho). Exit 1 if the proxy looks dead.
"""

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.telegram import send_message
from utils.logger import get_logger

logger = get_logger("proxy_health")

# Proxy-friendly IP-echo endpoints that should ALWAYS succeed through a healthy
# residential proxy (no Cloudflare, no bot wall).
CANARIES = ["https://api.ipify.org?format=json", "http://httpbin.org/ip"]
ATTEMPTS_PER_CANARY = 3


def probe(proxy_url):
    proxies = {"http": proxy_url, "https": proxy_url}
    healthy, symptoms = 0, []
    for url in CANARIES:
        for _ in range(ATTEMPTS_PER_CANARY):
            try:
                r = requests.get(url, proxies=proxies, timeout=20)
                body = (r.text or "").strip()
                if r.status_code == 200 and body:
                    healthy += 1
                elif r.status_code == 403 and "just a moment" in body.lower():
                    # Cloudflare wall on the target — NOT a proxy-balance problem.
                    healthy += 1
                else:
                    symptoms.append(f"{url} -> HTTP {r.status_code}")
            except requests.exceptions.ProxyError as e:
                symptoms.append(f"{url} -> ProxyError: {str(e)[:80]}")
            except Exception as e:
                symptoms.append(f"{url} -> {type(e).__name__}: {str(e)[:60]}")
    return healthy, symptoms


def main():
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if not proxy_url:
        logger.error("[ERR] PROXY_URL not set")
        sys.exit(2)

    healthy, symptoms = probe(proxy_url)
    total = len(CANARIES) * ATTEMPTS_PER_CANARY
    logger.info("[OK] proxy probe: %d/%d healthy", healthy, total)

    if healthy == 0:
        host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        send_message(
            "<b>711proxy Health — FLEET RISK</b>\n"
            f"<b>Status: PROXY DOWN</b>\n"
            f"0/{total} canary requests succeeded through <code>{host}</code> — "
            "this is NOT a Cloudflare wall, so the proxy account is almost "
            "certainly <b>out of GB or suspended</b>. Every scraper that routes "
            "through it will go dark.\n\n"
            "<b>Fix:</b> reload GB on the 711proxy plan and allocate it to the "
            "<code>USER981811-zone-custom</code> zone, then re-test.\n\n"
            "<b>Symptoms:</b>\n" + "\n".join(f"  • {s}" for s in symptoms[:6])
        )
        logger.error("[ERR] proxy appears DEAD — alerted")
        sys.exit(1)

    if symptoms:
        logger.warning("[WARN] %d/%d healthy, partial symptoms: %s",
                       healthy, total, symptoms[:3])


if __name__ == "__main__":
    main()
