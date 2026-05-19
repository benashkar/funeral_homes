# Legacy Obituary Scraper — Project Plan

_Last updated: 2026-05-19 15:45 CT_

## Active Incident — Legacy.com Next.js Migration (RESOLVED, verifying)

### Symptom
- 2026-05-17 14:54 CT: `Obituary Scraper — oh,ks  Status: OK  Markets: 22 |
  Found: 0 | New: 0 | Errors: 0 | Bad: 0 | Dupes: 0` — silent zero, no blocks,
  no errors, just empty.
- Reproduced across every state batch over 2026-05-17 / 18 cron runs.

### Root cause
Legacy.com migrated listing pages to Next.js 14 with React Server Components.
Two breaking changes:

1. **URL format**: JSON-LD `mainEntity.itemListElement` URLs changed from
   `https://www.legacy.com/us/obituaries/{paper}/name/{slug}` to
   `https://www.legacy.com/person/{Name-Slug}-{personId}`. The
   `_LEGACY_OBIT_URL_RE` filter (`/us/obituaries/(?:[^/]+/)?name/`) rejected
   every one of them → 0 URLs accepted → 0 detail fetches → `Status: OK,
   Found: 0` because no error was raised, just an empty URL set.
2. **Detail JSON-LD shape**: The new `/person/` pages no longer expose
   top-level `Person` or `NewsArticle` blocks. The Person schema is nested
   in `CreativeWork.about` (name, givenName, familyName, birthDate,
   deathDate). **`funeral_home` and `obit_text` are gone from the detail
   page entirely** — they now live only on the listing-page result cards.

### Fix (PRs #7, #9, deployed 2026-05-19)

PR #7 (`fix/legacy-nextjs-person-urls`) — primary parser fix:
- `_LEGACY_OBIT_URL_RE` extended to match both URL formats.
- New `LegacyScraper._extract_listing_metadata()` harvests
  `{funeral_home, snippet, name}` from each
  `<article data-testid="result-card-N">` on the listing page.
  `scrape_today` accumulates the map across pages and passes the per-URL
  entry into `_parse_detail_page(listing_meta=...)`, which uses it to
  backfill `funeral_home` + `obit_text` when the detail page's JSON-LD
  lacks them.
- `obit_parser.parse_name` / `parse_dates` now read the `Person` schema
  nested under `CreativeWork.about` (new format), keeping legacy
  `Person` + `NewsArticle` paths as the primary source for old-format
  detail pages.

PR #9 (`fix/parse-name-strip-obituary-year`) — name cleanup hardening:
- Confirmed live in logs after PR #7 deployed:
  `"Margaret Janet Morss Herren Obituary 2026"` was written to
  `obituaries.deceased_name` as-is. Existing `_HEADLINE_SUFFIX_RE` required
  a literal `" - "` separator and missed the no-dash variant.
- Added `_clean_extracted_name()` — strips from the first 19YY/20YY token
  onward (with optional preceding "Obituary") plus trailing " Obituary"
  alone. Applied to **both** `Person.name` and the `NewsArticle.headline`
  fallback path in `parse_name()`.

PR #8 (`chore/backfill-clean-names`) — historical cleanup:
- `scripts/backfill_clean_names.py` strips the same pollution from existing
  `obituaries.deceased_name` rows.
- Cross-checks cleaned first/last against `church_scrapes.ref_ssa_names`
  and `church_scrapes.ref_census_surnames` on the same db99 instance;
  unrecognized names are logged `SUSPECT` but still cleaned (the year
  suffix is unambiguous junk).
- Ran with `--apply` on 2026-05-19; verification dry-run after returned
  the post-cleanup row count.

### Tuning applied 2026-05-19 (env vars on all 13 services)
After the URL filter fix, each scraper now makes ~50× more requests per
market (50 detail pages per market instead of zero), which hot-burned the
711proxy IP pool and produced `BLOCKED 50-90%` rates on cron runs. Also
discovered the three `cr-rescue-scraper-1/2/3` services had **no
`PROXY_URL` set at all** → `BLOCKED 100%` on the 2026-05-19 14:53 oh,ks
run (cr-rescue-2). Applied via `~/.local/bin/set_proxy_and_tune.py`:

