# Legacy Obituary Scraper — Project Plan

_Last updated: 2026-05-22 02:00 CT (full-auto v4 trigger + audit dashboard + kill switch)_

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

### Follow-up shipped — Silent-zero canary (PR #11, 2026-05-19 18:30 CT)
The original 2026-05-17 alert (`Status: OK, Markets: 22, Found: 0, Errors: 0,
Blocked: 0`) sat for two days before anyone investigated because nothing in
the message screamed "broken." Added `_build_status()` in
`scheduler/run_daily.py` that returns
`WARNING: silent zero — possible parser break` when `total_found == 0` and
`markets_count >= 5` (threshold avoids false positives on small batches).
Priority: BLOCKED >= 50% still wins (the cause is already named), then
ERRORS, then this new WARNING, then DEGRADED, then OK. Unit-tested with 9
cases pinning the 2026-05-17 incident shape plus boundary conditions.
Merged + redeployed to all 13 services on commit `1fb4c9ed`.

### Follow-up shipped — Per-market quiet canary (PR #16, 2026-05-19 21:30 CT)
PRs #11 + #14 catch batch-level regressions. PR #16 catches the case
where ONE market silently goes to zero while siblings keep producing
data (batch total stays non-zero → previous canaries don't fire).
- New `find_quiet_markets()` SQL helper joins today's `scrape_log`
  rows where `obits_found = 0` against the prior 7 days for the same
  site_id. A market is reported quiet only if it had `obits_found > 0`
  on >= 3 of the prior 7 days — the 3-day floor avoids flagging
  legitimately-dormant rural counties.
- `run_daily.run()` appends a `Quiet markets: site_id(avg=N.N), ...
  +K more` line to the Telegram alert when any are found. Capped at
  25 entries; preview shows the top 5 by 7-day average.
- 3 new tests (happy path, empty-site_ids short-circuit, no-quiet case).
- Merged + redeployed to all 13 services on commit `d8725e4f`.

### Follow-up shipped — Missing-funeral-home canary (PR #14, 2026-05-19 20:45 CT)
The silent-zero canary doesn't catch a different shape that's specific to
the new `/person/` URL flow: listing-page result-card HTML is the **only**
source of `funeral_home` (and `obit_text`) for those URLs — the detail
pages no longer expose them. If Legacy.com tweaks the result-card markup
again, URLs and names would still flow but `funeral_home` would silently
become NULL for every obit. PR #14 plugs that gap:
- `scrape_market` now tracks a per-market `missing_fh` count and returns
  it in a 5-tuple. `run()` aggregates `total_missing_fh`.
- `_build_status` adds an `obits_missing_fh` parameter and emits
  `WARNING: N% missing funeral_home — listing-metadata harvest may be
  broken` when `total_found >= 50` and `missing-ratio >= 70%`. The
  50-obit floor avoids tiny-batch noise; the 70% ratio is well above
  the natural ~5% baseline of working runs.
- 7 new tests pin fire / no-fire cases and priority ordering against
  BLOCKED and silent-zero.
- Merged + redeployed to all 13 services on commit `b38be1de`.

### Status — RESOLVED & VERIFYING 2026-05-19 18:35 CT
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
- [x] Silent-zero canary deployed (PR #11) — future format breaks page
      via `WARNING:` Telegram instead of silent Status:OK
- [x] Missing-funeral-home canary deployed (PR #14) — catches
      listing-metadata regression that silent-zero would miss
- [x] Per-market quiet canary deployed (PR #16) — catches single-market
      silent zeros within a healthy batch
- [ ] Confirm tomorrow's scheduled run shows block rate <30% across all
      13 services
- [ ] Watch for "Obituary YYYY" no-dash polluted names in next-day audit
      — should be zero after PR #9
- [ ] If a real silent-zero ever recurs, confirm the canary fires by
      checking the Telegram alert wording (should start with `WARNING:`)

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

## Architecture (current — 2026-05-19)
- **13 cron services** total, all Virginia/Docker, autoDeploy OFF (deploy
  via Render API — `~/.local/bin/deploy_funeral_homes.py` batches them):
  - `funeral-homes-scraper-1..10` — main daily scrapes, staggered 06:00–10:00 UTC
  - `cr-rescue-scraper-1/2/3` — Cherry Road priority retries, 16:00–18:00 UTC
- **Env-var baseline (every service):**
  - `PROXY_URL=…711proxy…` (US-zone residential rotation, shared org account)
  - `STATE_COOLDOWN=60` seconds between states
  - `MAX_PROXY_ROTATIONS=6` per blocked request (fresh IP each retry)
- **Telegram alerts at end of every run** — see [`_build_status()`](scheduler/run_daily.py)
  for the status state machine. The silent-zero canary (PR #11) makes
  parser-break incidents surface within hours instead of days.
- Health Telegram 21:00 UTC; CCR self-healing agent 22:00 UTC.
- See `CLAUDE.md` for full service IDs, schema, and env vars.

## 2026-05-21 — first full-fleet wave verification + NM slug-rot fix

### Wave results vs yesterday's predictions
Tomorrow-morning's scheduled wave produced exactly the signal we
designed for:
- Block rate dropped from yesterday's 50-90% to **0-23%** across all
  alerts. `BLOCKED 100%` cr-rescue-2 incident yesterday → `DEGRADED:1`
  + 613 obits today. Proxy reload + tuning **validated**.
- Silent-zero pattern: **none**. Every alert showed real captures.
- scraper-12's first run (id,wa,nm): **9,288 found / 6,932 new** on
  116 markets — biggest haul of any single batch today.
- scraper-1 post-split (va,ak,ut): 10,827 found / 7,784 new on
  188 markets, DEGRADED:43.
- New `Quiet markets:` line surfacing real signal on every alert.

### NM slug-rot (PR #25)
scraper-12's first NM run flagged 14 quiet markets. Live probing
revealed two distinct failure modes:
- **5 markets 404** on the standard `{county}-county` URL pattern
  because Legacy.com restructured NM URLs from county-suffix to
  city-name slugs. Fixed 3 confirmed working slugs in markets.json:
  - nm-bernalillo: `bernalillo-county` → `albuquerque` (50 cards)
  - nm-sandoval: `sandoval-county` → `rio-rancho` (50 cards)
  - nm-do-a-ana: `do-a-ana-county` → `dona-ana` (36 cards)
  - nm-mckinley / nm-otero: 200 with 0 cards even on city slugs —
    probably genuinely low-volume rural; leaving canary to track.
- **5 markets 200 with JSON-LD ItemList data** (santa-fe / quay /
  chaves / valencia / san-juan) — transient blip on scraper-12's
  first run, not a slug issue. Canary will surface them again if
  they recur.

`site_id`s preserved across the slug change so historical
`scrape_log` and `obituaries` rows still join.

### Self-healing canary bridge (PR #26)
**Why:** The Layer-3 self-healing trigger
(`trig_01EzPjGTvjG6cK9BW5NDY3bz`, daily 22:00 UTC, model
`claude-sonnet-4-6`) reads `/health-status.json` to decide whether to
PR a fix. Its Recipe B (URL slug regression → PR) only knew about
`stale_markets` — markets that hadn't run for days. That **misses the
exact pattern** of today's NM incident: markets that DID run today,
returned 0, but had a healthy 7-day baseline.

**Fix:**
- `/health-status.json` now exposes a `quiet_markets[]` array from the
  same `find_quiet_markets()` helper PR #16 / PR #20 use.
- Status logic elevates to `ISSUES` when `quiet_markets` is non-empty.
- Recipe B's trigger condition updated to fire on either
  `quiet_markets[]` or `stale_markets[]`. The probe-and-classify flow
  now handles three outcomes: 404 → PR slug fix; 200-empty → report;
  200-healthy → report as transient.
- Tests +2: assert `quiet_markets` key present + status flips to
  ISSUES when canary fires (139 pass).
- Trigger manually fired post-deploy. If it sees the current 25 quiet
  markets, it will probe the top 5 by 7d avg and (per recipe) PR any
  that are 404 slug-rot.

Now any future NM-style URL restructure → next day at 22:00 UTC the
agent automatically opens a PR with the corrected slugs. No human in
the loop required for confirmed 404 patterns.

### Auto-merge gates wired (2026-05-22)
The trigger prompt now auto-merges Recipe B PRs when ALL four safety
gates pass:
1. **markets.json-only**: PR diff touches no other file (no code, no
   schema, no tests). Recipe C-bug PRs fail this and stay in review.
2. **≥10 cards verified**: WebFetch on the proposed new URL must
   return 200 with at least 10 `result-card-` markers or 10
   `mainEntity.itemListElement` items. If the PR fixes multiple
   markets, the weakest one must still clear the bar.
3. **Tests pass**: the `pytest tests/` run from Step 4 exited 0.
4. **Rate limit**: at most 3 auto-merges per UTC day (counts
   `[AUTO-MERGE]` strings in commit subjects on master). Prevents a
   runaway agent from shipping many bad slugs.

If all 4 pass → `gh pr merge --admin --squash` with subject suffix
`[AUTO-MERGE]` → trigger redeploys on all 15 scrapers so the new
slug takes effect. Telegram message explicitly says "AUTO-MERGED"
with per-gate PASS/FAIL audit lines.

If any gate fails → PR stays open with merge URL in the Telegram
(prior behavior).

Net result: confirmed 404 slug-rot incidents → detected at 22:00 UTC
→ fixed and live by ~22:30 UTC without human review. Anything more
ambiguous (200-empty pages, code bugs, mass blocks) still escalates.

### Full-auto v4 — all recipes auto-actionable + safety net (2026-05-22)
The earlier v3 only auto-merged Recipe B (markets.json slug fixes).
v4 expands autonomy across every recipe AND adds operator controls.

**Expanded auto-actions per recipe:**
- **Recipe A (mass IP block)** — agent now classifies as single-service
  vs fleet-wide. If 95% of `blocked_sample` falls on ONE scraper while
  other services ran fine: auto-suspend the affected service via
  `POST /v1/services/{id}/suspend`. Otherwise (fleet-wide, proxy
  exhausted): still escalates as before.
- **Recipe B (slug rot)** — same as v3 but Gate 2 lowered from
  ≥10 cards to ≥3 cards (captures low-volume rural fixes).
- **Recipe C-bug (backfill code regression)** — agent fixes the
  script AND adds a unit test in `tests/test_*.py`. Auto-merge
  eligible via new Gate 1B: diff touches ONLY
  `scripts/backfill_*.py` + a test file containing "regression"
  or a bug-reference comment.
- **Recipe D-dead (30+ day stale)** — agent auto-prepares a
  `markets.json` PR removing the orphaned `site_id` entry (keeping
  the historical `obituaries` + `scrape_log` rows intact for lookup).
  Auto-merge via Gate 1A.
- **Recipe D-transient (3-29 day stale)** — report only, could come
  back.
- **Rate limit** — Gate 4 raised from <3 to <6 auto-merges per UTC
  day to handle wider Legacy.com migration days.

**Safety net (PR #29 + trigger Step 0):**
- **Kill switch:** the agent's new Step 0 checks for
  `.self_healing_paused` at the repo root (on disk AND via the GitHub
  contents API). If present → PAUSED Telegram → exit. No diagnosis,
  no merge, no suspend.
- **`scripts/pause_self_healing.sh`** + **`scripts/resume_self_healing.sh`**
  — one-command toggle scripts.
- **`/self-healing-audit` dashboard page** — 30-day history of
  every `[AUTO-MERGE]` commit (SHA, files, date, author, linked to
  GitHub) + every currently-suspended scraper service. Pause status
  banner at top (red/green/yellow). Stdlib only — uses GitHub
  commits API + Render `/v1/services`, no `git` binary, no new
  package deps. Linked from the daily Telegram so audit trail is one
  click away.
- **Telegram audit lines:** every action now includes a per-gate
  PASS/FAIL breakdown plus the audit-page URL.
- **Existing guardrails preserved:** every auto-merge commit subject
  is tagged `[AUTO-MERGE]`. `git revert {sha} && git push` undoes
  any auto-merge in seconds.

Layered safety:
```
Layer 0: kill switch       — .self_healing_paused → all auto halted
Layer 1: 4 auto-merge gates — path scope + ≥3 cards + tests + rate
Layer 2: Telegram audit     — every action with diff + gate breakdown
Layer 3: dashboard audit    — /self-healing-audit 30-day history
Layer 4: human revert       — git revert {sha} && git push
```

Test count: 150 (started session at 42 / 2026-05-19 AM). 29 PRs
merged across funeral_homes (#7 → #29) over the three days.

## Operational notes
- **Render `/v1/logs` does NOT serve cron-job runtime logs after the run
  exits** — only build/deploy events surface. Verified 2026-05-19 by
  polling for 3.5 hours. Don't poll the API to verify a one-off cron;
  use `job.status` (succeeded/failed), the Telegram alert, or the Render
  dashboard UI instead.

## End-of-session snapshot (2026-05-19 → 2026-05-20)
- **PRs merged**: 12 functional/docs across both sessions.
  - Day-1 (2026-05-19): #7 parser, #8 backfill, #9 name cleanup, #11
    silent-zero canary, #14 missing-fh canary, #16 per-market quiet
    canary, plus docs #10/#12/#13/#15/#17.
  - Day-2 (2026-05-20): #18 end-of-session docs, #19 audit script,
    #20 `/scrape-health` dashboard view.
- **scraper-10 load split (2026-05-20):** scraper-10 was running ~6h
  with 471 markets. Split by sub-agent into:
    - scraper-10 (`crn-d7a6qmshg0os73bd3ai0`) — `SCRAPE_STATES=fl,wi,ca,sc`, 243 markets, `0 10 * * *`
    - scraper-11 (`crn-d86kkoojs32c73ephgt0`, NEW) — `SCRAPE_STATES=la,mt,wv,nd`, 228 markets, `0 11 * * *`
  All 13 env vars cloned from scraper-10 onto scraper-11 (PROXY_URL,
  STATE_COOLDOWN=60, MAX_PROXY_ROTATIONS=6, etc.). 1h schedule offset
  so they don't overlap. Total fleet is now 14 scrapers (10 main + 1
  new + 3 cr-rescue).
- **Dashboard health view (PR #20, 2026-05-20):** added
  `/api/scrape-health` JSON endpoint + `/scrape-health` HTML heatmap
  (rows=states, cols=last 7 days, cells red/yellow/green vs
  per-state median) + "Quiet markets" section. Reuses
  `find_quiet_markets()` from PR #16 — no duplication. Live at
  https://cr-obituaries-dashboard.onrender.com/scrape-health and
  already surfacing real quiet-market signal (sc-greenville,
  va-henrico, ny-albany among the first hits).
- **Audit script (PR #19):** `scripts/audit_data_quality.py`
  Telegrams a concise report of polluted-name residue, last-24h NULL
  funeral_home / NULL obit_text rates, and per-state capture counts.
  Ran post-deploy; output went to Telegram per the Render cron-log
  limitation.
- **Proxy reloaded by operator (2026-05-20 AM CT):** addresses the
  root cause of yesterday's `BLOCKED 50–90%` Telegram alerts. With
  fresh 711proxy credits + the env-var tuning, tomorrow's wave is
  the clean reading. If block rate drops below ~20% the tuning is
  sufficient.
- **Proxy + tuning validated 2026-05-20 17:17 UTC:** cr-rescue-2 (oh,ks,
  22 markets) one-off ran 59 minutes — vs yesterday's 6-min fast-fail
  on banned IPs OR 24-min retry-storm pre-tuning. A 59-min runtime is
  the signature of a real working scrape (~2.5min/market). The
  reloaded 711proxy account + STATE_COOLDOWN=60 + MAX_PROXY_ROTATIONS=6
  is sufficient. Compare point: yesterday's 19:53 UTC
  `Markets: 22 | Found: 0 | Blocked: 22` run.
- **scraper-1 also split (2026-05-20 PM):** overload-audit agent
  flagged scraper-1 as the only remaining service over the 300-mk
  SPLIT threshold (304 markets, va,id,wa,nm,ak,ut, ~7.6h est). Split
  into:
    - scraper-1 (`crn-d6li6f5m5p6s73chtqh0`) — `SCRAPE_STATES=va,ak,ut`, 188 markets, `0 6 * * *`
    - scraper-12 (`crn-d8707mjtqb8s7381kfhg`, NEW) — `SCRAPE_STATES=id,wa,nm`, 116 markets, `0 12 * * *`
  Fleet now totals **15 services** (12 main + 3 cr-rescue). Audit
  report at `C:\Users\cashk\tmp-clone\fh_overload_audit\OVERLOAD_AUDIT.md`;
  scrapers 2 and 3 are the next-most-likely to need splitting (298 and
  278 markets respectively) but stay in the WATCH band for now.
- **Name-cleanup hardening (PR #22, 2026-05-20 13:55 CT):** defensive
  variants added for 7 plausible pollution shapes Legacy.com might
  emit (life-span in parens, leading `OBITUARY:`, em-dash separator,
  comma-year-range, dash-separated life span, dash+FH without year,
  all-lowercase). `_clean_extracted_name` extended with a 4-step
  pipeline + FH-keyword whitelist for the no-year case. Guardrails
  pinned: legitimate names like `Smith, John` / `John Smith, Jr.` /
  `Mary-Lou O'Brien` / `José García-López` pass through unchanged.
  Tests: 137 pass (+10 new). Deployed to all 14 scrapers on commit
  `88f64731`.
- **Live verification across 10 metros (2026-05-20):** sub-agent ran
  the post-fix parser against ny-erie, ca-los-angeles, fl-miami-dade,
  tx-harris, il-cook, pa-philadelphia, ga-fulton, mi-wayne,
  mn-hennepin, wa-king. Verdict: **all 10 markets healthy.** Key
  finding: the Next.js migration is partial — 5/10 metros serve the
  new `/person/` format (LA, Miami, Cook, Philly, Wayne; 100%
  listing-meta coverage), the other 5 still serve the legacy
  `/us/obituaries/.../name/` shape (NY/Erie, Harris, Fulton, Hennepin,
  King) which doesn't need listing-meta because detail-page JSON-LD
  still carries name + body + funeralHome. Both code paths are
  production-ready. Minor follow-up: `tx-harris` + `ga-fulton` are
  missing from `config/markets.json` despite being major metros —
  worth adding.
- **Test count**: 127 unit tests pass (122 + 5 new in PR #20).
- **First production exercise of canaries**: tomorrow's 06–11 UTC
  scheduled cron runs (scraper-1 through scraper-11). The
  previously-silent 2026-05-17 incident would now page as
  `WARNING: silent zero` if it recurred. Per-market `Quiet markets:`
  line surfaces single-market regressions. `WARNING: N% missing
  funeral_home` catches listing-metadata-harvest breakage.

## Backlog
- Step deferred: golf-tracker GitHub Actions sync (unrelated project, noted only)
- ~~**Lower scraper-10's 471-market load**~~ — _shipped 2026-05-20_. Split
  into scraper-10 (243 mk, fl,wi,ca,sc) + scraper-11 (228 mk, la,mt,wv,nd).
- ~~**Audit the other 5 scrapers for similar overload**~~ — _done
  2026-05-20_. Found: only scraper-1 was over the SPLIT threshold
  (now split). Scrapers 2 and 3 are the next-most-likely to tip but
  still in the WATCH band. Audit report at
  `C:\Users\cashk\tmp-clone\fh_overload_audit\OVERLOAD_AUDIT.md`.
- ~~**Add tx-harris + ga-fulton to markets.json**~~ — _shipped
  2026-05-20 (PR #24)_. Houston + Atlanta now in the registry.
- **Auto-revert on harm detection** — not built yet. If an
  auto-merged slug fix makes a market WORSE the day after merge
  (today's obits_found < pre-merge 3-day avg / 2), the next-day
  agent run should auto-revert that commit. Requires the agent to
  track which `[AUTO-MERGE]` SHAs it owns and parse the affected
  site_ids from each diff. Design-only at this point; ship when a
  real bad-auto-merge incident motivates it.
- **Watch scrapers 2 and 3** — closest to the 300-market SPLIT
  threshold (298 and 278 markets). Re-audit after the next CR
  market wave.
- ~~**Zero-found canary**~~ — _shipped 2026-05-19 (PR #11)_. Now emits
  `WARNING: silent zero` Telegram when a batch returns Found=0 with
  Markets>=5 and no blocks/errors.
- ~~**Per-market 7-day check**~~ — _shipped 2026-05-19 (PR #16)_.
  `find_quiet_markets()` flags any single market that scraped 0 today
  but had >=3 active days in the prior 7. Surfaces as a
  `Quiet markets:` line in the Telegram alert.
- ~~**Missing-funeral-home canary**~~ — _shipped 2026-05-19 (PR #14)_.
  Now emits `WARNING: N% missing funeral_home` when total_found >= 50
  and >= 70% of obits come back with funeral_home=NULL. Guards the
  /person/-page-only listing-metadata harvest path.
- **Auto-recovery of /person/ pages without listing context** — if a
  detail-page fetch for a /person/ URL happens without listing metadata
  (e.g. backfill re-fetches), funeral_home + obit_text will be NULL.
  Either re-fetch the parent listing page on demand, or store
  funeral_home / snippet in a side-table keyed by personId at first
  capture.
