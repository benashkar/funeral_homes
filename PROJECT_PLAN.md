# CROSS-PROJECT INCIDENT - 2026-09-02: db99 connection exhaustion

_Last updated: 2026-09-02_

## What happened

db99 is ONE MySQL instance shared by every project. On 2026-09-02 it reached
**1,286 of 1,289 connections** and began refusing new ones with
`ER_CON_COUNT_ERROR` / errno 1040. Scrapes broke in five unrelated projects.
1,071 connections have been refused since April. `wait_timeout` is **28800
(eight hours)**, so a connection that is never closed holds its slot for most
of a day.

**funeral_homes was the largest single leaker: 96 connections, 95 of them
`Sleep`** at the time of measurement.

## What was actually wrong in THIS project

Measured before any change (`information_schema.PROCESSLIST`):

| database | total | sleeping | oldest |
|---|---|---|---|
| **funeral_homes** | **96** | **95** | 5,021s |
| cr_sources | 22 | 21 | 28,070s |
| college_athletes | 15 | 13 | 1,802s |
| crime | 14 | 9 | 24,910s |
| church_scrapes | 2 | 2 | 864s |

### 1. `dashboard/app.py` - raw unpooled connection per call, zero `finally`

`_get_conn()` opened a raw `mysql.connector` connection on every call. Ten call
sites, ten `conn.close()` calls, and **zero `finally:` blocks in the module**.

Every route was shaped:

```python
try:
    conn = _get_conn()
    ...queries...
    conn.close()          # only reached when nothing raises
except Exception as e:
    return error, 503     # connection stranded for 8 hours
```

Because each route swallows its own exception, the leak was completely silent.
This created a positive feedback loop: connection pressure caused query errors,
each error skipped a `close()`, and the leak increased the pressure. That is
why this project *accumulated* during the incident rather than merely suffering
from it.

Worst sites: `/health` (polled by Render constantly, and it returns
`status: ok` even on DB failure so nothing ever restarts the leaking pod),
`/health-status` (ten queries before its close), `/` and `/health-status.json`.

### 2. `dashboard/db.py` - `close_db()` defined but NEVER registered

The whole-repo occurrence count of `close_db` was **1**: its own `def`. No
`teardown_appcontext`, no `teardown_request`. `get_db()` therefore leaked one
connection per request.

**Contributed zero connections in practice** - `dashboard/db.py` and
`dashboard/routes/` were never committed to git (untracked), so they have never
been deployed, and no `register_blueprint` call exists anywhere. Fixed anyway as
a latent trap.

### 3. `scraper/db_writer.py` - pool_size=8 on a single-threaded pipeline

`MySQLConnectionPool` opens **every** connection eagerly at construction.
Measured 2026-09-02: building a `pool_size=8` pool took funeral_homes from 97
to 105 live connections **before a single query ran**. Checking a connection
out did not change the count, confirming pooled `.close()` returns rather than
closes.

The repo has no `ThreadPoolExecutor`, no `threading.Thread`, and `run_daily.py`
holds exactly one connection at a time - measured high-water mark of concurrent
checkouts is **1**. So 7 of the 8 were pure waste, across ~15 scraper services.

Worse, `scrape_market()` held a pooled connection across `enrich_funeral_home()`
and `upload_photo()`, both of which make HTTP calls. A network exception there
never returned the connection; after 8 such failures the pool was exhausted and
every remaining market failed with `PoolError`, writing nothing.

## What was fixed

- **`dashboard/app.py`**: `_get_conn()` now registers each connection on Flask's
  `g`, and `create_app()` registers `@app.teardown_appcontext` to close
  everything registered. This is a safety net rather than 11 separate patches -
  it also covers routes added in future, which patching each call site does not.
  Outside an app context (scripts importing the module) behaviour is unchanged.
  Explicit `conn.close()` calls remain and are now harmless no-ops.
- **`dashboard/app.py`**: explicit `try/finally` on `/health` and
  `/health-status` as well, since those are the constantly-polled hot paths.
- **`dashboard/app.py` + `scraper/db_writer.py`**: connect retries **only**
  errno 1040 with linear backoff (2s, 4s, 6s). Every other error raises
  immediately - retrying everything turns a real fault into a slow one.
- **`dashboard/db.py`**: added `init_app(app)` which registers `close_db` via
  `teardown_appcontext`; called from `create_app()`.
- **`scraper/db_writer.py`**: `POOL_SIZE` 8 -> 2, plus `release_connection()`
  and a `pooled_connection()` context manager.
- **`scheduler/run_daily.py`**: every pooled checkout now returns in a
  `finally`, including the HTTP-spanning window in `scrape_market()`.
- **`tests/test_db99_connection_teardown.py`**: 4 regression tests. Verified to
  FAIL against the pre-fix code and PASS after (full suite 118 passed).

## Still open

- The real fix is a lower `wait_timeout`, which needs an RDS parameter-group
  change - out of scope for this repo.
- `cherry-road-dashboard`'s `cr-db99-conn-reaper` cron is a net, not a fix.

---

# Legacy Obituary Scraper — Project Plan

_Last updated: 2026-05-14 12:40 CT_

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
