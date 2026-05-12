# CLAUDE.md — Legacy Obituary Scraper
## Project: CR Community News — Obituary Automation

---

## Project Overview

Daily obituary scraper targeting Legacy.com for ~30 initial CR Community News markets
(expanding to 1,200+ nationwide). Repurposed from:
- **ahplummer/LegacyObituaries** (MIT) — BeautifulSoup daily scraper, core pattern
- **comp-strat/scrape_obituaries** (GPL-3.0) — URL discovery and text extraction reference

Output: Obituaries stored in MySQL keyed to CR Community News site IDs, ready
for editorial automation (Lumen CMS pipeline).

---

## Tech Stack
- Python 3.10+, requests + BeautifulSoup4, lxml
- MySQL (existing CR infra), mysql-connector-python
- Cron daily at 6 AM CT (Render.com or EC2)
- .env for all credentials

---

## Directory Structure
```
legacy-obit-scraper/
├── CLAUDE.md
├── requirements.txt
├── .env.example
├── config/
│   └── markets.json           # All target markets + Legacy URL slugs
├── scraper/
│   ├── url_builder.py         # Builds Legacy.com URLs from markets.json
│   ├── legacy_scraper.py      # Main BeautifulSoup scraper
│   ├── obit_parser.py         # Extracts structured fields from obit page HTML
│   └── db_writer.py           # MySQL upsert logic
├── scheduler/
│   └── run_daily.py           # Cron entry point — loops all markets
├── utils/
│   ├── logger.py
│   └── rate_limiter.py        # 2-sec polite delay + User-Agent header
├── tests/
│   ├── test_url_builder.py
│   ├── test_obit_parser.py
│   └── test_db_writer.py
└── sql/
    └── schema.sql
```

---

## Legacy.com URL Pattern (2026)
```
County: https://www.legacy.com/us/obituaries/local/{state}/{county-name}-county
City:   https://www.legacy.com/us/obituaries/local/{state}/{city-name}
```

---

## markets.json — Phase 1 (31 markets)
```json
[
  {"site_id":"tx-brown",        "state":"texas",         "legacy_slug":"brown-county",       "type":"county"},
  {"site_id":"tx-erath",        "state":"texas",         "legacy_slug":"erath-county",       "type":"county"},
  {"site_id":"tx-palo-pinto",   "state":"texas",         "legacy_slug":"palo-pinto-county",  "type":"county"},
  {"site_id":"tx-somervell",    "state":"texas",         "legacy_slug":"somervell-county",   "type":"county"},
  {"site_id":"tx-runnels",      "state":"texas",         "legacy_slug":"runnels-county",     "type":"county"},
  {"site_id":"tx-ellis",        "state":"texas",         "legacy_slug":"ellis-county",       "type":"county"},
  {"site_id":"tx-jim-wells",    "state":"texas",         "legacy_slug":"jim-wells-county",   "type":"county"},
  {"site_id":"tx-grayson",      "state":"texas",         "legacy_slug":"grayson-county",     "type":"county"},
  {"site_id":"tx-fannin",       "state":"texas",         "legacy_slug":"fannin-county",      "type":"county"},
  {"site_id":"oh-franklin",     "state":"ohio",          "legacy_slug":"franklin-county",    "type":"county"},
  {"site_id":"oh-obetz",        "state":"ohio",          "legacy_slug":"obetz",              "type":"city"},
  {"site_id":"oh-canal-winch",  "state":"ohio",          "legacy_slug":"canal-winchester",   "type":"city"},
  {"site_id":"oh-hamilton-twp", "state":"ohio",          "legacy_slug":"hamilton-township",  "type":"city"},
  {"site_id":"oh-lithopolis",   "state":"ohio",          "legacy_slug":"lithopolis",         "type":"city"},
  {"site_id":"oh-lockbourne",   "state":"ohio",          "legacy_slug":"lockbourne",         "type":"city"},
  {"site_id":"oh-groveport",    "state":"ohio",          "legacy_slug":"groveport",          "type":"city"},
  {"site_id":"oh-madison-twp",  "state":"ohio",          "legacy_slug":"madison-township",   "type":"city"},
  {"site_id":"oh-west-columbus","state":"ohio",          "legacy_slug":"west-columbus",      "type":"city"},
  {"site_id":"oh-lincoln-vil",  "state":"ohio",          "legacy_slug":"lincoln-village",    "type":"city"},
  {"site_id":"oh-prairie-twp",  "state":"ohio",          "legacy_slug":"prairie-township",   "type":"city"},
  {"site_id":"oh-westgate",     "state":"ohio",          "legacy_slug":"westgate",           "type":"city"},
  {"site_id":"oh-galloway",     "state":"ohio",          "legacy_slug":"galloway",           "type":"city"},
  {"site_id":"oh-hilliard",     "state":"ohio",          "legacy_slug":"hilliard",           "type":"city"},
  {"site_id":"oh-grove-city",   "state":"ohio",          "legacy_slug":"grove-city",         "type":"city"},
  {"site_id":"oh-urbancrest",   "state":"ohio",          "legacy_slug":"urbancrest",         "type":"city"},
  {"site_id":"oh-comm-point",   "state":"ohio",          "legacy_slug":"commercial-point",   "type":"city"},
  {"site_id":"ma-grafton",      "state":"massachusetts", "legacy_slug":"grafton",            "type":"city"},
  {"site_id":"ma-millbury",     "state":"massachusetts", "legacy_slug":"millbury",           "type":"city"},
  {"site_id":"ma-sutton",       "state":"massachusetts", "legacy_slug":"sutton",             "type":"city"},
  {"site_id":"ma-holden",       "state":"massachusetts", "legacy_slug":"holden",             "type":"city"},
  {"site_id":"ga-claxton",      "state":"georgia",       "legacy_slug":"claxton",            "type":"city"}
]
```

