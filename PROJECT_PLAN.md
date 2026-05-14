# Legacy Obituary Scraper — Project Plan

_Last updated: 2026-05-14 12:15 CT_

## Active Incident — 92% Block Rate (RESOLVED, verifying)

### Symptom
Daily Telegram report (2026-05-14 16:47 UTC, scraper-10): `BLOCKED 92% — 437/471
markets blocked`. Every listing fetch returned HTTP 403 through all 8 proxy
retries.

### Root cause
`curl_cffi`'s Chrome-120 TLS impersonation, when tunneled through the 711proxy
HTTP proxy, is **reliably rejected by Cloudflare** — 0% success in live testing.
The impersonated ClientHello does not survive the proxy CONNECT cleanly, and the
"claims-to-be-Chrome-but-isn't" fingerprint is itself a hard bot signal.

Secondary issue: `session.proxies` was set as a bare attribute (a `requests`
idiom that `curl_cffi` does not honor) — a silent direct-egress path.

Verified live:
- curl_cffi chrome120 via 711proxy → **0/10** success
- plain `requests` via 711proxy → **11/15 (~73%)** success per request
- 711proxy service itself is healthy; PROXY_URL was correctly set on all services

### Fix (commit 39900e0, deployed 2026-05-14)
- `create_session()`: use plain stdlib `requests` in PROXY mode; keep curl_cffi
  Chrome-120 impersonation only for DIRECT mode (datacenter IP still needs it).
- `polite_get()` / `s3_uploader`: pass `proxies=` explicitly on every request.
- Added `_proxy_self_test()` on startup — fetches an IP-echo endpoint and logs
  the exit IP so a silent direct-fallback can never go unnoticed again.

### Status — RESOLVED & VERIFIED 2026-05-14 12:25 CT
- [x] Root cause identified and verified
- [x] Fix committed + pushed to master (39900e0)
- [x] Deployed to all 10 scraper services
- [x] Manual job triggered on all 10 scrapers
- [x] Verified per-scraper: proxy self-test passed on all 10 (residential exit
      IP confirmed), parsed counts climbing (22–106 obits each within ~6 min),
      listing-block count 0–6 per scraper (was 437 on scraper-10 alone)
- [ ] Confirm next-day Telegram report shows <5% block rate (cron 10:00 UTC)

Note: per-retry `Got 403` still appears 0–9×/scraper — expected. The 711proxy
pool is ~27% Cloudflare-flagged per request; the 8-retry policy (fresh IP each
retry) absorbs it. Only a market that loses all 8 retries counts as blocked.

## Architecture (current)
- 10 cron scrapers (scraper-1..10), staggered 06:00–10:00 UTC, all Virginia/Docker
- Egress: 711proxy rotating US residential gateway via `PROXY_URL` env var
- Health Telegram 21:00 UTC; CCR self-healing agent 22:00 UTC
- See `CLAUDE.md` for full service IDs, schema, and env vars

## Backlog
- Step deferred: golf-tracker GitHub Actions sync (unrelated project, noted only)
- Consider lowering scraper-10's 471-market load — full runs take 6+ hours