| Service | Change |
|---|---|
| cr-rescue-scraper-1/2/3 | `PROXY_URL=…711proxy…` set (was unset) |
| funeral-homes-scraper-1..10 | `STATE_COOLDOWN=15→60`, `MAX_PROXY_ROTATIONS=3→6` |

### Status — RESOLVED & VERIFYING 2026-05-19 15:45 CT
- [x] Root cause identified and verified live
- [x] PR #7 (URL + harvest) merged + deployed to all 13 services
- [x] PR #9 (name cleanup) merged + redeployed
- [x] PR #8 (backfill script) merged; `--apply` run completed
- [x] Live verified: `oh-adams` returns 26 obits with clean names,
      death_date, funeral_home, and full 1.4 KB obit_text (was 0)
- [x] Mid-day Telegram alerts on 2026-05-19 show `Found:` counts of
      8,415 / 10,523 / 9,382 / 11,040 / 11,645 — fix is producing data
- [x] PROXY_URL set on cr-rescue-1/2/3; cooldown + rotations bumped on
      main scrapers
- [x] One-off run on cr-rescue-scraper-2 (oh,ks) post-tuning: exit-0 in
      6 min (was 24-min retry storm before fix)
- [ ] Confirm tomorrow's scheduled run shows block rate <30% across all
      13 services
- [ ] Watch for "Obituary YYYY" no-dash polluted names in next-day audit
      — should be zero after PR #9

### Lessons / process notes
- **Wrong-repo trap**: First attempted fix was opened against
  `benashkar/legacy-obit-scraper` (a similarly named but unused standalone
  repo, accidentally visible under `OneDrive/Projects/legacy-obit-scraper`)
  before discovering production code lives in `benashkar/funeral_homes`
  with crons named `funeral-homes-scraper-*`. Cost: ~30 min + one
  wrong-repo PR. Future: always trace the Telegram alert wording back to
  a Render service first, then to its `service.repo` URL — that's
  authoritative.
- **Silent-zero alerts are worse than errors**. `Status: OK, Found: 0,
  Errors: 0` looked benign for 2 days before the user noticed; an
  exception-raising failure would have paged within hours via the
  self-healing diagnostic agents. Consider a follow-up: dashboard
  canary that flags zero-found runs as suspicious when prior 7-day
  median is >0.

## Previous Incident — 92% Block Rate (RESOLVED, verifying)

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

### Progress snapshot — 2026-05-14 12:40 CT (~25 min into manual runs)
All 10 jobs still `running`, healthy:

| scraper | parsed | listing_blocked |
|---------|--------|-----------------|
| scraper-1  | 64  | 0 |
| scraper-2  | 139 | 0 |
| scraper-3  | 297 | 1 |
| scraper-4  | 288 | 9 |
| scraper-5  | 95  | 0 |
| scraper-6  | 86  | 0 |
| scraper-7  | 85  | 0 |
| scraper-8  | 65  | 0 |
| scraper-9  | 134 | 0 |
| scraper-10 | 155 | 0 |

Compare: scraper-10 alone had 437 blocked markets pre-fix. Full runs take
several hours; jobs will complete on their own and send per-run Telegrams.

## Architecture (current)
- 10 cron scrapers (scraper-1..10), staggered 06:00–10:00 UTC, all Virginia/Docker
- Egress: 711proxy rotating US residential gateway via `PROXY_URL` env var
- Health Telegram 21:00 UTC; CCR self-healing agent 22:00 UTC
- See `CLAUDE.md` for full service IDs, schema, and env vars

## Backlog
- Step deferred: golf-tracker GitHub Actions sync (unrelated project, noted only)
- Consider lowering scraper-10's 471-market load — full runs take 6+ hours
- **Zero-found canary** — dashboard alert when a state batch returns
  `Found: 0` on a market whose 7-day median is >0. Would have surfaced
  the 2026-05-17 Next.js incident hours instead of days late.
- **Auto-recovery of /person/ pages without listing context** — if a
  detail-page fetch for a /person/ URL happens without listing metadata
  (e.g. backfill re-fetches), funeral_home + obit_text will be NULL.
  Either re-fetch the parent listing page on demand, or store
  funeral_home / snippet in a side-table keyed by personId at first
  capture.