---

## MySQL Schema (sql/schema.sql)
```sql
CREATE TABLE IF NOT EXISTS obituaries (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    site_id         VARCHAR(50)   NOT NULL,
    legacy_url      VARCHAR(500)  NOT NULL UNIQUE,   -- dedup key
    deceased_name   VARCHAR(255),
    published_date  DATE,
    death_date      DATE,
    funeral_home    VARCHAR(255),
    obit_text       LONGTEXT,
    scraped_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    sent_to_cms     TINYINT(1)    DEFAULT 0,
    INDEX idx_site_id (site_id),
    INDEX idx_published_date (published_date),
    INDEX idx_sent_to_cms (sent_to_cms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scrape_log (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    site_id     VARCHAR(50) NOT NULL,
    run_date    DATE        NOT NULL,
    obits_found INT         DEFAULT 0,
    obits_new   INT         DEFAULT 0,
    errors      TEXT,
    run_at      DATETIME    DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run_date (run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## Scraper Architecture

### legacy_scraper.py
Class `LegacyScraper(market_dict)`. Method `scrape_today()`:
1. Build listing URL from market dict
2. GET listing page with polite headers
3. Find all obit card links on the page (BeautifulSoup selectors)
4. For each URL not already in DB: GET full obit page
5. Call obit_parser functions to extract structured fields
6. Return list of dicts

### obit_parser.py
Stateless parsing functions. Input: BeautifulSoup soup of a single obit page.
- `parse_name(soup)` → str
- `parse_dates(soup)` → dict `{'published': date|None, 'death': date|None}`
- `parse_funeral_home(soup)` → str|None
- `parse_obit_text(soup)` → str (full text, stripped of HTML tags)

### db_writer.py
- `upsert_obit(obit_dict, site_id)` → bool (True=new, False=duplicate)
  Uses `INSERT IGNORE` on `legacy_url` unique key
- `log_run(site_id, found, new, errors)` → writes to scrape_log

### scheduler/run_daily.py
- Loads markets.json
- Loops each market sequentially (no concurrency — polite)
- Instantiates LegacyScraper, calls scrape_today(), passes to db_writer
- Prints per-market summary to stdout

---

## Polite Scraping Rules
- `time.sleep(2)` between every request — do not remove this
- User-Agent: `"Mozilla/5.0 (compatible; CR-NewsBot/1.0; +https://crcommunity.news)"`
- On 429 or 503: wait 60 seconds, retry once, then log and skip
- Never run markets concurrently — one at a time only

---

## Coding Standards
- **Docstrings on every function** — what it does, params, return value
- **Inline comments** on CSS selectors and SQL — explain what you're targeting and why
- **Named constants** for all selectors and queries at top of each file (no magic strings)
- **Explicit error handling** — separate `requests` exceptions from parse errors
- **No silent failures** — unparseable fields log a WARNING and store None, never crash
- **Tests** — every parser function needs 2+ unit tests (happy path + missing field)

---

## Cron (6 AM CT daily)
```bash
0 6 * * * cd /app/legacy-obit-scraper && python scheduler/run_daily.py >> /var/log/obit_scraper.log 2>&1
```

---

## Known Gotchas
- Legacy.com HTML selectors change occasionally — if scraper goes silent, inspect the
  listing page first before assuming a bigger issue
- Some small OH townships may not have city-level pages — they fall through to county-level;
  Franklin County (`oh-franklin`) will catch most Columbus-area obits anyway
- Rural TX counties (Somervell, Runnels) expect 0-2 obits/day — this is normal
- Increase sleep to 3-4 seconds if you see 429s in production logs

---

## Phase 2 (after Phase 1 validated)
- Add Dignity Memorial fallback for rural TX markets with thin Legacy coverage
- Wire `sent_to_cms=0` records into Lumen CMS push pipeline
- Generate markets.json from CR site database to scale to 1,200 markets
- Parameterize cron by state for regional deploys

---

## Reference Repos
- https://github.com/ahplummer/LegacyObituaries (MIT — core pattern)
- https://github.com/comp-strat/scrape_obituaries (GPL-3.0 — URL discovery reference)

---

## Current Production Architecture (as of 2026-05-12)

The Phase 1 scope above is historical. Production state:

- **10 scraper cron services** on Render (Virginia, Docker), names `funeral-homes-scraper-1` … `-10`, staggered 06:00–10:00 UTC. All `autoDeploy: no` — deploys must be triggered via Render API.
- **~1,500 markets** loaded from `config/markets.json`, all 50 states + DC. Each scraper assigned a subset of states via `SCRAPE_STATES` env var.
- **Dashboard:** https://cr-obituaries-dashboard.onrender.com (Flask, web service). Exposes `/health-status.json` with `scrape_today`, `backfill`, `error_sample`, `stale_markets`.
- **DB:** MySQL `funeral_homes` on db99 (us-east-1). Tables: `obituaries`, `scrape_log`, `funeral_homes`.
- **S3 photo storage:** `hamster-storage1/claude/legacy/{site_id}/{id}{,_16x9}.jpg`.
- **Daily health Telegram:** 21:00 UTC.
- **CCR self-healing trigger:** 22:00 UTC daily (1h after health report).

## Anti-Bot Stack

Legacy.com fronts with Cloudflare. Scraper layer combats this with:

1. **TLS fingerprint impersonation** — `curl_cffi` with `impersonate="chrome120"` mimics Chrome's JA3/JA4 + HTTP/2 framing. The default `requests` library is blocked on sight.
2. **Rotating residential proxy** — `PROXY_URL` env var on every scraper service points to `http://USER981811-zone-custom-country-us:d445c2@global.rotgb.711proxy.com:10000`. The gateway URL is static; each TCP connection grabs a fresh US residential exit IP. No daily handoff required — just keep 711proxies credits funded.
3. **Cloudflare challenge detection** — `utils/rate_limiter.py` recognizes three challenge variants and retries through fresh proxy IPs:
   - HTTP 403/429/503 (classic block status codes)
   - HTTP 200 with `Just a moment...` / `cf-challenge` / `challenge-platform` / `cdn-cgi/challenge` in body
   - HTTP 200 with body >50 KB and no `<title>` tag anywhere (silent Legacy/CF interstitial)
4. **Retry policy:** in proxy mode, minimum 8 attempts per request (`PROXY_MIN_RETRIES`). With ~60% of US residential IPs Cloudflare-flagged on any given day, 8 attempts → 0.6^8 = ~1.7% expected miss-rate.
5. **Politeness:** `MIN_DELAY=6s` in direct mode, `MIN_DELAY_PROXY=1s` in proxy mode (gateway IP rotates per request, no per-IP cooldown needed).

## Recent Incidents

### 2026-05-12 — 56% blocked rate despite proxy
- **Symptom:** Daily run hit 853/1516 markets blocked (56%) despite all 10 scrapers having `PROXY_URL` configured and `curl_cffi` in use.
- **Root cause:** `polite_get` only retried on HTTP 403/429/503. Cloudflare returned a third challenge variant — HTTP 200 + ~720 KB body + no `<title>` — that the rate_limiter accepted as success. The scraper then parsed 0 obits from the challenge page and recorded `found=0`.
- **Fix:** PR #5 (`fix/cloudflare-200-challenge-retry`, commit `cea9185`). Added `_is_challenge_response()` detecting no-title 200s and Cloudflare body markers, treats matches as retryable blocks, bumped `PROXY_MIN_RETRIES` 3 → 8, dropped `MIN_DELAY_PROXY` 6s → 1s.
- **Tests:** 10 new in `tests/test_rate_limiter.py`. Full suite (99 tests) green.
- **Deploy:** Triggered via Render API on all 10 scraper services.
- **Verification:** One-shot remote agent `trig_01Shgw7UQM6ZKQ2pxRP1R1LY` fires 2026-05-13T22:00Z to compare blocked_pct against the 56% baseline and Telegram-report the outcome.

## Operational Notes

- **All actions must use APIs** (Render API, gh CLI, AWS Secrets Manager). Never ask the user to manually configure a service.
- **711proxies maintenance:** when credits run out the proxy returns auth errors and the scraper falls back to direct, which hits Cloudflare hard. Reload credits at https://711proxy.com.
- **If `blocked_pct` rises again:** first verify proxy works (`curl -x global.rotgb.711proxy.com:10000 -U USER981811-zone-custom-country-us:d445c2 ipinfo.io`). If it does, inspect `error_sample` patterns from `/health-status.json` — a new challenge variant means adding markers to `_CHALLENGE_MARKERS` in `utils/rate_limiter.py`.
- **Render cron services:** triggering a deploy does NOT kill an in-flight job. The running container completes on the old image; the next scheduled invocation picks up the new image. Render only allows one concurrent job per cron service — manual smoke-test jobs queued while a daily run is active will fail immediately.